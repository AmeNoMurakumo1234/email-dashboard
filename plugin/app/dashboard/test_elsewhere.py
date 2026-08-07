"""The scoreboard, and the four ways it could congratulate its owner for nothing.

This is the only number on the board that measures the OUTCOME rather than the activity, so
it is also the easiest one to make dishonest. The failure modes, all tested here:

  * counting broadcasts as reaches - the first version did, on real mail, at scale;
  * reporting 0 as a score when nothing was ever measurable;
  * calling a quiet month an improvement;
  * comparing six days of this month against all of last month.
"""
import datetime
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import elsewhere                                                   # noqa: E402

CHAT = "no-reply@chat.example.com"          # any automated notifier
SOCIAL = "notification@social.example.com"  # one that also broadcasts
PATS = ()          # no configured list: the shape test applies


def row(sender, subject, day):
    return {"sender": sender, "subject": subject, "day": day}


def bulk(n, day, sender="someone@example.com", subject="ordinary mail"):
    return [row(sender, subject, day) for _ in range(n)]


class WhatCountsAsGoingElsewhere(unittest.TestCase):

    def test_somebody_messaging_you_counts(self):
        self.assertTrue(elsewhere.went_elsewhere(CHAT, "Alice Smith sent you a message", PATS))

    def test_a_broadcast_from_the_same_sender_does_not(self):
        """THE bug this feature nearly shipped with. Matching on sender alone reported
        every 'X posted a new photo' as somebody coming to find you - on a real mailbox
        that was 144 reaches, none of which was a elsewhere."""
        for subject in ("Sam Doe posted a new photo",
                        "Heather commented: \"call tonight\"",
                        "catch up on moments you've missed",
                        "Sam shared a memory",
                        "It's Jamie's birthday"):
            self.assertFalse(elsewhere.went_elsewhere(SOCIAL, subject, PATS), subject)

    def test_a_direct_message_on_a_social_platform_still_counts(self):
        """The control. If the broadcast filter were too broad the feature would be dead
        on consumer platforms, which is where personal mailboxes get reached."""
        self.assertTrue(elsewhere.went_elsewhere(SOCIAL, "Jane Doe sent you a message", PATS))

    def test_a_person_saying_they_messaged_you_is_not_a_reach(self):
        """Sent by a colleague, from their own address: that is email WORKING."""
        self.assertFalse(
            elsewhere.went_elsewhere("boss@work.example.com", "Alice sent you a message on Teams",
                           PATS))

    def test_a_nameless_notice_is_still_a_reach(self):
        self.assertTrue(elsewhere.went_elsewhere("noreply@another.example.com", "You have 3 unread messages",
                                       PATS))
        self.assertIsNone(elsewhere.who_reached("You have 3 unread messages"),
                          "nobody is named, so nobody may be named")

    def test_who_reached_survives_a_pattern_with_no_capture_group(self):
        """It raised IndexError, which took out the endpoint rather than returning the
        honest "reached by someone, name unknown"."""
        for subject in ("You have 3 unread messages", "Missed activity",
                        "Alice sent you a message"):
            elsewhere.who_reached(subject)          # must not raise

    def test_a_configured_list_narrows_to_exactly_those_senders(self):
        pats, chosen = elsewhere.configured_senders({"elsewhere_senders": ["mychat.example.com"]})
        self.assertTrue(chosen)
        self.assertEqual(pats, ("mychat.example.com",))
        self.assertFalse(elsewhere.went_elsewhere(CHAT, "Alice sent you a message", pats))

    def test_no_config_reports_that_the_list_is_a_default(self):
        _, chosen = elsewhere.configured_senders({})
        self.assertFalse(chosen, "a default list matching nothing means something "
                                 "different from a chosen one matching nothing")


class NotMeasuredIsNotZero(unittest.TestCase):

    def test_a_mailbox_with_no_chat_platform_is_not_a_perfect_score(self):
        out = elsewhere.scoreboard(bulk(50, "2026-07-01"), today=datetime.date(2026, 8, 6))
        self.assertFalse(out["measured"])
        self.assertTrue(out["why_not"])
        self.assertEqual(out["total"], 0)

    def test_the_explanation_differs_when_the_owner_chose_the_list(self):
        """A default that matches nothing means "we did not find your platform"; a chosen
        one that matches nothing means "your platform is quiet". Different findings."""
        rows = bulk(10, "2026-07-01")
        default = elsewhere.scoreboard(rows, today=datetime.date(2026, 8, 6))
        chosen = elsewhere.scoreboard(rows, cfg={"elsewhere_senders": ["mychat.example.com"]},
                                  today=datetime.date(2026, 8, 6))
        self.assertNotEqual(default["why_not"], chosen["why_not"])

    def test_one_real_reach_makes_it_measured(self):
        rows = bulk(10, "2026-07-01") + [row(CHAT, "Alice sent you a message", "2026-07-02")]
        self.assertTrue(elsewhere.scoreboard(rows, today=datetime.date(2026, 8, 6))["measured"])


