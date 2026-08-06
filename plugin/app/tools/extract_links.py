"""Extract hyperlinks + text from an email. Usage: python tools/extract_links.py <account> <uid> [filter]"""
import re
import subprocess
import sys
from pathlib import Path

import email

sys.stdout.reconfigure(encoding="utf-8")
account, uid = sys.argv[1], sys.argv[2]
needle = sys.argv[3].lower() if len(sys.argv) > 3 else None

raw = subprocess.run(
    [sys.executable, str(Path(__file__).parent / "mailtool.py"), "body",
     "--account", account, "--uid", uid],
    capture_output=True, check=True).stdout
msg = email.message_from_bytes(raw)
for part in msg.walk():
    if part.get_content_type() in ("text/html", "text/plain"):
        t = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        links = list(dict.fromkeys(re.findall(r"https?://[^\s\"'<>)]+", t)))
        print("--- LINKS ---")
        for l in links:
            if needle is None or needle in l.lower():
                print(l[:200])
        text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        print("--- BODY ---")
        print(text[:1200])
        break
