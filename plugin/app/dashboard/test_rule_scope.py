"""The auto-trash guard: can it ever say yes, and can it say yes about a real mailbox? (F22, F28)

Two reported defects that turned out to be one problem, and the second is the larger half.

F22 - A read-only intake permanently closes the guard. The honest verdict of a read-only
      pass ("this is bot noise, I would bin it, I have no power to") was not expressible, so
      `kept` got written instead - and `kept` in this tool means the routine DECIDED to keep
      it, a positive judgment about the sender. The "not pure noise" rule then refused every
      sender in the store, forever, with sound-looking reasons. A connector install could
      never escape it, having no fetcher to trash with. The read-only discipline this project
      insists on poisoned the guard this project relies on, on day one.

      `REFUSED 6 of 6` with good reasons is indistinguishable from a healthy guard.

F28 - Rules were keyed on SENDER, and mail does not arrive that way. With F22's data problem
      corrected, the number of senders eligible for a rule on a real work store was still
      ZERO of sixty-six. The high-volume senders are notification services that multiplex
      many message kinds through one address: the volume that makes a sender worth ruling on
      is the volume that guarantees the sender is mixed. `rule_min_messages` seals it - below
      the threshold there is not enough evidence, above it the sender is mixed.

      The guard was RIGHT to refuse. "this sender is pure noise" is a false statement about
      such an address, and no fix should ever make it pass. The engine was correct and
      useless, because the only thing it could express was untrue of everything worth saying
      it about.

The tests below are built on the shape the report measured: one notification address carrying
mostly status noise and a handful of messages where a person named the owner.

    python dashboard/test_rule_scope.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import server                                                      # noqa: E402

RULES_TEMPLATE = """# Rules

## Confirmed junk senders

| Sender | Confirmed | Why |
|---|---|---|
| existing@example.test | 2026-01-01 | pre-existing row that must survive |

## Something else
"""


class Install:
    """A whole install in a temp directory: its own store, protected list and rules file.

    Controlling the install directory rather than reaching for a global "ignore config"
    switch - see test_config_isolation.py for why that distinction earned its own suite.
    """

    def __init__(self, protected_names=("a real person",), concepts=("family",),
                 min_messages=8):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        db.init_db(db.connect(self.db))
        self.protected = os.path.join(self.dir, "protected.local.json")
        with open(self.protected, "w", encoding="utf-8") as f:
            json.dump({"protected_names": list(protected_names),
                       "protected_concepts": list(concepts),
                       "rule_min_messages": min_messages}, f)
        self.rules = os.path.join(self.dir, "rules-and-policies.md")
        with open(self.rules, "w", encoding="utf-8", newline="\n") as f:
            f.write(RULES_TEMPLATE)

    def __enter__(self):
        self._p, self._r = server.PROTECTED_FILE, server.RULES_FILE
        server.PROTECTED_FILE, server.RULES_FILE = self.protected, self.rules
        return self

    def __exit__(self, *exc):
        server.PROTECTED_FILE, server.RULES_FILE = self._p, self._r

    def conn(self):
        return db.connect(self.db)

    def write(self, sender, category, n, disposition="trashed", importance=None,
              concept="operations & company", day="2026-08-01"):
        c = self.conn()
        c.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES (?, 'x')", (day,))
        rid = c.execute("SELECT id FROM runs WHERE run_date = ?", (day,)).fetchone()[0]
        for i in range(n):
            c.execute(
                "INSERT INTO messages (run_id, run_date, account, sender, subject, "
                "disposition, category, concept, importance) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, day, "owner@example.test", sender, "%s %d" % (category, i),
                 disposition, category, concept, importance))
        c.commit()

    def mixed_notifier(self, sender="notifications@tracker.test"):
        """The reported shape: one address, mostly noise, with real signal inside it."""
        self.write(sender, "ci-statistics", 27, "trashed")
        self.write(sender, "bot-pr", 21, "trashed")
        self.write(sender, "tracker-mention", 11, "kept",
                   importance="action-needed", concept="family")
        return sender

    def rules_text(self):
        with open(self.rules, encoding="utf-8") as f:
            return f.read()


class TheGuardSaysWhenItCannotEverPass(unittest.TestCase):
    """F22, the reporting half: "the answer is always no" and "the answer happens to be no"
    were rendered identically."""

    def test_a_store_with_no_disposable_mail_closes_the_guard_for_everyone(self):
        with Install() as inst:
            inst.write("noise@vendor.test", "vendor-marketing", 10, "kept")
            v = server.sender_rule_verdict(inst.conn(), "noise@vendor.test")
            self.assertFalse(v["eligible"])
            self.assertIn("not pure noise", v["why"])

    def test_would_trash_is_the_missing_value_and_it_opens_the_guard(self):
        """The proof that the RULE is sound and was being fed a history that could only say
        no: the same sender, the same count, the same everything but the word."""
        with Install() as inst:
            inst.write("noise@vendor.test", "vendor-marketing", 10, "would_trash")
            v = server.sender_rule_verdict(inst.conn(), "noise@vendor.test")
            self.assertTrue(v["eligible"], v["why"])
            self.assertEqual((v["binned"], v["kept"]), (10, 0))

    def test_would_trash_does_not_count_as_kept_anywhere_in_the_verdict(self):
        with Install() as inst:
            inst.write("noise@vendor.test", "vendor-marketing", 10, "would_trash")
            v = server.sender_rule_verdict(inst.conn(), "noise@vendor.test")
            self.assertEqual(v["kept"], 0,
                             "would_trash counted as kept is the whole defect")

    def test_a_protected_concept_still_refuses_a_would_trash_sender(self):
        """The control that matters most: the new value must open the guard, not disarm it."""
        with Install() as inst:
            inst.write("family@example.test", "family-note", 10, "would_trash",
                       concept="family")
            v = server.sender_rule_verdict(inst.conn(), "family@example.test")
            self.assertFalse(v["eligible"])
            self.assertIn("protected category", v["why"])

    def test_an_unconfigured_guard_still_refuses_everything(self):
        with Install(protected_names=()) as inst:
            inst.write("noise@vendor.test", "vendor-marketing", 10, "would_trash")
            v = server.sender_rule_verdict(inst.conn(), "noise@vendor.test")
            self.assertFalse(v["eligible"])
            self.assertFalse(v["configured"])


class WouldTrashIsNotOutstandingWork(unittest.TestCase):
    """The other side of the new value: a message the triage judged disposable must not
    seed the standing work list just because nothing removed it from the mailbox."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(self.path))
        self._real = db.DB_PATH
        db.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH = self._real

    def msg(self, disposition):
        return [{"account": "owner@example.test", "sender": "bot@vendor.test",
                 "subject": "please action this", "disposition": disposition,
                 "category": "bank-statement", "reason": "r", "importance": "action-needed",
                 "message_id": "<x@y>"}]

    def opened(self, disposition):
        _, _, stats = db.ingest_run("2026-08-01", accounts=[],
                                    messages=self.msg(disposition))
        return stats["opened"]

    def test_a_kept_attention_message_opens_an_item(self):
        """The positive control. Without it, an open-items path that opened nothing at all
        would pass the test below."""
        self.assertEqual(self.opened("kept"), 1)

    def test_a_would_trash_attention_message_does_not(self):
        self.assertEqual(self.opened("would_trash"), 0)

    def test_a_trashed_attention_message_does_not(self):
        self.assertEqual(self.opened("trashed"), 0)


