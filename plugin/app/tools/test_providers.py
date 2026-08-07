"""Which backend gets dialled - and, more importantly, which does not.

The bug being pinned: `connect()` opened an IMAP socket on its first line, before it had
looked at the provider at all. Every test here that asserts a refusal uses an `imap_host`
that cannot resolve, so if the routing check ever moves back below the socket the test does
not merely fail - it hangs or raises a DNS error, which is the loudest possible signal that
the tool dialled something it had already been told not to.
"""
import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mailtool                                                    # noqa: E402
import providers                                                   # noqa: E402

# A host that must never be reached. If it is, that is the finding.
POISON = "imap.invalid.this-host-must-never-be-dialled.example"


def acct(provider, **kw):
    return dict({"email": "someone@example.com", "provider": provider,
                 "imap_host": POISON}, **kw)


class Routing(unittest.TestCase):

    def test_graph_is_refused_without_opening_a_socket(self):
        with self.assertRaises(RuntimeError) as cm:
            mailtool.connect("someone@example.com", acct=acct("graph"))
        self.assertIn("msgraph.py", str(cm.exception))
        self.assertNotIn(POISON, str(cm.exception),
                         "the error must describe the arrangement, not the socket")

    def test_connector_is_refused_and_pointed_at_ingest(self):
        with self.assertRaises(RuntimeError) as cm:
            mailtool.connect("someone@example.com", acct=acct("connector"))
        self.assertIn("ingest.py", str(cm.exception))

    def test_an_unknown_provider_is_refused_rather_than_guessed(self):
        """Defaulting a typo to IMAP dials the wrong backend with a confusing error."""
        with self.assertRaises(RuntimeError):
            mailtool.connect("someone@example.com", acct=acct("grpah"))
        self.assertIsNone(providers.backend_of(acct("grpah")))

    def test_imap_accounts_still_route_to_imap(self):
        """The control. If everything were refused, the tests above prove nothing."""
        for p in ("gmail", "microsoft", "imap", ""):
            self.assertEqual(providers.backend_of(acct(p)), providers.IMAP, p)


class WhatEachBackendRequires(unittest.TestCase):

    def test_imap_host_is_required_only_for_imap(self):
        self.assertTrue(any("imap_host" in x for x in
                            providers.problems({"email": "a@b.c", "provider": "gmail"}, {})))
        for p in ("graph", "connector"):
            probs = providers.problems({"email": "a@b.c", "provider": p},
                                       {"ms_client_id": "guid"})
            self.assertFalse(any("imap_host" in x for x in probs), (p, probs))

    def test_a_leftover_imap_host_does_not_break_a_graph_account(self):
        self.assertEqual(providers.problems(acct("graph"), {"ms_client_id": "guid"}), [])

    def test_a_connector_account_needs_nothing_at_all(self):
        """Declaring the mailbox IS the configuration. Requiring more would push people
        back to leaving it out of the config, where nothing can see it."""
        self.assertEqual(providers.problems({"email": "a@b.c", "provider": "connector"}, {}),
                         [])

    def test_graph_without_an_app_registration_says_what_to_run(self):
        probs = providers.problems({"email": "a@b.c", "provider": "graph"}, {})
        self.assertTrue(probs)
        self.assertIn("msgraph.py auth", probs[0])

    def test_microsoft_imap_without_a_registration_still_says_so(self):
        probs = providers.problems(acct("microsoft"), {})
        self.assertTrue(any("ms_client_id" in x for x in probs), probs)


class ConnectorIsNotAFailure(unittest.TestCase):

    def test_status_is_its_own_state_not_FAILED(self):
        status, detail = providers.status_of({"email": "a@b.c", "provider": "connector"}, {})
        self.assertEqual(status, "CONNECTOR")
        self.assertNotIn("FAIL", status)
        self.assertTrue(detail)

    def test_a_misconfigured_account_is_still_reported_as_such(self):
        """The control: CONNECTOR must not become a way for real problems to pass."""
        status, _ = providers.status_of({"provider": "connector"}, {})   # no email key
        self.assertEqual(status, "NOT CONFIGURED")

    def test_accounts_this_tool_cannot_fetch_are_named(self):
        self.assertFalse(providers.fetches_itself({"provider": "connector"}))
        self.assertTrue(providers.fetches_itself({"provider": "graph"}))
        self.assertTrue(providers.fetches_itself({"provider": "gmail"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DelegationTranslatesOrRefuses(unittest.TestCase):
    """The dangerous half of delegating: a flag that quietly does not survive the trip."""

    def args(self, **kw):
        base = dict(cmd="fetch", account="a@b.c", mailbox="INBOX", days=7, limit=100,
                    offset=0, unseen=False, no_snippets=False)
        base.update(kw)
        # argparse.Namespace, not an ad-hoc class. A class built with type() holds its
        # fields as CLASS attributes, which `vars(instance)` does not see - so the fake
        # translated to an empty argv and the refusal tests passed nothing while looking
        # like they passed.
        return argparse.Namespace(**base)

    def test_a_fetch_translates_with_every_flag_carried(self):
        argv = mailtool.graph_argv("fetch", self.args(days=3, limit=50, unseen=True,
                                                      mailbox="Archive"))
        self.assertEqual(argv[0], "fetch")
        self.assertIn("--unseen", argv)
        self.assertEqual(argv[argv.index("--days") + 1], "3")
        self.assertEqual(argv[argv.index("--limit") + 1], "50")
        # mailbox is called folder on the other side; the value must survive the rename
        self.assertEqual(argv[argv.index("--folder") + 1], "Archive")
        self.assertNotIn("--mailbox", argv)

    def test_a_flag_with_no_equivalent_stops_the_command(self):
        """It must NOT be dropped. `fetch --uid-range 1:50` silently becoming an unbounded
        fetch answers a different question and looks like it worked."""
        with self.assertRaises(SystemExit) as cm:
            mailtool.graph_argv("fetch", self.args(uid_range="1:50"))
        self.assertIn("uid-range", str(cm.exception))
        self.assertIn("Refusing", str(cm.exception))

    def test_act_is_refused_because_graph_here_cannot_write(self):
        with self.assertRaises(SystemExit) as cm:
            mailtool.graph_argv("act", self.args(cmd="act", action="trash", uids="1"))
        self.assertIn("READ-ONLY", str(cm.exception))

    def test_false_and_empty_flags_are_not_passed_as_values(self):
        argv = mailtool.graph_argv("fetch", self.args(unseen=False, no_snippets=False))
        self.assertNotIn("--unseen", argv)
        self.assertNotIn("--no-snippets", argv)
