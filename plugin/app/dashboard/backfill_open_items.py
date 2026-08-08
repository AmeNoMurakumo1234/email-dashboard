"""Seed the standing open-items list from mail already in the store. Dry by default.

WHY THIS IS NOT AUTOMATIC, and why it is bounded. Carry-forward starts working on the next
ingest, which means an install upgrading to 0.8.0 sees an empty "Still open" panel - and an
empty panel reads as *nothing is outstanding*, which is a claim the tool has no basis for.
It has months of attention-worthy mail in it and simply was not keeping the list.

But backfilling the whole history is worse than empty. Most of what needed a person in
March was dealt with in March, so opening all of it would produce a list of hundreds of
mostly-finished items - and a list that is mostly wrong on the first read is one nobody
reads again. That is the failure this panel exists to prevent, delivered by the fix for it.

So: a WINDOW, `--since` (default 14 days back from the newest run), and a dry run first.
Look at what it proposes, pick a date that matches when you actually stopped being able to
account for things, and only then `--write`.

    python dashboard/backfill_open_items.py                 # show what it would open
    python dashboard/backfill_open_items.py --since YYYY-MM-DD
    python dashboard/backfill_open_items.py --since YYYY-MM-DD --write
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                                          # noqa: E402

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
try:
    from consoleio import safe_console
except ImportError:  # running from another cwd
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from consoleio import safe_console
safe_console()



def newest_run(conn):
    row = conn.execute("SELECT MAX(COALESCE(msg_day, run_date)) FROM messages").fetchone()
    return (row and row[0]) or None


def candidates(conn, since):
    """Attention-worthy, not binned, on or after `since`. Newest row per key wins.

    Grouped by key first so a notice that arrived four times becomes ONE proposed item,
    exactly as the live carry-forward would have made it. A backfill that produced a
    different shape from the thing it is seeding would put the panel in a state no later
    run could ever reach.
    """
    rows = conn.execute(
        "SELECT sender, subject, account, category, importance, message_id, msg_date, "
        "COALESCE(msg_day, run_date) AS day FROM messages "
        # DELIBERATELY_KEPT, not `!= 'trashed'`. The negative spelling silently readmits any
        # disposition added later - which is exactly what happened to would_trash, whose whole
        # meaning is "the triage judged this disposable". True of the string, false of the
        # meaning, and the backfill would have seeded the standing list with binned noise.
        "WHERE COALESCE(msg_day, run_date) >= ? AND disposition IN (%s) "
        "AND importance IN (%s) ORDER BY day ASC"
        % (",".join("?" * len(db.DELIBERATELY_KEPT)), ",".join("?" * len(db.ATTENTION))),
        (since,) + tuple(sorted(db.DELIBERATELY_KEPT)) + db.ATTENTION).fetchall()
    seen = {}
    for r in rows:
        m = dict(r)
        kind, key = db.open_item_key(m)
        if not key:
            continue
        seen.setdefault(key, (kind, m))
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="YYYY-MM-DD; default is 14 days before the newest run")
    ap.add_argument("--write", action="store_true", help="actually open these items")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_db(conn)
    conn = db.connect(args.db)
    try:
        newest = newest_run(conn)
        if not newest:
            print("no runs in the store - nothing to backfill from")
            return 1
        since = args.since
        if not since:
            y, m, d = (int(x) for x in str(newest)[:10].split("-"))
            since = str(date(y, m, d) - timedelta(days=14))

        found = candidates(conn, since)
        already = {r[0] for r in conn.execute("SELECT key FROM open_items")}

        # ACKNOWLEDGED IS NOT OUTSTANDING, and skipping this was the whole point of the
        # window being a safeguard rather than the safeguard. Run on a real store, this
        # seeded the standing list with a message the owner had acknowledged two days
        # earlier - so the panel opened demanding a decision about something they had
        # already dealt with, and the tool was arguing with its own record of their
        # judgment. A `--since` window cannot catch that: the item was recent, it was just
        # already handled.
        import server                                              # noqa: PLC0415
        acked = server.acked_message_keys(conn)
        acked_threads = {r[0] for r in conn.execute(
            "SELECT key FROM acks WHERE kind = 'thread'")}
        skipped_acked = 0
        fresh = {}
        for k, v in found.items():
            if k in already:
                continue
            kind, m = v
            ids = server.ack_identities("message", k if kind == "message" else None,
                                        m.get("sender"), m.get("subject"),
                                        m.get("account"))
            thread = server.ack_key("thread", None, m.get("sender"), m.get("subject"),
                                    m.get("account"))
            if any(i in acked for i in ids) or thread in acked_threads:
                skipped_acked += 1
                continue
            fresh[k] = v

        print("newest run in store : %s" % newest)
        print("window              : %s onward" % since)
        # Reported as three numbers, not one. "12 items" cannot distinguish a store that
        # was already tracking from one where the backfill found nothing to track.
        print("attention items     : %d in the window" % len(found))
        print("already tracked     : %d" % (len(found) - len(fresh) - skipped_acked))
        # Reported, not silently dropped: "3 skipped" and "3 not found" are different
        # facts, and the owner should be able to see their own acknowledgements working.
        print("already acknowledged: %d  <- you have seen these; not reopened"
              % skipped_acked)
        print("would open          : %d" % len(fresh))
        if not fresh:
            print("\nnothing to add.")
            return 0

        print()
        for key, (kind, m) in sorted(fresh.items(), key=lambda kv: kv[1][1]["day"]):
            print("  %s  %-11s %-30s %s"
                  % (m["day"], (m.get("importance") or "")[:11],
                     (m.get("sender") or "")[:30], (m.get("subject") or "")[:60]))

        if not args.write:
            print("\nDRY RUN - nothing written. Re-run with --write, and consider a "
                  "different --since\nif this list contains things you finished long ago: "
                  "a standing list that is\nmostly stale on its first read is one nobody "
                  "opens twice.")
            return 0

        for key, (kind, m) in fresh.items():
            conn.execute(
                "INSERT INTO open_items (key, kind, account, sender, subject, concept, "
                "importance, first_seen, last_seen, runs_seen, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,'open')",
                (key, kind, m.get("account"), m.get("sender"), m.get("subject"),
                 db.concept_of(m.get("category")), m.get("importance"),
                 m["day"], newest))
        conn.commit()
        print("\nOPENED %d item(s). Resolve anything already dealt with from the "
              "dashboard - \"Done elsewhere\" is there for exactly that." % len(fresh))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
