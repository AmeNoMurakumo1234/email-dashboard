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
    if it["silent_days"] != 14 or it["worst_gap"] != 7 or it["gap_unit"] != "days":
        fails.append(f"WRONG NUMBERS: {it['silent_days']} silent / worst "
                     f"{it['worst_gap']} / unit {it['gap_unit']}")

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

# ---------------------------------------------------------------------------------------
# A BACKFILL MUST NOT MANUFACTURE SILENCE.
#
# Found on the live store: 252 runs, of which 51 were real sweeps and 139 contained exactly
# ONE mailbox, because a historical intake stages one run per arrival day and a given day's
# batch comes from a single account. Measuring every sender against every run then counts
# "a day when somebody else's mailbox was being backfilled" as a day this sender was silent.
# A monthly biller went from a normal cadence to "3.69x its own worst gap" without changing
# its behaviour at all.
class _Cur:
    """A cursor-shaped wrapper: fetchall(), and iterable for PRAGMA-style reads."""

    def __init__(self, items):
        self.items = items

    def fetchall(self):
        return self.items

    def __iter__(self):
        return iter(self.items)


def _case_backfill():
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server                                                  # noqa: PLC0415

    class Conn:
        """Just enough of a store: two mailboxes, and runs that only ever cover one."""

        def __init__(self, rows_, runs_):
            self.rows, self.runs = rows_, runs_

        def execute(self, sql, args=()):
            # server.rows() calls .fetchall(), so the fake has to answer like a cursor
            # rather than like a list - a fake that is merely list-shaped fails inside the
            # code under test and reports it as a bug in the code.
            if "table_info" in sql:
                return _Cur([(0, "account", "TEXT")])
            if "account_status" in sql:
                # This fixture has no account_status rows, which is exactly the shape a
                # store left by --by-arrival has. Returning the message rows here would
                # have quietly re-supplied the very days the test is checking are absent.
                return _Cur([])
            if "FROM runs" in sql:
                return _Cur([{"run_date": d} for d in self.runs])
            return _Cur(self.rows)

    # `a@x` is swept on ten consecutive days and appears on every one of them - it has not
    # gone quiet by any measure. `b@x` is then backfilled across twenty later days.
    # Spread so the sender actually QUALIFIES: enough appearances, spanning enough runs.
    # The first version packed ten appearances into nine runs, which is under the minimum
    # span - so the control "a real stop is still caught" failed because the sender was
    # never established, not because the stop was missed. A fixture that cannot qualify
    # tests nothing and blames the code.
    rows_ = []
    for i in range(10):
        rows_.append({"sender": "Biller <biller@example.com>", "account": "a@x",
                      "run_date": "2026-01-%02d" % (i * 3 + 1), "category": "bill"})
    for i in range(28):
        rows_.append({"sender": "Filler <filler@example.com>", "account": "a@x",
                      "run_date": "2026-01-%02d" % (i + 1), "category": "promo"})
    for i in range(20):
        rows_.append({"sender": "Other <other@example.com>", "account": "b@x",
                      "run_date": "2026-02-%02d" % (i + 1), "category": "promo"})
    runs_ = sorted({r["run_date"] for r in rows_})

    out = server.api_quiet(Conn(rows_, runs_), {})
    flagged = {i["sender"] for i in out["items"]}
    if "biller" in flagged:
        fails.append("FALSE ALARM: a sender counted silent on days that swept a DIFFERENT "
                     "mailbox - a backfill manufacturing silence")
    # And the control: the same shape, but the biller really does stop.
    rows2 = [r for r in rows_ if r["account"] == "a@x"]
    for i in range(20):
        rows2.append({"sender": "Other <other@example.com>", "account": "a@x",
                      "run_date": "2026-02-%02d" % (i + 1), "category": "promo"})
    out2 = server.api_quiet(Conn(rows2, sorted({r["run_date"] for r in rows2})), {})
    if "biller" not in {i["sender"] for i in out2["items"]}:
        fails.append("MISS: a real stop in a mailbox that WAS swept must still be caught")
    for i in out2.get("items", []):
        if not isinstance(i.get("observed_days"), int) or i["observed_days"] < 1:
            fails.append("MISS: a flagged item does not state its own denominator, so the "
                         "reader divides by the global run count")
        break


_case_backfill()


