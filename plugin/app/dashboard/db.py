"""
SQLite layer for the email dashboard.

One small database (email_dashboard.db) holds the history of every daily routine
run: per-account status, and every triaged message with its disposition
(trashed / surfaced / kept), category, and reason. The dashboard reads from here;
the daily routine writes to here via ingest.py.

Stdlib only (sqlite3) — no third-party dependencies.
"""
import os
import sqlite3
from datetime import datetime, timezone

from concepts import concept_of

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_dashboard.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL UNIQUE,          -- YYYY-MM-DD (one row per day)
    created_at  TEXT NOT NULL,                 -- ISO timestamp of ingest
    fetched     INTEGER NOT NULL DEFAULT 0,
    trashed     INTEGER NOT NULL DEFAULT 0,
    kept        INTEGER NOT NULL DEFAULT 0,
    otp         INTEGER NOT NULL DEFAULT 0,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS account_status (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    account      TEXT NOT NULL,
    role         TEXT,
    status       TEXT,                          -- CONNECTED / FAILED
    auth         TEXT,                          -- app_password / oauth2
    inbox_count  INTEGER,
    fetched      INTEGER NOT NULL DEFAULT 0,
    trashed      INTEGER NOT NULL DEFAULT 0,
    kept         INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date     TEXT NOT NULL,
    account      TEXT NOT NULL,
    sender       TEXT,
    subject      TEXT,
    msg_date     TEXT,
    disposition  TEXT NOT NULL,                 -- trashed / surfaced / kept
    category     TEXT,                          -- the raw label as the run wrote it
    concept      TEXT,                          -- canonical concept (concepts.py); 'unmapped' if unknown
    reason       TEXT,
    importance   TEXT                           -- action-needed / family / financial / security / info
);

CREATE INDEX IF NOT EXISTS idx_msg_run     ON messages(run_id);
CREATE INDEX IF NOT EXISTS idx_msg_disp    ON messages(disposition);
CREATE INDEX IF NOT EXISTS idx_msg_cat     ON messages(category);
CREATE INDEX IF NOT EXISTS idx_acct_run    ON account_status(run_id);

