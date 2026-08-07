"""The stored `concept` column, against a map that is edited after ingest - by design.

This is the failure that the existing concept test cannot see. `test_concepts.py` resolves
live through `concept_of()` and reports ALL PASS while nearly every stored row is wrong,
because the dashboard reads the column and the test reads the function. So every assertion
here goes through the STORE, and the map is changed between writing and reading.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import concepts                                                    # noqa: E402
import db                                                          # noqa: E402


def store():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(path)
    db.init_db(conn)
    return path, db.connect(path)


def write_rows(conn, label, n=3):
    conn.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES ('2026-08-01','x')")
    rid = conn.execute("SELECT id FROM runs").fetchone()[0]
    for i in range(n):
        conn.execute(
            "INSERT INTO messages (run_id, run_date, account, sender, subject, "
            "disposition, category, concept) VALUES (?,?,?,?,?,?,?,?)",
            (rid, "2026-08-01", "owner@example.com", "sender@example.com", "sub %d" % i, "surfaced",
             label, concepts.concept_of(label)))
    conn.commit()


def stored(conn, label):
    return [r[0] for r in conn.execute(
        "SELECT concept FROM messages WHERE category = ?", (label,))]


class TeachingTheMapRepairsTheStore(unittest.TestCase):

    def setUp(self):
        self._map = dict(concepts._LABEL_TO_CONCEPT)
        self.path, self.conn = store()

    def tearDown(self):
        concepts._LABEL_TO_CONCEPT.clear()
        concepts._LABEL_TO_CONCEPT.update(self._map)

    def test_a_label_learned_after_ingest_repairs_existing_rows(self):
        """The reported case, in three steps: ingest unknown, teach the map, re-open.

        The label used to be `inner-circle-fyi`, which is a name a real owner would plausibly
        teach their own map - and the plugin's own guidance tells them to. So this test, whose
        whole subject is "teaching the map repairs the store", FAILED on any install where the
        owner had taught the map that label: the feature under test and the thing that broke
        the test were the same action. Worse, it failed in the useless direction - green on a
        bare install, red on a configured one. See F25.

        Two changes, and the second is the one that generalises: a sentinel label no map would
        ever carry, and an ASSERTED precondition, so if it ever does collide the test says
        which of its own assumptions broke instead of blaming the code.
        """
        label = "zz-f25-sentinel-label-no-real-map-teaches"
        self.assertNotIn(label, concepts._LABEL_TO_CONCEPT,
                         "this test requires a label no map on this machine has taught; "
                         "if it collides, rename it - do not weaken the control below")
        write_rows(self.conn, label)
        self.assertEqual(set(stored(self.conn, label)), {concepts.UNMAPPED},
                         "control: it must be unmapped at write time")

        concepts._LABEL_TO_CONCEPT[label] = "colleagues & direct asks"
        db.init_db(db.connect(self.path))                    # what starting the server does

        self.assertEqual(set(stored(db.connect(self.path), label)),
                         {"colleagues & direct asks"})

    def test_it_is_idempotent_and_costs_nothing_when_the_map_is_unchanged(self):
        write_rows(self.conn, "receipts")
        concepts._LABEL_TO_CONCEPT["receipts"] = "money (bills, receipts, banking)"
        db.init_db(db.connect(self.path))
        first = stored(db.connect(self.path), "receipts")
        # A second pass must change nothing AND must skip the sweep entirely.
        self.assertEqual(db._reconcile_concepts(db.connect(self.path)), 0)
        self.assertEqual(stored(db.connect(self.path), "receipts"), first)

    def test_a_label_REMOVED_from_the_map_goes_back_to_unmapped(self):
        """Drift runs both ways. A label deleted from concepts.local.json must stop
        resolving, or the store keeps asserting a mapping the owner has withdrawn."""
        concepts._LABEL_TO_CONCEPT["temporary-label"] = "operations & company"
        write_rows(self.conn, "temporary-label")
        self.assertEqual(set(stored(self.conn, "temporary-label")),
                         {"operations & company"})
        del concepts._LABEL_TO_CONCEPT["temporary-label"]
        db.init_db(db.connect(self.path))
        self.assertEqual(set(stored(db.connect(self.path), "temporary-label")),
                         {concepts.UNMAPPED})

    def test_rows_whose_label_did_not_move_are_left_alone(self):
        concepts._LABEL_TO_CONCEPT["stable-label"] = "operations & company"
        write_rows(self.conn, "stable-label")
        concepts._LABEL_TO_CONCEPT["something-else"] = "money (bills, receipts, banking)"
        db.init_db(db.connect(self.path))
        self.assertEqual(set(stored(db.connect(self.path), "stable-label")),
                         {"operations & company"})


class TheFingerprintTracksMeaning(unittest.TestCase):
    """It must fire on a changed MAPPING and not on a touched file."""

    def setUp(self):
        self._map = dict(concepts._LABEL_TO_CONCEPT)

    def tearDown(self):
        concepts._LABEL_TO_CONCEPT.clear()
        concepts._LABEL_TO_CONCEPT.update(self._map)

    def test_same_mapping_same_fingerprint(self):
        a = concepts.fingerprint()
        concepts._LABEL_TO_CONCEPT.update(dict(concepts._LABEL_TO_CONCEPT))  # no-op churn
        self.assertEqual(concepts.fingerprint(), a)

    def test_a_new_label_changes_it(self):
        a = concepts.fingerprint()
        concepts._LABEL_TO_CONCEPT["brand-new-label"] = "operations & company"
        self.assertNotEqual(concepts.fingerprint(), a)

    def test_a_label_pointing_somewhere_else_changes_it(self):
        concepts._LABEL_TO_CONCEPT["movable"] = "operations & company"
        a = concepts.fingerprint()
        concepts._LABEL_TO_CONCEPT["movable"] = "money (bills, receipts, banking)"
        self.assertNotEqual(concepts.fingerprint(), a,
                            "the same labels pointing elsewhere is exactly the drift case")



class TheRepairCountIsAClaim(unittest.TestCase):
    """How many rows it says it fixed has to be how many it fixed.

    Rows of one category can hold DIFFERENT stored concepts - written before and after a map
    edit - so the sweep sees two (category, concept) pairs for one label. An UPDATE that
    ignored the stored value would reach the same final data and report the work twice, and
    a repair that overstates itself is the same defect as a count with no reach beside it.
    """

    def setUp(self):
        self._map = dict(concepts._LABEL_TO_CONCEPT)
        self.path, self.conn = store()

    def tearDown(self):
        concepts._LABEL_TO_CONCEPT.clear()
        concepts._LABEL_TO_CONCEPT.update(self._map)

    def test_it_counts_only_the_rows_it_actually_changed(self):
        concepts._LABEL_TO_CONCEPT["mixed-label"] = "operations & company"
        write_rows(self.conn, "mixed-label", n=2)            # stored: operations & company
        db.init_db(db.connect(self.path))                    # settles the store

        # Two more rows of the same label, stored as something else entirely, as a run
        # under an older map would have left them.
        self.conn.execute("INSERT INTO messages (run_id, run_date, account, sender, "
                          "subject, disposition, category, concept) SELECT run_id, "
                          "run_date, account, sender, 'other', disposition, 'mixed-label', "
                          "'money (bills, receipts, banking)' FROM messages LIMIT 2")
        self.conn.commit()

        before = stored(self.conn, "mixed-label")
        self.assertEqual(sorted(set(before)),
                         ["money (bills, receipts, banking)", "operations & company"],
                         "control: the category must really hold two stored values")

        changed = db._reconcile_concepts(db.connect(self.path))
        after = stored(db.connect(self.path), "mixed-label")
        self.assertEqual(set(after), {"operations & company"})
        self.assertEqual(changed, 2,
                         "only the two wrong rows moved; reporting 4 would be counting "
                         "the rows that were already right")


if __name__ == "__main__":
    unittest.main(verbosity=2)
