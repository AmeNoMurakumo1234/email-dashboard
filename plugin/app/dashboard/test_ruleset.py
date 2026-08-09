"""Rules as a Pareto frontier - the three axes, and the two properties that make it worth it.

    python dashboard/test_ruleset.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ruleset as R                                                  # noqa: E402


def concept_of(sender):
    return {"the-post": "mail logistics",
            "a-credit-union": "marketing / promo"}.get(sender)


class Lanes(unittest.TestCase):
    """"Different concepts stay in their own lanes without collision.\""""

    def test_two_unrelated_concepts_never_compare(self):
        a = R.rule("a", "background", scope="concept", key="marketing / promo",
                   source="human", date="2026-08-08")
        b = R.rule("b", "surface", scope="concept", key="medical",
                   source="ai", date="2020-01-01")
        self.assertFalse(R.collides(a, b))
        self.assertFalse(R.dominates(a, b))
        self.assertEqual(len(R.analyse([a, b])["frontier"]), 2)

    def test_a_sender_collides_with_the_concept_that_contains_it(self):
        """The collision the flat model missed: a category-wide "background" ruling and
        a sender-level 'always surface' ruling are two rules about the same messages."""
        concept = R.rule("c", "background", scope="concept", key="mail logistics",
                         source="human", date="2026-08-08")
        sender = R.rule("s", "surface today's digest", scope="sender",
                        key="the-post", source="human", date="2026-06-11")
        self.assertTrue(R.collides(sender, concept, concept_of))
        self.assertFalse(R.collides(sender, concept))   # no map -> no invented containment

    def test_the_more_specific_rule_wins_when_they_collide(self):
        concept = R.rule("c", "background", scope="concept", key="mail logistics",
                         source="human", date="2026-08-08")
        sender = R.rule("s", "surface it", scope="sender", key="the-post",
                        source="human", date="2026-08-08")
        self.assertTrue(R.dominates(sender, concept, concept_of))
        self.assertFalse(R.dominates(concept, sender, concept_of))


class Axes(unittest.TestCase):
    def test_newer_beats_older(self):
        old = R.rule("o", "old", scope="sender", key="x", source="human", date="2026-01-01")
        new = R.rule("n", "new", scope="sender", key="x", source="human", date="2026-08-08")
        self.assertTrue(R.dominates(new, old))
        self.assertFalse(R.dominates(old, new))

    def test_human_beats_ai(self):
        ai = R.rule("a", "inferred", scope="sender", key="x", source="ai", date="2026-08-08")
        hu = R.rule("h", "ruled", scope="sender", key="x", source="human", date="2026-08-08")
        self.assertTrue(R.dominates(hu, ai))
        self.assertFalse(R.dominates(ai, hu))

    def test_an_old_human_rule_and_a_new_ai_rule_do_NOT_resolve(self):
        """The case the whole model exists for. The human wins provenance, the machine wins
        recency, neither dominates - so both stay on the frontier and a person is asked.
        Silently picking either one is the failure: overruling a person with an inference, or
        ignoring something learned since."""
        hu = R.rule("h", "human said keep", scope="sender", key="x",
                    source="human", date="2026-01-01")
        ai = R.rule("a", "agent inferred bin", scope="sender", key="x",
                    source="ai", date="2026-08-08")
        self.assertFalse(R.dominates(hu, ai))
        self.assertFalse(R.dominates(ai, hu))
        res = R.analyse([hu, ai])
        self.assertEqual(len(res["frontier"]), 2)
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertIn("NEEDS YOU", R.explain(res)[0])


class NothingIsDeleted(unittest.TestCase):
    def test_a_dominated_rule_is_kept_with_a_pointer_to_its_successor(self):
        old = R.rule("o", "old ruling", scope="sender", key="x",
                     source="human", date="2026-01-01")
        new = R.rule("n", "new ruling", scope="sender", key="x",
                     source="human", date="2026-08-08")
        res = R.analyse([old, new])
        self.assertEqual([r["id"] for r in res["frontier"]], ["n"])
        self.assertEqual(res["dominated"][0][0]["id"], "o")
        self.assertEqual(res["dominated"][0][1][0]["id"], "n")

    def test_DELETING_THE_NEW_RULE_REVIVES_THE_OLD_ONE(self):
        """The owner's point, and it costs nothing because dominated rules were never thrown
        away: retract the newer ruling and the previous one governs again, by itself. No undo
        log, no remembering what it used to say."""
        old = R.rule("o", "old ruling", scope="sender", key="x",
                     source="human", date="2026-01-01")
        new = R.rule("n", "new ruling", scope="sender", key="x",
                     source="human", date="2026-08-08")
        self.assertEqual([r["id"] for r in R.analyse([old, new])["frontier"]], ["n"])
        self.assertEqual([r["id"] for r in R.analyse([old])["frontier"]], ["o"])

    def test_revival_works_through_a_chain(self):
        r1 = R.rule("1", "first", scope="sender", key="x", source="human", date="2026-01-01")
        r2 = R.rule("2", "second", scope="sender", key="x", source="human", date="2026-04-01")
        r3 = R.rule("3", "third", scope="sender", key="x", source="human", date="2026-08-01")
        self.assertEqual([r["id"] for r in R.analyse([r1, r2, r3])["frontier"]], ["3"])
        self.assertEqual([r["id"] for r in R.analyse([r1, r2])["frontier"]], ["2"])
        self.assertEqual([r["id"] for r in R.analyse([r1])["frontier"]], ["1"])


class RealCase(unittest.TestCase):
    def test_todays_measured_contradiction(self):
        """Live on this store: 'treat mail logistics as background, never surface it' was
        answered minutes after a sender-level ruling to surface it, and an older
        hand-written rule says to surface today's digest. All three are about the same mail."""
        concept = R.rule("c", "mail logistics is background - never surface",
                         scope="concept", key="mail logistics",
                         source="human", date="2026-08-08")
        sender_new = R.rule("s2", "surface delivery mail addressed to me", scope="sender",
                            key="the-post", source="human", date="2026-08-08")
        sender_old = R.rule("s1", "surface today's delivery digest", scope="sender",
                            key="the-post", source="human", date="2026-06-11")
        res = R.analyse([concept, sender_new, sender_old], concept_of)
        ids = sorted(r["id"] for r in res["frontier"])
        # The specific, newest human ruling governs; the broad "never surface" does not get to
        # silence the post, and the June rule is superseded rather than deleted.
        self.assertEqual(ids, ["s2"])
        self.assertEqual(len(res["conflicts"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
