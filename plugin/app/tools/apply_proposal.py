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
from server import (_sender_key, load_protected, protected_hit,          # noqa: E402
                    protected_names_hit)
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
    #
    # THE LADDER MUST END IN A QUERY THAT ASKS FOR THE BARE MINIMUM. When `run_date` was added
    # here (2026-08-11, so a refusal could say when its evidence was taken) it went into BOTH
    # rungs - so a store without that column failed every rung and the guard lost its memory
    # entirely, which is the precise failure this comment was written to prevent. Caught by
    # building such a store and asking. Each new column gets its own rung, and the LAST rung
    # asks only for what has always existed: a nicety must never be able to cost the memory.
    rows, sliced, dated = None, True, True
    for sql, has_cat, has_date in (
        ("SELECT sender, disposition, COALESCE(concept,'') concept, "
         "COALESCE(importance,'') importance, COALESCE(category,'') category, "
         "COALESCE(run_date,'') run_date "
         "FROM messages WHERE sender IS NOT NULL AND sender != ''", True, True),
        ("SELECT sender, disposition, COALESCE(concept,'') concept, "
         "COALESCE(importance,'') importance, COALESCE(category,'') category "
         "FROM messages WHERE sender IS NOT NULL AND sender != ''", True, False),
        ("SELECT sender, disposition, COALESCE(concept,'') concept, "
         "COALESCE(importance,'') importance "
         "FROM messages WHERE sender IS NOT NULL AND sender != ''", False, False),
    ):
        try:
            got = conn.execute(sql).fetchall()
        except sqlite3.Error:
            continue
        # Normalise to (sender, disposition, concept, importance, category, run_date) so one
        # unpacking serves every rung.
        rows = [tuple(r) + ("",) * (6 - len(r)) for r in got]
        sliced, dated = has_cat, has_date
        break
    if rows is None:
        print("\nWARNING: could not read sender history from the store. This guard is now "
              "working\n         from NO memory of what you have kept - treat every clearance "
              "below as\n         unverified, and fix the store before applying anything.")
        return hist
    if not sliced:
        print("note          : this store has no per-message category, so history is judged "
              "at whole-sender level")
    if not dated:
        print("note          : this store has no per-message run_date, so a refusal can say "
              "WHY but not WHEN")
    for sender, disposition, concept, importance, category, run_date in rows:
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
            _accumulate(h, disposition, importance, concept, run_date)
    return hist


def _accumulate(h, disposition, importance, concept, run_date=None):
    """Fold one stored row into one history bucket.

    An ELSE branch used to decide what counted as kept, so every disposition that was not the
    single string "trashed" became evidence that the sender was worth keeping. On a read-only
    or connector install that is every row, because there was no way to record "I would bin
    this and cannot" - so the guard refused every sender forever, and did it with
    sound-looking reasons. Judged-disposable is now its own thing.

    `oldest_kept` / `newest_kept` are RECORDED, never weighed. Nothing in `judge` reads them
    and nothing should: how old a keep is does not make it less of a keep, and a guard that
    quietly expired its own memory would fail open exactly where it matters. They exist so a
    refusal can SAY when its evidence was taken - which is what makes it visible that a
    refusal rests entirely on keeps predating a ruling that has since changed the answer.
    """
    if disposition in db.DISPOSABLE:
        h["trashed"] += 1
    elif disposition in db.DELIBERATELY_KEPT:
        h["kept"] += 1
        if run_date:
            if not h.get("oldest_kept") or run_date < h["oldest_kept"]:
                h["oldest_kept"] = run_date
            if not h.get("newest_kept") or run_date > h["newest_kept"]:
                h["newest_kept"] = run_date
    if importance in ATTENTION:
        h["attention"] = True
    if concept:
        h["concepts"].add(concept)


