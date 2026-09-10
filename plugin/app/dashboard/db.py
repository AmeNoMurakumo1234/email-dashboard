"""
SQLite layer for the email dashboard.

One small database (email_dashboard.db) holds the history of every daily routine
run: per-account status, and every triaged message with its disposition
(trashed / surfaced / kept), category, and reason. The dashboard reads from here;
the daily routine writes to here via ingest.py.

Stdlib only (sqlite3) — no third-party dependencies.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from concepts import concept_of, fingerprint as concept_fingerprint

# EMAIL_DASHBOARD_DB names the store, so a caller can redirect one WITHOUT editing this
# file. Added because a test set that variable, nothing read it, and three subprocess runs
# wrote their fixtures straight into the owner's live database - the second time this suite
# has damaged real data by assuming a redirect that did not exist. A knob that is documented
# and ignored is worse than no knob.
DB_PATH = (os.environ.get("EMAIL_DASHBOARD_DB")
           or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "email_dashboard.db"))

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
    msg_date     TEXT,                          -- exactly as the run wrote it (ISO, RFC 2822, or absent)
    msg_day      TEXT,                          -- derived YYYY-MM-DD; what the calendar is allowed to trust
    disposition  TEXT NOT NULL,                 -- trashed / would_trash / surfaced / kept / saved
                                                --   see DISPOSABLE / DELIBERATELY_KEPT below
    category     TEXT,                          -- the raw label as the run wrote it
    concept      TEXT,                          -- canonical concept (concepts.py); 'unmapped' if unknown
    reason       TEXT,
    importance   TEXT,                          -- action-needed / family / financial / security / info
    injection_signals TEXT,                     -- JSON list; mail addressed to the TRIAGER, not to a person
    recipients   TEXT,                          -- To + Cc, as received
    recipient_count INTEGER,                    -- how many people got it; NULL = unknown
    addressed_directly INTEGER,                 -- 1 = this mailbox is in To (not merely Cc); NULL = unknown
    -- THE TWO COLUMNS THAT MAKE A CONNECTOR INSTALL USABLE.
    --
    -- Declaring that something else fetches your mail did not make a single message
    -- openable, so on that class of install the entire viewer - the sandboxed reader, the
    -- image blocking, the tracking-host report, which are the headline privacy features -
    -- was unreachable for every row. The instinct that added recipients applies here too:
    -- CARRY MORE OF WHAT THE FETCHER SAW.
    --
    -- body_text is the one that matters. With it the sanitising reader works with no fetch
    -- at all. web_link is the cheaper half: every Graph and connector result already
    -- carries a direct URL to that message, and "opens in your mail client, images and
    -- all" is a labelled, explicit affordance rather than a silent fallback - but it beats
    -- cannot open.
    body_text    TEXT,                          -- raw MIME, or just the text/html body
    web_link     TEXT                           -- provider URL to this message, if any
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

-- WHAT THE OWNER HAS TOLD US, AND WHAT WE ASKED TO GET IT.
--
-- The tool ships with its rules file full of "fill this in" and never asks. This table is
-- the other half of fixing that: questions generated from the mailbox (dashboard/
-- questions.py) are answered here, and the answer is written into the rules file or the
-- protected list from these rows.
--
-- The QUESTION is stored beside the answer on purpose. A rule recovered a year later reads
-- as an arbitrary preference unless you can still see what was asked and what evidence
-- prompted it - the difference between a rule its owner chose and a rule someone guessed.
-- `evidence` is the JSON the question carried at the time, frozen: re-deriving it later
-- would show today's mailbox, not the one the answer was about.
--
-- Answering is also what stops a question being asked again, so an unanswered question and
-- a question answered "leave it alone" must be distinguishable. They are: the second has a
-- row.
CREATE TABLE IF NOT EXISTS answers (
    question_id TEXT PRIMARY KEY,            -- questions.generate() id, stable across runs
    kind        TEXT,                        -- question kind, for reporting
    question    TEXT,                        -- verbatim, as asked
    evidence    TEXT,                        -- JSON, frozen at ask time
    answer      TEXT,                        -- what the owner said
    answered_at TEXT NOT NULL,
    written_to  TEXT                         -- file the answer was applied to, once applied
);

-- THINGS THAT ARE STILL OPEN, AND THINGS THAT WERE FINISHED SOMEWHERE ELSE.
--
-- A brief is a delta. A task assigned three weeks ago appeared in exactly one brief and
-- then vanished, because every run reports what ARRIVED rather than what is outstanding.
-- The reporting deployment had to invent two markdown files to survive that - one listing
-- what was still open, one listing what had been dealt with elsewhere - and, as they put
-- it, both were markdown files pretending to be tables. So they are tables.
--
-- NOT THE SAME AS AN ACK, and the difference is the whole point. An ack says "I have seen
-- this"; seeing something is not doing it. An item you acknowledged on Monday and have not
-- done is still open on Friday, and the tool that keeps telling you about new mail should
-- be the one that remembers.
--
-- RESOLVED OFF-CHANNEL IS A FIRST-CLASS OUTCOME. Most things that arrive by mail are
-- finished somewhere the mail tool cannot see - a phone call, a chat message, a
-- conversation in a corridor. Without somewhere to say so, the only ways to clear an item
-- are to lie about it or to leave it open forever, and both end with the list being
-- ignored. `resolved_where` records that it was closed, and that the closing did not
-- happen here.
--
-- Keyed the same way acks are (message-id, or sender+subject shape for a recurring
-- series), so the two can always be talked about together.
CREATE TABLE IF NOT EXISTS open_items (
    key           TEXT PRIMARY KEY,      -- message_id, or sender_key|subject_shape
    kind          TEXT NOT NULL,         -- 'message' | 'thread'
    account       TEXT,
    sender        TEXT,
    subject       TEXT,
    concept       TEXT,
    importance    TEXT,
    first_seen    TEXT,                  -- the day it first needed attention
    last_seen     TEXT,                  -- the most recent run that still saw it
    runs_seen     INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'resolved'
    resolved_at   TEXT,
    resolved_where TEXT,                 -- 'email' | 'off-channel' | 'moot'
    resolved_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_open_state ON open_items(state);

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

-- THE NEW-HOST CHECK'S FINDINGS, KEPT INSTEAD OF PRINTED.
--
-- check_new_hosts.py is the one instrument in this lane that is noise-free by construction:
-- it fires only when a sender with an established profile links somewhere it never has.
-- Its output used to exist nowhere but a terminal and whatever prose the run report carried,
-- which means the sharpest security signal here was also the most likely to scroll past.
--
-- One row per (sender, new host), not per message: the QUESTION is "is this host normal for
-- this sender", and that question is asked once. A verdict therefore silences the pairing
-- for good rather than for a day, which is what keeps the panel from becoming the noise it
-- exists to cut through. verdict NULL = nobody has looked yet; that is the only state the
-- dashboard shouts about.
CREATE TABLE IF NOT EXISTS host_flags (
    sender_key        TEXT NOT NULL,           -- normalised sender (server._sender_key)
    host              TEXT NOT NULL,           -- the host that was new for this sender
    sender            TEXT,                    -- display From, as it arrived
    account           TEXT,
    subject           TEXT,                    -- the message that first showed this pairing
    profile_messages  INTEGER,                 -- how much history stands behind the sender
    weighty           INTEGER NOT NULL DEFAULT 0,  -- subject/sender matched a WEIGHTY term
    first_flagged     TEXT,                    -- run date of the first sighting
    last_flagged      TEXT,
    times_seen        INTEGER NOT NULL DEFAULT 1,
    verdict           TEXT,                    -- NULL = unreviewed | 'cleared' | 'suspicious'
    verdict_note      TEXT,
    verdict_by        TEXT,
    verdict_at        TEXT,
    PRIMARY KEY (sender_key, host)
);

CREATE INDEX IF NOT EXISTS idx_host_flags_open ON host_flags(verdict);

-- WHY THE DISPOSER SAID NO, kept so that a refusal stops reading as a failure.
--
-- `would_trash` + still-in-INBOX has TWO causes and the store recorded neither: the guard
-- REFUSED it (correct - it is meant to stay) or the disposal never happened (a defect).
-- stranded_scan could only ever report the union, so it printed a list in which every entry
-- looked like a defect - and in practice a mature install's list is dominated by correct
-- refusals, because that is the intended resting state. A permanent false positive is not
-- merely noise: it teaches the reader to skim the panel, which is how a real disposal
-- failure ends up somewhere nobody looks any more. Same disease as the health-report ruling
-- amnesia and the watchdog's all-clear blocker: one outcome where two truths exist.
--
-- One row per message_id, overwritten on each re-presentation, because the question is
-- "why is this still here NOW" and an old reason for a message the guard has since cleared
-- is worse than no reason. refused_at dates the verdict so a reader can see when the
-- evidence was taken; evidence_from dates the OLDEST keep the refusal rests on, which is
-- what exposes a refusal resting entirely on history a later owner ruling has overturned.
--
-- A stranded message with NO row here is "unexplained", never "proven defect": the table
-- postdates the scanner, so early refusals have no row through no fault of their own. That
-- failure direction is deliberate - calling one a defect would recreate the false positive
-- in the mirror.
CREATE TABLE IF NOT EXISTS disposal_refusals (
    message_id    TEXT PRIMARY KEY,
    run_date      TEXT,                        -- the run whose proposal was refused
    account       TEXT,
    sender        TEXT,
    subject       TEXT,
    category      TEXT,
    reasons       TEXT,                        -- the guard's own wording, newline-separated
    refused_at    TEXT,                        -- ISO ts of this verdict
    evidence_from TEXT                         -- oldest run_date behind the keeps cited, if known
);

CREATE INDEX IF NOT EXISTS idx_refusals_run ON disposal_refusals(run_date);

-- THE RULE-15 SHELF ANSWER, WHICH USED TO DIE WITH THE SESSION THAT PRODUCED IT.
-- `tools/retention_scan.py` walks all eight mailboxes every morning to answer the one
-- question the daily --days 2 window structurally cannot: what summary-tier mail is past
-- its shelf life. It printed to a console and stopped there, so the page - the only surface
-- anybody actually looks at - had never seen it, while an open owner question (Q41) was
-- being argued from the number.
--
-- ONE ROW PER SCAN, and the answer is deliberately stored WITH ITS REACH. A shelf list is a
-- claim about a whole mailbox, and this lane's oldest lesson is that a claim without its
-- scope is a lie in the reassuring direction: a scan that lost a throttled box to a timeout
-- reports a SMALLER number and reads as better news.
CREATE TABLE IF NOT EXISTS retention_scans (
    scanned_at     TEXT PRIMARY KEY,           -- ISO ts; the newest row is the truth
    walked         INTEGER,                    -- messages actually looked at
    matched        INTEGER,                    -- messages the search said were there
    boxes_scanned  INTEGER,                    -- boxes that answered
    boxes_total    INTEGER,                    -- boxes there are
    truncated      TEXT,                       -- JSON: boxes cut short by the time budget
    failed         TEXT,                       -- JSON: boxes that never answered, with why
    shift_days     INTEGER DEFAULT 0           -- >0 marks a POSITIVE-CONTROL run, never served
);

CREATE TABLE IF NOT EXISTS retention_shelf (
    scanned_at  TEXT,                          -- FK to retention_scans.scanned_at
    account     TEXT,
    sender      TEXT,
    subject     TEXT,
    msg_date    TEXT,
    uid         TEXT,
    label       TEXT,                          -- the rule-15 tier that matched
    shelf_days  INTEGER,                       -- that tier's shelf
    age_days    REAL                           -- age at scan time, real instant not midnight
);

CREATE INDEX IF NOT EXISTS idx_shelf_scan ON retention_shelf(scanned_at);
"""


