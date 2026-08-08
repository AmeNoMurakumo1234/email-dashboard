"""Already seen is not news - and "new" must survive the suppression that hides the rest.

Reported by an owner, before any of it was measured: so much repeated security mail arriving
that a real alert would be ignored, because the habit of not looking had already been trained.

The store agreed. Account-security listings outnumbered the DISTINCT messages behind them by
about three to two: roughly two in five of the "alerts" anyone read were a repeat of one
already read. One provider notice was raised on four consecutive days with nothing about it
changed. On the day this was written the daily surfaced list fell by two thirds.

A message still sitting in the inbox is re-listed by every sweep. That is not a wrong answer,
which is why nothing ever caught it - it is a repetitive one, and repetition is how an alert
channel gets its reader to stop looking, so that the one alert that matters arrives into a
habit of not looking.

WHAT THIS SUITE IS ACTUALLY GUARDING. Suppression is the dangerous kind of feature: every
test that it hides things is also satisfied by a panel that hides everything. So the cases
below are weighted the other way - a genuinely new message, a NEW message from a sender whose
older mail was already seen, a changed subject on the same thread, and the count of what was
held back all have to survive.

    python dashboard/test_carried.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import server                                                      # noqa: E402


class Store:
    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(self.path))

    def conn(self):
        return db.connect(self.path)

    def add(self, day, subject, message_id=None, sender="Google", disposition="surfaced",
            account="owner@example.test", importance="security"):
        c = self.conn()
        c.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES (?, 'x')", (day,))
        rid = c.execute("SELECT id FROM runs WHERE run_date = ?", (day,)).fetchone()[0]
        c.execute("INSERT INTO messages (run_id, run_date, account, sender, subject, "
                  "disposition, category, concept, importance, message_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (rid, day, account, sender, subject, disposition, "security",
                   "account & security", importance, message_id))
        c.commit()

    def run(self, day, carried=False):
        q = {"date": [day]}
        if carried:
            q["carried"] = ["1"]
        return server.api_run(self.conn(), q)

    def subjects(self, day, carried=False):
        return [m["subject"] for m in self.run(day, carried)["surfaced"]]


class ARepeatIsNotRaisedTwice(unittest.TestCase):

    def setUp(self):
        self.s = Store()
        # The reported case: one notice, four consecutive days, nothing changed.
        for day in ("2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01"):
            self.s.add(day, "You shared some Google Account data with Claude", "<g1@x>")

    def test_the_first_day_still_shows_it(self):
        """The control that matters most. A suppressor that hides everything passes every
        other test in this file."""
        self.assertEqual(self.s.subjects("2026-07-29"),
                         ["You shared some Google Account data with Claude"])
        self.assertEqual(self.s.run("2026-07-29")["carried_hidden"], 0)

    def test_the_later_days_do_not(self):
        for day in ("2026-07-30", "2026-07-31", "2026-08-01"):
            self.assertEqual(self.s.subjects(day), [], "%s repeated it" % day)

    def test_what_was_held_back_is_counted(self):
        """Suppression that cannot be seen is indistinguishable from having found nothing -
        which is the same silence this whole project argues against, in the pleasant
        direction."""
        self.assertEqual(self.s.run("2026-07-31")["carried_hidden"], 1)

    def test_it_can_still_be_asked_for(self):
        self.assertEqual(self.s.subjects("2026-08-01", carried=True),
                         ["You shared some Google Account data with Claude"])
        self.assertEqual(self.s.run("2026-08-01", carried=True)["carried_hidden"], 0)

    def test_the_carried_row_says_when_it_was_first_raised(self):
        m = self.s.run("2026-08-01", carried=True)["surfaced"][0]
        self.assertTrue(m["carried"])
        self.assertEqual(m["first_surfaced"], "2026-07-29")


class NewIsStillNew(unittest.TestCase):
    """Everything here is a way the suppression could swallow something that matters."""

    def setUp(self):
        self.s = Store()
        self.s.add("2026-07-29", "New sign in to Steam", "<s1@x>")

    def test_a_genuinely_new_message_the_next_day(self):
        self.s.add("2026-07-30", "New sign in to Steam", "<s1@x>")       # the repeat
        self.s.add("2026-07-30", "Your password was changed", "<p1@x>")  # brand new
        self.assertEqual(self.s.subjects("2026-07-30"), ["Your password was changed"])

    def test_a_SECOND_notice_from_the_same_sender_and_subject(self):
        """The one that would hurt. A second sign-in to Steam is a different event with the
        same subject line - and if a repeat were keyed on the subject alone it would vanish
        exactly when someone else is signing in to your account."""
        self.s.add("2026-07-30", "New sign in to Steam", "<s2@x>")
        self.assertEqual(self.s.subjects("2026-07-30"), ["New sign in to Steam"])

    def test_a_changed_subject_on_the_same_thread(self):
        self.s.add("2026-07-30", "New sign in to Steam - unrecognised device", "<s3@x>")
        self.assertEqual(self.s.subjects("2026-07-30"),
                         ["New sign in to Steam - unrecognised device"])

    def test_the_same_subject_from_a_different_sender(self):
        self.s.add("2026-07-30", "New sign in to Steam", "<s4@x>", sender="Someone Else")
        self.assertEqual(self.s.subjects("2026-07-30"), ["New sign in to Steam"])

    def test_the_same_subject_in_a_different_mailbox(self):
        self.s.add("2026-07-30", "New sign in to Steam", "<s5@x>",
                   account="other@example.test")
        self.assertEqual(self.s.subjects("2026-07-30"), ["New sign in to Steam"])


class WithoutAMessageIdItFallsBackToTheShape(unittest.TestCase):
    """Most backfilled rows have no Message-ID. Preferring the ID and falling back only when
    it is absent is what stops a later linking pass from silently changing what counts as
    'already seen' - the defect the ack identity set exists to prevent, met again from a
    different direction."""

    def setUp(self):
        self.s = Store()

    def test_an_unlinked_repeat_is_still_recognised(self):
        self.s.add("2026-07-29", "Security alert", None)
        self.s.add("2026-07-30", "Security alert", None)
        self.assertEqual(self.s.subjects("2026-07-30"), [])

    def test_an_unlinked_row_with_no_sender_or_subject_matches_nothing(self):
        """An empty shape would match every subject-less, sender-less row in the store -
        one stale row silencing an unbounded set."""
        self.s.add("2026-07-29", "", None, sender="")
        self.s.add("2026-07-30", "", None, sender="")
        self.assertEqual(len(self.s.run("2026-07-30")["surfaced"]), 1)

    def test_a_linked_row_is_not_matched_against_a_different_message_with_the_same_shape(self):
        """Two distinct arrivals sharing a subject must stay two, once they are linked."""
        self.s.add("2026-07-29", "Payment Successful", "<a@x>")
        self.s.add("2026-07-30", "Payment Successful", "<b@x>")
        self.assertEqual(self.s.subjects("2026-07-30"), ["Payment Successful"])


class TrashedMailIsNotAffected(unittest.TestCase):
    """The trashed list is a record of what was binned that day, not an attention queue -
    collapsing it would make the day's own numbers stop adding up."""

    def test_trashed_rows_are_untouched(self):
        s = Store()
        s.add("2026-07-29", "Promo", "<t1@x>", disposition="trashed")
        s.add("2026-07-30", "Promo", "<t1@x>", disposition="trashed")
        self.assertEqual(len(s.run("2026-07-30")["trashed"]), 1)
        self.assertEqual(s.run("2026-07-30")["carried_hidden"], 0)

    def test_a_message_surfaced_then_binned_still_appears_in_the_binned_list(self):
        """The only sequence in which the attention history can reach the trashed list, and
        the one a mutation found: raised on Monday, binned on Tuesday. Tuesday's record of
        what it binned has to be complete, or the day's own numbers stop adding up and the
        `trashed` count on the run row disagrees with the list beneath it.

        Suppression belongs to the ATTENTION queue. The trashed list is a ledger."""
        s = Store()
        s.add("2026-07-29", "Promo", "<t2@x>", disposition="surfaced")
        s.add("2026-07-30", "Promo", "<t2@x>", disposition="trashed")
        day = s.run("2026-07-30")
        self.assertEqual([m["subject"] for m in day["trashed"]], ["Promo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
