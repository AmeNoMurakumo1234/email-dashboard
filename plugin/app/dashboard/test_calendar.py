"""The record shows when mail ARRIVED, not when a sweep happened to run.

WHY THIS EXISTS. Both dates were stored from the beginning and only `run_date` was ever
queried - 61 uses in server.py against zero for `msg_date`. So an onboarding intake, which
triages months of existing mail in one session, rendered as a SINGLE tile: "1 run, 93
messages" for mail spanning January to August. The one thing a new user most wants to see -
the shape of what they have been missing - was the one thing the view could not show.

It was not a missing column. It was the wrong column.

AND IT IS NOT A ONE-LINE GROUP BY, which is what makes it worth a test. `msg_date` holds
whatever each run wrote: a live store had ISO dates, RFC 2822 dates ("Wed, 5 Aug 2026
06:06:25 -0500") and NULLs in the same column. Grouping on the raw text buckets those RFC
rows under their weekday and produces a calendar that looks plausible and is wrong. So the
day is derived on write into `msg_day`, exactly as `concept` sits beside `category`: the raw
value is evidence and is kept, the derived one is what queries may trust.

Runs against a throwaway database. Nothing here touches the live store.

    python dashboard/test_calendar.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL"), name, ("" if cond else f"-> {detail}"))
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="emaildash-cal-")
try:
    # A private copy of the module set, pointed at a private database.
    for name in ("db.py", "concepts.py", "categorize.py", "categorize.example.json",
                 "concepts.example.json", "server.py", "ingest.py", "mailview.py"):
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, name))
    sys.path.insert(0, tmp)
    for mod in ("db", "server", "concepts", "categorize"):
        sys.modules.pop(mod, None)
    import db                                                        # noqa: E402
    db.DB_PATH = os.path.join(tmp, "test.db")
    import server                                                    # noqa: E402
    server.DB_PATH = db.DB_PATH

    print("=== the normaliser handles every format a real store contains ===")
    for raw, want, why in (
            ("2026-03-14", "2026-03-14", "already ISO"),
            ("2026-03-14T09:12:00+00:00", "2026-03-14", "ISO with a time"),
            ("Wed, 5 Aug 2026 06:06:25 -0500", "2026-08-05", "RFC 2822"),
            ("Wed, 05 Aug 2026 16:17:13 +0000", "2026-08-05", "RFC 2822, padded day"),
            ("5 Aug 2026 22:41:27 +0000", "2026-08-05", "RFC 2822, no weekday"),
    ):
        check(f"{why}: {raw[:34]!r}", db.msg_day(raw) == want, db.msg_day(raw))
    check("an empty value falls back", db.msg_day("", "2026-01-01") == "2026-01-01")
    check("None falls back", db.msg_day(None, "2026-01-01") == "2026-01-01")
    # Guessing at unparseable input is how a wrong date enters the record silently.
    check("unparseable input does NOT guess",
          db.msg_day("sometime last tuesday", None) is None,
          db.msg_day("sometime last tuesday", None))

    print("\n=== a backfill spans the days it arrived on, not the day it was ingested ===")
    # A synthetic intake: one message every few days across ~8 months, ALL ingested under a
    # single run date - exactly the shape of an onboarding backfill.
    RUN = "2026-08-06"
    msgs = []
    for i in range(40):
        month = 1 + (i // 5)
        day = 1 + (i % 5) * 5
        msgs.append({"account": "me@example.invalid", "sender": f"Sender {i%4} <s@b.example>",
                     "subject": f"message {i}", "msg_date": f"2026-{month:02d}-{day:02d}",
                     "disposition": "kept" if i % 3 else "trashed",
                     "category": "newsletter", "reason": "test", "importance": ""})
    # ...plus a handful in the RFC form, which is where a naive GROUP BY goes wrong.
    for d in ("Wed, 5 Aug 2026 06:06:25 -0500", "Tue, 4 Aug 2026 14:39:48 -0700"):
        msgs.append({"account": "me@example.invalid", "sender": "RFC <r@b.example>",
                     "subject": "rfc dated", "msg_date": d, "disposition": "kept",
                     "category": "newsletter", "reason": "test", "importance": ""})
    db.ingest_run(RUN, accounts=[], messages=msgs, notes="synthetic intake")

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    arrived = server.api_calendar(conn, {})
    swept = server.api_calendar(conn, {"by": ["swept"]})

    check("by=swept still sees ONE run day (that is what a sweep is)",
          len(swept["days"]) == 1, [d["day"] for d in swept["days"]])
    check("by=arrived spreads them across the months they came from",
          len(arrived["days"]) > 20, len(arrived["days"]))
    check("...spanning the real range",
          arrived["days"][0]["day"].startswith("2026-01")
          and arrived["days"][-1]["day"].startswith("2026-08"),
          (arrived["days"][0]["day"], arrived["days"][-1]["day"]))
    check("no message is lost or duplicated by the regrouping",
          arrived["totals"]["messages"] == swept["totals"]["messages"] == len(msgs),
          (arrived["totals"]["messages"], swept["totals"]["messages"], len(msgs)))
    check("the RFC-dated rows land on their real day, not under a weekday",
          any(d["day"] == "2026-08-05" for d in arrived["days"])
          and not any(d["day"].startswith("Wed") for d in arrived["days"]),
          [d["day"] for d in arrived["days"] if not d["day"].startswith("2026")])
    check("every cell carries a run_date so clicking one still selects a run",
          all("run_date" in d for d in arrived["days"]))
    conn.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL PASS - an intake of months of mail renders as months, and a sweep still "
      "renders as a sweep.")
