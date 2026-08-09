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
JOURNAL = ROOT / "deletion-journal.md"

# Anything ever flagged this way is something the owner was meant to look at. A later run
# proposing to bin the same sender is exactly the case worth stopping.
ATTENTION = ("action-needed", "family", "security", "financial")


def _history(conn):
    """What the store already knows about each sender key: kept, attention-flagged, concepts.

    Read once. This is the memory the proposal cannot overwrite, and the reason a sender that
    mattered last month cannot be quietly binned this month.

    KEYED TWO WAYS, DELIBERATELY. `hist[key]` is the whole sender; `hist[(key, category)]` is
    one slice of it. The slice exists because a rule may name (sender, category) - that is the
    only statement about a high-volume notification address that is actually TRUE - and a
    guard that can only reason about the whole address cannot enforce a rule written about a
    slice.

    Reported from the field before this existed: most of the refusals in one sweep were a
    single tracker address, every one of them refused because a handful of OTHER messages from
    that address - a human naming the owner - were rightly kept. Their protection was applied
    to the whole address, so status mail inherited it. Worse, it moved the wrong way under
    ordinary use: deciding to KEEP some mail from an address silently removed the ability to
    bin different mail from it, with nothing warning that it had happened.
    """
    hist = {}
    # DEGRADE, DO NOT VANISH. An older store has no `category` column, and the first version
    # of the sliced query let that raise into a bare `except: return {}` - which reads as "this
    # sender has no history", i.e. nothing is protected. A guard whose memory fails silently
    # fails OPEN, which is the one direction it must never fail in. So: try the slice, fall
    # back to the whole sender, and if even that is unreadable say so loudly rather than
    # returning a confident empty dict.
    rows, sliced = None, True
    for sql in (
        "SELECT sender, disposition, COALESCE(concept,'') concept, "
        "COALESCE(importance,'') importance, COALESCE(category,'') category "
        "FROM messages WHERE sender IS NOT NULL AND sender != ''",
        "SELECT sender, disposition, COALESCE(concept,'') concept, "
        "COALESCE(importance,'') importance, '' category "
        "FROM messages WHERE sender IS NOT NULL AND sender != ''",
    ):
        try:
            rows = conn.execute(sql).fetchall()
            break
        except sqlite3.Error:
            sliced = False
            continue
    if rows is None:
        print("\nWARNING: could not read sender history from the store. This guard is now "
              "working\n         from NO memory of what you have kept - treat every clearance "
              "below as\n         unverified, and fix the store before applying anything.")
        return hist
    if not sliced:
        print("note          : this store has no per-message category, so history is judged "
              "at whole-sender level")
    for sender, disposition, concept, importance, category in rows:
        key = _sender_key(sender)
        if not key:
            continue
        # Both buckets are accumulated from the same row, so the slice can never claim
        # evidence the whole sender does not have.
        targets = [hist.setdefault(key, {"kept": 0, "trashed": 0, "attention": False,
                                         "concepts": set()})]
        if category:
            targets.append(hist.setdefault((key, category),
                                           {"kept": 0, "trashed": 0, "attention": False,
                                            "concepts": set()}))
        for h in targets:
            _accumulate(h, disposition, importance, concept)
    return hist


def _accumulate(h, disposition, importance, concept):
    """Fold one stored row into one history bucket.

    An ELSE branch used to decide what counted as kept, so every disposition that was not the
    single string "trashed" became evidence that the sender was worth keeping. On a read-only
    or connector install that is every row, because there was no way to record "I would bin
    this and cannot" - so the guard refused every sender forever, and did it with
    sound-looking reasons. Judged-disposable is now its own thing.
    """
    if disposition in db.DISPOSABLE:
        h["trashed"] += 1
    elif disposition in db.DELIBERATELY_KEPT:
        h["kept"] += 1
    if importance in ATTENTION:
        h["attention"] = True
    if concept:
        h["concepts"].add(concept)


