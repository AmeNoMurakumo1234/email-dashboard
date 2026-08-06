"""Two-sided control on the repeat-collapsing endpoint.

The panel exists to stop a recurring notice from drowning, so it has exactly two ways to
fail and both are bad:

  * UNDER-count and the escalating thing stays invisible - the failure it was built to fix.
  * OVER-count and it manufactures urgency, which trains the reader to ignore it - and the
    first build did exactly that, reporting "6th notice" about ONE bank statement that had
    simply been re-listed on six consecutive runs.

Run: python dashboard/test_repeats.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server                                                        # noqa: E402

fails = []
RUNS = [f"2026-06-{d:02d}" for d in range(1, 31)] + \
       [f"2026-07-{d:02d}" for d in range(1, 31)]


def build(rows):
    """rows: (run_date, sender, subject, message_id)"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE messages (run_date TEXT, account TEXT, sender TEXT, "
              "subject TEXT, disposition TEXT, category TEXT, concept TEXT, "
              "importance TEXT, message_id TEXT)")
    c.execute("CREATE TABLE runs (run_date TEXT)")
    for d in RUNS:
        c.execute("INSERT INTO runs VALUES (?)", (d,))
    for rd, sender, subj, mid in rows:
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                  (rd, "mailbox@example.com", sender, subj, "kept", "bill",
                   "money (bills, receipts, banking)", "fyi", mid))
    return c


def items(c, **q):
    qq = {k: [str(v)] for k, v in q.items()}
    return {i["subject"]: i for i in server.api_repeats(c, qq)["items"]}


# 1. THE OVER-COUNT BUG: one message re-listed on six consecutive runs is ONE notice.
c = build([(RUNS[i], "Example Bank", "Your statement is available", "<one@bank.example>")
           for i in range(6)])
got = items(c, min=3)
if got:
    fails.append(f"one message re-listed 6x was reported as a repeat: "
                 f"{[(k, v['notices']) for k, v in got.items()]}")

# 2. THE UNDER-COUNT FAILURE: four genuinely distinct arrivals must be seen as four.
c = build([(RUNS[i * 7], "Biller", "Payment due", f"<n{i}@biller>") for i in range(4)])
got = items(c, min=3)
row = got.get("Payment due")
if not row:
    fails.append("four distinct arrivals were not reported as a repeat at all")
elif row["notices"] != 4 or row["basis"] != "messages":
    fails.append(f"expected 4 notices on the messages basis, got "
                 f"{row.get('notices')} on {row.get('basis')}")

# 3. THE SHAPE: a changing amount or date must not split one recurring notice into many.
c = build([(RUNS[i * 7], "Biller", f"Payment of $12.{i}4 due 0{i+1}/21", f"<s{i}@b>")
           for i in range(4)])
if len(items(c, min=3)) != 1:
    fails.append("a notice whose amount/date changes was not recognised as recurring")

# 4. POSITIVE CONTROL ON ACCELERATION - the live data reports zero accelerating items, and
#    a zero is only worth believing if the branch can fire. Gaps 12, 8, 4, 2 must trip it.
pos, acc = 0, []
for gap in (12, 8, 4, 2):
    acc.append(pos)
    pos += gap
acc.append(pos)
c = build([(RUNS[p], "Biller", "Overdue notice", f"<a{i}@b>") for i, p in enumerate(acc)])
row = items(c, min=3).get("Overdue notice")
if not row or not row["accelerating"]:
    fails.append("an accelerating series (gaps 12,8,4,2) was NOT flagged - the branch "
                 "cannot fire, so a zero from it means nothing")

# 5. And a steady monthly series must NOT be called acceleration.
c = build([(RUNS[i * 10], "Biller", "Monthly statement", f"<m{i}@b>") for i in range(5)])
row = items(c, min=3).get("Monthly statement")
if row and row["accelerating"]:
    fails.append("a steady monthly series was reported as accelerating")

# 6. Mixed/unlinked rows must fall back AND admit it, never quote an exact count.
c = build([(RUNS[i], "Biller", "Mixed notice", ("<x@b>" if i else None)) for i in range(4)])
row = items(c, min=3).get("Mixed notice")
if row and row["basis"] != "listings":
    fails.append("rows without message ids did not fall back to the approximate basis")
if row and row["accelerating"]:
    fails.append("acceleration was claimed on the approximate basis")

print("=== repeat-collapsing: two-sided control ===")
print("  over-count . one message re-listed 6x is ONE notice")
print("  under-count four distinct arrivals are four")
print("  shape ...... changing amounts/dates still collapse")
print("  control .... an accelerating series IS flagged")
print("  control .... a steady series is NOT")
print("  honesty .... unlinked rows fall back and say so")
if fails:
    print(f"\n{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
