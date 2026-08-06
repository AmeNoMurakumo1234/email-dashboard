"""The acknowledge endpoint: it works, and it cannot be driven by another web page.

WHY THE SECURITY HALF MATTERS. This server binds 127.0.0.1, which is routinely mistaken
for "private". It is not: localhost is reachable by ANY page the browser happens to be on.
A hostile site cannot READ the response (no CORS headers are sent) but a plain form POST
would still FIRE - so an unguarded write endpoint would let any website silence the CEO's
mail. That is the whole attack, and "it's only localhost" is exactly the reasoning that
leaves it open.

Run the dashboard, then: python dashboard/test_ack.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9770"
fails = []


def post(payload, headers=None, origin=None):
    req = urllib.request.Request(BASE + "/api/ack", method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ONE RECORD, NOT TWO. These tests write to the live store on purpose: a second database
# would be a second schema to keep in step, and the endpoint being tested (CSRF guards,
# real handler, real DB) is exactly what a copy would stop exercising.
#
# What makes that safe is not luck, it is discipline:
#   * every row this file creates carries MARKER, so its rows are identifiable as the
#     harness's beyond any doubt;
#   * cleanup deletes BY EXACT KEY, only keys this run created - never by time, never by
#     "everything recent";
#   * and the run FAILS if it leaves residue behind.
#
# That last part exists because the real incident was not a test writing to the live store.
# It was me tidying up afterwards by deleting every ack stamped after a wall-clock time I
# picked - a guess about WHEN rather than evidence about WHO - which removed two of the
# owner's own acknowledgements. A destructive cleanup must be able to name what it created.
MARKER = "zz-harness-do-not-keep"
CREATED = []


def track(payload):
    CREATED.append((payload.get("kind", "message"), payload))
    return payload


ROW = {"kind": "message", "message_id": f"<{MARKER}-1@example.invalid>",
       "account": f"{MARKER}@example.invalid",
       "sender": f"{MARKER} Sender <t@example.invalid>",
       "subject": f"{MARKER} Payment due 08/21 for $12.34"}

# ---- security: the guards must actually refuse ----
code, _ = post(ROW)                                   # no dashboard header
if code != 403:
    fails.append(f"a POST WITHOUT the dashboard header was accepted ({code}) - any web "
                 f"page could drive this endpoint")

code, _ = post(ROW, headers={"X-Dashboard": "1"}, origin="https://evil.example")
if code != 403:
    fails.append(f"a cross-origin POST was accepted ({code})")

# a GET must never write, however the URL is reached
try:
    with urllib.request.urlopen(BASE + "/api/ack", timeout=10) as r:
        body = json.loads(r.read())
    if body.get("ok"):
        fails.append("GET /api/ack performed a write - a link or image tag could fire it")
except urllib.error.HTTPError as e:
    if e.code != 404:
        fails.append(f"GET /api/ack returned {e.code}, expected 404")

# ---- function: the real path works, and is reversible ----
OK = {"X-Dashboard": "1"}
code, res = post(ROW, headers=OK, origin=BASE)
if code != 200 or not res.get("acked"):
    fails.append(f"a legitimate acknowledge failed: {code} {res}")

code, res = post(dict(ROW, on=False), headers=OK, origin=BASE)
if code != 200 or res.get("acked"):
    fails.append(f"un-acknowledging failed: {code} {res}")

# ---- thread scope: an amount/date change must not escape the acknowledgement ----
THREAD = dict(ROW, kind="thread")
post(THREAD, headers=OK, origin=BASE)
try:
    with urllib.request.urlopen(BASE + "/api/acks", timeout=10) as r:
        keys = {i["key"] for i in json.loads(r.read())["items"] if i["kind"] == "thread"}
except Exception as e:
    keys = set()
    fails.append(f"could not read back acks: {e}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import ack_key                                          # noqa: E402

# Same MARKER on both sides, or the shapes differ for a reason that has nothing to do with
# the date and amount this case is actually about.
next_month = ack_key("thread", None, ROW["sender"],
                     f"{MARKER} Payment due 09/21 for $99.99")
if next_month not in keys:
    fails.append("next month's notice, with a different date and amount, would NOT be "
                 "covered by the acknowledgement - the thread scope is not collapsing")
different = ack_key("thread", None, ROW["sender"], f"{MARKER} Your account was locked")
if different in keys:
    fails.append("an unrelated subject collapsed into the same thread key")

post(dict(THREAD, on=False), headers=OK, origin=BASE)     # clean up

# ---- an item with NO Message-ID must still be acknowledgeable ----
# Most historical rows cannot be linked to a message, and the first build disabled the
# button for exactly those - so the items that could not be opened were also the ones that
# could not be dismissed, and they piled up. Acknowledging is a statement about attention,
# not about whether the mail can be fetched.
UNLINKED = {"kind": "message", "message_id": None,
            "account": f"{MARKER}@example.invalid",
            "sender": f"{MARKER} Example Service <no-reply@example.invalid>",
            "subject": f"{MARKER} Security alert: new trusted device added"}
code, res = post(UNLINKED, headers=OK, origin=BASE)
if code != 200 or not res.get("acked"):
    fails.append(f"an unlinked row could not be acknowledged: {code} {res}")
if res.get("key") and not str(res["key"]).startswith("row:"):
    fails.append(f"unlinked ack did not use the row-identity key: {res.get('key')!r}")

# and it must be ONE item, not the whole series: a different subject from the same sender
# must get a different key.
OTHER_SUBJ = f"{MARKER} Your invoice is ready"
_, other = post(dict(UNLINKED, subject=OTHER_SUBJ), headers=OK, origin=BASE)
if other.get("key") == res.get("key"):
    fails.append("two different subjects from one sender collapsed to the same message ack")
post(dict(UNLINKED, on=False), headers=OK, origin=BASE)
post(dict(UNLINKED, subject=OTHER_SUBJ, on=False), headers=OK, origin=BASE)

# ---- RESIDUE CHECK: prove this run left the owner's record exactly as it found it ----
try:
    with urllib.request.urlopen(BASE + "/api/acks", timeout=10) as r:
        left = [i for i in json.loads(r.read())["items"]
                if MARKER in (i.get("key") or "") or MARKER in (i.get("subject") or "")
                or MARKER in (i.get("sender") or "") or MARKER in (i.get("account") or "")]
except Exception as e:
    left = []
    fails.append(f"could not verify cleanup: {e}")
if left:
    fails.append(f"{len(left)} harness row(s) left in the live store: "
                 f"{[i['key'][:40] for i in left]}")

print("=== acknowledge endpoint ===")
print("  security . no header -> refused; cross-origin -> refused; GET never writes")
print("  function . acknowledge works and is reversible")
print("  scope .... a changed date/amount stays covered; an unrelated subject does not")
print("  unlinked . an item with no Message-ID is still dismissable, as ONE item")
print("  residue .. every row this run created is gone from the live store")
if fails:
    print(f"\n{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
