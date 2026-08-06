"""Two-sided control on the SHIPPED api_quiet, against a synthetic store.

A detector is only trustworthy if it both FIRES on a real absence and STAYS SILENT on a
sender that is merely bursty or still active. Testing the prototype proves nothing about
the code that actually serves the page, so this drives server.api_quiet itself.
"""
import os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

RUNS = [f"2026-06-{d:02d}" for d in range(1, 31)] + [f"2026-07-{d:02d}" for d in range(1, 21)]


def build(spec):
    """spec: {sender: [run indices it appeared on]} -> in-memory conn"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (sender TEXT, run_date TEXT, category TEXT)")
    conn.execute("CREATE TABLE runs (run_date TEXT)")
    # The lattice is EVERY run day - including days that triaged nothing. That is the
    # point: a run with no mail is still a day I looked.
    for d in RUNS:
        conn.execute("INSERT INTO runs VALUES (?)", (d,))
    for sender, days in spec.items():
        for i in days:
            conn.execute("INSERT INTO messages VALUES (?,?,?)", (sender, RUNS[i], "promo"))
    return conn


def flagged(conn):
    return {i["sender"]: i for i in server.api_quiet(conn, {})["items"]}


fails = []

# 1. MUST FIRE: steady weekly sender that stops dead well past its worst gap.
c = build({"Weekly Biller": [0, 7, 14, 21, 28, 35]})       # last at run 35 of 49 -> silent 14
f = flagged(c)
if "weekly biller" not in f:
    fails.append("MISS: a steady weekly sender silent 14 runs was not flagged")
else:
    it = f["weekly biller"]
    if it["silent_runs"] != 14 or it["worst_gap"] != 7:
        fails.append(f"WRONG NUMBERS: {it['silent_runs']} silent / worst {it['worst_gap']}")

# 2. MUST NOT FIRE: still-active daily sender.
c = build({"Daily Noise": list(range(0, 50, 1))})
if "daily noise" in flagged(c):
    fails.append("FALSE ALARM: a still-active daily sender was flagged")

# 3. MUST NOT FIRE: a BURST - 5 consecutive days then nothing. Not a cadence at all.
c = build({"Burst Sender": [0, 1, 2, 3, 4]})
if "burst sender" in flagged(c):
    fails.append("FALSE ALARM: a 5-day burst was treated as an established rhythm")

# 4. MUST NOT FIRE: irregular sender whose current silence is within its worst gap.
c = build({"Irregular": [0, 20, 22, 24, 40]})              # worst gap 20, silent 9
if "irregular" in flagged(c):
    fails.append("FALSE ALARM: silence inside the sender's own worst gap was flagged")

# 5. THE FOLD: two spellings of one sender must not read as one going silent.
c = build({"Acme Bank <no-reply@acme.example>": [0, 7, 14],
           "Acme Bank": [21, 28, 35, 42, 49]})
f = flagged(c)
if "acme bank" in f:
    fails.append("FALSE ALARM: a sender that kept arriving under another spelling was flagged")
c2 = build({"Acme Bank <no-reply@acme.example>": [0, 7, 14, 21, 28, 35]})
if "acme bank" not in flagged(c2):
    fails.append("MISS: the folded sender should still be flagged when it truly stops")

print("=== two-sided control on server.api_quiet ===")
for line in ("1 fires on a real stop", "2 silent on an active sender",
             "3 silent on a burst", "4 silent inside worst gap",
             "5 fold does not invent silence, and still catches a true stop"):
    print("  case", line)
if fails:
    print("\nFAILURES:")
    for x in fails:
        print("  -", x)
    sys.exit(1)
print("\nALL PASS - the detector fires when it should and stays quiet when it should.")

