"""Microsoft Graph backend for the email-cleanup routine.

NOT YET VERIFIED AGAINST A LIVE TENANT. Read this before relying on it.

Every branch below is covered by tests, but those tests use a fake transport: no request in
this file has ever reached graph.microsoft.com, no token has ever been issued, and no real
mailbox has ever been read. The throttle rules encode behaviour observed on a real Graph
workload, but the code implementing them here has never itself been throttled. Specifically
unknown: whether the PKCE round trip completes against Entra, whether $skip paging behaves as
assumed on /messages, and whether a real 429 is shaped the way the tests assume.

That is a deliberate disclosure rather than a disclaimer. A backend that reports mail is one
whose silence a person will eventually trust, and "it has tests" is not the same claim as "it
has worked". Expect one debugging session; the IMAP backend remains the verified path.

WHY THIS EXISTS. The shipped tool speaks IMAP only, and IMAP is a dead end for Microsoft 365
organisations: tenants disable it as a hardening step (usually right after a phishing
incident), it is an admin-only setting a normal employee can neither inspect nor change, and
the per-user app-registration model it implies does not scale past one enthusiast. Graph is
not gated by the IMAP flag, is consentable once for a whole tenant, and works on every
device.

DELIBERATELY ABSENT: `act`. This backend cannot move, delete or flag anything. The routine is
report-only for now, and code that does not exist is a stronger guarantee than code that
exists and is not called. Add it when the owner decides the routine has earned hands, not
before.

Auth is authorization-code + PKCE, not device code. Device code is the flow used in
device-code phishing and security teams alert on it specifically; generating that signal for
a legitimate mail tool is an unforced cost. PKCE also runs through the system browser, so it
inherits whatever session and Conditional Access state already lets the user's webmail work.

Config (config/accounts.json):
    {
      "ms_client_id": "<application (client) id>",
      "ms_authority": "<tenant guid>",
      "accounts": [{ "email": "...", "provider": "graph", "role": "primary" }]
    }

Note `provider: "graph"` needs no `imap_host`.

CLI (mirrors mailtool.py so ingest.py, the dashboard and the skills consume it unchanged):
    python tools/msgraph.py auth   --account EMAIL
    python tools/msgraph.py doctor [--account EMAIL]
    python tools/msgraph.py fetch  --account EMAIL [--days 7] [--limit 100] [--offset 0]
                                   [--unseen] [--folder inbox] [--with-hosts]
                                   [--with-headers] [--no-snippets]
    python tools/msgraph.py body   --account EMAIL --uid ID [--out FILE]
    python tools/msgraph.py find   --account EMAIL --message-id <...> [--out FILE]

On `uid`: Graph's opaque message id is used wherever the IMAP backend uses a UID, so callers
need no special-casing. Unlike an IMAP UID it survives a move between folders - but
`message_id` (`internetMessageId`) remains the durable cross-folder handle the rest of the
tool joins on, exactly as with IMAP.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import credstore as secret_store  # noqa: E402  - the DPAPI store, not the stdlib
import untrusted  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Loaded LAZILY. This used to be read at import time, which made the module unimportable
# without config - so the test suite could not run on a clean clone, and you had to install
# before you could test. Deferred behind a function, a missing file now surfaces where it
# means something (at the command that needs it) instead of at `import`.
_CONFIG = None


def config():
    global _CONFIG
    if _CONFIG is None:
        try:
            # utf-8-sig: hand-edited on Windows, where editors still add a BOM by default
            # and json.load raises on one.
            _CONFIG = json.loads(
                (ROOT / "config" / "accounts.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            _CONFIG = {"accounts": []}
    return _CONFIG


GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
# Mail.Read only. Read-only at the TOKEN level, so a bug in this tool cannot mutate the
# mailbox even if it tried - the tenant refuses it. Widen to Mail.ReadWrite only when the
# routine is actually given hands, and expect to re-consent when you do.

socket.setdefaulttimeout(30)


def _authority():
    """Which Microsoft accounts may sign in. Default MUST match mailtool.py's: both read
    the same config key, and two backends disagreeing on its default is a bug that only
    shows up when someone switches provider and their sign-in silently changes meaning."""
    return str(config().get("ms_authority") or "common").strip("/ ") or "common"


def _client_id():
    cid = config().get("ms_client_id")
    if not cid:
        raise SystemExit(
            "ERROR: no 'ms_client_id' in config/accounts.json.\n"
            "\n"
            "Graph needs an Entra ID app registration - ONE per deployment, not one per\n"
            "user, and it is not a secret. At https://entra.microsoft.com ->\n"
            "App registrations -> New registration:\n"
            "  * Redirect URI platform: 'Mobile and desktop applications', NOT 'Web'.\n"
            "    Choosing Web demands a client secret and rejects the PKCE flow, and it\n"
            "    fails late, at token exchange, without naming the platform as the cause.\n"
            "    This is the most common first-run failure.\n"
            "  * Redirect URI: http://localhost  (no port - Entra matches any port)\n"
            "  * API permissions -> Microsoft Graph -> Delegated -> Mail.Read and\n"
            "    offline_access. Mail.ReadBasic looks safer and is useless: it strips\n"
            "    message bodies, so no snippets and no link extraction.\n"
            "  * 'Allow public client flows' is NOT needed for PKCE - that toggle is for\n"
            "    device code and ROPC, and enabling it widens the surface for nothing.\n"
            "\n"
            "Then add the Application (client) ID as a top-level \"ms_client_id\" key.\n"
            "\n"
            "Tip: your tenant GUID is readable with no authentication at all -\n"
            "  https://login.microsoftonline.com/<your-domain>/v2.0/.well-known/openid-configuration"
        )
    return cid


def account_config(addr):
    for acct in config()["accounts"]:
        if acct["email"].lower() == addr.lower():
            return acct
    raise SystemExit(f"ERROR: {addr} is not in config/accounts.json")


# ---------------------------------------------------------------- OAuth: auth code + PKCE

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


class _CatchCode(http.server.BaseHTTPRequestHandler):
    """Single-shot loopback listener that catches the ?code= redirect.

    The result is stored on the SERVER instance, not on the class. As a class attribute it
    was mutable state shared by every sign-in in the process: harmless for one interactive
    auth, but two in one process would have had the second read the first's code - and the
    failure would look like a state mismatch, which is the one error here that should only
    ever mean something is wrong.
    """

    def do_GET(self):  # noqa: N802 - stdlib naming
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result = {k: v[0] for k, v in q.items()}
        self.server.oauth_result = result
        ok = "code" in result
        # The error text comes from the redirect URL, so it is escaped rather than
        # interpolated raw - it lands in a browser, and nothing that arrives over the wire
        # gets to write markup into a page this tool renders.
        import html as _html
        body = (
            "<html><body style='font:16px system-ui;padding:3em'>"
            + ("<h2>Signed in.</h2><p>You can close this tab and go back to the terminal.</p>"
               if ok else
               "<h2>Sign-in failed.</h2><pre>%s</pre>"
               % _html.escape(result.get("error_description", "")))
            + "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # the redirect URL carries the auth code; never write it to a log


def auth(addr):
    """Interactive sign-in. The user authenticates in their own browser; no password is ever
    typed into, passed through, or visible to this process."""
    print("NOTE: this Graph backend has not yet been confirmed against a live tenant.\n"
          "      If this sign-in fails, the most common cause is the app registration's\n"
          "      redirect URI platform being 'Web' instead of 'Mobile and desktop\n"
          "      applications'. Please report what happens either way.\n", file=sys.stderr)
    client_id = _client_id()
    # stdlib secrets, usable again now that the credential store is no longer named
    # secrets.py and shadowing it. RFC 7636 verifier length, url-safe, unpadded.
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _CatchCode)
    srv.oauth_result = {}
    port = srv.server_address[1]
    redirect = f"http://localhost:{port}"
    # Entra special-cases loopback redirect URIs: the registration holds "http://localhost"
    # and any port matches, so this needs no per-machine registration change.

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": addr,
        "prompt": "select_account",
    }
    url = (f"https://login.microsoftonline.com/{_authority()}/oauth2/v2.0/authorize?"
           + urllib.parse.urlencode(params))

    threading.Thread(target=srv.handle_request, daemon=True).start()
    print(f"Opening your browser to sign in as {addr} ...")
    print(f"If it does not open, paste this into a browser:\n\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + 300
    while not srv.oauth_result and time.time() < deadline:
        time.sleep(0.4)
    got = srv.oauth_result
    srv.server_close()
    if not got:
        raise SystemExit("ERROR: timed out waiting for the browser redirect (5 min)")
    if "error" in got:
        raise SystemExit(f"ERROR: {got['error']}: {got.get('error_description', '')}")
    if got.get("state") != state:
        # A mismatched state means the response is not the one we asked for. Refuse it.
        raise SystemExit("ERROR: state mismatch on the auth redirect - refusing the response")

    tok = _post_form(f"https://login.microsoftonline.com/{_authority()}/oauth2/v2.0/token", {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": got["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if "access_token" not in tok:
        raise SystemExit(f"ERROR: token exchange failed: {tok.get('error')}: "
                         f"{tok.get('error_description', '')}")
    _store(addr, tok)
    print(f"SUCCESS: Graph tokens stored for {addr}")
    print(f"  scopes granted: {tok.get('scope', '(not reported)')}")
    return 0


def _store(addr, tok):
    # ONE transaction. Three separate set_value calls meant three full decrypt-modify-encrypt
    # cycles of the whole store, and a token set that could land half-written if anything
    # else wrote in between - leaving an access token with no matching refresh token, which
    # fails later and somewhere else.
    secret_store.set_values(addr, {
        "graph_refresh_token": tok.get("refresh_token"),      # absent on some refreshes
        "graph_access_token": tok["access_token"],
        "graph_token_expiry": str(int(time.time())
                                  + int(tok.get("expires_in", 3600)) - 120),
    })


def access_token(addr):
    expiry = secret_store.get(addr, "graph_token_expiry")
    if expiry and int(expiry) > time.time():
        return secret_store.get(addr, "graph_access_token")
    refresh = secret_store.get(addr, "graph_refresh_token")
    if not refresh:
        raise RuntimeError(f"no Graph tokens - run: python tools/msgraph.py auth --account {addr}")
    tok = _post_form(f"https://login.microsoftonline.com/{_authority()}/oauth2/v2.0/token", {
        "client_id": _client_id(),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": SCOPE,
    })
    if "access_token" not in tok:
        raise RuntimeError(f"token refresh failed ({tok.get('error')}: "
                           f"{tok.get('error_description', '')}) - re-run auth for {addr}")
    _store(addr, tok)
    return tok["access_token"]


# ---------------------------------------------------------------- Graph calls

# ---------------------------------------------------------------- throttling

# Graph throttles per app per mailbox and answers 429 with a Retry-After header. The danger
# is not steady-state volume - a daily sweep of a few thousand messages is ~tens of requests
# at $top=100. The danger is a LOOP BUG turning into thousands of calls, and retrying a 429
# faster than the server told you to, which escalates a short throttle into a long block.
#
# So: obey Retry-After to the second, back off with jitter when it is absent, retry only the
# statuses that mean "later" (never a 403, which means "no" and will never become yes), and
# cap the whole run with a hard request budget that raises rather than continuing.

MIN_INTERVAL = 0.12          # polite floor between requests; ~free on a daily job
# Ceiling on a single raw message body. Overridable with --max-body-mb.
MAX_BODY_BYTES = 25 * 1024 * 1024

# THE 10-SECOND FLOOR IS THE WHOLE LESSON. Graph frequently answers 429 with NO Retry-After
# header, so the fallback path is the common path, not a corner case. During a per-user
# penalty window a sub-10s retry virtually never succeeds, AND every 429 EXTENDS the penalty
# - so a 1s/2s/4s exponential backoff is not merely useless, it actively lengthens the block.
# An earlier version of this file did exactly that.
MAX_RETRIES = 5
MIN_FALLBACK_BACKOFF = 10.0
MAX_BACKOFF = 120.0
# Budget per call: 10+10+10+10+16 ~= 56s. Patient enough to ride out a transient throttle,
# short enough that the run-level breaker below bails a genuinely penalised run in minutes.

# A "throttle-out" is a call that burned the ENTIRE per-call retry budget and still failed -
# already strong evidence of a penalty window. Counted CUMULATIVELY across the run: a later
# success does NOT reset it, because throttling is intermittent in practice (throttle A ->
# succeed B -> throttle C) and a consecutive-only counter simply never trips.
THROTTLE_STOP_AFTER = 3
THROTTLE_COOLDOWN = 15.0     # breather after a throttle-out that didn't trip the breaker


class GraphError(RuntimeError):
    """A real failure - wrong permissions, bad request, missing resource. Will not fix itself."""


class GraphThrottleError(GraphError):
    """429/5xx that survived the retry budget. Means COME BACK LATER, not "try harder".

    Distinct from GraphError so callers can tell a transient penalty window from a genuine
    error - and, more importantly, so a partial result can be labelled partial.
    """


def _bytes(n):
    """Human-readable size. A ceiling reported as '0 MB' teaches the reader nothing."""
    for unit, size in (("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return "%.1f %s" % (n / size, unit)
    return "%d bytes" % n


class _Budget:
    """Per-run ceilings. A runaway loop, not steady-state volume, is the real ban risk.

    `sleep` is injected so a test can exercise the backoff arithmetic without actually
    waiting a real minute for it.
    """

    def __init__(self, max_requests=600, sleep=time.sleep, clock=time.time):
        self.max = max_requests
        self.used = self.throttled = self.retried_401 = self.throttle_outs = 0
        self.slept = 0.0
        self.last_call = 0.0
        self.stopped_early = False
        self._sleep = sleep
        self._clock = clock

    def spend(self):
        if self.used >= self.max:
            raise GraphError(
                f"request budget exhausted ({self.max} Graph calls in one run). Refusing to "
                f"continue rather than risk a throttle. Raise --max-requests deliberately if "
                f"this run genuinely needs more.")
        gap = self._clock() - self.last_call
        if gap < MIN_INTERVAL:
            self._sleep(MIN_INTERVAL - gap)
        self.used += 1
        self.last_call = self._clock()

    def note_throttle_out(self, say=True):
        """Record a burned retry budget. Returns True if the run should stop now."""
        self.throttle_outs += 1
        self.stopped_early = self.throttle_outs >= THROTTLE_STOP_AFTER
        if self.stopped_early:
            if say:
                print(f"  BREAKER: {self.throttle_outs} throttle-outs this run - stopping "
                      f"gracefully rather than hammering a penalty window.", file=sys.stderr)
            return True
        if say:
            print(f"  throttle-out {self.throttle_outs}/{THROTTLE_STOP_AFTER}; cooling down "
                  f"{THROTTLE_COOLDOWN:.0f}s before the next call.", file=sys.stderr)
        self._sleep(THROTTLE_COOLDOWN)
        self.slept += THROTTLE_COOLDOWN
        return False

    def stats(self):
        return {"graph_requests": self.used, "budget": self.max,
                "throttle_429s": self.throttled, "throttle_outs": self.throttle_outs,
                "seconds_waiting_on_throttle": round(self.slept, 1),
                "stopped_early": self.stopped_early}


def _backoff(exc, attempt):
    """Seconds to wait. The server's number wins outright; ours is only the fallback."""
    hdr = exc.headers.get("Retry-After") if exc.headers else None
    if hdr:
        try:
            return min(float(int(hdr)), MAX_BACKOFF)   # the server said so - honour it exactly
        except (TypeError, ValueError):
            pass  # HTTP-date form; fall through rather than guess
    return min(max(2.0 ** attempt, MIN_FALLBACK_BACKOFF) + random.uniform(0, 1.5), MAX_BACKOFF)