def connect(path=None):
    """Open the store. `path` exists so a caller can say WHICH store.

    It used to be unconditionally DB_PATH, which meant a test could not build a fixture
    store without opening the real one - `init_db()` on a temp file was simply not
    expressible, and the obvious workaround (reassigning the module global) leaks into
    whatever runs next in the same process. An argument is the honest version of that.
    """
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn=None):
    """Create and migrate the schema. Pass a connection to initialise a store you opened.

    Idempotent either way: every statement here is CREATE IF NOT EXISTS or an ALTER guarded
    by a PRAGMA check, because it runs on every start against stores of every age.
    """
    conn = conn or connect()
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

        # msg_day: the calendar used to key on run_date, so an intake of months of existing
        # mail rendered as ONE tile - a single run covering the better part of a year. The
        # arrival date was stored all along and queried nowhere.
        if "msg_day" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN msg_day TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_day ON messages(msg_day)")
        for col, ddl in (("body_text", "TEXT"), ("web_link", "TEXT")):
            if col not in mcols:
                conn.execute("ALTER TABLE messages ADD COLUMN %s %s" % (col, ddl))
        _backfill_msg_day(conn)
        _reconcile_concepts(conn)

        # account_status is a SET - one row per account per run - and the write path treated
        # it as a log. Collapse first, THEN constrain: a CREATE UNIQUE INDEX in SCHEMA would
        # run inside executescript before any migration could clean up, and abort the entire
        # schema on exactly the stores that hold duplicates, which are the ones that need it.
        _collapse_duplicate_account_status(conn)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_acct_run_account "
                     "ON account_status(run_id, account)")

        # The injection label has to OUTLIVE ingest. Computed and then discarded, it was
        # invisible to the dashboard, so the one place a person would notice "this mail
        # tried to steer the triager" never showed it.
        if "injection_signals" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN injection_signals TEXT")

        # WAS THIS SENT TO YOU, OR TO TWO HUNDRED PEOPLE? Both fetchers captured To from
        # the start and ingest discarded it, so the tool could not tell a bot's blast from
        # the same bot assigning you work. A reported rule would have binned GitHub
        # mentions and task assignments, unread, on exactly that mistake.
        for col, decl in (("recipients", "TEXT"), ("recipient_count", "INTEGER"),
                          ("addressed_directly", "INTEGER")):
            if col not in mcols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_direct "
                     "ON messages(addressed_directly)")

        # THREAD ACK KEYS CHANGED SHAPE, so the ones already stored have to move with them.
        #
        # A thread key used to be sender|shape and is now account|shape - because a thread is
        # a subject, not a person, and the old form gave every participant in one
        # conversation a separate key. Left alone, every acknowledgement made before the
        # change would simply stop matching: the item would quietly return to the attention
        # list with no indication that a decision had been lost.
        #
        # Rewritable because the acks table already stores the account, sender and subject
        # each key was derived from. Idempotent: a key that already has the new shape is
        # recomputed to itself. Runs on every init, so an install that upgrades by overlay
        # rather than by installer still gets it.
        _migrate_thread_ack_keys(conn)

        conn.commit()
    finally:
        conn.close()



