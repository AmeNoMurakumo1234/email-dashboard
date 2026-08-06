"""DPAPI-encrypted secret store for the email-cleanup task.

Secrets live in secrets/secrets.store as a base64 DPAPI blob wrapping a JSON
object: { "<account>": { "<field>": "<value>", ... }, ... }
Fields used: password, app_password, ms_refresh_token, ms_access_token, ms_token_expiry.

DPAPI ties the blob to this Windows user on this machine - same model as the
original accounts.credentials.enc.

CLI (never prints secret values):
  python tools/secrets.py list
  python tools/secrets.py set <account> <field>      # value read from stdin
  python tools/secrets.py del <account> [<field>]
  python tools/secrets.py import-json                # merge {"acct":{"field":"val"}} from stdin
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


def main(argv):
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        store = load()
        for account in sorted(store):
            print(f"{account}: {', '.join(sorted(store[account]))}")
        if not store:
            print("(store is empty)")
    elif cmd == "set":
        account, field = argv[1], argv[2]
        value = sys.stdin.read().strip()
        if field == "app_password":
            value = value.replace(" ", "")  # Gmail displays app passwords with spaces
        if not value:
            print("ERROR: empty value on stdin", file=sys.stderr)
            return 1
        set_value(account, field, value)
        print(f"stored {field} for {account} ({len(value)} chars)")
    elif cmd == "del":
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
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
