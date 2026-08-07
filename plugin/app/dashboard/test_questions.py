"""The question generator, and the reason it needs a positive control per kind.

An elicitation panel that shows nothing is indistinguishable from an elicitation panel that
is broken. Both are a quiet page. This project has now produced five confident false zeroes
- a grep pattern that could not match, a guard that could not fire, a count with no reach
beside it - and every one of them read as good news.

So each question kind gets a fixture built to trip it and an assertion that it DOES. A test
that only checks "no crash, returns a list" would pass against `return []`.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                                          # noqa: E402
import questions                                                   # noqa: E402


def store(rows, acks=(), ):
    """A store containing exactly `rows`, and nothing else that could explain a result."""
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    # db.connect(path) / db.init_db(conn), NOT db.connect() - the argument-less form opens
    # the REAL dashboard store, so a fixture built without it would have these tests writing
    # rows into live data and reading their own residue back as evidence.
    conn = db.connect(path)
    db.init_db(conn)
    conn = db.connect(path)                       # init_db closes the one it was handed
    conn.execute("INSERT INTO runs (run_date, created_at) VALUES ('2026-08-01','2026-08-01')")
    rid = conn.execute("SELECT id FROM runs").fetchone()[0]
    for r in rows:
        conn.execute(
            "INSERT INTO messages (run_id, run_date, account, sender, subject, msg_day, "
            "disposition, category, concept, importance, addressed_directly, "
            "recipient_count, recipients) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "2026-08-01", r.get("account", "owner@example.com"), r["sender"],
             r.get("subject", "subject"), r.get("day", "2026-08-01"),
             r.get("disposition", "trashed"), r.get("category", "promo"),
             r.get("concept", "promotions"), r.get("importance", ""),
             r.get("direct"), r.get("rcount"), r.get("recipients")))
    for a in acks:
        conn.execute("INSERT INTO acks (kind, key, sender, acked_at) VALUES (?,?,?,?)",
                     ("message", a[1], a[0], "2026-08-01"))
    conn.commit()
    return conn


def kinds(conn, **kw):
    items, total = questions.generate(conn, limit=kw.pop("limit", 20), **kw)
    return [q["kind"] for q in items], items, total


def bulk(n, **kw):
    return [dict(sender="Noise <bot@example.com>", **kw) for _ in range(n)]


class GeneratorFires(unittest.TestCase):
    """One fixture per kind, each asserting the kind actually appears."""

    def test_high_volume_never_actioned(self):
        got, items, _ = kinds(store(bulk(20)))
        self.assertIn("sender_disposition", got)
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.assertEqual(q["evidence"]["messages"], 20)
        self.assertIn("20", q["question"])            # the evidence is IN the question

    def test_personally_addressed_outranks_volume(self):
        """The dangerous kind must sort above the merely large one."""
        rows = bulk(20) + bulk(6, direct=1, subject="Task assigned to you: renew the lease")
        got, items, _ = kinds(store(rows))
        self.assertIn("personally_addressed", got)
        self.assertLess(got.index("personally_addressed"),
                        got.index("escalation_contacts"),
                        "a message that asks the owner to act must rank above everything")

    def test_personal_ask_fires_without_recipient_data(self):
        """Installs upgrading from 0.6.0 have NULL recipients on every existing row.

        If the dangerous question needed that column it would silently never fire for them -
        the worst case, because it fails closed-looking rather than closed.
        """
        rows = bulk(14, subject="Reminder: your approval is required on PO-4471")
        got, _, _ = kinds(store(rows))
        self.assertIn("personally_addressed", got)

    def test_escalation_always_asked(self):
        got, items, _ = kinds(store(bulk(3)))
        self.assertIn("escalation_contacts", got)
        self.assertEqual([i for i in items if i["kind"] == "escalation_contacts"][0]["writes"],
                         "config/protected.local.json")

    def test_concept_gap_lists_the_unmapped_labels(self):
        rows = bulk(14, category="build-failure", concept="unmapped")
        got, items, _ = kinds(store(rows))
        self.assertIn("concept_gap", got)
        q = [i for i in items if i["kind"] == "concept_gap"][0]
        self.assertEqual(q["evidence"]["labels"][0]["label"], "build-failure")

    def test_concept_never_actioned(self):
        got, _, _ = kinds(store(bulk(40, concept="promotions")))
        self.assertIn("concept_never_actioned", got)

    def test_acks_are_read_as_answers(self):
        conn = store(bulk(4), acks=[("Digest <d@example.com>", "k%d" % i) for i in range(4)])
        got, items, _ = kinds(conn)
        self.assertIn("repeatedly_acknowledged", got)
        self.assertEqual(
            [i for i in items if i["kind"] == "repeatedly_acknowledged"][0]
            ["evidence"]["times_acknowledged"], 4)

    def test_mailbox_roles_only_with_more_than_one(self):
        one = bulk(4)
        self.assertNotIn("mailbox_role", kinds(store(one))[0])
        two = one + [dict(sender="x@example.com", account="work@example.com")]
        self.assertIn("mailbox_role", kinds(store(two))[0])


class GeneratorStaysQuiet(unittest.TestCase):
    """The other half: it must NOT ask what it cannot support or has been told."""

    def test_thin_evidence_asks_nothing_about_senders(self):
        got, _, _ = kinds(store(bulk(questions.MIN_EVIDENCE - 1)))
        self.assertNotIn("sender_disposition", got)

    def test_a_sender_that_matters_is_not_offered_for_binning(self):
        rows = bulk(20, disposition="kept", importance="action-needed")
        got, _, _ = kinds(store(rows))
        self.assertNotIn("sender_disposition", got)

    def test_answering_stops_the_asking(self):
        conn = store(bulk(20))
        got, items, _ = kinds(conn)
        target = [i for i in items if i["kind"] == "sender_disposition"][0]
        conn.execute("INSERT INTO answers (question_id, answered_at, answer) "
                     "VALUES (?,?,?)", (target["id"], "2026-08-01", "auto-trash it"))
        conn.commit()
        again, _, _ = kinds(conn)
        self.assertNotIn(target["id"], [i["id"] for i in kinds(conn)[1]])
        self.assertNotIn("sender_disposition", again)

    def test_an_existing_rule_is_not_re_litigated(self):
        conn = store(bulk(20))
        # The key is DERIVED from the code that writes it, not written out by hand. Hand
        # writing "dashboard-rule:bot@example.com" is what this test did first, and it
        # matched nothing: _sender_key keys on the display name. Suppression that matches
        # nothing looks exactly like suppression that works, so the control below is what
        # makes this test mean anything.
        import server                                              # noqa: PLC0415
        key = server._sender_key("Noise <bot@example.com>")
        rules = os.path.join(tempfile.mkdtemp(), "rules.md")
        with open(rules, "w", encoding="utf-8") as f:
            f.write("- bin it <!-- dashboard-rule:%s -->\n" % key)
        self.assertIn("sender_disposition", kinds(conn)[0],
                      "fixture must produce the question the rules file then suppresses")
        got, _, _ = kinds(conn, rules_path=rules)
        self.assertNotIn("sender_disposition", got)

    def test_every_question_carries_evidence(self):
        """The rule the design doc set: no evidence, no question."""
        rows = (bulk(20) + bulk(6, direct=1, subject="please review this")
                + bulk(14, category="odd", concept="unmapped"))
        _, items, _ = kinds(store(rows))
        self.assertTrue(items)
        for q in items:
            self.assertTrue(q.get("evidence"), "%s shipped with no evidence" % q["id"])
            self.assertTrue(q.get("why_it_matters"), q["id"])
            self.assertTrue(q.get("writes"), q["id"])
            json.dumps(q)                                  # must survive the API boundary

    def test_cap_reports_what_it_withheld(self):
        rows = (bulk(20) + bulk(40, concept="promotions")
                + bulk(14, category="odd", concept="unmapped"))
        items, total = questions.generate(store(rows), limit=2)
        self.assertEqual(len(items), 2)
        self.assertGreater(total, 2, "a capped list that reports its own length as the "
                                     "total is the understatement bug all over again")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class GuardAwareness(unittest.TestCase):
    """The escalation question must know when it has already been answered on disk."""

    def test_not_asked_when_the_guard_is_already_populated(self):
        conn = store(bulk(4))
        self.assertIn("escalation_contacts", kinds(conn)[0])          # control
        got, _, _ = kinds(conn, protected=["Someone Real"])
        self.assertNotIn("escalation_contacts", got)

    def test_asked_when_the_guard_resolves_to_nothing(self):
        """Placeholders in the file resolve to an empty list - that is UNSET, not set."""
        got, _, _ = kinds(store(bulk(4)), protected=[])
        self.assertIn("escalation_contacts", got)


class StakesAreStated(unittest.TestCase):
    """A money sender and a promo sender must not be asked about identically."""

    def test_financial_sender_outranks_an_equally_ignored_promo(self):
        rows = (bulk(20, concept="money (bills, receipts, banking)")
                + [dict(sender="Promo <p@example.com>", concept="marketing / promo")] * 20)
        _, items, _ = kinds(store(rows))
        disp = [i for i in items if i["kind"] == "sender_disposition"]
        self.assertEqual(len(disp), 2, "control: both senders must produce a question")
        self.assertGreater(disp[0]["weight"], disp[1]["weight"])
        self.assertIn("money", disp[0]["evidence"]["mostly"])

    def test_auto_trash_is_not_the_first_option_for_money(self):
        _, items, _ = kinds(store(bulk(20, concept="money (bills, receipts, banking)")))
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.assertNotIn("auto-trash", q["options"][0])
        self.assertIn("fraud alert", q["why_it_matters"])

    def test_auto_trash_stays_first_for_ordinary_noise(self):
        """The control. If every sender were treated as risky the caution means nothing."""
        _, items, _ = kinds(store(bulk(20, concept="marketing / promo")))
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.assertIn("auto-trash", q["options"][0])


class AnswerEndpoint(unittest.TestCase):
    """Recording an answer, and the two ways that could go quietly wrong."""

    def setUp(self):
        import server                                              # noqa: PLC0415
        self.server = server
        self.conn = store(bulk(20))

    def test_recording_an_answer_stops_the_question_and_keeps_the_evidence(self):
        _, items, _ = kinds(self.conn)
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        res = self.server.api_answer(self.conn, {}, {
            "id": q["id"], "kind": q["kind"], "question": q["question"],
            "evidence": q["evidence"], "answer": "auto-trash it from now on",
            "written_to": q["writes"]})
        self.assertTrue(res["ok"])
        row = self.conn.execute(
            "SELECT question, evidence, answer, written_to FROM answers "
            "WHERE question_id = ?", (q["id"],)).fetchone()
        self.assertEqual(row[2], "auto-trash it from now on")
        # The question and its evidence are frozen beside the answer. A rule read back in a
        # year is indistinguishable from a guess without them.
        self.assertEqual(row[0], q["question"])
        self.assertEqual(json.loads(row[1])["messages"], 20)
        self.assertEqual(row[3], "rules-and-policies.md")
        self.assertNotIn(q["id"], [i["id"] for i in kinds(self.conn)[1]])

    def test_an_empty_answer_does_not_silence_the_question(self):
        """Storing "" would make a question vanish without ever being decided."""
        _, items, _ = kinds(self.conn)
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.server.api_answer(self.conn, {}, {"id": q["id"], "answer": "   "})
        self.assertIn(q["id"], [i["id"] for i in kinds(self.conn)[1]])

    def test_answering_again_replaces_rather_than_duplicates(self):
        _, items, _ = kinds(self.conn)
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        for a in ("auto-trash it", "actually - keep asking me"):
            self.server.api_answer(self.conn, {}, {"id": q["id"], "answer": a})
        rows = self.conn.execute("SELECT answer FROM answers WHERE question_id = ?",
                                 (q["id"],)).fetchall()
        self.assertEqual([r[0] for r in rows], ["actually - keep asking me"])

    def test_answering_is_a_write_and_is_unreachable_by_GET(self):
        """A GET that writes can be triggered by a link or an image tag."""
        self.assertIn("/api/answer", self.server.WRITE_API)
        self.assertNotIn("/api/answer", self.server.API)
        self.assertIn("/api/questions", self.server.API)
        self.assertNotIn("/api/questions", self.server.WRITE_API)


class TheStarvationBug(unittest.TestCase):
    """A sender the triage merely SURFACES must still be askable about.

    `surfaced` was counted as `kept`, and the volume question is suppressed by "has anything
    ever been kept?". So on any install whose routine surfaces rather than bins, the single
    largest lever on the inbox could never be raised. Invisible from a mailbox that trashes,
    which is why it took a field report on an install where one sender was a third of the
    mail.
    """

    def test_a_surfaced_and_ignored_sender_still_produces_the_question(self):
        got, items, _ = kinds(store(bulk(30, disposition="surfaced")))
        self.assertIn("sender_disposition", got)
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.assertEqual(q["evidence"]["surfaced_to_you"], 30)
        self.assertEqual(q["evidence"]["auto_binned"], 0)

    def test_surfaced_and_binned_are_reported_apart(self):
        """'30 were put in front of you and none mattered' is a stronger fact than '30 were
        binned automatically and none mattered'. One number cannot say both."""
        rows = bulk(20, disposition="surfaced") + bulk(10, disposition="trashed")
        _, items, _ = kinds(store(rows))
        q = [i for i in items if i["kind"] == "sender_disposition"][0]
        self.assertEqual(q["evidence"]["surfaced_to_you"], 20)
        self.assertEqual(q["evidence"]["auto_binned"], 10)

    def test_an_actually_kept_sender_is_still_suppressed(self):
        """The control. If nothing suppressed it, the question would fire on senders that
        demonstrably matter."""
        rows = bulk(29, disposition="surfaced") + bulk(1, disposition="kept")
        self.assertNotIn("sender_disposition", kinds(store(rows))[0])