# A tiny key/value table for facts ABOUT the store rather than in it. Currently one fact:
# which version of the concept map the `concept` column was derived with.
_META_DDL = ("CREATE TABLE IF NOT EXISTS store_meta ("
             "key TEXT PRIMARY KEY, value TEXT)")


def _reconcile_concepts(conn):
    """Re-derive `concept` whenever the map it was derived FROM has changed. Idempotent.

    THE DEFECT THIS CLOSES: on a real install, almost every row still read `unmapped` long
    after the owner had written a local map covering every label in use - while
    `test_concepts.py` reported ALL PASS, because that test resolves live through
    `concept_of()` and the dashboard reads the stored column. Two instruments, opposite
    answers, and the one the user sees is the stale one.

    `concept` is resolved once at ingest and frozen. The map it resolves against is edited
    later BY DESIGN - the shipped map is deliberately generic and the onboarding skill tells
    people to add their own labels as they meet them. So the column does not drift by
    accident; it drifts as a direct consequence of using the tool as documented.

    What it broke was not only the concept view. `questions.py` decides what is unmapped
    from this column, so it raised a question naming labels that resolved perfectly well -
    and CROWDED OUT real ones, emitting a fraction of the questions it should have. A stale
    derived value did not merely display wrong; it degraded what the tool asked its owner,
    which is the feature the previous release exists to add.

    `msg_day` already had exactly this treatment; `concept` did not. The general rule is
    now written down: any value derived at write time from configuration the owner is
    invited to edit WILL drift, and the drift is invisible because every count still
    balances. The map fingerprint is recorded in `store_meta` for diagnosis - which version
    of the map the store was last reconciled against - but nothing is gated on it.
    """
    try:
        conn.execute(_META_DDL)
        # NO FINGERPRINT GATE. There was one, and it was wrong in a way worth recording:
        # add a label, run, remove the label, run again, and the fingerprint returns to its
        # earlier value - so the sweep is skipped and the rows written in between keep
        # asserting a mapping the owner has since withdrawn. An optimisation that
        # reintroduces the exact staleness it was added to fix is not an optimisation.
        #
        # The scan below is over DISTINCT (category, concept) - dozens of rows, not
        # thousands - so gating it saved almost nothing and could be fooled. Comparing
        # against the live map every time cannot be.
        changed = 0
        for r in conn.execute("SELECT DISTINCT category, concept FROM messages "
                              "WHERE COALESCE(category,'') != ''").fetchall():
            category, stored = r[0], r[1]
            live = concept_of(category)
            if stored == live:
                continue
            cur = conn.execute(
                "UPDATE messages SET concept = ? WHERE category = ? AND concept IS ?",
                (live, category, stored))
            changed += cur.rowcount
        conn.execute("INSERT INTO store_meta (key, value) VALUES ('concept_map', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (concept_fingerprint(),))
        conn.commit()
        if changed:
            # Said out loud. A silent repair of a whole store is indistinguishable from no
            # repair, and the owner has no way to learn their concept view had been wrong.
            print("concepts: re-derived %d row(s) after a change to the concept map"
                  % changed, file=sys.stderr)
        return changed
    except sqlite3.Error as exc:
        print("concepts: could not reconcile (%s)" % exc, file=sys.stderr)
        return 0


def _collapse_duplicate_account_status(conn):
    """Fold each (run_id, account) group down to one row. Idempotent; returns rows removed.

    `record_run(append=True)` gave `runs` a proper accumulate path and gave account_status an
    unconditional INSERT, fifteen lines apart in the same function under the same flag. The
    only thing that had ever held it to one row per account was the DELETE in the NON-append
    branch, which append correctly skips because that branch also wipes the day's messages.

    So every hourly top-up added another card for the same mailbox, and the account panel -
    the one panel whose entire job is to say which mailboxes exist - counted rows and reported
    "4/4 connected" for one mailbox. Self-concealing, because each card's counts were REAL: it
    read as a healthy multi-mailbox install rather than as corruption. And it bit precisely
    the deployments that sweep often, which is the guidance the plugin itself gives.

    Counters SUM; snapshot fields take the LATEST value. Summing an inbox count is
    meaningless, and a stale CONNECTED must never survive a later FAILED.
    """
    try:
        dupes = list(conn.execute(
            "SELECT run_id, account, COUNT(*) FROM account_status "
            "GROUP BY run_id, account HAVING COUNT(*) > 1"))
    except sqlite3.Error:
        return 0
    if not dupes:
        return 0
    removed = 0
    for run_id, account, _n in dupes:
        rows_ = list(conn.execute(
            "SELECT id, role, status, auth, inbox_count, fetched, trashed, kept, error "
            "FROM account_status WHERE run_id = ? AND account = ? ORDER BY id",
            (run_id, account)))
        keep = rows_[0][0]
        latest = rows_[-1]
        conn.execute(
            "UPDATE account_status SET role = ?, status = ?, auth = ?, inbox_count = ?, "
            "error = ?, fetched = ?, trashed = ?, kept = ? WHERE id = ?",
            (latest[1], latest[2], latest[3], latest[4], latest[8],
             sum(r[5] or 0 for r in rows_), sum(r[6] or 0 for r in rows_),
             sum(r[7] or 0 for r in rows_), keep))
        cur = conn.execute(
            "DELETE FROM account_status WHERE run_id = ? AND account = ? AND id != ?",
            (run_id, account, keep))
        removed += cur.rowcount
    conn.commit()
    print("account status: collapsed %d duplicate row(s) across %d account/run pair(s)"
          % (removed, len(dupes)), file=sys.stderr)
    left = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM account_status "
                        "GROUP BY run_id, account HAVING COUNT(*) > 1)").fetchone()[0]
    if left:
        # A zero is a claim. Say it out loud rather than letting the CREATE UNIQUE INDEX
        # below fail with an opaque IntegrityError on the owner's store.
        raise sqlite3.IntegrityError(
            "account_status still holds %d duplicate group(s) after the collapse migration; "
            "the store was not repaired and the unique index cannot be created" % left)
    return removed


