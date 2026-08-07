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
# ENDING AT THE PRESENT. "Arriving faster" is a claim in the present tense, and this fixture
# used to leave its last notice 23 days back on a 6-day cadence - four cycles of silence,
# described as speeding up. It passed only because the code shared the same blind spot: the
# arithmetic looked at gaps BETWEEN arrivals and never at the gap between the last one and
# now. The live store had this at 246 days silent with a 4-day median, badged "arriving
# faster". An accelerating series is one that is still arriving.
shift = (len(RUNS) - 1) - acc[-1]
acc = [i + shift for i in acc]
c = build([(RUNS[p], "Biller", "Overdue notice", f"<a{i}@b>") for i, p in enumerate(acc)])
row = items(c, min=3).get("Overdue notice")
if not row or not row["accelerating"]:
    fails.append("an accelerating series (gaps 12,8,4,2) ending TODAY was NOT flagged - "
                 "the branch cannot fire, so a zero from it means nothing")
if row and row.get("dormant"):
    fails.append("a series that arrived today was marked dormant")

# 4b. THE OTHER SIDE OF IT: the identical series, stopped. Same gaps, same shape, same
#     everything except that it ended and nothing came after. It must NOT be called
#     accelerating, and it must be marked dormant so it cannot outrank a live one.
early = []
pos = 0
for gap in (12, 8, 4, 2):
    early.append(pos)
    pos += gap
early.append(pos)
c = build([(RUNS[p], "Biller", "Overdue notice", f"<b{i}@b>") for i, p in enumerate(early)])
row = items(c, min=3).get("Overdue notice")
if not row:
    fails.append("the stalled series vanished entirely - it is history, not noise")
else:
    if row["accelerating"]:
        fails.append("a series silent for several of its own cycles was still called "
                     "'arriving faster' - the present-tense claim nobody can act on")
    if not row["dormant"]:
        fails.append("a long-stopped series was not marked dormant, so it competes with "
                     "the ones still arriving")

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
# ---------------------------------------------------------------------------------------
# GAPS ARE CALENDAR DAYS, NOT RUNS.
#
# "Arriving faster" is a claim about the world. Counted in runs it was partly a claim about
# how often the tool ran - survivable while every run was a daily sweep, and not once a
# historical intake staged one run per arrival day from a single mailbox. On the reporting
# store that took 252 runs, of which ~51 were sweeps.
#
# The danger is not that the numbers grow, it is that they grow UNEVENLY: acceleration
# compares early gaps against recent ones, so an intake concentrated in one period can
# manufacture an acceleration that never happened.
def _case_days_not_runs():
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server                                                  # noqa: PLC0415

    class Cur:
        def __init__(self, items):
            self.items = items

        def fetchall(self):
            return self.items

        def __iter__(self):
            return iter(self.items)

    class Conn:
        def __init__(self, rows_, runs_):
            self.rows, self.runs = rows_, runs_

        def execute(self, sql, args=()):
            if "FROM runs" in sql:
                return Cur([(d,) for d in self.runs])
            return Cur(self.rows)

    # A steady monthly notice: four arrivals, 28 days apart. Not accelerating, whatever
    # the run history looks like.
    dates = ["2026-01-01", "2026-01-29", "2026-02-26", "2026-03-26"]
    rows_ = [{"run_date": d, "account": "a@x", "sender": "Biller <b@example.com>",
              "subject": "statement ready", "disposition": "kept", "category": "bill",
              "concept": "money (bills, receipts, banking)",
              "message_id": "<%s@x>" % d} for d in dates]

    # Sparse run history: only the four days themselves.
    sparse = server.api_repeats(Conn(rows_, dates), {})
    # Dense run history: a backfill added ninety single-mailbox days in the middle.
    dense_runs = sorted(set(dates) | {"2026-02-%02d" % (i + 1) for i in range(28)})
    dense = server.api_repeats(Conn(rows_, dense_runs), {})

    def cadence(out):
        return [(i["median_gap"], i["accelerating"]) for i in out["items"]]

    if not cadence(sparse):
        fails.append("MISS: the steady series did not qualify at all, so this proves nothing")
    elif cadence(sparse) != cadence(dense):
        fails.append("DRIFT: the same series read differently once the run history grew - "
                     "%s vs %s" % (cadence(sparse), cadence(dense)))
    # An unreadable date must DROP OUT, not contribute a zero. A zero merges two real gaps
    # into a third that never happened, and it lands in the middle of the series where the
    # acceleration comparison is most sensitive to it.
    bad = [dict(r) for r in rows_]
    bad[2]["run_date"] = "not-a-date"
    bad_dates = [r["run_date"] for r in bad]
    out_bad = server.api_repeats(Conn(bad, sorted(set(bad_dates))), {})
    # Valid pairs are 28 and 56 days, so the median must be 42. A zero appended for the
    # unreadable pair gives [28, 56, 0] and a median of 28 - a plausible-looking number,
    # which is why "is it zero?" was the wrong question to ask.
    for i in out_bad["items"]:
        if i["median_gap"] != 42:
            fails.append("MISS: an unreadable date changed the cadence to %r, and the two "
                         "readable gaps are 28 and 56 - it should drop out, not contribute "
                         "an interval that never happened" % i["median_gap"])

    for i in sparse["items"]:
        if i["median_gap"] != 28:
            fails.append("WRONG UNIT: a monthly series reported a median gap of %r, and it "
                         "is 28 days" % i["median_gap"])
        if i["accelerating"]:
            fails.append("FALSE ALARM: a perfectly steady series called accelerating")
        if i.get("gap_unit") != "days":
            fails.append("MISS: the payload does not say the gap is in days, so a reader "
                         "keeps the old run-based reading")


_case_days_not_runs()


print("  under-count four distinct arrivals are four")
print("  shape ...... changing amounts/dates still collapse")
print("  control .... an accelerating series IS flagged")
print("  control .... a steady series is NOT")
print("  honesty .... unlinked rows fall back and say so")
print("  unit ....... gaps are calendar days, unmoved by run history")
if fails:
    print(f"\n{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