def judge(msg, prot, hist):
    """Every reason this message must NOT be trashed. Empty list means it may be.

    Reasons are accumulated rather than short-circuited: a report that names one objection
    when three apply invites someone to fix the one and retry.

    SCOPED TO THE SLICE, matching `server.sender_rule_verdict`. Those two functions answer the
    same question - may this be binned? - and for a while they answered it differently: the
    dashboard would call a (sender, category) slice rulable with no reservations while this
    function refused every message in it, in the same store, in the same instant, with no way
    for a reader to tell which one governed. One concept spelled twice, in the code that
    decides what gets deleted.

    The narrowing applies ONLY to evidence drawn from history. The protected-NAME check stays
    at whole-sender level on purpose: if a person is protected, no slice of their mail may be
    binned one label at a time.
    """
    reasons = []
    sender = msg.get("sender") or msg.get("from") or ""
    key = _sender_key(sender) or ""
    concept = msg.get("concept") or ""
    category = msg.get("category") or ""
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

    # The slice when the message carries a category and the store has seen that slice;
    # the whole sender otherwise. Narrower evidence, not weaker evidence - every check below
    # is unchanged and simply runs against the smaller set.
    sliced = bool(category) and (key, category) in hist
    h = hist.get((key, category)) if sliced else hist.get(key)
    scope = ("this sender under %r" % category) if sliced else "this sender"
    if h:
        if h["kept"]:
            reasons.append(f"{scope} has {h['kept']} kept or surfaced message(s) on "
                           f"record - not pure noise")
        if h["attention"]:
            reasons.append(f"{scope} has been flagged as needing attention before")
        hit = h["concepts"] & prot["concepts"]
        if hit:
            reasons.append("%s has history in a protected category: %s"
                           % (scope, ", ".join(sorted(hit))))
    return reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proposal", help="run JSON written by the triage step")
    ap.add_argument("--apply", action="store_true",
                    help="actually move the survivors to Trash (default: decide only)")
    ap.add_argument("--account", help="limit to one mailbox")
    ap.add_argument("--emit-cleared", metavar="PATH", dest="emit_cleared",
                    help="write the guard's cleared set to PATH as JSON and act on nothing. "
                         "For installs with no IMAP: your client executes exactly this list, "
                         "then feeds a receipt back with `ingest.py --file receipt.json`.")
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

    # HOW THIN IS THE EVIDENCE BEHIND A CLEARANCE - reported instead of thresholded.
    #
    # The slice fix means a (sender, category) slice with one prior binned message and nothing
    # kept will clear, where the whole sender would have been refused. A minimum-evidence floor
    # was the obvious guard against that, and measuring the store argued against it: of 138
    # such thin slices, every single one sits in a noise label - promo, social-notification,
    # marketing, junk - and not one is money, security, family or medical. That is not luck.
    # A slice only accumulates disposable history because the triager kept judging it
    # disposable, so thin slices self-select for noise.
    #
    # A floor would also add latency to exactly the labels nobody disputes, while protecting
    # nothing the protected-name, protected-category and attention checks do not already cover.
    # So the honest move is the project's usual one: do not threshold it, SHOW it. If this line
    # ever names something that is not noise, that is the evidence for a floor - and it will be
    # evidence rather than a hunch.
    thin = []
    for m, _ in allowed:
        key = _sender_key(m.get("sender") or m.get("from") or "") or ""
        cat = m.get("category") or ""
        h = hist.get((key, cat))
        if h and h["trashed"] <= 2:
            thin.append((m, h["trashed"]))
    if thin:
        print(f"  of those, {len(thin)} rest on THIN evidence (<=2 prior binned, none kept):")
        for m, n in thin[:8]:
            print("    %-34s %-20s %d prior"
                  % ((m.get("sender") or "?")[:34], (m.get("category") or "-")[:20], n))
        if len(thin) > 8:
            print("    ... and %d more" % (len(thin) - 8))

    if args.emit_cleared:
        n = emit_cleared(allowed, args.emit_cleared)
        print(f"\nwrote {n} cleared message(s) to {args.emit_cleared}")
        print("  Execute exactly that list in your client, then record what actually moved:")
        print("    python dashboard/ingest.py --file receipt.json")
        print("    receipt.json = {\"disposed\": [\"<message-id>\", ...]}")
        print("  Nothing was moved here.")
        return 1 if refused else 0

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
    journalled = journal_disposals(allowed, done_ids)
    print(f"\napplied {moved} of {len(messages)} proposed.")
    if promoted:
        print(f"record updated: {promoted} row(s) would_trash -> trashed.")
    if journalled:
        print(f"journal updated: {journalled} line(s) appended to {JOURNAL.name}.")
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

    The UPDATE itself lives in `db.record_disposed`, shared with the receipt path in
    `ingest.py`. Two files answering "was this binned?" is the drift this project keeps
    paying for; there is one answer and both callers ask it.
    """
    ids = [mid for _, mid in moved if mid]
    if not conn or not ids:
        return 0
    try:
        return db.record_disposed(conn, ids)
    except sqlite3.Error as e:
        # Never fatal: the mail HAS moved, and failing here must not make a successful
        # disposal look like a failed one.
        print(f"  (could not update the record: {type(e).__name__}: {e})")
        return 0


def emit_cleared(allowed, path):
    """Write what the guard CLEARED, so an install that cannot act still gets the verdict.

    `ingest.py` opens with a promise it keeps - BRING YOUR OWN FETCHER, this takes plain JSON
    and needs no IMAP. There was no counterpart for the ACT half, and no seam either: this
    program hard-codes one executor and keys on a UID, which is per-folder IMAP state that a
    connector ingest never produces. So a connector install could propose forever and never
    dispose - `CLEARED 28 of 133` followed by `applied 0`, the guard reaching a verdict that
    was unexecutable. The propose/dispose split was half implemented for exactly the
    population `ingest.py` was written to rescue.

    "Then act in your client" is not the answer, and the reason is the whole point of this
    program. It earns its keep by being an ORDINARY PROGRAM: it reads no message bodies, calls
    no model, and re-derives every entitlement from the store before anything moves. Move
    execution into an AI client and that property is gone - the thing reading attacker-written
    text becomes the thing deleting mail. This file is the boundary.

    So the model never decides what is deleted here either. It carries out a decision a
    program already made, and it CANNOT ADD TO THE LIST: anything absent from this file was
    refused. `message_id` is the key in both directions because it is durable and a UID is
    not - and because the connector world has nothing else.
    """
    out = {
        "generated_by": "apply_proposal.py --emit-cleared",
        "note": ("Every message here was cleared by the guard. Execute exactly this list - "
                 "nothing added. Then send back {\"disposed\": [message_id, ...]} through "
                 "ingest.py so the store records what actually moved."),
        "cleared": [{
            "account": m.get("account"),
            "message_id": m.get("message_id"),
            "web_link": m.get("web_link"),
            "subject": m.get("subject"),
            "sender": m.get("sender"),
            # Why it SURVIVED the guard, not why it was proposed - the proposal's own reason
            # came from the triager and is not what entitles anything.
            "cleared_because": "no objection from the protected list, the attention flags, "
                               "the injection labels, or this sender's recorded history",
        } for m, _why in allowed],
    }
    p = Path(path)
    if p.parent and str(p.parent):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(out["cleared"])


def _journal_cell(s):
    """One table cell: no pipes, no newlines, or the row stops being a row."""
    return (str(s or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip() or "-")


def journal_disposals(allowed, moved, journal=None, today=None):
    """Write the deletion-journal line in the SAME call that moved the mail.

    WHY THIS LIVES HERE NOW. On 2026-08-08 I found six messages sitting in Trash with no
    journal entry at all. Nothing was lost - they were recoverable the whole time and the
    store had them recorded correctly as `trashed` - but the journal is the promise this lane
    makes about deletion, and for a night it was not kept.

    The cause was structural, not careless. This program wrote back to the STORE and stopped;
    appending to the markdown journal was a separate hand-run step in the routine, and an
    evening session that ended after the disposal never reached it. So the paper trail
    depended on a human-shaped step happening after a machine-shaped one, which is exactly the
    kind of coupling that holds until the first time it does not.

    Same shape as `record_disposals` and the same rule: the record and the act must not be
    able to come apart. Only messages this run actually MOVED are written, matched on
    Message-ID, so a refusal or a skipped uid never produces a line claiming a deletion that
    did not happen. Failure direction is deliberate: a missing line is caught by the next
    run's reconciliation, while a line for mail still in the inbox is a lie in the ledger.
    """
    journal = Path(journal) if journal else JOURNAL
    ids = {mid for _, mid in moved if mid}
    if not ids:
        return 0
    from datetime import date
    today = today or date.today().isoformat()

    try:
        existing = journal.read_text(encoding="utf-8") if journal.exists() else ""
    except OSError as e:
        print(f"  (could not read the journal: {type(e).__name__}: {e})")
        return 0

    lines = []
    for m, _why in allowed:
        mid = (m.get("message_id") or "").strip()
        if mid not in ids:
            continue
        subject = _journal_cell(m.get("subject"))
        account = _journal_cell(m.get("account"))
        # Idempotent: re-running the applier must not double-write a day's line.
        if f"| {today} | {account} |" in existing and subject in existing:
            continue
        lines.append("| %s | %s | %s | %s | %s |" % (
            today, account, _journal_cell(m.get("sender")), subject,
            _journal_cell(m.get("reason")) + " [journalled by apply_proposal]"))

    if not lines:
        return 0
    try:
        with journal.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(lines) + "\n")
    except OSError as e:
        # Never fatal, for the same reason record_disposals is not: the mail HAS moved, and
        # failing to describe it must not make a successful disposal look like a failed one.
        print(f"  (could not write the journal: {type(e).__name__}: {e})")
        return 0
    return len(lines)


if __name__ == "__main__":
    sys.exit(main())