def _backfill_msg_day(conn):
    """Derive msg_day for rows written before the column existed. Idempotent."""
    try:
        rows = list(conn.execute(
            "SELECT id, msg_date, run_date FROM messages WHERE msg_day IS NULL"))
    except sqlite3.Error:
        return 0
    if not rows:
        return 0
    unparsed = 0
    for row_id, raw, run_date in rows:
        day = msg_day(raw, None)
        if day is None:
            # Fall back to the run date rather than leaving a hole - a row with no day at
            # all would vanish from the calendar, which is a silent loss. Counted and
            # reported, because "we guessed for N rows" is a claim that should be visible.
            day = run_date
            if (raw or "").strip():
                unparsed += 1
        conn.execute("UPDATE messages SET msg_day = ? WHERE id = ?", (day, row_id))
    note = f"  derived msg_day for {len(rows)} row(s)"
    if unparsed:
        note += f"; {unparsed} had an unparseable msg_date and fell back to the run date"
    print(note)
    return len(rows)


def _migrate_thread_ack_keys(conn):
    """Recompute thread ack keys with the current rule. Returns how many moved."""
    try:
        rows = list(conn.execute(
            "SELECT key, account, sender, subject FROM acks WHERE kind = 'thread'"))
    except sqlite3.Error:
        return 0                                  # no acks table yet: nothing to move
    if not rows:
        return 0
    # Imported here, not at module scope: db.py is imported BY server.py, and importing it
    # back at the top would be a cycle.
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from server import ack_key                    # noqa: PLC0415

    moved = 0
    for old_key, account, sender, subject in rows:
        new_key = ack_key("thread", None, sender, subject, account)
        if not new_key or new_key == old_key or new_key.endswith("|"):
            continue
        # An acknowledgement already at the new key wins; this one is a duplicate of a
        # decision already recorded, so drop it rather than overwrite the newer note.
        exists = conn.execute(
            "SELECT 1 FROM acks WHERE kind = 'thread' AND key = ?", (new_key,)).fetchone()
        if exists:
            conn.execute("DELETE FROM acks WHERE kind = 'thread' AND key = ?", (old_key,))
        else:
            conn.execute("UPDATE acks SET key = ? WHERE kind = 'thread' AND key = ?",
                         (new_key, old_key))
        moved += 1
    if moved:
        print(f"  migrated {moved} thread acknowledgement(s) to the subject-scoped key")
    return moved


def msg_day(raw, fallback=None):
    """The calendar day a message ARRIVED, as YYYY-MM-DD, or the fallback.

    WHY THIS IS NOT A ONE-LINER. `msg_date` is stored exactly as the run wrote it, and runs
    do not agree: a live store holds ISO dates, RFC 2822 dates ("Wed, 5 Aug 2026 06:06:25
    -0500"), and NULLs, all in the same column. Grouping on the raw value - or on its first
    ten characters - buckets those RFC rows under "Wed, 5 Aug" and produces a calendar that
    looks plausible and is wrong, which is the failure this project keeps meeting.

    So the raw value is KEPT and a normalised day is derived beside it, exactly as `concept`
    sits beside `category`. The raw history is evidence and is not thrown away; the derived
    column is what queries are allowed to trust.
    """
    s = (raw or "").strip()
    if not s:
        return fallback
    if len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit():
        return s[:10]                                   # already ISO, or ISO-prefixed
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return fallback                                 # unparseable: say so by not guessing


_ADDR = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def recipients_of(msg, account):
    """(recipients, count, addressed_directly) for one message.

    WHY THIS COLUMN EXISTS, and it is the most valuable thing in the store that was not in
    it. Both fetchers captured `To` from the beginning and ingest threw it away, so the tool
    could not tell the difference between a message sent to YOU and the same sender's blast
    to two hundred people. That distinction is what separates "this sender is a bot, bin it"
    from "this sender is a bot EXCEPT when it is assigning you work" - and a reported rule
    would have binned GitHub mentions and task assignments, unread, on exactly that mistake.

    `addressed_directly` means the mailbox appears in **To**, not merely in Cc: being one of
    twenty on a Cc line is not the same as being asked. Errors fall toward NOT-direct, so a
    rule built on this under-claims rather than over-claims.
    """
    to = str(msg.get("to") or "")
    cc = str(msg.get("cc") or "")
    joined = ", ".join(x for x in (to, cc) if x.strip())
    addrs = _ADDR.findall(joined)
    # No parseable address is not zero recipients - it is an unknown, and a count of 0 would
    # read as "sent to nobody", which is a claim we have no evidence for.
    count = len(set(a.lower() for a in addrs)) if addrs else None
    acct = (account or "").strip().lower()
    direct = None
    if acct:
        direct = 1 if acct in (a.lower() for a in _ADDR.findall(to)) else 0
        if not to.strip():
            direct = None                        # nothing to judge from
    return (joined[:400] or None), count, direct


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



# Which importances mean "a person has to do something". Anything else is information, and
# information does not stay open - it is read or it is not.
ATTENTION = ("action-needed", "family", "security", "financial")


