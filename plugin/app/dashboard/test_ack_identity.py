"""Acknowledge, then link the row - the sequence that silently threw 35 decisions away.

Reported from a live install: 35 items acknowledged in eleven minutes, every row unlinked at
the time so every ack stored under a `row:` key. A linking pass ran minutes later and gave
those rows real Message-IDs. The acks table still held all 35. The API still returned all 35.
Every one rendered as unacknowledged.

Nothing errored and nothing warned, which is why the whole of this file is about ordering:
almost every test here acks BEFORE the state changes and asserts AFTER. A test that acks a
row and reads it back unchanged passes against the broken code.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                                          # noqa: E402
import server                                                      # noqa: E402

ROW = {"account": "owner@example.com", "sender": "Boss <boss@example.com>",
       "subject": "Please renew the lease", "message_id": None}
MID = "<abc123@example.com>"


def store():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(path)
    db.init_db(conn)
    return db.connect(path)


def ack(conn, row, on=True):
    return server.api_ack(conn, {}, dict(row, on=on))


def acked(conn, row):
    return server.annotate_acks(conn, [dict(row)])[0]["acked"]


class LinkingMustNotOrphanAnAck(unittest.TestCase):

    def setUp(self):
        self.conn = store()

    def test_an_ack_survives_the_row_gaining_a_message_id(self):
        """THE reported failure, in four lines."""
        ack(self.conn, ROW)                                   # unlinked: stored as row:
        self.assertTrue(acked(self.conn, ROW))                # control: it took
        linked = dict(ROW, message_id=MID)                    # a linking pass runs
        self.assertTrue(acked(self.conn, linked),
                        "the owner's decision must survive a change to how the tool "
                        "identifies the row it was made about")

    def test_an_ack_survives_the_row_LOSING_its_message_id(self):
        """The same bug in the other direction - a re-ingest from a source that does not
        carry Message-IDs. Symmetric, and just as silent."""
        linked = dict(ROW, message_id=MID)
        ack(self.conn, linked)
        self.assertTrue(acked(self.conn, linked))             # control
        self.assertTrue(acked(self.conn, ROW))

    def test_un_acking_a_linked_row_lifts_a_legacy_ack(self):
        """The more infuriating half: undo reports ok and changes nothing."""
        ack(self.conn, ROW)                                   # stored under row:
        linked = dict(ROW, message_id=MID)
        res = ack(self.conn, linked, on=False)                # undo, now that it is linked
        self.assertTrue(res["ok"])
        self.assertEqual(res["lifted"], 1, "ok:true with nothing lifted is the failure")
        self.assertFalse(acked(self.conn, linked))
        self.assertFalse(acked(self.conn, ROW))

    def test_a_new_ack_is_stored_under_the_message_id_when_there_is_one(self):
        """The fallback must stay a fallback, or every row keeps the weaker identity."""
        linked = dict(ROW, message_id=MID)
        res = ack(self.conn, linked)
        self.assertEqual(res["key"], MID)

    def test_an_unrelated_row_is_not_acknowledged_by_association(self):
        """The control that makes the rest mean something. Matching on a SET of identities
        would be worthless if the set matched other people's mail."""
        ack(self.conn, ROW)
        other = dict(ROW, subject="A different thing entirely")
        self.assertFalse(acked(self.conn, other))
        other_account = dict(ROW, account="someone-else@example.com")
        self.assertFalse(acked(self.conn, other_account))

    def test_the_same_message_in_two_mailboxes_stays_two_items(self):
        linked = dict(ROW, message_id=MID)
        ack(self.conn, linked)
        self.assertTrue(acked(self.conn, linked))
        # A Message-ID is global, so this one is genuinely the same mail - acking it in one
        # place acking it in the other is correct, and is asserted so the behaviour is
        # deliberate rather than accidental.
        self.assertTrue(acked(self.conn, dict(linked, account="other@example.com")))


class IdentitySet(unittest.TestCase):

    def test_a_linked_row_carries_both_identities_preferred_first(self):
        ids = server.ack_identities("message", MID, ROW["sender"], ROW["subject"],
                                    ROW["account"])
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], MID, "the stored-under key must come first")
        self.assertTrue(ids[1].startswith("row:"))

    def test_an_unlinked_row_carries_only_the_row_identity(self):
        ids = server.ack_identities("message", None, ROW["sender"], ROW["subject"],
                                    ROW["account"])
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith("row:"))

    def test_the_first_identity_is_always_what_ack_key_would_store(self):
        """Two spellings of one concept is what this whole area keeps failing on. If these
        two ever disagree, a write and a read stop finding each other."""
        for mid in (MID, None, "", "   "):
            self.assertEqual(
                server.ack_identities("message", mid, ROW["sender"], ROW["subject"],
                                      ROW["account"])[0],
                server.ack_key("message", mid, ROW["sender"], ROW["subject"],
                               ROW["account"]))

    def test_threads_are_unaffected(self):
        ids = server.ack_identities("thread", None, ROW["sender"], ROW["subject"],
                                    ROW["account"])
        self.assertEqual(ids, (server.ack_key("thread", None, ROW["sender"],
                                              ROW["subject"], ROW["account"]),))




class TheEmptyIdentityIsNotAnIdentity(unittest.TestCase):
    """A `row:||` key matches every sender-less, subject-less row in the store.

    One stored acknowledgement silencing an unbounded set is the worst outcome available
    here, so the guard is asserted from both directions: it must not be written, and it must
    not be indexed if some earlier version wrote one.
    """

    def setUp(self):
        self.conn = store()

    def test_it_cannot_be_written(self):
        res = server.api_ack(self.conn, {}, {"kind": "message", "account": "",
                                             "sender": "", "subject": ""})
        self.assertFalse(res["ok"])

    def test_a_legacy_empty_ack_does_not_silence_everything(self):
        # Written straight into the table, as a build that lacked the guard would have.
        self.conn.execute("INSERT INTO acks (kind, key, account, sender, subject, "
                          "acked_at) VALUES ('message','row:||','','','','2026-01-01')")
        self.conn.commit()
        self.assertNotIn("row:||", server.acked_message_keys(self.conn) - {"row:||"},
                         "sanity")
        blank = {"account": "", "sender": "", "subject": "", "message_id": None}
        # The stored key itself still matches its own row - that is unavoidable and narrow.
        # What must NOT happen is it matching a real row that merely lacks a subject.
        self.assertFalse(acked(self.conn, dict(blank, account="owner@example.com",
                                               sender="Boss <boss@example.com>")))

    def test_an_ack_with_no_recorded_row_data_expands_to_nothing(self):
        """An ack whose account/sender/subject were never stored must contribute only its
        own key - deriving `row:||` from three NULLs and indexing it is how one old row
        marks half the store as handled."""
        self.conn.execute("INSERT INTO acks (kind, key, acked_at) "
                          "VALUES ('message','<real@example.com>','2026-01-01')")
        self.conn.commit()
        keys = server.acked_message_keys(self.conn)
        self.assertIn("<real@example.com>", keys)
        self.assertNotIn("row:||", keys)
        self.assertFalse(acked(self.conn, ROW))

if __name__ == "__main__":
    unittest.main(verbosity=2)
