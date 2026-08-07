"""IMAP toolkit for the email-cleanup-and-summary daily routine.

Connects to each account in config/accounts.json using credentials from the
DPAPI secret store (tools/credstore.py):
  - gmail accounts:     IMAP + app password (field "app_password")
  - microsoft accounts: IMAP + OAuth2 (field "ms_refresh_token", via `auth-ms`)

Commands (all output is UTF-8; fetch/folders emit JSON for the routine to parse):
  python tools/mailtool.py doctor [--account EMAIL]
  python tools/mailtool.py auth-ms --account EMAIL          # one-time device-code consent
  python tools/mailtool.py folders --account EMAIL
  python tools/mailtool.py fetch  --account EMAIL [--mailbox INBOX] [--days 7]
                                  [--unseen] [--limit 100] [--no-snippets]
  python tools/mailtool.py body   --account EMAIL --uid N [--mailbox INBOX] [--out FILE]
  python tools/mailtool.py act    --account EMAIL --uids N,N,... --action trash|markread|move
                                  [--mailbox INBOX] [--dest FOLDER]

`act --action trash` moves to the account's Trash folder (recoverable) - it
never permanently deletes. Exit code 0 = success.
"""
import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# credstore, NOT secrets. This directory goes on sys.path at position 0, so a module here
# named secrets.py SHADOWS the standard library's for the whole process - and the
# `as secret_store` alias does not prevent it, because shadowing happens at import, not at
# binding. It stayed dormant only because nothing on the import path needed stdlib secrets;
# the first person to reach for secrets.token_urlsafe in this tree would have got a module
# that does not have it.
import credstore as secret_store
import providers
import runmode
import untrusted

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


# WHICH MICROSOFT ACCOUNTS CAN SIGN IN. This was hard-coded to /consumers/, which accepts
# personal accounts ONLY - outlook.com, hotmail, live. A work or school mailbox does not
# exist in the consumers tenant at all, so every business Microsoft user was rejected at
# sign-in with no way to configure around it. That is most of the "Outlook" audience the
# onboarding skill invites.
#
# `common` accepts both, which is the right default for a tool that does not know which
# kind of account you have. Set "ms_authority" in accounts.json to narrow it:
#   common         personal + work/school   (default)
#   organizations  work/school only
#   consumers      personal only
#   <tenant-guid>  one specific tenant
MS_AUTHORITY = str(config().get("ms_authority") or "common").strip("/ ") or "common"
MS_TOKEN_URL = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % MS_AUTHORITY
MS_DEVICECODE_URL = ("https://login.microsoftonline.com/%s/oauth2/v2.0/devicecode"
                     % MS_AUTHORITY)
MS_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def ms_client_id():
    """The Entra ID app registration this tool signs in through.

    Read through a function so a missing key names itself and says what to do. It used to be
    config()["ms_client_id"] inline, so the first thing every Microsoft user met was a bare
    KeyError with no indication that an app registration was needed at all - and the
    onboarding skill never mentioned the step either.

    ONE registration per deployment, not per user: an admin registers it once, consents it
    for the tenant, and everyone shares the id. It is not a secret - it identifies the app,
    not the person - which is why it lives in accounts.json rather than the credential store.
    """
    cid = (config().get("ms_client_id") or "").strip()
    if not cid:
        raise SystemExit(
            "ERROR: no \"ms_client_id\" in config/accounts.json.\n"
            "\n"
            "Microsoft sign-in needs an Entra ID app registration. Create one at\n"
            "  https://entra.microsoft.com -> App registrations -> New registration\n"
            "    * Supported account types: match your ms_authority (default 'common')\n"
            "    * Authentication -> Allow public client flows: YES\n"
            "    * API permissions -> Microsoft Graph -> Delegated ->\n"
            "        offline_access, and IMAP.AccessAsUser.All from Office 365 Exchange Online\n"
            "\n"
            "Then put the Application (client) ID in config/accounts.json:\n"
            "    { \"ms_client_id\": \"<guid>\", \"ms_authority\": \"common\", \"accounts\": [...] }\n"
            "\n"
            "One registration serves everyone in a deployment - it is not per user, and it\n"
            "is not a secret.")
    return cid