_SHAPE_SUBS = [
    # REPLY AND FORWARD PREFIXES COME OFF FIRST, and the whole chain in one pass - "Re: Fwd:
    # Re: " is one match, not three. This has to run BEFORE the punctuation rule below, which
    # strips the colons and would leave "re" and "fwd" looking like ordinary leading words.
    #
    # Leaving them in split a thread from its own replies, and - because subject_shape also
    # feeds api_repeats - split a notice from its own follow-ups in the very view whose
    # comment calls it THE DROWNING MECHANISM. One missing rule, two features quietly wrong.
    #
    # The colon is required, so "Re-engineering the process" and "Fwd Thinking Ltd" are
    # untouched. Non-English prefixes included because a mailbox is not monolingual; single
    # letters ("R:", "I:") are deliberately NOT, since they collide with real subjects.
    (re.compile(r"^(?:\s*(?:re|ref|fw|fwd|aw|wg|sv|vb|vs|vl|rv|tr|antw|doorst|enc|res|odp|pd"
                r"|ynt|ilt|回复|轉寄)\s*(?:\[\d+\]|\(\d+\))?\s*:)+\s*", re.I), " "),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"\b\d{1,3}(?:[,.]\d{3})*(?:\.\d\d)?\b"), " "),   # amounts / counts
    (re.compile(r"[#$£€]\s*\S+"), " "),
    (re.compile(r"\b\d+\b"), " "),
    (re.compile(r"[^\w\s]+"), " "),
    (re.compile(r"\s+"), " "),
]


def subject_shape(subject):
    s = (subject or "").lower()
    for pat, rep in _SHAPE_SUBS:
        s = pat.sub(rep, s)
    return s.strip()


def open_item_key(msg):
    """The durable handle for an outstanding item.

    Message-ID when there is one, because it survives folders and re-sends. Otherwise the
    sender-and-subject shape, which is what a recurring obligation actually is - the same
    notice arriving monthly is ONE open item, not twelve, and keying it per message would
    reproduce the drowning this table exists to stop.
    """
    mid = (msg.get("message_id") or "").strip()
    if mid:
        return "message", mid
    sender = (msg.get("sender") or "").strip().lower()
    # subject_shape, NOT a local regex. A second, simpler shaping here would have left the
    # reply prefixes on - so "Re: your renewal" and "your renewal" would be two separate
    # open items, which is precisely the split 0.5.2 was spent closing for acks and
    # repeats. The rule has one home.
    subject = subject_shape(msg.get("subject"))
    if not sender or not subject:
        return None, None
    return "thread", "%s|%s" % (sender, subject)



def _already_acknowledged(conn, msg):
    """Has the owner already dismissed this message, or its whole series?

    Queried by CONTENT - the account, sender and subject the ack recorded - so it works
    whether the ack was stored under a Message-ID or under the row fallback, and needs no
    key derivation of its own.
    """
    mid = (msg.get("message_id") or "").strip()
    if mid and conn.execute(
            "SELECT 1 FROM acks WHERE kind = 'message' AND key = ?", (mid,)).fetchone():
        return True
    account = (msg.get("account") or "").strip()
    subject = (msg.get("subject") or "").strip()
    # THE CONTENT FALLBACK IS ONLY FOR ACKS THAT HAVE NO MESSAGE-ID OF THEIR OWN.
    #
    # `server.ack_key` stores a message ack under its Message-ID when there is one, and
    # under a `row:` identity when there is not - and its docstring states why the fallback
    # uses the EXACT subject: "so it stays one item and does not silence a whole series."
    # Matching account+subject for EVERY message ack threw that away. It made a `message`
    # ack behave like a `thread` ack, which is the one distinction the two scopes exist to
    # draw.
    #
    # THE SHAPE THAT MAKES THIS WORST. Some notification series carry a constant,
    # uninformative subject by design - a portal that mails "you have a new secure message"
    # and deliberately keeps the content out of the mail sends the identical subject every
    # time. For such a series the subject is the ONE thing every instance shares, so an
    # account+subject match is effectively a match on the series. Acknowledging a single
    # message then suppresses the standing item for every later arrival, permanently, from
    # one click that was only ever about a different message. Any message ack carrying a
    # Message-ID was capable of this, which on a mature store is most of them.
    #
    # An ack keyed on a Message-ID is a statement about THAT message; if this message has a
    # different one, it is a different message. The `row:` acks keep the fallback, because
    # for them the account and subject are the only handle that was ever recorded.
    if subject and conn.execute(
            "SELECT 1 FROM acks WHERE kind = 'message' AND COALESCE(account,'') = ? "
            "AND COALESCE(subject,'') = ? AND COALESCE(key,'') NOT LIKE '<%>'",
            (account, subject)).fetchone():
        return True
    # A thread ack silences the whole recurring series, so it covers this instance too.
    shape = subject_shape(subject)
    if not shape:
        return False
    for r in conn.execute("SELECT COALESCE(account,''), COALESCE(subject,'') "
                          "FROM acks WHERE kind = 'thread'"):
        if r[0].lower() == account.lower() and subject_shape(r[1]) == shape:
            return True
    return False

def carry_open_items(conn, messages, run_date):
    """Open an item for anything that needs a person, and age the ones already open.

    Returns (opened, still_open_seen). Reported by ingest rather than done silently: a
    carry-forward that quietly opens nothing looks exactly like a mailbox with nothing
    outstanding, and those two states must never render the same.

    A RESOLVED ITEM IS NOT REOPENED by the same message arriving again in a later batch -
    that is a re-ingest, not a new obligation. It IS reopened by a genuinely new message,
    because a new Message-ID is a new thing to do.
    """
    opened = seen = 0
    for m in messages:
        if (m.get("importance") or "") not in ATTENTION:
            continue
        if m.get("disposition") in DISPOSABLE:
            # Binned by the triage that just ran, or judged bin-worthy by a run with no
            # power to act - either way the triage said it is not outstanding, and whether
            # the mailbox obeyed is a separate question from what was decided.
            continue
        kind, key = open_item_key(m)
        if not key:
            continue
        row = conn.execute("SELECT state, runs_seen FROM open_items WHERE key = ?",
                           (key,)).fetchone()
        if row is None and _already_acknowledged(conn, m):
            # ALREADY DISMISSED, SO NOT NEWLY OUTSTANDING. An acknowledgement means "I have
            # seen this", and seeing is not doing - so an ack does NOT close an item that is
            # already open, and that distinction is deliberate. But OPENING one for mail the
            # owner has already dealt with in the dashboard is the tool arguing with its own
            # record of their judgment, and it is how a standing list fills with things
            # somebody already answered.
            #
            # Matched on what the acks table actually stored rather than by re-deriving a
            # key here: db.py writes keys and server.py reads them, and a third derivation
            # in between is exactly the drift that orphaned every ack once already.
            continue
        if row is None:
            conn.execute(
                "INSERT INTO open_items (key, kind, account, sender, subject, concept, "
                "importance, first_seen, last_seen, runs_seen, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,'open')",
                (key, kind, m.get("account"), m.get("sender"), m.get("subject"),
                 concept_of(m.get("category")), m.get("importance"),
                 msg_day(m.get("msg_date"), run_date), run_date))
            opened += 1
        elif row["state"] == "open":
            # runs_seen counts RUNS, not messages, so a second batch on the same day does
            # not make a three-day-old item look three times as urgent.
            conn.execute(
                "UPDATE open_items SET last_seen = ?, "
                "runs_seen = runs_seen + (CASE WHEN last_seen = ? THEN 0 ELSE 1 END) "
                "WHERE key = ?", (run_date, run_date, key))
            seen += 1
    return opened, seen



