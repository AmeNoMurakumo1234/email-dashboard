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
import re
import socket
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

ROOT = Path(__file__).resolve().parent.parent
# utf-8-sig, not utf-8: these are hand-edited files on Windows, where editors still add
# a BOM by default and json.load raises on one. The installer wrote a BOM here for a
# while and made accounts.json unreadable on every fresh install.
CONFIG = json.loads((ROOT / "config" / "accounts.json").read_text(encoding="utf-8-sig"))

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
MS_AUTHORITY = str(CONFIG.get("ms_authority") or "common").strip("/ ") or "common"
MS_TOKEN_URL = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % MS_AUTHORITY
MS_DEVICECODE_URL = ("https://login.microsoftonline.com/%s/oauth2/v2.0/devicecode"
                     % MS_AUTHORITY)
MS_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def ms_client_id():
    """The Entra ID app registration this tool signs in through.

    Read through a function so a missing key names itself and says what to do. It used to be
    CONFIG["ms_client_id"] inline, so the first thing every Microsoft user met was a bare
    KeyError with no indication that an app registration was needed at all - and the
    onboarding skill never mentioned the step either.

    ONE registration per deployment, not per user: an admin registers it once, consents it
    for the tenant, and everyone shares the id. It is not a secret - it identifies the app,
    not the person - which is why it lives in accounts.json rather than the credential store.
    """
    cid = (CONFIG.get("ms_client_id") or "").strip()
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


def account_config(addr):
    for acct in CONFIG["accounts"]:
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

def connect(addr):
    """Returns (imap_connection, method_used). Raises RuntimeError with a fix-it hint."""
    acct = account_config(addr)
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

def cmd_doctor(args):
    targets = [a for a in CONFIG["accounts"] if not args.account or a["email"].lower() == args.account.lower()]
    results, ok_count = [], 0
    for acct in targets:
        addr = acct["email"]
        try:
            conn, method = connect(addr)
            typ, data = conn.select("INBOX", readonly=True)
            count = data[0].decode() if typ == "OK" else "?"
            trash = find_trash(conn)
            conn.logout()
            results.append({"account": addr, "status": "CONNECTED", "auth": method,
                            "inbox_messages": count, "trash_folder": trash})
            ok_count += 1
        except Exception as exc:  # report every account regardless of individual failures
            results.append({"account": addr, "status": "FAILED", "error": str(exc)})
    print(json.dumps({"connected": ok_count, "total": len(targets), "accounts": results}, indent=2))
    return 0 if ok_count == len(targets) else 1


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
        messages.append(entry)
    conn.logout()
    print(json.dumps({"account": args.account, "mailbox": args.mailbox,
                      "total_matched": total_matched, "returned": len(uids),
                      "offset_from_newest": args.offset, "messages": messages},
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
