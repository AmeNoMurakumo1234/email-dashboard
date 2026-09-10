"""The disposer writes the deletion journal in the same call that moves the mail.

WHAT THIS IS FOR. Several messages were once found sitting in Trash with no line in
`deletion-journal.md`. Nothing was destroyed - Trash is recoverable and the store had every one
recorded correctly as `trashed` - but "everything is journaled" is the promise this lane makes
about deletion, and for a night it was not kept.

The cause was structural. `apply_proposal --apply` wrote back to the STORE and stopped;
appending the markdown line was a separate hand-run step in the routine, and an evening
session that ended right after the disposal never reached it. A paper trail that depends on a
human-shaped step following a machine-shaped one holds right up until the first time it does
not.

The sibling of test_disposal_record.py: that one proves the store learns what was done, this
one proves the journal does. The property both share - the record and the act must not be able
to come apart.

    python tools/test_disposal_journal.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
sys.path.insert(0, HERE)

import apply_proposal as ap                                        # noqa: E402


def msg(mid, subject="Promo of the day", sender="Shop <a@shop.test>",
        account="o@example.test", reason="rule 3 - retail promo"):
    return {"message_id": mid, "subject": subject, "sender": sender,
            "account": account, "reason": reason}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "deletion-journal.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# Deletion journal\n\n| Run date | Account | From | Subject | Reason |\n")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_moved_message_is_journalled(self):
        allowed = [(msg("<a@x>"), [])]
        n = ap.journal_disposals(allowed, [("o@example.test", "<a@x>")],
                                 journal=self.path, today="2026-08-08")
        self.assertEqual(n, 1)
        body = self.read()
        self.assertIn("2026-08-08", body)
        self.assertIn("Promo of the day", body)
        self.assertIn("rule 3 - retail promo", body)

    def test_refused_message_is_never_journalled(self):
        """The one that matters. A line claiming a deletion that did not happen is a lie in
        the ledger, and it is worse than a missing line - the missing one gets caught by the
        next reconciliation, the false one is believed forever."""
        allowed = [(msg("<moved@x>"), []), (msg("<refused@x>", subject="Still in the inbox"), [])]
        n = ap.journal_disposals(allowed, [("o@example.test", "<moved@x>")],
                                 journal=self.path, today="2026-08-08")
        self.assertEqual(n, 1)
        self.assertNotIn("Still in the inbox", self.read())

    def test_nothing_moved_writes_nothing(self):
        n = ap.journal_disposals([(msg("<a@x>"), [])], [], journal=self.path, today="2026-08-08")
        self.assertEqual(n, 0)
        self.assertNotIn("Promo of the day", self.read())

    def test_rerun_does_not_double_write(self):
        allowed = [(msg("<a@x>"), [])]
        moved = [("o@example.test", "<a@x>")]
        ap.journal_disposals(allowed, moved, journal=self.path, today="2026-08-08")
        again = ap.journal_disposals(allowed, moved, journal=self.path, today="2026-08-08")
        self.assertEqual(again, 0)
        self.assertEqual(self.read().count("Promo of the day"), 1)

    def test_pipes_and_newlines_cannot_break_the_table(self):
        """A subject is whatever a sender typed. An unescaped pipe silently splits the row
        into extra columns, so the table stops parsing as a table."""
        allowed = [(msg("<a@x>", subject="Deal | 50% off\nsecond line"), [])]
        ap.journal_disposals(allowed, [("o@example.test", "<a@x>")],
                             journal=self.path, today="2026-08-08")
        row = [ln for ln in self.read().splitlines() if "50% off" in ln][0]
        self.assertEqual(row.count("|"), 6)          # 5 cells -> 6 delimiters
        self.assertNotIn("Deal | 50%", row)

    def test_missing_journal_file_is_created_not_fatal(self):
        gone = os.path.join(tempfile.mkdtemp(), "nope.md")
        n = ap.journal_disposals([(msg("<a@x>"), [])], [("o@example.test", "<a@x>")],
                                 journal=gone, today="2026-08-08")
        self.assertEqual(n, 1)
        self.assertTrue(os.path.exists(gone))


if __name__ == "__main__":
    unittest.main(verbosity=2)