# THE PUBLIC INPUT CONTRACT, kept beside the INSERT that consumes it.
#
# `ingest.py` is documented as the supported entry point from any source, which makes this
# list an API. It used to be discoverable only by reading the INSERT statement below, and
# anything not in it was dropped on the floor with `ok: true` and every count correct - so a
# connector author supplying `web_link`, or anyone who typed `messageId` instead of
# `message_id`, got silence and assumed it landed. The source data is gone by the time
# anyone notices.
#
# Defined here rather than in ingest.py because this is where the fields are actually
# consumed; a contract that lives away from its implementation is one that drifts.
# ---------------------------------------------------------------------------------------
# THE DISPOSITION VOCABULARY, in one place, because it is read in seventeen.
#
# `would_trash` was added because the honest verdict of a READ-ONLY triage - "this is bot
# noise, I would bin it, I have no power to" - was not expressible. The valid set was
# trashed / surfaced / kept / saved, and writing `trashed` would have been a lie about what
# happened to the mail. So `kept` got written instead, and `kept` in this tool's vocabulary
# means THE ROUTINE DECIDED TO KEEP THIS: a positive judgment about the sender.
#
# The consequence was that the read-only discipline this project insists on - and which a
# connector install has no way to escape, because it has no fetcher to act with - poisoned
# the auto-trash guard on day one, permanently. The "not pure noise" rule refuses any sender
# with kept mail, so on such an install NO sender could ever clear it. Not because anything
# was protected: because there was no correct value to write.
#
# The distinction that matters is between what was JUDGED and what was DONE:
#
#   DISPOSABLE          judged to be noise. `trashed` was acted on, `would_trash` was not.
#                       Both are evidence about the sender; only one is a claim about the
#                       mailbox's current contents.
#   DELIBERATELY_KEPT   a positive decision to keep. This is what the guard weighs against
#                       a rule, and nothing may land here by default.
#
# Read these sets rather than comparing to a literal. `disposition != 'trashed'` was the
# spelling that made would_trash mail count as kept - true of the string, false of the
# meaning.
DISPOSABLE = frozenset(("trashed", "would_trash"))
DELIBERATELY_KEPT = frozenset(("kept", "surfaced", "saved"))
DISPOSITIONS = DISPOSABLE | DELIBERATELY_KEPT


MESSAGE_FIELDS = frozenset((
    "account", "sender", "subject", "msg_date", "disposition", "category", "reason",
    "importance", "message_id", "injection_signals", "to", "cc", "body_text", "web_link",
))

RUN_FIELDS = frozenset(("run_date", "notes", "accounts", "messages", "steam_sales",
                        "acknowledgements", "disposed"))


def store_ready(path=None):
    """Does a real store exist here - schema and all - or merely a FILE?

    EXISTENCE IS NOT READINESS, and the difference is what makes a fresh clone look broken.
    `sqlite3.connect()` CREATES an empty file, so any suite or script that connects before the
    schema exists leaves a 0-byte database behind. The next thing to ask `os.path.exists(db)`
    then gets True, concludes there is a store, queries a table that was never created, and
    dies with `no such table: messages` - which reads like a corrupt install rather than one
    that was simply never installed.

    Measured on a freshly built tree: four suites failed that way, and every one of them was
    correct code meeting an empty file that an earlier suite had conjured.

    Same shape as the other honesty rules here: an HTTP 200 answers "is something listening",
    never "is it what you shipped"; a file answers "does a path exist", never "is there
    anything in it".
    """
    p = path or DB_PATH
    try:
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return False
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        try:
            return c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                             "AND name='messages'").fetchone() is not None
        finally:
            c.close()
    except (sqlite3.Error, OSError):
        return False


def record_disposed(conn, message_ids):
    """Promote rows to `trashed` for mail that has ACTUALLY been disposed of.

    THE ONE IMPLEMENTATION, on purpose. Two callers need it and they used to be two copies
    waiting to drift: the applier, after it moves mail itself; and `ingest.py`, taking a
    receipt back from a client that did the moving on an install with no IMAP. "May this be
    binned / was this binned" answered in two files is the defect this project keeps naming,
    so it is answered here.

    Only rows sitting at `would_trash` move. A row somebody has since re-triaged by hand is
    never overwritten by a receipt, and a row already `trashed` is not double-counted - which
    makes replaying the same receipt harmless.

    Returns how many rows actually moved, so a caller can report what LANDED rather than what
    it sent. A receipt naming ten messages and moving zero is a real answer and must be
    visible: it means the store never had them.
    """
    ids = [str(m).strip() for m in (message_ids or []) if str(m or "").strip()]
    if not ids:
        return 0
    moved, touched = 0, set()
    # Chunked: SQLite caps host parameters, and a receipt from a large sweep can exceed it.
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT DISTINCT run_date FROM messages WHERE disposition = 'would_trash' "
                "AND message_id IN (%s)" % marks, chunk):
            touched.add(r[0])
        cur = conn.execute(
            "UPDATE messages SET disposition = 'trashed' "
            "WHERE disposition = 'would_trash' AND message_id IN (%s)" % marks, chunk)
        moved += cur.rowcount or 0
    # The day's own numbers have to move with the rows, or the run row disagrees with the list
    # underneath it - which is how a record starts lying quietly. RECOMPUTED from the messages
    # rather than incremented: an increment is only correct once, so replaying a receipt would
    # inflate it, and this whole function is meant to be safe to replay.
    for d in touched:
        conn.execute(
            "UPDATE runs SET trashed = (SELECT COUNT(*) FROM messages WHERE run_date = ? "
            "  AND disposition = 'trashed'), "
            "kept = (SELECT COUNT(*) FROM messages WHERE run_date = ? "
            "  AND disposition IN ('kept','surfaced','saved')) "
            "WHERE run_date = ?", (d, d, d))
    conn.commit()
    return moved

