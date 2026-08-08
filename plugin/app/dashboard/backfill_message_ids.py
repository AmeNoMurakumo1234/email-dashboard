"""Link historical dashboard rows to their real messages, so they can be opened.

Rows triaged before the message-id column existed recorded no identifier. This walks every mailbox,
indexes what is actually still there, and links a row ONLY when the match is unambiguous.

THE RULE THAT MATTERS: never link on a guess. Opening the WRONG email in a viewer whose
entire job is inspecting untrusted mail is a worse outcome than leaving a row unopenable.
So a row is linked only when

  * exactly one message in that account carries that exact subject, OR
  * several do, but exactly one of them has a Date within DATE_WINDOW days of the run that
    triaged it (a daily run only ever sees mail from the last couple of days).

Anything else is left alone and counted as ambiguous. No prefix matching, no fuzzy
matching, no "closest guess".

WHAT CANNOT BE FIXED, and it is most of the backlog: trashed mail is purged by the
provider after about 30 days. For those rows there is no message left anywhere to open -
that is not a matching failure and no amount of cleverness recovers it. Kept and surfaced
mail stays in the inbox, which is also the mail worth opening, so that is where this
actually pays off.

  python dashboard/backfill_message_ids.py            # report only, changes nothing
  python dashboard/backfill_message_ids.py --apply    # write the links
"""
import argparse
import collections
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from email import utils as email_utils
from email.utils import parsedate_to_datetime

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
try:
    from consoleio import safe_console
except ImportError:  # running from another cwd
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from consoleio import safe_console
safe_console()


HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "email_dashboard.db")
TOOL = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
DATE_WINDOW = 3          # days either side of the run, for subject matches
SENDER_WINDOW = 3        # days BEFORE the run, for the weaker sender-only match

# INBOX + Trash only, deliberately. Gmail's All Mail is a SUPERSET of the inbox running to
# tens of thousands of messages, and walking it turned a two-minute job into an open-ended
# one for almost no gain: kept and surfaced mail is left in the inbox, trashed mail is in
# Trash until the provider purges it, and this lane archives almost nothing (rule 13 sends
# only a narrow slice of promos to All Mail, which nobody needs to re-open). Scanning it was cost with
# no recall behind it. Pass --deep to include it if that ever stops being true.
BOXES = {
    "gmail": ["INBOX", "[Gmail]/Trash"],
    "outlook": ["INBOX", "Deleted"],
}
DEEP_EXTRA = {"gmail": ["[Gmail]/All Mail"], "outlook": ["Archive"]}


DEEP = False


def boxes_for(account):
    kind = "gmail" if account.endswith("gmail.com") else "outlook"
    return BOXES[kind] + (DEEP_EXTRA[kind] if DEEP else [])


