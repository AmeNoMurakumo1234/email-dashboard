"""The new-host panel: what it stores, what it shows, and what it refuses to forget.

THE PROPERTY THAT MATTERS is not "does it display a row". It is that this panel can live
permanently on the page WITHOUT becoming noise, and that rests on three things a test can
actually pin down:

  1. One row per (sender, host) - not per message. The question "is this host normal for
     this sender" is asked once, so a sender that raises it in ten messages is still one
     thing to look at.
  2. A verdict survives the next scan. If a re-scan could re-open something already ruled
     on, every cleared pairing would come back tomorrow and the panel would be exactly the
     noise it exists to cut through.
  3. `open` contains only unreviewed pairings, because the panel hides on `open` being
     empty. If a reviewed row leaked into `open`, the panel would never hide again.

Also guarded: the write endpoint carries the same CSRF posture as the other writers, and a
verdict is reversible - a wrong ruling has to be undoable, same as an acknowledgement.

Run the dashboard, then: python dashboard/test_host_flags.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db                                                            # noqa: E402
import check_new_hosts                                               # noqa: E402

BASE = os.environ.get("EMAIL_DASHBOARD_BASE") or "http://127.0.0.1:9770"   # overridable so a
# test can point the preflight at a port it KNOWS is dead, and so anyone running the
# dashboard on another port can drive these against it.

# PREFLIGHT. This suite drives the LIVE dashboard, and without one it used to dump a raw urllib traceback
# instead of saying so. A suite that did not run is neither a pass nor a failure.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from livecheck import require_dashboard                              # noqa: E402
require_dashboard(BASE, 'test_host_flags.py')

KEY = "test-sender-do-not-ship"
HOST_A = "a.invalid-test-host.example"
HOST_B = "b.invalid-test-host.example"
fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())


def post(payload, headers=None, origin=None, path="/api/host-review"):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cleanup():
    conn = db.connect()
    conn.execute("DELETE FROM host_flags WHERE sender_key = ?", (KEY,))
    conn.commit()
    conn.close()


def rows_for(key):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM host_flags WHERE sender_key = ?", (key,))]
    finally:
        conn.close()


cleanup()
print("\n1. storing findings")

finding = {"account": "test@example.invalid", "sender": KEY,
           "subject": "a subject", "profile_messages": 12,
           "new_hosts": [HOST_A, HOST_B], "weighty": False}

conn = db.connect()
saved, already = check_new_hosts._save(conn, [finding], run_date="2026-01-01")
conn.close()
check("two new hosts store as two rows", saved == 2 and already == 0, f"{saved}/{already}")
check("one row per (sender, host)", len(rows_for(KEY)) == 2, str(len(rows_for(KEY))))

# The same finding again is the SAME question, not a second one.
conn = db.connect()
saved2, already2 = check_new_hosts._save(conn, [finding], run_date="2026-01-02")
conn.close()
check("a re-scan does not duplicate", saved2 == 0 and already2 == 2, f"{saved2}/{already2}")
check("a re-scan bumps times_seen", all(r["times_seen"] == 2 for r in rows_for(KEY)))
check("a re-scan moves last_flagged",
      all(r["last_flagged"] == "2026-01-02" and r["first_flagged"] == "2026-01-01"
          for r in rows_for(KEY)))

print("\n2. the open list drives whether the panel is visible at all")
open_keys = {(r["sender_key"], r["host"]) for r in get("/api/new-hosts")["open"]}
check("an unreviewed pairing is open", (KEY, HOST_A) in open_keys)

print("\n3. ruling on a pairing")
code, res = post({"sender_key": KEY, "host": HOST_A, "verdict": "cleared",
                  "note": "test"}, headers={"X-Dashboard": "1"})
check("a verdict is accepted", code == 200 and res.get("ok"), json.dumps(res))
data = get("/api/new-hosts?show=all")
open_keys = {(r["sender_key"], r["host"]) for r in data["open"]}
rev_keys = {(r["sender_key"], r["host"]) for r in data.get("reviewed", [])}
check("a ruled pairing leaves the open list", (KEY, HOST_A) not in open_keys)
check("a ruled pairing appears under reviewed", (KEY, HOST_A) in rev_keys)
check("its sibling host is untouched", (KEY, HOST_B) in open_keys)

# THE ONE THAT KEEPS THE PANEL QUIET. If a scan could re-open a ruling, every cleared
# pairing would be back tomorrow morning.
conn = db.connect()
check_new_hosts._save(conn, [finding], run_date="2026-01-03")
conn.close()
open_keys = {(r["sender_key"], r["host"]) for r in get("/api/new-hosts")["open"]}
check("a later scan does NOT re-open a ruled pairing", (KEY, HOST_A) not in open_keys)

print("\n4. a ruling is reversible")
code, res = post({"sender_key": KEY, "host": HOST_A, "verdict": None},
                 headers={"X-Dashboard": "1"})
open_keys = {(r["sender_key"], r["host"]) for r in get("/api/new-hosts")["open"]}
check("clearing the verdict puts it back", code == 200 and (KEY, HOST_A) in open_keys)

print("\n5. refusals")
code, res = post({"sender_key": KEY, "host": HOST_A, "verdict": "probably-fine"},
                 headers={"X-Dashboard": "1"})
check("an unknown verdict is refused", not res.get("ok"), json.dumps(res))
code, res = post({"sender_key": "nobody", "host": "nowhere", "verdict": "cleared"},
                 headers={"X-Dashboard": "1"})
check("an unknown pairing is refused", not res.get("ok"), json.dumps(res))
code, res = post({"sender_key": KEY, "host": HOST_A, "verdict": "cleared"})
check("a write with no dashboard header is refused (CSRF)", code == 403, str(code))
code, res = post({"sender_key": KEY, "host": HOST_A, "verdict": "cleared"},
                 headers={"X-Dashboard": "1"}, origin="https://evil.example")
check("a cross-origin write is refused", code == 403, str(code))

try:
    get("/api/new-hosts?show=sideways")
    check("a bad `show` value is rejected, not silently defaulted", False)
except urllib.error.HTTPError as e:
    check("a bad `show` value is rejected, not silently defaulted", e.code == 400, str(e.code))

print("\n6. the empty state names its own reach")
d = get("/api/new-hosts")
check("reports how many senders have enough history to judge",
      isinstance(d.get("profiled_senders"), int) and d["profiled_senders"] > 0,
      str(d.get("profiled_senders")))
check("reports how many pairings were ever flagged",
      isinstance(d.get("ever_flagged"), int), str(d.get("ever_flagged")))

cleanup()
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