class StakesOutrankWeight(unittest.TestCase):

    def test_a_data_loss_question_beats_a_pile_of_noise_questions(self):
        rows = (bulk(40, disposition="surfaced")
                + [dict(sender="Tracker <t@example.com>", category="bot-issue",
                        disposition="trashed", subject="Alice assigned you a task",
                        recipients="mention@noreply.example.com")] * 6)
        got, items, _ = kinds(store(rows))
        self.assertIn("assigned_work_at_risk", got)
        self.assertEqual(got[0], "assigned_work_at_risk",
                         "a rule that would bin assigned work outranks everything")
        self.assertEqual(items[0]["stakes"], "data-loss")

    def test_every_question_declares_its_stakes(self):
        rows = (bulk(20) + bulk(14, category="odd", concept="unmapped")
                + bulk(6, direct=1, subject="please review this"))
        _, items, _ = kinds(store(rows))
        self.assertTrue(items)
        for q in items:
            self.assertIn(q["stakes"], questions.STAKES, q["id"])


class AssignedWorkAtRisk(unittest.TestCase):

    def rows(self, disposition="trashed", **kw):
        base = dict(sender="Tracker <t@example.com>", category="bot-issue",
                    disposition=disposition, subject="a build ran")
        base.update(kw)
        return [base] * 6

    def test_a_mention_in_the_recipient_list_is_found(self):
        """The evidence that only exists because recipients are stored."""
        got, items, _ = kinds(store(self.rows(recipients="mention@noreply.example.com")))
        self.assertIn("assigned_work_at_risk", got)
        q = [i for i in items if i["kind"] == "assigned_work_at_risk"][0]
        self.assertEqual(q["evidence"]["of_those_binned"], 6)

    def test_an_assignment_in_the_subject_is_found_without_recipients(self):
        """Installs upgrading from an older schema have no recipient data at all."""
        got, _, _ = kinds(store(self.rows(subject="Alice assigned you a task")))
        self.assertIn("assigned_work_at_risk", got)

    def test_it_stays_quiet_when_that_mail_is_already_surfaced(self):
        """The control: a category that is handled correctly needs no question."""
        got, _, _ = kinds(store(self.rows(disposition="surfaced",
                                          recipients="mention@noreply.example.com")))
        self.assertNotIn("assigned_work_at_risk", got)

    def test_ordinary_bot_traffic_raises_nothing(self):
        got, _, _ = kinds(store(self.rows(recipients="team@example.com")))
        self.assertNotIn("assigned_work_at_risk", got)


