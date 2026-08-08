"""A suite that needs the running dashboard must say so in one line, and must not hang.

Three of the suites in this repo drive the live server over HTTP. That is the right way to
test them - the entitlement checks they cover live in the request handler, and a test that
called the functions directly would not exercise the guard a browser actually meets.

But when the dashboard is NOT running they behaved three different ways, all bad:

  * two dumped a raw urllib traceback, which reads as "the ack guard is broken" rather than
    "nothing was listening on the port";
  * one HUNG until it was killed, which is worse than either. A hang looks like slowness, so
    the honest reading - this could not run - never reaches anybody, and in a batch it just
    quietly eats the clock.

NOT RUN IS NOT PASSED, and it is not FAILED either; it is a third thing, and the only way it
stays honest is if it is loud and instant. So: probe the port with a short socket connect
before a single request is made, print what is wrong and how to fix it, and exit non-zero.

Non-zero, deliberately. Skipping would let `run_tests.py` report ALL PASS over a suite that
never executed a line, which is the exact shape of every defect this project has spent its
time on - a confident answer from an instrument that did not run.
"""
import socket
import sys
from urllib.parse import urlparse

CONNECT_TIMEOUT = 2.0


def dashboard_is_up(base, timeout=CONNECT_TIMEOUT):
    """True if something is accepting connections on that host/port.

    A socket connect rather than an HTTP request: it cannot hang on a half-open connection,
    it needs no handler to be working, and it answers the only question the caller has -
    is there a server there at all.
    """
    parts = urlparse(base if "//" in base else "http://" + base)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def require_dashboard(base, suite=None):
    """Exit(2) with a readable explanation unless the dashboard is reachable."""
    if dashboard_is_up(base):
        return True
    name = suite or sys.argv[0]
    print("")
    print("COULD NOT RUN: %s needs the dashboard running at %s" % (name, base))
    print("  Nothing is listening there, so not one assertion in this file was evaluated.")
    print("  This is NOT a pass and NOT a failure - it is a suite that did not run, and")
    print("  reporting it as either would be a confident answer from an instrument that")
    print("  never fired.")
    print("")
    print("  Start it with:  python dashboard/server.py --port %s"
          % (urlparse(base if "//" in base else "http://" + base).port or 9770))
    print("")
    sys.exit(2)
