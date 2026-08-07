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

    def test_the_reported_probe_names_web_link(self):
        got = db.unknown_fields(run_doc(web_link="https://example.com/owa?ItemID=PROBE"))
        self.assertIn("web_link (message)", got)

    def test_a_typo_is_named_rather_than_swallowed(self):
        """The sharper everyday case: it ingests cleanly and the row is unopenable."""
        doc = {"run_date": "2020-01-01",
               "messages": [{k: v for k, v in GOOD.items() if k != "message_id"}]}
        doc["messages"][0]["messageId"] = "<probe@example.com>"
        self.assertIn("messageId (message)", db.unknown_fields(doc))

    def test_it_counts_occurrences_not_just_names(self):
        doc = {"run_date": "2020-01-01",
               "messages": [dict(GOOD, web_link="x") for _ in range(4)]}
        self.assertEqual(db.unknown_fields(doc)["web_link (message)"], 4)

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
        r = self.ingest(run_doc(web_link="https://example.com"), "--strict")
        self.assertEqual(r.returncode, 2, r.stderr[-400:])
        self.assertIn("web_link", r.stderr)

    def test_without_strict_it_succeeds_and_still_names_it(self):
        """Forward compatibility: a caller on a newer contract than the installed version
        must still succeed. Naming what was dropped costs nothing."""
        r = self.ingest(run_doc(web_link="https://example.com"))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("web_link", r.stderr)
        self.assertIn("web_link (message)", json.loads(r.stdout)["ignored_keys"])

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