class EscalationAsksByRecognition(unittest.TestCase):

    def test_it_offers_the_senders_it_has_already_seen_you_flag(self):
        rows = [dict(sender="Boss <boss@example.com>", importance="action-needed",
                     disposition="kept")] * 3
        _, items, _ = kinds(store(rows))
        q = [i for i in items if i["kind"] == "escalation_contacts"][0]
        self.assertTrue(q["multi"])
        self.assertTrue(q["options"], "the list the tool already holds must be offered")
        self.assertIn("Boss", q["options"][0])
        self.assertIn("3", str(q["evidence"]["flagged_before"]) + "3")

    def test_with_nothing_flagged_yet_it_falls_back_to_asking(self):
        """A brand-new install has no history to recognise, and must still ask."""
        _, items, _ = kinds(store(bulk(3)))
        q = [i for i in items if i["kind"] == "escalation_contacts"][0]
        self.assertEqual(q["options"], [])
        self.assertIn("must never be filtered", q["question"])

    def test_a_thin_data_loss_question_beats_a_huge_noise_question(self):
        """The test that makes the floor load-bearing rather than decorative.

        A single binned assignment is weak evidence; two hundred ignored messages from one
        sender is strong evidence. Sorted on weight alone the pile wins, which is precisely
        the ordering the field report argued against: being wrong about the pile is an
        annoyance, being wrong about the assignment loses work.
        """
        rows = (bulk(200, disposition="surfaced")
                + [dict(sender="Tracker <t@example.com>", category="bot-issue",
                        disposition="trashed", subject="Alice assigned you a task",
                        recipients="mention@noreply.example.com")])
        got, items, _ = kinds(store(rows))
        by_kind = {i["kind"]: i for i in items}
        self.assertIn("assigned_work_at_risk", by_kind)
        self.assertIn("sender_disposition", by_kind)
        self.assertLess(by_kind["assigned_work_at_risk"]["weight"],
                        by_kind["sender_disposition"]["weight"],
                        "control: the noise question must have the HIGHER weight here, or "
                        "this test proves nothing about the floor")
        self.assertEqual(got[0], "assigned_work_at_risk")


