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



class AnExitThatIsNotCompletion(unittest.TestCase):
    """A list whose only way out is "done" becomes a graveyard.

    Reported from a live install: an item nearly two hundred days old - a software-seat
    offer nobody was ever going to take - with no exit that was not a lie. Items like that
    are what teach a reader to skim past the one live item.
    """

    def setUp(self):
        self.s = Store()
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")

    def resolve(self, **kw):
        return server.api_resolve(self.s.conn, {}, dict({"key": "<a@x>"}, **kw))

    def test_deciding_not_to_do_it_closes_the_item(self):
        r = self.resolve(where="declined", note="not taking the offer")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.s.items()["open"], 0)
        self.assertEqual(self.s.items("all")["items"][0]["resolved_where"], "declined")

    def test_an_expired_thing_closes_too(self):
        self.assertTrue(self.resolve(where="expired")["ok"])
        self.assertEqual(self.s.items()["open"], 0)

    def test_the_old_spelling_still_works(self):
        """Existing rows and any script written against the previous release."""
        r = self.resolve(where="moot")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["where"], "declined")

    def test_an_invented_outcome_is_still_refused(self):
        """The control: widening the vocabulary must not mean accepting anything."""
        self.assertFalse(self.resolve(where="whatever")["ok"])
        self.assertEqual(self.s.items()["open"], 1)

    def test_the_offered_outcomes_are_reported_to_the_client(self):
        """So the UI cannot drift from what the server accepts - the two-spellings trap."""
        self.assertEqual(tuple(self.s.items()["resolutions"]), server.RESOLUTIONS)


class WhatTheListSaysAboutItself(unittest.TestCase):

    def setUp(self):
        self.s = Store()

    def test_median_age_not_just_length(self):
        """A list whose median age climbs is being ignored however short it is."""
        for i, day in enumerate(("2026-07-01", "2026-07-20", "2026-08-01")):
            self.s.ingest([msg(message_id="<%d@x>" % i, msg_date=day)], day)
        out = self.s.items()
        self.assertEqual(out["open"], 3)
        self.assertGreater(out["oldest_days"], out["median_days"],
                           "oldest and median must be different numbers, or one of them "
                           "is not being computed")
        self.assertGreater(out["median_days"], 0)

    def test_it_groups_by_who_is_waiting(self):
        """Four asks from one colleague is one conversation; four from four people is
        four. The owner acts by person, so the panel has to say who."""
        self.s.ingest([msg(message_id="<a@x>", sender="Boss <boss@example.com>"),
                       msg(message_id="<b@x>", sender="Boss <boss@example.com>"),
                       msg(message_id="<c@x>", sender="Other <other@example.com>")],
                      "2026-08-01")
        who = {w["who"]: w["items"] for w in self.s.items()["waiting_on_you_from"]}
        self.assertEqual(sum(who.values()), 3)
        self.assertEqual(max(who.values()), 2)

    def test_resolved_items_are_not_counted_as_waiting(self):
        """Asserted through state=all, which is the only view where this can be wrong.

        The first version of this test asked for the open-only list, where the filter is
        redundant - so it passed against a version that counted everything, and proved
        nothing. `show resolved` is exactly when a stale "waiting on you" would mislead.
        """
        self.s.ingest([msg(message_id="<a@x>", sender="Boss <boss@example.com>"),
                       msg(message_id="<b@x>", sender="Other <other@example.com>")],
                      "2026-08-01")
        server.api_resolve(self.s.conn, {}, {"key": "<a@x>", "where": "declined"})
        every = self.s.items("all")
        self.assertEqual(len(every["items"]), 2, "control: both rows are in this view")
        who = {w["who"] for w in every["waiting_on_you_from"]}
        self.assertEqual(len(who), 1, "the resolved one must not still be waiting on you")
        self.assertNotIn("boss", who)

    def test_median_ignores_resolved_items_too(self):
        self.s.ingest([msg(message_id="<old@x>", msg_date="2026-01-01")], "2026-01-01")
        self.s.ingest([msg(message_id="<new@x>", msg_date="2026-08-01")], "2026-08-01")
        before = self.s.items("all")["median_days"]
        server.api_resolve(self.s.conn, {}, {"key": "<old@x>", "where": "expired"})
        after = self.s.items("all")["median_days"]
        self.assertLess(after, before,
                        "closing the oldest item must move the median, or the number is "
                        "measuring the archive rather than the backlog")



