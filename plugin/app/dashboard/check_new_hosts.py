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
        weighty = any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", haystack)
                      for w in WEIGHTY)
        print(f"  {'** ' if weighty else '   '}{(f['sender'] or '')[:44]}")
        print(f"     {(f['subject'] or '')[:66]}")
        print(f"     profile: {f['profile_messages']} messages; "
              f"NEW: {', '.join(f['new_hosts'][:6])}")
    if not findings:
        print("  none - every link went to a host its sender has used before.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
