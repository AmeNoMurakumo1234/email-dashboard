"""The write-back, tested where it can do damage: someone else's file.

This program edits a file a person wrote by hand. The failure that matters is not "the rule
came out wrong" - it is "everything around the rule came out wrong", silently, in a file
nobody diffs. So most of these tests are about what must NOT change.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "dashboard"))

import apply_answers as aa                                         # noqa: E402
import db                                                          # noqa: E402

PROSE = ("# My rules\n"
         "\n"
         "1. Never bin a message from a person.\n"
         "2. Some hand-written thing with trailing spaces   \n"
         "\n"
         "## A section I care about\n"
         "Text.\n")


def conn_with(answers):
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn = db.connect(path)
    for a in answers:
        conn.execute("INSERT INTO answers (question_id, kind, question, evidence, answer, "
                     "answered_at) VALUES (?,?,?,?,?,?)", a)
    conn.commit()
    return conn


def rules_file(text=PROSE, nl="\n"):
    path = os.path.join(tempfile.mkdtemp(), "rules-and-policies.md")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", nl))
    return path


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


ANSWER = ("never-actioned:digest", "sender_disposition", "How should it be handled?",
          '{"messages": 9, "kept": 0}', "auto-trash it from now on", "2026-08-06")


class BlockContents(unittest.TestCase):

    def test_an_answer_becomes_a_rule_with_its_evidence(self):
        block, skipped = aa.build_block(conn_with([ANSWER]))
        text = "\n".join(block)
        self.assertIn("Auto-trash mail from `digest`", text)
        self.assertIn("9 messages", text)           # the evidence travels with the rule
        self.assertIn("2026-08-06", text)
        self.assertEqual(skipped, [])

    def test_an_answer_that_correctly_implies_nothing_is_reported_not_dropped(self):
        row = ANSWER[:4] + ("it matters sometimes - keep asking me", "2026-08-06")
        block, skipped = aa.build_block(conn_with([row]))
        self.assertEqual(block, [])
        self.assertEqual(len(skipped), 1, "an answer that writes nothing must still be "
                                          "accounted for, not silently absent")

    def test_the_dangerous_answer_writes_the_protective_rule(self):
        row = ("personal-ask:tracker", "personally_addressed", "q?", "{}",
               "yes - surface anything addressed to me or asking me to act", "2026-08-06")
        text = "\n".join(aa.build_block(conn_with([row]))[0])
        self.assertIn("Never bin mail from `tracker` that is addressed to me", text)

    def test_escalation_and_concept_answers_do_not_become_prose(self):
        """Both belong in structured files; guessing them into prose puts a wrong label
        or a fake guard entry somewhere the loader will never read."""
        rows = [("escalation-contacts", "escalation_contacts", "q?", "{}",
                 "my sister, my bank", "2026-08-06"),
                ("concept-gap", "concept_gap", "q?", "{}", "build-failure means ops",
                 "2026-08-06")]
        block, skipped = aa.build_block(conn_with(rows))
        self.assertEqual(block, [])
        self.assertEqual(len(skipped), 2)


class TheGuardBindsTheATTENTIONPathToo(unittest.TestCase):
    """`protected_names` stopped the applier BINNING a sender's mail. It never stopped an
    elicited rule making that sender UNSURFACED - the quieter half of the same outcome, since
    the mail stays in the inbox and the tool simply stops mentioning it.

    That gap aimed itself at the worst possible targets. Acknowledging correlates with mail a
    person actually processes, so the senders with the highest ack counts are colleagues, the
    accountant, the IT lead - never newsletters, which get binned rather than acknowledged. On
    a field install four of the five senders offered for silencing were on the protected list.
    """

    def setUp(self):
        self._real = aa._is_protected

    def tearDown(self):
        aa._is_protected = self._real

    def _row(self, answer):
        return ("engaged-sender:payroll", "engaged_sender",
                "you deal with this sender often", "{}", answer)

    def test_a_protected_sender_cannot_be_unsurfaced(self):
        aa._is_protected = lambda who: True
        lines = aa._lines_for(self._row("actually I want to see less of it"))
        self.assertTrue(lines)
        self.assertIn("REFUSED", lines[0])
        self.assertIn("protected list", lines[0])

    def test_the_refusal_says_how_to_proceed_rather_than_just_saying_no(self):
        aa._is_protected = lambda who: True
        lines = aa._lines_for(self._row("stop surfacing it"))
        self.assertIn("Remove them from the guard first", lines[0])

    def test_an_unprotected_sender_is_unaffected(self):
        aa._is_protected = lambda who: False
        lines = aa._lines_for(self._row("actually I want to see less of it"))
        self.assertTrue(lines)
        self.assertNotIn("REFUSED", lines[0])

    def test_ranking_UP_is_what_a_yes_now_means(self):
        aa._is_protected = lambda who: False
        lines = aa._lines_for(self._row("yes - rank it higher"))
        self.assertIn("Rank `payroll` higher", lines[0])

    # ---- THE EMPTY CELL: protected sender, NON-suppressing answer ----------------------
    #
    # The first four cases covered protected+suppressing and unprotected+either, so the matrix
    # had a hole and the suite stayed green while the guard fired on every branch. That is the
    # shape worth remembering: a test class can be correctly named, correctly reasoned and
    # still miss the defect, if every case varies both axes together.

    def test_a_protected_sender_can_still_be_ranked_UP(self):
        """The guard exists so these people are not missed. Ranking one higher is that goal
        expressed through this panel, so refusing it inverts the guard."""
        aa._is_protected = lambda who: True
        lines = aa._lines_for(self._row("yes - rank it higher"))
        self.assertIn("Rank `payroll` higher", lines[0])
        self.assertNotIn("REFUSED", lines[0])

    def test_a_no_op_answer_on_a_protected_sender_writes_NOTHING(self):
        """"No - leave it as it is" changes nothing, so it must not put a `(REFUSED)` line in
        the record of what the owner decided. A refusal for a decision nobody was refused is a
        third thing that looks like both halves of the pair 0.26.0 separated: your answer
        changed nothing, and your answer was lost."""
        aa._is_protected = lambda who: True
        self.assertIsNone(aa._lines_for(self._row("no - leave it as it is")))
        self.assertIsNone(aa._lines_for(self._row("keep surfacing - I want to see each one")))

    def test_the_do_not_guess_fallthrough_is_REACHABLE_for_a_protected_sender(self):
        """The most careful line in the function was dead code for exactly the people it was
        written to protect, because the guard returned before an ambiguous answer could reach
        it."""
        aa._is_protected = lambda who: True
        self.assertIsNone(aa._lines_for(self._row("hmm, not sure really")))

    def test_collapsing_a_protected_sender_is_allowed_ON_PURPOSE(self):
        """A collapsed series is still surfaced, so it silences nobody. Documented as a
        decision rather than left as a side effect of where the guard sits."""
        aa._is_protected = lambda who: True
        lines = aa._lines_for(self._row("surface it less often"))
        self.assertIn("collapsed series", lines[0])
        self.assertNotIn("REFUSED", lines[0])


class TheRestOfTheFileSurvives(unittest.TestCase):

    def setUp(self):
        self.conn = conn_with([ANSWER])
        # The store path, threaded through every call below. Without --db these tests would
        # read the live dashboard database - and the ones that assert "the file came back
        # byte-identical" would have been stamping written_to on the owner's real answers
        # to prove it.
        self.db = self.conn.execute("PRAGMA database_list").fetchone()[2]

    def test_prose_is_untouched(self):
        path = rules_file()
        before = read(path)
        aa.main(["--write", "--rules", path, "--db", self.db])
        after = read(path)
        self.assertIn(before.rstrip("\n"), after,
                      "everything the person wrote must still be there, in order")
        self.assertIn("trailing spaces   ", after)

    def test_crlf_files_stay_crlf(self):
        path = rules_file(nl="\r\n")
        aa.main(["--write", "--rules", path, "--db", self.db])
        after = read(path)
        # Every LF must be part of a CRLF. A single lone LF makes the next diff of this
        # file show every line as changed, for a one-line addition.
        self.assertEqual(after.count("\n"), after.count("\r\n"),
                         "a lone LF in a CRLF file rewrites every line in the next diff")

    def test_writing_twice_updates_rather_than_duplicates(self):
        path = rules_file()
        aa.main(["--write", "--rules", path, "--db", self.db])
        once = read(path)
        aa.main(["--write", "--rules", path, "--db", self.db])
        self.assertEqual(read(path), once)
        self.assertEqual(read(path).count(aa.START), 1)

    def test_revert_removes_everything_it_added(self):
        path = rules_file()
        before = read(path)
        aa.main(["--write", "--rules", path, "--db", self.db])
        self.assertNotEqual(read(path), before)          # control: it did write
        aa.main(["--revert", "--write", "--rules", path, "--db", self.db])
        self.assertEqual(read(path), before,
                         "a tool that writes into someone's own file must be removable "
                         "in one gesture, with no residue")

    def test_write_revert_cycles_do_not_grow_the_file(self):
        path = rules_file()
        for _ in range(3):
            aa.main(["--write", "--rules", path, "--db", self.db])
            aa.main(["--revert", "--write", "--rules", path, "--db", self.db])
        self.assertEqual(read(path), PROSE)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        path = rules_file()
        before = read(path)
        rc = aa.main(["--rules", path, "--db", self.db])
        self.assertEqual(rc, 0)
        self.assertEqual(read(path), before,
                         "the default must never touch the file - this is the whole "
                         "propose/dispose split")


if __name__ == "__main__":
    unittest.main(verbosity=2)
