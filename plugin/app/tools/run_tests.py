"""Run every suite in the repo, and say what was run - not just that it passed.

There was no runner. Twenty-three suites were being run by hand, which meant the count of
suites was whatever I remembered it to be, and a suite that stopped being run would never
announce itself. A suite nobody runs is indistinguishable from a suite that passes.

Two things this deliberately does:

  * It DISCOVERS suites rather than listing them, so a new test_*.py is picked up without
    anyone remembering to add it here. The inverse - a hard-coded list - is how a suite
    goes quiet.
  * It reports the count and the roster, every time. `ALL PASS` on its own does not say
    whether it passed twenty-three suites or two.

`--no-local-config` runs with EMAIL_DASHBOARD_NO_LOCAL_CONFIG=1, which makes the config
loaders skip the owner's *.local.json files. Compare the two runs: any suite whose result
DIFFERS between them is reading live user configuration, and will pass or fail depending on
how the machine it runs on is set up. See F25.
"""
import argparse
import os
import subprocess
import sys
import time

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "dashboard"))
from consoleio import safe_console            # noqa: E402
safe_console()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = ("dashboard", "tools", "plugin")


def discover():
    found = []
    for d in DIRS:
        full = os.path.join(ROOT, d)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if name.startswith("test_") and name.endswith(".py"):
                found.append((d, name))
    return found


def run_one(d, name, env):
    started = time.time()
    proc = subprocess.run([sys.executable, name], cwd=os.path.join(ROOT, d),
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")
    tail = (proc.stderr or "") + (proc.stdout or "")
    # unittest writes its summary to stderr; keep the last few lines for a failure report
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return proc.returncode, time.time() - started, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-local-config", action="store_true",
                    help="run with the owner's *.local.json files ignored")
    ap.add_argument("--quiet", action="store_true", help="only report failures")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"          # a stale .pyc has burned this project once
    env["PYTHONIOENCODING"] = "utf-8"
    if args.no_local_config:
        env["EMAIL_DASHBOARD_NO_LOCAL_CONFIG"] = "1"
    else:
        env.pop("EMAIL_DASHBOARD_NO_LOCAL_CONFIG", None)

    suites = discover()
    if not suites:
        print("NO SUITES FOUND - that is a failure, not a pass", file=sys.stderr)
        return 2

    # EXIT 2 MEANS "COULD NOT RUN", and it is reported as its own thing.
    #
    # Three suites drive the live dashboard, and folding "nothing was listening on the port"
    # into "FAILED" hides the one fact the reader needs: those assertions were never
    # evaluated. Folding it into "passed" would be far worse - it is how a green board comes
    # to cover code nothing has executed - so it still fails the run. Named, counted, and
    # never silent.
    failed, unrun = [], []
    for d, name in suites:
        rc, secs, lines = run_one(d, name, env)
        mark = {0: "ok  ", 2: "SKIP"}.get(rc, "FAIL")
        if rc == 2:
            unrun.append((d, name, lines))
        elif rc != 0:
            failed.append((d, name, lines))
        if not args.quiet or rc != 0:
            print("%s %-28s %-10s %5.1fs" % (mark, name, d, secs))

    print()
    print("%d suite(s)%s" % (len(suites),
                             ", local config IGNORED" if args.no_local_config else ""))
    if unrun:
        print("COULD NOT RUN: %d - these need the dashboard running; not one assertion in "
              "them was evaluated" % len(unrun))
        for _, name, _ in unrun:
            print("   - %s" % name)
    if failed:
        print("FAILED: %d" % len(failed))
        for d, name, lines in failed:
            print("\n--- %s/%s" % (d, name))
            for ln in lines[-25:]:
                print("    " + ln)
    if failed or unrun:
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
