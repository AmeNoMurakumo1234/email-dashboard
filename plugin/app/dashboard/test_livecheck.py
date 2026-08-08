"""A suite that cannot run must say so - loudly, instantly, and non-zero.

Three suites here drive the live dashboard over HTTP, which is the right way to test them:
the entitlement checks they cover live in the request handler, and calling the functions
directly would skip the guard a browser actually meets.

With no server running they behaved three different ways, all wrong. Two dumped a raw urllib
traceback, which reads as "the ack guard is broken" rather than "nothing was listening". One
HUNG until killed - worse than either, because a hang reads as slowness, so the honest answer
never reaches anybody at all.

NOT RUN IS NOT PASSED, and it is not FAILED either. It is a third thing, and it only stays
honest if it is loud, instant and non-zero. Reporting it as a pass is how a green board comes
to cover code that nothing has executed, which is this project's own named failure mode
pointed at its own test suite.

    python dashboard/test_livecheck.py
"""
import os
import socket
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import livecheck                                                   # noqa: E402

def live_suites():
    """Every suite that ACTUALLY calls the preflight - discovered, never listed.

    This was a hard-coded tuple, and it named `test_host_flags.py`, which the plugin package
    does not ship. So a clean install ran the assertion against a file that does not exist,
    Python reported a missing file, and this suite reported that as a FAILURE.

    The irony is the point, and it is worth keeping written down: 0.18.1 existed to stop a
    suite that never executed from being reported as a suite that failed. A hard-coded roster
    inside the file that polices rosters reproduced exactly that conflation, one level up, in
    the release about it. `run_tests.py` already discovers rather than lists, for this reason.

    Derived from the call, not the import: a file can import the helper and never call it,
    and a grep for the import would be satisfied by that.
    """
    out = []
    for name in sorted(os.listdir(HERE)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        if name == os.path.basename(__file__):
            continue
        try:
            src = open(os.path.join(HERE, name), encoding="utf-8").read()
        except OSError:
            continue
        if "require_dashboard(" in src:
            out.append(name)
    return tuple(out)


LIVE_SUITES = live_suites()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TheProbeAnswersTheOnlyQuestionItIsAsked(unittest.TestCase):

    def test_a_closed_port_is_down(self):
        self.assertFalse(livecheck.dashboard_is_up("http://127.0.0.1:%d" % free_port()))

    def test_an_open_port_is_up(self):
        """The positive control. A probe that always said 'down' would satisfy every other
        test in this file and turn the whole live suite off permanently."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: None, daemon=True).start()
        try:
            self.assertTrue(livecheck.dashboard_is_up("http://127.0.0.1:%d" % port))
        finally:
            srv.close()

    def test_it_does_not_hang_on_a_dead_port(self):
        """The defect that started this. A hang is worse than a failure, because it reads as
        slowness and the honest answer never arrives."""
        import time                                                # noqa: PLC0415
        started = time.time()
        livecheck.dashboard_is_up("http://127.0.0.1:%d" % free_port())
        self.assertLess(time.time() - started, livecheck.CONNECT_TIMEOUT + 2)


class EveryLiveSuiteRefusesToRunBlind(unittest.TestCase):
    """The guard is only worth anything if every suite that needs it actually calls it.

    Checked by RUNNING them with nothing listening, not by grepping for the import - a file
    can import the helper and never call it, and the grep would be satisfied.
    """

    def run_blind(self, name):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_BASE="http://127.0.0.1:%d" % free_port())
        return subprocess.run([sys.executable, name], cwd=HERE, capture_output=True,
                              text=True, env=env, encoding="utf-8", errors="replace",
                              timeout=45)

    def test_each_one_exits_2_and_says_why(self):
        for name in LIVE_SUITES:
            with self.subTest(suite=name):
                p = self.run_blind(name)
                out = (p.stdout or "") + (p.stderr or "")
                self.assertEqual(p.returncode, 2,
                                 "%s exited %s, not 2 (could-not-run)\n%s"
                                 % (name, p.returncode, out[-600:]))
                self.assertIn("COULD NOT RUN", out, name)
                self.assertIn("needs the dashboard running", out, name)

    def test_none_of_them_leaks_a_traceback_instead(self):
        """The old behaviour: a urllib stack trace, which a reader takes for a broken guard."""
        for name in LIVE_SUITES:
            with self.subTest(suite=name):
                p = self.run_blind(name)
                out = (p.stdout or "") + (p.stderr or "")
                self.assertNotIn("Traceback (most recent call last)", out, name)

    def test_none_of_them_hangs(self):
        """45s subprocess timeout above. One of these used to run until it was killed."""
        for name in LIVE_SUITES:
            with self.subTest(suite=name):
                self.run_blind(name)          # a timeout here raises and fails the test

    def test_the_roster_is_not_empty_and_every_file_in_it_exists(self):
        """The positive control on the discovery, and the exact defect it replaces.

        An empty roster would make every test in this class pass over nothing at all - a
        green check on zero coverage. And a roster naming a file that is not there produced
        the original failure: Python reports a missing file, and a missing file is reported
        as a failing suite.
        """
        self.assertTrue(LIVE_SUITES, "no suite was found calling require_dashboard - either "
                                     "the preflight is gone or the discovery is broken, and "
                                     "both look identical from here")
        for name in LIVE_SUITES:
            self.assertTrue(os.path.isfile(os.path.join(HERE, name)),
                            "%s is in the roster but not on disk" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
