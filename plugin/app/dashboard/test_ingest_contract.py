"""What ingest accepts, and what it says about everything else.

`ingest.py` is documented as the supported entry point from any source, which makes its
input a public API. An API that ignores what it does not understand - silently, with
`ok: true` and every count correct - is the hardest kind to write against: the failure is
invisible on the day and unrecoverable later, because the source data is gone.

Two shapes of that failure, both real:
  * a typo (`messageId`, `date`) produces a row that ingests cleanly and is quietly
    unopenable or mis-dated;
  * a connector author supplies something the schema does not have yet (`web_link`, a
    plain-text body, a thread id) and gets silence.
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

GOOD = {"account": "probe@example.com", "sender": "a@example.com", "subject": "probe",
        "msg_date": "2020-01-01", "disposition": "kept", "category": "social-notification",
        "reason": "probe", "message_id": "<probe@example.com>"}


def run_doc(**extra):
    return {"run_date": "2020-01-01", "messages": [dict(GOOD, **extra)]}


class UnknownFieldsAreNamed(unittest.TestCase):

    def test_a_fully_documented_message_reports_nothing(self):
        """The control. If this ever reports a key, every other test here is meaningless."""
        self.assertEqual(db.unknown_fields(run_doc()), {})

    def test_something_the_schema_does_not_have_is_named(self):
        """`web_link` was the reported example and is now an accepted field - which is why
        this uses a key that genuinely is not. A test whose fixture quietly becomes valid
        stops testing anything, and reports a pass either way."""
        got = db.unknown_fields(run_doc(thread_id="AAQkAD..."))
        self.assertIn("thread_id (message)", got)

    def test_the_fields_added_for_connector_installs_are_accepted(self):
        """The other half of the same contract: these must NOT be reported as unknown, or
        the seam tells connector authors their data was dropped when it was stored."""
        self.assertEqual(db.unknown_fields(run_doc(
            web_link="https://example.com/owa?ItemID=PROBE",
            body_text="Hello.")), {})

    def test_a_typo_is_named_rather_than_swallowed(self):
        """The sharper everyday case: it ingests cleanly and the row is unopenable."""
        doc = {"run_date": "2020-01-01",
               "messages": [{k: v for k, v in GOOD.items() if k != "message_id"}]}
        doc["messages"][0]["messageId"] = "<probe@example.com>"
        self.assertIn("messageId (message)", db.unknown_fields(doc))

    def test_it_counts_occurrences_not_just_names(self):
        doc = {"run_date": "2020-01-01",
               "messages": [dict(GOOD, thread_id="x") for _ in range(4)]}
        self.assertEqual(db.unknown_fields(doc)["thread_id (message)"], 4)

    def test_unknown_keys_at_every_level_are_found(self):
        doc = run_doc()
        doc["weird_run_key"] = 1
        doc["accounts"] = [{"account": "a@b.c", "weird_account_key": 2}]
        got = db.unknown_fields(doc)
        self.assertIn("weird_run_key (run)", got)
        self.assertIn("weird_account_key (account)", got)

    def test_the_contract_matches_what_the_writer_actually_reads(self):
        """The list and the INSERT must not drift. Both live in db.py for that reason, and
        this asserts the relationship rather than trusting the comment that says so."""
        import inspect, re                                         # noqa: PLC0415
        # The whole module, not just ingest_run: `to` and `cc` are consumed by
        # `recipients_of`, so grepping one function reported them as unread and the test
        # was wrong rather than the code. The contract list itself is cut out first, or
        # every field would match its own definition and this would assert nothing.
        src = inspect.getsource(db)
        src = re.sub(r"MESSAGE_FIELDS = frozenset\(\(.*?\)\)", "", src, flags=re.S)
        for field in db.MESSAGE_FIELDS:
            self.assertRegex(src, r'"%s"' % re.escape(field),
                             "%s is advertised as accepted and nothing in db.py reads it"
                             % field)

    def test_the_drift_check_can_actually_fail(self):
        """A positive control for the test above. Grepping a source file for a string is
        exactly the kind of check that quietly matches everything."""
        import inspect, re                                         # noqa: PLC0415
        src = inspect.getsource(db)
        src = re.sub(r"MESSAGE_FIELDS = frozenset\(\(.*?\)\)", "", src, flags=re.S)
        self.assertNotRegex(src, r'"a_field_nothing_reads"')


class StrictRefuses(unittest.TestCase):
    """Reported, not rejected - except under --strict, where everything else is an error
    too. Run as a subprocess because the exit code is the contract."""

    def ingest(self, doc, *flags):
        path = os.path.join(tempfile.mkdtemp(), "run.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        # EMAIL_DASHBOARD_DB has to be READ by db.py for this to be a redirect rather
        # than a decoration. It was not, the first time these tests ran, and three of them
        # wrote their fixtures into the owner's live database. The assertion below is the
        # guard against that ever being silently true again.
        scratch = os.path.join(tempfile.mkdtemp(), "t.db")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_DB=scratch)
        self.scratch = scratch
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "ingest.py"), "--file", path] + list(flags),
            capture_output=True, text=True, env=env)

    def test_strict_refuses_an_unrecognised_key(self):
        r = self.ingest(run_doc(thread_id="AAQkAD..."), "--strict")
        self.assertEqual(r.returncode, 2, r.stderr[-400:])
        self.assertIn("thread_id", r.stderr)

    def test_without_strict_it_succeeds_and_still_names_it(self):
        """Forward compatibility: a caller on a newer contract than the installed version
        must still succeed. Naming what was dropped costs nothing."""
        r = self.ingest(run_doc(thread_id="AAQkAD..."))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("thread_id", r.stderr)
        self.assertIn("thread_id (message)", json.loads(r.stdout)["ignored_keys"])

    def test_the_redirect_is_real_and_the_live_store_is_untouched(self):
        """The control that makes every test in this class safe to run."""
        self.ingest(run_doc())
        self.assertTrue(os.path.exists(self.scratch),
                        "the run must have gone to the scratch store, and it did not - "
                        "which means it went to the real one")

    def test_a_clean_run_passes_strict(self):
        """The control - otherwise --strict refusing everything would look like working."""
        r = self.ingest(run_doc(), "--strict")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StagedByArrival(unittest.TestCase):
    """A historical batch belongs to the days it happened on, not to the day we read it.

    Ingesting a year of old mail as one run put all of it into TODAY, so today's summary
    reported a refund notice from last September as this morning's news. The calendar had
    already been fixed to key on arrival and looked correct throughout - which is exactly
    why this went unnoticed: one view had been corrected and the other had not.
    """

    def doc(self, *dates):
        return {"run_date": "2026-08-07",
                "messages": [dict(GOOD, message_id="<%s@x>" % d, msg_date=d) for d in dates]}

    def ingest(self, doc, *flags):
        path = os.path.join(tempfile.mkdtemp(), "run.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_DB=os.path.join(tempfile.mkdtemp(), "t.db"))
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "ingest.py"), "--file", path] + list(flags),
            capture_output=True, text=True, env=env)
        return r, env["EMAIL_DASHBOARD_DB"]

    def rows(self, dbpath):
        conn = db.connect(dbpath)
        return sorted(tuple(r) for r in conn.execute(
            "SELECT run_date, msg_day FROM messages ORDER BY msg_day"))

    def test_each_message_lands_in_the_run_for_the_day_it_arrived(self):
        r, dbp = self.ingest(self.doc("2025-09-12", "2025-11-03", "2026-08-07"),
                             "--by-arrival")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        for run_date, msg_day in self.rows(dbp):
            self.assertEqual(run_date, msg_day,
                             "a backfilled message must sit in the run for its own day")
        self.assertEqual(json.loads(r.stdout)["runs_touched"], 3)

    def test_without_the_flag_it_still_files_everything_under_the_run_date(self):
        """The control. Staging by arrival must be something you ask for: a daily sweep
        genuinely IS one run, and splitting it would be its own misrepresentation."""
        r, dbp = self.ingest(self.doc("2025-09-12", "2026-08-07"))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertEqual({rd for rd, _ in self.rows(dbp)}, {"2026-08-07"})

    def test_mail_with_no_readable_date_is_refused_rather_than_filed_under_today(self):
        """Guessing is how a message from last year becomes today's news - the exact bug
        this flag exists to prevent, reintroduced by the fix for it."""
        doc = self.doc("2025-09-12")
        doc["messages"].append(dict(GOOD, message_id="<nodate@x>", msg_date=""))
        r, _ = self.ingest(doc, "--by-arrival")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no readable date", r.stderr)