class Graph:
    """One Graph session: its transport, its token source, its clock and its budget.

    EVERYTHING EXTERNAL IS A CONSTRUCTOR ARGUMENT, and that is the point. The first version
    of this file reached for `urllib.request.urlopen`, `access_token` and `time.sleep` as
    module globals, so the only way to test any of it was to reassign those globals - and a
    patch left in place by one test leaked into the next, where it produced a failure that
    read as a defect in the code under test rather than in the harness. That happened here,
    on the size-guard test, and cost more time to diagnose than the seam costs to build.

    Injected instead of patched: `send` (urlopen's contract), `token` (address -> bearer),
    `sleep` and `clock`. Production passes nothing and gets the real ones; a test passes
    fakes and holds its own instance, so no test can affect another.
    """

    def __init__(self, token=None, send=None, sleep=time.sleep, clock=time.time,
                 store=None, max_requests=600, max_body_bytes=None):
        self._token = token or access_token
        self._send = send or urllib.request.urlopen
        # The credential store is injected for the same reason as the rest: the 401 path
        # WRITES to it, so a test exercising that branch against the module-level store
        # writes into the real DPAPI store of whoever ran the tests. It did.
        self._store = store or secret_store
        self._sleep = sleep
        self.budget = _Budget(max_requests, sleep=sleep, clock=clock)
        self.max_body_bytes = max_body_bytes or MAX_BODY_BYTES

    def get(self, addr, url, raw=False):
        if url.startswith("/"):
            url = GRAPH + url
        fresh_token = False
        for attempt in range(MAX_RETRIES + 1):
            self.budget.spend()
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self._token(addr),
                "Accept": "application/json",
            })
            try:
                with self._send(req) as resp:
                    if raw:
                        # SIZE GUARD. /$value returns the full MIME source including every
                        # attachment, and read() with no ceiling pulls all of it into
                        # memory - one large attachment is enough to take the process down.
                        # Refuse loudly: the sanitiser wants the message, not the payload.
                        cap = self.max_body_bytes
                        declared = resp.headers.get("Content-Length")
                        if declared and int(declared) > cap:
                            raise GraphError(
                                "message is %s, over the %s ceiling for a single body. It "
                                "is almost certainly a large attachment. Raise "
                                "--max-body-mb deliberately if you really want it."
                                % (_bytes(int(declared)), _bytes(cap)))
                        data = resp.read(cap + 1)
                        if len(data) > cap:
                            raise GraphError(
                                "message body exceeded the %s ceiling while streaming (the "
                                "server sent no Content-Length)." % _bytes(cap))
                        return data
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if exc.code == 401 and not fresh_token:
                    # Clock skew or an early revocation. Drop the cached token and try once
                    # more; never more than once, or a dead grant becomes a retry storm.
                    fresh_token = True
                    self._store.set_value(addr, "graph_token_expiry", "0")
                    self.budget.retried_401 += 1
                    continue
                if retryable and attempt < MAX_RETRIES:
                    wait = _backoff(exc, attempt)
                    self.budget.throttled += exc.code == 429
                    self.budget.slept += wait
                    print(f"  Graph {exc.code}; waiting {wait:.0f}s "
                          f"(attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                    self._sleep(wait)
                    continue
                if retryable:
                    raise GraphThrottleError(
                        f"Graph {exc.code} on {url.split('?')[0]}: throttled, exhausted "
                        f"{MAX_RETRIES} retries")
                detail = exc.read().decode(errors="replace")[:600]
                raise GraphError(f"Graph {exc.code} on {url.split('?')[0]}: {detail}")
        raise GraphThrottleError(
            f"Graph: gave up after {MAX_RETRIES} retries on {url.split('?')[0]}")


def cmd_doctor(args, gc):
    targets = [a for a in config()["accounts"]
               if a.get("provider") == "graph"
               and (not args.account or a["email"].lower() == args.account.lower())]
    if not targets:
        # A zero is a claim. "connected 0 / total 0" is indistinguishable from a clean run
        # unless it says WHY there was nothing to check - that ambiguity is the exact shape
        # of failure this project keeps meeting.
        other = [a["email"] + " (provider=" + str(a.get("provider")) + ")"
                 for a in config()["accounts"]]
        print(json.dumps({
            "connected": 0, "total": 0, "accounts": [],
            "error": "NO GRAPH ACCOUNTS CONFIGURED - this checked nothing, it is not an all-clear",
            "configured_accounts": other or ["(none at all)"],
            "fix": "set provider to \"graph\" in config/accounts.json for the mailbox you want",
        }, indent=2))
        return 1
    results, ok = [], 0
    for acct in targets:
        addr = acct["email"]
        try:
            me = gc.get(addr, "/me?$select=userPrincipalName,displayName,mail")
            box = gc.get(addr, "/me/mailFolders/inbox?$select=displayName,totalItemCount,unreadItemCount")
            signed_in = (me.get("mail") or me.get("userPrincipalName") or "").lower()
            entry = {"account": addr, "status": "CONNECTED", "auth": "graph-pkce",
                     "signed_in_as": signed_in,
                     "display_name": me.get("displayName"),
                     "inbox_messages": box.get("totalItemCount"),
                     "inbox_unread": box.get("unreadItemCount")}
            if signed_in and signed_in != addr.lower():
                # Consented as somebody else. Everything downstream would silently be the
                # wrong mailbox, which is the worst kind of working.
                entry["status"] = "WRONG_MAILBOX"
                entry["error"] = (f"tokens are for {signed_in}, not {addr} - "
                                  f"re-run auth and pick the right account")
            else:
                ok += 1
            results.append(entry)
        except Exception as exc:
            results.append({"account": addr, "status": "FAILED", "error": str(exc)})
    print(json.dumps({"connected": ok, "total": len(targets), "accounts": results}, indent=2))
    return 0 if ok == len(targets) and targets else 1


def _addr_of(party):
    ea = (party or {}).get("emailAddress") or {}
    name, mail = ea.get("name") or "", ea.get("address") or ""
    return f"{name} <{mail}>".strip() if name else mail


_HOST_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")


def _link_hosts(body):
    """Every link host in a Graph `body` object, sorted and de-duplicated.

    Matches mailtool.py's --with-hosts output so build_sender_hosts.py and check_new_hosts.py
    consume either backend unchanged. Graph returns {"contentType": "html"|"text",
    "content": "..."}; the regex is applied to the raw content either way, since a host
    inside an href is exactly what we want and stripping tags first would lose nothing but
    cost time.
    """
    content = (body or {}).get("content") or ""
    hosts = set()
    for h in _HOST_RE.findall(content):
        h = h.lower().strip(".")
        if h and "." in h:
            hosts.add(h)
    return sorted(hosts)


def cmd_fetch(args, gc):
    addr = args.account
    select = ["id", "internetMessageId", "subject", "from", "toRecipients", "ccRecipients",
              "receivedDateTime", "isRead", "hasAttachments", "webLink"]
    if not args.no_snippets:
        select.append("bodyPreview")
    if args.with_headers:
        select.append("internetMessageHeaders")
    if args.with_hosts:
        # Link-host extraction needs the body. Pulling it in the SAME page request is the
        # whole point - the IMAP backend learned this the hard way, because doing it
        # caller-side means one IMAP session per message and turns a profile build into an
        # overnight job. On Graph the equivalent mistake is a GET /messages/{id} per message.
        select.append("body")

    q = {"$select": ",".join(select),
         "$orderby": "receivedDateTime desc",
         "$top": str(min(args.limit, 100))}
    filters = []
    if args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters.append(f"receivedDateTime ge {since}")
    if args.unseen:
        filters.append("isRead eq false")
    if filters:
        q["$filter"] = " and ".join(filters)
    if args.offset:
        # Paging back from the newest, matching mailtool.py's --offset. Graph's $skip is
        # applied server-side against the same $orderby, so it is stable in a way the IMAP
        # backend's list-slicing is not under concurrent deletion.
        q["$skip"] = str(args.offset)

    url = f"/me/mailFolders/{args.folder}/messages?" + urllib.parse.urlencode(q)
    messages, pages = [], 0
    while url and len(messages) < args.limit:
        try:
            page = gc.get(addr, url)
        except GraphThrottleError:
            # Burned the retry budget on this page. Cool down and try the SAME page again;
            # after THROTTLE_STOP_AFTER of these the breaker stops the run and we keep what
            # we already have. Bounded either way - it cannot loop.
            if gc.budget.note_throttle_out():
                break
            continue
        pages += 1
        for m in page.get("value", []):
            hdrs = {h.get("name", "").lower(): h.get("value", "")
                    for h in (m.get("internetMessageHeaders") or [])}
            entry = {
                # Graph's message id is the durable handle here and, unlike an IMAP UID, it
                # survives a move between folders. internetMessageId is kept anyway because
                # it is what the dashboard already stores and joins on.
                "uid": m.get("id"),
                "message_id": (m.get("internetMessageId") or "").strip(),
                "from": _addr_of(m.get("from")),
                "to": ", ".join(_addr_of(t) for t in (m.get("toRecipients") or [])),
                "cc": ", ".join(_addr_of(t) for t in (m.get("ccRecipients") or [])),
                "subject": m.get("subject") or "",
                "date": m.get("receivedDateTime") or "",
                "size": None,          # not exposed on the message list; not worth a call each
                "unread": not m.get("isRead", True),
                "list_unsubscribe": bool(hdrs.get("list-unsubscribe")) if args.with_headers else None,
                "has_attachments": m.get("hasAttachments"),
                "web_link": m.get("webLink"),
            }
            if not args.no_snippets:
                entry["snippet"] = (m.get("bodyPreview") or "").strip()[:400]
            if args.with_hosts:
                entry["link_hosts"] = _link_hosts(m.get("body"))
            # SAME LABELLING AS THE IMAP BACKEND, and it was missing here for a whole
            # release. apply_proposal refuses to bin anything carrying injection_signals -
            # a good guard - but a Graph-fetched message never had the field, so the guard
            # could not fire. It failed OPEN, silently, with no error or warning, in
            # exactly the case it exists for: Graph is the Microsoft 365 path, so the
            # deployments most likely to adopt this were the ones getting none of it.
            #
            # The bug class is "a second implementation of an interface misses a
            # cross-cutting concern". test_backend_parity.py now fails if any backend
            # forgets, because the next one will.
            untrusted.annotate(entry)
            messages.append(entry)
            if len(messages) >= args.limit:
                break
        url = page.get("@odata.nextLink")

    flagged = [m for m in messages if m.get("injection_signals")]
    out = {"account": addr, "mailbox": args.folder,
           # Travels with the data rather than living only in a skill file.
           "_UNTRUSTED": untrusted.NOTICE,
           "injection_flagged": len(flagged),
           # `days` and `offset_from_newest` echo mailtool.py's fetch envelope so callers
           # need no per-backend branching.
           "days": args.days, "offset_from_newest": args.offset,
           "returned": len(messages), "pages_walked": pages,
           # State the reach beside the count. A number without its scope is how a partial
           # scan gets read as an all-clear.
           "reach": (f"newest {len(messages)} messages in {args.folder}"
                     + (f" from the last {args.days} days" if args.days
                        else " with no date filter")
                     + (", unread only" if args.unseen else "")),
           "truncated": bool(url),
           "complete": not gc.budget.stopped_early,
           "throttling": gc.budget.stats(),
           "messages": messages}
    if gc.budget.stopped_early:
        # THE important flag. A throttled run saw part of the mailbox; anything downstream
        # that reads it as a full look will conclude "nothing needs you" from a sample. That
        # is the failure that destroys trust in the whole board, so it is stated loudly and
        # the exit code is non-zero so a caller cannot ignore it by accident.
        out["WARNING"] = ("INCOMPLETE - the run stopped on sustained throttling. These "
                          "messages are a PARTIAL view. Do not read an absence here as "
                          "'nothing needs attention', and do not trash or rule on this run. "
                          "Re-run once the penalty window clears.")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if gc.budget.stopped_early else 0


def cmd_body(args, gc):
    """Raw MIME for one message, so it feeds the existing sanitiser unchanged.

    `--uid` takes Graph's opaque message id, mirroring mailtool.py's `--uid`. Graph's
    `/$value` returns the full RFC822 source, so mailview.py needs no Graph-specific path.
    Reading via this endpoint never marks the message read, matching the IMAP backend's
    BODY.PEEK discipline.
    """
    raw = gc.get(args.account, f"/me/messages/{urllib.parse.quote(args.uid)}/$value", raw=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(raw)
        print(f"wrote {len(raw)} bytes to {args.out}")
    else:
        sys.stdout.buffer.write(raw)
    return 0


def cmd_find(args, gc):
    """Locate a message by Message-ID anywhere in the mailbox.

    One tenant-wide lookup, against every folder at once - no folder walk, and it finds
    messages that have since moved to Deleted Items. Reading never marks anything read.
    """
    mid = args.message_id.strip()
    if not (mid.startswith("<") and mid.endswith(">")):
        mid = "<" + mid.strip("<>") + ">"
    # $top=5, not 1. internetMessageId is NOT reliably unique within a mailbox - a self-CC, a
    # forwarded loop, or the same message filed in two folders all produce duplicates. Asking
    # for one and taking it means silently picking a copy and never saying there were others,
    # which is the wrong answer given without the information that would reveal it.
    q = urllib.parse.urlencode({
        "$filter": "internetMessageId eq '%s'" % mid.replace("'", "''"),
        "$select": "id,subject,parentFolderId,receivedDateTime",
        "$top": "5",
    })
    hits = gc.get(args.account, f"/me/messages?{q}").get("value", [])
    if not hits:
        print(json.dumps({"found": False, "message_id": mid}), file=sys.stderr)
        return 3
    # Newest first, so "the" copy is a defined choice rather than whatever Graph returned.
    hits.sort(key=lambda h: h.get("receivedDateTime") or "", reverse=True)
    hit = hits[0]
    folder = gc.get(args.account,
                  f"/me/mailFolders/{hit['parentFolderId']}?$select=displayName"
                  ).get("displayName", "?")
    if args.out:
        raw = gc.get(args.account, f"/me/messages/{urllib.parse.quote(hit['id'])}/$value", raw=True)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(raw)
        print(json.dumps({"found": True, "mailbox": folder, "uid": hit["id"],
                          "message_id": mid, "bytes": len(raw), "out": args.out,
                          "copies": len(hits)}))
    else:
        out = {"found": True, "mailbox": folder, "uid": hit["id"],
               "message_id": mid, "subject": hit.get("subject"), "copies": len(hits)}
        if len(hits) > 1:
            # Say so rather than presenting one copy as the answer.
            out["note"] = ("%d copies of this Message-ID exist in the mailbox; this is the "
                           "most recent. A self-CC, a forwarded loop or a message filed in "
                           "two folders all do this." % len(hits))
        print(json.dumps(out))
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth"); a.add_argument("--account", required=True)
    d = sub.add_parser("doctor"); d.add_argument("--account")

    f = sub.add_parser("fetch")
    f.add_argument("--account", required=True)
    f.add_argument("--folder", default="inbox")
    f.add_argument("--days", type=int, default=7, help="0 = no date filter")
    f.add_argument("--limit", type=int, default=100)
    f.add_argument("--offset", type=int, default=0,
                   help="skip the N newest matches (paging back from newest)")
    f.add_argument("--unseen", action="store_true")
    f.add_argument("--no-snippets", action="store_true")
    f.add_argument("--with-hosts", action="store_true", dest="with_hosts",
                   help="also return every link host per message (for sender profiling)")
    f.add_argument("--with-headers", action="store_true",
                   help="also pull internetMessageHeaders (needed for List-Unsubscribe)")

    b = sub.add_parser("body")
    b.add_argument("--account", required=True)
    b.add_argument("--uid", required=True, help="Graph message id (mirrors mailtool.py --uid)")
    b.add_argument("--out")

    fi = sub.add_parser("find")
    fi.add_argument("--account", required=True)
    fi.add_argument("--message-id", required=True, dest="message_id")
    fi.add_argument("--out")

    for sp in (d, f, b, fi):
        sp.add_argument("--max-requests", type=int, default=600, dest="max_requests",
                        help="hard ceiling on Graph calls for this run (runaway-loop guard)")
    for sp in (b, fi):
        sp.add_argument("--max-body-mb", type=float, default=25.0, dest="max_body_mb",
                        help="ceiling on a single raw message body (default 25)")

    args = p.parse_args()
    # ONE client per run, built here and passed down. No module-level session state, so two
    # runs in one process cannot share a budget or a token cache by accident.
    gc = Graph(max_requests=getattr(args, "max_requests", None) or 600,
               max_body_bytes=int(getattr(args, "max_body_mb", None) * 1024 * 1024)
               if getattr(args, "max_body_mb", None) else None)
    run = {"auth": lambda: auth(args.account), "doctor": lambda: cmd_doctor(args, gc),
           "fetch": lambda: cmd_fetch(args, gc), "body": lambda: cmd_body(args, gc),
           "find": lambda: cmd_find(args, gc)}[args.cmd]
    # A Graph failure is an EXPECTED outcome, not a crash. Without this, the most likely
    # first-run result - a 403 because the app registration lacks Mail.Read, or consent was
    # never granted - printed a Python traceback, which tells the reader nothing about which
    # of the setup steps they missed. Distinct exit codes so a caller can tell "come back
    # later" from "this will never work".
    try:
        return run()
    except GraphThrottleError as exc:
        print(f"\nTHROTTLED: {exc}\n\nThis is temporary. Wait for the penalty window to "
              f"clear and re-run; do NOT retry in a tight loop, which extends it.",
              file=sys.stderr)
        return 2
    except GraphError as exc:
        msg = str(exc)
        print(f"\nGRAPH ERROR: {msg}", file=sys.stderr)
        if " 403 " in msg or "Forbidden" in msg:
            print("\n403 means the token is valid but not permitted. Usually one of:\n"
                  "  * the app registration is missing delegated Mail.Read, or\n"
                  "  * consent was never granted (an admin may need to grant it), or\n"
                  "  * the scope was widened after the last sign-in - re-run `auth`.",
                  file=sys.stderr)
        elif " 401 " in msg:
            print("\n401 after a refresh attempt means the grant is gone. Re-run `auth`.",
                  file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
