"""A suite's result must not depend on what is in the owner's config directory. (F25)

The reported case: `test_concept_drift` hard-coded the label `inner-circle-fyi` and asserted
it was UNMAPPED as its control. That control holds only while no map on the machine has
taught that label - and the plugin's own guidance tells owners to teach exactly that kind of
label. So the test for "teaching the map repairs the store" failed on installs where the
owner had taught the map. The feature under test and the thing that broke the test were the
same action.

It failed in the useless direction, too: green on a bare install, red on a configured one.
A suite that is expected to have one failure is a suite that no longer means anything.

Two mechanisms, and this file exists to keep both honest:

  1. EMAIL_DASHBOARD_NO_LOCAL_CONFIG=1 makes the import-time config loaders behave as if the
     owner had no *.local.json. `tools/run_tests.py --no-local-config` runs the whole suite
     that way, and any suite whose RESULT differs between the two runs is reading live user
     config. That diagnostic is only worth anything if the flag actually does something,
     which is what the positive control below establishes.

  2. The strong form: a test that stands up its own install directory. Where that is
     available it wins, and the flag must not override it - the first attempt at F25
     redirected server.py's PROTECTED_FILE under the flag and broke eight assertions in
     test_separation.py, which had been isolating correctly all along.
"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def child(code, flag):
    """Run `code` in a fresh interpreter, with or without the flag.

    A subprocess rather than importlib.reload, because the thing under test happens at
    IMPORT time and a reloaded module in a process that has already imported it is not the
    same object graph. The bug being guarded against is specifically about import.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if flag:
        env["EMAIL_DASHBOARD_NO_LOCAL_CONFIG"] = "1"
    else:
        env.pop("EMAIL_DASHBOARD_NO_LOCAL_CONFIG", None)
    p = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True,
                       text=True, env=env, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise AssertionError("child failed:\n%s\n%s" % (p.stdout, p.stderr))
    return p.stdout.strip().splitlines()[-1]


def owner_has_local_concepts():
    return os.path.exists(os.path.join(HERE, "concepts.local.json"))


class TheFlagActuallyDoesSomething(unittest.TestCase):
    """The positive control. Without this, a flag that did nothing would pass every test
    here and the two-way suite comparison would agree for the wrong reason."""

    def test_local_labels_are_loaded_without_the_flag(self):
        if not owner_has_local_concepts():
            self.skipTest("no concepts.local.json on this machine - nothing to suppress, "
                          "so this control cannot run. Not run is not passed.")
        n = int(child("import concepts; print(concepts.LOCAL_LABELS_ADDED)", flag=False))
        self.assertGreater(n, 0, "concepts.local.json exists but taught nothing - either it "
                                 "is empty (then this control is vacuous) or loading broke")

    def test_the_flag_suppresses_them(self):
        n = int(child("import concepts; print(concepts.LOCAL_LABELS_ADDED)", flag=True))
        self.assertEqual(n, 0)

    def test_a_locally_taught_label_stops_resolving_under_the_flag(self):
        """LOCAL_LABELS_ADDED is a counter; this checks the map it is counting."""
        if not owner_has_local_concepts():
            self.skipTest("no concepts.local.json on this machine")
        with open(os.path.join(HERE, "concepts.local.json"), encoding="utf-8-sig") as f:
            local = json.load(f)
        taught = [str(l).lower()
                  for c, labels in (local.get("concepts") or {}).items()
                  if not str(c).startswith("_")
                  for l in (labels or []) if not str(l).startswith("_")]
        code = ("import concepts,sys;"
                "print(sum(1 for l in sys.argv[1:] "
                "if concepts.concept_of(l) != concepts.UNMAPPED))")
        # run with the labels passed on argv so no label is compiled into this file
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_NO_LOCAL_CONFIG="1")
        p = subprocess.run([sys.executable, "-c", code] + taught, cwd=HERE,
                           capture_output=True, text=True, env=env, encoding="utf-8")
        resolved_under_flag = int(p.stdout.strip().splitlines()[-1])
        env.pop("EMAIL_DASHBOARD_NO_LOCAL_CONFIG")
        p2 = subprocess.run([sys.executable, "-c", code] + taught, cwd=HERE,
                            capture_output=True, text=True, env=env, encoding="utf-8")
        resolved_normally = int(p2.stdout.strip().splitlines()[-1])
        self.assertGreater(resolved_normally, resolved_under_flag,
                           "the flag changed the counter but not the map")

    def test_categorize_falls_back_to_the_committed_example(self):
        got = child("import categorize,os;print(os.path.basename(categorize._loaded_from))",
                    flag=True)
        self.assertNotIn(".local.", got)


class NoSuiteReadsTheOwnersConfigDirectory(unittest.TestCase):
    """The guard that generalises: run the whole suite both ways and demand the same answer.

    This is the expensive test in the file and it is the one that would have caught F25
    before it shipped. It is also the one that catches the NEXT instance, which will not be
    about concepts.
    """

    def test_every_suite_agrees_with_and_without_local_config(self):
        if os.environ.get("EMAIL_DASHBOARD_SKIP_SELFTEST") == "1":
            self.skipTest("re-entrancy guard: this is the outer run")
        runner = os.path.join(ROOT, "tools", "run_tests.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   EMAIL_DASHBOARD_SKIP_SELFTEST="1")
        results = {}
        for flag in (False, True):
            argv = [sys.executable, runner, "--quiet"]
            if flag:
                argv.append("--no-local-config")
            p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env=env,
                               encoding="utf-8", errors="replace")
            results[flag] = (p.returncode, p.stdout)
        self.assertEqual(results[False][0], results[True][0],
                         "a suite passes with the owner's config and fails without it (or "
                         "the reverse) - it is reading live user configuration:\n"
                         + results[True][1][-3000:] + "\n---\n" + results[False][1][-3000:])
        self.assertEqual(results[False][0], 0, results[False][1][-3000:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
