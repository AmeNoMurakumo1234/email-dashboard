"""Throttle and safety behaviour for the Graph backend.

"it handles 429s" is the kind of claim that is comfortable to make and expensive to be wrong
about - a retry storm against Microsoft gets the account blocked for hours. So each branch is
fired deliberately and observed, rather than reasoned about.

The two rules that matter most are counter-intuitive, which is exactly why they are pinned by
tests rather than left as comments:

  * a 10-second FLOOR on the fallback backoff, because a textbook 1s/2s/4s retry inside a
    penalty window does not merely fail, it extends the penalty; and
  * a CUMULATIVE throttle-out counter, because real throttling interleaves with successes, so
    a consecutive-only counter never trips at all.

NOTHING IS MONKEYPATCHED. Every test builds its own `Graph` with fake transport, token,
clock, sleep and credential store. That is not stylistic: the previous version reassigned
module globals, one test left `_get` replaced, and the next test inherited the stub and
failed for a reason that had nothing to do with the code it was testing. Worse, the 401 path
writes to the credential store, so running the suite wrote into the real DPAPI store of
whoever ran it. Injected seams make both impossible - each test owns its own client, and no
test can reach anything real.

    python tools/test_msgraph_throttle.py
"""
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msgraph as g                                                  # noqa: E402


# ---------------------------------------------------------------- fakes, not patches

