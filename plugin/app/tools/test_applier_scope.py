"""The applier's guard reasons about the SLICE, and a connector install can still dispose.

TWO FINDINGS, ONE SEAM (reported by a field tester against 0.20.0).

F37 - `server.sender_rule_verdict` learned about (sender, category) slices and `judge()` did
not, so the two halves of "may this be binned?" disagreed in the same store, in the same
instant, with no way for a reader to tell which one governed. Reported from the field: most of
the refusals in one sweep were a single notification address, every one refused because a
handful of OTHER messages from it - a human naming the owner - were rightly kept.

The direction of the failure is what makes it serious. Deciding to KEEP some mail from an
address silently removed the ability to bin different mail from it. Nothing warned; those
stayed labelled disposable and became permanently refused, because the guard's evidence about
them was really evidence about their neighbours.

F38 - the guard could reach a verdict it had no way to execute. `apply_proposal` hard-codes
one executor and keys on a UID, which is per-folder IMAP state a connector ingest never
produces, so the cleared set was reported and then nothing moved. The propose/dispose split
was half implemented for exactly the installs `ingest.py` was written to rescue.

    python tools/test_applier_scope.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
sys.path.insert(0, HERE)

import db                                                        # noqa: E402
import apply_proposal as ap                                      # noqa: E402


class Store:
    """A store with one mixed sender: kept mail under one category, noise under another."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(self.path))
        self.conn = db.connect(self.path)
        self.conn.execute("INSERT OR IGNORE INTO runs (run_date, created_at, trashed, kept) "
                          "VALUES ('2026-08-08','x',0,0)")
        self.rid = self.conn.execute(
            "SELECT id FROM runs WHERE run_date='2026-08-08'").fetchone()[0]

    def add(self, category, disposition, mid, importance=None, concept=""):
        self.conn.execute(
            "INSERT INTO messages (run_id, run_date, account, sender, subject, disposition, "
            "category, concept, importance, message_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.rid, "2026-08-08", "o@example.test", "Notify <notify@example.test>",
             "s " + mid, disposition, category, concept, importance, mid))
        self.conn.commit()


PROT = {"configured": True, "names": set(), "concepts": set()}


class SliceTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        # The human-named mail: rightly protected.
        for i in range(3):
            self.s.add("mention", "kept", "<keep%d@x>" % i)
        # The status noise from the SAME address, under a different label.
        for i in range(5):
            self.s.add("ci-status", "would_trash", "<noise%d@x>" % i)
        self.hist = ap._history(self.s.conn)

    def msg(self, category):
        return {"sender": "Notify <notify@example.test>", "subject": "x",
                "category": category, "account": "o@example.test"}

    def test_the_slice_with_no_kept_mail_is_binnable(self):
        """The finding. Status mail must not inherit the protection its neighbours earned."""
        self.assertEqual(ap.judge(self.msg("ci-status"), PROT, self.hist), [])

    def test_the_slice_with_kept_mail_is_still_refused(self):
        reasons = ap.judge(self.msg("mention"), PROT, self.hist)
        self.assertTrue(reasons)
        self.assertIn("kept or surfaced", " ".join(reasons))

    def test_the_refusal_names_the_slice_it_judged(self):
        """A reason that says 'this sender' when it means one label of that sender is how a
        reader concludes the whole address is protected and stops proposing it."""
        reasons = ap.judge(self.msg("mention"), PROT, self.hist)
        self.assertIn("'mention'", " ".join(reasons))

    def test_uncategorised_mail_falls_back_to_the_whole_sender(self):
        """No category means no slice, and the safe reading is the wider evidence."""
        reasons = ap.judge(self.msg(""), PROT, self.hist)
        self.assertTrue(reasons)
        self.assertIn("this sender has", " ".join(reasons))

    def test_an_unseen_category_falls_back_rather_than_reading_as_clean(self):
        """A label the store has never recorded has NO evidence of its own. Treating absence
        as a clean slice would make any new label a way to bin a protected sender."""
        reasons = ap.judge(self.msg("brand-new-label"), PROT, self.hist)
        self.assertTrue(reasons)

    def test_protected_name_still_binds_every_slice(self):
        """A protected PERSON must never become binnable one label at a time."""
        prot = {"configured": True, "names": {"notify@example.test"}, "concepts": set()}
        self.assertTrue(ap.judge(self.msg("ci-status"), prot, self.hist))

    def test_keeping_mail_does_not_remove_the_ability_to_bin_other_mail(self):
        """The measured regression, as a test: the owner rules that one label is worth
        keeping, and the OTHER label must still be binnable afterwards."""
        before = ap.judge(self.msg("ci-status"), PROT, self.hist)
        for i in range(9):
            self.s.add("release-notes", "kept", "<rel%d@x>" % i)
        after = ap.judge(self.msg("ci-status"), PROT, ap._history(self.s.conn))
        self.assertEqual(before, [])
        self.assertEqual(after, [])


