"""Printing a stored subject must not kill the program. (F31, F34)

Subjects contain whatever a sender typed. A Windows console defaults to cp1252, which cannot
encode most emoji - so any entry point that prints one used to abort mid-listing with a
UnicodeEncodeError, on a machine where nothing was wrong with the data or the tool.

Measured on one store: 170 of its distinct subjects are not cp1252-encodable. Emoji in
marketing subject lines and social notifications are not an edge case, they are most of a
modern inbox.

REPORTED TWICE, AGAINST TWO DIFFERENT FILES, before it was fixed as a class - first against
`ack.py --list`, then against `backfill_bodies.py --dry-run`, a file WRITTEN AFTER the first
report was closed. Fixing the file that was named instead of the shape of the defect is how
the same bug gets reported a third time. So this suite checks EVERY entry point, and derives
the list rather than carrying one.

    python dashboard/test_console_encoding.py
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import consoleio                                                   # noqa: E402

EMOJI = "Fw: Claude 1M context \U0001F4DA, Perplexity’s bid \U0001F4BB"


def run(code, encoding="cp1252"):
    env = dict(os.environ, PYTHONIOENCODING=encoding, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, env=env, encoding="utf-8", errors="replace",
                          timeout=60)


class TheHelperDoesTheJob(unittest.TestCase):

    def test_without_it_a_cp1252_console_crashes(self):
        """The control. Without this the suite could pass on a machine where nothing was
        ever broken, and prove nothing about the machines where it was."""
        p = run("print(%r)" % EMOJI)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("UnicodeEncodeError", p.stderr)

    def test_with_it_the_same_line_prints(self):
        p = run("import sys; sys.path.insert(0, 'dashboard');"
                "from consoleio import safe_console; safe_console(); print(%r)" % EMOJI)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        self.assertIn("Claude 1M context", p.stdout)

    def test_it_never_raises_even_on_a_stream_it_cannot_touch(self):
        """It runs at the top of entry points. A helper that could abort a program before it
        starts, over the ENCODING of its output, would be worse than the bug it fixes."""
        import io                                                   # noqa: PLC0415
        real_out, real_err = sys.stdout, sys.stderr
        try:
            sys.stdout = io.StringIO()          # no .reconfigure
            sys.stderr = io.StringIO()
            self.assertTrue(consoleio.safe_console())
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    def test_it_is_idempotent(self):
        self.assertTrue(consoleio.safe_console())
        self.assertTrue(consoleio.safe_console())


class EVERY_ENTRY_POINT_IS_COVERED(unittest.TestCase):
    """The class-level guard, and the reason this file exists rather than two assertions.

    DERIVED, not listed. A hard-coded roster here would go stale the first time somebody adds
    a script - which is precisely how the second report happened.
    """

    def entry_points(self):
        """Every non-test module that has a __main__ block and prints."""
        out = []
        for folder in ("dashboard", "tools"):
            d = os.path.join(ROOT, folder)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                p = os.path.join(d, name)
                try:
                    src = open(p, encoding="utf-8").read()
                except OSError:
                    continue
                if '__main__' not in src or "print(" not in src:
                    continue
                out.append((folder, name, src))
        return out

    # Modules that legitimately never print stored mail text. Named individually, with the
    # reason, so that adding to this list is a decision somebody makes on purpose.
    EXEMPT = {
        "server.py": "writes HTTP responses; its logging never prints a stored subject",
        "credstore.py": "credential plumbing, no mail text",
        "untrusted.py": "pure labelling library",
        "msgraph.py": "transport layer, invoked by mailtool",
        "db.py": "schema/migration output only",
        "consoleio.py": "this is the helper",
        "livecheck.py": "prints only its own fixed strings",
        "signin.py": "no __main__ output of stored text",
        "questions.py": "library",
        "elsewhere.py": "library",
        "mailview.py": "library",
        "concepts.py": "library",
        "categorize.py": "library",
        "extract_links.py": "library",
        "providers.py": "library",
        "runmode.py": "library",
        "apply_answers.py": "prints its own report; kept covered anyway",
        "steam_refresh.py": "prints game titles, covered anyway",
    }

    def test_every_printing_entry_point_calls_safe_console(self):
        missing = []
        for folder, name, src in self.entry_points():
            if "safe_console" in src:
                continue
            if name in self.EXEMPT:
                continue
            missing.append("%s/%s" % (folder, name))
        self.assertEqual(missing, [],
                         "these can print stored text and would die on a cp1252 console:\n  "
                         + "\n  ".join(missing))

    def test_the_scan_actually_finds_entry_points(self):
        """The positive control on the scan itself. A discovery that matched nothing would
        make the test above pass over an empty set - a green check on no coverage at all."""
        found = self.entry_points()
        self.assertGreater(len(found), 8, "entry-point discovery found almost nothing")
        names = {n for _, n, _ in found}
        self.assertIn("ack.py", names)
        self.assertIn("backfill_bodies.py", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
