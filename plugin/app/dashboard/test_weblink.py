"""A web link is a durable handle, and the encoding detail that makes it work. (F33)

`backfill_bodies.py` required a Message-ID and called every other row a permanent hole. On the
store where this was reported, Message-IDs covered under a third of the rows while web links covered
**all of them** - so that rule reached under a third of the mail and declared the rest
unrecoverable, while every one of those rows was fetchable through the identifier its link
already carried. Saying "permanent hole" out loud was the right instinct attached to the wrong
arithmetic, which is worse than saying nothing.

THE ENCODING IS THE WHOLE TRICK, and it fails in the worst possible pattern.

`ItemID` in an OWA web link is percent-encoded **standard base64** - the alphabet with `/` and
`+`. Graph's own `id` is **base64URL** - `-` and `_`. Hand the decoded standard form to Graph
and a `/` is read as a path separator: `ErrorInvalidIdMalformed`. Roughly one id in twelve
contains one of those characters, so a naive version works for the first dozen messages and
then fails - it looks like it works, which is the distribution that gets shipped.

NOT VERIFIED END TO END HERE. Every account on this install is IMAP, and IMAP has no web
link, so no row in this store has one to test against. The extraction and the conversion are
tested; the fetch that uses them is not. Stated rather than implied - "it should work" and
"it was seen to work" are different claims and only one has been earned.

    python dashboard/test_weblink.py
"""
import os
import sys
import unittest
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import weblink                                                     # noqa: E402

# A realistic OWA deep link. The id deliberately contains BOTH characters that break a naive
# implementation.
STD_ID = "AAMkAGI2THVSAAA=/x+y/z+w"
OWA = ("https://outlook.office365.com/owa/?ItemID=" + quote(STD_ID, safe="")
       + "&exvsurl=1&viewmodel=ReadMessageItem")


class TheAlphabetConversion(unittest.TestCase):

    def test_slash_and_plus_become_dash_and_underscore(self):
        self.assertEqual(weblink.to_base64url("a/b+c"), "a-b_c")

    def test_an_id_with_neither_is_unchanged(self):
        """Most ids have neither, which is exactly why a naive version survives testing."""
        self.assertEqual(weblink.to_base64url("AAMkAGI2THVSAAA="), "AAMkAGI2THVSAAA=")

    def test_padding_is_left_alone(self):
        self.assertTrue(weblink.to_base64url("abc=").endswith("="))


class ExtractingTheHandle(unittest.TestCase):

    def test_an_owa_link_yields_a_base64url_id(self):
        got = weblink.item_id(OWA)
        self.assertIsNotNone(got)
        self.assertNotIn("/", got)
        self.assertNotIn("+", got)
        self.assertIn("-", got)
        self.assertIn("_", got)

    def test_it_percent_decodes_BEFORE_converting(self):
        """Order matters. Converting first would rewrite the '%2F' escape's own characters
        and produce an id that decodes to nothing."""
        self.assertEqual(weblink.item_id(OWA), weblink.to_base64url(STD_ID))

    def test_the_parameter_name_is_matched_case_insensitively(self):
        low = OWA.replace("ItemID=", "itemid=")
        self.assertEqual(weblink.item_id(low), weblink.item_id(OWA))

    def test_a_link_with_no_identifier_returns_None(self):
        """A real answer, not a failure: a link that merely opens a mailbox is not a handle
        for a message, and pretending otherwise would send a fetch after nothing."""
        self.assertIsNone(weblink.item_id("https://outlook.office365.com/mail/inbox"))

    def test_empty_and_garbage_are_None_rather_than_raising(self):
        for bad in (None, "", "not a url", "://///"):
            self.assertIsNone(weblink.item_id(bad), repr(bad))


class WhichHandleARowHas(unittest.TestCase):

    def test_message_id_wins_when_both_are_present(self):
        """Provider-independent and survives the message moving between folders; an item id
        is scoped to one provider's API. The link is the fallback, not the preference."""
        kind, value = weblink.handle_of({"message_id": "<a@b>", "web_link": OWA})
        self.assertEqual(kind, "message_id")
        self.assertEqual(value, "<a@b>")

    def test_the_web_link_is_used_when_there_is_no_message_id(self):
        kind, value = weblink.handle_of({"message_id": None, "web_link": OWA})
        self.assertEqual(kind, "item_id")
        self.assertNotIn("/", value)

    def test_whitespace_is_not_a_message_id(self):
        kind, _ = weblink.handle_of({"message_id": "   ", "web_link": OWA})
        self.assertEqual(kind, "item_id")

    def test_neither_is_a_real_hole(self):
        """The control that keeps the corrected claim honest. Widening 'recoverable' until
        nothing is ever a hole would be the same failure in the opposite direction."""
        self.assertIsNone(weblink.handle_of({"message_id": "", "web_link": ""}))
        self.assertIsNone(weblink.handle_of({}))

    def test_a_link_without_an_id_is_also_a_hole(self):
        self.assertIsNone(weblink.handle_of(
            {"message_id": "", "web_link": "https://outlook.office365.com/mail/inbox"}))


class TheBackfillUsesIt(unittest.TestCase):
    """The claim the tool prints has to match the rule it actually applies."""

    def setUp(self):
        import backfill_bodies                                      # noqa: PLC0415
        self.bf = backfill_bodies

    def test_a_row_with_only_a_web_link_is_a_candidate(self):
        import sqlite3                                              # noqa: PLC0415
        import tempfile                                             # noqa: PLC0415
        import db                                                   # noqa: PLC0415
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(db.connect(path))
        c = db.connect(path)
        c.execute("INSERT OR IGNORE INTO runs (run_date, created_at) VALUES ('2026-08-01','x')")
        rid = c.execute("SELECT id FROM runs").fetchone()[0]
        for mid, link in ((None, OWA), ("<has@id>", None), (None, None)):
            c.execute("INSERT INTO messages (run_id, run_date, account, sender, subject, "
                      "disposition, category, message_id, web_link) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (rid, "2026-08-01", "o@example.test", "s@example.test", "subj",
                       "kept", "bank-statement", mid, link))
        c.commit()
        rows = self.bf.candidates(db.connect(path))
        self.assertEqual(len(rows), 2, "the web-link-only row was not offered")
        kinds = sorted(weblink.handle_of(r)[0] for r in rows)
        self.assertEqual(kinds, ["item_id", "message_id"])
        self.assertIsInstance(sqlite3.connect(path), sqlite3.Connection)

    def test_a_row_with_neither_is_refused_before_any_fetch(self):
        body, note = self.bf.fetch_one({"account": "o@example.test", "message_id": None,
                                        "web_link": None})
        self.assertIsNone(body)
        self.assertIn("no Message-ID", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