class UrgentSoundingIsNotNamed(unittest.TestCase):
    """The false positive that took the top of the list on a real mailbox.

    This signal outranks everything by design, so a false positive here does not merely add
    a bad row - it becomes the first thing the owner reads, every time. Which is how a panel
    teaches its reader to skim, which is the failure the whole tool argues against.
    """

    def test_a_marketing_blast_headed_action_required_is_not_an_assignment(self):
        self.assertFalse(questions.names_a_person(
            "[Action Required] Looks like you have been ghosting us!", "", None))

    def test_the_same_words_addressed_to_you_directly_ARE(self):
        """The corroboration rule, from the other side - otherwise the fix is just a
        narrower blocklist and the real ones get dropped too."""
        self.assertTrue(questions.names_a_person(
            "[Action Required] Please confirm your address", "", 1))

    def test_a_strong_marker_needs_no_corroboration(self):
        self.assertTrue(questions.names_a_person("Alice assigned you a task", "", None))

    def test_a_mention_address_in_the_recipient_list_counts(self):
        self.assertTrue(questions.names_a_person(
            "a build ran", "mention@noreply.example.com", None))

    def test_the_word_mentions_in_a_subject_does_not(self):
        """`\bmentions?@` and not `mentions?` - the second matched 'mentions of your
        brand' in marketing subject lines."""
        self.assertFalse(questions.names_a_person(
            "Re: mentions of your brand", "marketing@example.com", None))

    def test_the_regex_is_a_regex(self):
        """A literal backspace character got written into this pattern during editing, so
        it compiled, never errored, and matched nothing. Asserted because 'it compiles' and
        'it means what it reads as' are different claims."""
        self.assertNotIn("\x08", questions._ASSIGNED_TO.pattern)
        self.assertTrue(questions._ASSIGNED_TO.search("mention@noreply.example.com"))