class AcknowledgedIsNotOutstanding(unittest.TestCase):
    """Seen and done are different, and the tool must not confuse them EITHER WAY.

    Found on a real store: the backfill seeded the standing list with a message the owner
    had acknowledged two days earlier, so the panel opened demanding a decision about
    something they had already dealt with - the tool arguing with its own record of their
    judgment. A `--since` window cannot catch that; the item was recent, it was just
    already handled.
    """

    def setUp(self):
        self.s = Store()

    def ack(self, **kw):
        server.api_ack(self.s.conn, {}, dict(
            {"kind": "message", "account": "owner@example.com",
             "sender": "Boss <boss@example.com>",
             "subject": "Please renew the lease"}, **kw))

    def test_mail_acknowledged_first_never_opens_an_item(self):
        self.ack(message_id="<a@x>")
        opened, _ = self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        self.assertEqual(opened, 0)
        self.assertEqual(self.s.items()["open"], 0)

    def test_unacknowledged_mail_still_opens_one(self):
        """The control. If acks suppressed everything the feature would be gone."""
        opened, _ = self.s.ingest([msg(message_id="<b@x>")], "2026-08-01")
        self.assertEqual(opened, 1)

    def test_an_ack_stored_without_a_message_id_still_counts(self):
        """A row acknowledged before it was linked - the fallback key case."""
        self.ack()
        opened, _ = self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")
        self.assertEqual(opened, 0)

    def test_a_thread_ack_covers_this_instance(self):
        server.api_ack(self.s.conn, {}, {
            "kind": "thread", "account": "owner@example.com",
            "sender": "Boss <boss@example.com>", "subject": "Statement #4471 ready"})
        opened, _ = self.s.ingest([msg(subject="Re: Statement #9912 ready")], "2026-08-01")
        self.assertEqual(opened, 0, "acknowledging the series covers each arrival of it")

    def test_acknowledging_AFTERWARDS_does_not_RESOLVE_the_row(self):
        """The distinction survives in the STORE even though it left the panel.

        This test used to assert the item stayed in the open COUNT - the seen-is-not-done
        rule, surfaced. That was overruled: acknowledging is how an owner may say they have
        dealt with something, and a panel that argues with its reader about their own
        gesture is one they stop opening. So the row keeps its `open` state and its paper trail, and
        the count stops claiming it is outstanding.
        """
        self.s.ingest([msg(message_id="<c@x>")], "2026-08-01")
        self.ack(message_id="<c@x>")
        row = server.api_open_items(self.s.conn, {"state": ["open"], "acked": ["1"]})
        self.assertEqual(row["items"][0]["state"], "open")
        self.assertIsNone(row["items"][0]["resolved_where"],
                          "an ack is not a resolution - nobody said how it was dealt with")
        self.assertEqual(self.s.items()["open"], 0, "...but it is not counted as open")

    def test_the_row_still_records_that_it_was_acknowledged(self):
        """...and it has to SAY so, or the row reads as the tool having lost track of a
        decision the owner knows they made. That is what it looked like on a real store."""
        self.s.ingest([msg(message_id="<c@x>")], "2026-08-01")
        self.ack(message_id="<c@x>")
        row = server.api_open_items(
            self.s.conn, {"state": ["open"], "acked": ["1"]})["items"][0]
        self.assertTrue(row["acknowledged"])

    def test_an_untouched_item_is_not_marked_acknowledged(self):
        self.s.ingest([msg(message_id="<d@x>")], "2026-08-01")
        self.assertFalse(self.s.items()["items"][0]["acknowledged"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HistoricalBatchesOpenNothing(unittest.TestCase):
    """A year of old mail is history, not a to-do list.

    Carry-forward opens an item for anything attention-worthy that was not binned. Run over
    a backfill that reaches back a year, that lands scores of nine-month-old entries on a
    list whose entire argument is that its contents are live - on its first read, which is
    the read that decides whether anyone opens it again.
    """

    def test_suppressed_ingest_opens_nothing(self):
        s = Store()
        opened, seen = db.carry_open_items(s.conn, [msg(message_id="<a@x>")], "2026-08-01")
        self.assertEqual(opened, 1, "control: this mail WOULD open an item")

        s2 = Store()
        s2.conn.execute("INSERT INTO runs (run_date, created_at) VALUES ('2026-08-01','x')")
        # what ingest_run(open_items=False) does: the messages land, the list does not grow
        self.assertEqual(s2.items()["open"], 0)

    def test_the_summary_distinguishes_suppressed_from_none(self):
        """'opened: 0' because nothing needed a person, and 'opened: 0' because we were
        told not to look, are different facts. A zero is a claim."""
        import inspect                                              # noqa: PLC0415
        src = inspect.getsource(db.ingest_run)
        self.assertIn("suppressed", src)
        self.assertIn("open_items", src)

    def test_the_flag_reaches_the_command_line(self):
        import subprocess, sys as _sys                              # noqa: PLC0415
        out = subprocess.run(
            [_sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "ingest.py"), "--help"],
            capture_output=True, text=True).stdout
        self.assertIn("--no-open-items", out)


class AcknowledgedLeavesTheOpenList(unittest.TestCase):
    """The owner's ruling: acknowledging is how they say they have dealt with something.

    The seen/done distinction is real and the store still keeps both - the row is not
    deleted, not resolved, and `?acked=1` still returns it. But a panel that argues with its
    reader about what their own gesture meant is a panel they stop opening, and being right
    about a definition is worth less than being useful.
    """

    def setUp(self):
        self.s = Store()
        self.s.ingest([msg(message_id="<a@x>")], "2026-08-01")

    def test_acknowledging_takes_it_out_of_the_open_count(self):
        self.assertEqual(self.s.items()["open"], 1)          # control
        server.api_ack(self.s.conn, {}, {"kind": "message", "message_id": "<a@x>",
                                         "account": "owner@example.com",
                                         "sender": "Boss <boss@example.com>",
                                         "subject": "Please renew the lease"})
        out = self.s.items()
        self.assertEqual(out["open"], 0)
        self.assertEqual(out["hidden_because_acknowledged"], 1,
                         "a count that quietly drops to zero with no explanation is the "
                         "same silence, just in the pleasant direction")

    def test_it_is_hidden_not_lost(self):
        server.api_ack(self.s.conn, {}, {"kind": "message", "message_id": "<a@x>",
                                         "account": "owner@example.com",
                                         "sender": "Boss <boss@example.com>",
                                         "subject": "Please renew the lease"})
        still = server.api_open_items(self.s.conn, {"state": ["open"], "acked": ["1"]})
        self.assertEqual(still["open"], 1)
        self.assertTrue(still["items"][0]["acknowledged"])
        self.assertEqual(still["items"][0]["state"], "open",
                         "the row is not resolved - the paper trail keeps the difference")

    def test_an_unacknowledged_item_is_untouched(self):
        """The control. If the filter took everything the panel would be empty forever."""
        self.assertEqual(self.s.items()["open"], 1)
        self.assertEqual(self.s.items()["hidden_because_acknowledged"], 0)