-- Steam wishlist sales we've learned about (one row per game/app).
-- The email tells us title + discount + app_id; steam_refresh.py enriches the
-- real prices from Steam's store API and flips `active` off once the sale ends,
-- so the dashboard panel always shows current knowledge of ACTIVE sales only.
-- "I HAVE SEEN THIS" - the owner acknowledging an item.
--
-- Closes the loop from the other side. The lane's standing pain is that the rare real
-- thing drowns; half of that is noise arriving, and the other half is things you have ALREADY
-- dealt with continuing to compete for attention run after run. Without this the routine
-- has no way to know, so it keeps re-surfacing a handled item forever - which is exactly
-- how the genuinely-new item gets lost in a list of stale ones.
--
-- TWO SCOPES, because they mean different things:
--   'message' - this one email, keyed by Message-ID.
--   'thread'  - this whole recurring item (sender + subject shape), so acknowledging a
--               notice that arrives monthly silences the SERIES rather than one instance.
--
-- Acknowledged is not deleted and not hidden: the row stays, the paper trail stays, and an
-- ack can be lifted. It only stops the item from shouting.
CREATE TABLE IF NOT EXISTS acks (
    kind        TEXT NOT NULL,               -- 'message' | 'thread'
    key         TEXT NOT NULL,               -- message_id, or sender_key|subject_shape
    account     TEXT,
    sender      TEXT,
    subject     TEXT,
    note        TEXT,
    acked_at    TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- WHICH HOSTS EACH SENDER NORMALLY LINKS TO.
--
-- The question "does this link stay on the sender's own domain?" is static and has no
-- memory, so it cannot tell that url1719.example-bank.org is that bank's normal
-- redirector while some new host is not - and it cries wolf on every ESP (a facebookmail
-- .com sender linking to facebook.com trips it nine times in one message).
--
-- This table answers the better question: is this host NORMAL FOR THIS SENDER. It catches
-- the attack that actually happens - impersonating a sender already trusted - because a
-- mail claiming to be the bank that links somewhere the bank has never linked before is
-- exactly the shape worth shouting about, and a domain test cannot see it.
--
-- Evidence, not permission: `messages` counts how much support a host has, so a thin
-- profile can fail toward "unknown" instead of blessing whatever arrived first.
CREATE TABLE IF NOT EXISTS sender_hosts (
    sender_key  TEXT NOT NULL,               -- normalised sender (server._sender_key)
    host        TEXT NOT NULL,               -- a link host seen in that sender's mail
    messages    INTEGER NOT NULL DEFAULT 0,  -- how many distinct messages used it
    first_seen  TEXT,                        -- message date we first saw this pairing
    last_seen   TEXT,
    PRIMARY KEY (sender_key, host)
);

CREATE TABLE IF NOT EXISTS sender_profile (
    sender_key  TEXT PRIMARY KEY,
    messages    INTEGER NOT NULL DEFAULT 0,  -- messages profiled; the confidence denominator
    first_seen  TEXT,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS steam_sales (
    app_id            INTEGER PRIMARY KEY,        -- Steam store app id (from the email link)
    title             TEXT,
    url               TEXT,
    discount_pct      INTEGER,
    price_initial     INTEGER,                    -- cents, regular price (from API)
    price_final       INTEGER,                    -- cents, sale price (from API)
    currency          TEXT,
    price_initial_fmt TEXT,                       -- e.g. "$39.99"
    price_final_fmt   TEXT,                       -- e.g. "$27.99"
    first_seen        TEXT,                       -- run_date we first learned of the sale (approx start)
    last_seen         TEXT,                       -- last run_date the email reappeared
    last_checked      TEXT,                       -- ISO ts of last Steam API refresh
    active            INTEGER NOT NULL DEFAULT 1, -- 1 while on sale, 0 once ended
    ended_at          TEXT,                       -- date we detected the sale had ended
    sale_ends         TEXT                        -- scheduled end date (ISO), scraped from the store page's "Offer ends ..." countdown
);

CREATE INDEX IF NOT EXISTS idx_steam_active ON steam_sales(active);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Lightweight migrations for columns added after a DB already existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(steam_sales)")}
        if "sale_ends" not in cols:
            conn.execute("ALTER TABLE steam_sales ADD COLUMN sale_ends TEXT")
        # message_id: the DURABLE handle for re-locating a message later. Deliberately not
        # a UID - a UID is per-folder and is reassigned the moment a message is moved, so
        # every uid recorded before a trash operation is stale by the end of the run.
        # Verified live: one message carried a different UID in INBOX and in Trash minutes
        # later, while its Message-ID was unchanged throughout.
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if "message_id" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
        # concept: the canonical 12-concept rollup over however many raw labels a store has
        # drifted into. Backfill the history with `python dashboard/migrate_concepts.py`
        # (idempotent); new rows get it on write. See concepts.py for why a single-label
        # query can answer with a fraction of the truth, stated as the whole.
        if "concept" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN concept TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_concept ON messages(concept)")
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def upsert_steam_sale(app_id, title=None, url=None, discount_pct=None, seen_date=None):
    """
    Record (or refresh) a Steam wishlist sale learned from an email.

    New app_id  -> insert, first_seen = last_seen = seen_date, active = 1.
    Seen again  -> bump last_seen, refresh title/url/discount, and REACTIVATE
                   (active = 1, ended_at = NULL) in case a prior sale had ended
                   and a fresh one started. Prices are filled in later by
                   steam_refresh.py from Steam's store API.
    """
    if app_id is None:
        return
    app_id = int(app_id)
    init_db()
    conn = connect()
    try:
        row = conn.execute("SELECT app_id FROM steam_sales WHERE app_id = ?", (app_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE steam_sales SET "
                "title = COALESCE(?, title), url = COALESCE(?, url), "
                "discount_pct = COALESCE(?, discount_pct), "
                "last_seen = COALESCE(?, last_seen), active = 1, ended_at = NULL "
                "WHERE app_id = ?",
                (title, url, discount_pct, seen_date, app_id),
            )
        else:
            conn.execute(
                "INSERT INTO steam_sales "
                "(app_id, title, url, discount_pct, first_seen, last_seen, active) "
                "VALUES (?,?,?,?,?,?,1)",
                (app_id, title, url, discount_pct, seen_date, seen_date),
            )
        conn.commit()
    finally:
        conn.close()


def list_steam_sales(active_only=True):
    init_db()
    conn = connect()
    try:
        where = "WHERE active = 1" if active_only else ""
        # active sales first, deepest discount first, then most recently seen
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM steam_sales {where} "
            "ORDER BY active DESC, discount_pct DESC, last_seen DESC")]
    finally:
        conn.close()


def update_steam_price(app_id, discount_pct, price_initial, price_final, currency,
                       price_initial_fmt, price_final_fmt, checked_iso):
    conn = connect()
    try:
        conn.execute(
            "UPDATE steam_sales SET discount_pct = ?, price_initial = ?, price_final = ?, "
            "currency = ?, price_initial_fmt = ?, price_final_fmt = ?, last_checked = ?, "
            "active = 1, ended_at = NULL WHERE app_id = ?",
            (discount_pct, price_initial, price_final, currency,
             price_initial_fmt, price_final_fmt, checked_iso, int(app_id)),
        )
        conn.commit()
    finally:
        conn.close()


def update_steam_end(app_id, sale_ends):
    """Store the scheduled sale-end date (ISO, or None if unknown/unparseable)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE steam_sales SET sale_ends = ? WHERE app_id = ?",
            (sale_ends, int(app_id)),
        )
        conn.commit()
    finally:
        conn.close()


def mark_steam_ended(app_id, ended_date, checked_iso):
    conn = connect()
    try:
        conn.execute(
            "UPDATE steam_sales SET active = 0, ended_at = ?, discount_pct = 0, "
            "last_checked = ? WHERE app_id = ?",
            (ended_date, checked_iso, int(app_id)),
        )
        conn.commit()
    finally:
        conn.close()


def ingest_run(run_date, accounts=None, messages=None, notes=None, steam_sales=None):
    """
    Idempotent per run_date: replaces any existing data for that day.

    accounts: list of dicts with keys
        account, role, status, auth, inbox_count, fetched, trashed, kept, error
    messages: list of dicts with keys
        account, sender, subject, msg_date, disposition, category, reason, importance
    steam_sales: optional list of dicts with keys
        app_id, title, url, discount_pct  (prices are filled in by steam_refresh.py)

    Totals on the run row are derived from the messages list.
    """
    accounts = accounts or []
    messages = messages or []
    steam_sales = steam_sales or []
    init_db()
    conn = connect()
    try:
        # wipe any prior data for this date, then re-create the run row
        row = conn.execute("SELECT id FROM runs WHERE run_date = ?", (run_date,)).fetchone()
        if row:
            conn.execute("DELETE FROM messages WHERE run_id = ?", (row["id"],))
            conn.execute("DELETE FROM account_status WHERE run_id = ?", (row["id"],))
            conn.execute("DELETE FROM runs WHERE id = ?", (row["id"],))

        trashed = sum(1 for m in messages if m.get("disposition") == "trashed")
        kept = sum(1 for m in messages if m.get("disposition") in ("kept", "surfaced"))
        otp = sum(1 for m in messages if (m.get("category") or "").lower() == "otp")
        fetched = sum(int(a.get("fetched") or 0) for a in accounts) or len(messages)

        cur = conn.execute(
            "INSERT INTO runs (run_date, created_at, fetched, trashed, kept, otp, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_date, now_iso(), fetched, trashed, kept, otp, notes),
        )
        run_id = cur.lastrowid

        for a in accounts:
            conn.execute(
                "INSERT INTO account_status "
                "(run_id, account, role, status, auth, inbox_count, fetched, trashed, kept, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, a.get("account"), a.get("role"), a.get("status"), a.get("auth"),
                 a.get("inbox_count"), int(a.get("fetched") or 0), int(a.get("trashed") or 0),
                 int(a.get("kept") or 0), a.get("error")),
            )

        for m in messages:
            # Resolve the canonical concept on WRITE, so the store carries one vocabulary and the
            # category drift cannot silently reopen. An unrecognised label lands as 'unmapped' and
            # stays visible - it is never folded into 'other'. See concepts.py for the 62-labels-
            # for-12-concepts defect this closes.
            conn.execute(
                "INSERT INTO messages "
                "(run_id, run_date, account, sender, subject, msg_date, disposition, "
                "category, concept, reason, importance, message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_date, m.get("account"), m.get("sender"), m.get("subject"),
                 m.get("msg_date"), m.get("disposition"), m.get("category"),
                 concept_of(m.get("category")),
                 m.get("reason"), m.get("importance"),
                 (m.get("message_id") or "").strip() or None),
            )
        conn.commit()
    finally:
        conn.close()

    # Steam sales are keyed by app_id and persist across runs (not wiped per day),
    # so upsert them separately after the per-day data is committed.
    for s in steam_sales:
        upsert_steam_sale(
            s.get("app_id"), title=s.get("title"), url=s.get("url"),
            discount_pct=s.get("discount_pct"), seen_date=run_date)

    return run_id


if __name__ == "__main__":
    init_db()
    print("initialized", DB_PATH)
