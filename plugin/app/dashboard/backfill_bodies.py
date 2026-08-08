"""Fill in body_text for rows that were ingested without it. Resumable, read-only, honest.

WHY THIS EXISTS. `body_text` was NULL on every row in this store for a year. The viewer did
not appear broken, because it falls back to RE-FETCHING each message on demand - so the
sandboxed reader, the image blocking and the tracking-host report all worked, right up until
the message was no longer in the mailbox. A feature that quietly depends on a network round
trip against mail that ages out is not a working feature; it is one that will fail later,
for reasons nobody will connect to this.

MEASURED BEFORE BUILDING, and the measurement changed the plan. The assumption was that old
mail would be gone and only a fix-forward was worth doing. A stratified sample across every
month in the store came back 43 of 43 retrievable - 100%, including BINNED mail from over a
year earlier, because trash goes to the provider's Trash folder and stays there. So the
history is recoverable and this is worth running.

(The first version of that probe reported 0% across every month, including mail from three
days earlier that had visibly opened in the viewer an hour before. It was parsing `find`'s
--out file as JSON, which is the raw message. A zero from an instrument that never fired,
produced while measuring for exactly that class of defect. Nothing but the contradiction with
something already seen would have caught it.)

WHAT IT COSTS, so the choice is informed rather than discovered: about two seconds per message
and roughly 30 KB per row. Every linked row in a store this size means tens of megabytes and
the better part of an hour. `--limit` and `--account` exist so it can be done in slices.

    python dashboard/backfill_bodies.py --dry-run
    python dashboard/backfill_bodies.py --kept-first --limit 200
    python dashboard/backfill_bodies.py --account someone@example.com

READ-ONLY against the mailbox: it sets MAILTOOL_READONLY=1 for every child and only ever
calls `find`, which uses BODY.PEEK and cannot mark anything read or move anything.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

MAILTOOL = os.path.join(ROOT, "tools", "mailtool.py")
BODY_LIMIT = 400000


def readable_body(raw_bytes):
    """text/html preferred, text/plain otherwise, attachments excluded.

    Same rule as `mailtool.full_body`, applied to a message read from disk. Attachments are
    left out on purpose: the viewer renders the body, and carrying a large PDF into a SQLite
    column would make the store's size a function of what other people email you.
    """
    import email                                                    # noqa: PLC0415
    msg = email.message_from_bytes(raw_bytes)
    for want in ("text/html", "text/plain"):
        parts = []
        for part in msg.walk():
            if part.get_content_type() != want or part.get_filename():
                continue
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8",
                                            errors="replace"))
        if parts:
            return "\n".join(parts)[:BODY_LIMIT]
    return None


def candidates(conn, account=None, kept_first=False, limit=0):
    """Rows that could be filled: linked, and not already carrying a body."""
    sql = ("SELECT id, account, message_id, subject, msg_day, disposition FROM messages "
           "WHERE message_id IS NOT NULL AND message_id != '' "
           "AND (body_text IS NULL OR body_text = '')")
    args = []
    if account:
        sql += " AND account = ?"
        args.append(account)
    # Kept and surfaced mail first: it is what a person actually reopens, and it is the most
    # likely to still be where we left it.
    sql += (" ORDER BY CASE WHEN disposition IN ('kept','surfaced','saved') THEN 0 ELSE 1 "
            "END, msg_day DESC")
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    if not kept_first:
        rows.sort(key=lambda r: str(r.get("msg_day") or ""), reverse=True)
    return rows


def fetch_one(row, timeout=90):
    """(body, note). body is None when nothing could be read, and the note says WHY.

    The verdict comes from `find`'s STDOUT; --out receives the raw message. Keeping those
    two straight is the whole reason the first measurement of this was wrong.
    """
    tmp = os.path.join(tempfile.mkdtemp(), "m.eml")
    env = dict(os.environ, MAILTOOL_READONLY="1", PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(
            [sys.executable, MAILTOOL, "find", "--account", row["account"],
             "--message-id", row["message_id"], "--out", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    try:
        verdict = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError, AttributeError):
        tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
        return None, ("could not search: " + (tail[-1][:70] if tail else "no output"))
    if not verdict.get("found"):
        return None, "not in the mailbox any more"
    if not os.path.exists(tmp):
        return None, "found but nothing was written"
    try:
        body = readable_body(open(tmp, "rb").read())
    except Exception as e:
        return None, "unreadable MIME (%s)" % type(e).__name__
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not body:
        return None, "no text or html part (attachment-only?)"
    return body, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--kept-first", action="store_true", dest="kept_first", default=True)
    ap.add_argument("--all-order", action="store_false", dest="kept_first",
                    help="newest first instead of kept-and-surfaced first")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)

    conn = db.connect()
    rows = candidates(conn, args.account, args.kept_first, args.limit)
    total_linked = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id IS NOT NULL "
        "AND message_id != ''").fetchone()[0]
    unlinked = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id IS NULL "
        "OR message_id = ''").fetchone()[0]

    print("store: %s" % db.DB_PATH)
    print("%d row(s) to try, of %d linked. %d row(s) have NO Message-ID and can never be "
          "filled this way - that is a permanent hole, not a queue." % (len(rows),
                                                                        total_linked,
                                                                        unlinked))
    if args.dry_run:
        print("\n(dry run - nothing fetched, nothing written)")
        for r in rows[:10]:
            print("  would try %s  %s  %s" % (r["msg_day"], r["account"][:24],
                                              (r["subject"] or "")[:44]))
        if len(rows) > 10:
            print("  ... and %d more" % (len(rows) - 10))
        return 0

    filled = skipped = 0
    reasons, bytes_written = {}, 0
    started = time.time()
    for i, r in enumerate(rows, 1):
        body, note = fetch_one(r)
        if body:
            conn.execute("UPDATE messages SET body_text = ? WHERE id = ?", (body, r["id"]))
            conn.commit()
            filled += 1
            bytes_written += len(body)
        else:
            skipped += 1
            reasons[note] = reasons.get(note, 0) + 1
        if i % 25 == 0 or i == len(rows):
            rate = i / max(0.001, time.time() - started)
            print("  %d/%d  filled %d  skipped %d  (%.1f/s, %.0fs elapsed)"
                  % (i, len(rows), filled, skipped, rate, time.time() - started))

    print("\nfilled %d, skipped %d, %.1f MB written" % (filled, skipped,
                                                        bytes_written / 1e6))
    if reasons:
        # WHY each one failed, named. "skipped 40" on its own invites the reader to assume
        # the mail is gone, when it might equally be a mailbox that would not connect - and
        # those two call for completely different responses.
        print("reasons:")
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print("  %-44s %d" % (why[:44], n))
    left = conn.execute("SELECT COUNT(*) FROM messages WHERE message_id IS NOT NULL "
                        "AND message_id != '' AND (body_text IS NULL OR body_text = '')"
                        ).fetchone()[0]
    print("\n%d linked row(s) still without a body. Re-run to continue - it is resumable "
          "by construction, because it only ever selects rows that lack one." % left)
    return 0


if __name__ == "__main__":
    sys.exit(main())
