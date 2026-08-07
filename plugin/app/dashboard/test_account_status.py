"""account_status is a SET - one row per account per run - and the writer treated it as a log.

Reported as: one mailbox, one entry in accounts.json, and a panel reading "4/4 connected"
with four cards, each holding a fraction of the day's traffic. `record_run(append=True)`
accumulates the `runs` row properly and, fifteen lines later in the same function under the
same flag, INSERTs account_status unconditionally. The DELETE that had always held it to one
row per account lives in the NON-append branch, which append correctly skips.

Self-concealing, because every per-card number was real. Nothing looked corrupt; it looked
like a multi-mailbox install that worked. And it bit precisely the deployments that sweep
more than once a day - which is what the plugin's own guidance tells them to do.

Same class as F17: a fix applied in one place and not to the parallel structure beside it.

    python dashboard/test_account_status.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402


def fresh():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.init_db(db.connect(path))
    return path


def acct(account="owner@example.com", **kw):
    row = {"account": account, "role": "primary", "status": "CONNECTED",
           "auth": "m365_connector", "inbox_count": 900, "fetched": 1, "trashed": 0,
           "kept": 1}
    row.update(kw)
    return row


def msg(subject, account="owner@example.com", **kw):
    row = {"account": account, "sender": "a@b.example", "subject": subject,
           "disposition": "kept", "category": "receipts", "reason": "r"}
    row.update(kw)
    return row


def statuses(path, run_date="2026-08-07"):
    conn = db.connect(path)
    return list(conn.execute(
        "SELECT a.* FROM account_status a JOIN runs r ON r.id = a.run_id "
        "WHERE r.run_date = ? ORDER BY a.account, a.id", (run_date,)))


class AppendKeepsOneRowPerAccount(unittest.TestCase):
    """The reported case, driven through the public seam rather than through SQL."""

    def setUp(self):
        self.path = fresh()
        self._real = db.DB_PATH
        db.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH = self._real

    def append(self, **kw):
        a = acct(**{k: v for k, v in kw.items() if k not in ("subject",)})
        db.ingest_run("2026-08-07", accounts=[a],
                      messages=[msg(kw.get("subject", "s"))], append=True,
                      open_items=False)

    def test_four_hourly_appends_are_one_card(self):
        for i in range(4):
            self.append(subject="s%d" % i)
        rows = statuses(self.path)
        self.assertEqual(len(rows), 1,
                         "four appends produced %d cards for one mailbox" % len(rows))

    def test_counters_accumulate_across_appends(self):
        for i in range(4):
            self.append(subject="s%d" % i, fetched=1, kept=1)
        row = statuses(self.path)[0]
        self.assertEqual((row["fetched"], row["kept"]), (4, 4),
                         "the card must describe the whole day, not the last batch")

    def test_inbox_count_is_a_snapshot_and_is_not_summed(self):
        """The counter/snapshot split is the whole substance of the fix. Summing four
        readings of how big the mailbox is would report 3600 for a 900-message inbox -
        arithmetically tidy and completely false."""
        for i in range(4):
            self.append(subject="s%d" % i, inbox_count=900)
        self.assertEqual(statuses(self.path)[0]["inbox_count"], 900)

    def test_a_later_failure_overwrites_an_earlier_connected(self):
        """A stale CONNECTED surviving a FAILED is the dangerous direction: the panel would
        report a mailbox as reachable on the strength of a sweep that happened hours ago."""
        self.append(subject="s1", status="CONNECTED", error=None)
        self.append(subject="s2", status="FAILED", error="auth expired")
        row = statuses(self.path)[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["error"], "auth expired")

    def test_two_real_mailboxes_still_get_two_cards(self):
        """The control. Collapsing everything to one row would also pass every test above."""
        db.ingest_run("2026-08-07",
                      accounts=[acct("a@example.com"), acct("b@example.com")],
                      messages=[msg("s", account="a@example.com")],
                      append=True, open_items=False)
        self.assertEqual([r["account"] for r in statuses(self.path)],
                         ["a@example.com", "b@example.com"])

    def test_replace_still_replaces(self):
        """append=False must keep wiping the day, or a re-run of a corrected sweep doubles
        the counts instead of superseding them."""
        self.append(subject="s1", fetched=5, kept=5)
        db.ingest_run("2026-08-07", accounts=[acct(fetched=2, kept=2)],
                      messages=[msg("s2")], append=False, open_items=False)
        rows = statuses(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fetched"], 2, "replace must not accumulate")


class TheMigrationRepairsAnExistingStore(unittest.TestCase):
    """Stores already hold duplicates - this one did, and so did the reporter's. A fix that
    only stops NEW duplicates leaves the panel lying about every day already recorded."""

    def duplicated_store(self):
        """A store in the pre-fix state: built from SCHEMA alone, so the unique index that
        would forbid these rows does not exist yet."""
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO runs (run_date, created_at, fetched, kept) "
                     "VALUES ('2026-08-07','x',5,5)")
        rid = conn.execute("SELECT id FROM runs").fetchone()[0]
        for fetched, kept, status, inbox in ((1, 1, "CONNECTED", 260), (2, 2, "CONNECTED", 1),
                                             (1, 1, "CONNECTED", 1), (1, 1, "FAILED", 265)):
            conn.execute(
                "INSERT INTO account_status (run_id, account, role, status, auth, "
                "inbox_count, fetched, trashed, kept, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, "owner@example.com", "primary", status, "m365_connector",
                 inbox, fetched, 0, kept, None))
        conn.commit()
        conn.close()
        return path

    def test_the_four_cards_become_one(self):
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        rows = statuses(path)
        self.assertEqual(len(rows), 1)

    def test_the_survivor_matches_the_runs_row(self):
        """The check worth making after any collapse: the account card and the run row now
        agree. If the migration dropped rows instead of folding them they would not."""
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        row = statuses(path)[0]
        run = db.connect(path).execute(
            "SELECT fetched, kept FROM runs WHERE run_date = '2026-08-07'").fetchone()
        self.assertEqual((row["fetched"], row["kept"]), (5, 5))
        self.assertEqual((row["fetched"], row["kept"]), (run["fetched"], run["kept"]))

    def test_snapshot_fields_come_from_the_latest_row(self):
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        row = statuses(path)[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["inbox_count"], 265)

    def test_it_is_idempotent(self):
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        before = dict(statuses(path)[0])
        db.init_db(db.connect(path))
        self.assertEqual(dict(statuses(path)[0]), before,
                         "a second start must not re-fold the row it already folded")

    def test_the_index_exists_afterwards(self):
        """Without the constraint the repair is a one-off tidy-up that drifts again."""
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        idx = [r[0] for r in db.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='account_status'")]
        self.assertIn("idx_acct_run_account", idx)

    def test_the_constraint_actually_forbids_a_raw_duplicate(self):
        """The positive control on the index itself. An index that exists but does not
        constrain would satisfy the test above and nothing else."""
        path = self.duplicated_store()
        db.init_db(db.connect(path))
        conn = db.connect(path)
        rid = conn.execute("SELECT id FROM runs").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO account_status (run_id, account, fetched, trashed, "
                         "kept) VALUES (?,?,?,?,?)", (rid, "owner@example.com", 1, 0, 1))

    def test_a_clean_store_is_left_alone(self):
        """The other control: the migration must not report work it did not do."""
        path = fresh()
        conn = db.connect(path)
        conn.execute("INSERT INTO runs (run_date, created_at) VALUES ('2026-08-06','x')")
        rid = conn.execute("SELECT id FROM runs").fetchone()[0]
        conn.execute("INSERT INTO account_status (run_id, account, fetched, trashed, kept) "
                     "VALUES (?,?,?,?,?)", (rid, "solo@example.com", 3, 0, 3))
        conn.commit()
        self.assertEqual(db._collapse_duplicate_account_status(db.connect(path)), 0)
        self.assertEqual(len(statuses(path, "2026-08-06")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
