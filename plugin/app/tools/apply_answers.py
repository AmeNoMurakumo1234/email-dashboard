"""Turn recorded answers into rules the routine actually reads. Propose first, write second.

WHY THIS IS A SEPARATE PROGRAM. Collecting answers and acting on them are different risks.
Recording is safe - a row in a table. Writing is not: a rule the owner did not quite mean
becomes an instruction that silently shapes every future run, and the reader who inherits it
cannot tell it from a rule someone thought hard about. So this follows the same split
apply_proposal.py uses for mail: something proposes, a person looks, and only then does a
non-LLM program write.

Default is DRY. `--write` is the only thing that touches the file.

EVERYTHING IT WRITES IS INSIDE ONE MARKED BLOCK.

    <!-- elicited:start --> ... <!-- elicited:end -->

Prose above and below it is never touched, re-running updates the block rather than
appending a second copy, and deleting the block by hand removes every elicited rule at once
with no residue. A tool that writes into a person's own file has to be removable in one
gesture, or people are right not to let it write at all.

Each line records the evidence and the date it came from, so a year later the file still
distinguishes a rule its owner chose from a rule someone guessed:

    - Auto-trash mail from Example Digest.
      <!-- elicited:never-actioned:example <date> evidence: N messages, 0 kept, 0 flagged -->

Usage:
    python tools/apply_answers.py                 # show what WOULD be written
    python tools/apply_answers.py --write         # write it
    python tools/apply_answers.py --revert        # remove the whole elicited block
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import db                                                          # noqa: E402

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "dashboard"))
from consoleio import safe_console            # noqa: E402
safe_console()


RULES = ROOT / "rules-and-policies.md"
START = "<!-- elicited:start -->"
END = "<!-- elicited:end -->"
HEADING = "## Rules from your own mail"
# A LIST OF LINES, not one string with newlines in it. The block is joined with the file's
# OWN line ending, so an embedded "\n" in any single entry writes a lone LF into a CRLF file
# and rewrites every line in the next diff. test_crlf_files_stay_crlf caught exactly that.
PREAMBLE = [
    "_Written by `tools/apply_answers.py` from questions you answered. Each line carries "
    "the evidence and the date behind it. Delete this whole block to remove every rule in "
    "it; edit a line by hand and re-running will not restore it._",
    "",
    "**How these resolve against each other and against the numbered rules above.**",
    "People contradict themselves, answer two questions at different altitudes in one",
    "sitting, and cannot contradict a rule they were never shown. So rules are ordered as a",
    "**Pareto frontier** (`dashboard/ruleset.py`) on three axes, not by a priority list:",
    "",
    "- **specificity** - global < concept < **sender**",
    "- **time** - older < **newer**",
    "- **provenance** - inferred by the agent < **ruled by you**",
    "",
    "One rule supersedes another only when they are about the **same mail** and it is at",
    "least as strong on every axis. Rules in different lanes never compete, so a ruling about",
    "one category cannot quietly reach into another. Treating a category as background does",
    "**not** silence a specific sender inside it - that is a sender-level rule and it wins.",
    "",
    "Two consequences worth knowing. A rule you made long ago against one the agent inferred",
    "yesterday **does not resolve** - you outrank it, it outranks you on recency - so it is",
    "reported for you to settle rather than decided by whichever was read last. And nothing",
    "here is ever deleted: a superseded rule keeps its place with a pointer to what replaced",
    "it, so **removing the newer rule brings the older ruling back into force by itself**._",
]


def _is_protected(who):
    """Is this sender key on the guard list? Errs toward YES.

    Deliberately generous and deliberately local. A false positive costs one refused rule with
    a message saying exactly how to proceed; a false negative silences somebody the owner said
    must never be missed. Those are not the same mistake, so the matching leans the cheap way.

    Never raises: an unreadable guard is treated as protecting nothing here, because the caller
    already refuses to do anything at all when the guard is unconfigured.
    """
    try:
        sys.path.insert(0, str(ROOT / "dashboard"))
        from server import load_protected                             # noqa: PLC0415
        names = load_protected().get("names") or []
    except Exception:                                                 # noqa: BLE001
        return False
    low = (who or "").strip().lower()
    if not low:
        return False
    return any(n and (n.lower() in low or low in n.lower()) for n in names)


def _lines_for(row):
    """The rule text an answer implies, or None if the answer implies no rule.

    DELIBERATELY CONSERVATIVE. Several answers are worth recording and imply nothing this
    program should write - "it matters sometimes, keep asking me" is a real answer whose
    correct effect on the rules file is nothing at all. Inventing a rule for it would be
    the failure this whole release exists to fix, one layer further in.
    """
    qid, kind, question, evidence, answer = row
    a = (answer or "").strip()
    low = a.lower()

    # WANTS TO STILL SEE SOME OF IT, said in whatever words came to hand.
    #
    # Every branch below used to classify by `startswith` against the dashboard's canned
    # options, which works right up until somebody types their own sentence - and the whole
    # point of a free-text box is that they will. Measured live: "mostly, but surface anything
    # addressed to me" matched and became an exception rule, while "surface anything addressed
    # to me and continue scanning for steam sales" - the same instruction, different opening
    # word - matched nothing and fell through to the default, which was
    # "never surface it". The exact OPPOSITE of what was asked for.
    #
    # In a tool whose entire job is deciding what a person sees, silently inverting "show me
    # this" is the worst direction available. So intent is read from the whole sentence.
    wants_exception = (("address" in low or "addressed to me" in low or "asking me" in low
                        or "asks me" in low or "aimed at me" in low)
                       and ("surface" in low or "keep" in low or "show" in low
                            or "except" in low or "but" in low))
    try:
        ev = json.loads(evidence or "{}")
    except ValueError:
        ev = {}

    if kind == "sender_disposition":
        who = qid.split(":", 1)[-1]
        if low.startswith("auto-trash"):
            return ["- Auto-trash mail from `%s`." % who]
        if low.startswith("bin it"):
            return ["- Bin mail from `%s`, but keep it searchable - do not surface it."
                    % who]
        if low.startswith("leave it") or low.startswith("it matters"):
            return None                     # a real answer that correctly writes nothing
        # Prose that matched no option used to be pasted in verbatim as though it were a rule.
        # A sentence like "chances are high that this is a scam" is a genuine and useful
        # answer, and it is not an instruction the routine can follow - so it is recorded and
        # reported, never dressed up as policy.
        return None

    if kind == "personally_addressed":
        who = qid.split(":", 1)[-1]
        if low.startswith("no "):
            return ["- Treat everything from `%s` as noise, including mail addressed to me "
                    "directly - I have confirmed this." % who]
        if "never auto-trash" in low:
            return ["- Never auto-trash `%s` at all." % who]
        return ["- Never bin mail from `%s` that is addressed to me directly or asks me to "
                "act, even when the rest of that sender's mail is binned." % who]

    if kind == "concept_never_actioned":
        what = qid.split(":", 1)[-1]
        if wants_exception or low.startswith("mostly"):
            return ["- Treat \"%s\" as background, except anything addressed to me "
                    "directly." % what]
        if low.startswith("no"):
            return None
        if low.startswith("yes") or "never surface" in low or "background" in low:
            return ["- Treat \"%s\" as background - never surface it." % what]
        # Free text that matches no known shape. Writing nothing and SAYING SO beats guessing:
        # the guess here would be "never surface it", and a wrong guess in that direction hides
        # mail the person asked to see.
        return None

    if kind in ("repeatedly_acknowledged", "engaged_sender"):
        who = qid.split(":", 1)[-1]
        # THE GUARD IS CONSULTED ON THE ATTENTION PATH TOO, not only on the disposal path.
        # `protected_names` stops the applier BINNING someone's mail; it never stopped an
        # elicited rule making that sender UNSURFACED, which is the quieter half of the same
        # outcome. On a field install four of the five senders this question was generated for
        # were on the protected list.
        #
        # GUARD THE ACTION, NOT THE SENDER. The check sat here at the TOP and therefore fired
        # on every branch, which inverted a correct guard three ways: it answered a request to
        # make somebody MORE visible with "no rule may stop it being surfaced", which is false;
        # it wrote a `(REFUSED)` line into the rules file for "no - leave it as it is", an
        # answer that changes nothing, putting a refusal in the record of decisions for a
        # decision nobody was refused; and it blocked the one action that actually serves the
        # protected list, since ranking the accountant higher is the guard's own goal expressed
        # through this panel.
        #
        # It also made the fallthrough below unreachable for protected senders - so the most
        # careful line in the function, the one that refuses to guess toward hiding people, was
        # dead code for exactly the people it was written to protect.
        if low.startswith("keep surfacing") or low.startswith("no -"):
            return None
        if low.startswith("yes - rank") or "rank it higher" in low:
            return ["- Rank `%s` higher; I deal with their mail regularly." % who]
        # DELIBERATELY UNGUARDED, as a decision rather than an accident: a collapsed series is
        # still surfaced, so it silences nobody, and asking for a chatty protected sender to
        # take one row instead of nine is a reasonable thing to want. Only outright suppression
        # is refused below.
        if low.startswith("surface it less"):
            return ["- Surface `%s` as a collapsed series, not one row per message." % who]
        if low.startswith("stop surfacing") or "see less of it" in low or "stop" in low:
            if _is_protected(who):
                return ["- (REFUSED) `%s` is on your protected list, so no rule may stop it "
                        "being surfaced. Remove them from the guard first if you really mean "
                        "it." % who]
            return ["- Surface `%s` less prominently; I have said I want less of it." % who]
        return None                         # unclear: do not guess toward hiding things

    if kind == "mailbox_role":
        return ["- Mailbox roles: %s" % a]

    if kind == "concept_gap":
        # Deliberately NOT written as a rule. This answer belongs in the concept map, which
        # is a JSON file with a schema, and guessing at that mapping from free text here
        # would put a wrong label on every future message in the category.
        return None

    if kind == "escalation_contacts":
        # Belongs in the guard, not in prose. Written through the protected-names endpoint
        # so the loader's opinion of what counts as configured stays the only one.
        return None

    # An answer of a kind this program does not know how to translate. Recorded in the store,
    # reported to the operator, and deliberately NOT written as a rule - pasting free prose
    # under a heading called "Rules" makes it look like policy the routine follows, when
    # nothing reads it.
    return None


def _evidence_note(qid, evidence, when):
    try:
        ev = json.loads(evidence or "{}")
    except ValueError:
        ev = {}
    bits = ", ".join("%s %s" % (v, k.replace("_", " "))
                     for k, v in ev.items()
                     if isinstance(v, int) and k != "weight")
    return "  <!-- elicited:%s %s%s -->" % (qid, when, (" evidence: " + bits) if bits else "")


def build_block(conn):
    """The whole managed block, rebuilt from the answers table. Returns (lines, skipped)."""
    rows = conn.execute(
        "SELECT question_id, kind, question, evidence, answer, answered_at FROM answers "
        "WHERE answer IS NOT NULL AND TRIM(answer) != '' ORDER BY kind, question_id"
    ).fetchall()
    out, skipped = [], []
    for r in rows:
        lines = _lines_for(tuple(r)[:5])
        if not lines:
            skipped.append((r[0], r[4]))
            continue
        out.extend(lines)
        out.append(_evidence_note(r[0], r[3], (r[5] or "")[:10]))
    if not out:
        return [], skipped
    return [START, HEADING, ""] + PREAMBLE + [""] + out + ["", END], skipped


def splice(raw, block, nl):
    """Replace the managed block, or append it. Everything else survives byte-for-byte."""
    lines = raw.split(nl)

    def _tidy(head, mid, tail):
        """Exactly one blank line at each seam, and exactly one newline at the end.

        One helper for all three paths - first write, rewrite, revert - because they have
        to agree, and they did not. Writing twice added a blank line each time and
        write-then-revert left the file one line longer than it started: invisible in a
        rendered Markdown preview, permanent in git, and growing every cycle. "Removable
        with no residue" has to mean the bytes.
        """
        head, tail = list(head), list(tail)
        while head and head[-1] == "":
            head.pop()
        while tail and tail[0] == "":
            tail.pop(0)
        while tail and tail[-1] == "":
            tail.pop()
        out = head
        for part in (mid, tail):
            if part:
                if out:
                    out.append("")
                out.extend(part)
        return out + [""]

    try:
        a = lines.index(START)
        b = lines.index(END)
    except ValueError:
        if not block:
            return raw
        return nl.join(_tidy(lines, block, []))
    return nl.join(_tidy(lines[:a], block, lines[b + 1:]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true",
                    help="actually write; without it this only shows the proposal")
    ap.add_argument("--revert", action="store_true",
                    help="remove the elicited block entirely, leaving the rest untouched")
    ap.add_argument("--rules", default=str(RULES))
    # WHICH store. Defaulting to the real one is right for a person at a prompt and wrong
    # for everything else: without this the tests that prove this program does not damage a
    # file would have been reading and stamping the live database to do it.
    ap.add_argument("--db", default=None, help="store to read answers from")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    try:
        block, skipped = build_block(conn)
    finally:
        conn.close()
    if args.revert:
        block = []

    try:
        with open(args.rules, encoding="utf-8", newline="") as f:
            raw = f.read()
    except FileNotFoundError:
        print("ERROR: no rules file at %s" % args.rules, file=sys.stderr)
        return 2
    nl = "\r\n" if "\r\n" in raw else "\n"
    new = splice(raw, block, nl)

    if new == raw:
        print("nothing to change (%d answer(s) recorded, %d imply no rule)"
              % (len(block and [x for x in block if x.startswith("- ")]) or 0, len(skipped)))
        return 0

    print("--- would write into %s ---" % args.rules)
    for line in block:
        print(line)
    if skipped:
        # Named, not silently dropped. "12 answers, 4 rules" with no explanation is the
        # understatement this project keeps finding; an answer that correctly writes
        # nothing should say so rather than look like an answer that went missing.
        print("\n%d answer(s) recorded that this program did NOT turn into a rule:"
              % len(skipped))
        print("  Some of these correctly imply nothing. Others are free text it could not")
        print("  translate - READ THEM. An answer that produced no rule and no mention is")
        print("  indistinguishable from one that was never given.")
        for qid, ans in skipped:
            print("  %-40s %s" % (qid, (ans or "")[:80]))

    if not args.write:
        print("\nDRY RUN - nothing written. Re-run with --write to apply.")
        return 0

    with open(args.rules, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    conn = db.connect(args.db)
    try:
        conn.execute("UPDATE answers SET written_to = ? WHERE answer IS NOT NULL "
                     "AND TRIM(answer) != ''", (os.path.basename(args.rules),))
        conn.commit()
    finally:
        conn.close()
    print("\nWROTE %s" % args.rules)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
