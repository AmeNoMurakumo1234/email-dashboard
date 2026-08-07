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

RULES = ROOT / "rules-and-policies.md"
START = "<!-- elicited:start -->"
END = "<!-- elicited:end -->"
HEADING = "## Rules from your own mail"
PREAMBLE = (
    "_Written by `tools/apply_answers.py` from questions you answered. Each line carries "
    "the evidence and the date behind it. Delete this whole block to remove every rule in "
    "it; edit a line by hand and re-running will not restore it._")


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
        return ["- `%s`: %s" % (who, a)]

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
        if low.startswith("no"):
            return None
        if low.startswith("mostly"):
            return ["- Treat \"%s\" as background, except anything addressed to me "
                    "directly." % what]
        return ["- Treat \"%s\" as background - never surface it." % what]

    if kind == "repeatedly_acknowledged":
        who = qid.split(":", 1)[-1]
        if low.startswith("keep surfacing"):
            return None
        if low.startswith("surface it less"):
            return ["- Surface `%s` as a collapsed series, not one row per message." % who]
        return ["- Stop surfacing `%s`; I have acknowledged it repeatedly." % who]

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

    return ["- %s" % a] if a else None


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
    return [START, HEADING, "", PREAMBLE, ""] + out + ["", END], skipped


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
        print("\n%d answer(s) recorded that correctly imply no rule here:" % len(skipped))
        for qid, ans in skipped:
            print("  %-40s %s" % (qid, (ans or "")[:50]))

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