class AQuietMonthIsNotAWin(unittest.TestCase):

    def months(self, jun_reaches, jun_mail, jul_reaches, jul_mail):
        rows = []
        for i in range(jun_reaches):
            rows.append(row(CHAT, "A%d sent you a message" % i, "2026-06-01"))
        rows += bulk(jun_mail, "2026-06-01")
        for i in range(jul_reaches):
            rows.append(row(CHAT, "A%d sent you a message" % i, "2026-07-01"))
        rows += bulk(jul_mail, "2026-07-01")
        return elsewhere.scoreboard(rows, today=datetime.date(2026, 8, 6))

    def test_the_rate_is_what_moves_not_the_count(self):
        """Half the reaches in half the mail is the SAME performance, and a count-based
        scoreboard would call it a 50% improvement."""
        out = self.months(10, 90, 5, 45)
        self.assertEqual(out["trend"]["direction"], "flat")

    def test_a_real_improvement_is_reported_as_one(self):
        """The control - if it called everything flat it would be useless."""
        self.assertEqual(self.months(10, 90, 2, 98)["trend"]["direction"], "better")

    def test_getting_worse_is_reported_too(self):
        self.assertEqual(self.months(2, 98, 10, 90)["trend"]["direction"], "worse")

    def test_the_volume_caveat_travels_with_the_verdict(self):
        """In the payload, not in a footnote somewhere nobody reads."""
        out = self.months(10, 990, 1, 99)
        self.assertIn("1000", out["trend"]["caveat"])   # reaches count as messages too
        self.assertIn("100", out["trend"]["caveat"])


class TheMonthInProgressIsNotADataPoint(unittest.TestCase):

    def build(self, today):
        rows = ([row(CHAT, "Alice sent you a message", "2026-06-%02d" % (i + 1))
                 for i in range(10)] + bulk(90, "2026-06-01")
                + [row(CHAT, "Alice sent you a message", "2026-07-%02d" % (i + 1))
                   for i in range(4)] + bulk(96, "2026-07-01")
                + [row(CHAT, "Alice sent you a message", "2026-08-01")])
        return elsewhere.scoreboard(rows, today=today)

    def test_it_is_flagged_partial(self):
        out = self.build(datetime.date(2026, 8, 6))
        by = {m["month"]: m["partial"] for m in out["months"]}
        self.assertTrue(by["2026-08"])
        self.assertFalse(by["2026-07"])

    def test_the_trend_ignores_it(self):
        """One day of August is a 100% rate. Included, it reports catastrophe every time
        the page is opened early in a month - and the catastrophe is the calendar."""
        out = self.build(datetime.date(2026, 8, 6))
        self.assertEqual(out["trend"]["to"], "2026-07")
        self.assertEqual(out["trend"]["direction"], "better")

    def test_the_same_data_a_month_later_does_include_august(self):
        """The control: the exclusion must be about the month being INCOMPLETE, not about
        August."""
        out = self.build(datetime.date(2026, 9, 2))
        self.assertEqual(out["trend"]["to"], "2026-08")

    def test_a_one_character_name_is_still_a_reach(self):
        """It was not. `{2,60}` on the name made the whole notice invisible, when the
        honest answer is "reached, name too short to attribute"."""
        self.assertTrue(elsewhere.went_elsewhere(CHAT, "X sent you a message", PATS))
        self.assertIsNone(elsewhere.who_reached("X sent you a message"))

    def test_one_complete_month_is_not_a_trend(self):
        out = elsewhere.scoreboard(
            [row(CHAT, "Alice sent you a message", "2026-08-01")] + bulk(9, "2026-08-01"),
            today=datetime.date(2026, 8, 6))
        self.assertEqual(out["trend"]["direction"], "unknown")


class ScoringAgainstWhoMatters(unittest.TestCase):

    def test_a_reach_from_a_protected_person_is_counted_separately(self):
        rows = ([row(CHAT, "Alice Smith sent you a message", "2026-07-01"),
                 row(CHAT, "Random Stranger sent you a message", "2026-07-01")]
                + bulk(98, "2026-07-01"))
        out = elsewhere.scoreboard(rows, protected=["Smith"],
                               today=datetime.date(2026, 8, 6))
        jul = [m for m in out["months"] if m["month"] == "2026-07"][0]
        self.assertEqual(jul["reaches"], 2)
        self.assertEqual(jul["from_people_who_matter"], 1)
        self.assertIn("Alice Smith", jul["who"])

    def test_with_an_empty_guard_the_column_cannot_mean_anything(self):
        """0 "from people who matter" when nobody is on the list is scoring against
        nobody, and the caller has to be able to tell that from a real zero."""
        out = elsewhere.scoreboard([row(CHAT, "Alice sent you a message", "2026-07-01")],
                               protected=[], today=datetime.date(2026, 8, 6))
        self.assertEqual(out["protected_known"], 0)


class RegexesMeanWhatTheyRead(unittest.TestCase):
    """A guard for a class of corruption that has now bitten this project three times.

    Editing a regex through a shell heredoc can turn `\\b` into a literal backspace. The
    result compiles, never errors, and silently matches nothing - the exact shape of defect
    this whole codebase is organised against, arriving through the editing tools rather than
    through the logic. Twenty-two of them accumulated in one file before anyone looked.
    """

    MODULES = ("elsewhere", "questions", "concepts", "db", "untrusted")

    def test_no_compiled_pattern_contains_a_control_character(self):
        import importlib                                            # noqa: PLC0415
        checked = 0
        for name in self.MODULES:
            try:
                mod = importlib.import_module(name)
            except ImportError:
                continue
            for attr in vars(mod).values():
                for pat in (attr if isinstance(attr, (list, tuple)) else [attr]):
                    if not isinstance(pat, re.Pattern):
                        continue
                    checked += 1
                    bad = [c for c in pat.pattern if ord(c) < 32 and c not in "\n\t"]
                    self.assertEqual(
                        bad, [],
                        "%s carries %r - almost certainly a \\\\b eaten by an editor; it "
                        "compiles and matches nothing" % (name, bad))
        self.assertGreater(checked, 10, "control: this must actually be finding patterns, "
                                        "or it passes by inspecting nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