def sender_key(raw):
    """Fold a sender string the way a person means it (mirrors server._sender_key)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "@" in raw or "<" in raw:
        name, addr = email_utils.parseaddr(raw)
        name = name.strip().strip('"').strip()
        if name:
            return name.lower()
        if addr:
            return addr.lower()
    return raw.strip('"').strip().lower()


def harvest(account, verbose=True):
    """subject(lower) -> [ {message_id, when} ]  for everything still on the server."""
    index = collections.defaultdict(list)
    by_sender = collections.defaultdict(list)
    seen_ids = set()
    for box in boxes_for(account):
        p = subprocess.run(
            [sys.executable, TOOL, "fetch", "--account", account, "--mailbox", box,
             "--days", "36500", "--limit", "5000", "--no-snippets"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0 or not p.stdout:
            if verbose:
                print(f"    {box:<20} unavailable ({(p.stderr or '').strip()[:60]})")
            continue
        try:
            data = json.loads(p.stdout)
        except ValueError:
            if verbose:
                print(f"    {box:<20} unreadable response")
            continue
        n = 0
        for m in data.get("messages", []):
            subj = (m.get("subject") or "").strip()
            mid = (m.get("message_id") or "").strip()
            if not subj or not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            try:
                when = parsedate_to_datetime(m.get("date"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except Exception:
                when = None
            rec = {"message_id": mid, "when": when, "subject": subj}
            index[subj.lower()].append(rec)
            sk = sender_key(m.get("from"))
            if sk:
                by_sender[sk].append(rec)
            n += 1
        if verbose:
            # state the reach: returned vs matched, so a short harvest is never mistaken
            # for an empty mailbox
            print(f"    {box:<20} walked {data.get('returned')} of "
                  f"{data.get('total_matched')}  (+{n} new)")
    return index, by_sender


def pick(cands, run_date):
    """Return the single unambiguous match, or None."""
    if len(cands) == 1:
        return cands[0]["message_id"]
    try:
        rd = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    near = [c for c in cands
            if c["when"] and abs((c["when"] - rd).days) <= DATE_WINDOW]
    return near[0]["message_id"] if len(near) == 1 else None


def pick_by_sender(cands, run_date):
    """Second pass, for rows whose SUBJECT was paraphrased and can never match.

    Many older rows recorded a description rather than the real subject line (for example
    "<contact> commented (condolence on a third-party post)"), so subject matching can
    never reach them. The sender string, though, was captured verbatim.

    The window is TIGHTER here (the daily fetch only ever looks back ~2 days) and the match
    must still be UNIQUE, because sender alone is far weaker evidence than sender+subject -
    a sender who wrote twice in the window gives two equally good candidates and gets
    skipped rather than guessed at. Returns the record so the caller can also repair the
    stored subject from the real one.
    """
    try:
        rd = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    near = [c for c in cands if c["when"]
            and -1 <= (rd - c["when"]).days <= SENDER_WINDOW]
    return near[0] if len(near) == 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the links (default: report only)")
    ap.add_argument("--account", help="limit to one account")
    ap.add_argument("--deep", action="store_true",
                    help="also walk All Mail/Archive (very slow, rarely worth it)")
    args = ap.parse_args()
    global DEEP
    DEEP = args.deep

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    accounts = [r[0] for r in conn.execute(
        "SELECT DISTINCT account FROM messages WHERE account IS NOT NULL ORDER BY account")]
    if args.account:
        accounts = [a for a in accounts if a == args.account]

    grand = collections.Counter()
    per_disp = collections.defaultdict(collections.Counter)
    updates = []
    samples = []

    for acct in accounts:
        rows = list(conn.execute(
            "SELECT id, run_date, subject, sender, disposition FROM messages "
            "WHERE account=? AND (message_id IS NULL OR message_id='')", (acct,)))
        if not rows:
            continue
        print(f"\n{acct}  ({len(rows)} unlinked rows)")
        index, by_sender = harvest(acct)
        c = collections.Counter()
        for r in rows:
            subj = (r["subject"] or "").strip().lower()
            mid = pick(index.get(subj, []), r["run_date"]) if subj in index else None
            if mid:
                c["linked"] += 1
                per_disp[r["disposition"]]["linked"] += 1
                updates.append((mid, None, r["id"]))
                continue
            # SECOND PASS: the subject was paraphrased, so try the sender instead and
            # repair the stored subject from the real message while we are there.
            sk = sender_key(r["sender"])
            hit = pick_by_sender(by_sender.get(sk, []), r["run_date"]) if sk else None
            if hit:
                c["by_sender"] += 1
                per_disp[r["disposition"]]["by_sender"] += 1
                updates.append((hit["message_id"], hit["subject"], r["id"]))
                samples.append((r["run_date"], r["sender"], r["subject"], hit["subject"]))
            elif subj in index:
                c["ambiguous"] += 1
                per_disp[r["disposition"]]["ambiguous"] += 1
            else:
                c["gone"] += 1
                per_disp[r["disposition"]]["gone"] += 1
        print(f"    -> linked {c['linked']} by subject, {c['by_sender']} by sender"
              f" (+subject repaired), ambiguous {c['ambiguous']}, unreachable {c['gone']}")
        grand.update(c)

    print("\n" + "=" * 72)
    print(f"{'disposition':<12} {'by subject':>11} {'by sender':>10} {'ambiguous':>11} {'gone':>8}")
    for disp in sorted(per_disp):
        d = per_disp[disp]
        print(f"{disp:<12} {d['linked']:>11} {d['by_sender']:>10} "
              f"{d['ambiguous']:>11} {d['gone']:>8}")
    print(f"{'TOTAL':<12} {grand['linked']:>11} {grand['by_sender']:>10} "
          f"{grand['ambiguous']:>11} {grand['gone']:>8}")
    subj_fixes = sum(1 for _, s, _ in updates if s)
    print(f"\nsubjects to repair from the real message: {subj_fixes}")
    if samples:
        # Sender-only matching is weaker evidence than sender+subject, so show what it
        # would actually rewrite. A silent bulk overwrite of every paraphrased subject is
        # exactly the kind of change to look at before it lands, not after.
        print("\nsample of the subject repairs (paraphrase -> real subject):")
        step = max(1, len(samples) // 18)
        for run_date, sender, old, new in samples[::step][:18]:
            print(f"  {run_date}  {str(sender)[:22]:<22}")
            print(f"      was: {str(old)[:72]}")
            print(f"      now: {str(new)[:72]}")

    if args.apply and updates:
        conn.executemany(
            "UPDATE messages SET message_id=?, subject=COALESCE(?, subject) WHERE id=?",
            updates)
        conn.commit()
        tot = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        have = conn.execute("SELECT COUNT(*) FROM messages WHERE message_id IS NOT NULL "
                            "AND message_id!=''").fetchone()[0]
        print(f"\nAPPLIED {len(updates)} links. Store is now {have} of {tot} linked.")
    elif updates:
        print(f"\n(dry run - {len(updates)} links NOT written; re-run with --apply)")
    conn.close()


if __name__ == "__main__":
    main()