ACK_FIELDS = frozenset(("kind", "message_id", "sender", "subject", "account", "note", "on"))

ACCOUNT_FIELDS = frozenset((
    "account", "role", "status", "auth", "inbox_count", "fetched", "trashed", "kept",
    "error",
))


def unknown_fields(data):
    """Every key the store will not read, with how many times it appeared.

    Reported rather than rejected. Forward compatibility matters - a caller running against
    a newer contract than the installed version should still succeed - but naming what was
    dropped costs nothing, and it is the same move already made for `linked N/M` and
    `mapped N/M`.
    """
    out = {}

    def note(key, where):
        out.setdefault("%s (%s)" % (key, where), 0)
        out["%s (%s)" % (key, where)] += 1

    for k in (data or {}):
        if k not in RUN_FIELDS:
            note(k, "run")
    for a in (data or {}).get("accounts") or []:
        for k in a:
            if k not in ACCOUNT_FIELDS:
                note(k, "account")
    for m in (data or {}).get("messages") or []:
        for k in m:
            if k not in MESSAGE_FIELDS:
                note(k, "message")
    for a in (data or {}).get("acknowledgements") or []:
        for k in a:
            if k not in ACK_FIELDS:
                note(k, "acknowledgement")
    return out

def ingest_run(run_date, accounts=None, messages=None, notes=None, steam_sales=None,
               append=False, open_items=True):
    """
    Idempotent per run_date: REPLACES any existing data for that day. Returns
    (run_id, replaced_count, open_stats) where open_stats is
    {"opened": n, "still_open_seen": n, "suppressed": bool}

    `open_items=False` ingests the mail WITHOUT opening standing to-do items. For a
    historical batch that is the correct behaviour and not an optimisation: a year of old
    `action-needed` would arrive as scores of stale entries on a list whose whole argument
    is that its contents are live. History is history. Anything in it that is genuinely
    still outstanding comes back on its own, because whoever wants it is still asking. - the carry-forward, RETURNED rather than left in a
    module global for a caller to fish out. Reported by ingest because a carry-forward that
    quietly opened nothing looks exactly like a mailbox with nothing outstanding.

    Replace is correct for a daily sweep and a footgun for a batched intake: every batch
    had to re-send every message already ingested for that date, or the earlier ones were
    silently deleted - and the return value reported the count it had just written, which
    looked exactly like success. `append=True` adds to the day instead, and the caller is
    told what was removed either way.

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
        row = conn.execute("SELECT id FROM runs WHERE run_date = ?", (run_date,)).fetchone()
        replaced = 0
        existing_id = row["id"] if row else None
        if row:
            replaced = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE run_id = ?", (row["id"],)).fetchone()[0]
        if row and not append:
            # wipe any prior data for this date, then re-create the run row
            conn.execute("DELETE FROM messages WHERE run_id = ?", (row["id"],))
            conn.execute("DELETE FROM account_status WHERE run_id = ?", (row["id"],))
            conn.execute("DELETE FROM runs WHERE id = ?", (row["id"],))
            existing_id = None
        elif row and append:
            replaced = 0            # nothing was removed; the day grew

        # The run row counts what was DONE, so `trashed` stays actual removals - a summary
        # claiming it binned mail it had no power to bin would be the same lie in reverse.
        trashed = sum(1 for m in messages if m.get("disposition") == "trashed")
        kept = sum(1 for m in messages if m.get("disposition") in DELIBERATELY_KEPT)
        otp = sum(1 for m in messages if (m.get("category") or "").lower() == "otp")
        fetched = sum(int(a.get("fetched") or 0) for a in accounts) or len(messages)

        if existing_id is not None:
            # Appending: keep the run row and carry the totals forward, so the day's
            # numbers describe everything in it rather than only the last batch.
            run_id = existing_id
            conn.execute(
                "UPDATE runs SET fetched = fetched + ?, trashed = trashed + ?, "
                "kept = kept + ?, otp = otp + ?, created_at = ?, "
                "notes = COALESCE(?, notes) WHERE id = ?",
                (fetched, trashed, kept, otp, now_iso(), notes, run_id))
        else:
            cur = conn.execute(
                "INSERT INTO runs (run_date, created_at, fetched, trashed, kept, otp, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_date, now_iso(), fetched, trashed, kept, otp, notes),
            )
            run_id = cur.lastrowid

        for a in accounts:
            # UPSERT, matching what `runs` does fifteen lines up. Counters accumulate so the
            # day's card describes everything in it rather than the last batch; snapshot
            # fields overwrite, because summing an inbox size is meaningless and a stale
            # CONNECTED must not survive a later FAILED. Without this, every append invented
            # a mailbox - see _collapse_duplicate_account_status.
            conn.execute(
                "INSERT INTO account_status "
                "(run_id, account, role, status, auth, inbox_count, fetched, trashed, kept, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, account) DO UPDATE SET "
                "  role = excluded.role, status = excluded.status, auth = excluded.auth, "
                "  inbox_count = excluded.inbox_count, error = excluded.error, "
                "  fetched = account_status.fetched + excluded.fetched, "
                "  trashed = account_status.trashed + excluded.trashed, "
                "  kept    = account_status.kept    + excluded.kept",
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
                "(run_id, run_date, account, sender, subject, msg_date, msg_day, "
                "disposition, category, concept, reason, importance, message_id, "
                "injection_signals, recipients, recipient_count, addressed_directly, "
                "body_text, web_link) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_date, m.get("account"), m.get("sender"), m.get("subject"),
                 m.get("msg_date"),
                 # Normalised on WRITE, falling back to the run date so every row has a
                 # day the calendar can group on and none silently vanish from it.
                 msg_day(m.get("msg_date"), run_date),
                 m.get("disposition"), m.get("category"),
                 concept_of(m.get("category")),
                 m.get("reason"), m.get("importance"),
                 (m.get("message_id") or "").strip() or None,
                 json.dumps(m["injection_signals"]) if m.get("injection_signals")
                 else None,
                 *recipients_of(m, m.get("account")),
                 (m.get("body_text") or None), (m.get("web_link") or None)),
            )
        opened, still_open = (carry_open_items(conn, messages, run_date)
                              if open_items else (0, 0))
        conn.commit()
    finally:
        conn.close()

    # Steam sales are keyed by app_id and persist across runs (not wiped per day),
    # so upsert them separately after the per-day data is committed.
    for s in steam_sales:
        upsert_steam_sale(
            s.get("app_id"), title=s.get("title"), url=s.get("url"),
            discount_pct=s.get("discount_pct"), seen_date=run_date)

    # BOTH numbers, always. A caller that only learns what it wrote cannot tell a clean
    # append from a replace that silently deleted the previous nine batches.
    return run_id, replaced, {"opened": opened, "still_open_seen": still_open,
                              "suppressed": not open_items}


def save_retention_scan(conn, scanned_at, walked, matched, boxes_scanned, boxes_total,
                        truncated, failed, items, shift_days=0):
    """Persist one rule-15 shelf scan, answer and reach together.

    `shift_days > 0` marks a POSITIVE-CONTROL run (`retention_scan.py --shift-days N`), which
    ages every message to prove the scan is capable of firing at all. Its inflated list is a
    test result, not the state of the mailbox - so it is recorded (the control having run is
    worth knowing) and never served. Serving it would be the exact inversion of the thing the
    control exists to prevent: an instrument whose reassurance is unearned, here reading as
    alarm nobody can act on.
    """
    conn.execute(
        "INSERT OR REPLACE INTO retention_scans "
        "(scanned_at, walked, matched, boxes_scanned, boxes_total, truncated, failed, "
        " shift_days) VALUES (?,?,?,?,?,?,?,?)",
        (scanned_at, walked, matched, boxes_scanned, boxes_total,
         json.dumps(truncated or []), json.dumps(failed or []), int(shift_days or 0)))
    conn.execute("DELETE FROM retention_shelf WHERE scanned_at = ?", (scanned_at,))
    for it in items or []:
        conn.execute(
            "INSERT INTO retention_shelf "
            "(scanned_at, account, sender, subject, msg_date, uid, label, shelf_days, "
            " age_days) VALUES (?,?,?,?,?,?,?,?,?)",
            (scanned_at, it.get("account"), it.get("sender"), it.get("subject"),
             it.get("msg_date"), str(it.get("uid")) if it.get("uid") is not None else None,
             it.get("label"), it.get("shelf_days"), it.get("age_days")))
    conn.commit()


def _refusal_sender_key(sender):
    """ONE normaliser, used on BOTH sides of the shelf/refusal join.

    Two tables record the same sender as free text, and matching raw strings across them is
    how this lane has produced a confident wrong number six times now - a sender that differs
    only by case or by surrounding whitespace silently fails to join, and the answer comes
    back smaller and more reassuring than the truth. So neither side gets to spell it its own
    way: both go through here.
    """
    return (sender or "").strip().lower() or None


def retention_shelf(conn):
    """The newest REAL shelf scan - its items, and what it actually reached.

    `ever_scanned` is the distinction an empty list must never lose. "Nothing is past its
    shelf" and "nobody has ever asked" are opposite claims that render identically as [], and
    this lane has already shipped that mistake twice in the scan itself.

    `complete` is the second one. A scan that lost a box to a throttled account, or was cut
    short by its own time budget, reports a SMALLER number of overdue items - so a partial
    walk always looks like better news than a full one. The flag travels ON the answer rather
    than beside it, because the last layer is where an instrument's honesty is decided.
    """
    row = conn.execute(
        "SELECT * FROM retention_scans WHERE shift_days = 0 "
        "ORDER BY scanned_at DESC LIMIT 1").fetchone()
    if row is None:
        return {"ever_scanned": False, "scanned_at": None, "items": [], "count": 0,
                "walked": None, "matched": None, "boxes_scanned": None, "boxes_total": None,
                "truncated": [], "failed": [], "complete": False}

    truncated = json.loads(row["truncated"] or "[]")
    failed = json.loads(row["failed"] or "[]")
    items = []
    for r in conn.execute(
            "SELECT * FROM retention_shelf WHERE scanned_at = ?", (row["scanned_at"],)):
        it = dict(r)
        shelf, age = it.get("shelf_days"), it.get("age_days")
        # Derived, never stored: an overdue figure kept alongside the two numbers it comes
        # from is one more thing that can drift out of agreement with them.
        it["overdue_days"] = (round(age - shelf, 1)
                              if age is not None and shelf is not None else None)
        items.append(it)
    items.sort(key=lambda i: (i["overdue_days"] is None, -(i["overdue_days"] or 0)))

    # WHY THIS PILE IS STUCK, attached to the pile itself. A bare count reads as a backlog
    # nobody has got round to. It can equally be a pile the disposal guard has ALREADY refused
    # to release, which is the opposite claim about whose move it is - and the panel rendered
    # the two identically. Retention rules and the guard can disagree permanently, so this
    # distinction decides whether a reader is looking at work or at a deadlock.
    #
    # This reports what the guard DID (a row it wrote in disposal_refusals), never a
    # re-derivation of what it would do. Predicting the verdict here would put a second,
    # drifting copy of the guard's reasoning on the page - the duplicated-definition disease
    # this lane already knows - and it would claim more than the evidence supports. A shelf
    # item with no recorded refusal is left unmarked rather than guessed at: unmarked means
    # "the guard has not been asked or did not record", never "the guard would allow it".
    refusals = {}
    for r in conn.execute(
            "SELECT sender, run_date, reasons FROM disposal_refusals ORDER BY run_date"):
        key = _refusal_sender_key(r["sender"])
        if key and key not in refusals:
            refusals[key] = {"since": r["run_date"], "reason": r["reasons"]}
    for it in items:
        hit = refusals.get(_refusal_sender_key(it.get("sender")))
        it["blocked"] = bool(hit)
        it["blocked_since"] = hit["since"] if hit else None
        it["blocked_reason"] = hit["reason"] if hit else None

    return {
        "ever_scanned": True,
        "scanned_at": row["scanned_at"],
        "items": items,
        "count": len(items),
        "blocked_count": sum(1 for i in items if i["blocked"]),
        "walked": row["walked"],
        "matched": row["matched"],
        "boxes_scanned": row["boxes_scanned"],
        "boxes_total": row["boxes_total"],
        "truncated": truncated,
        "failed": failed,
        "complete": (not truncated and not failed
                     and row["walked"] == row["matched"]
                     and row["boxes_scanned"] == row["boxes_total"]),
    }


if __name__ == "__main__":
    init_db()
    print("initialized", DB_PATH)
