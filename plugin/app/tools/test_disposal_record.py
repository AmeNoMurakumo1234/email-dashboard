"""The disposer records what it disposed. The other half of propose/dispose.

`apply_proposal --apply` moved mail and never wrote back, so the store said `would_trash` -
"judged disposable, NOT acted on" - about messages it had in fact just moved to Trash. The
mirror image of the defect that created `would_trash` in the first place: there the store
overstated a judgment nobody had made, here it understates an action it did take.

Found by running a real sweep, applying six messages, and then reading the store: six rows in
Trash, nine rows still saying would_trash, and a run row reporting `trashed 0`.

It matters beyond tidiness. Both values are DISPOSABLE, so the guard is not misled - but "did
the routine actually bin this?" had no answer anywhere in the record, and a day's own numbers
disagreed with the list underneath them. Distinguishing what was DECIDED from what was DONE is
the entire reason this vocabulary exists.

    python tools/test_disposal_record.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import apply_proposal as ap                                        # noqa: E402


class Store:
    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(self.path))

    def add(self, mid, disposition="would_trash", day="2026-08-08"):
        c = db.connect(self.path)
        c.execute("INSERT OR IGNORE INTO runs (run_date, created_at, trashed, kept) "
                  "VALUES (?, 'x', 0, 0)", (day,))
        rid = c.execute("SELECT id FROM runs WHERE run_date = ?", (day,)).fetchone()[0]
        c.execute("INSERT INTO messages (run_id, run_date, account, sender, subject, "
                  "disposition, category, message_id) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, day, "o@example.test", "s@example.test", "subj " + mid,
                   disposition, "promo", mid))
        c.commit()

    def dispositions(self):
        return {r["message_id"]: r["disposition"] for r in
                db.connect(self.path).execute("SELECT message_id, disposition FROM messages")}

    def run_row(self, day="2026-08-08"):
        return dict(db.connect(self.path).execute(
            "SELECT trashed, kept FROM runs WHERE run_date = ?", (day,)).fetchone())


class WhatMovedIsRecordedAsMoved(unittest.TestCase):

    def setUp(self):
        self.s = Store()
        for mid in ("<a@x>", "<b@x>", "<c@x>"):
            self.s.add(mid)

    def test_moved_rows_become_trashed(self):
        n = ap.record_disposals(db.connect(self.s.path), [("", "<a@x>"), ("", "<b@x>")])
        self.assertEqual(n, 2)
        d = self.s.dispositions()
        self.assertEqual(d["<a@x>"], "trashed")
        self.assertEqual(d["<b@x>"], "trashed")

    def test_rows_that_did_NOT_move_are_left_alone(self):
        """The control, and the important one. Promoting everything would make the record
        claim the guard's refusals were acted on - the same false statement in the other
        direction, and about the messages the guard deliberately protected."""
        ap.record_disposals(db.connect(self.s.path), [("", "<a@x>")])
        self.assertEqual(self.s.dispositions()["<c@x>"], "would_trash")

    def test_the_run_row_moves_with_them(self):
        """Otherwise the day's own numbers disagree with the list underneath them."""
        ap.record_disposals(db.connect(self.s.path), [("", "<a@x>"), ("", "<b@x>")])
        self.assertEqual(self.s.run_row()["trashed"], 2)

    def test_a_row_already_trashed_is_not_double_counted(self):
        """Re-running an apply must not inflate the day's trashed count."""
        conn = db.connect(self.s.path)
        ap.record_disposals(conn, [("", "<a@x>")])
        ap.record_disposals(db.connect(self.s.path), [("", "<a@x>")])
        self.assertEqual(self.s.run_row()["trashed"], 1)

    def test_a_hand_retriaged_row_is_never_overwritten(self):
        """Only rows still sitting at would_trash. If somebody has since decided to keep a
        message, an apply from an older proposal must not quietly bin it in the record."""
        self.s.add("<kept@x>", disposition="kept")
        ap.record_disposals(db.connect(self.s.path), [("", "<kept@x>")])
        self.assertEqual(self.s.dispositions()["<kept@x>"], "kept")

    def test_nothing_moved_changes_nothing(self):
        before = self.s.dispositions()
        self.assertEqual(ap.record_disposals(db.connect(self.s.path), []), 0)
        self.assertEqual(self.s.dispositions(), before)

    def test_an_unknown_message_id_is_harmless(self):
        self.assertEqual(
            ap.record_disposals(db.connect(self.s.path), [("", "<never-seen@x>")]), 0)

    def test_a_broken_connection_does_not_make_a_successful_move_look_failed(self):
        """The mail HAS moved by the time this runs. A bookkeeping failure must never be
        reported as a disposal failure - that would send someone hunting for mail that is
        exactly where it should be."""
        class Dead:
            def execute(self, *a, **k):
                import sqlite3                                      # noqa: PLC0415
                raise sqlite3.Error("disk gone")
        self.assertEqual(ap.record_disposals(Dead(), [("", "<a@x>")]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
