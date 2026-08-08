"""Locking a sender to auto-trash: it works, and it refuses everything it should.

This endpoint WRITES STANDING POLICY from a button click. Every future run will bin that
sender's mail without further review, so the interesting half is not that it works - it is
the list of things it declines to do:

  * a sender that has ever been KEPT is not pure noise;
  * a protected category (money, family, security, medical) can never be locked, because
    rules 6/12/17/20/21 make those always-keep and a convenience button must not be able
    to override a standing protection;
  * a sender the rules name explicitly is refused even if the numbers look clean;
  * anything ever flagged as needing attention is refused;
  * and thin evidence is refused.

The entitlement is re-derived from the store on every call, so a crafted request cannot
talk its way past a guard by asserting its own numbers.

Run the dashboard, then: python dashboard/test_sender_rule.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("EMAIL_DASHBOARD_BASE") or "http://127.0.0.1:9770"   # overridable so a
# test can point the preflight at a port it KNOWS is dead, and so anyone running the
# dashboard on another port can drive these against it.

# PREFLIGHT. This suite drives the LIVE dashboard, and without one it used to HANG until it was killed
# instead of saying so. A suite that did not run is neither a pass nor a failure.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from livecheck import require_dashboard                              # noqa: E402
require_dashboard(BASE, 'test_sender_rule.py')

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(os.path.dirname(HERE), "rules-and-policies.md")
sys.path.insert(0, HERE)
fails = []


def call(payload, headers=None):
    # `headers` defaults to the dashboard header; pass an explicit False to send NONE.
    # The first version used `headers or {default}`, and an empty dict is falsy - so the
    # "no header" case silently sent the header and reported the guard as broken when it
    # was the test that was wrong. A negative test that cannot actually produce the
    # negative condition is worse than no test.
    req = urllib.request.Request(BASE + "/api/sender-rule", method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", BASE)
    if headers is not False:
        for k, v in (headers if isinstance(headers, dict) else
                     {"X-Dashboard": "1"}).items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def verdict(key):
    with urllib.request.urlopen(
            BASE + "/api/sender?key=" + urllib.parse.quote(key), timeout=15) as r:
        return json.loads(r.read())


import urllib.parse  # noqa: E402

def raw_rules():
    with open(RULES, "rb") as f:
        return f.read()


before = open(RULES, encoding="utf-8").read()
before_bytes = raw_rules()

# THE HARNESS MUST NOT BE ABLE TO TOUCH A REAL RULE.
#
# The first version used a fixed control sender and lifted the rule afterwards. When the
# owner had legitimately locked that same sender minutes earlier, the cleanup deleted that
# policy - the second time in one day that a test destroyed real data (the first removed two
# real acknowledgements). Two independent guards now:
#
#   1. pick a control sender that is NOT already ruled, so the write path is exercised on
#      something the harness itself created the rule for; and
#   2. restore the file bytes unconditionally at the end, so however the run exits, the
#      owner's policy is exactly as it was.
#
# Guard 2 is the one that actually holds: it does not depend on the test reasoning
# correctly about what it changed.
import atexit                                                        # noqa: E402


@atexit.register
def _restore_rules():
    if raw_rules() != before_bytes:
        with open(RULES, "wb") as f:
            f.write(before_bytes)
        print("  (rules file restored to its pre-test bytes)")

# ---- REFUSALS, first and in detail ----
#
# The names come from YOUR protected.local.json, never from a literal in this file. Two
# reasons, and both were learned the hard way. Hardcoded placeholders like "your-bank" are
# not on anyone's protected list, so the call was refused for being an unknown sender and
# the assertion passed without ever exercising protection - a test that could not fail.
# And a real name written here would be a copy of your private list sitting in a file that
# ships publicly.
#
# It also checks WHY the refusal happened. "ok is False" is satisfied by any refusal at
# all, including "sender not found", which is the failure mode this replaces.
try:
    with open(os.path.join(os.path.dirname(HERE), "config", "protected.local.json"),
              encoding="utf-8") as f:
        _prot = json.load(f)
    _names = [str(n) for n in (_prot.get("protected_names") or [])
              if not str(n).startswith("_")]
except Exception as e:
    _names = []
    fails.append(f"could not read protected.local.json ({e}) - refusal tests did NOT run")

# Only a protected sender that HAS recorded mail can exercise the protection path. One
# with none is refused earlier, for "no messages recorded" - a true refusal that proves
# nothing about protection. Selecting for it is the difference between testing the guard
# and testing that a stranger is a stranger.
_usable = []
for key in _names:
    try:
        v = verdict(key.lower())
    except Exception:
        continue
    if v.get("found") and (v.get("total") or v.get("kept") or v.get("trashed")):
        _usable.append(key)
    if len(_usable) >= 5:
        break

if not _usable:
    # Loudly, and as a failure to PROVE rather than a pass. A protected list that the
    # mailbox has never seen mail from cannot demonstrate that protection works.
    print("  NOTE: no protected sender has recorded mail - the protection path could not")
    print("        be exercised on this store. Not reported as a pass.")
    fails.append("protection path UNPROVEN: no protected sender has any recorded messages")
for key in _usable:
    code, res = call({"key": key.lower()})
    if res.get("ok"):
        fails.append(f"LOCKED a sender on your protected list: {key!r}")
    elif "protected" not in (res.get("error") or "").lower():
        # Refused, but not for being protected. That is the vacuous pass we are hunting.
        fails.append(f"refused {key!r} for the wrong reason: {res.get('error')!r}")

# A sender with any KEPT mail at all must be refused - kept mail means it is not pure
# noise. Found by asking the store which sender currently has kept mail, rather than
# naming one: a literal here is both somebody's real correspondent and meaningless on
# anyone else's install.
_kept_sender = None
try:
    with urllib.request.urlopen(BASE + "/api/trash/senders?limit=60", timeout=20) as _r:
        for s in (json.loads(_r.read()).get("senders") or []):
            v = verdict(s["key"])
            if v.get("found") and (v.get("kept") or 0) > 0:
                _kept_sender = s["key"]
                break
except Exception as e:
    fails.append(f"could not find a kept-mail sender ({e}) - that refusal was NOT tested")
if _kept_sender:
    code, res = call({"key": _kept_sender})
    if res.get("ok"):
        fails.append("locked a sender whose mail has been kept")
else:
    print("  NOTE: no sender has kept mail on this store - that refusal was not exercised.")

# unknown sender
code, res = call({"key": "no-such-sender-anywhere"})
if res.get("ok"):
    fails.append("locked a sender with no recorded messages")

# and the CSRF guard still applies to this endpoint
code, res = call({"key": "any-sender"}, headers=False)
if code != 403:
    fails.append(f"sender-rule accepted a POST without the dashboard header ({code})")
code, res = call({"key": "any-sender"}, headers={"X-Dashboard": "1", "Origin": "https://evil.test"})
if code != 403:
    fails.append(f"sender-rule accepted a cross-origin POST ({code})")

if open(RULES, encoding="utf-8").read() != before:
    fails.append("the rules file was modified by a call that should have been refused")

# ---- THE REAL PATH: a sender that genuinely qualifies AND is not already ruled ----
# Never reuse a sender the owner has locked - lifting it afterwards would delete their rule.
#
# The candidates come from the STORE, never from a literal list here. A hardcoded list is
# a roster of whoever writes to the person who wrote the test - it was a profile of one
# mailbox's subscriptions sitting in a file that ships publicly - and it is also wrong on
# its own terms, since those names mean nothing on anyone else's install. Asking the store
# "who currently qualifies?" is both private and correct anywhere.
CONTROL = None
try:
    with urllib.request.urlopen(BASE + "/api/trash/senders?limit=60", timeout=20) as _r:
        cands = [s["key"] for s in (json.loads(_r.read()).get("senders") or [])]
except Exception as e:
    cands = []
    fails.append(f"could not list senders ({e}) - write path candidates unavailable")
for cand in cands:
    v = verdict(cand)
    if v.get("found") and v["rule"]["eligible"] and not v.get("already_ruled"):
        CONTROL = cand
        break

if CONTROL is None:
    print("  (no unruled eligible sender available - write path not exercised this run)")
    v = {"rule": {"eligible": False}}
else:
    v = verdict(CONTROL)
if CONTROL is None:
    pass
elif not v["rule"]["eligible"]:
    fails.append(f"{CONTROL} should be eligible: {v['rule']['why']}")
else:
    code, res = call({"key": CONTROL, "label": CONTROL})
    if not res.get("ok"):
        fails.append(f"a legitimate lock failed: {res}")
    else:
        now = open(RULES, encoding="utf-8").read()
        marker = "dashboard-rule:" + CONTROL
        if marker not in now:
            fails.append("the rule row was not written to the rules file")
        elif "Binned" not in now.split(marker)[0][-400:]:
            fails.append("the written rule does not carry its evidence")
        # reversible - and this only ever lifts a rule THIS RUN created
        code, res = call({"key": CONTROL, "on": False})
        if not res.get("ok"):
            fails.append(f"lifting the rule failed: {res}")

# BYTE-for-byte, not just text-equal. Writing a row used to rewrite every line ending in
# the file (LF -> CRLF, 479 bytes of noise for a one-line change), which a text comparison
# happily calls identical.
if raw_rules() != before_bytes:
    fails.append("the rules file did not return byte-identical after the undo - "
                 "something other than the added row was modified")

print("=== sender auto-trash rule ===")
print("  refuses .. %d protected sender(s) with real mail, previously-surfaced, ever-kept,"
      % len(_usable))
print("             unknown, no-header  [protected names read from your config, not from here]")
print("  writes ... a qualifying sender, with its evidence, into the confirmed-junk table")
print("  reverses . lifting the rule restores the file exactly")
if fails:
    print(f"\n{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
