"""
Ingest one daily run into the dashboard DB.

The daily routine calls this at the end of each run. Two ways to pass data:

  python dashboard/ingest.py --file run.json
  cat run.json | python dashboard/ingest.py     (reads JSON from stdin)

Expected JSON shape:
{
  "run_date": "2026-06-17",
  "notes": "optional free text",
  "accounts": [
    {"account":"user@example.com","role":"primary","status":"CONNECTED",
     "auth":"app_password","inbox_count":346,"fetched":19,"trashed":12,"kept":7}
  ],
  "messages": [
    {"account":"user@example.com","sender":"Example Social","subject":"You have 3 new notifications",
     "msg_date":"2026-06-17","disposition":"trashed","category":"social-notification",
     "reason":"rule 10 - engagement notification","importance":null},
    {"account":"user@example.com","sender":"appointments@example.org",
     "subject":"Upcoming appointment reminder","disposition":"surfaced","category":"action-needed",
     "reason":"new appointment Sep 15","importance":"action-needed"}
  ],
  "steam_sales": [
    {"app_id":123456,"title":"Example Game","discount_pct":30,
     "url":"https://store.steampowered.com/app/123456/Example_Game/"}
  ]
}

Any message missing "category" gets one inferred from its reason. Re-ingesting
the same run_date replaces that day's data (idempotent). steam_sales are keyed by
app_id and persist across runs; run steam_refresh.py afterward to pull live prices
and retire ended sales.
"""
import argparse
import json
import sys

import db
from categorize import categorize

# Two spellings of "this account is fine" reached the DB - `CONNECTED` (what mailtool doctor
# emits) and `ok` (what the hand-written run JSON used). The UI only recognised the first, so
# for four consecutive runs every account rendered as NOT connected. The old view showed
# that as a row of quiet grey dots and nobody read it as an alarm.
#
# Same disease as the category-label drift in ROUTINE step 5 and the sender-spelling drift in
# the trash panel: one concept, several spellings, no single point that pins the word down.
# Normalise on WRITE so the store carries one vocabulary, and keep the reader forgiving too -
# a normaliser that only exists in the reader lets the store keep drifting underneath it.
_STATUS_OK = {"connected", "ok", "okay", "up", "healthy", "connected."}


def normalize_status(raw):
    """Fold the known synonyms for a healthy account onto CONNECTED.

    Anything NOT recognised is passed through UNCHANGED rather than assumed healthy. The
    reader then shows it as an explicit 'unknown' rather than a silent green - an unrecognised
    status must be visible, because guessing green is how a real outage hides.
    """
    s = (raw or "").strip()
    return "CONNECTED" if s.lower() in _STATUS_OK else s


def main():
    ap = argparse.ArgumentParser(description="Ingest a daily run into the dashboard DB")
    ap.add_argument("--file", help="path to run JSON (otherwise read stdin)")
    args = ap.parse_args()

    raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    data = json.loads(raw)

    run_date = data["run_date"]
    accounts = data.get("accounts", [])
    messages = data.get("messages", [])
    steam_sales = data.get("steam_sales", [])

    for a in accounts:
        a["status"] = normalize_status(a.get("status"))

    for m in messages:
        # ACCEPT `from` AS AN ALIAS FOR `sender`. The store reads `sender`; the hand-written
        # run JSON drifted to `from` (it reads naturally next to `subject`). Nothing errored -
        # the column just went NULL, so the dashboard's From column was blank and the
        # top-senders view under-counted, silently, for four consecutive runs.
        # A key nobody validates is a silent data loss; accept both spellings and move on.
        if not m.get("sender") and m.get("from"):
            m["sender"] = m["from"]
        # Collapse folded-header whitespace. A long Subject is folded across lines in the
        # raw header, so a captured copy can carry a literal "\r\n " mid-subject. It renders
        # as a stray gap and it defeats exact-match lookups. It made correctly-linked
        # messages report as mismatches during verification.
        if m.get("subject"):
            m["subject"] = " ".join(str(m["subject"]).split())
        if not m.get("category"):
            m["category"] = categorize(m.get("reason"), m.get("subject"))

    # THE ACCOUNT IS A KEY, SO PIN ITS SPELLING ON WRITE (from a measured defect).
    # A handful of rows recorded a mailbox as
    # as a bare local-part with the domain missing. Nothing errored - it simply
    # became an EXTRA account that does not exist, splitting that mailbox's counts and making
    # those rows unresolvable to any real mailbox (the message backfill could not look them
    # up at all). Same disease as the category, filename and sender-string drift: a key that
    # nobody validates drifts one reasonable-looking call at a time and produces a confident
    # wrong number. Repair the obvious case, shout about the rest.
    known = {a.get("account") for a in accounts if a.get("account")}
    stems = {a.split("@")[0]: a for a in known if "@" in a}
    for m in messages:
        acct = (m.get("account") or "").strip()
        if acct and "@" not in acct:
            if acct in stems:
                print(f"NOTE: account {acct!r} is missing its domain - recording it as "
                      f"{stems[acct]!r}", file=sys.stderr)
                m["account"] = stems[acct]
            else:
                print(f"WARNING: account {acct!r} has no domain and matches no account in "
                      f"this run - it will become a phantom mailbox in the store",
                      file=sys.stderr)

    missing_sender = sum(1 for m in messages if not m.get("sender"))
    if messages and missing_sender == len(messages):
        # Loud, not fatal: a whole run with no sender at all is almost certainly a key-name
        # mismatch rather than a real absence, and it must not pass quietly again.
        print(f"WARNING: all {len(messages)} messages have no sender - check the run JSON's "
              f"key name (expected 'sender', 'from' also accepted)", file=sys.stderr)

    run_id = db.ingest_run(run_date, accounts=accounts, messages=messages,
                           notes=data.get("notes"), steam_sales=steam_sales)
    print(json.dumps({
        "ok": True, "run_id": run_id, "run_date": run_date,
        "accounts": len(accounts), "messages": len(messages),
        "steam_sales": len(steam_sales),
        "trashed": sum(1 for m in messages if m.get("disposition") == "trashed"),
        "kept": sum(1 for m in messages if m.get("disposition") in ("kept", "surfaced")),
    }))


if __name__ == "__main__":
    main()
