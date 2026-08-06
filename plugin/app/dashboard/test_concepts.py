"""Two-sided control on the canonical concept map.

The point of the concept rollup is to stop a query returning a confident wrong number. A test
that only checked "known labels resolve" would pass just as happily on a map that silently
answered "other" for everything - which is the exact failure being fixed. So this checks BOTH
sides: that real labels land where they belong, AND that an unknown label is visibly UNMAPPED
rather than quietly bucketed.

    python dashboard/test_concepts.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import concepts as C

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


print("=== two-sided control on concepts.concept_of ===")

# --- positive: every declared label resolves to its own concept, exactly once ---
seen = {}
dupes = []
for concept, labels in C.CONCEPTS.items():
    for lab in labels:
        if lab in seen and seen[lab] != concept:
            dupes.append((lab, seen[lab], concept))
        seen[lab] = concept
check("every declared label resolves to its declared concept",
      all(C.concept_of(l) == c for l, c in seen.items()
          if l not in C._AMBIGUOUS))
check("no label silently claimed by two concepts (except the recorded ambiguous ones)",
      not [d for d in dupes if d[0] not in C._AMBIGUOUS], str(dupes))

# --- negative: unknown labels must be VISIBLE, never folded into 'other' ---
for bogus in ("totally-new-label-2027", "", None, "  ", "bill-ish"):
    got = C.concept_of(bogus)
    check("unknown %r -> unmapped (not 'other')" % (bogus,),
          got == C.UNMAPPED, "got %r" % got)
check("'other' is a real concept and still resolves",
      C.concept_of("other") == "other")

# --- the mismapping regression: a label is filed by its ROWS, not by a word in its name ---
#
# Three labels beginning "business-" were once filed under MONEY on the strength of that
# word, when the rows behind them were a newsletter and two account notices. Those labels
# are personal vocabulary and now live in concepts.local.json, so this pins the LESSON
# using the local file if it is present, and says so plainly when it is not - rather than
# hardcoding one mailbox's labels into a test that ships publicly.
_local_map = {}
try:
    import json as _json
    with open(os.path.join(HERE, "concepts.local.json"), encoding="utf-8-sig") as _f:
        for _concept, _labels in (_json.load(_f).get("concepts") or {}).items():
            for _l in _labels or []:
                _local_map[str(_l).lower()] = _concept
except FileNotFoundError:
    pass
except Exception as _e:
    check("concepts.local.json is readable", False, "%s: %s" % (type(_e).__name__, _e))

if _local_map:
    for _label, _want in _local_map.items():
        check("local label %r resolves to %r" % (_label, _want),
              C.concept_of(_label) == _want, C.concept_of(_label))
    print("  (%d local label(s) checked against concepts.local.json)" % len(_local_map))
else:
    print("  (no concepts.local.json - local-label resolution not exercised)")

# The shipped defaults must stand on their own, with or without a local file: a word in a
# label's name never decides its concept.
check("a shipped money label is money", C.concept_of("bank-statement")
      == "money (bills, receipts, banking)", C.concept_of("bank-statement"))
check("a shipped newsletter label is a newsletter",
      C.concept_of("org-newsletter") == "newsletters", C.concept_of("org-newsletter"))
check("a shipped account label is account & security",
      C.concept_of("account-notice") == "account & security", C.concept_of("account-notice"))

# --- keys round-trip, so a URL param can never resolve to the wrong concept ---
check("every concept has a short key", all(c in C.CONCEPT_KEYS for c in C.all_concepts()))
check("keys are unique", len(set(C.CONCEPT_KEYS.values())) == len(C.CONCEPT_KEYS))
check("key -> concept round-trips",
      all(C.concept_for_key(C.key_of(c)) == c for c in C.all_concepts()))
check("unknown key resolves to None, not a default concept",
      C.concept_for_key("not-a-key") is None)

# --- the map must actually cover what is in the live DB ---
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_dashboard.db")
if os.path.exists(DB):
    import sqlite3
    conn = sqlite3.connect(DB)
    labels = [r[0] for r in conn.execute("SELECT DISTINCT category FROM messages")]
    missing = [l for l in labels if C.concept_of(l) == C.UNMAPPED]
    # Your own vocabulary lives in concepts.local.json, so an unmapped label here almost
    # always means a label your runs write is not listed there yet. Say that, rather than
    # printing a bare list and leaving the reader to work out what to do with it.
    check("every label present in the live DB is mapped (%d labels)" % len(labels),
          not missing,
          "%d label(s) resolve to UNMAPPED - add them to concepts.local.json under the "
          "concept each one means (copy concepts.example.json if you have no local file "
          "yet): %s" % (len(missing), missing))
    nulls = conn.execute("SELECT COUNT(*) FROM messages WHERE concept IS NULL").fetchone()[0]
    check("no row left without a concept", nulls == 0, "%d null" % nulls)
    # Totals must be conserved: rolling up must not lose or duplicate a row.
    tot = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    summed = conn.execute("SELECT SUM(n) FROM (SELECT COUNT(*) n FROM messages GROUP BY concept)").fetchone()[0]
    check("rollup conserves the total (%d)" % tot, tot == summed, "summed %s" % summed)
    conn.close()
else:
    print("  skip live-DB coverage (no email_dashboard.db here)")

print()
if fails:
    print("FAILED: %d" % len(fails))
    raise SystemExit(1)
print("ALL PASS - known labels land where they belong, and an unknown label stays visible.")