def normalise_senders(messages):
    """Accept `from` as an alias for `sender`, ONCE, at the door.

    `ingest.py` has taken this alias for a while, because hand-written run JSON keeps drifting
    to `from` - that drift once left the top-senders view under-counting for four runs. The
    alias was added there, where the bug was felt, and nowhere else.

    This file had it too, but only in its REASONING paths: every entitlement lookup reads
    `msg.get("sender") or msg.get("from")`, so a `from`-keyed proposal is judged correctly.
    Every RECORDING path took the bare `m.get("sender")` - the journal cell, the
    `disposal_refusals` insert, the console list. So a `from`-keyed run binned the right
    messages, refused the right ones, and wrote every journal line with `-` where the sender
    belongs, plus refusal rows with sender NULL. Nothing failed and every count agreed; the
    only thing missing was who had sent them, and `-` reads as "no sender" rather than "the key
    was spelled differently".

    Normalising here rather than at each use is the point. Adding `or m.get("from")` to three
    more call sites fixes today and leaves the next recording path to rediscover it - the
    fix-does-not-reach-its-siblings shape. A normalisation that has already run cannot be
    missed by a path added downstream of it.

    `sender` always wins when both are present: the alias stands in for the real key, it does
    not override it.

    IT ALSO UNFOLDS THE VALUE, for the same reason it aliases the key. RFC 5322 lets a long
    header be broken across lines with the continuation indented by whitespace, and a reader is
    meant to join it back before using it. Ours did not, so one sender reached the store under
    two spellings that differ only by a `\r\n `:

        'Example Sender <noreply@example.com>'
        'Example Sender\r\n <noreply@example.com>'

    Nothing errors and every count stays internally consistent; the only effect is that every
    instrument keyed on the sender string reports a number smaller than the truth. It surfaced
    in the refusals panel on the one refusal that mattered most - the row proving a standing
    auto-trash rule had quietly stopped executing appeared twice, as `runs=8 self_feeding=True`
    and, as its folded twin, `runs=1 self_feeding=False`. The split resets the clock and drops
    the flag, so the instrument dims exactly in proportion to the severity of what it exists to
    reveal. Same shape as the grouping-key defect (keying on reason text that grows), one
    field over: never key on a string the transport is free to reformat.

    Unfolding here, at the door, rather than in each consumer is the point - see above.
    Whitespace INSIDE the value is collapsed to single spaces, which is what unfolding means;
    a sender with no folding in it is returned byte-for-byte unchanged, and that control is a
    test leg, because a normalisation that rewrites clean input is a new bug.
    """
    for m in messages or []:
        if not m.get("sender") and m.get("from"):
            m["sender"] = m["from"]
        s = m.get("sender")
        if s and ("\n" in s or "\r" in s):
            m["sender"] = " ".join(s.split())
    return messages


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

    # Name the entry that fired. The check is unchanged - only its explanation is. A refusal
    # that says "protected" and nothing else cannot be audited, and a protected entry that is
    # matching the wrong sender then looks exactly like one that is doing its job.
    matched = protected_names_hit(prot, sender)
    if key:
        matched += [n for n in protected_names_hit(prot, key) if n not in matched]
    if matched:
        reasons.append("sender matches protected list entry: %s"
                       % ", ".join(repr(n) for n in matched))
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
    run_date = run.get("run_date") or ""
    # DISPOSABLE, not just "trashed". A read-only triage - which is what the skill mandates,
    # and the only thing a connector install can do - proposes `would_trash`. Reading only
    # `trashed` would mean the applier found nothing to consider in precisely the proposals
    # this propose/dispose split exists to serve.
    messages = [m for m in (run.get("messages") or [])
                if (m.get("disposition") or "") in db.DISPOSABLE
                and (not args.account
                     or (m.get("account") or "").lower() == args.account.lower())]

    normalise_senders(messages)

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
            key = _sender_key(m.get("sender") or m.get("from") or "") or ""
            h = hist.get((key, m.get("category") or "")) or hist.get(key) or {}
            if h.get("oldest_kept"):
                print("      (evidence: keeps from %s to %s)"
                      % (h["oldest_kept"], h.get("newest_kept") or h["oldest_kept"]))
        # Recorded whether or not we go on to apply: the verdict is real either way, and a
        # dry run is the commonest way it is reached. Without this the reason exists only in
        # a terminal nobody kept, and stranded_scan is left reporting the union of "refused"
        # and "failed" - a list in which a correct refusal is indistinguishable from a bug.
        n = record_refusals(conn, refused, run_date, hist)
        if n:
            print(f"  recorded {n} refusal reason(s) so the stranded scan can explain them.")
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

    # DID MY LABEL MOVE THIS MESSAGE ACROSS THE DELETE LINE? (Q44 option (a))
    #
    # Everything above asks whether the guard was right. This asks the other question, which
    # had no asker: did the guard's INPUT change? A refusal I can dissolve by relabelling is
    # only as strong as my labelling discipline, and from outside an honest correction and an
    # abuse look identical. This changes no verdict - it ends the silence.
    entries = [(m, "cleared") for m, _ in allowed] + [(m, "refused") for m, _ in refused]
    flips = label_flips(entries, prior_verdicts(
        conn, [m.get("message_id") for m, _ in entries], run_date))
    if flips:
        print()
        print(f"!! LABEL FLIP - {len(flips)} message(s) changed CATEGORY and changed VERDICT:")
        for f in flips:
            print(f"  - {str(f['sender'])[:44]}")
            print(f"    {str(f['subject'])[:66]}")
            print("      %s (%s)  ->  %s (%s)"
                  % (f["old_category"], f["old_verdict"],
                     f["new_category"], f["new_verdict"]))
            print(f"      last judged {f['old_run_date']}")
        print("  Name each of these in the run report and say which label is right.")
        print("  This is a report, not a refusal - the verdicts above stand.")

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
    unresolved = []
    for m, _ in allowed:
        uid = str(m.get("uid") or "").strip()
        acct = m.get("account")
        mid = (m.get("message_id") or "").strip()
        if not uid and acct and mid:
            uid = locate_uid(acct, mid)
        if uid and acct:
            by_account.setdefault(acct, []).append(uid)
            by_message.setdefault(acct, []).append((acct, mid))
        else:
            unresolved.append(m)
    moved = 0
    done_ids = []
    failed_accounts = []
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
            failed_accounts.append((acct, len(uids), (r.stderr or r.stdout).strip()))
            print(f"  {acct}: FAILED - {(r.stderr or r.stdout)[-300:]}")
    promoted = record_disposals(conn, done_ids)
    journalled = journal_disposals(allowed, done_ids)
    # A message that has now MOVED must not keep yesterday's refusal note: an explanation for
    # a disposal that has since happened is more misleading than no explanation at all.
    cleared_notes = clear_refusals(conn, done_ids)
    print(f"\napplied {moved} of {len(messages)} proposed.")
    if promoted:
        print(f"record updated: {promoted} row(s) would_trash -> trashed.")
    if journalled:
        print(f"journal updated: {journalled} line(s) appended to {JOURNAL.name}.")
    if cleared_notes:
        print(f"refusal notes : {cleared_notes} cleared (these moved, so the reason is spent).")

    if unresolved:
        # A cleared message that cannot be located is the WORST outcome available here: the
        # guard said yes, nothing moved, and the old code said so in one parenthetical and
        # exited 0. That happened against a proposal written with no uids at all - it moved
        # nothing, reported `applied 0`, and read as a clean run, while the mail it had just
        # been cleared to bin stayed in the INBOX. Silence is the defect, so this shouts and
        # fails.
        print(f"\n!! {len(unresolved)} CLEARED message(s) COULD NOT BE LOCATED and were NOT moved.")
        print("   The guard said yes and nothing happened. Not a refusal - a miss.")
        for m in unresolved:
            print(f"   - {(m.get('account') or '?')}: {(m.get('subject') or '')[:60]}")
        print("   Likely cause: no uid in the proposal AND no Message-ID match in INBOX.")

    if failed_accounts:
        # SAME defect as `unresolved`, one branch over, and the fix above never reached it: the
        # guard said yes and the MOVE ITSELF failed. That path printed one FAILED line per
        # account and still exited 0, so a run that disposed of NOTHING - every account refused
        # by the read-only guard, a dropped connection, a revoked app password - read as clean
        # to any caller that checks the status code. Observed in the field: every account
        # refused, nothing applied, and the process still exited 0. A miss by transport is not
        # more forgivable than a miss by lookup; both are the guard saying yes and nothing
        # happening.
        total = sum(n for _, n, _ in failed_accounts)
        print()
        print(f"!! {total} CLEARED message(s) were NOT moved: {len(failed_accounts)} account(s) FAILED.")
        print("   The guard said yes and the move itself did not happen. Not a refusal - a failure.")
        for acct, n, err in failed_accounts:
            print(f"   - {acct}: {n} message(s) not moved - {err.splitlines()[0][:120] if err else 'no error text'}")

    return 1 if (refused or unresolved or failed_accounts) else 0


