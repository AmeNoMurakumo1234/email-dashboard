"""Reading a message on an install where nothing here fetches mail.

Two defects, one cause: declaring a connector account told the tool the mailbox exists but
gave it nothing to read. So the viewer fell into the "searched and found nothing" branch and
rendered **not found in this mailbox** above a detail that correctly said nothing had gone
looking - a headline contradicting its own explanation, in the one place the tool has the
most certainty about what happened. And the sandboxed reader, the image blocking and the
tracking-host report - the headline privacy features - were unreachable for every row.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import db                                                          # noqa: E402
import server                                                      # noqa: E402

ACCOUNT = "owner@example.com"
MID = "<stored@example.com>"


def store(**msg):
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn = db.connect(path)
    conn.execute("INSERT INTO runs (run_date, created_at) VALUES ('2026-08-01','x')")
    rid = conn.execute("SELECT id FROM runs").fetchone()[0]
    conn.execute(
        "INSERT INTO messages (run_id, run_date, account, sender, subject, disposition, "
        "category, message_id, body_text, web_link) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, "2026-08-01", ACCOUNT, "Someone <s@example.com>", "hello", "surfaced",
         "action", MID, msg.get("body_text"), msg.get("web_link")))
    conn.commit()
    return conn


class ConnectorAccounts(unittest.TestCase):
    """Every test here forces the connector backend, because that is the state under test.

    Passed explicitly rather than by writing an accounts.json: what is being checked is the
    VIEWER's behaviour given a backend, and threading a config file through it would test
    the config loader instead.
    """

    def read(self, conn, **q):
        real = server._backend_for_account
        server._backend_for_account = lambda a: "connector"
        try:
            return server.api_message(conn, dict({"message_id": [MID],
                                                  "account": [ACCOUNT]}, **q))
        finally:
            server._backend_for_account = real

    def test_it_never_claims_the_message_was_not_found(self):
        """The headline is the finding. Nothing was searched, so "not found" is a claim the
        tool has no basis for - and it is the exact absence-reported-as-fact this endpoint
        was rewritten to remove."""
        out = self.read(store())
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_local_fetcher")
        self.assertFalse(out["searched"])
        self.assertNotIn("not found", (out.get("error") or "").lower())

    def test_it_offers_the_provider_link_when_there_is_one(self):
        out = self.read(store(web_link="https://mail.example.com/read?id=PROBE"))
        self.assertEqual(out["web_link"], "https://mail.example.com/read?id=PROBE")

    def test_a_stored_plain_body_is_readable_with_no_fetch(self):
        """The one that makes the sandboxed reader reachable at all on this install."""
        out = self.read(store(body_text="Hello there.\nThis is the body."))
        self.assertTrue(out["ok"], out)
        self.assertIn("This is the body.", out["text"])

    def test_a_stored_html_body_goes_through_the_same_sanitiser(self):
        html = ('<html><body><p>hi</p>'
                '<img src="https://tracker.example.net/pixel.gif">'
                '<script>alert(1)</script></body></html>')
        out = self.read(store(body_text=html), html=["1"])
        self.assertTrue(out["ok"], out)
        blob = (out.get("html") or "")
        self.assertNotIn("<script", blob.lower(),
                         "a second rendering route is a second place for this to leak")
        self.assertNotIn("tracker.example.net", blob)
        # And the report is what tells the reader the message tried.
        self.assertTrue(out.get("report"), "the tracking-host report must still be built")

    def test_raw_mime_is_accepted_as_well_as_a_bare_body(self):
        """Some connectors can hand over the whole message; most cannot. Both work, and the
        difference is resolved here rather than in every connector author's code."""
        raw = ("Subject: stored\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\nRaw MIME body.")
        out = self.read(store(body_text=raw))
        self.assertTrue(out["ok"], out)
        self.assertIn("Raw MIME body.", out["text"])
        self.assertEqual(out["headers"]["subject"], "stored")

    def test_nothing_is_spawned_when_the_body_is_already_stored(self):
        """No subprocess, no socket, no temp file - the point of storing it."""
        import subprocess                                           # noqa: PLC0415
        real = subprocess.run

        def refuse(*a, **k):
            raise AssertionError("api_message spawned a fetcher for a stored body")

        subprocess.run = refuse
        try:
            out = self.read(store(body_text="Body."))
        finally:
            subprocess.run = real
        self.assertTrue(out["ok"], out)


class MimeCoercion(unittest.TestCase):

    def test_plain_text_becomes_a_text_plain_part(self):
        raw = server._as_mime_bytes("just words").decode()
        self.assertIn("text/plain", raw)
        self.assertIn("just words", raw)

    def test_html_is_detected(self):
        raw = server._as_mime_bytes("<div>hello</div>").decode()
        self.assertIn("text/html", raw)

    def test_an_existing_message_is_not_wrapped_twice(self):
        """Wrapping a whole message in another header block makes the real headers body
        text - the subject, the sender and the date all silently vanish."""
        raw = server._as_mime_bytes("Subject: x\r\n\r\nbody").decode()
        self.assertEqual(raw.count("Subject:"), 1)
        self.assertNotIn("MIME-Version: 1.0\r\nContent-Type: text/plain", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
