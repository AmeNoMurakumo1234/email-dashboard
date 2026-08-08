"""A stored body is used whatever the backend is - and the proof is that nothing is spawned.

`body_text` was NULL on every row of a real store for a year. The viewer did not LOOK broken,
because it falls back to re-fetching each message on demand, so the sandboxed reader, the
image blocking and the tracking-host report all worked - right up until the message was no
longer in the mailbox. A feature that quietly depends on a network round trip against mail
that ages out is not a working feature; it is one that will fail later for reasons nobody
will connect back to this.

Fixing that meant carrying the body at fetch time (free - the fetcher already downloads the
whole message and throws it away after taking a snippet) and backfilling the history (a
stratified sample said 100% of it was still retrievable, including binned mail over a year
old).

AND IT WOULD ALL HAVE DONE NOTHING. The stored body was only ever consulted inside the
CONNECTOR branch, on the reasoning that a connector install has no fetcher and therefore
needs it. True, and the wrong place for it: the value of a stored body has nothing to do with
which backend an account uses. On an IMAP store every row could have had its body sitting in
the column and every single open would still have gone to the network for a second copy.

So the assertion that matters here is not "the right bytes come back" - they did before. It
is that NO SUBPROCESS RUNS. That is the difference between a body that is stored and a body
that is merely also stored.

    python dashboard/test_stored_body.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402
import server                                                      # noqa: E402

HTML = "<html><body><p>hello</p><img src='https://tracker.example.test/px.gif'></body></html>"


class Store:
    def __init__(self, backend=None):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(self.path))
        self.backend = backend

    def add(self, message_id, account="owner@example.test", body=None, web_link=None):
        c = db.connect(self.path)
        c.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES ('2026-08-01','x')")
        rid = c.execute("SELECT id FROM runs").fetchone()[0]
        c.execute("INSERT INTO messages (run_id, run_date, account, sender, subject, "
                  "disposition, category, message_id, body_text, web_link) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (rid, "2026-08-01", account, "S <s@example.test>", "subj", "kept",
                   "bank-statement", message_id, body, web_link))
        c.commit()
        return c


class NoSubprocessWhenTheBodyIsStored(unittest.TestCase):
    """The spawn counter IS the test. Everything else here was already true."""

    def setUp(self):
        self.spawns = []
        self._real_run = server.subprocess.run
        self._real_backend = server._backend_for_account

        def counting_run(*a, **kw):
            self.spawns.append(a[0] if a else None)
            return self._real_run(*a, **kw)

        server.subprocess.run = counting_run

    def tearDown(self):
        server.subprocess.run = self._real_run
        server._backend_for_account = self._real_backend

    def open_msg(self, conn, mid, account="owner@example.test"):
        return server.api_message(conn, {"message_id": [mid], "account": [account]})

    def test_an_imap_account_with_a_stored_body_never_shells_out(self):
        """The defect. This is the case that used to re-fetch regardless."""
        server._backend_for_account = lambda a: "imap"
        s = Store()
        conn = s.add("<a@x>", body=HTML)
        out = self.open_msg(conn, "<a@x>")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.spawns, [], "it went to the network for a body it already had")

    def test_a_graph_account_with_a_stored_body_never_shells_out(self):
        server._backend_for_account = lambda a: "graph"
        s = Store()
        conn = s.add("<b@x>", body=HTML)
        out = self.open_msg(conn, "<b@x>")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.spawns, [])

    def test_a_connector_account_with_a_stored_body_never_shells_out(self):
        """Unchanged behaviour, kept under test so the rearrangement did not lose it."""
        server._backend_for_account = lambda a: "connector"
        s = Store()
        conn = s.add("<c@x>", body=HTML)
        out = self.open_msg(conn, "<c@x>")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.spawns, [])

    def test_the_stored_body_is_actually_rendered_and_sanitised(self):
        """Not just 'ok': the body has to reach the same parse-and-sanitise path, or a
        stored body would be a second rendering route - and a second route is a second place
        for image blocking to differ, with the untested one being the one that leaks."""
        server._backend_for_account = lambda a: "imap"
        s = Store()
        conn = s.add("<d@x>", body=HTML)
        out = self.open_msg(conn, "<d@x>")
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("has_html"))
        blob = repr(out.get("report")) + repr(out.get("headers"))
        self.assertIn("tracker.example.test", blob,
                      "the tracking host was not reported, so the body skipped the "
                      "sanitising path this whole feature exists for")

    def test_WITHOUT_a_stored_body_an_imap_account_still_fetches(self):
        """The control, and it is the important one: a version that never spawned would
        pass every assertion above and quietly break opening mail that was never stored."""
        server._backend_for_account = lambda a: "imap"
        s = Store()
        conn = s.add("<e@x>", body=None)
        self.open_msg(conn, "<e@x>")
        self.assertTrue(self.spawns, "no stored body and no fetch attempt - the message "
                                     "is simply unreachable now")

    def test_an_empty_string_is_not_a_stored_body(self):
        """'' must mean absent, not 'the message is blank'. Otherwise a row written with an
        empty column becomes permanently unopenable, with no fetch ever attempted."""
        server._backend_for_account = lambda a: "imap"
        s = Store()
        conn = s.add("<f@x>", body="   ")
        self.open_msg(conn, "<f@x>")
        self.assertTrue(self.spawns, "whitespace was treated as a real body")

    def test_a_connector_with_no_body_says_it_did_not_search(self):
        """Absence reported as fact is the failure this endpoint was rewritten to remove."""
        server._backend_for_account = lambda a: "connector"
        s = Store()
        conn = s.add("<g@x>", body=None, web_link="https://mail.example.test/g")
        out = self.open_msg(conn, "<g@x>")
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("searched"))
        self.assertEqual(out.get("reason"), "no_local_fetcher")
        self.assertEqual(out.get("web_link"), "https://mail.example.test/g")
        self.assertEqual(self.spawns, [])


class TheFetcherCarriesTheBodyItAlreadyHas(unittest.TestCase):
    """`fetch` pulls BODY.PEEK[] for every message and discards it after a 400-char snippet.
    Carrying it through costs nothing on the wire, which is why this went unfixed so long:
    the expensive-looking fix was already paid for."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import mailtool                                             # noqa: PLC0415
        self.mailtool = mailtool

    def message(self, html=None, text=None, attach=False):
        import email.message                                        # noqa: PLC0415
        m = email.message.EmailMessage()
        m["Subject"] = "s"
        m.set_content(text or "plain body")
        if html:
            m.add_alternative(html, subtype="html")
        if attach:
            m.add_attachment(b"x" * 5000, maintype="application",
                             subtype="pdf", filename="big.pdf")
        return m

    def test_html_is_preferred_over_plain(self):
        got = self.mailtool.full_body(self.message(html="<p>rich</p>", text="flat"))
        self.assertIn("rich", got)

    def test_plain_is_used_when_there_is_no_html(self):
        got = self.mailtool.full_body(self.message(text="flat only"))
        self.assertIn("flat only", got)

    def test_attachments_are_excluded(self):
        """Otherwise the store's size becomes a function of what other people email you."""
        got = self.mailtool.full_body(self.message(html="<p>rich</p>", attach=True))
        self.assertIn("rich", got)
        self.assertLess(len(got), 3000)

    def test_a_message_with_no_readable_part_returns_None(self):
        """None, not '' - absent and empty are different claims, and '' would make the row
        permanently unopenable with no fetch ever attempted."""
        import email.message                                        # noqa: PLC0415
        m = email.message.EmailMessage()
        m["Subject"] = "s"
        m.set_content(b"\x00\x01", maintype="application", subtype="octet-stream")
        self.assertIsNone(self.mailtool.full_body(m))


if __name__ == "__main__":
    unittest.main(verbosity=2)