socket.setdefaulttimeout(30)


def account_config(addr, cfg=None):
    """The config block for one address. `cfg` names WHICH config, for callers that have
    one in hand - the tests, and anything checking a config it has not installed."""
    for acct in (cfg or config())["accounts"]:
        if acct["email"].lower() == addr.lower():
            return acct
    raise SystemExit(f"ERROR: {addr} is not in config/accounts.json")


# ---------------------------------------------------------------- OAuth (Microsoft)

def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def ms_device_auth(addr):
    client_id = ms_client_id()
    dc = _post_form(MS_DEVICECODE_URL, {"client_id": client_id, "scope": MS_SCOPE})
    if "device_code" not in dc:
        raise SystemExit(f"ERROR: device-code request failed: {dc.get('error')}: {dc.get('error_description')}")
    print(dc["message"])  # "To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code XXXX..."
    print(f"(sign in as {addr}; waiting up to {dc['expires_in']}s)")
    sys.stdout.flush()
    interval = dc.get("interval", 5)
    deadline = time.time() + dc["expires_in"]
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post_form(MS_TOKEN_URL, {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"],
        })
        if "access_token" in tok:
            _store_ms_tokens(addr, tok)
            print(f"SUCCESS: OAuth tokens stored for {addr}")
            return 0
        if tok.get("error") in ("authorization_pending", "slow_down"):
            if tok["error"] == "slow_down":
                interval += 5
            continue
        raise SystemExit(f"ERROR: {tok.get('error')}: {tok.get('error_description')}")
    raise SystemExit("ERROR: device-code flow timed out before sign-in completed")


def _store_ms_tokens(addr, tok):
    secret_store.set_value(addr, "ms_refresh_token", tok["refresh_token"])
    secret_store.set_value(addr, "ms_access_token", tok["access_token"])
    secret_store.set_value(addr, "ms_token_expiry", str(int(time.time()) + int(tok.get("expires_in", 3600)) - 120))


def ms_access_token(addr):
    expiry = secret_store.get(addr, "ms_token_expiry")
    if expiry and int(expiry) > time.time():
        return secret_store.get(addr, "ms_access_token")
    refresh = secret_store.get(addr, "ms_refresh_token")
    if not refresh:
        raise RuntimeError("no OAuth tokens - run: python tools/mailtool.py auth-ms --account " + addr)
    tok = _post_form(MS_TOKEN_URL, {
        "client_id": ms_client_id(),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": MS_SCOPE,
    })
    if "access_token" not in tok:
        raise RuntimeError(f"token refresh failed ({tok.get('error')}) - re-run auth-ms for {addr}")
    _store_ms_tokens(addr, tok)
    return tok["access_token"]


# ---------------------------------------------------------------- IMAP connect

