"""Apply a run's trash proposal - as a program, not as an agent.

THE GAP THIS CLOSES. The triage agent reads sender names, subjects and body snippets: text
written by anyone who knows the address. That text flows into the same context that decides
what happens to the message. If the same agent also holds the power to trash, then a crafted
email is one step from influencing what gets hidden - and the realistic harm is not "an
attacker deleted my mail", because Trash is recoverable. It is quieter: a genuine security
alert marked unimportant so it never reaches the person. The whole purpose of this tool is
deciding what a human sees, which makes PERCEPTION the asset worth attacking.

So the run is split in two. The agent CLASSIFIES and writes a proposal. This program DISPOSES,
and it re-derives every entitlement from the store and the protected list rather than
believing the proposal. A proposal is a request, exactly like a click on the dashboard - and
the dashboard already refuses to trust those.

WHAT THIS PROGRAM NEVER DOES: read a message body, call a model, or take an instruction from
anything a sender wrote. It reads structured fields and stored history. That is the property
that matters, and it is a property of the architecture rather than of anyone's vigilance.

WHY THE ATTACKER-CONTROLLED FIELDS ARE STILL SAFE TO MATCH ON. `sender` and `subject` in the
proposal come from the mail, so a sender controls them. They are used only to look for
reasons to REFUSE - so the worst an attacker achieves by forging them is that their own mail
is protected from the bin. Every error the forgery can cause falls on the conservative side.

    python tools/apply_proposal.py run.json                 # dry run: decide, change nothing
    python tools/apply_proposal.py run.json --apply         # actually move to Trash
    python tools/apply_proposal.py run.json --apply --account one@example.com

Exit codes: 0 applied cleanly, 1 refused something (read the report), 2 refused everything
because the guard is not configured.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "dashboard"))
from server import (_sender_key, load_protected, protected_hit)          # noqa: E402
import db  # noqa: E402
import untrusted  # noqa: E402

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "dashboard"))
from consoleio import safe_console            # noqa: E402
safe_console()


DB = ROOT / "dashboard" / "email_dashboard.db"
MAILTOOL = HERE / "mailtool.py"

# Anything ever flagged this way is something the owner was meant to look at. A later run
# proposing to bin the same sender is exactly the case worth stopping.
ATTENTION = ("action-needed", "family", "security", "financial")


def _history(conn):
    """What the store already knows about each sender key: kept, attention-flagged, concepts.

    Read once. This is the memory the proposal cannot overwrite, and the reason a sender that
    mattered last month cannot be quietly binned this month.
    """
    hist = {}
    try:
        rows = conn.execute(
            "SELECT sender, disposition, COALESCE(concept,'') concept, "
            "COALESCE(importance,'') importance FROM messages "
            "WHERE sender IS NOT NULL AND sender != ''")
    except sqlite3.Error:
        return hist
    for sender, disposition, concept, importance in rows:
        key = _sender_key(sender)
        if not key:
            continue
        h = hist.setdefault(key, {"kept": 0, "trashed": 0, "attention": False,
                                  "concepts": set()})
        # An ELSE branch decided what counted as kept, so every disposition that was not the
        # single string "trashed" became evidence that the sender was worth keeping. On a
        # read-only or connector install that is every row, because there was no way to
        # record "I would bin this and cannot" - so the guard refused every sender forever,
        # and did it with sound-looking reasons. Judged-disposable is now its own thing.
        if disposition in db.DISPOSABLE:
            h["trashed"] += 1
        elif disposition in db.DELIBERATELY_KEPT:
            h["kept"] += 1
        if importance in ATTENTION:
            h["attention"] = True
        if concept:
            h["concepts"].add(concept)
    return hist


def judge(msg, prot, hist):
    """Every reason this message must NOT be trashed. Empty list means it may be.

    Reasons are accumulated rather than short-circuited: a report that names one objection
    when three apply invites someone to fix the one and retry.
    """
    reasons = []
    sender = msg.get("sender") or msg.get("from") or ""
    key = _sender_key(sender) or ""
    concept = msg.get("concept") or ""
    importance = msg.get("importance") or ""

    if protected_hit(prot, sender) or (key and protected_hit(prot, key)):
        reasons.append("sender is on your protected list")
    if concept and concept in prot["concepts"]:
        reasons.append(f"protected category: {concept}")
    if importance in ATTENTION:
        reasons.append(f"this run flagged it as {importance}")
    if msg.get("injection_signals"):
        # Mail that tried to steer the triager does not get quietly binned by that same
        # triager's decision. Surface it to a person instead.
        reasons.append("carries injection signals - needs a human look, not a silent bin")

    h = hist.get(key)
    if h:
        if h["kept"]:
            reasons.append(f"this sender has {h['kept']} kept or surfaced message(s) on "
                           f"record - not pure noise")
        if h["attention"]:
            reasons.append("this sender has been flagged as needing attention before")
        hit = h["concepts"] & prot["concepts"]
        if hit:
            reasons.append("sender has history in a protected category: "
                           + ", ".join(sorted(hit)))
    return reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proposal", help="run JSON written by the triage step")
    ap.add_argument("--apply", action="store_true",
                    help="actually move the survivors to Trash (default: decide only)")
    ap.add_argument("--account", help="limit to one mailbox")
    args = ap.parse_args()

    with open(args.proposal, encoding="utf-8-sig") as f:
        run = json.load(f)
    # DISPOSABLE, not just "trashed". A read-only triage - which is what the skill mandates,
    # and the only thing a connector install can do - proposes `would_trash`. Reading only
    # `trashed` would mean the applier found nothing to consider in precisely the proposals
    # this propose/dispose split exists to serve.
    messages = [m for m in (run.get("messages") or [])
                if (m.get("disposition") or "") in db.DISPOSABLE
                and (not args.account
                     or (m.get("account") or "").lower() == args.account.lower())]

    # LABEL THE PROPOSAL HERE TOO, whatever produced it. The injection guard below refuses
    # anything carrying signals - but if the only labeller was the fetcher, then on an
    # install that cannot run one the guard had nothing to refuse and failed open silently.
    # Re-labelling is idempotent, so a proposal already marked by a fetcher is unchanged.
    flagged = untrusted.annotate_all(messages)

    prot = load_protected()
    print(f"proposal      : {args.proposal}")
    print(f"proposed trash: {len(messages)} message(s)"
          + (f" in {args.account}" if args.account else ""))

    if not prot["configured"]:
        # FAIL CLOSED, and loudly. Without the guard there is nothing to check a proposal
        # against, and "no list" must never be read as "nobody is protected".
        print("\nREFUSING EVERYTHING: the protected-sender guard is not configured.")
        print(f"  {prot['why']}")
        print("  Nothing was applied. Fill the list in (the dashboard can do it) and re-run.")
        return 2

    conn = sqlite3.connect(DB) if DB.exists() else sqlite3.connect(":memory:")
    hist = _history(conn)
    print(f"guard         : {len(prot['names'])} protected name(s), "
          f"{len(prot['concepts'])} protected category(ies)")
    print(f"injection     : {flagged} of {len(messages)} carry signals "
          f"(labelled here, not trusted from the caller)")
    print(f"history       : {len(hist)} sender(s) with recorded messages")

    # IS THIS GUARD CAPABLE OF SAYING YES?
    #
    # `REFUSED 6 of 6` with stacked, specific, correct reasons is indistinguishable from a
    # healthy guard doing its job - and on a store where no sender has ever had a message
    # judged disposable, the "not pure noise" rule refuses EVERY sender by construction. Not
    # because anything is protected. Because there is no evidence of noise for anything.
    #
    # That is the state a fresh install is in, and the state a read-only or connector install
    # stays in until the routine starts recording would_trash. A guard that cannot currently
    # pass anything should say so, rather than presenting as a guard that happens to refuse -
    # the same courtesy `doctor` extends with NOT CONFIGURED and the scoreboard extends with
    # "not measured is not zero".
    with_noise = sum(1 for h in hist.values() if h["trashed"])
    if hist and not with_noise:
        print("\nNOTE: no sender in this store has a single message recorded as trashed or "
              "would_trash,")
        print("      so the \"not pure noise\" rule will refuse every sender no matter what "
              "you propose.")
        print("      This is a fresh or read-only install, NOT a set of protected senders. A "
              "read-only")
        print("      pass can record `would_trash` - judged disposable, not acted on - which "
              "gives this")
        print("      guard real evidence to weigh instead of the absence of an impossible "
              "action.")
    print()

    allowed, refused = [], []
    for m in messages:
        why = judge(m, prot, hist)
        (refused if why else allowed).append((m, why))

    if refused:
        print(f"REFUSED {len(refused)} of {len(messages)}:")
        for m, why in refused:
            print(f"  - {(m.get('sender') or '?')[:44]}")
            print(f"    {(m.get('subject') or '')[:66]}")
            for r in why:
                print(f"      * {r}")
        print()

    print(f"CLEARED {len(allowed)} of {len(messages)} to trash.")
    if not args.apply:
        print("\n(dry run - nothing moved. Re-run with --apply to act on the cleared set.)")
        return 1 if refused else 0

    # Group by account: mailtool takes UIDs per mailbox, and a UID means nothing without one.
    by_account = {}
    # The Message-IDs that go with those uids, kept in step so the record can be updated
    # afterwards. The uid moves the mail; the Message-ID is what the store is keyed on, and
    # a uid is stale the moment the message lands in Trash.
    by_message = {}
    for m, _ in allowed:
        uid = str(m.get("uid") or "").strip()
        acct = m.get("account")
        if uid and acct:
            by_account.setdefault(acct, []).append(uid)
            by_message.setdefault(acct, []).append((acct, (m.get("message_id") or "").strip()))
    moved = 0
    done_ids = []
    for acct, uids in by_account.items():
        r = subprocess.run(
            [sys.executable, str(MAILTOOL), "act", "--account", acct,
             "--uids", ",".join(uids), "--action", "trash"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            moved += len(uids)
            done_ids += [(acct, mid) for a2, mid in by_message.get(acct, []) if a2 == acct]
            print(f"  {acct}: moved {len(uids)} to Trash")
        else:
            print(f"  {acct}: FAILED - {(r.stderr or r.stdout)[-300:]}")
    skipped = len(allowed) - sum(len(v) for v in by_account.values())
    if skipped:
        print(f"  ({skipped} cleared message(s) had no uid/account and were left alone)")

    promoted = record_disposals(conn, done_ids)
    print(f"\napplied {moved} of {len(messages)} proposed.")
    if promoted:
        print(f"record updated: {promoted} row(s) would_trash -> trashed.")
    return 1 if refused else 0


def record_disposals(conn, moved):
    """Tell the store what was actually DONE. The other half of propose/dispose.

    The applier moved mail and never wrote back, so the record said `would_trash` -
    "judged disposable, NOT acted on" - about messages that had in fact been acted on. The
    mirror image of the defect that created `would_trash` in the first place: there, the
    store overstated a judgment it had not made; here it understates an action it did take.

    It matters beyond tidiness. `would_trash` and `trashed` are both DISPOSABLE, so the guard
    is not misled - but "did the routine actually bin this?" had no answer anywhere, and the
    run row went on reporting `trashed 0` while messages were in the Trash folder. A record
    that cannot distinguish what was decided from what was done is exactly the thing this
    vocabulary was introduced to fix.

    Only rows this run actually moved, matched on Message-ID, and only ones still sitting at
    `would_trash` - never a row somebody has since re-triaged by hand.
    """
    ids = [mid for _, mid in moved if mid]
    if not conn or not ids:
        return 0
    try:
        cur = conn.execute(
            "UPDATE messages SET disposition = 'trashed' "
            "WHERE disposition = 'would_trash' AND message_id IN (%s)"
            % ",".join("?" * len(ids)), ids)
        n = cur.rowcount
        # The run row counts what was DONE, so it has to move with them or the day's own
        # numbers disagree with the list underneath.
        conn.execute(
            "UPDATE runs SET trashed = trashed + ?, kept = MAX(0, kept - 0) "
            "WHERE run_date IN (SELECT DISTINCT run_date FROM messages "
            "WHERE message_id IN (%s))" % ",".join("?" * len(ids)), [n] + ids)
        conn.commit()
        return n
    except sqlite3.Error as e:
        # Never fatal: the mail HAS moved, and failing here must not make a successful
        # disposal look like a failed one.
        print(f"  (could not update the record: {type(e).__name__}: {e})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
