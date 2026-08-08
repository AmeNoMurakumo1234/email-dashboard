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

BASE = os.environ.get("EMAIL_DASHBOARD_BASE") or "http://127.0.0.1:9770"   # overridable so a
# test can point the preflight at a port it KNOWS is dead, and so anyone running the
# dashboard on another port can drive these against it.

# PREFLIGHT. This suite drives the LIVE dashboard, and without one it used to dump a raw urllib traceback
# instead of saying so. A suite that did not run is neither a pass nor a failure.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from livecheck import require_dashboard                              # noqa: E402
require_dashboard(BASE, 'test_ack.py')

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL"), name, ("" if cond else f"-> {detail}"))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------------------
# A THREAD IS A SUBJECT, NOT A PERSON - and this half needs no server, so it always runs.
#
# The thread key used to include the sender, so every participant in one conversation got
# a distinct key: acknowledging a thread silenced exactly one person in it while everyone
# else kept arriving. It never errored - the API returned ok and the row rendered as
# acknowledged. Reported from a live four-participant thread.
#
# The reply prefix was the second half of the same bug: subject_shape() had no rule for
# Re:/Fwd:, so an original and its own replies did not share a shape even from one sender.
# That also feeds api_repeats, which meant the repeat-collapsing view was splitting a
# notice from its own follow-ups.
#
# This is the test the report said was missing: "the current behaviour is consistent with a
# test that only ever passed one sender."
# ---------------------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server                                                        # noqa: E402

print("=== a multi-participant thread is ONE thread ===")
THREAD_SUBJECT = "Response Requested: vendor seat audit"
PARTICIPANTS = [
    ("colleague-a@example.com", THREAD_SUBJECT),
    ("someone@example.com", f"Re: {THREAD_SUBJECT}"),
    ("colleague-c@partner.example", f"RE: {THREAD_SUBJECT}"),
    ("colleague-d@personal.example", f"Re: Re: Fwd: {THREAD_SUBJECT}"),
    ("colleague-a@example.com", f"AW: {THREAD_SUBJECT}"),
]
keys = {server.ack_key("thread", None, s, subj, "me@example.com")
        for s, subj in PARTICIPANTS}
check(f"{len(PARTICIPANTS)} participants and reply forms share ONE thread key",
      len(keys) == 1, keys)

check("a different subject is a different thread",
      server.ack_key("thread", None, "a@b.example", "Something else", "me@example.com")
      not in keys)
check("the same thread in another mailbox is scoped separately",
      server.ack_key("thread", None, "colleague-a@example.com", THREAD_SUBJECT,
                     "other@example.com")
      not in keys)

print("\n=== subject_shape strips reply prefixes (this also feeds api_repeats) ===")
base = server.subject_shape("Notice: your statement is ready")
for prefix in ("Re: ", "RE: ", "Fwd: ", "FW: ", "AW: ", "SV: ", "Re: Re: Fwd: ", "Re[2]: "):
    check(f"{prefix!r} collapses onto the original",
          server.subject_shape(prefix + "Notice: your statement is ready") == base,
          server.subject_shape(prefix + "Notice: your statement is ready"))

print("\n=== ...and does not eat subjects that merely start with those letters ===")
for subject, must_keep in (("Re-engineering the onboarding flow", "engineering"),
                           ("Fwd Thinking Ltd invoice", "fwd thinking"),
                           ("Resolution required: outage", "resolution"),
                           ("Review: Q3 numbers", "review")):
    shaped = server.subject_shape(subject)
    check(f"{subject!r} survives", must_keep in shaped, shaped)


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
# The account is part of a thread key now (a thread is a subject, scoped to the mailbox
# it landed in), so it must be passed here exactly as api_ack passes it from the payload -
# otherwise this recomputes a different key and reports a scope failure that is really a
# harness failure.
next_month = ack_key("thread", None, ROW["sender"],
                     f"{MARKER} Payment due 09/21 for $99.99", ROW.get("account"))
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
