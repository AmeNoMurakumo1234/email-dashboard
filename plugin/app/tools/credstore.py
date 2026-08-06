"""DPAPI-encrypted secret store for the email-cleanup task.

Named credstore, NOT secrets: a module called secrets.py on sys.path shadows the standard
library's for the whole process.

Secrets live in secrets/secrets.store as a base64 DPAPI blob wrapping a JSON
object: { "<account>": { "<field>": "<value>", ... }, ... }
Fields used: password, app_password, ms_refresh_token, ms_access_token, ms_token_expiry.

DPAPI ties the blob to this Windows user on this machine - same model as the
original accounts.credentials.enc.

CLI (never prints secret values):
  python tools/credstore.py list
  python tools/credstore.py set <account> <field>    # prompts if a terminal, else stdin
  python tools/credstore.py del <account> [<field>]
  python tools/credstore.py import-json              # merge {"acct":{"field":"val"}} from stdin
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "secrets" / "secrets.store"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32


def _in_blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take_bytes(blob: DATA_BLOB) -> bytes:
    data = ctypes.string_at(blob.pbData, blob.cbData)
    _kernel32.LocalFree(blob.pbData)
    return data


def protect(data: bytes) -> bytes:
    inp, out = _in_blob(data), DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    return _take_bytes(out)


def unprotect(data: bytes) -> bytes:
    inp, out = _in_blob(data), DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed - wrong Windows user or corrupted store")
    return _take_bytes(out)


def load() -> dict:
    if not STORE.exists():
        return {}
    return json.loads(unprotect(base64.b64decode(STORE.read_text())))


def save(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(base64.b64encode(protect(json.dumps(store).encode())).decode())


def get(account: str, field: str):
    return load().get(account, {}).get(field)


def set_value(account: str, field: str, value: str) -> None:
    store = load()
    store.setdefault(account, {})[field] = value
    save(store)


def set_values(account: str, fields: dict) -> None:
    """Write several fields in ONE read-modify-write.

    Storing an OAuth result is three fields - access token, refresh token, expiry - and
    doing them one at a time is three full decrypt-modify-encrypt cycles of the entire
    store. Wasteful, and genuinely racy: another writer between two of them loses whatever
    it wrote. One transaction, so a token set lands whole or not at all.
    """
    store = load()
    store.setdefault(account, {}).update({k: v for k, v in fields.items() if v is not None})
    save(store)


USAGE = """usage:
  python tools/credstore.py list
  python tools/credstore.py set <account> <field>   # prompts if a terminal, else stdin
  python tools/credstore.py del <account> [<field>]
  python tools/credstore.py import-json             # {"acct":{"field":"val"}} on stdin

fields: password, app_password, ms_refresh_token, ms_access_token, ms_token_expiry
values are never printed back."""


def _read_value(field):
    """Prompt when a human is at the terminal; read stdin when piped.

    THE OLD BEHAVIOUR WAS WORSE THAN A USAGE ERROR. This read stdin unconditionally and
    parsed argv positionally with no argparse, so someone following a documented
    `set --account <address>` bound account="--account", field=the address, and then blocked
    silently on stdin.read() with nothing printed at all - a hung terminal and no prompt, at
    exactly the moment the instructions had told them to be careful with a password. A
    visible usage message is a better outcome than that, and a prompt is better still.

    getpass keeps the value off the screen and out of shell history either way.
    """
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(f"{field} (input hidden, not echoed): ")
    return sys.stdin.read()


def main(argv):
    cmd = argv[0] if argv else "list"
    # Reject flag-shaped arguments outright rather than silently treating them as values.
    flags = [a for a in argv[1:] if a.startswith("-")]
    if flags:
        print(f"ERROR: unexpected option {flags[0]!r} - this CLI takes positional "
              f"arguments only.\n\n{USAGE}", file=sys.stderr)
        return 2
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "list":
        store = load()
        for account in sorted(store):
            print(f"{account}: {', '.join(sorted(store[account]))}")
        if not store:
            print("(store is empty)")
    elif cmd == "set":
        if len(argv) < 3:
            print(f"ERROR: set needs <account> and <field>.\n\n{USAGE}", file=sys.stderr)
            return 2
        account, field = argv[1], argv[2]
        value = _read_value(field).strip()
        if field == "app_password":
            value = value.replace(" ", "")  # Gmail displays app passwords with spaces
        if not value:
            print("ERROR: empty value on stdin", file=sys.stderr)
            return 1
        set_value(account, field, value)
        print(f"stored {field} for {account} ({len(value)} chars)")
    elif cmd == "del":
        if len(argv) < 2:
            print("ERROR: del needs <account>.\n\n" + USAGE, file=sys.stderr)
            return 2
        store = load()
        account = argv[1]
        if len(argv) > 2:
            store.get(account, {}).pop(argv[2], None)
        else:
            store.pop(account, None)
        save(store)
        print("deleted")
    elif cmd == "import-json":
        incoming = json.loads(sys.stdin.read())
        store = load()
        for account, fields in incoming.items():
            store.setdefault(account, {}).update(fields)
        save(store)
        print(f"imported {len(incoming)} account(s)")
    else:
        print("ERROR: unknown command %r.\n\n%s" % (cmd, USAGE), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
