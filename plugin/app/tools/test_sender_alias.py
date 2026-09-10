"""A proposal keyed `from` must be RECORDED with a sender, not only reasoned about with one.

WHAT THIS IS FOR, measured on a live run.

`ingest.py` has accepted `from` as an alias for `sender` since the day a hand-written run JSON
drifted to that key and the top-senders view silently under-counted for four runs. The alias was
added there, where the bug was felt, and nowhere else.

`apply_proposal.py` had the alias too - but only in its REASONING paths. Every entitlement
lookup read `msg.get("sender") or msg.get("from")`, so the guard judged a `from`-keyed proposal
perfectly correctly. Every RECORDING path took the bare `m.get("sender")`:

  * `journal_disposals` wrote the from column as `-`
  * `record_refusals` inserted `sender = NULL`
  * the console refusal list printed `?`

So a `from`-keyed run binned the right messages, refused the right ones, and wrote a full set
of deletion-journal lines that did not say who had sent them. Nothing failed. Every count was
internally consistent. The only thing missing was the fact a keeper's ledger exists to
hold - and `-` in a column reads as "no sender", not as "the key was spelled differently".

The property under test is NOT "the alias exists". It is that reasoning and recording cannot
disagree about who the sender is. That is why the fix normalises once at the door
(`normalise_senders`) rather than adding `or m.get("from")` to three more call sites: a
recording path added later cannot miss a normalisation that happened before it ran.

Sibling of test_disposal_journal.py (the record and the act must not come apart) - this one says
the record and the REASONING must not come apart either.

    python tools/test_sender_alias.py
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

SENDER = "Digest <no-reply@digest.test>"


def from_keyed(mid="<a@x>", subject="Top stories for you"):
    """A proposal message keyed `from`, the way a hand-written run JSON keeps drifting to."""
    return {"message_id": mid, "subject": subject, "from": SENDER,
            "account": "o@example.test", "reason": "locked auto-trash sender",
            "disposition": "would_trash"}


class NormaliseTests(unittest.TestCase):
    def test_from_becomes_sender(self):
        msgs = [from_keyed()]
        ap.normalise_senders(msgs)
        self.assertEqual(msgs[0]["sender"], SENDER)

    def test_an_explicit_sender_always_wins(self):
        """`sender` is the real key. An alias must never overwrite the thing it stands in for."""
        m = from_keyed()
        m["sender"] = "Real <real@x.test>"
        ap.normalise_senders([m])
        self.assertEqual(m["sender"], "Real <real@x.test>")

    def test_neither_key_is_survivable(self):
        m = {"message_id": "<a@x>", "subject": "s"}
        ap.normalise_senders([m])                      # must not raise
        self.assertFalse(m.get("sender"))

    def test_empty_string_sender_is_treated_as_absent(self):
        m = from_keyed()
        m["sender"] = ""
        ap.normalise_senders([m])
        self.assertEqual(m["sender"], SENDER)


class RecordingTests(unittest.TestCase):
    """The half that actually broke. Reasoning was never the problem."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "deletion-journal.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# Deletion journal\n\n| Run date | Account | From | Subject | Reason |\n")

    def test_journal_line_names_the_sender(self):
        msgs = [from_keyed()]
        ap.normalise_senders(msgs)
        n = ap.journal_disposals([(msgs[0], [])], [("o@example.test", "<a@x>")],
                                 journal=self.path, today="2026-01-02")
        self.assertEqual(n, 1)
        with open(self.path, encoding="utf-8") as fh:
            row = [ln for ln in fh if "Top stories" in ln][0]
        self.assertIn(SENDER, row)

    def test_without_normalising_the_journal_loses_the_sender(self):
        """Pins the defect itself, so a regression is a failing test and not a silent `-`."""
        n = ap.journal_disposals([(from_keyed(), [])], [("o@example.test", "<a@x>")],
                                 journal=self.path, today="2026-01-02")
        self.assertEqual(n, 1)
        with open(self.path, encoding="utf-8") as fh:
            row = [ln for ln in fh if "Top stories" in ln][0]
        self.assertNotIn(SENDER, row)      # this is what the run actually wrote, and why

    def test_refusal_row_carries_the_sender(self):
        import sqlite3
        import db as dashdb
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(dashdb.SCHEMA)
        msgs = [from_keyed(subject="Refused one")]
        ap.normalise_senders(msgs)
        ap.record_refusals(conn, [(msgs[0], ["protected"])], "2026-01-02")
        got = conn.execute("SELECT sender FROM disposal_refusals").fetchone()
        self.assertEqual(got["sender"], SENDER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
