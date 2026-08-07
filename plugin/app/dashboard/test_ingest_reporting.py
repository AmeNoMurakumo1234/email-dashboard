"""What ingest.py TELLS a caller about what it did with their data. (F24, F27, F29)

Three reports, one shape. `ingest.py` is documented as the supported entry point from any
source, which means its callers are by definition not reading the internals - and three times
now it has accepted something and then never mentioned it again:

  F24  `inbox_count` is listed as an accepted key and never defined or checked, so a caller
       sent the sweep's own filtered result count into a field meaning "how big is the
       mailbox" and got `inbox 1 / fetched 5` for a 265-message inbox, ok:true, every count
       "correct".
  F27  `web_link` and `body_text` are accepted and appear in no report, so a caller that
       silently stops sending them sees an unchanged, entirely healthy result - while the
       sandboxed viewer, the image blocking and the tracking-host report become unreachable
       for every row it wrote. On installs checked before the fix, the great majority of
       rows carried neither - on one of them, every single row.
  F29  `--by-arrival` hard-codes `accounts=[]`, and because `accounts` is a RECOGNISED key
       the discard sails past the unrecognised-key report. The run prints `ignored 0
       unrecognised keys` while having ignored something.

Every test here goes through the COMMAND LINE rather than through main()'s internals, because
the contract under test is the one a caller sees: exit code, stderr, and the JSON on stdout.

    python dashboard/test_ingest_reporting.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

INGEST = os.path.join(HERE, "ingest.py")


def message(subject="s", **kw):
    m = {"account": "owner@example.com", "sender": "a@b.example", "subject": subject,
         "msg_date": "2026-03-04T10:00:00+00:00", "disposition": "kept",
         # A SHIPPED label, not a plausible-sounding one. The first draft used "receipts",
         # which resolves on machines whose local map teaches it and UNMAPPED everywhere
         # else - so the --strict test passed or failed depending on the machine. That is
         # F25 again, in a fixture written the same afternoon F25 was fixed.
         "category": "bank-statement", "reason": "r", "message_id": "<%s@x>" % subject}
    m.update(kw)
    return m


def account(**kw):
    a = {"account": "owner@example.com", "role": "primary", "status": "CONNECTED",
         "auth": "m365_connector", "inbox_count": 900, "fetched": 1, "trashed": 0, "kept": 1}
    a.update(kw)
    return a


class Ingested:
    """One CLI run against a private store, with its three channels kept separate."""

    def __init__(self, payload, *flags):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "run.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_DB=os.path.join(d, "t.db"))
        p = subprocess.run([sys.executable, INGEST, "--file", path] + list(flags),
                           cwd=HERE, capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace")
        self.rc, self.err, self.out = p.returncode, p.stderr, p.stdout
        try:
            self.json = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.json = None


class ImpossibleAccountCounts(unittest.TestCase):
    """F24. Not a difference of opinion - a mailbox cannot hold fewer messages than one
    sweep pulled out of it."""

    def payload(self, **acct):
        return {"run_date": "2026-08-07", "accounts": [account(**acct)],
                "messages": [message("a"), message("b")]}

    def test_inbox_count_below_fetched_warns(self):
        r = Ingested(self.payload(inbox_count=1, fetched=5))
        self.assertIn("IMPOSSIBLE", r.err)
        self.assertIn("SIZE OF THE MAILBOX", r.err)
        self.assertEqual(r.rc, 0, "a warning must not become a refusal without --strict")
        self.assertTrue(r.json["ok"])
        self.assertTrue(r.json["impossible_accounts"])

    def test_inbox_count_below_fetched_refuses_under_strict(self):
        r = Ingested(self.payload(inbox_count=1, fetched=5), "--strict")
        self.assertEqual(r.rc, 2)
        self.assertIn("impossible account counts", r.err)

    def test_trashed_plus_kept_over_fetched(self):
        r = Ingested(self.payload(fetched=2, trashed=2, kept=3))
        self.assertIn("trashed 2 + kept 3 > fetched 2", r.err)

    def test_a_null_inbox_count_is_accepted_in_silence(self):
        """The contract says an absent number is honest. It must not be nagged at, or
        callers learn to send a wrong one to quiet the tool."""
        r = Ingested(self.payload(inbox_count=None, fetched=5, kept=1))
        self.assertNotIn("IMPOSSIBLE", r.err)
        self.assertEqual(r.json["impossible_accounts"], [])

    def test_a_correct_row_says_nothing(self):
        """The control. Warning on everything would also 'catch' the reported case."""
        r = Ingested(self.payload(inbox_count=900, fetched=5, trashed=2, kept=3))
        self.assertNotIn("IMPOSSIBLE", r.err)
        self.assertEqual(r.rc, 0)

    def test_strict_still_passes_a_correct_run(self):
        r = Ingested(self.payload(inbox_count=900, fetched=2, trashed=0, kept=2), "--strict")
        self.assertEqual(r.rc, 0, r.err[-2000:])


class ViewerCoverageIsReported(unittest.TestCase):
    """F27. `linked` says a row can be re-FOUND; these say it can be READ."""

    def run_with(self, msgs):
        return Ingested({"run_date": "2026-08-07", "accounts": [account(fetched=len(msgs),
                                                                       kept=len(msgs))],
                         "messages": msgs})

    def test_neither_column_sent_is_visible_in_the_report(self):
        r = self.run_with([message("a"), message("b")])
        self.assertEqual((r.json["with_body"], r.json["with_link"]), (0, 0))
        self.assertIn("viewer  0/2", r.err)
        self.assertIn("cannot be read in the sandboxed viewer", r.err)

    def test_full_coverage_reports_full_and_does_not_warn(self):
        msgs = [message("a", body_text="hello", web_link="https://example.test/a"),
                message("b", body_text="hi", web_link="https://example.test/b")]
        r = self.run_with(msgs)
        self.assertEqual((r.json["with_body"], r.json["with_link"]), (2, 2))
        self.assertNotIn("cannot be read in the sandboxed viewer", r.err)

    def test_partial_coverage_is_distinguishable_from_none(self):
        """The reported failure was that "I sent nothing" and "I sent everything" produced
        the same report. Half must look like neither."""
        r = self.run_with([message("a", body_text="hello"), message("b")])
        self.assertEqual(r.json["with_body"], 1)
        self.assertIn("viewer  1/2", r.err)

    def test_an_empty_string_is_not_coverage(self):
        r = self.run_with([message("a", body_text="", web_link="   ")])
        self.assertEqual((r.json["with_body"], r.json["with_link"]), (0, 0))

    def test_the_columns_are_documented_as_accepted_keys(self):
        """They were accepted by db.MESSAGE_FIELDS and missing from the ACCEPTED KEYS block
        a caller actually reads - so sending them looked unsupported."""
        import ingest                                              # noqa: PLC0415
        doc = ingest.__doc__
        for key in ("body_text", "web_link", "inbox_count"):
            self.assertIn(key, doc)
        self.assertIn("not this sweep's result count", doc.lower())


class ByArrivalSaysWhatItDiscarded(unittest.TestCase):
    """F29. The discard is defensible; the silence is not."""

    def payload(self, with_accounts=True):
        p = {"run_date": "2026-08-07", "notes": "by-arrival probe",
             "messages": [message("a", msg_date="2026-03-04T10:00:00+00:00"),
                          message("b", msg_date="2026-03-05T10:00:00+00:00")]}
        if with_accounts:
            p["accounts"] = [account(fetched=2, kept=2)]
        return p

    def test_the_discard_is_reported_on_stderr(self):
        r = Ingested(self.payload(), "--by-arrival")
        self.assertIn("does not record account status", r.err)
        self.assertIn("1 account block(s) discarded", r.err)

    def test_the_discard_is_in_the_result_so_it_need_not_be_scraped(self):
        r = Ingested(self.payload(), "--by-arrival")
        self.assertEqual(r.json["discarded"], {"accounts": 1})

    def test_it_still_discards_them(self):
        """The behaviour is unchanged and deliberately so: asserting CONNECTED for a day on
        which nothing connected would be a lie about a sweep that never happened."""
        r = Ingested(self.payload(), "--by-arrival")
        self.assertEqual(r.json["runs_touched"], 2)
        self.assertEqual(r.json["written"], 2)

    def test_sending_no_accounts_says_nothing(self):
        """The control: a caller that sent nothing must not be told something was dropped."""
        r = Ingested(self.payload(with_accounts=False), "--by-arrival")
        self.assertNotIn("discarded", r.err)
        self.assertEqual(r.json["discarded"], {})

    def test_the_ordinary_path_does_not_claim_a_discard(self):
        r = Ingested(self.payload())
        self.assertEqual(r.json["discarded"], {})
        self.assertEqual(r.json["accounts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