def connect(addr, acct=None):
    """Returns (imap_connection, method_used). Raises RuntimeError with a fix-it hint.

    THE PROVIDER IS CHECKED BEFORE THE SOCKET IS OPENED, and it used to be the other way
    round. `imaplib.IMAP4_SSL(acct["imap_host"])` ran on the first line, so an account that
    should never touch IMAP still had to name a host, and a tenant with IMAP disabled -
    which is the usual hardening step after a phishing incident, and the whole reason some
    installs cannot use IMAP at all - failed at the connection before authentication was
    even attempted. Worse, the error described the socket rather than the arrangement.
    """
    acct = acct or account_config(addr)
    backend = providers.backend_of(acct)
    if backend != providers.IMAP:
        # Named as a routing decision, not as a failure. Both of these are supported
        # configurations; this function is simply not the one that serves them.
        raise RuntimeError(
            "%s is configured as %s, which does not go over IMAP.\n%s"
            % (addr, providers.LABEL.get(backend, "an unknown provider"),
               ("  Fetch it with: python tools/msgraph.py fetch --account %s" % addr)
               if backend == providers.GRAPH else
               "  Nothing here fetches it - that is the configuration. Produce the run\n"
               "  JSON however your connector allows and pipe it into\n"
               "  `python dashboard/ingest.py`, whose docstring documents the shape."))
    conn = imaplib.IMAP4_SSL(acct["imap_host"])
    if acct["provider"] == "microsoft":
        token = ms_access_token(addr)
        auth = f"user={addr}\x01auth=Bearer {token}\x01\x01"
        try:
            conn.authenticate("XOAUTH2", lambda _: auth.encode())
            return conn, "oauth2"
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(f"XOAUTH2 login failed: {exc} - re-run auth-ms for {addr}")
    # gmail
    app_pw = secret_store.get(addr, "app_password")
    if app_pw:
        try:
            conn.login(addr, app_pw)
            return conn, "app_password"
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(f"app-password login failed: {exc} - regenerate the app password for {addr}")
    pw = secret_store.get(addr, "password")
    if pw:
        try:
            conn.login(addr, pw)
            return conn, "password"
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(
                f"plain-password login rejected ({str(exc)[:120]}) - Gmail requires an app password; "
                f"see HOWTO-connect-accounts.md for {addr}"
            )
    raise RuntimeError(f"no credentials stored for {addr}")


# One parser for an IMAP LIST line, used by everything that walks folders.
#
# There were two, and they did not agree. This one handles a quoted OR bare mailbox name;
# the other matched only `"..."` at end of line, so on a server that returns simple names
# unquoted - which is common and perfectly legal - `find --all-folders` silently skipped
# every such folder and reported "not found" over a subset it never looked at.
_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')


def parse_list_line(raw):
    """-> (flags, mailbox name) for one LIST response line, or (None, None)."""
    line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    m = _LIST_LINE.match(line.strip())
    if not m:
        return None, None
    return m.group("flags"), m.group("name").strip().strip('"')


def find_trash(conn):
    typ, listing = conn.list()
    candidates = []
    if typ == "OK":
        for raw in listing:
            flags, name = parse_list_line(raw)
            if name is None:
                continue
            if "\\Trash" in flags:
                return name
            candidates.append(name)
    for guess in ("[Gmail]/Trash", "Trash", "Deleted", "Deleted Items"):
        if guess in candidates:
            return guess
    return "Trash"


# ---------------------------------------------------------------- commands

def config_problems(acct):
    """Everything wrong with this account's CONFIG, before any socket is opened.

    doctor used to call connect() and report whatever exception came back last. On an
    install with no app registration that produced "no OAuth tokens - run: auth-ms", which
    is a real error and useless advice: auth-ms cannot work without ms_client_id, and
    mailtool already has a good specific message for exactly that case. The user was sent
    one step down a road that dead-ends at the very next command.

    The rules themselves live in providers.py, because the dashboard's setup panel asks the
    same question and two implementations of "is this account configured?" is one too many -
    they drift, and the one that drifts is always the one the user is looking at.
    """
    return providers.problems(acct, config())


# ---------------------------------------------------------------- delegating to Graph

# What each command's flags are called on the other side. `mailbox` is `folder` there, and
# everything else that matters happens to share a name.
#
# THE POINT OF SPELLING THIS OUT is the ELSE branch below. A delegator that quietly drops a
# flag it cannot translate would answer a DIFFERENT QUESTION than the one asked - `fetch
# --unseen` silently becoming "fetch everything" is a wrong answer that looks like a right
# one, which is the failure mode this whole project is organised around. Anything not in
# this table stops the command and says so.
_GRAPH_FLAGS = {
    "fetch": {"account": "--account", "mailbox": "--folder", "days": "--days",
              "limit": "--limit", "offset": "--offset", "unseen": "--unseen",
              "no_snippets": "--no-snippets"},
    "body": {"account": "--account", "uid": "--uid", "out": "--out"},
    "find": {"account": "--account", "message_id": "--message-id", "out": "--out"},
}
# Arguments that exist on this side, are irrelevant on the other, and may be dropped
# without changing the meaning of the command.
_GRAPH_IGNORE = {"cmd", "func", "account_backend"}