class ARuleCanNameWhatItActuallyMeans(unittest.TestCase):
    """F28. The whole-sender verdict on a notification address is correct and useless."""

    def test_the_whole_sender_is_refused_and_should_be(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            v = server.sender_rule_verdict(inst.conn(), s)
            self.assertFalse(v["eligible"])
            self.assertIn("not pure noise", v["why"])

    def test_the_noise_slice_is_eligible(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            v = server.sender_rule_verdict(inst.conn(), s, "ci-statistics")
            self.assertTrue(v["eligible"], v["why"])
            self.assertEqual(v["total"], 27)
            self.assertEqual(v["sender_total"], 59, "the whole sender is still reported")

    def test_the_signal_slice_is_refused_with_a_stated_reason(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            v = server.sender_rule_verdict(inst.conn(), s, "tracker-mention")
            self.assertFalse(v["eligible"])
            self.assertIn("not pure noise", v["why"])
            self.assertIn("protected category", v["why"])

    def test_the_breakdown_shows_which_part_could_be_ruled_on(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            slices = {x["category"]: x for x in server.sender_rule_slices(inst.conn(), s)}
            self.assertTrue(slices["ci-statistics"]["eligible"])
            self.assertTrue(slices["bot-pr"]["eligible"])
            self.assertFalse(slices["tracker-mention"]["eligible"])
            self.assertEqual(slices["ci-statistics"]["n"], 27)

    def test_a_protected_sender_has_no_eligible_slice(self):
        """Narrowing must sharpen the evidence, never weaken the protection. A protected
        person does not become binnable one label at a time."""
        with Install(protected_names=("a real person",)) as inst:
            sender = "a real person <p@example.test>"
            inst.write(sender, "chit-chat", 20, "trashed")
            # The key is whatever _sender_key folds the variants onto, not the address -
            # asking for the address returned "no messages recorded", which is a PASS for
            # the wrong reason and would have hidden a real hole in the protection check.
            key = server._sender_key(sender)
            v = server.sender_rule_verdict(inst.conn(), key, "chit-chat")
            self.assertFalse(v["eligible"])
            self.assertIn("protected-sender list", v["why"])

    def test_min_messages_still_applies_to_the_slice(self):
        """Otherwise slicing becomes a way to get under the evidence threshold: any sender
        could be ruled by finding a label they used three times."""
        with Install(min_messages=8) as inst:
            inst.write("bot@vendor.test", "rare-label", 3, "trashed")
            v = server.sender_rule_verdict(inst.conn(), "bot@vendor.test", "rare-label")
            self.assertFalse(v["eligible"])
            self.assertIn("only 3 messages", v["why"])

    def test_a_slice_with_any_kept_mail_is_still_refused(self):
        with Install() as inst:
            inst.write("bot@vendor.test", "mostly-noise", 19, "trashed")
            inst.write("bot@vendor.test", "mostly-noise", 1, "kept")
            v = server.sender_rule_verdict(inst.conn(), "bot@vendor.test", "mostly-noise")
            self.assertFalse(v["eligible"])
            self.assertIn("1 of 20", v["why"])


class WritingAndLiftingAScopedRule(unittest.TestCase):

    def post(self, inst, **body):
        return server.api_sender_rule(inst.conn(), {}, body)

    def test_a_scoped_rule_names_its_scope_in_the_row_not_only_the_marker(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            r = self.post(inst, key=s, category="ci-statistics")
            self.assertTrue(r["ok"], r)
            text = inst.rules_text()
            self.assertIn("only mail labelled `ci-statistics`", text)
            self.assertIn("UNAFFECTED", text)
            self.assertIn("<!-- dashboard-rule:%s|ci-statistics -->" % s, text)

    def test_the_refusal_survives_a_crafted_request(self):
        """The entitlement is re-derived server-side; the caller's claims are not evidence."""
        with Install() as inst:
            s = inst.mixed_notifier()
            r = self.post(inst, key=s, category="tracker-mention", label="totally fine")
            self.assertFalse(r["ok"])
            self.assertIn("not eligible", r["error"])
            self.assertNotIn("tracker-mention -->", inst.rules_text())

    def test_lifting_removes_only_that_rule(self):
        with Install() as inst:
            s = inst.mixed_notifier()
            self.post(inst, key=s, category="ci-statistics")
            self.post(inst, key=s, category="bot-pr")
            self.post(inst, key=s, category="ci-statistics", on=False)
            text = inst.rules_text()
            self.assertNotIn("|ci-statistics -->", text)
            self.assertIn("|bot-pr -->", text)
            self.assertIn("existing@example.test", text, "an unrelated row was destroyed")

    def test_a_whole_sender_rule_covers_every_slice(self):
        """Otherwise the panel offers a second, redundant rule for mail already locked."""
        with Install() as inst:
            inst.write("bot@vendor.test", "noise-a", 10, "trashed")
            inst.write("bot@vendor.test", "noise-b", 10, "trashed")
            self.post(inst, key="bot@vendor.test")
            self.assertTrue(server._already_ruled("bot@vendor.test", "noise-a"))

    def test_an_unscoped_rule_still_reads_and_writes_the_old_marker(self):
        """Rules written before scoped rules existed must stay liftable. Re-keying them
        would orphan every existing rule from the button that lifts it."""
        with Install() as inst:
            inst.write("bot@vendor.test", "noise", 10, "trashed")
            self.post(inst, key="bot@vendor.test")
            self.assertIn("<!-- dashboard-rule:bot@vendor.test -->", inst.rules_text())
            self.assertTrue(server._already_ruled("bot@vendor.test"))
            r = self.post(inst, key="bot@vendor.test", on=False)
            self.assertTrue(r["ok"])
            self.assertFalse(server._already_ruled("bot@vendor.test"))


class HistoryCountsOnlyWhatItRecognises(unittest.TestCase):
    """The applier's sender history used an ELSE branch to decide what counted as kept.

    That is how would_trash became evidence the sender was worth keeping - but the branch is
    wrong for a second reason that survives the first fix: ANY value that is not recognised
    lands there too. A typo, or a disposition from a newer contract, silently becomes a
    positive judgment about the sender and refuses a rule with "not pure noise" - a
    protection asserted on the strength of a spelling mistake.

    Caught by a mutant that survived the first pass of tests for this fix: reverting the
    `elif` to `else` broke nothing, which meant nothing was checking it.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import apply_proposal                                      # noqa: PLC0415
        self.mod = apply_proposal
        self.inst = Install()

    def history_for(self, disposition):
        self.inst.write("bot@vendor.test", "noise", 3, disposition)
        return self.mod._history(self.inst.conn()).get("bot@vendor.test",
                                                       {"kept": 0, "trashed": 0})

    def test_a_recognised_keep_is_counted(self):
        """The positive control: a history function that counted nothing would pass the
        assertions below and mean nothing."""
        self.assertEqual(self.history_for("kept")["kept"], 3)

    def test_a_recognised_disposal_is_counted(self):
        self.assertEqual(self.history_for("would_trash")["trashed"], 3)

    def test_an_unknown_disposition_is_evidence_of_nothing(self):
        h = self.history_for("kept_maybe")
        self.assertEqual((h["kept"], h["trashed"]), (0, 0),
                         "an unrecognised value must not become a positive judgment about "
                         "the sender - that is a protection asserted from a typo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
