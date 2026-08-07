"""Record an acknowledgement without a browser. (F26)

`INSERT INTO acks` existed in exactly ONE place - the dashboard's HTTP handler - so the only
way to say "I have dealt with this" was to click in a browser. That is fine for a person at a
screen, and wrong for the operating model this plugin prescribes, where the thing maintaining
the board day to day is a scheduled task with no UI, no session and no browser. It could read
the acks table only by opening the SQLite file directly, and it could not write one at all.

The gap is not cosmetic. An item gets dealt with OFF-CHANNEL all the time - answered in a
call, decided in a meeting, delegated verbally - while the mail thread shows nothing. A
routine with no way to record that re-escalates it every single run. So a parallel ledger gets
invented, one the sweep reads and the dashboard does not, and then two stores answer "has the
owner dealt with this?" - each authoritative for a different consumer, both behaving
correctly, disagreeing.

The divergence runs the wrong way, too. Off-channel resolutions are the single most valuable
thing a human can tell a mail tool, because it can never infer them - and they were exactly
the ones that could only be recorded in the store the dashboard ignores.

The table, the key derivation and the annotation path all existed. Only the door was missing.

    python dashboard/ack.py --subject "Response requested: seat audit" \
                            --sender vendor@example.com --note "answered on the call"
    python dashboard/ack.py --message-id "<abc@example.com>" --lift
    python dashboard/ack.py --list
    python dashboard/ack.py --import-md briefs/ACKNOWLEDGED.md --dry-run

`--import-md` exists for the ledger the gap forced people to invent: it reads a markdown file
and records each line it can identify, so the workaround becomes an EXPORT of the table rather
than a second database that argues with it. It matches against messages actually in the store
and refuses to invent an identity for a line it cannot place, because a silent no-op here
would be the same failure one level up.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import server                                                      # noqa: E402


def cmd_list(conn, args):
    rows = list(conn.execute(
        "SELECT kind, key, account, sender, subject, note, acked_at FROM acks "
        "ORDER BY acked_at DESC"))
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=1))
        return 0
    if not rows:
        # A zero is a claim. Say which store was consulted, so "nothing acknowledged" cannot
        # be confused with "looked in the wrong place".
        print("no acknowledgements recorded in %s" % db.DB_PATH)
        return 0
    for r in rows:
        print("%-10s %-19s %s" % (r["kind"], (r["acked_at"] or "")[:19],
                                  (r["subject"] or r["key"])[:64]))
        if r["note"]:
            print("%32s%s" % ("", r["note"][:70]))
    print("\n%d acknowledgement(s) in %s" % (len(rows), db.DB_PATH))
    return 0


def _find_match(conn, subject=None, sender=None):
    """The stored row this line is about, or None.

    Deliberately refuses to guess. An acknowledgement stored against an identity no message
    has is a row that silences nothing and reports success - the exact shape of failure this
    whole area keeps producing.
    """
    text = " ".join((subject or "").split()).lower()
    if not text:
        return None
    cands = list(conn.execute(
        "SELECT message_id, sender, subject, account FROM messages "
        "WHERE subject IS NOT NULL AND subject != ''"))
    hits = [r for r in cands
            if " ".join((r["subject"] or "").split()).lower() == text
            and (not sender or sender.lower() in (r["sender"] or "").lower())]
    if not hits:
        hits = [r for r in cands
                if text in " ".join((r["subject"] or "").split()).lower()
                and (not sender or sender.lower() in (r["sender"] or "").lower())]
    return hits[0] if len(hits) >= 1 else None


def cmd_import_md(conn, args):
    """Read a markdown ledger and record what it can identify."""
    try:
        with open(args.import_md, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError as e:
        print("cannot read %s (%s)" % (args.import_md, e), file=sys.stderr)
        return 2
    entries = []
    for ln in lines:
        m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", ln)
        if not m:
            continue
        body = m.group(1)
        # Strip a leading "YYYY-MM-DD - " or "**bold** - " prefix if there is one; whatever
        # remains is treated as the subject.
        body = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[-—:]\s*", "", body)
        body = body.replace("**", "").strip()
        if body:
            entries.append(body)
    if not entries:
        print("no list entries found in %s - nothing to import" % args.import_md)
        return 0
    done = missed = 0
    for text in entries:
        row = _find_match(conn, subject=text)
        if not row:
            missed += 1
            print("  NO MATCH  %s" % text[:70])
            continue
        if args.dry_run:
            print("  would ack %s" % (row["subject"] or "")[:66])
            done += 1
            continue
        res = server.record_ack(conn, kind="message", message_id=row["message_id"],
                                sender=row["sender"], subject=row["subject"],
                                account=row["account"],
                                note=args.note or "imported from %s"
                                % os.path.basename(args.import_md))
        if res.get("ok"):
            done += 1
            print("  acked     %s" % (row["subject"] or "")[:66])
        else:
            missed += 1
            print("  REFUSED   %s (%s)" % (text[:50], res.get("error")))
    print("\n%d of %d entr%s recorded%s"
          % (done, len(entries), "y" if len(entries) == 1 else "ies",
             "  (dry run - nothing written)" if args.dry_run else ""))
    if missed:
        print("%d could not be placed against a message in the store. They are NOT recorded; "
              "an ack stored against an identity no row has would silence nothing and report "
              "success." % missed)
    return 1 if missed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show what is acknowledged")
    ap.add_argument("--json", action="store_true", help="machine-readable --list")
    ap.add_argument("--kind", default="message", choices=("message", "thread"))
    ap.add_argument("--message-id", dest="message_id")
    ap.add_argument("--sender")
    ap.add_argument("--subject")
    ap.add_argument("--account")
    ap.add_argument("--note", help="WHY it is closed - the part a mail tool can never infer")
    ap.add_argument("--lift", action="store_true", help="un-acknowledge instead")
    ap.add_argument("--import-md", dest="import_md",
                    help="import a markdown ledger of closed items")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)

    db.init_db()
    conn = db.connect()
    if args.list:
        return cmd_list(conn, args)
    if args.import_md:
        return cmd_import_md(conn, args)
    if not (args.message_id or args.subject):
        ap.error("give --message-id or --subject (or use --list / --import-md)")

    res = server.record_ack(conn, kind=args.kind, message_id=args.message_id,
                            sender=args.sender, subject=args.subject,
                            account=args.account, note=args.note,
                            on=not args.lift)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
