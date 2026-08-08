"""Learn which link hosts each sender NORMALLY uses.

WHY THIS AND NOT A DOMAIN CHECK. Comparing a link's domain to the sender's domain is a
static test with no memory. It cannot tell that `url1719.example-bank.org` is
that bank's own redirector, and it cries wolf on every mail service on earth (a
`facebookmail.com` sender linking to `facebook.com` trips it nine times in one message).
Worse, it cannot see the attack that actually happens: a message impersonating a sender
already trusted. A "the bank" mail linking somewhere the bank has never linked in
eight prior messages is the shape worth shouting about, and domain matching is blind to it.

WHAT THIS IS NOT. It is a RANKING signal, never a verdict. Real senders change ESPs and
add hosts, so "new for this sender" means *look at this*, not *block this*.

THE TRAP IT AVOIDS. A profile built from one message would bless whatever that message
linked to - if the first mail from a sender is the phish, it writes its own permission
slip. So `sender_profile.messages` records how much evidence stands behind a profile, and
callers are expected to treat a thin one as UNKNOWN rather than as safe.

  python dashboard/build_sender_hosts.py --account you@example.com   # one mailbox
  python dashboard/build_sender_hosts.py                     # all of them
"""
import argparse
import collections
import email
import os
import re
import sqlite3
import subprocess
import sys
from email import policy

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "email_dashboard.db")
TOOL = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
sys.path.insert(0, HERE)
from server import _sender_key                                    # noqa: E402

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
try:
    from consoleio import safe_console
except ImportError:  # running from another cwd
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from consoleio import safe_console
safe_console()


BOXES = {"gmail": ["INBOX", "[Gmail]/Trash"], "outlook": ["INBOX", "Deleted"]}
HOST_RE = re.compile(r'https?://([A-Za-z0-9.\-]+)', re.I)


def boxes_for(account):
    return BOXES["gmail"] if account.endswith("gmail.com") else BOXES["outlook"]


def hosts_in(raw_bytes):
    """Every link host in a message, counted ONCE per message.

    Per-message rather than per-link on purpose: a newsletter with 60 links to one host
    would otherwise look like 60 independent pieces of evidence for that host when it is
    really one.
    """
    try:
        msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    except Exception:
        return None, set()
    found, sender = set(), ""
    try:
        sender = str(msg.get("From") or "")
    except Exception:
        pass
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() not in ("text/html", "text/plain"):
            continue
        try:
            body = part.get_content()
        except Exception:
            continue
        for h in HOST_RE.findall(body or ""):
            h = h.lower().strip(".")
            if h and "." in h:
                found.add(h)
    return sender, found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account")
    ap.add_argument("--limit", type=int, default=1200, help="messages per folder")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    accounts = ([args.account] if args.account else
                [r[0] for r in conn.execute(
                    "SELECT DISTINCT account FROM messages WHERE account LIKE '%@%' "
                    "ORDER BY account")])

    host_rows = collections.Counter()      # (sender_key, host) -> messages
    seen_dates = {}                        # (sender_key, host) -> [first, last]
    prof = collections.Counter()           # sender_key -> messages
    prof_dates = {}

    for acct in accounts:
        print(f"\n{acct}")
        for box in boxes_for(acct):
            p = subprocess.run(
                [sys.executable, TOOL, "fetch", "--account", acct, "--mailbox", box,
                 "--days", "36500", "--limit", str(args.limit), "--with-hosts"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if p.returncode != 0 or not p.stdout:
                print(f"    {box:<18} unavailable")
                continue
            import json
            data = json.loads(p.stdout)
            msgs = data.get("messages", [])
            print(f"    {box:<18} walked {data.get('returned')} of "
                  f"{data.get('total_matched')}", end="", flush=True)
            n_prof = 0
            for m in msgs:
                # hosts come back from the SAME bulk walk - see mailtool --with-hosts
                hosts = set(m.get("link_hosts") or [])
                sk = _sender_key(m.get("from"))
                if not sk:
                    continue
                when = (m.get("date") or "")[:31]
                prof[sk] += 1
                d = prof_dates.setdefault(sk, [when, when])
                d[0], d[1] = min(d[0] or when, when), max(d[1] or when, when)
                n_prof += 1
                for h in hosts:
                    host_rows[(sk, h)] += 1
                    dd = seen_dates.setdefault((sk, h), [when, when])
                    dd[0], dd[1] = min(dd[0] or when, when), max(dd[1] or when, when)
            print(f"  -> profiled {n_prof}")

    for (sk, h), n in host_rows.items():
        f, l = seen_dates[(sk, h)]
        conn.execute(
            "INSERT INTO sender_hosts (sender_key, host, messages, first_seen, last_seen) "
            "VALUES (?,?,?,?,?) ON CONFLICT(sender_key, host) DO UPDATE SET "
            "messages = messages + excluded.messages, "
            "first_seen = MIN(first_seen, excluded.first_seen), "
            "last_seen = MAX(last_seen, excluded.last_seen)", (sk, h, n, f, l))
    for sk, n in prof.items():
        f, l = prof_dates[sk]
        conn.execute(
            "INSERT INTO sender_profile (sender_key, messages, first_seen, last_seen) "
            "VALUES (?,?,?,?) ON CONFLICT(sender_key) DO UPDATE SET "
            "messages = messages + excluded.messages, "
            "first_seen = MIN(first_seen, excluded.first_seen), "
            "last_seen = MAX(last_seen, excluded.last_seen)", (sk, n, f, l))
    conn.commit()

    print("\n" + "=" * 68)
    print(f"senders profiled : {len(prof)}")
    print(f"sender/host pairs: {len(host_rows)}")
    print("\nbest-supported profiles:")
    for r in conn.execute(
            "SELECT p.sender_key, p.messages, COUNT(h.host) hosts FROM sender_profile p "
            "LEFT JOIN sender_hosts h ON h.sender_key = p.sender_key "
            "GROUP BY p.sender_key ORDER BY p.messages DESC LIMIT 12"):
        print(f"   {r['messages']:>3} msgs, {r['hosts']:>3} hosts   {r['sender_key'][:44]}")
    conn.close()


if __name__ == "__main__":
    main()
