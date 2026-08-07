"""Resume, tested against a fake mailbox - because the point of intake is what survives a stop.

Every test here drives a fake fetcher. Proving that a half-finished intake resumes correctly
by talking to a real IMAP server would test the network; the behaviour that matters is what
the progress file says after a session ends in the middle, and that is pure logic.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import intake                                                      # noqa: E402


class FakeMailbox:
    """A mailbox with known UIDs, and a record of every range it was asked for."""

    def __init__(self, uids, total=None):
        self.uids = sorted(uids)
        self.total = total if total is not None else len(uids)
        self.asked = []

    def __call__(self, argv):
        # Scanned by name rather than zipped into pairs. Pairing argv[::2] with argv[1::2]
        # is off by one the moment there is a leading verb, and the result was a fake that
        # answered every call as if it were the plan probe - so `asked` stayed empty and
        # four tests failed pointing at the code instead of at this line.
        def flag(name):
            return argv[argv.index(name) + 1] if name in argv else None

        rng = flag("--uid-range")
        if rng:
            lo, hi = (int(x) for x in rng.split(":"))
            self.asked.append((lo, hi))
            got = [u for u in self.uids if lo <= u <= hi]
            return {"total_matched": len(got), "returned": len(got),
                    "messages": [{"uid": str(u), "subject": "s%d" % u} for u in got]}
        # the plan-time probe: newest message only
        top = self.uids[-1] if self.uids else 0
        return {"total_matched": self.total,
                "messages": [{"uid": str(top), "subject": "newest"}] if top else []}


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class IntakeResumes(unittest.TestCase):

    def setUp(self):
        # The state directory is redirected so a test never writes into the real runs/ tree.
        self._real = intake.STATE_DIR
        intake.STATE_DIR = Path(tempfile.mkdtemp())
        self.out = tempfile.mkdtemp()
        self.box = FakeMailbox(list(range(1, 101)))
        self.acct = "owner@example.com"

    def tearDown(self):
        intake.STATE_DIR = self._real

    def plan(self, batch=25, days=0):
        return intake.cmd_plan(
            Args(account=self.acct, mailbox="INBOX", batch=batch, days=days),
            run=self.box)

    def nxt(self):
        n = len([f for f in os.listdir(self.out)])
        return intake.cmd_next(
            Args(account=self.acct, out=os.path.join(self.out, "b%d.json" % n),
                 limit=0, no_snippets=True), run=self.box)

    def test_plan_covers_the_whole_uid_space_including_the_top(self):
        self.assertEqual(self.plan(), 0)
        state = intake.load_state(self.acct)
        ranges = [tuple(int(x) for x in b["uid_range"].split(":"))
                  for b in state["batches"]]
        self.assertEqual(ranges[0][0], 1)
        self.assertEqual(ranges[-1][1], 100, "the newest message must be inside a batch")
        for (_, hi), (lo, _) in zip(ranges, ranges[1:]):
            self.assertEqual(lo, hi + 1, "a gap between windows loses messages silently")

    def test_a_batch_marked_done_is_never_offered_again(self):
        """`done` is the acknowledgement, and it is the only thing that retires a batch.

        Note what this deliberately does NOT claim: that a *fetched* batch is retired. It
        is not, and the test below says so. A session killed between fetch and ingest has
        pulled messages that never reached the store, and re-offering that batch is the
        safe direction - a duplicated row is visible in the dashboard and can be removed,
        while a batch quietly skipped is a hole nothing ever reports. These two tests
        contradicted each other when they were written; this is the one that gave way.
        """
        self.plan()
        self.nxt()
        intake.cmd_done(Args(account=self.acct, batch=1, count=25))
        done_range = self.box.asked[-1]
        for _ in range(3):
            self.nxt()
            self.assertNotEqual(self.box.asked[-1], done_range,
                                "an acknowledged batch must never come back")

    def test_batch_2_is_offered_again_if_it_was_never_marked_done(self):
        """Fetched-but-not-ingested is the state a killed session leaves behind, and it
        must be re-offered - the messages were pulled but never reached the store."""
        self.plan()
        self.nxt()
        intake.cmd_done(Args(account=self.acct, batch=1, count=25))
        self.nxt()                                   # batch 2 fetched, never marked done
        state = intake.load_state(self.acct)
        self.assertEqual(state["batches"][1]["state"], "fetched")
        self.nxt()
        self.assertEqual(self.box.asked[-1], self.box.asked[-2],
                         "an un-ingested batch must come back, not be skipped")

    def test_done_refuses_a_batch_that_was_never_fetched(self):
        """Otherwise a hole is marked complete and the summary still reads 100%."""
        self.plan()
        rc = intake.cmd_done(Args(account=self.acct, batch=3, count=99))
        self.assertEqual(rc, 1)
        self.assertEqual(intake.load_state(self.acct)["batches"][2]["state"], "pending")

    def test_every_message_is_covered_exactly_once_across_a_full_run(self):
        """The claim intake actually makes. Nothing fetched twice, nothing missed."""
        self.plan(batch=25)
        state = intake.load_state(self.acct)
        seen = []
        for i in range(len(state["batches"])):
            self.nxt()
            intake.cmd_done(Args(account=self.acct, batch=i + 1, count=None))
        for lo, hi in self.box.asked:
            seen += [u for u in self.box.uids if lo <= u <= hi]
        self.assertEqual(sorted(seen), self.box.uids)
        self.assertEqual(len(seen), len(set(seen)), "a message fetched twice is triaged twice")

    def test_a_sparse_mailbox_still_reaches_the_newest_message(self):
        """UIDs are not dense. Years of deletions leave a huge, mostly-empty span, and a
        plan built from the message count alone stops far short of the top."""
        self.box = FakeMailbox([1, 2, 3, 9001, 9002], total=5)
        self.plan(batch=2)
        state = intake.load_state(self.acct)
        self.assertEqual(int(state["batches"][-1]["uid_range"].split(":")[1]), 9002)
        seen = []
        for i in range(len(state["batches"])):
            self.nxt()
            intake.cmd_done(Args(account=self.acct, batch=i + 1, count=None))
        for lo, hi in self.box.asked:
            seen += [u for u in self.box.uids if lo <= u <= hi]
        self.assertEqual(sorted(seen), self.box.uids)

    def test_an_empty_mailbox_says_so_rather_than_planning_nothing(self):
        self.box = FakeMailbox([], total=0)
        self.assertEqual(self.plan(), 1)
        self.assertIsNone(intake.load_state(self.acct))



class BoundedByDate(unittest.TestCase):
    """A one-year intake must not page eleven years of UID space.

    The batches page by UID and `--days` bounds by DATE, so the plan has to ASK the mailbox
    which UID the window starts at. Deriving it from the date arithmetically would assume
    UIDs advance evenly with time, and they do not - a quiet December and a busy March
    consume the same span at very different rates.
    """

    def setUp(self):
        self._real = intake.STATE_DIR
        intake.STATE_DIR = Path(tempfile.mkdtemp())
        self.acct = "owner@example.com"

    def tearDown(self):
        intake.STATE_DIR = self._real

    def test_the_plan_starts_at_the_windows_oldest_uid(self):
        # A mailbox of five hundred, of which only the newest hundred fall
        # inside the window the owner asked for.
        box = WindowedMailbox(all_uids=list(range(1, 501)), window_uids=list(range(401, 501)))
        intake.cmd_plan(Args(account=self.acct, mailbox="INBOX", batch=50, days=365),
                        run=box)
        state = intake.load_state(self.acct)
        self.assertEqual(state["lowest_uid"], 401,
                         "planning from UID 1 would re-fetch four hundred messages that "
                         "are outside the window the owner asked for")
        first = int(state["batches"][0]["uid_range"].split(":")[0])
        last = int(state["batches"][-1]["uid_range"].split(":")[1])
        self.assertEqual((first, last), (401, 500))

    def test_without_days_it_still_plans_the_whole_mailbox(self):
        """The control: bounding must be something you ask for, not the new default."""
        box = WindowedMailbox(all_uids=list(range(1, 501)), window_uids=list(range(401, 501)))
        intake.cmd_plan(Args(account=self.acct, mailbox="INBOX", batch=50, days=0), run=box)
        self.assertEqual(intake.load_state(self.acct)["lowest_uid"], 1)


class WindowedMailbox:
    """A mailbox where `--days` matches only some of the messages."""

    def __init__(self, all_uids, window_uids):
        self.all, self.window = sorted(all_uids), sorted(window_uids)

    def __call__(self, argv):
        def flag(name):
            return argv[argv.index(name) + 1] if name in argv else None

        rng = flag("--uid-range")
        if rng:
            lo, hi = (int(x) for x in rng.split(":"))
            got = [u for u in self.all if lo <= u <= hi]
            return {"total_matched": len(got),
                    "messages": [{"uid": str(u), "subject": "s"} for u in got]}
        pool = self.window if flag("--days") not in (None, "0") else self.all
        offset = int(flag("--offset") or 0)
        # mailtool's offset skips the N NEWEST, so offset total-1 is the oldest.
        chosen = pool[len(pool) - 1 - offset] if offset else pool[-1]
        return {"total_matched": len(pool),
                "messages": [{"uid": str(chosen), "subject": "s"}]}


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StatusSurvivesItsOwnOutputDirectory(unittest.TestCase):
    """`status` globbed intake-*.json and handed every match to the reporter.

    Batches land in that same directory by default, so the first fetched batch made the
    status command crash with a KeyError. A status command that dies on the contents of its
    own output directory is not a status command - and it fails at exactly the moment you
    reach for it, which is when you have lost track of where an intake got to.
    """

    def setUp(self):
        self._real = intake.STATE_DIR
        intake.STATE_DIR = Path(tempfile.mkdtemp())

    def tearDown(self):
        intake.STATE_DIR = self._real

    def write(self, name, obj):
        with open(intake.STATE_DIR / name, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_a_fetched_batch_in_the_same_directory_is_ignored(self):
        self.write("intake-owner_example.com.json",
                   {"account": "owner@example.com", "mailbox": "INBOX",
                    "total_at_plan": 1, "batches": [
                        {"n": 1, "uid_range": "1:9", "state": "pending",
                         "fetched": None, "ingested": None}]})
        # what `next --out runs/intake-batch-1.json` leaves behind
        self.write("intake-batch-1.json",
                   {"account": "owner@example.com", "mailbox": "INBOX",
                    "messages": [{"uid": "1", "subject": "s"}]})
        self.write("intake-batch-1-triaged.json",
                   {"run_date": "2026-08-07", "messages": [], "accounts": []})
        self.assertEqual(intake.cmd_status(Args(account=None)), 0)

    def test_unreadable_json_beside_the_state_is_ignored(self):
        with open(intake.STATE_DIR / "intake-broken.json", "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertEqual(intake.cmd_status(Args(account=None)), 0)

    def test_it_still_reports_a_real_plan(self):
        """The control. Ignoring everything would also 'not crash'."""
        self.write("intake-owner_example.com.json",
                   {"account": "owner@example.com", "mailbox": "INBOX",
                    "total_at_plan": 9, "batches": [
                        {"n": 1, "uid_range": "1:9", "state": "done",
                         "fetched": 9, "ingested": 9}]})
        import io, contextlib                                      # noqa: PLC0415
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            intake.cmd_status(Args(account=None))
        self.assertIn("owner@example.com", buf.getvalue())
        self.assertIn("1/1", buf.getvalue())
