"""Rule on a flagged (sender, host) pairing from the command line.

WHY THIS EXISTS. The verdict had exactly one caller - the HTTP handler behind a button on the
page - so a pairing could only be ruled on by a person at a browser. That is fine for a human
reader and wrong for an agent, whose job here is to rule on the day's findings so that a
benign row never greets the reader. The tell that the reachable path was missing: clearing a
backlog took a throwaway script written against `api_host_review`.

Same posture as `db.record_ack`: one implementation of the decision, reachable both ways.

    python dashboard/rule_host.py --list
    python dashboard/rule_host.py --sender-key acme --host click.example.com \
        --verdict cleared --note "the sender's own click tracker, same registrable domain"
    python dashboard/rule_host.py --sender-key acme --host click.example.com --verdict none

`--verdict none` clears the ruling and puts the pairing back in the open list, because a wrong
call has to be undoable - the same reasoning as un-acknowledging.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db                                                            # noqa: E402
import server                                                        # noqa: E402

# --list prints stored SUBJECTS and SENDER NAMES, which contain whatever the sender typed,
# and a Windows console defaults to cp1252 - so one emoji in a subject aborts the whole
# listing with a UnicodeEncodeError. Missed when this file shipped yesterday (0.29.16);
# test_console_encoding caught it, which is the entry-point sweep working as intended.
from consoleio import safe_console                                   # noqa: E402
safe_console()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true",
                    help="show the pairings still awaiting a verdict and exit")
    ap.add_argument("--sender-key")
    ap.add_argument("--host")
    ap.add_argument("--verdict", choices=("cleared", "suspicious", "none"),
                    help="'none' clears the ruling and re-opens the pairing")
    ap.add_argument("--note", default="",
                    help="the reasoning. Say WHY, so a wrong call is visible and undoable.")
    ap.add_argument("--by", default="agent")
    args = ap.parse_args(argv)

    conn = db.connect()
    try:
        if args.list or not (args.sender_key and args.host and args.verdict):
            rows = list(conn.execute(
                "SELECT sender_key, host, sender, subject, first_flagged, times_seen "
                "FROM host_flags WHERE verdict IS NULL ORDER BY first_flagged, sender_key"))
            if not rows:
                print("awaiting a verdict: none - every flagged pairing has been ruled on.")
            else:
                print(f"{len(rows)} pairing(s) awaiting a verdict:\n")
                for r in rows:
                    print(f"  {r['sender_key']}  ->  {r['host']}")
                    print(f"     first flagged {r['first_flagged']}, "
                          f"seen {r['times_seen']}x: {(r['subject'] or '')[:60]}")
            # Not an error when --list was asked for; an error when a ruling was attempted
            # without enough to identify it, because silently listing instead of writing is
            # the shape that lets a caller believe it ruled.
            return 0 if args.list else 2

        verdict = None if args.verdict == "none" else args.verdict
        res = server.api_host_review(conn, None, body={
            "sender_key": args.sender_key, "host": args.host,
            "verdict": verdict, "note": args.note, "by": args.by})
        if not res.get("ok"):
            print(f"REFUSED: {res.get('error')}")
            return 1
        print(f"{args.sender_key} -> {args.host}: {verdict or 're-opened'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
