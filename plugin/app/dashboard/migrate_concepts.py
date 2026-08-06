"""Backfill the canonical `concept` column across every historical run.

Idempotent: re-running recomputes every row from concepts.py, so fixing a mapping and re-running
is the supported way to correct the tree. The raw `category` label is never modified.

    python dashboard/migrate_concepts.py --dry-run   # show what would change, touch nothing
    python dashboard/migrate_concepts.py             # apply

The report is the point. It prints, per concept, what a single-label query used to return versus
what the concept returns now - i.e. exactly how much each number was understated.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import concepts as C  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_dashboard.db")


def ensure_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "concept" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN concept TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_concept ON messages(concept)")
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    # --dry-run must mean it. The first version of this script added the column BEFORE checking
    # the flag and then printed "DRY RUN - nothing written", which was false on the one run where
    # it mattered. A dry run that quietly alters the schema is exactly the kind of instrument this
    # whole migration exists to stop trusting.
    has_col = "concept" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if args.dry_run:
        if not has_col:
            print("(dry run: column messages.concept does NOT exist yet and was NOT created)")
    elif ensure_column(conn):
        print("added column messages.concept (+ index)")

    rows = conn.execute("SELECT id, category FROM messages").fetchall()
    total = len(rows)

    updates = []
    unmapped = {}
    for mid, cat in rows:
        c = C.concept_of(cat)
        if c == C.UNMAPPED:
            unmapped[cat] = unmapped.get(cat, 0) + 1
        updates.append((c, mid))

    print("\nmessages: %d   distinct raw labels: %d   canonical concepts: %d"
          % (total, len({c for _, c in rows}), len(C.all_concepts())))

    if unmapped:
        # Loud on purpose. A label nobody mapped is a real finding, not a rounding error.
        print("\n*** UNMAPPED LABELS - these will show as '%s' in the UI, not hidden in 'other' ***"
              % C.UNMAPPED)
        for label, n in sorted(unmapped.items(), key=lambda x: -x[1]):
            print("    %-28s %4d" % (label, n))
    else:
        print("unmapped labels: none - every label in the tree resolves to a concept")

    if not args.dry_run:
        conn.executemany("UPDATE messages SET concept=? WHERE id=?", updates)
        conn.commit()
        print("\nwrote concept for %d rows" % len(updates))
    else:
        print("\nDRY RUN - nothing written")

    # The undercount report: concept total vs the biggest single label inside it.
    print("\n=== what each number was understating ===")
    print("%-34s %7s %8s %9s %8s" % ("concept", "labels", "concept", "best label", "was off"))
    grand_true = grand_best = 0
    for concept in C.all_concepts():
        labels = [l for l in C.CONCEPTS[concept]]
        present = []
        for l in labels:
            n = conn.execute("SELECT COUNT(*) FROM messages WHERE category=?", (l,)).fetchone()[0]
            if n:
                present.append((l, n))
        if not present:
            continue
        true_total = sum(n for _, n in present)
        best = max(n for _, n in present)
        grand_true += true_total
        grand_best += best
        pct = 100.0 * (true_total - best) / true_total if true_total else 0.0
        print("%-34s %7d %8d %9d %7.0f%%" % (concept, len(present), true_total, best, pct))
    miss = 100.0 * (grand_true - grand_best) / grand_true if grand_true else 0.0
    print("%-34s %7s %8d %9d %7.0f%%" % ("ALL", "", grand_true, grand_best, miss))
    print("\nRead that last row as: asking the dashboard by its single most common label used to")
    print("reach %d of %d triaged messages. By concept it now reaches all %d." % (grand_best, grand_true, grand_true))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