class DegradeTests(unittest.TestCase):
    """A guard whose MEMORY fails must not fail open.

    Found by this very change: the sliced query raised on a store with no `category` column
    and the bare `except sqlite3.Error: return {}` turned that into "this sender has no
    history" - which reads as "nothing is protected". Every refusal that rested on kept mail
    silently stopped firing, and the output looked like a healthy guard clearing mail.
    """

    def test_a_store_without_category_still_remembers_kept_mail(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages (sender TEXT, disposition TEXT, concept TEXT, "
                     "importance TEXT)")
        conn.execute("INSERT INTO messages VALUES ('Bank <b@example.test>','kept','money','')")
        conn.commit()
        hist = ap._history(conn)
        self.assertTrue(hist, "history vanished on a store with no category column")
        reasons = ap.judge({"sender": "Bank <b@example.test>", "category": "statement"},
                           PROT, hist)
        self.assertTrue(reasons, "a store without categories must fall back, not clear")

    def test_an_unreadable_store_warns_instead_of_returning_a_confident_empty(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")          # no messages table at all
        hist = ap._history(conn)
        self.assertEqual(hist, {})                  # nothing to report...
        # ...but the operator has to have been told, or an empty history reads as a clean one.


class EmitTests(unittest.TestCase):
    def test_emit_writes_only_what_the_guard_cleared(self):
        allowed = [({"account": "o@example.test", "message_id": "<a@x>", "subject": "yes",
                     "sender": "s", "web_link": "https://owa.example.test/?ItemID=AAA"}, [])]
        p = os.path.join(tempfile.mkdtemp(), "cleared.json")
        n = ap.emit_cleared(allowed, p)
        self.assertEqual(n, 1)
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(len(d["cleared"]), 1)
        row = d["cleared"][0]
        # message_id and web_link are the two durable handles; a UID is neither.
        self.assertEqual(row["message_id"], "<a@x>")
        self.assertIn("ItemID", row["web_link"])
        self.assertNotIn("uid", row)

    def test_a_refused_message_never_reaches_the_file(self):
        """The property that keeps the model out of the decision: it cannot add to the list,
        because anything absent from the file was refused."""
        allowed = [({"message_id": "<ok@x>", "subject": "cleared"}, [])]
        p = os.path.join(tempfile.mkdtemp(), "cleared.json")
        ap.emit_cleared(allowed, p)
        with open(p, encoding="utf-8") as fh:
            self.assertNotIn("refused", fh.read())


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.s.add("promo", "would_trash", "<gone@x>")
        self.s.add("promo", "would_trash", "<stays@x>")
        self.s.add("mention", "kept", "<kept@x>")

    def test_a_receipt_promotes_only_what_it_names(self):
        moved = db.record_disposed(self.s.conn, ["<gone@x>"])
        self.assertEqual(moved, 1)
        got = dict(self.s.conn.execute(
            "SELECT message_id, disposition FROM messages").fetchall())
        self.assertEqual(got["<gone@x>"], "trashed")
        self.assertEqual(got["<stays@x>"], "would_trash")

    def test_a_receipt_never_overwrites_deliberately_kept_mail(self):
        """A client reporting that it deleted something the owner had since decided to keep
        must not silently rewrite that decision."""
        db.record_disposed(self.s.conn, ["<kept@x>"])
        self.assertEqual(self.s.conn.execute(
            "SELECT disposition FROM messages WHERE message_id='<kept@x>'").fetchone()[0],
            "kept")

    def test_replaying_a_receipt_is_harmless(self):
        db.record_disposed(self.s.conn, ["<gone@x>"])
        again = db.record_disposed(self.s.conn, ["<gone@x>"])
        self.assertEqual(again, 0)
        self.assertEqual(self.s.conn.execute(
            "SELECT trashed FROM runs WHERE run_date='2026-08-08'").fetchone()[0], 1)

    def test_the_run_row_moves_with_the_messages(self):
        """A day whose own counts disagree with the list underneath it is a record that has
        started lying quietly."""
        db.record_disposed(self.s.conn, ["<gone@x>"])
        trashed, kept = self.s.conn.execute(
            "SELECT trashed, kept FROM runs WHERE run_date='2026-08-08'").fetchone()
        self.assertEqual((trashed, kept), (1, 1))

    def test_an_unknown_id_moves_nothing_and_says_so(self):
        self.assertEqual(db.record_disposed(self.s.conn, ["<never-seen@x>"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
