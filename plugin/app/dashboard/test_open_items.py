"""Carrying an item forward, and letting someone say it was finished somewhere else.

The behaviour under test is what happens ACROSS runs, so almost every test here ingests
twice. A carry-forward that works within one run and forgets between them would pass a
single-run test and fail at the only thing it exists to do.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                                          # noqa: E402
import server                                                      # noqa: E402


def msg(**kw):
    return dict({"account": "owner@example.com", "sender": "Boss <boss@example.com>",
                 "subject": "Please renew the lease", "msg_date": "2026-08-01",
                 "disposition": "surfaced", "category": "action", "importance":
                 "action-needed"}, **kw)


class Store:
    """A store this test owns, so nothing here can touch or read the live database."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        conn = db.connect(self.path)
        db.init_db(conn)
        self.conn = db.connect(self.path)

    def ingest(self, messages, run_date):
        """ingest_run writes to db.DB_PATH, so drive carry_open_items directly against
        this store instead - the unit under test is the carry-forward, and routing it
        through the live-path writer would put these rows in the owner's real database."""
        self.conn.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES (?,?)",
                          (run_date, run_date))
        out = db.carry_open_items(self.conn, messages, run_date)
        self.conn.commit()
        return out

    def items(self, state="open"):
        return server.api_open_items(self.conn, {"state": [state]})


class CarriesForward(unittest.TestCase):

    def setUp(self):
        self.s = Store()

    def test_an_item_that_needs_a_person_is_opened_once_and_stays(self):
        opened, seen = self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        self.assertEqual((opened, seen), (1, 0))
        # A later run never sees the message again - it is not in that run's mail at all.
        self.assertEqual(self.s.items()["open"], 1,
                         "an item must survive a run that did not mention it; that is the "
                         "entire point")

    def test_seeing_it_again_ages_it_rather_than_duplicating_it(self):
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        opened, seen = self.s.ingest([msg(message_id="<a@x>")], "2026-08-02")
        self.assertEqual((opened, seen), (0, 1))
        rows = self.s.items()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runs_seen"], 2)
        self.assertEqual(rows[0]["first_seen"], "2026-08-01")

    def test_two_batches_on_one_day_do_not_age_it_twice(self):
        """runs_seen counts runs. Otherwise a batched intake makes a one-day-old item look
        weeks old, and the age is the only thing this panel sorts on."""
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        self.assertEqual(self.s.items()["items"][0]["runs_seen"], 1)

    def test_a_recurring_notice_is_one_item_not_twelve(self):
        """Keyed by thread shape when there is no Message-ID - and the SAME shape rule the
        acks use, so a reply prefix does not split an obligation from itself."""
        self.s.ingest([msg(subject="Statement #4471 ready")], "2026-08-01")
        self.s.ingest([msg(subject="Re: Statement #9912 ready")], "2026-09-01")
        self.assertEqual(self.s.items()["open"], 1)

    def test_information_does_not_stay_open(self):
        """The control. If everything opened an item the list would be the inbox again."""
        opened, _ = self.s.ingest([msg(message_id="<b@x>", importance="info")],
                                  "2026-08-01")
        self.assertEqual(opened, 0)
        self.assertEqual(self.s.items()["open"], 0)

    def test_something_the_triage_binned_is_not_outstanding(self):
        opened, _ = self.s.ingest(
            [msg(message_id="<c@x>", disposition="trashed")], "2026-08-01")
        self.assertEqual(opened, 0)

    def test_age_is_reported_and_unknown_is_not_zero(self):
        self.s.ingest([msg(message_id="<a@x>", msg_date="2026-08-01")], "2026-08-01")
        row = self.s.items()["items"][0]
        self.assertIsNotNone(row["days_open"])
        self.assertIsNone(server._days_between(None, "2026-08-10"),
                          "a missing date must read as unknown, not as 0 days old - "
                          "otherwise the oldest item renders as the newest")


class ResolvingElsewhere(unittest.TestCase):

    def setUp(self):
        self.s = Store()
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")

    def test_it_can_be_closed_off_channel(self):
        r = server.api_resolve(self.s.conn, {}, {"key": "<a@x>", "where": "off-channel",
                                                 "note": "sorted out on a call"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.s.items()["open"], 0)
        all_rows = self.s.items("all")
        self.assertEqual(all_rows["resolved_off_channel"], 1)
        self.assertEqual(all_rows["items"][0]["resolved_note"], "sorted out on a call")

    def test_resolving_does_not_delete_it(self):
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>"})
        self.assertEqual(len(self.s.items("all")["items"]), 1,
                         "the paper trail is the point; resolved is a state, not a delete")

    def test_it_can_be_reopened(self):
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>"})
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>", "open": True})
        row = self.s.items()["items"][0]
        self.assertEqual(row["state"], "open")
        self.assertIsNone(row["resolved_where"])

    def test_a_resolved_item_is_not_reopened_by_a_re_ingest(self):
        """Re-ingesting yesterday's run is not a new obligation."""
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>"})
        opened, seen = self.s.ingest([msg(message_id="<a@x>")], "2026-08-02")
        self.assertEqual((opened, seen), (0, 0))
        self.assertEqual(self.s.items()["open"], 0)

    def test_a_genuinely_new_message_still_opens(self):
        """The control for the test above: closing one thing must not close the next."""
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>"})
        opened, _ = self.s.ingest([msg(message_id="<NEW@x>")], "2026-08-02")
        self.assertEqual(opened, 1)
        self.assertEqual(self.s.items()["open"], 1)

    def test_an_unrecorded_reason_is_refused(self):
        r = server.api_resolve(self.s.conn, {}, {"key": "<a@x>", "where": "whatever"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.s.items()["open"], 1, "a refused write must change nothing")

    def test_resolving_something_that_is_not_open_is_refused(self):
        r = server.api_resolve(self.s.conn, {}, {"key": "<nope@x>"})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
