"""Flag mail where a TRUSTED sender links somewhere it has never linked before.

THE POINT, in the owner's words: most of what arrives is not worth looking at, and that is
fine - the pain is that every now and then something IS worth looking at and it gets
drowned in the noise. This is a noise-free signal by construction. It cannot fire on a
stranger, it cannot fire on a promo blast from a sender that always uses the same hosts,
and it stays silent on the ESP redirectors that a naive domain check screams about.

It fires on exactly one thing: a sender with an ESTABLISHED history suddenly pointing
somewhere new. That is the shape of a spoof of someone already trusted, which is the
attack a busy inbox is least able to catch by eye.

It is a RANKING signal, not a verdict - senders do change ESPs. The output belongs in the
run report so a human decides.

  python dashboard/check_new_hosts.py --days 2
"""
import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "email_dashboard.db")
TOOL = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
sys.path.insert(0, HERE)
from server import _sender_key, PROFILE_MIN_MESSAGES              # noqa: E402

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
try:
    from consoleio import safe_console
except ImportError:  # running from another cwd
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from consoleio import safe_console
safe_console()


# Words that mean an unexpected destination actually costs something. A promo blast
# pointing at a new CDN is noise; a bank, a login notice or a family member is not.
#
# Two things were wrong with the first version. It carried a two-letter leftover label from
# one real mailbox, which disclosed something about its owner. And because these are matched
# as SUBSTRINGS, a two-letter entry also matches inside ordinary words like "available" and
# "advance", so nearly every finding was marked weighty and the marker meant nothing. A term
# list that flags everything is the same failure as one that flags nothing: no signal.
#
# Matched on word boundaries now, so a short term cannot hide inside a longer word.
WEIGHTY = ("bill", "bills", "invoice", "receipt", "payment", "statement", "financial",
           "bank", "banking", "credit", "security", "login", "sign-in", "password",
           "account", "policy", "family", "person", "medical", "appointment")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--account")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--run-date", help="date to stamp saved findings with (default: today)")
    ap.add_argument("--no-save", action="store_true",
                    help="print only; do not record findings in host_flags")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    accounts = ([args.account] if args.account else
                [r[0] for r in conn.execute(
                    "SELECT DISTINCT account FROM messages WHERE account LIKE '%@%'")])

    profiled = conn.execute("SELECT COUNT(*) c FROM sender_profile WHERE messages >= ?",
                            (PROFILE_MIN_MESSAGES,)).fetchone()["c"]
    print(f"established sender profiles available: {profiled}")
    if not profiled:
        print("NO PROFILES YET - run build_sender_hosts.py first. Reporting nothing would "
              "be a false all-clear, so this is an explicit refusal to answer.")
        return 2

    findings, scanned, unprofiled = [], 0, 0
    for acct in accounts:
        p = subprocess.run(
            [sys.executable, TOOL, "fetch", "--account", acct, "--days", str(args.days),
             "--limit", str(args.limit), "--with-hosts"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0 or not p.stdout:
            print(f"  ! {acct}: unreachable")
            continue
        data = json.loads(p.stdout)
        for m in data.get("messages", []):
            scanned += 1
            key = _sender_key(m.get("from"))
            if not key:
                continue
            row = conn.execute("SELECT messages FROM sender_profile WHERE sender_key = ?",
                               (key,)).fetchone()
            n = row["messages"] if row else 0
            if n < PROFILE_MIN_MESSAGES:
                unprofiled += 1
                continue                      # no history = no claim, in either direction
            known = {r["host"] for r in conn.execute(
                "SELECT host FROM sender_hosts WHERE sender_key = ?", (key,))}
            fresh = sorted(set(m.get("link_hosts") or []) - known)
            if fresh:
                findings.append({"account": acct, "sender": m.get("from"),
                                 "subject": m.get("subject"), "profile_messages": n,
                                 "new_hosts": fresh})

    print(f"scanned {scanned} messages from the last {args.days} day(s); "
          f"{unprofiled} had too little history to judge")
    print(f"\n{len(findings)} message(s) where an established sender used a NEW host:\n")
    for f in findings:
        haystack = ((f["subject"] or "") + " " + (f["sender"] or "")).lower()
        f["weighty"] = any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", haystack)
                           for w in WEIGHTY)
        print(f"  {'** ' if f['weighty'] else '   '}{(f['sender'] or '')[:44]}")
        print(f"     {(f['subject'] or '')[:66]}")
        print(f"     profile: {f['profile_messages']} messages; "
              f"NEW: {', '.join(f['new_hosts'][:6])}")
    if not findings:
        print("  none - every link went to a host its sender has used before.")

    if not args.no_save:
        saved, already = _save(conn, findings, args.run_date)
        print(f"\nrecorded to host_flags: {saved} new pairing(s), {already} already known "
              f"(a pairing with a verdict is never re-opened)")
    return 0


def _save(conn, findings, run_date=None):
    """Persist each (sender, new host) pairing so the dashboard can show what is unreviewed.

    ONE ROW PER PAIRING, not per message. The question being asked is "is this host normal
    for this sender", and it is the same question however many messages raise it - so a
    second sighting bumps a counter instead of re-opening something already judged. That is
    the whole reason this can live on the page without becoming the noise it exists to cut.

    An existing verdict is never overwritten here. A human's ruling is not something a scan
    gets to undo on its next pass.
    """
    import datetime
    day = run_date or datetime.date.today().isoformat()
    saved = already = 0
    for f in findings:
        key = _sender_key(f["sender"])
        for host in f["new_hosts"]:
            row = conn.execute(
                "SELECT times_seen FROM host_flags WHERE sender_key = ? AND host = ?",
                (key, host)).fetchone()
            if row:
                conn.execute("UPDATE host_flags SET times_seen = times_seen + 1, "
                             "last_flagged = ? WHERE sender_key = ? AND host = ?",
                             (day, key, host))
                already += 1
            else:
                conn.execute(
                    "INSERT INTO host_flags (sender_key, host, sender, account, subject, "
                    "profile_messages, weighty, first_flagged, last_flagged, times_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (key, host, f["sender"], f["account"], f["subject"],
                     f["profile_messages"], 1 if f.get("weighty") else 0, day, day))
                saved += 1
    conn.commit()
    return saved, already


if __name__ == "__main__":
    sys.exit(main())