# ---------------------------------------------------------------------------------------
# CASES 7-12: what the runs-based version could not express, and one thing it got WRONG.
#
# The panel counted RUNS ELAPSED. That was sound while every run was a daily sweep and
# stopped being the same quantity the moment a backfill existed - a year of arrival-dated
# history packs hundreds of runs into the past while the present accrues one a day, so "23
# runs" in 2025 and "23: runs" in 2026 describe different amounts of the world. It reported
# "silent 105 of the 173 runs that looked at this mailbox", which is a true sentence about
# the store and tells nobody anything about their bank.

def build_dates(spec, runs=None):
    """spec: {sender: [YYYY-MM-DD, ...]}; runs defaults to the union of those dates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (sender TEXT, run_date TEXT, category TEXT, "
                 "account TEXT)")
    conn.execute("CREATE TABLE runs (run_date TEXT)")
    days = sorted(runs or {d for v in spec.values() for d in v})
    for d in days:
        conn.execute("INSERT INTO runs VALUES (?)", (d,))
    for sender, ds in spec.items():
        for d in ds:
            conn.execute("INSERT INTO messages VALUES (?,?,?,?)",
                         (sender, d, "promo", "a@x"))
    # A HEARTBEAT: the mailbox kept receiving mail on every run day.
    #
    # Without it the account's only evidence of being looked at IS the sender under test,
    # so its observation window ends the day that sender stopped and it can never read as
    # silent. That is the code being right - nothing in such a store shows anyone looked
    # afterwards - and a fixture that omits it is modelling a mailbox that cannot exist.
    # Case 6 has always done this, with its Filler sender.
    for d in days:
        conn.execute("INSERT INTO messages VALUES ('Heartbeat <hb@e.example>',?,?,?)",
                     (d, "promo", "a@x"))
    return conn


def days_from(start, step, n):
    from datetime import date, timedelta
    y, m, dd = (int(x) for x in start.split("-"))
    d0 = date(y, m, dd)
    return [(d0 + timedelta(days=step * i)).isoformat() for i in range(n)]


# 7. A TINY WORST GAP MUST NOT PRODUCE AN ALARM FROM A MULTIPLE.
#    A sender whose worst gap was 2 days clears "5x its worst" by pausing over a long
#    weekend. The live panel carried one such row at 5x and another at 1.25x - the second
#    is not an anomaly, it is rounding - beside a bank that had genuinely vanished.
# 15 appearances every 2 days ends 2026-01-29; the window closes 10 days later, so the
# pause is genuinely short. The first version ran the window out to 2026-03-01 - a 31-day
# silence from a 2-day sender, which SHOULD fire. The fixture was wrong, not the code.
runs = days_from("2026-01-01", 1, 39)
c = build_dates({"Chatty": days_from("2026-01-01", 2, 15)}, runs)
if "chatty" in flagged(c):
    fails.append("FALSE ALARM: a 2-day-cadence sender flagged for a short pause - a "
                 "multiple of a tiny gap is not evidence")

# 8. ...AND THE CONTROL: the same sender gone long enough that it IS worth saying. Without
#    this, a floor set absurdly high would pass case 7 and destroy the panel.
c = build_dates({"Chatty": days_from("2026-01-01", 2, 15)},
                days_from("2026-01-01", 1, 150))
if "chatty" not in flagged(c):
    fails.append("MISS: the absolute floor swallowed a sender that really did stop - the "
                 "floor must suppress noise, not findings")

# 9. THE UNIT IS DAYS, AND IT IS DAYS EVEN WHEN THE RUNS ARE NOT DAILY.
#    Two stores describing the same world: one swept every day, one swept every third day.
#    A run-counted panel gives two different answers. The sender behaved identically.
sender_days = days_from("2026-01-01", 7, 8)
dense_runs = days_from("2026-01-01", 1, 120)
dense = build_dates({"Weekly": sender_days}, dense_runs)
# Same first and LAST day - the only difference is how often the tool looked in between.
# Without pinning the last day the two windows close two days apart and the comparison
# measures the fixture rather than the code.
sparse_runs = sorted(set(days_from("2026-01-01", 3, 40)) | set(sender_days)
                     | {dense_runs[0], dense_runs[-1]})
sparse = build_dates({"Weekly": sender_days}, sparse_runs)
fd, fs = flagged(dense).get("weekly"), flagged(sparse).get("weekly")
if not fd or not fs:
    fails.append("MISS: the weekly sender was not flagged in both stores, so case 9 "
                 "cannot compare anything")
elif (fd["worst_gap"], fd["silent_days"]) != (fs["worst_gap"], fs["silent_days"]):
    fails.append("UNIT LEAK: the same sender reads differently depending on how often the "
                 "TOOL ran (%s vs %s) - that is a run count wearing a day's clothes"
                 % ((fd["worst_gap"], fd["silent_days"]),
                    (fs["worst_gap"], fs["silent_days"])))

# 10. THE DATES MUST BE THE SENDER'S OWN.
#     This is the one the old version got WRONG, and no test caught it. Per-sender scoping
#     re-derived each appearance as a position in that sender's SHORTER lattice, then read
#     the date back out of the FULL run list - so first_seen and last_seen were dates lifted
#     from the wrong sequence. On a live store a bank was captioned with a first-seen date
#     a week before its real one and a last-seen date nearly three months early - every date
#     on the panel was wrong for every sender whose mailbox was not in every run, and each
#     one looked entirely plausible, which is why it survived a rewrite of the function.
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE messages (sender TEXT, run_date TEXT, category TEXT, account TEXT)")
conn.execute("CREATE TABLE runs (run_date TEXT)")
mine = days_from("2026-01-01", 10, 6)              # a@x biller: 01-01 .. 02-20
a_days = days_from("2026-01-01", 1, 90)            # a@x kept getting mail to 03-31
other = days_from("2026-03-01", 1, 60)             # b@x: a long later backfill
for d in sorted(set(a_days) | set(other)):
    conn.execute("INSERT INTO runs VALUES (?)", (d,))
for d in mine:
    conn.execute("INSERT INTO messages VALUES ('Biller <b@e.example>',?,'bill','a@x')", (d,))
for d in a_days:
    conn.execute("INSERT INTO messages VALUES ('Heartbeat <hb@e.example>',?,'promo','a@x')",
                 (d,))
for d in other:
    conn.execute("INSERT INTO messages VALUES ('Other <o@e.example>',?,'promo','b@x')", (d,))
it = flagged(conn).get("biller")
if not it:
    fails.append("MISS: case 10 needs the biller flagged in order to check its dates")
elif it["first_seen"] != mine[0] or it["last_seen"] != mine[-1]:
    fails.append("WRONG DATES: reported %s..%s, the sender's real history is %s..%s - a "
                 "lattice position read out of the full run list"
                 % (it["first_seen"], it["last_seen"], mine[0], mine[-1]))

# 11. SOCIAL NOTIFICATIONS ARE HIDDEN, AND THE HIDING IS REPORTED.
#     A friend posting less often is not a finding a mail tool should raise, and left in
#     they dominate the list by count alone. Suppression that cannot be seen is
#     indistinguishable from having found nothing.
c = build_dates({"Someone On Social": days_from("2026-01-01", 3, 12)},
                days_from("2026-01-01", 1, 150))
c.execute("UPDATE messages SET category = 'social-notification'")
default = server.api_quiet(c, {})
every = server.api_quiet(c, {"include": ["all"]})
if any(i["sender"] == "someone on social" for i in default["items"]):
    fails.append("a social-notification sender was not hidden by default")
if not any(i["sender"] == "someone on social" for i in every["items"]):
    fails.append("?include=all did not return the hidden sender, so the hiding is a drop")
if default.get("hidden_social") != 1:
    fails.append("the hidden count was not reported (%r) - silent suppression reads exactly "
                 "like an all-clear" % default.get("hidden_social"))
if every.get("hidden_social") != 0:
    fails.append("?include=all still claims to be hiding something")

# 12. THE MONTHLY CAVEAT IS DERIVED, NOT ASSERTED.
#     The caption stated flatly that monthly billers could not qualify. True when written,
#     false once a year of arrival-dated history existed - and it went on saying it while a
#     monthly bank statement sat at the top of the list it was captioning.
short = build_dates({"X": days_from("2026-01-01", 5, 6)}, days_from("2026-01-01", 1, 40))
long_ = build_dates({"X": days_from("2026-01-01", 5, 6)}, days_from("2026-01-01", 1, 400))
if server.api_quiet(short, {})["reach"]["monthly_observable"]:
    fails.append("a 40-day window claims a monthly rhythm is observable")
if not server.api_quiet(long_, {})["reach"]["monthly_observable"]:
    fails.append("a 400-day window still claims monthly billers cannot qualify")


# 13. THE RATIO FLOOR IS LOAD-BEARING AND MUST BE TESTED SEPARATELY FROM THE DAY FLOOR.
#     A silence that is longer than the worst gap by a hair is not a finding. This sender's
#     worst gap is 20 days and it has been quiet 25 - past its record, comfortably past the
#     14-day floor, and nowhere near a change of behaviour. Removing MIN_RATIO used to break
#     no test at all, which is how a threshold gets deleted by someone tidying up.
runs13 = days_from("2026-01-01", 1, 130)
appear13 = ["2026-01-01", "2026-01-21", "2026-02-10", "2026-02-20", "2026-03-02",
            "2026-03-12"]                                  # worst gap 20 days
c = build_dates({"NearlyNormal": appear13}, runs13[:runs13.index("2026-04-06") + 1])
if "nearlynormal" in flagged(c):
    fails.append("FALSE ALARM: 25 days quiet against a 20-day worst gap was flagged - "
                 "just past a record is not a change of behaviour")

# 13b. THE CONTROL: the same sender, far enough past its record to mean something.
c = build_dates({"NearlyNormal": appear13}, runs13)
if "nearlynormal" not in flagged(c):
    fails.append("MISS: the ratio floor swallowed a sender well past its own worst gap")

# 14. A MAILBOX IS OBSERVED ON THE DAYS A RUN CONNECTED TO IT, not only on the days it
#     happened to produce mail. Deriving the window from messages alone credits a sender
#     for every day nobody can prove anyone looked - in the direction that UNDER-reports
#     silence, which is the direction that loses a stopped biller. account_status is the
#     table that records which accounts a run actually reached.
conn14 = sqlite3.connect(":memory:")
conn14.row_factory = sqlite3.Row
conn14.execute("CREATE TABLE messages (sender TEXT, run_date TEXT, category TEXT, "
               "account TEXT)")
conn14.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, run_date TEXT)")
conn14.execute("CREATE TABLE account_status (run_id INTEGER, account TEXT)")
all_days = days_from("2026-01-01", 1, 120)
biller_days = days_from("2026-01-01", 10, 6)               # 01-01 .. 02-20
for i, d in enumerate(all_days):
    conn14.execute("INSERT INTO runs VALUES (?,?)", (i + 1, d))
    # Every day, a run connected to a@x - and after 02-20 it found nothing from the biller.
    conn14.execute("INSERT INTO account_status VALUES (?,?)", (i + 1, "a@x"))
for d in biller_days:
    conn14.execute("INSERT INTO messages VALUES ('Biller <b@e.example>',?,'bill','a@x')",
                   (d,))
it14 = flagged(conn14).get("biller")
if not it14:
    fails.append("MISS: a biller that stopped, in a mailbox swept every day afterwards, "
                 "was not flagged - the window was taken from its own mail")
elif it14["last_looked"] != all_days[-1]:
    fails.append("the window closed at %s, but a run connected to that mailbox on %s - "
                 "days a sweep reached the account are observations of it"
                 % (it14["last_looked"], all_days[-1]))

print("=== two-sided control on server.api_quiet ===")
for line in ("1: fires on a real stop", "2: silent on an active sender",
             "3: silent on a burst", "4: silent inside worst gap",
             "5: fold does not invent silence, and still catches a true stop",
             "6: a backfill of one mailbox is not silence from the others",
             "7: a multiple of a tiny gap is not an alarm",
             "8: ...but a real stop still fires",
             "9: the answer does not depend on how often the TOOL ran",
             "10: the dates belong to the sender, not to the global run list",
             "11: social senders are hidden AND the hiding is reported",
             "12: the monthly caveat is derived from the window",
             "13: just past a record is not a change of behaviour",
             "14: a run that connected and found nothing is still an observation"):
    print("  case", line)
if fails:
    print("\nFAILURES:")
    for x in fails:
        print("  -", x)
    sys.exit(1)
print("\nALL PASS - the detector fires when it should and stays quiet when it should.")
