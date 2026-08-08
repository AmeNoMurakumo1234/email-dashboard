"""Escalate on anomaly, never on occurrence - and prove every branch can fire. (rule 26)

An alert that fires on every login is not an alert, it is a log. The failure this guards
against is not a wrong answer; it is a correct, boring one repeated until the reader stops
opening it, so that the alert that matters arrives into a habit of not looking.

That makes the shape of this suite unusual, and deliberate:

  * The SILENCES are the dangerous output. "No anomalies" is a claim, and it is only worth
    believing if the branches that produce anomalies can be shown to fire. Every escalation
    below has a positive control.
  * The BURST is the case with no individual evidence at all. Three services in one window is
    the shape of someone working a credential list, and every message in it looks
    unremarkable on its own - so it exists only across messages, and a tool that judges one
    message at a time cannot see it by construction. It gets the most attention here.
  * The PARSER must under-claim. A device signature we merely suspect fabricates novelty in
    the one panel whose whole value is being believed when it fires. An early version pulled
    "required" out of "[ACTION REQUIRED]" and reported it as a device never seen before.

    python dashboard/test_signin.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signin                                                      # noqa: E402


def msg(subject, sender="Example <no-reply@example.test>", day="2026-08-01", time=None):
    return {"subject": subject, "sender": sender, "msg_day": day,
            "msg_date": time or (day + "T09:00:00+00:00")}


def led(rows, **kw):
    return signin.ledger(rows, **kw)


class AChangeIsNotALogin(unittest.TestCase):
    """Password, 2FA, recovery address, tokens, keys. These are the STEPS of an account
    takeover, and they differ from a sign-in in kind: a sign-in happens constantly and
    legibly, a recovery-address change happens twice a decade and locks you out."""

    CHANGES = [
        "Your password was changed",
        "You have a new password for Payrollco",
        "[CodeHost] Please download your two-factor recovery codes",
        "Security alert: new passkey added to your Assistant account",
        "Security alert: new trusted device added to your Assistant account",
        "[CodeHost] A personal access token (classic) has been added to your account",
        "[CodeHost] A third-party OAuth application has been added to your account",
        "A Provider identity was just linked to your CodeHost account",
        "Your recovery email was changed",
        "Chatapp Email Address Changed",
        "Your Account Has Been Closed - Personal account",
    ]

    def test_every_change_escalates(self):
        for s in self.CHANGES:
            kind, why = signin.classify(s)
            self.assertEqual(kind, signin.ANOMALY, s)
            self.assertTrue(why, s)

    BLOCKED = [
        "We blocked a sign-in attempt to your account",
        "Sign-in attempt prevented",
        "Unsuccessful sign-in attempt",
        "Access denied from an unrecognised device",
        "If this was not you, secure your account",
    ]

    def test_a_blocked_or_failed_attempt_escalates(self):
        """Something was STOPPED. That differs from a successful sign-in in the direction
        that matters most - it is evidence somebody tried, which is the strongest signal a
        provider ever hands you and the cheapest one to overlook. No test covered this until
        a mutant that deleted the whole branch changed nothing."""
        for s in self.BLOCKED:
            kind, why = signin.classify(s)
            self.assertEqual(kind, signin.ANOMALY, s)
            self.assertIn("blocked or failed", " ".join(why), s)

    def test_a_change_is_never_swallowed_by_the_ledger(self):
        out = led([msg("Your password was changed", day="2026-08-02")],
                  baseline=[msg("New sign in", day="2026-01-01")])
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertEqual(out["routine"], [])


class ARoutineSignInGoesQuiet(unittest.TestCase):
    """The bucket that exists so the others can be loud. If this does not go quiet the whole
    exercise is pointless; if it goes quiet too eagerly it is dangerous."""

    def setUp(self):
        # A baseline that has already seen this service and this device.
        self.base = [msg("New login to SvcX from ChromeDesktop on Windows",
                         sender="SvcX <verify@svc-x.example.test>", day="2026-01-%02d" % d)
                     for d in range(1, 6)]

    def test_a_known_service_and_device_is_routine(self):
        out = led([msg("New login to SvcX from ChromeDesktop on Windows",
                       sender="SvcX <verify@svc-x.example.test>", day="2026-08-01")], baseline=self.base)
        self.assertEqual(out["anomalies"], [])
        self.assertEqual(len(out["routine"]), 1)
        self.assertEqual(out["summary"]["routine"], 1)

    def test_a_NEW_device_for_that_service_escalates(self):
        """The positive control on novelty. Same service, same wording, different device."""
        out = led([msg("New login to SvcX from EdgeDesktop on Linux",
                       sender="SvcX <verify@svc-x.example.test>", day="2026-08-01")], baseline=self.base)
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertIn("device never seen", " ".join(out["anomalies"][0]["reasons"]))

    def test_a_service_never_seen_before_escalates(self):
        out = led([msg("New login to your Delivery account",
                       sender="Delivery <no-reply@delivery.example.test>", day="2026-08-01")],
                  baseline=self.base)
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertIn("first security notice", " ".join(out["anomalies"][0]["reasons"]))

    def test_a_financial_service_escalates_even_when_routine(self):
        out = led([msg("Sign-in notice", sender="Retailer <a@retailer.example.test>",
                       day="2026-08-01")],
                  baseline=self.base + [msg("Sign-in notice",
                                            sender="Retailer <a@retailer.example.test>",
                                            day="2026-01-01")],
                  financial={"Retailer"})
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertIn("financial", " ".join(out["anomalies"][0]["reasons"]))

    def test_the_baseline_is_never_reported_on(self):
        """Learn from the past, judge only the window. Without this split the first run
        hands the owner a wall of novelty and teaches them, on day one, that the panel cries
        wolf - which is the exact failure it exists to prevent."""
        out = led([msg("New login to SvcX from ChromeDesktop on Windows",
                       sender="SvcX <verify@svc-x.example.test>", day="2026-08-01")], baseline=self.base)
        self.assertEqual(out["coverage"]["messages"], 1)


class TheBurstIsTheOneWithNoIndividualEvidence(unittest.TestCase):
    """Three services in one window. Every message in it is individually unremarkable, which
    is why a tool that judges one message at a time cannot see it - and why alarm fatigue
    guarantees a person will not either."""

    def base(self):
        out = []
        for svc, addr in (("Gamestore", "s@games.example.test"), ("Filesync", "d@files.example.test"),
                          ("Tracker", "l@tracker.example.test"), ("X", "verify@svc-x.example.test")):
            for d in range(1, 4):
                out.append(msg("New sign in to %s" % svc,
                               sender="%s <%s>" % (svc, addr), day="2026-01-%02d" % d))
        return out

    def test_three_services_in_one_day_is_a_burst(self):
        day = "2026-08-01"
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", day),
                msg("New sign in to Filesync", "Filesync <d@files.example.test>", day),
                msg("New sign in to Tracker", "Tracker <l@tracker.example.test>", day)]
        out = led(rows, baseline=self.base())
        self.assertTrue(out["bursts"], "the burst branch did not fire at all")
        self.assertEqual(len(out["anomalies"]), 3,
                         "a burst must escalate every message in it, not just one")
        self.assertIn("burst", " ".join(out["anomalies"][0]["reasons"]))

    def test_two_services_is_not_a_burst(self):
        """The control. A threshold that fires on two would fire on an ordinary morning."""
        day = "2026-08-01"
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", day),
                msg("New sign in to Filesync", "Filesync <d@files.example.test>", day)]
        out = led(rows, baseline=self.base())
        self.assertEqual(out["bursts"], [])
        self.assertEqual(out["anomalies"], [])

    def test_three_services_spread_over_weeks_is_not_a_burst(self):
        """Otherwise every install with three services is permanently on fire."""
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", "2026-08-01"),
                msg("New sign in to Filesync", "Filesync <d@files.example.test>", "2026-08-11"),
                msg("New sign in to Tracker", "Tracker <l@tracker.example.test>", "2026-08-21")]
        out = led(rows, baseline=self.base())
        self.assertEqual(out["bursts"], [])

    def test_one_service_writing_three_times_is_not_a_burst(self):
        """The burst is about BREADTH. Three notices from one service is a chatty provider;
        three services in an hour is somebody working a list."""
        day = "2026-08-01"
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", day)
                for _ in range(3)]
        out = led(rows, baseline=self.base())
        self.assertEqual(out["bursts"], [])

    def test_one_provider_using_several_from_addresses_is_not_three_services(self):
        """The worst possible bug in this file: a FALSE burst. One provider sent six notices
        from four different no-reply addresses inside a minute, so keying the service on the
        address rather than the display name would have rendered one desktop setup as an
        account-takeover alarm."""
        day = "2026-08-01"
        rows = [msg("Security alert: new trusted device added",
                    "Vendor <no-reply-%s@vendor.example.test>" % tag, day)
                for tag in ("Cb6SzjJ", "CFArJIX", "lM6o0_p")]
        out = led(rows, baseline=self.base())
        self.assertEqual(out["bursts"], [], "one provider read as three services")


class OneEventIsOneItem(unittest.TestCase):
    """A single desktop setup produced six messages within minutes, across two senders.
    Reporting it six times is the disease this whole module treats."""

    def test_notices_minutes_apart_collapse(self):
        rows = [msg("Security alert: new trusted device added to your Assistant account",
                    "Vendor <no-reply-a@vendor.example.test>", "2026-07-29",
                    "2026-07-29T17:23:00+00:00"),
                msg("Security alert: new passkey added to your Assistant account",
                    "Vendor <no-reply-b@vendor.example.test>", "2026-07-29",
                    "2026-07-29T17:24:00+00:00")]
        out = led(rows)
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertEqual(out["anomalies"][0]["collapsed"], 2)

    def test_the_collapse_keeps_every_reason_and_subject(self):
        """Keeping only the first would turn a collapse into a loss - the interesting thing
        about that cluster was that it held a new passkey AND a new trusted device."""
        rows = [msg("Security alert: new passkey added", "Vendor <a@vendor.example.test>",
                    "2026-07-29", "2026-07-29T17:23:00+00:00"),
                msg("Your Account Was Accessed From a New Device",
                    "Vendor <b@vendor.example.test>", "2026-07-29",
                    "2026-07-29T17:25:00+00:00")]
        out = led(rows)
        a = out["anomalies"][0]
        self.assertEqual(a["collapsed"], 2)
        self.assertTrue(a["also"])
        self.assertIn("provider itself calls the device", " ".join(a["reasons"]))

    def test_notices_hours_apart_do_NOT_collapse(self):
        """Two sign-ins to one service in a day are two events. Collapsing on service alone
        would hide the second, which is the one that is not you."""
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", "2026-08-01",
                    "2026-08-01T09:00:00+00:00"),
                msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", "2026-08-01",
                    "2026-08-01T19:00:00+00:00")]
        out = led(rows, baseline=[msg("New sign in to Gamestore",
                                      "Gamestore <s@games.example.test>", "2026-01-01")])
        self.assertEqual(len(out["routine"]), 2)

    def test_two_ANOMALIES_from_one_service_hours_apart_stay_two(self):
        """The case that matters, and the one the first draft of this file missed: collapsing
        runs over ANOMALIES, so a test written with routine sign-ins never exercised it at
        all. Two 'new device' alerts from one service ten hours apart are two events, and the
        second is the one that is not you. A mutant removing the time check survived until
        this existed."""
        rows = [msg("Your Account Was Accessed From a New Device",
                    "Videosite <hello@video.example.test>", "2026-08-01",
                    "2026-08-01T09:00:00+00:00"),
                msg("Your Account Was Accessed From a New Device",
                    "Videosite <hello@video.example.test>", "2026-08-01",
                    "2026-08-01T19:00:00+00:00")]
        out = led(rows)
        self.assertEqual(len(out["anomalies"]), 2,
                         "two separate device alerts collapsed into one")

    def test_different_services_never_collapse(self):
        rows = [msg("New sign in to Gamestore", "Gamestore <s@games.example.test>", "2026-08-01",
                    "2026-08-01T09:00:00+00:00"),
                msg("New sign in to Filesync", "Filesync <d@files.example.test>", "2026-08-01",
                    "2026-08-01T09:01:00+00:00")]
        out = led(rows)
        self.assertEqual(len(out["anomalies"]), 2)


class TheParserUnderClaims(unittest.TestCase):
    """A signature we merely suspect fabricates novelty in the one panel whose entire value
    is being believed when it fires."""

    def test_real_devices_parse(self):
        for s, want in (("New login to SvcX from ChromeDesktop on Mac", "chrome"),
                        ("New login to SvcX from EdgeDesktop on Windows", "windows"),
                        ("Your Apple Account was used to sign in to iCloud on a Mac mini "
                         "(2024).", "mac")):
            got = signin.device_of(s)
            self.assertIsNotNone(got, s)
            self.assertIn(want, got, s)

    def test_prose_does_not(self):
        """The measured false positive: 'required' pulled out of '[ACTION REQUIRED]' and
        reported as a device never seen before for that service."""
        for s in ("[ACTION REQUIRED] Your CodeHost account will soon require two-factor",
                  "New sign in to Gamestore",
                  "Hi eric, we noticed a new sign in to your Filesync account",
                  "Sign-in notice",
                  "You shared some Provider Account data with Assistant"):
            self.assertIsNone(signin.device_of(s), s)

    def test_coverage_is_reported_rather_than_assumed(self):
        rows = [msg("New login to SvcX from ChromeDesktop on Mac", "SvcX <v@svc-x.example.test>"),
                msg("New sign in to Gamestore", "Gamestore <s@games.example.test>")]
        out = led(rows)
        self.assertEqual(out["coverage"]["device_parsed"], 1)
        self.assertEqual(out["coverage"]["messages"], 2)
        self.assertIn("UNKNOWN", out["coverage"]["note"])


class NoiseIsSeparatedFromEvents(unittest.TestCase):

    def test_a_consent_receipt_is_its_own_bucket(self):
        kind, _ = signin.classify("You shared some Provider Account data with Assistant")
        self.assertEqual(kind, signin.CONSENT)

    def test_terms_and_policy_updates_are_not_events(self):
        for s in ("Updates to our terms of use",
                  "We're making some changes to our Payservice legal agreements",
                  "New Mobileos Backup Storage Policy & Controls",
                  "New privacy settings for Search services",
                  "Help strengthen the security of your Provider Account"):
            self.assertEqual(signin.classify(s)[0], signin.POLICY, s)

    def test_an_ambiguous_subject_escalates_rather_than_going_quiet(self):
        """A consent receipt that also mentions a device is still about a change, and the
        fail-safe direction for this file is loud."""
        kind, _ = signin.classify(
            "You shared some Provider Account data with Assistant - new passkey added")
        self.assertEqual(kind, signin.ANOMALY)


class ServiceFolding(unittest.TestCase):

    def test_spellings_of_one_provider_fold(self):
        a = signin.service_of("Provider")
        b = signin.service_of("Provider <noreply-accounts@provider.example.test>")
        c = signin.service_of('"Provider" <no-reply@id.provider.example.test>')
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_one_display_name_across_different_domains_is_one_service(self):
        """Keyed on the DISPLAY NAME, not the address - and this is the case that proves it.

        Real providers send from several domains: the account-notice arm and the marketing
        arm live on different hosts under the same brand. Keying on the address makes one
        provider read as several services, and several services inside a window is the burst
        rule - the loudest thing this module can say. A false burst is the worst bug in the
        file, so the guard against it gets a test that can only pass one way.

        An earlier version of this test used three addresses on ONE domain, which a mutation
        pass showed proves nothing: the domain fallback folds those anyway.
        """
        a = signin.service_of("Provider <no-reply@id.provider.example.test>")
        b = signin.service_of("Provider <notices@mail-two.example.test>")
        c = signin.service_of("Provider <alerts@a-third-host.example.test>")
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(a, "provider")

    def test_different_providers_do_not_fold(self):
        self.assertNotEqual(signin.service_of("Gamestore Team"),
                            signin.service_of("Filesync <no-reply@files.example.test>"))

    def test_a_bare_address_falls_back_to_its_domain(self):
        self.assertIn("files", signin.service_of("no-reply@files.example.test"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