def graph_argv(command, args):
    """The msgraph.py argv this command translates to, or SystemExit saying why not.

    Split out from the call so the translation can be checked without spawning anything.
    The interesting behaviour here is the REFUSAL, and a test that had to run a subprocess
    to observe it is a test that does not get written.
    """
    flags = _GRAPH_FLAGS.get(command)
    if flags is None:
        raise SystemExit("ERROR: %s has no Microsoft Graph equivalent.\n"
                         "  Graph is READ-ONLY here by design - it cannot move, delete or "
                         "flag anything." % command)
    argv = [command]
    for name, value in sorted(vars(args).items()):
        if name in _GRAPH_IGNORE or value in (None, False, ""):
            continue
        flag = flags.get(name)
        if flag is None:
            raise SystemExit(
                "ERROR: --%s has no Microsoft Graph equivalent, so this command cannot be\n"
                "  delegated without changing what it asks for. Refusing rather than\n"
                "  dropping it: a fetch that quietly ignores one of its own filters returns\n"
                "  the wrong messages and looks like it worked.\n"
                "  Run tools/msgraph.py %s directly, without --%s."
                % (name.replace("_", "-"), command, name.replace("_", "-")))
        if value is True:
            argv.append(flag)
        else:
            argv += [flag, str(value)]
    return argv


def delegate_to_graph(command, args):
    """Run the msgraph equivalent of this command and pass its output straight through.

    Delegation rather than a signpost. `doctor` and `fetch` are the two commands a person
    runs to find out whether their mail is reachable, and answering "use msgraph.py" is a
    direction, not an answer - it also means no single command can describe an install that
    mixes backends, which is exactly the install this release is for.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "msgraph.py")]
        + graph_argv(command, args),
        text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def cmd_doctor(args):
    targets = [a for a in config()["accounts"] if not args.account or a.get("email", "").lower() == args.account.lower()]
    results, ok_count = [], 0
    if not targets:
        # A zero here is a claim like any other. "connected 0 / total 0" is
        # indistinguishable from a clean run unless it says there was nothing to check.
        print(json.dumps({
            "connected": 0, "total": 0, "accounts": [],
            "error": "NO ACCOUNTS CONFIGURED - this checked nothing, it is not an all-clear",
            "fix": "add a mailbox to config/accounts.json (see the onboard-mailbox skill)",
        }, indent=2))
        return 1
    unreachable = 0
    for acct in targets:
        addr = acct.get("email") or "(no email key)"
        backend = providers.backend_of(acct)
        problems = config_problems(acct)
        if problems:
            # Never open a socket on a config that cannot work: the connection error would
            # describe a symptom of the misconfiguration rather than the misconfiguration.
            results.append({"account": addr, "backend": backend, "status": "NOT CONFIGURED",
                            "error": problems[0],
                            "all_problems": problems if len(problems) > 1 else None})
            continue

        if backend == providers.CONNECTOR:
            # NOT A FAILURE, and it must never be counted as one. This mailbox is declared
            # as fetched by something else; there is nothing here to dial and nothing
            # wrong. A red row against a mailbox that is working exactly as configured
            # teaches its reader to stop reading red rows.
            status, detail = providers.status_of(acct, config())
            results.append({"account": addr, "backend": backend, "status": status,
                            "note": detail})
            unreachable += 1
            continue

        if backend == providers.GRAPH:
            # DELEGATED, not refused. `doctor` is the one command a person runs to find out
            # whether their mail is reachable, and it used to answer "use msgraph.py" for
            # Graph accounts - which is a signpost, not an answer, and left the only
            # complete picture of an install in nobody's hands.
            res = _graph_doctor(addr)
            results.append(dict({"account": addr, "backend": backend}, **res))
            ok_count += 1 if res.get("status") == "CONNECTED" else 0
            continue

        try:
            conn, method = connect(addr)
            typ, data = conn.select("INBOX", readonly=True)
            count = data[0].decode() if typ == "OK" else "?"
            trash = find_trash(conn)
            conn.logout()
            results.append({"account": addr, "backend": backend, "status": "CONNECTED",
                            "auth": method, "inbox_messages": count, "trash_folder": trash})
            ok_count += 1
        except Exception as exc:  # report every account regardless of individual failures
            results.append({"account": addr, "backend": backend, "status": "FAILED",
                            "error": str(exc)})
    # `connected` counts only accounts this tool actually reached, and `not_fetched_here`
    # is reported beside it rather than folded in. Adding connector accounts to the
    # connected count would read as "8 mailboxes verified" when some were never contacted -
    # the reassuring summary this project keeps finding, one layer up.
    print(json.dumps({"connected": ok_count, "total": len(targets),
                      "not_fetched_here": unreachable,
                      "checked": len(targets) - unreachable,
                      "accounts": results}, indent=2))
    return 0 if ok_count == len(targets) - unreachable else 1


def _graph_doctor(addr):
    """Ask msgraph.py about one account and hand back its verdict.

    Shelled out rather than imported: msgraph keeps its own config and token handling, and
    importing it here would give this process two credential stores and two opinions about
    the authority to sign in against. The subprocess boundary is the honest one.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "msgraph.py"),
         "doctor", "--account", addr],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"status": "FAILED",
                "error": ((proc.stderr or proc.stdout).strip()[:500]
                          or "msgraph.py doctor produced no output")}
    for a in (data.get("accounts") or []):
        if str(a.get("account", "")).lower() == addr.lower():
            return {k: v for k, v in a.items() if k != "account"}
    return {"status": "FAILED",
            "error": "msgraph.py doctor did not report on %s" % addr}