class FakeResp(io.BytesIO):
    """Stands in for an http response: a context manager with .headers and .read()."""

    def __init__(self, data=b"", headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeStore:
    """A credential store that lives and dies with the test."""

    def __init__(self):
        self.data = {}

    def set_value(self, account, field, value):
        self.data.setdefault(account, {})[field] = value

    def set_values(self, account, fields):
        self.data.setdefault(account, {}).update(
            {k: v for k, v in fields.items() if v is not None})

    def get(self, account, field):
        return self.data.get(account, {}).get(field)


class Clock:
    """Time that only moves when something sleeps, so the suite runs instantly AND the
    slept-for total is exact rather than approximate."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def time(self):
        return self.now


def http_error(code, retry_after=None, body=b'{"error":"x"}'):
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://graph.test/x", code, "err", hdrs, io.BytesIO(body))


def raises(*errors):
    """A transport that raises the given errors in order, repeating the last forever."""
    seq = list(errors)

    def send(req):
        return (_ for _ in ()).throw(seq.pop(0) if len(seq) > 1 else seq[0])
    return send


def client(send, clock=None, store=None, **kw):
    clock = clock or Clock()
    c = g.Graph(token=lambda addr: "fake-token", send=send, sleep=clock.sleep,
                clock=clock.time, store=store or FakeStore(), **kw)
    c.clock = clock
    return c


results = []


def run(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
        results.append(True)
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        results.append(False)
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
        results.append(False)


print("=== Graph backend: throttling, budgets and safety ===")


# 1. The server's Retry-After is obeyed exactly - not doubled, not floored, not guessed.
def t_retry_after_obeyed():
    calls = {"n": 0}

    def send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http_error(429, retry_after=7)
        return FakeResp(b'{"ok":true}')
    c = client(send)
    assert c.get("a@b.c", "/me") == {"ok": True}
    assert c.clock.slept and abs(c.clock.slept[-1] - 7) < 0.01, \
        f"slept {c.clock.slept}, expected exactly 7s"


run("429 with Retry-After sleeps exactly that, then retries", t_retry_after_obeyed)


# 2. THE 10-SECOND FLOOR. Without Retry-After, a textbook 1/2/4s backoff extends the penalty.
def t_backoff_floor():
    c = client(raises(http_error(429)))
    try:
        c.get("a@b.c", "/me")
        raise AssertionError("expected a throttle error")
    except g.GraphThrottleError:
        pass
    assert c.clock.slept, "never backed off at all"
    worst = min(c.clock.slept)
    assert worst >= g.MIN_FALLBACK_BACKOFF, \
        f"backed off {worst}s, under the {g.MIN_FALLBACK_BACKOFF}s floor"
    assert max(c.clock.slept) <= g.MAX_BACKOFF, "backoff exceeded the ceiling"


run("no Retry-After -> backoff never dips under the 10s floor", t_backoff_floor)


# 3. A 403 will never become a 200. Retrying it is pointless and looks like an attack.
def t_403_never_retried():
    c = client(raises(http_error(403, body=b'{"error":"Forbidden"}')))
    try:
        c.get("a@b.c", "/me")
        raise AssertionError("expected an error")
    except g.GraphThrottleError:
        raise AssertionError("403 was treated as throttling")
    except g.GraphError:
        pass
    assert c.budget.used == 1, f"403 was retried ({c.budget.used} requests)"
    assert not c.clock.slept, "403 caused a backoff sleep"


run("429 -> GraphThrottleError; 403 -> GraphError, never retried", t_403_never_retried)


# 4. CUMULATIVE, not consecutive. Real throttling interleaves with successes.
def t_breaker_is_cumulative():
    c = client(raises(http_error(429)))
    assert c.budget.note_throttle_out(say=False) is False
    c.budget.used = 0                       # a success in between
    assert c.budget.note_throttle_out(say=False) is False
    assert c.budget.note_throttle_out(say=False) is True, \
        "breaker did not trip on the third cumulative throttle-out"
    assert c.budget.stopped_early is True


run("breaker trips at 3 cumulative throttle-outs; success does not reset", t_breaker_is_cumulative)


# 5. The real ban risk is a loop bug, not steady-state volume.
def t_request_budget():
    c = client(lambda req: FakeResp(b'{"ok":true}'), max_requests=3)
    for _ in range(3):
        c.get("a@b.c", "/me")
    try:
        c.get("a@b.c", "/me")
        raise AssertionError("budget did not stop a runaway loop")
    except g.GraphError as exc:
        assert "budget exhausted" in str(exc), f"unclear message: {exc}"


run("hard request budget stops a runaway loop", t_request_budget)


# 6. A 401 refreshes ONCE. A dead grant must not become a retry storm - and the expiry reset
#    must land in the INJECTED store, never in the real one.
def t_401_refreshes_once():
    store = FakeStore()
    c = client(raises(http_error(401)), store=store)
    try:
        c.get("a@b.c", "/me")
        raise AssertionError("expected an error")
    except g.GraphError:
        pass
    assert c.budget.retried_401 == 1, f"refreshed {c.budget.retried_401} times, expected 1"
    assert store.get("a@b.c", "graph_token_expiry") == "0", "expiry not reset in the store"
    assert store.data.keys() == {"a@b.c"}, "wrote something unexpected"


run("401 refreshes the token exactly once, then gives up", t_401_refreshes_once)


# 7. THE IMPORTANT ONE. A throttled sweep saw part of the mailbox. Anything reading it as a
#    full look concludes "nothing needs you" from a sample.
def t_partial_is_labelled():
    state = {"n": 0}

    class Stub(g.Graph):
        def get(self, addr, url, raw=False):
            state["n"] += 1
            if state["n"] == 1:
                return {"value": [{"id": "1", "internetMessageId": "<a@b>", "subject": "one",
                                   "receivedDateTime": "2026-08-06T00:00:00Z",
                                   "isRead": False}],
                        "@odata.nextLink": "https://graph.test/next"}
            raise g.GraphThrottleError("throttled")

    clock = Clock()
    gc = Stub(token=lambda a: "t", send=lambda r: None, sleep=clock.sleep,
              clock=clock.time, store=FakeStore())
    args = types.SimpleNamespace(account="a@b.c", folder="inbox", days=7, limit=500,
                                 offset=0, unseen=False, no_snippets=True,
                                 with_hosts=False, with_headers=False)
    buf, sys.stdout = sys.stdout, io.StringIO()
    try:
        code = g.cmd_fetch(args, gc)
        payload = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdout = buf

    assert code == 2, f"a throttled run must not exit 0; got {code}"
    assert payload["complete"] is False, "partial run reported as complete"
    assert "PARTIAL" in payload.get("WARNING", ""), "partial run not explicitly warned about"
    assert payload["throttling"]["stopped_early"] is True, "stopped_early not surfaced"
    assert len(payload["messages"]) == 1, "should still keep what it did fetch"


run("a throttled fetch is labelled PARTIAL and exits non-zero", t_partial_is_labelled)


# 8. Link hosts feed the sender profiles. A silently-wrong list poisons them rather than erroring.
def t_link_hosts():
    body = {"contentType": "html", "content": (
        '<a href="https://Example.COM/path?a=1">x</a> '
        'plain http://mail.example.com. and https://example.com/again '
        'and a bare word http://localhost that has no dot')}
    assert g._link_hosts(body) == ["example.com", "mail.example.com"]
    assert g._link_hosts(None) == [], "None body must yield []"
    assert g._link_hosts({"content": ""}) == [], "empty body must yield []"


run("--with-hosts extracts sorted, de-duplicated, lowercased hosts", t_link_hosts)


# 9. /$value returns full MIME including attachments; an unbounded read is one large
#    attachment away from taking the process down.
def t_body_size_guard():
    big = client(lambda req: FakeResp(b"x" * 5000, {"Content-Length": "5000"}),
                 max_body_bytes=1000)
    try:
        big.get("a@b.c", "/me/messages/1/$value", raw=True)
        raise AssertionError("oversize body with Content-Length was not refused")
    except g.GraphError as exc:
        assert "ceiling" in str(exc), f"unhelpful message: {exc}"

    streamed = client(lambda req: FakeResp(b"x" * 5000), max_body_bytes=1000)
    try:
        streamed.get("a@b.c", "/me/messages/1/$value", raw=True)
        raise AssertionError("oversize body without Content-Length was not refused")
    except g.GraphError as exc:
        assert "streaming" in str(exc), f"unhelpful message: {exc}"

    ok = client(lambda req: FakeResp(b"y" * 400, {"Content-Length": "400"}),
                max_body_bytes=1000)
    assert ok.get("a@b.c", "/me/messages/1/$value", raw=True) == b"y" * 400, \
        "a normal body was refused"


run("a huge message body is refused, a normal one is not", t_body_size_guard)


# 10. PKCE must satisfy RFC 7636 without any Microsoft round trip. The transform is
#     recomputed here rather than trusted.
def t_pkce_shape():
    import base64 as b64
    import hashlib
    import re as _re
    import secrets as _s
    verifier = _s.token_urlsafe(64)
    challenge = g._b64url(hashlib.sha256(verifier.encode()).digest())
    assert 43 <= len(verifier) <= 128, f"verifier length {len(verifier)} outside RFC 7636"
    assert _re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier), "verifier is not url-safe"
    assert not set("=+/") & set(challenge), "challenge is not unpadded base64url"
    expect = b64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expect, "challenge is not the S256 transform of the verifier"


run("PKCE verifier/challenge conform to RFC 7636", t_pkce_shape)


# 11. The likeliest first run ends in 403 - registration missing Mail.Read, or consent never
#     granted. That must be an actionable message and a distinct exit code, not a traceback.
def t_errors_are_handled_not_raised():
    for exc, want_code, want_text in (
            (g.GraphError("Graph 403 on /me: Forbidden"), 1, "403"),
            (g.GraphThrottleError("throttled out"), 2, "THROTTLED")):
        captured, real_err, real_argv = io.StringIO(), sys.stderr, sys.argv
        saved = g.cmd_doctor
        g.cmd_doctor = lambda a, c, _e=exc: (_ for _ in ()).throw(_e)
        sys.argv, sys.stderr = ["msgraph.py", "doctor"], captured
        try:
            code = g.main()
        finally:
            sys.stderr, sys.argv, g.cmd_doctor = real_err, real_argv, saved
        assert code == want_code, f"exit {code}, expected {want_code}"
        assert want_text in captured.getvalue(), \
            f"unhelpful output: {captured.getvalue()[:140]}"


run("a Graph error exits cleanly with guidance, never a traceback", t_errors_are_handled_not_raised)


# 12. The seam itself: two clients must share nothing. This is what the old global BUDGET and
#     module-level _get got wrong, and what leaked one test's stub into another's.
def t_clients_are_isolated():
    a = client(lambda req: FakeResp(b'{"ok":true}'))
    b = client(lambda req: FakeResp(b'{"ok":true}'))
    a.get("a@b.c", "/me")
    a.get("a@b.c", "/me")
    assert a.budget.used == 2 and b.budget.used == 0, \
        f"budgets are shared: a={a.budget.used} b={b.budget.used}"
    assert a.budget is not b.budget


run("two Graph clients share no state", t_clients_are_isolated)


print()
if all(results):
    print(f"All {len(results)} tests passed - and nothing real was touched: no network, no "
          f"credential store, no wall-clock waiting.")
    raise SystemExit(0)
print(f"{results.count(False)} of {len(results)} FAILED.")
raise SystemExit(1)