def locate_uid(account, message_id):
    """Resolve a Message-ID to its CURRENT INBOX uid, or return "" if it is not there.

    A uid is per-folder and is reassigned on move, so it goes stale between the read pass and
    the disposal - which is exactly why the record is keyed on Message-ID instead. That left
    the mover with the one identifier the proposal is least able to carry, and a proposal
    written without it moved nothing at all, quietly (2026-08-08).

    INBOX only, deliberately. A hit in Trash means the message is already disposed; "moving"
    it again would journal a deletion that this run did not perform. Not-found is a normal
    answer here, not an error.

    This reads no body: `find --locate` stops at the SEARCH and never issues the FETCH, so the
    disposer keeps its property of taking no instruction from anything a sender wrote.
    """
    try:
        r = subprocess.run(
            [sys.executable, str(MAILTOOL), "find", "--account", account,
             "--message-id", message_id, "--locate"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    try:
        got = json.loads((r.stdout or "").strip() or "{}")
    except ValueError:
        return ""
    if got.get("found") and str(got.get("mailbox", "")).upper() == "INBOX":
        return str(got.get("uid") or "").strip()
    return ""


def _label_key(category):
    """One spelling for one label. Case and surrounding whitespace are not a relabel.

    The store already splits SENDERS by case (TODO #8) and that costs an undercount; letting
    the same shape through here would cost something worse - a false flip in the one report
    whose value is that everything in it is real.
    """
    return (category or "").strip().lower()


def label_flips(entries, prior):
    """Say when MY label, not the guard, moved a message across the delete line (Q44 (a)).

    The guard re-derives its verdict from the store, but one of its inputs is the `category`
    the triage step writes into the run JSON. Measured in the field: one identical message -
    same Message-ID, same subject - went in under a money label on one run and was REFUSED as
    protected, then under a promo label on the next and was CLEARED and binned. Nothing
    anywhere said the input had changed. A guard whose refusal the triager can dissolve by
    relabelling is only as strong as that triager's labelling discipline - which is the exact
    thing the guard exists not to depend on.

    This does not refuse anything and does not change a verdict; option (b) in Q44 would, and
    is deliberately not built because it would fire on honest corrections. Silence was the
    defect, so the fix is to end the silence.

    Fires only when BOTH changed, against the MOST RECENT prior run that put this message to
    the guard:
      * the label differs from last time, AND
      * the verdict differs from last time.
    A verdict change under a stable label is the guard learning from accumulated history -
    that is it working, and flagging it would train me to ignore this block. A prior row with
    no recorded verdict (`kept` mail was never put to the guard) is not a comparison at all.

    `entries` : [(message, "cleared"|"refused"), ...]
    `prior`   : {message_id: [{"run_date", "category", "verdict"}, ...]} - any order.
    """
    out = []
    for m, verdict in entries:
        mid = m.get("message_id")
        if not mid:
            # No identity, so nothing to compare against. Guessing one from sender+subject
            # is how a detector starts inventing findings.
            continue
        rows = [r for r in (prior.get(mid) or []) if r.get("verdict")]
        if not rows:
            continue
        last = max(rows, key=lambda r: r.get("run_date") or "")
        if _label_key(last.get("category")) == _label_key(m.get("category")):
            continue
        if (last.get("verdict") or "") == verdict:
            continue
        out.append({
            "message_id": mid,
            "sender": m.get("sender") or m.get("from") or "?",
            "subject": m.get("subject") or "",
            "old_category": last.get("category"),
            "new_category": m.get("category"),
            "old_verdict": last.get("verdict"),
            "new_verdict": verdict,
            "old_run_date": last.get("run_date"),
        })
    return out


def prior_verdicts(conn, message_ids, run_date):
    """Read each message's label and guard verdict from EARLIER runs.

    The verdict is not a column, so it is reconstructed from the two places the store does
    record it:

      * `trashed`     -> the guard CLEARED it and it was moved;
      * `would_trash` + a `disposal_refusals` row dated AT OR AFTER that run -> REFUSED;
      * anything else -> None, so `label_flips` will not read it as a comparison. `kept` and
        `surfaced` mail was never put to the guard, and a `would_trash` with no refusal row is
        the stranded-scan "unexplained" shape - a disposal that never happened. Calling either
        of those `cleared` would manufacture a flip out of nothing.

    THE DATE COMPARISON IS THE WHOLE SUBTLETY. `disposal_refusals` is keyed
    `message_id PRIMARY KEY` - ONE row per message, holding only the LATEST refusal - so a
    message refused on several consecutive runs has one row bearing the newest date. The first
    version of this reader joined on (message_id, run_date) and filtered `run_date < run_date`,
    which could never match a still-refused message: yesterday's verdict is recorded under
    today's date. It reported "no verdict" for a refusal `stranded_scan` was printing the same
    morning, and it was a positive control on live rows that caught it, not the unit tests.

    KNOWN LIMIT, stated rather than papered over: once a refused message is finally binned,
    `clear_refusals` deletes its refusal row and `record_disposed` rewrites every row sharing
    that Message-ID to `trashed`. The store then holds no trace that it was ever refused. So
    this reader cannot see a flip that has ALREADY COMPLETED - including the very case that
    raised the question. A backward scan over a mature store therefore reports zero historical
    flips, and that zero is a fact about what the record keeps, not a clean bill of health.
    Going forward it works, because detection runs BEFORE this run's own disposal erases the
    previous verdict - an ordering worth preserving if either step ever moves.
    """
    if not conn or not message_ids:
        return {}
    ids = [i for i in message_ids if i]
    if not ids:
        return {}
    prior = {}
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "SELECT message_id, run_date, category, disposition FROM messages "
            "WHERE message_id IN (%s) AND run_date < ?" % marks, ids + [run_date]).fetchall()
        refused_rows = conn.execute(
            "SELECT message_id, run_date FROM disposal_refusals "
            "WHERE message_id IN (%s)" % marks, ids).fetchall()
    except sqlite3.Error as e:
        # Never fatal - this is a report about the run, not part of deciding it.
        print(f"  (could not check for label flips: {type(e).__name__}: {e})")
        return {}
    # One row per message, carrying the LATEST refusal date - see the docstring.
    refused_on = {r[0]: (r[1] or "") for r in refused_rows}
    for mid, rd, cat, disp in rows:
        if disp == "trashed":
            verdict = "cleared"
        elif disp in db.DISPOSABLE and refused_on.get(mid, "") >= (rd or ""):
            verdict = "refused"
        else:
            verdict = None
        prior.setdefault(mid, []).append(
            {"run_date": rd, "category": cat, "verdict": verdict})
    return prior


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


def record_refusals(conn, refused, run_date, hist=None):
    """Write down WHY the guard said no, in the same call that says it.

    The sibling of `record_disposals`, and it exists for the same reason: a decision that
    leaves no trace becomes indistinguishable from a decision nobody made. `would_trash` +
    still-in-INBOX has two causes - refused (correct) or never disposed (a defect) - and
    because the store recorded neither, `stranded_scan` could only print the union. Every
    correct refusal therefore read as a failure, on every run, forever.

    On a mature install that list is dominated by correct refusals, because refused-and-still-
    present is the intended resting state. A list that is very nearly all false positives is
    worse than no list, because it trains the reader to skim - which is precisely where a real
    disposal failure would then land. That is gotcha 127's lesson (a detector that misclassifies
    is worse than noise when its remedy is wrong) arriving in this lane's own instrument.

    Written on EVERY run including a dry run: the refusal is a real verdict whether or not
    anything was moved afterwards, and a dry run is the commonest way it is reached.

    `evidence_from` records the OLDEST run_date behind the keeps the refusal cites. That is
    the field that exposes a refusal resting entirely on history a later owner ruling has
    overturned. That shape is common and invisible if you record only the reason text: a
    sender kept many times under a "keep until ruled" default, then ruled auto-trash, still
    reads as "not pure noise" forever on the strength of keeps the ruling superseded.

    Never fatal. A store that will not take the note must not turn a correct refusal into a
    crash, so every failure here is reported and swallowed.
    """
    if not conn or not refused:
        return 0
    from datetime import datetime
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for m, why in refused:
        mid = (m.get("message_id") or "").strip()
        if not mid:
            continue                       # keyed on Message-ID; a uid is stale by design
        oldest = None
        if hist:
            key = _sender_key(m.get("sender") or m.get("from") or "") or ""
            h = hist.get((key, m.get("category") or ""))
            if h:
                oldest = h.get("oldest_kept")
        rows.append((mid, run_date, m.get("account"), m.get("sender"), m.get("subject"),
                     m.get("category"), "\n".join(why or []), now, oldest))
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT INTO disposal_refusals (message_id, run_date, account, sender, subject, "
            "category, reasons, refused_at, evidence_from) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET run_date=excluded.run_date, "
            "reasons=excluded.reasons, refused_at=excluded.refused_at, "
            "category=excluded.category, evidence_from=excluded.evidence_from", rows)
        conn.commit()
        return len(rows)
    except sqlite3.Error as e:
        print(f"  (could not record the refusal reasons: {type(e).__name__}: {e})")
        return 0


def clear_refusals(conn, moved):
    """Drop the note once a message actually moves.

    Without this a message refused on Monday and cleared on Tuesday keeps its Monday reason
    forever, and the scan would explain a disposal that has since happened - a stale
    explanation being, if anything, more misleading than none.
    """
    ids = [mid for _, mid in moved if mid]
    if not conn or not ids:
        return 0
    try:
        cur = conn.execute(
            "DELETE FROM disposal_refusals WHERE message_id IN (%s)"
            % ",".join("?" * len(ids)), ids)
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as e:
        print(f"  (could not clear stale refusal notes: {type(e).__name__}: {e})")
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

    WHY THIS LIVES HERE NOW. A session once ended after the disposal and before the separate
    hand-run journal step, leaving several messages in Trash with no journal entry at all.
    Nothing was lost - they were recoverable the whole time and the store had them recorded
    correctly as `trashed` - but the journal is the promise this lane makes about deletion,
    and for a night it was not kept.

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