def _decode_header(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _snippet(msg, limit=400):
    body = None
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            body = part
            break
    if body is None:
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                body = part
                break
    if body is None:
        return ""
    try:
        text = body.get_payload(decode=True).decode(body.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def cmd_fetch(args):
    conn, _ = connect(args.account)
    conn.select(args.mailbox, readonly=True)
    criteria = []
    if args.unseen:
        criteria.append("UNSEEN")
    if getattr(args, "uid_range", None):
        criteria += ["UID", args.uid_range]
    elif args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%d-%b-%Y")
        criteria += ["SINCE", since]
    typ, data = conn.uid("SEARCH", None, *(criteria or ["ALL"]))
    uids = data[0].split() if typ == "OK" and data[0] else []
    total_matched = len(uids)
    if args.offset:
        uids = uids[:-args.offset] if args.offset < len(uids) else []
    uids = uids[-args.limit:]
    messages = []
    for uid in uids:
        spec = "(FLAGS RFC822.SIZE BODY.PEEK[])" if not args.no_snippets else \
               "(FLAGS RFC822.SIZE BODY.PEEK[HEADER])"
        typ, data = conn.uid("FETCH", uid, spec)
        if typ != "OK" or not data or data[0] is None:
            continue
        raw = b""
        flags = ""
        for item in data:
            if isinstance(item, tuple):
                raw = item[1]
                flags = item[0].decode(errors="replace")
        msg = email.message_from_bytes(raw)
        size_m = re.search(r"RFC822\.SIZE (\d+)", flags)
        entry = {
            "uid": uid.decode(),
            # The DURABLE handle. A UID is per-folder and CHANGES the moment a message is
            # moved, so every uid captured before a trash operation is stale by the time
            # the run ends. Message-ID travels with the message, which is what lets the
            # dashboard find a message again later regardless of where it ended up.
            "message_id": (msg.get("Message-ID") or "").strip(),
            "from": _decode_header(msg.get("From")),
            "to": _decode_header(msg.get("To")),
            # Cc as well as To: being one of twenty on a Cc line is not the same as
            # being asked, and the difference decides whether a "bot" sender is
            # noise or is assigning you work.
            "cc": _decode_header(msg.get("Cc")),
            "subject": _decode_header(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "size": int(size_m.group(1)) if size_m else None,
            "unread": "\\Seen" not in flags,
            "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
            "has_attachments": any(p.get_filename() for p in msg.walk()) if not args.no_snippets else None,
        }
        if not args.no_snippets:
            entry["snippet"] = _snippet(msg)
        if getattr(args, "grep", None):
            # Search the BODY inside the bulk walk. Doing this caller-side would mean an
            # IMAP session per message; here the body is already in hand.
            pat = re.compile(args.grep, re.I)
            hits = []
            for part in msg.walk():
                if part.get_content_maintype() == "multipart" or part.get_filename():
                    continue
                if part.get_content_type() not in ("text/html", "text/plain"):
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                if part.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                for m2 in pat.finditer(text):
                    a, b = max(0, m2.start() - 90), min(len(text), m2.end() + 90)
                    hits.append(re.sub(r"\s+", " ", text[a:b]).strip())
                    if len(hits) >= 3:
                        break
                if len(hits) >= 3:
                    break
            entry["grep_hits"] = hits
            if not hits:
                continue          # only matching messages are returned
        if getattr(args, "with_hosts", False):
            # Extract link hosts HERE, inside the bulk walk that already has the body in
            # hand. Doing it caller-side meant re-opening an IMAP session per message,
            # which turns a large profile build into an overnight job for data we
            # already fetched once.
            hosts = set()
            for part in msg.walk():
                if part.get_content_maintype() == "multipart" or part.get_filename():
                    continue
                if part.get_content_type() not in ("text/html", "text/plain"):
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                for h in re.findall(r"https?://([A-Za-z0-9.\-]+)", text):
                    h = h.lower().strip(".")
                    if h and "." in h:
                        hosts.add(h)
            entry["link_hosts"] = sorted(hosts)
        # Label mail that is addressed to the TRIAGER rather than to a person. Not a
        # filter: the message is kept exactly as it is, and the label is evidence for
        # the triage step. Legitimate senders do not write "ignore previous instructions".
        untrusted.annotate(entry)
        messages.append(entry)
    conn.logout()
    flagged = [m for m in messages if m.get("injection_signals")]
    print(json.dumps({"account": args.account, "mailbox": args.mailbox,
                      "total_matched": total_matched, "returned": len(uids),
                      "offset_from_newest": args.offset,
                      # Stated in the payload itself, so it travels with the data into
                      # whatever reads it rather than living only in a skill file.
                      "_UNTRUSTED": untrusted.NOTICE,
                      "injection_flagged": len(flagged),
                      "messages": messages},
                     indent=2, ensure_ascii=False))
    return 0


def cmd_body(args):
    conn, _ = connect(args.account)
    conn.select(args.mailbox, readonly=True)
    typ, data = conn.uid("FETCH", args.uid, "(BODY.PEEK[])")
    conn.logout()
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise SystemExit(f"ERROR: could not fetch uid {args.uid}")
    raw = data[0][1]
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(raw)
        print(f"wrote {len(raw)} bytes to {args.out}")
    else:
        sys.stdout.buffer.write(raw)
    return 0


def cmd_send(args):
    """Send mail (Gmail accounts only — SMTP with the stored app password). Supports an
    optional iCalendar invite part so Google Calendar picks the event up."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    acct = account_config(args.account)
    if acct["provider"] != "gmail":
        raise SystemExit("ERROR: send is implemented for gmail accounts only (SMTP app password)")
    app_pw = secret_store.get(args.account, "app_password")
    if not app_pw:
        raise SystemExit(f"ERROR: no app password stored for {args.account}")

    msg = MIMEMultipart("mixed")
    msg["From"] = args.account
    msg["To"] = args.to
    msg["Subject"] = args.subject
    body = args.text or (sys.stdin.read() if not sys.stdin.isatty() else "")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if args.ics:
        ics_text = Path(args.ics).read_text(encoding="utf-8")
        cal = MIMEText(ics_text, "calendar", "utf-8")
        cal.set_param("method", "REQUEST")
        cal.add_header("Content-Disposition", "attachment", filename="invite.ics")
        msg.attach(cal)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(args.account, app_pw)
        smtp.sendmail(args.account, [args.to], msg.as_string())
    print(json.dumps({"sent": True, "from": args.account, "to": args.to, "subject": args.subject}))
    return 0


def cmd_act(args):
    # THE READING PHASE MUST NOT HOLD HANDS - asked of the SHARED latch, not answered here.
    # This used to be an `if` in this function, which is a good control in the wrong place:
    # it protected exactly one backend. On an install that cannot run mailtool at all - no
    # app registration, IMAP closed at the tenant, a connector used instead - the project's
    # central safety mechanism was simply absent, and nothing said so.
    #
    # runmode.enforce() is what every backend asks, and test_backend_parity fails if a
    # mutating entry point stops asking.
    try:
        runmode.enforce("move, delete or flag mail", "mailtool (IMAP)")
    except runmode.ReadOnlyRefusal as exc:
        raise SystemExit(str(exc))
    conn, _ = connect(args.account)
    conn.select(args.mailbox)
    uids = args.uids.split(",")
    uid_set = ",".join(uids).encode()
    if args.action == "markread":
        conn.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")
        result = "marked read"
    else:
        dest = args.dest if args.action == "move" else find_trash(conn)
        caps = conn.capabilities
        if b"MOVE" in caps or "MOVE" in caps:
            typ, resp = conn.uid("MOVE", uid_set, f'"{dest}"')
        else:
            typ, resp = conn.uid("COPY", uid_set, f'"{dest}"')
            if typ == "OK":
                conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                conn.expunge()
        if typ != "OK":
            conn.logout()
            raise SystemExit(f"ERROR: {args.action} to {dest} failed: {resp}")
        result = f"moved to {dest}"
    conn.logout()
    print(json.dumps({"account": args.account, "uids": uids, "result": result}))
    return 0


def cmd_find(args):
    """Locate a message by Message-ID and return its RAW bytes, wherever it now lives.

    Written for the dashboard's message viewer. A UID cannot do this job: it is per-folder
    and is reassigned on move, so anything the run trashed has a different UID by the time
    anyone wants to look at it. Message-ID is stable, so this searches the likely folders
    in turn - INBOX first, then Trash, then everything else - and reports WHICH folder it
    was found in.

    BODY.PEEK is used deliberately: opening a message in the dashboard must never mark it
    read in the real mailbox.
    """
    conn, _ = connect(args.account)
    mid = args.message_id.strip()
    if not (mid.startswith("<") and mid.endswith(">")):
        mid = "<" + mid.strip("<>") + ">"

    order, seen = [], set()
    for box in ("INBOX", find_trash(conn)):
        if box and box not in seen:
            order.append(box); seen.add(box)
    if args.all_folders:
        typ, boxes = conn.list()
        if typ == "OK":
            for line in boxes or []:
                _flags, name = parse_list_line(line)
                if name and name not in seen:
                    order.append(name); seen.add(name)

    for box in order:
        try:
            typ, _ = conn.select(box, readonly=True)
            if typ != "OK":
                continue
            typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", mid)
            if typ != "OK" or not data or not data[0].split():
                continue
            uid = data[0].split()[-1]
            typ, fetched = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            raw = fetched[0][1]
            conn.logout()
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_bytes(raw)
                print(json.dumps({"found": True, "mailbox": box, "uid": uid.decode(),
                                  "bytes": len(raw), "out": args.out}))
            else:
                sys.stdout.buffer.write(raw)
            return 0
        except Exception:
            continue
    conn.logout()
    print(json.dumps({"found": False, "searched": order,
                      "message_id": mid}), file=sys.stderr)
    return 3


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor")
    d.add_argument("--account")

    a = sub.add_parser("auth-ms")
    a.add_argument("--account", required=True)

    fo = sub.add_parser("folders")
    fo.add_argument("--account", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--account", required=True)
    f.add_argument("--mailbox", default="INBOX")
    f.add_argument("--days", type=int, default=7, help="0 = no date filter (entire mailbox)")
    f.add_argument("--unseen", action="store_true")
    f.add_argument("--limit", type=int, default=100)
    f.add_argument("--offset", type=int, default=0, help="skip the N newest matches (for paging)")
    f.add_argument("--uid-range", help="IMAP UID range like 100:2500 (overrides --days; stable under concurrent deletions)")
    f.add_argument("--no-snippets", action="store_true")
    f.add_argument("--with-hosts", action="store_true", dest="with_hosts",
                   help="also return every link host per message (for sender profiling)")
    f.add_argument("--grep", help="regex searched in the BODY; only matching messages are "
                                  "returned, each with up to 3 surrounding excerpts")

    b = sub.add_parser("body")
    b.add_argument("--account", required=True)
    b.add_argument("--uid", required=True)
    b.add_argument("--mailbox", default="INBOX")
    b.add_argument("--out")

    fi = sub.add_parser("find", help="locate a message by Message-ID across folders")
    fi.add_argument("--account", required=True)
    fi.add_argument("--message-id", required=True, dest="message_id")
    fi.add_argument("--all-folders", action="store_true",
                    help="also search beyond INBOX and Trash")
    fi.add_argument("--out")

    s = sub.add_parser("send")
    s.add_argument("--account", required=True, help="sending gmail account")
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--text", help="body text (or pipe via stdin)")
    s.add_argument("--ics", help="path to .ics file to attach as a calendar invite")

    ac = sub.add_parser("act")
    ac.add_argument("--account", required=True)
    ac.add_argument("--uids", required=True, help="comma-separated UID list")
    ac.add_argument("--action", required=True, choices=["trash", "markread", "move"])
    ac.add_argument("--mailbox", default="INBOX")
    ac.add_argument("--dest", help="destination folder for --action move")

    args = p.parse_args()

    # ROUTE BEFORE DOING ANYTHING. Every command below this line assumes IMAP, and the
    # account may not be an IMAP account at all. Done here rather than inside each command
    # so a new command cannot forget - the failure would be silent, because an IMAP
    # codepath given a Graph account produces a connection error that describes the socket
    # instead of the arrangement. `doctor` is excluded: it reports on EVERY account,
    # including ones it must not dial, and does its own per-account routing.
    if args.cmd != "doctor" and getattr(args, "account", None):
        try:
            acct = account_config(args.account)
        except SystemExit:
            acct = None
        if acct is not None:
            backend = providers.backend_of(acct)
            if backend == providers.GRAPH:
                return delegate_to_graph(args.cmd, args)
            if backend == providers.CONNECTOR:
                raise SystemExit(
                    "ERROR: %s is declared as %s.\n"
                    "  Nothing in this tool fetches it - that is the configuration, not a\n"
                    "  fault. Produce the run JSON however your connector allows and pipe\n"
                    "  it into `python dashboard/ingest.py`, whose docstring documents the\n"
                    "  shape. Everything downstream - the record, the acks, the guard, the\n"
                    "  injection labelling - works identically on that path."
                    % (args.account, providers.LABEL[providers.CONNECTOR]))

    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "auth-ms":
        return ms_device_auth(args.account)
    if args.cmd == "folders":
        conn, _ = connect(args.account)
        typ, listing = conn.list()
        conn.logout()
        print(json.dumps([l.decode(errors="replace") for l in listing], indent=2))
        return 0
    if args.cmd == "fetch":
        return cmd_fetch(args)
    if args.cmd == "body":
        return cmd_body(args)
    if args.cmd == "find":
        return cmd_find(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "act":
        return cmd_act(args)


if __name__ == "__main__":
    sys.exit(main())
