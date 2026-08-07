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

    def plan(self, batch=25):
        return intake.cmd_plan(Args(account=self.acct, mailbox="INBOX", batch=batch),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
