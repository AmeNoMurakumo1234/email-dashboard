"""An acknowledgement can be recorded without a browser, and it is the SAME acknowledgement. (F26)

`INSERT INTO acks` appeared in exactly one place - the dashboard's HTTP handler - so the only
way to record "I have dealt with this" was to click. That is fine for a person at a screen and
wrong for the operating model this plugin prescribes, where the thing maintaining the board is
a scheduled task with no UI and no session.

What the gap forces is the interesting part. An item gets dealt with off-channel - answered in
a call, decided in a meeting - while the mail thread shows nothing, so a routine with no way
to record that re-escalates it every run. A parallel markdown ledger gets invented; the sweep
reads it and the dashboard does not; and two stores then answer "has the owner dealt with
this?" differently, each correct for its own reader. A clean result from a broken instrument.

So the test that matters is not "the CLI writes a row" - it is that the row written headlessly
and the row written by clicking are INDISTINGUISHABLE afterwards. Two doors, one record.

    python dashboard/test_ack_headless.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import server                                                      # noqa: E402

ACK_CLI = os.path.join(HERE, "ack.py")
INGEST = os.path.join(HERE, "ingest.py")


def message(subject="Response requested: seat audit", **kw):
    m = {"account": "owner@example.test", "sender": "vendor@example.test",
         "subject": subject, "msg_date": "2026-08-01T09:00:00+00:00",
         "disposition": "surfaced", "category": "bank-statement", "reason": "r",
         "importance": "action-needed", "message_id": "<%s@example.test>" % abs(hash(subject))}
    m.update(kw)
    return m


class Store:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        db.init_db(db.connect(self.path))

    def env(self):
        return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                    EMAIL_DASHBOARD_DB=self.path)

    def run(self, script, *argv):
        p = subprocess.run([sys.executable, script] + list(argv), cwd=HERE,
                           capture_output=True, text=True, env=self.env(),
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr

    def ingest(self, payload, *flags):
        f = os.path.join(self.dir, "run.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return self.run(INGEST, "--file", f, *flags)

    def acks(self):
        return [dict(r) for r in db.connect(self.path).execute(
            "SELECT kind, key, account, sender, subject, note FROM acks ORDER BY key")]


class TheCliWritesTheSameRowTheButtonDoes(unittest.TestCase):

    def setUp(self):
        self.s = Store()
        self.m = message()
        self.s.ingest({"run_date": "2026-08-01", "messages": [self.m]})

    def by_hand(self):
        """What the HTTP handler does, called directly."""
        conn = db.connect(self.s.path)
        return server.api_ack(conn, {}, {
            "kind": "message", "message_id": self.m["message_id"],
            "sender": self.m["sender"], "subject": self.m["subject"],
            "account": self.m["account"], "note": "clicked"})

    def test_the_cli_records_an_ack(self):
        rc, out, err = self.s.run(ACK_CLI, "--message-id", self.m["message_id"],
                                  "--sender", self.m["sender"],
                                  "--subject", self.m["subject"],
                                  "--account", self.m["account"],
                                  "--note", "answered on the call")
        self.assertEqual(rc, 0, err[-800:])
        rows = self.s.acks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "answered on the call")

    def test_the_two_doors_produce_the_same_key(self):
        """The whole point. A headless ack under a different identity would be a THIRD store
        answering the same question - the defect, moved rather than fixed."""
        self.s.run(ACK_CLI, "--message-id", self.m["message_id"],
                   "--sender", self.m["sender"], "--subject", self.m["subject"],
                   "--account", self.m["account"], "--note", "headless")
        headless = self.s.acks()
        self.assertEqual(len(headless), 1)
        # now do it the UI way, which must UPDATE that row rather than add a second
        self.by_hand()
        clicked = self.s.acks()
        self.assertEqual(len(clicked), 1, "the two doors disagreed about the identity")
        self.assertEqual(headless[0]["key"], clicked[0]["key"])

    def test_the_dashboard_sees_a_headless_ack(self):
        """It is not enough to write the row - the panel that decides what is outstanding
        has to honour it, which is the half the markdown ledger could never do."""
        self.s.run(ACK_CLI, "--message-id", self.m["message_id"],
                   "--sender", self.m["sender"], "--subject", self.m["subject"],
                   "--account", self.m["account"])
        conn = db.connect(self.s.path)
        rows = [dict(r) for r in conn.execute(
            "SELECT account, sender, subject, message_id FROM messages")]
        server.annotate_acks(conn, rows)
        self.assertTrue(rows[0]["acked"])

    def test_an_ack_with_nothing_identifiable_is_refused_not_stored(self):
        """A row stored against an empty identity silences nothing and reports success -
        and `row:||` would match every subject-less, sender-less row in the store."""
        rc, out, err = self.s.run(ACK_CLI, "--subject", "   ")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.s.acks(), [])

    def test_lift_works_headlessly_too(self):
        self.s.run(ACK_CLI, "--message-id", self.m["message_id"],
                   "--sender", self.m["sender"], "--subject", self.m["subject"])
        self.assertEqual(len(self.s.acks()), 1)
        rc, out, err = self.s.run(ACK_CLI, "--message-id", self.m["message_id"],
                                  "--sender", self.m["sender"],
                                  "--subject", self.m["subject"], "--lift")
        self.assertEqual(rc, 0, err[-400:])
        self.assertEqual(self.s.acks(), [])

    def test_list_says_which_store_it_looked_in(self):
        """"Nothing acknowledged" and "looked in the wrong file" must not read the same."""
        rc, out, err = self.s.run(ACK_CLI, "--list")
        self.assertEqual(rc, 0, err[-400:])
        self.assertIn("no acknowledgements recorded in", out)
        self.assertIn(os.path.basename(self.s.path), out)


class IngestCarriesAcknowledgements(unittest.TestCase):
    """A sweep records what it LEARNED in the same call that records what it SAW."""

    def setUp(self):
        self.s = Store()
        self.m = message()

    def test_an_acknowledgements_block_is_applied(self):
        rc, out, err = self.s.ingest({
            "run_date": "2026-08-01", "messages": [self.m],
            "acknowledgements": [{"message_id": self.m["message_id"],
                                  "sender": self.m["sender"],
                                  "subject": self.m["subject"],
                                  "account": self.m["account"],
                                  "note": "decided in the standup"}]})
        self.assertEqual(rc, 0, err[-800:])
        self.assertEqual(len(self.s.acks()), 1)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["acknowledged"], 1)

    def test_it_is_reported_rather_than_silent(self):
        _, _, err = self.s.ingest({
            "run_date": "2026-08-01", "messages": [self.m],
            "acknowledgements": [{"message_id": self.m["message_id"],
                                  "subject": self.m["subject"]}]})
        self.assertIn("acked   1/1", err)

    def test_an_unidentifiable_ack_is_counted_and_named(self):
        rc, out, err = self.s.ingest({
            "run_date": "2026-08-01", "messages": [self.m],
            "acknowledgements": [{"subject": "   "}]})
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((res["acknowledged"], res["acknowledgements_refused"]), (0, 1))
        self.assertIn("ack refused", err)

    def test_no_block_reports_nothing_and_writes_nothing(self):
        """The control: a run that sent no acknowledgements must not be told about any."""
        rc, out, err = self.s.ingest({"run_date": "2026-08-01", "messages": [self.m]})
        self.assertNotIn("acked", err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["acknowledged"], 0)
        self.assertEqual(self.s.acks(), [])

    def test_the_key_is_a_documented_accepted_key(self):
        """It must not trip the unrecognised-key report it is supposed to travel beside."""
        _, out, err = self.s.ingest({
            "run_date": "2026-08-01", "messages": [self.m],
            "acknowledgements": [{"subject": self.m["subject"]}]})
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["ignored_keys"], [])
        self.assertIn("acknowledgements", __import__("ingest").__doc__)

    def test_an_unknown_ack_field_is_still_reported(self):
        _, out, _ = self.s.ingest({
            "run_date": "2026-08-01", "messages": [self.m],
            "acknowledgements": [{"subject": self.m["subject"], "closed_by": "someone"}]})
        # The report names the SECTION too ("closed_by (acknowledgement)"), which is the
        # point of it - the same key name can be accepted in one block and unknown in another.
        ignored = json.loads(out.strip().splitlines()[-1])["ignored_keys"]
        self.assertIn("closed_by (acknowledgement)", ignored, ignored)


class ImportingTheLedgerTheGapForced(unittest.TestCase):
    """The markdown file becomes an export of the table, not a second database."""

    def setUp(self):
        self.s = Store()
        self.m = message()
        self.s.ingest({"run_date": "2026-08-01", "messages": [self.m]})
        self.md = os.path.join(self.s.dir, "ACKNOWLEDGED.md")

    def write_md(self, *lines):
        with open(self.md, "w", encoding="utf-8") as f:
            f.write("# Acknowledged\n\n" + "\n".join("- " + l for l in lines) + "\n")

    def test_a_line_that_matches_a_message_is_recorded(self):
        self.write_md(self.m["subject"])
        rc, out, err = self.s.run(ACK_CLI, "--import-md", self.md)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.s.acks()), 1)

    def test_a_line_that_matches_nothing_is_refused_not_invented(self):
        """An ack stored against an identity no row has silences nothing and reports
        success - the same failure this whole area keeps producing, one level up."""
        self.write_md("something that was never in this mailbox")
        rc, out, err = self.s.run(ACK_CLI, "--import-md", self.md)
        self.assertEqual(rc, 1)
        self.assertIn("NO MATCH", out)
        self.assertEqual(self.s.acks(), [])

    def test_dry_run_writes_nothing(self):
        self.write_md(self.m["subject"])
        rc, out, _ = self.s.run(ACK_CLI, "--import-md", self.md, "--dry-run")
        self.assertIn("would ack", out)
        self.assertEqual(self.s.acks(), [])

    def test_a_dated_prefix_is_stripped(self):
        self.write_md("2026-07-04 - " + self.m["subject"])
        self.s.run(ACK_CLI, "--import-md", self.md)
        self.assertEqual(len(self.s.acks()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
