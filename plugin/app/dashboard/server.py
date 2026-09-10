"""
Localhost dashboard server for the email routine. Stdlib only.

  python dashboard/server.py [--port 8765] [--host 127.0.0.1]

Serves the static UI at /  and a small JSON API at /api/*. Binds to 127.0.0.1
by default (local-only; not reachable from the network).
"""
import argparse
import collections
import email.utils as email_utils
import json
from datetime import date, datetime, timedelta
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import concepts
from version import VERSION

# Stamped ONCE, at import, deliberately. Paired with VERSION it answers "how long has this
# exact code been serving?" - and a start time that predates your last edit is the tell that
# the process is stale, whatever the files on disk say.
STARTED_AT = datetime.now().replace(microsecond=0).isoformat()
import db
import mailview
import signin
from categorize import LABELS

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# CREATE_NO_WINDOW. This server runs under `pythonw` as a 24/7 login-started service, so it
# has NO console of its own - and on Windows a console-less parent that spawns a console
# program (python, git, cmd) makes the OS allocate a VISIBLE console for the child. It
# appears, steals keyboard focus, and vanishes. Every spawn in this file gets the flag; the
# owner was chasing that flicker across three machines before it was traced here.
#
# Two things that look like the fix and are not: DETACHED_PROCESS only pushes the flash one
# level down (the child is then console-less itself), and the flag does NOT inherit, so
# guarding a launcher does nothing for what the launcher spawns. capture_output does not
# help either - that redirects the streams, and the console is a separate object.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- API handlers ----------

def api_runs(conn, q):
    return rows(conn.execute(
        "SELECT run_date, created_at, fetched, trashed, kept, otp, notes "
        "FROM runs ORDER BY run_date DESC"))


def _resolve_date(conn, q):
    """Resolve ?date= to a run that EXISTS, or to None.

    It used to return whatever the caller asked for, unchecked. So a client with no runs
    yet sent the literal string "null", and the page cheerfully reported "showing run for
    null" - a value that was never a date, echoed back as though it had been looked up.
    Small, but it is the same shape as every other defect here: an answer stated with more
    confidence than the lookup behind it.
    """
    date = (q.get("date") or [None])[0]
    if date and date not in ("latest", "null", "undefined", "None"):
        row = conn.execute("SELECT run_date FROM runs WHERE run_date = ?", (date,)).fetchone()
        if row:
            return date
        return None                      # asked for a run that does not exist
    row = conn.execute("SELECT run_date FROM runs ORDER BY run_date DESC LIMIT 1").fetchone()
    return row["run_date"] if row else None


def api_run(conn, q):
    date = _resolve_date(conn, q)
    if not date:
        return {"run_date": None, "accounts": [], "messages": [], "totals": {}}
    run = conn.execute("SELECT * FROM runs WHERE run_date = ?", (date,)).fetchone()
    run = dict(run) if run else {}
    run_id = run.get("id")
    accounts = rows(conn.execute(
        "SELECT * FROM account_status WHERE run_id = ? ORDER BY account", (run_id,))) if run_id else []
    # ACCOUNT STATUS IS ABOUT CONNECTIVITY, NOT ABOUT A DAY IN THE PAST.
    #
    # A historical run - anything staged by arrival date from an intake - carries no
    # account_status rows, because nothing connected to a mailbox on that day. The panel
    # then said "nothing recorded for this run", which is true and reads as the tool having
    # lost its accounts. Whether eight mailboxes are reachable is a fact about NOW.
    #
    # So it falls back to the most recent run that actually has a status, and says which day
    # that was. An old answer labelled with its date is useful; a blank panel is not.
    accounts_as_of = date
    if not accounts:
        # The MOST RECENT status anywhere, not the most recent BEFORE this date. Looking
        # backwards found nothing at all here: every run older than the first sweep is a
        # backfilled one, so "the last status before June 11th" does not exist. And it is
        # the wrong question anyway - whether a mailbox connects is true of today, not of
        # the day you happen to be looking at.
        row = conn.execute(
            "SELECT r.run_date, r.id FROM runs r JOIN account_status a ON a.run_id = r.id "
            "GROUP BY r.id ORDER BY r.run_date DESC LIMIT 1").fetchone()
        if row:
            accounts = rows(conn.execute(
                "SELECT * FROM account_status WHERE run_id = ? ORDER BY account",
                (row["id"],)))
            accounts_as_of = row["run_date"]
    messages = rows(conn.execute(
        "SELECT * FROM messages WHERE run_id = ? ORDER BY "
        "CASE disposition WHEN 'surfaced' THEN 0 WHEN 'kept' THEN 1 ELSE 2 END, account",
        (run_id,))) if run_id else []
    # Mark what has already been acknowledged. Acknowledged items are NOT removed - the row,
    # the reason and the paper trail all stay - they simply stop competing for attention,
    # which is the other half of the drowning problem.
    annotate_acks(conn, messages)
    annotate_carried(conn, messages, date)

    # ALREADY SEEN IS NOT NEWS.
    #
    # A message that is still sitting in the inbox gets re-listed by every run, so the same
    # item was surfaced day after day - one Google notice appeared on four consecutive days
    # with nothing whatsoever having changed. Measured on a live store, account-security
    # listings outnumbered the DISTINCT messages behind them by about three to two - so
    # roughly two in five of the "alerts" a person read were a repeat of one already read.
    #
    # That is how a security channel gets destroyed. Not by being wrong - by being boring in
    # a way that teaches its reader to skip it, so that the one alert that matters arrives
    # into a habit of not looking. The volume of DISTINCT security mail here is about one
    # every other day, which is fine; it was the repetition that made it unreadable.
    #
    # Carried items are NOT dropped from the payload - they are marked, counted and returned,
    # because a panel that quietly showed fewer things would be the same silence in the
    # pleasant direction. The UI leads with what is new and states what it held back.
    show_carried = (q.get("carried", ["0"])[0] or "0") == "1"
    surfaced_all = [m for m in messages if m["disposition"] in db.DELIBERATELY_KEPT]
    carried_n = sum(1 for m in surfaced_all if m.get("carried"))
    surfaced = surfaced_all if show_carried else [m for m in surfaced_all
                                                 if not m.get("carried")]
    # REFUSED IS NOT BINNED, and this list is the paper trail for deletions.
    #
    # `db.DISPOSABLE` is {"trashed", "would_trash"} - the set of things the disposer may be
    # ASKED about. Using it here made the drill-down behind the "What I binned, and why" tile
    # list every message the guard REFUSED alongside the ones it moved, so the list ran longer
    # than the tile that opened it. Refused mail is still in the inbox where the guard left it,
    # which means the panel was claiming deletions a reader can see did not happen.
    #
    # That is the over-claiming direction, and it is the worse one. The ingest-before-apply
    # and re-ingest defects both had the record UNDERSTATE what was done, which a glance at
    # the mailbox corrects. This said mail was thrown away inside the one panel built to
    # account for throwing mail away.
    #
    # Refused mail is REPORTED rather than dropped - a quieter panel would be the same lie in
    # the pleasant direction - so it gets its own list and its own tile. Derived at read time
    # from the run's own rows: no schema change, and nothing here decides what may be binned.
    trashed = [m for m in messages if m["disposition"] == "trashed"]
    held = [m for m in messages if m["disposition"] == "would_trash"]
    return {"run_date": date,
        "accounts_as_of": accounts_as_of, "run": run, "accounts": accounts,
            "surfaced": surfaced, "trashed": trashed, "held": held,
            "carried_hidden": 0 if show_carried else carried_n,
            "totals": {"fetched": run.get("fetched", 0), "trashed": run.get("trashed", 0),
                       "kept": run.get("kept", 0), "otp": run.get("otp", 0),
                       "held": len(held)}}


def api_trash_stats(conn, q):
    """Category/concept breakdown. Scoped by DISPOSITION, defaulting to trashed.

    'kept' here means kept OR surfaced - from the reader's side those are one idea ("mail
    I did not bin"), and splitting them would put the bills in one panel and the security
    notices in another for no reason a person would recognise.
    """
    # category breakdown across all time (or a date range)
    disposition = (q.get("disposition") or ["trashed"])[0].strip().lower()
    if disposition in ("all", "any", "*"):
        where, params = "WHERE 1=1", []
    elif disposition == "kept":
        where, params = "WHERE disposition IN ('kept','surfaced','saved')", []
    else:
        where, params = "WHERE disposition = ?", [disposition]
    if q.get("date"):
        where += " AND run_date = ?"; params.append(q["date"][0])
    if q.get("from"):
        where += " AND run_date >= ?"; params.append(q["from"][0])
    if q.get("to"):
        where += " AND run_date <= ?"; params.append(q["to"][0])
    by_cat = rows(conn.execute(
        f"SELECT category, COUNT(*) n FROM messages {where} GROUP BY category ORDER BY n DESC", params))
    for r in by_cat:
        r["label"] = LABELS.get(r["category"], r["category"] or "other")
    total = sum(r["n"] for r in by_cat)
    # The SAME rows rolled up onto the canonical 12 concepts. The raw breakdown above is
    # honest but unusable for a question like "how much money mail was there" - 12 different
    # labels have meant money, and the biggest single one reaches only a third of them.
    # Both are returned; neither replaces the other.
    by_concept = rows(conn.execute(
        f"SELECT COALESCE(concept,'unmapped') concept, COUNT(*) n FROM messages {where} "
        "GROUP BY 1 ORDER BY n DESC", params))
    for r in by_concept:
        r["key"] = concepts.key_of(r["concept"])
    by_day = rows(conn.execute(
        f"SELECT run_date, COUNT(*) n FROM messages {where} GROUP BY run_date ORDER BY run_date", params))
    return {"total": total, "by_category": by_cat, "by_concept": by_concept, "by_day": by_day}


def api_trash_list(conn, q):
    """Trashed messages, filtered + SEARCHED + PAGED in SQL.

    Search and paging happen HERE rather than in the browser on purpose. The panel grows
    without bound, and a single category can hold a third of it. Filtering client-side means
    either shipping all of them to the page (which is what made this view unusable) or
    searching only the rows that happen to be loaded - a search that silently answers over
    a subset is worse than no search, because a zero result reads as "nothing matched".

    Returns {total, offset, limit, items} so the UI can always say WHICH slice of WHAT it
    is showing. A count with no denominator is how a filtered view starts lying.
    """
    # DISPOSITION IS A SCOPE, AND AN UNSTATED SCOPE IS A LIE (measured).
    # This endpoint was hard-filtered to trashed mail while the UI called the box "search".
    # Measured against a live store: over a third of all triaged messages could not be
    # reached by any query, and they were exactly the ones worth finding - kept mail is
    # kept precisely because it matters. EVERY record mentioning a close family member
    # was invisible, as was every record for the account holder's bank. Searching a
    # relative's name returned a confident zero.
    # That is the same disease as the shelf-life scan that could only see two days: the
    # instrument answers over a subset and reports the subset's emptiness as absence.
    # 'trashed' stays the DEFAULT so the trash view is unchanged; 'all' lifts the filter.
    disposition = (q.get("disposition") or ["trashed"])[0].strip().lower()
    if disposition in ("all", "any", "*"):
        where, params = "WHERE 1=1", []
    elif disposition == "kept":
        # Same grouping as api_trash_stats: "kept" is the reader's idea of "not binned".
        where, params = "WHERE disposition IN ('kept','surfaced','saved')", []
    else:
        where, params = "WHERE disposition = ?", [disposition]
    if q.get("category"):
        where += " AND category = ?"; params.append(q["category"][0])
    # Filter by canonical concept. Accepts either the short key ("money") or the full concept
    # name. An unrecognised value fails CLOSED with an explicit error rather than silently
    # returning the unfiltered set - a filter that quietly does nothing is the same class of
    # lie as a search that answers over a subset.
    if q.get("concept"):
        raw = q["concept"][0].strip()
        resolved = concepts.concept_for_key(raw) or (raw if raw in concepts.all_concepts() else None)
        if resolved is None and raw != concepts.UNMAPPED:
            raise ValueError(
                "unknown concept %r - valid keys: %s"
                % (raw, ", ".join(sorted(concepts.CONCEPT_KEYS.values()) + [concepts.UNMAPPED])))
        where += " AND COALESCE(concept,'unmapped') = ?"
        params.append(resolved or concepts.UNMAPPED)
    if q.get("date"):
        where += " AND run_date = ?"; params.append(q["date"][0])
    if q.get("from"):
        where += " AND run_date >= ?"; params.append(q["from"][0])
    if q.get("to"):
        where += " AND run_date <= ?"; params.append(q["to"][0])

    term = (q.get("q") or [""])[0].strip()
    if term:
        # Match the fields a person would actually search by. LIKE with escaped wildcards
        # so a literal % or _ in a subject searches for itself instead of matching everything.
        needle = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where += (" AND (sender LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' "
                  "OR reason LIKE ? ESCAPE '\\' OR account LIKE ? ESCAPE '\\')")
        params.extend([needle] * 4)

    total = conn.execute(f"SELECT COUNT(*) c FROM messages {where}", params).fetchone()["c"]

    try:
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0])))
    except ValueError:
        limit = 50
    try:
        offset = max(0, int((q.get("offset") or ["0"])[0]))
    except ValueError:
        offset = 0

    items = rows(conn.execute(
        f"SELECT run_date, account, sender, subject, reason, category, disposition, "
        f"message_id FROM messages {where} ORDER BY run_date DESC, account, id "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset]))

    annotate_acks(conn, items)
    return {"total": total, "offset": offset, "limit": limit,
            "query": term, "disposition": disposition, "items": items}


def _sender_key(raw):
    """Normalise a stored sender string to what a PERSON means by 'who sent it'.

    The sender column is free text captured by the run, and it has drifted the same way the
    category vocabulary did: some runs record a bare display name, others the full
    'Name <address>' form. Grouped raw, a single high-volume sender appeared as two roughly
    equal entries, and the panel's headline answer to "who fills my bin" was wrong by half.

    Key on the DISPLAY NAME when there is one, else the address. That merges the two
    spellings correctly. It can also merge two genuinely different addresses that share a
    display name - so the response carries every raw variant it folded, and the UI shows them.
    A normalisation that hides what it merged is just a different kind of wrong number.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # ONLY parse as an address when it actually LOOKS like one (measured).
    # email.utils.parseaddr treats a bare multi-word display name as an address LIST and
    # keeps only the first token: 'Example Delivery Service' -> ('', 'Example'). So the
    # same sender split into two keys depending on whether that run happened to record
    # the angle-bracket address - the full display name AND its first word, as two
    # unrelated senders. That silently undercounted the Top-senders panel, and it made
    # the first build of the quiet-sender panel report a whole batch of senders as having
    # gone silent when every one of them was still
    # arriving under the other spelling. A false alarm on an absence detector is fatal to
    # the only thing it is for, so the fold has to be right before the feature can exist.
    if "@" in raw or "<" in raw:
        name, addr = email_utils.parseaddr(raw)
        name = name.strip().strip('"').strip()
        if name:
            return name.lower()
        if addr:
            return addr.lower()
    return raw.strip('"').strip().lower()


def api_trash_senders(conn, q):
    """Top trashed senders in scope - the 'who is filling my bin' view.

    The category breakdown answers WHAT kind of noise arrives; it cannot answer WHO sends
    the most of it, which is the question that turns into a filter rule.
    """
    where, params = "WHERE disposition = 'trashed'", []
    if q.get("date"):
        where += " AND run_date = ?"; params.append(q["date"][0])
    raw = rows(conn.execute(
        f"SELECT sender, COUNT(*) n, MIN(run_date) first_seen, MAX(run_date) last_seen "
        f"FROM messages {where} AND sender IS NOT NULL AND sender != '' "
        f"GROUP BY sender", params))

    grouped = {}
    for r in raw:
        key = _sender_key(r["sender"])
        if key is None:
            continue
        g = grouped.setdefault(key, {"key": key, "sender": r["sender"], "n": 0,
                                     "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                                     "variants": []})
        g["n"] += r["n"]
        g["variants"].append({"raw": r["sender"], "n": r["n"]})
        if r["first_seen"] and r["first_seen"] < g["first_seen"]:
            g["first_seen"] = r["first_seen"]
        if r["last_seen"] and r["last_seen"] > g["last_seen"]:
            g["last_seen"] = r["last_seen"]

    for g in grouped.values():
        # display the longest raw variant - it is the one carrying the address
        g["variants"].sort(key=lambda v: -v["n"])
        g["sender"] = max((v["raw"] for v in g["variants"]), key=len)
        g["variant_count"] = len(g["variants"])

    top = sorted(grouped.values(), key=lambda g: (-g["n"], g["key"]))
    return {"senders": top[:40], "distinct_senders": len(grouped),
            "raw_rows": len(raw), "showing": min(40, len(grouped))}


def ack_key(kind, message_id=None, sender=None, subject=None, account=None):
    """The identity an acknowledgement is stored against.

    ONE implementation, on the server, and the computed keys are handed to the browser with
    every row. The first version recomputed them in JavaScript too, which is the same
    two-spellings-of-one-concept trap that has already produced wrong numbers in the
    category labels, the sender strings and the account column - and here it would have
    failed silently, as acknowledgements that simply never rendered.

    'message' falls back to a ROW identity when there is no Message-ID. Acknowledging is a
    statement about YOUR attention, not about the tool's ability to fetch the mail, so an item it
    cannot open must still be dismissable. The fallback uses the EXACT subject rather than
    the thread shape, so it stays one item and does not silence a whole series.
    """
    if kind == "message":
        mid = (message_id or "").strip()
        if mid:
            return mid
        return "row:%s|%s|%s" % (
            (account or "").strip().lower(), _sender_key(sender) or "",
            " ".join((subject or "").split()).lower())
    # A THREAD IS A SUBJECT, NOT A PERSON. The sender used to be part of this key, so every
    # participant in a conversation got a distinct thread key: acknowledging a thread
    # silenced exactly one person in it, the API returned ok, the row rendered as
    # acknowledged, and everyone else's messages kept arriving. Acking was O(participants),
    # and the participant set grows after you act - so a busy thread could never be fully
    # acknowledged. It never errored; it reported success.
    #
    # THE TRADE-OFF, stated because it is real and it runs the other way. Without the
    # sender, two unrelated senders whose subjects reduce to the same shape - "your
    # statement is ready" from two banks - now share a thread key, so acking one marks the
    # other. That is the LESS bad error only because it is visible: both rows change on
    # screen the moment you act, where the old failure was silent. It is scoped per account
    # to keep one mailbox's threads out of another's.
    #
    # THE REAL FIX IS A THREAD ID, not a reconstruction from subject text. RFC 5322 gives
    # one in References / In-Reply-To, and both Graph and IMAP expose it; the store does not
    # carry it yet. When it does, this should key on that and fall back to the shape.
    return "%s|%s" % ((account or "").strip().lower(), subject_shape(subject))


def ack_identities(kind, message_id=None, sender=None, subject=None, account=None):
    """EVERY key this row could be acknowledged under, not just the preferred one.

    THE BUG THIS EXISTS TO KILL, and it is the worst kind this store can have. `ack_key`
    returns the Message-ID when the row has one and a `row:` identity when it does not -
    both correct. But the answer is computed from row state THAT CHANGES. The moment a
    linking pass gives a row its Message-ID, the key derived for it changes, and every
    acknowledgement stored under the old `row:` key stops matching.

    Reported from a live install: 35 items acknowledged in eleven minutes, a linking pass
    minutes later, and every one of them rendered as unacknowledged again. The acks table
    still held all 35. The API still returned all 35. Only the rendering was wrong, and the
    owner noticed only because the dots changed colour.

    An acknowledgement is the one thing in this store that is unambiguously the OWNER'S OWN
    JUDGMENT rather than the agent's inference. Everything else can be recomputed from the
    mailbox; this cannot. Losing it silently is the highest-cost failure available here.

    Matching on the SET rather than re-deriving one key also means no migration is needed:
    the acks that were orphaned start matching again the moment this ships, with nothing
    guessed and nothing rewritten. Re-keying them would have had to resolve rows that share
    sender, subject and account - and guessing there would trade a visible bug for an
    invisible one.

    The cost, stated because it is real: the `row:` identity is not unique. Two rows with
    the same account, sender and subject share it, so acknowledging one shows both as
    acknowledged. That was already true before linking; this makes it true afterwards too,
    which is the consistent answer rather than a new hazard.
    """
    if kind != "message":
        return (ack_key(kind, message_id, sender, subject, account),)
    row_key = "row:%s|%s|%s" % (
        (account or "").strip().lower(), _sender_key(sender) or "",
        " ".join((subject or "").split()).lower())
    mid = (message_id or "").strip()
    # Preferred identity first: it is what a NEW acknowledgement is stored under.
    return (mid, row_key) if mid else (row_key,)


def acked_message_keys(conn):
    """Every identity under which SOMETHING is acknowledged, expanded from both ends.

    An identity set on the row alone is not enough, because the change runs both ways. A
    row that GAINS a Message-ID is the reported case; a row that LOSES one - re-ingested
    from a source that does not carry them, which is every connector install - is the same
    bug reflected, and the row cannot help there because it no longer knows the Message-ID
    the ack was stored under.

    The acks table does know: it stores account, sender and subject beside every key. So
    each stored ack contributes its key AND the `row:` identity derivable from what it
    recorded, and matching succeeds whichever direction the row moved.
    """
    keys = set()
    for r in conn.execute("SELECT key, account, sender, subject FROM acks "
                          "WHERE kind = 'message'"):
        keys.add(r["key"])
        derived = ack_key("message", None, r["sender"], r["subject"], r["account"])
        # An EMPTY row identity would match every subject-less, sender-less row in the
        # store - one stored ack silencing an unbounded set. Same guard the write uses.
        if derived not in ("row:||", "row:|", ""):
            keys.add(derived)
    return keys


FAMILY_CONCEPT = "family & people"


def ack_covers(row, acked_msg, acked_thread):
    """Is this row covered by an acknowledgement? ONE implementation, three call sites.

    The three places that suppress an acknowledged item - the row annotation, the calendar's
    outstanding count and the open-items panel - each used to spell this out for themselves.
    That is the same two-spellings-of-one-concept trap this file already records for the
    category labels and the ack keys, and here it would drift silently: a carve-out added to
    one site and not the others gives an item that is hidden on one screen and shouting on
    the next.

    A THREAD ACK CANNOT SILENCE A FAMILY EMERGENCY. A thread key is a subject shape, and for
    social-network comment notifications that shape is a CONSTANT - every comment a person
    makes arrives as "<Name> commented on a post". So one ack on ordinary chatter silences
    that person's whole future stream, permanently, on a key that knows nothing about what
    any later message says. A handful of such acks can hold down a long tail of rows from
    people on the always-escalate list, and nothing on screen says so.

    That is correct for chatter and it is what the acknowledgement was for. It is wrong for
    the one case the family rule is written about - a relative in actual trouble - because
    the mechanism cannot tell the two apart, while the person acknowledging reasonably
    believes only the chatter was silenced. So a row that is BOTH family and already judged
    `action-needed` escapes a thread ack. It is deliberately that narrow: family comment
    rows are filed action-needed only on a genuine escalation, so this fires essentially
    never on routine mail and cannot become a new source of noise.

    A MESSAGE ack is untouched, at any importance. That one says "I have seen THIS email",
    which means exactly what it says and infers nothing about mail that has not arrived yet.
    """
    # Callers hand this both plain dicts and sqlite3.Row, and Row has no .get() - so field
    # access goes through one accessor rather than each call site converting (a missed
    # dict(r) would raise only on the path that happens to be exercised).
    def f(name):
        if isinstance(row, dict):
            return row.get(name)
        return row[name] if name in row.keys() else None

    ids = ack_identities("message", f("message_id"), f("sender"), f("subject"), f("account"))
    if any(i in acked_msg for i in ids):
        return True
    kt = ack_key("thread", None, f("sender"), f("subject"), f("account"))
    if kt not in acked_thread:
        return False
    escalated = (f("concept") == FAMILY_CONCEPT and f("importance") == "action-needed")
    return not escalated


def annotate_acks(conn, msgs):
    """Attach the ack keys and current state to each row, so the client never guesses."""
    acked_msg = acked_message_keys(conn)
    acked_thread = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'thread'")}
    for m in msgs:
        ids = ack_identities("message", m.get("message_id"), m.get("sender"),
                             m.get("subject"), m.get("account"))
        kt = ack_key("thread", None, m.get("sender"), m.get("subject"),
                     m.get("account"))
        m["ack_key_message"], m["ack_key_thread"] = ids[0], kt
        m["acked"] = ack_covers(m, acked_msg, acked_thread)
    return msgs


SIGNIN_WINDOW_DAYS = 30

# SEVEN DAYS, chosen against the FETCH WINDOW rather than out of the air. The daily sweep
# fetches two days of mail, so an item stops appearing in runs about three days after it
# arrives whether or not anyone is handling it - a threshold of 3 would fire on every row
# and mean nothing. Seven is four sweeps past that floor: long enough that "the sender is
# still reminding you" has clearly stopped being true.
#
# Deliberately NOT the same line as the 14-day `stale` flag. Stale is about the item's own
# age; this is about whether anything outside this list will raise it again. An item can be
# quiet on day 7 and not stale until day 14, and the quiet one is the one that vanishes.
QUIET_AFTER_DAYS = 7


def api_signins(conn, q):
    """Account-security mail split into what needs a person and what needs a line. (rule 26)

    THE BASELINE IS THE PAST; ONLY THE WINDOW IS JUDGED. Everything older than the window
    teaches the panel what normal looks like and is never reported on. Run without that split
    and every service in the history reads as "first ever seen" the first time anyone opens
    the page - a wall of novelty, delivered once, which would teach the owner on day one that
    this panel cries wolf. That is the failure it exists to prevent, reproduced by its own
    first run.

    What escalates is argued in signin.py. What matters here is the shape of the answer: an
    alert list, a one-line ledger, and a COVERAGE statement, because device novelty is only
    as good as what the provider chose to put in the subject and "no unknown devices" must
    never be readable as "every device was recognised".
    """
    try:
        days = max(1, int((q.get("days") or [str(SIGNIN_WINDOW_DAYS)])[0]))
    except (TypeError, ValueError):
        days = SIGNIN_WINDOW_DAYS
    rows_ = rows(conn.execute(
        "SELECT sender, subject, msg_date, COALESCE(msg_day, run_date) msg_day, account, "
        "message_id, category, concept, importance, disposition FROM messages "
        "WHERE COALESCE(concept,'') = 'account & security'"))
    # DISTINCT MESSAGES, not listings. The same notice is re-listed by every sweep while it
    # sits in the inbox, and counting those would inflate a burst - the one signal here whose
    # false positive is most expensive, because it is the one that says "someone is working
    # through your accounts".
    seen, uniq = set(), []
    for r in sorted(rows_, key=lambda x: str(x["msg_day"] or "")):
        key = (r["message_id"] or "").strip() or "%s|%s|%s" % (
            (r["account"] or "").lower(), _sender_key(r["sender"]) or "",
            " ".join((r["subject"] or "").split()).lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(dict(r))

    last = max((str(r["msg_day"] or "") for r in uniq), default="")
    cutoff = _shift_days(last, -days) if last else ""
    window = [r for r in uniq if str(r["msg_day"] or "") >= cutoff]
    baseline = [r for r in uniq if str(r["msg_day"] or "") < cutoff]

    # A service is FINANCIAL if the store has money-concept mail from it. Derived from the
    # record rather than a list someone has to maintain, for the same reason the guard's
    # needles are: a list nobody updates goes stale in the direction of missing things.
    fin = set()
    for r in conn.execute("SELECT DISTINCT sender FROM messages "
                          "WHERE concept = 'money (bills, receipts, banking)'"):
        svc = signin.service_of(r["sender"])
        if svc:
            fin.add(svc)

    out = signin.ledger(window, financial=fin, baseline=baseline)
    out["window"] = {"days": days, "from": cutoff, "to": last,
                     "judged": len(window), "baseline": len(baseline)}
    return out


def _shift_days(day, delta):
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in str(day)[:10].split("-"))
        return (date(y, m, d) + timedelta(days=delta)).isoformat()
    except (ValueError, TypeError):
        return str(day)[:10]


def annotate_carried(conn, msgs, date):
    """Mark rows that were already surfaced on an EARLIER run, and say when.

    A message still sitting in the inbox is re-listed by every sweep, so an item the owner
    read on Monday was raised again on Tuesday, Wednesday and Thursday with nothing about it
    having changed. Measured on this store: 108 account-security listings covering 68
    distinct messages, and the worst offenders appeared on four separate days each.

    That is how an alert channel dies. Not by being wrong - by being repetitive in a way that
    trains its reader to skip it, so the one alert that matters arrives into a habit of not
    looking. The owner said exactly this, unprompted, before the numbers were measured.

    IDENTITY, THE SAME PROBLEM ACKS HAD. A row is the same item as an earlier one if its
    Message-ID matches; where there is no Message-ID it falls back to the account+sender+
    subject shape. Preferring the Message-ID and falling back only when it is missing is what
    stops a linking pass from silently changing what counts as "already seen" - the defect
    `ack_identities` exists to prevent, met again from a different direction.
    """
    def shape_of(account, sender, subject):
        """account|sender|subject, or None when there is nothing identifying in it.

        The guard is on SENDER AND SUBJECT, not on the whole string. The first version
        rejected only a wholly empty shape - and the account is always present, so
        "owner@example.com||" sailed through and would have matched every subject-less,
        sender-less row in that mailbox: one stale row silencing an unbounded set. The
        account cannot disambiguate anything on its own; it is the other two that identify.
        """
        s = _sender_key(sender) or ""
        subj = " ".join((subject or "").split()).lower()
        if not s and not subj:
            return None
        return "%s|%s|%s" % ((account or "").strip().lower(), s, subj)

    prior_ids, prior_shapes = set(), {}
    try:
        rows_ = conn.execute(
            "SELECT message_id, account, sender, subject, MIN(run_date) first_run "
            "FROM messages WHERE run_date < ? AND disposition IN (%s) "
            "GROUP BY message_id, account, sender, subject"
            % ",".join("?" * len(db.DELIBERATELY_KEPT)),
            (date,) + tuple(sorted(db.DELIBERATELY_KEPT)))
    except Exception:
        return msgs
    for r in rows_:
        mid = (r["message_id"] or "").strip()
        if mid:
            prior_ids.add(mid)
        shape = shape_of(r["account"], r["sender"], r["subject"])
        if shape:
            prior_shapes.setdefault(shape, r["first_run"])
    for m in msgs:
        mid = (m.get("message_id") or "").strip()
        shape = shape_of(m.get("account"), m.get("sender"), m.get("subject"))
        m["carried"] = bool((mid and mid in prior_ids)
                            or (not mid and shape and shape in prior_shapes))
        m["first_surfaced"] = prior_shapes.get(shape) if m["carried"] and shape else None
    return msgs


def api_acks(conn, q):
    """Everything currently acknowledged, so the UI can render state and the routine can
    stop re-surfacing what has already been dealt with."""
    return {"items": rows(conn.execute(
        "SELECT kind, key, account, sender, subject, note, acked_at FROM acks "
        "ORDER BY acked_at DESC"))}


def api_new_hosts(conn, q):
    """What the new-host check found, and what has not been looked at yet.

    The panel this feeds hides itself when `open` is empty, on the same principle as the VA
    panel: something that is always on screen stops being read. `reviewed` is returned too,
    but only so the UI can offer it behind a toggle - it is history, not an alert.

    Ordered so the ones that would cost something come first. A promo blast pointing at a
    new CDN and a bank pointing at a host it has never used are the same event to a scanner
    and very different events to a person.
    """
    show = (q.get("show") or ["open"])[0]
    if show not in ("open", "reviewed", "all"):
        raise ValueError("show must be one of: open, reviewed, all")
    cols = ("sender_key, host, sender, account, subject, profile_messages, weighty, "
            "first_flagged, last_flagged, times_seen, verdict, verdict_note, verdict_by, "
            "verdict_at")
    order = " ORDER BY weighty DESC, profile_messages DESC, last_flagged DESC"
    openr = rows(conn.execute(
        f"SELECT {cols} FROM host_flags WHERE verdict IS NULL{order}"))
    out = {"open": openr, "open_count": len(openr)}
    if show in ("reviewed", "all"):
        out["reviewed"] = rows(conn.execute(
            f"SELECT {cols} FROM host_flags WHERE verdict IS NOT NULL{order}"))
    # An empty `open` from an EMPTY TABLE is not the same claim as an empty `open` from a
    # table full of cleared pairings, and the UI must be able to tell them apart. A check
    # that has never run reporting "nothing to see" is the false all-clear this lane keeps
    # meeting; say how much was ever examined instead of implying a clean bill of health.
    out["ever_flagged"] = conn.execute("SELECT COUNT(*) c FROM host_flags").fetchone()["c"]
    out["profiled_senders"] = conn.execute(
        "SELECT COUNT(*) c FROM sender_profile WHERE messages >= ?",
        (PROFILE_MIN_MESSAGES,)).fetchone()["c"]
    return out


def api_refusals(conn, q):
    """Where the disposal guard OVERRULED the triager, and on what evidence.

    These verdicts already existed - the disposer records every refusal so the stranded scan
    can tell a correct refusal from a failed disposal - but they lived only in a table two
    command-line tools read. So the one place in this system where an automated decision is
    reversed was the one place the page said nothing about.

    That matters in both directions and the second is the interesting one:

      * a refusal is REASSURING. It is the guard doing its job, and seeing it is how the
        person knows the split between proposing and disposing is real rather than decorative.
      * a refusal is also where a STANDING RULE STOPS EXECUTING. When the triager keeps
        proposing what the guard keeps refusing, some rule is claiming to do something it
        cannot do - and nothing anywhere announced that. A rule that quietly never runs is
        indistinguishable from one that runs and finds nothing.

    So this groups and counts, because the shape worth seeing is the REPEAT: one refusal is a
    judgement call, the same refusal every morning is a rule and a guard that disagree
    permanently.

    IT GROUPS BY (sender, category), AND IT USED TO GROUP BY (sender, reason) - WHICH BROKE
    THE ONE THING IT IS FOR. `reasons` is the guard's rendered explanation, and it embeds a
    COUNT: "has 13 kept or surfaced message(s) on record" becomes "has 16 ..." as that sender's
    history grows. Same disagreement, different grouping key - so the row SPLIT every time the
    evidence ticked up, and the persistence measure reset with it. A daily-digest sender refused
    on five separate mornings across two and a half weeks showed up as a three and a one.

    The failure direction is the bad one. A keep count only ever grows, and it grows fastest
    for the senders the guard refuses most - so the panel fragmented hardest exactly where the
    disagreement was most entrenched, reporting a confident smaller number and saying nothing.
    Two open owner questions are argued from this panel, so understating persistence understated
    the evidence for the question being asked.

    (sender, category) is the right key because it is the slice the GUARD reasons about, and the
    same slice `self_feeding` already reads evidence from. Collapsing further, to the sender
    alone, would merge one storefront's SALE refusal with its RECEIPT refusal - two different
    things, and one wrong number traded for another. `reasons` now carries the NEWEST text (the current
    state of the disagreement), `reason_variants` says how many distinct explanations were
    collapsed so the growth stays visible rather than absorbed, and `runs` counts distinct run
    dates - a truer reading of "every morning" than a message count that rises and falls with
    how many items happened to arrive that day.

    `days` bounds the window; `evidence_from` is passed straight through, so a refusal resting
    on keeps that predate a later ruling is visible as such rather than having to be inferred.

    `evidence_to` IS THE OTHER END, AND IT IS THE ONE THAT ANSWERS A DIFFERENT QUESTION.
    `evidence_from` is the OLDEST keep behind a refusal, which reads as "this rests on old
    evidence" - reassuringly stale. The newest keep is the one that decides whether the rule
    can EVER fire. When it lands on the same run that proposed the bin, the run has just
    manufactured the counter-evidence for its own proposal: the sender is summary-tier, so
    every sweep keeps today's issue minutes before proposing last week's, and the keep count
    the guard reasons about can never fall. That is `self_feeding`, and it is a different
    finding from `days_running` - a repeat says a rule and a guard disagree, this says the
    disagreement is structural and no amount of waiting resolves it.

    It is computed at READ time from the messages table rather than stored, so it is true for
    refusals already on record instead of only for ones recorded from today onward. Nothing
    here changes a verdict; the guard keeps the right to overrule the triager.
    """
    try:
        days = max(1, min(365, int((q.get("days") or ["30"])[0])))
    except ValueError:
        raise ValueError("days must be a number")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    items = rows(conn.execute(
        "SELECT sender, category, COUNT(*) AS messages, "
        "       COUNT(DISTINCT run_date) AS runs, "
        "       COUNT(DISTINCT reasons) AS reason_variants, "
        "       MIN(run_date) AS first_run, MAX(run_date) AS last_run, "
        "       MIN(evidence_from) AS evidence_from "
        "FROM disposal_refusals WHERE run_date >= ? "
        "GROUP BY sender, category ORDER BY messages DESC, last_run DESC", (cutoff,)))
    for it in items:
        # THE NEWEST reasons text, fetched per group rather than grouped on. See the note
        # above: grouping ON it is what split the row. `IS` (not `=`) so a refusal recorded
        # with a null sender matches its own group instead of silently matching nothing.
        newest_why = conn.execute(
            "SELECT reasons FROM disposal_refusals "
            "WHERE sender IS ? AND category IS ? AND run_date >= ? "
            "ORDER BY run_date DESC, refused_at DESC LIMIT 1",
            (it.get("sender"), it.get("category"), cutoff)).fetchone()
        it["reasons"] = [r for r in ((newest_why["reasons"] if newest_why else "") or "")
                         .split("\n") if r.strip()]
        it["days_running"] = _days_between(it.get("first_run"), it.get("last_run"))
        newest = conn.execute(
            "SELECT MAX(run_date) AS d FROM messages "
            "WHERE sender = ? AND category = ? AND disposition IN ('kept', 'surfaced')",
            (it.get("sender"), it.get("category"))).fetchone()
        it["evidence_to"] = newest["d"] if newest else None
        # THE GUARD'S EVIDENCE IS LABEL-SCOPED, AND THE WORDING DOES NOT SAY SO (2026-09-05).
        # Its refusal reads "this sender under 'X' has N kept or surfaced message(s) on
        # record", and both the count above and the guard's own query are filtered to the
        # category being proposed. So N is not "how much this sender was kept" - it is "how
        # much this sender was kept UNDER THIS LABEL", which is a materially weaker claim
        # than a reader takes from it.
        #
        # Measured, which is why this is here: Q43 recorded TWO bad keeps for one Facebook
        # sender (08-25, 08-26), and today's refusal on that same sender cited ONE. Both rows
        # are in the store. They differ only because the 08-25 keep is filed `fb-tier-c` and
        # the 08-26 keep is filed `social`. That makes Q43 (keeps blocking a rule) and Q44 (a
        # label silently deciding whether mail gets binned) the same defect from two sides.
        #
        # So report BOTH numbers and let the gap be visible. This changes no verdict and no
        # schema - it is derived at read time, like the rest of this panel - and it
        # deliberately does not "fix" the scoping, because widening what the guard counts
        # would change what may be binned, which is the owner's call (Q43/Q44).
        in_cat = conn.execute(
            "SELECT COUNT(*) c FROM messages "
            "WHERE sender = ? AND category IS ? AND disposition IN ('kept', 'surfaced')",
            (it.get("sender"), it.get("category"))).fetchone()["c"]
        other = rows(conn.execute(
            "SELECT category, COUNT(*) AS c FROM messages "
            "WHERE sender = ? AND category IS NOT ? AND disposition IN ('kept', 'surfaced') "
            "GROUP BY category ORDER BY c DESC",
            (it.get("sender"), it.get("category"))))
        it["keeps_in_category"] = in_cat
        it["keeps_other_labels"] = sum(r["c"] for r in other)
        it["other_labels"] = [r["category"] for r in other if r["category"]]
        # Only a refusal that actually RESTS on keeps can be self-feeding. A protected-list
        # refusal records no evidence date and must not be labelled with someone else's.
        it["self_feeding"] = bool(
            it.get("evidence_from") and it["evidence_to"] and it.get("last_run")
            and it["evidence_to"] >= it["last_run"])
    # An empty list from a store that has never recorded a refusal is a different claim from
    # an empty list because the guard agreed with everything lately. Say which.
    ever = conn.execute("SELECT COUNT(*) c FROM disposal_refusals").fetchone()["c"]
    return {"items": items, "count": len(items), "days": days, "ever_recorded": ever,
            "messages": sum(i["messages"] for i in items),
            "rate": _refusal_rate(conn, cutoff, days)}


def _refusal_rate(conn, cutoff, days):
    """Is the pile still filling? Refusals PER RUN over the window - and it can fall.

    Everything else on this panel describes the state: how many messages, how long each
    disagreement has run. None of it says which direction things are moving - and the
    direction is what a reader actually wants, because a pile that is filling faster and a
    pile that is filling slower look identical in a count.

    A RATE, NOT A TOTAL, and the reason is structural. `disposal_refusals` is append-only, so
    a cumulative count rises forever - it would report "growing" on the morning after the
    rules were fixed, which makes it a number that reads as evidence and can never be
    evidence. Refusals per run goes to zero the moment the guard and the rules stop
    disagreeing, while the total sits exactly where it is.

    THE DENOMINATOR COMES FROM `runs`, NOT FROM THIS TABLE, and that is the entire subtlety.
    A run that refuses nothing writes no refusal row. Counting the run dates present in
    `disposal_refusals` divides by the number of mornings that went badly, which cannot
    produce a rate below 1.0 however many clean mornings pass - blind in the reassuring
    direction, exactly like every other unstated scope this lane has met. A quiet morning is
    still a morning and has to be in the denominator.

    AND IT HAS A THIRD STATE. Under two runs in the window there is no trend to draw, and a
    two-state answer would pick the one that lets the page render: a confident "0.0 per run"
    over a measurement that never happened. Returns None, and the panel then says nothing.
    """
    run_dates = [r["run_date"] for r in conn.execute(
        "SELECT DISTINCT run_date FROM runs WHERE run_date >= ? ORDER BY run_date", (cutoff,))]
    if len(run_dates) < 2:
        return None
    per_day = {r["run_date"]: r["c"] for r in conn.execute(
        "SELECT run_date, COUNT(*) AS c FROM disposal_refusals "
        "WHERE run_date >= ? GROUP BY run_date", (cutoff,))}
    # Split the window in half by MORNINGS, not by calendar days - the runs are not evenly
    # spaced (reboots, skipped catch-up dispatches), so halving the dates would hand the two
    # sides different numbers of observations without saying so.
    half = len(run_dates) // 2
    prior_dates, recent_dates = run_dates[:half], run_dates[half:]

    def block(dates):
        n = sum(per_day.get(d, 0) for d in dates)
        return {"runs": len(dates), "refusals": n,
                "per_run": round(n / len(dates), 3) if dates else None}

    total = sum(per_day.get(d, 0) for d in run_dates)
    return {"days": days, "runs": len(run_dates), "refusals": total,
            "per_run": round(total / len(run_dates), 3),
            "recent": block(recent_dates), "prior": block(prior_dates),
            "by_run": [{"run_date": d, "refusals": per_day.get(d, 0)} for d in run_dates]}


def api_thin_evidence(conn, q):
    """Bins that rest on almost no precedent - the OTHER half of the refusals panel.

    The refusals panel shows where the guard overruled the sort, and it is reassuring by
    construction: every row in it is mail that was NOT deleted. This asks the question with
    the uncomfortable answer. Of the mail that WAS binned, which bins stood on the least
    prior evidence - and therefore, if any of today's calls was wrong, which ones are it?

    `apply_proposal` already computes this. It prints, under every clearance, how many of
    them rest on `<=2 prior binned, none kept`, and the comment above that code says exactly
    why it is printed rather than enforced: a minimum-evidence floor was measured against the
    store and argued against, because thin slices self-select for noise (of 138, not one was
    money, security, family or medical). The design conclusion was "do not threshold it,
    SHOW it. If this line ever names something that is not noise, that is the evidence for a
    floor - and it will be evidence rather than a hunch."

    It was shown to a console that closes with the session. So the evidence that decides an
    open design question was being generated every morning and read by nobody - the same
    shape as the refusals table before it got this treatment, and the same shape as the
    retention count before that. This is the third time in this lane that the honest number
    existed and had nowhere to live.

    WHAT `prior` COUNTS, because the CLI's word for it is wrong and copying the wrong number
    would be worse than not showing one. The disposer builds its history AFTER the run has
    been ingested (the ordering is deliberate - see ROUTINE step 3), so a slice's count there
    includes the run's OWN row: a sender binned for the very first time prints as "1 prior".
    That is fine for a threshold, which only needs an ordering, and misleading on a page,
    where a person reads "prior" as "before today". So this counts strictly `run_date <` the
    run being reported, and a first-ever bin reads 0. The two numbers therefore differ by
    one, on purpose, and `counts` says which convention is in force.

    `kept` is carried beside it because the pair is the real claim. The guard already refuses
    any slice with kept mail on record, so a thin row is not "the guard was unsure" - it is
    "the guard had almost nothing to reason FROM." Those are different, and only the second
    is worth a person's eye.
    """
    date = (q.get("date") or [""])[0].strip()
    if not date:
        row = conn.execute("SELECT MAX(run_date) d FROM messages").fetchone()
        date = (row["d"] if row else "") or ""
    try:
        floor = int((q.get("floor") or ["2"])[0])
    except ValueError:
        floor = 2

    # Only mail that actually MOVED. A `would_trash` row is a proposal the guard refused or
    # a read-only pass's verdict; neither is a deletion, and listing them here would put
    # mail that is still in the inbox under a heading about mail that is not.
    binned = conn.execute(
        "SELECT sender, subject, category, account, reason, message_id "
        "FROM messages WHERE run_date = ? AND disposition = 'trashed' "
        "AND sender IS NOT NULL AND sender != ''", (date,)).fetchall()

    # One pass over the prior history, bucketed the way the GUARD buckets it: by
    # (sender key, category). Keying on the raw sender string instead would split a sender
    # that reached the store under two spellings and hand back a thinner number than the
    # truth - which, for a panel whose whole job is to find thin evidence, would manufacture
    # its own findings.
    hist = {}
    for r in conn.execute(
            "SELECT sender, disposition, COALESCE(category,'') category "
            "FROM messages WHERE run_date < ? AND sender IS NOT NULL AND sender != ''",
            (date,)):
        key = _sender_key(r["sender"])
        if not key:
            continue
        h = hist.setdefault((key, r["category"]), {"trashed": 0, "kept": 0})
        if r["disposition"] in db.DISPOSABLE:
            h["trashed"] += 1
        elif r["disposition"] in db.DELIBERATELY_KEPT:
            h["kept"] += 1

    items = []
    for m in binned:
        key = _sender_key(m["sender"]) or ""
        h = hist.get((key, m["category"] or ""), {"trashed": 0, "kept": 0})
        if h["trashed"] > floor:
            continue
        items.append({
            "sender": m["sender"], "subject": m["subject"],
            "category": m["category"], "account": m["account"],
            "reason": m["reason"], "message_id": m["message_id"],
            "prior": h["trashed"], "kept": h["kept"],
            # A first-ever bin is the sharpest case and deserves its own word rather than
            # being read off a zero. This is the row where a standing rule did not decide
            # anything - the triager did, alone, this morning.
            "first_ever": h["trashed"] == 0,
        })
    items.sort(key=lambda i: (i["prior"], i["sender"] or ""))

    return {"items": items, "count": len(items), "run_date": date,
            "binned": len(binned), "floor": floor,
            "counts": "prior bins strictly before this run (a first-ever bin reads 0)"}


def api_retention_shelf(conn, q):
    """Mail that is past its rule-15 shelf life - the whole-mailbox answer, on the page.

    THE OTHER HALF OF THE REFUSALS PANEL. That panel shows where the disposal guard overruled
    the triager. This shows the pile that disagreement leaves behind: summary-tier mail whose
    shelf has expired, which a standing rule says to retire and which is still sitting in an
    inbox. Seeing one without the other gives you the argument and not the cost.

    It exists because the number had nowhere to live. `tools/retention_scan.py` walks all
    eight mailboxes every morning - the only thing here that does, since the daily --days 2
    fetch cannot by definition see mail old enough to retire - and printed its answer to a
    console that closed with the session. An open owner question (Q41) is argued from this
    count, and the count was drifting up unwatched.

    `complete` is the field that matters most and it is why the reach is stored WITH the
    answer rather than recomputed beside it. A scan that lost a box to a throttled account, or
    stopped at its own time budget, returns FEWER overdue items - so an incomplete walk always
    looks like better news than a full one, and it looks that way at the exact moment it
    deserves least trust. A count served without its reach is this lane's oldest bug.

    `ever_scanned` keeps the second distinction an empty list destroys: nobody has asked, versus
    asked and found nothing. They render identically as [] and mean opposite things.
    """
    return db.retention_shelf(conn)


def _days_between(a, b):
    """Whole days between two ISO dates, or None if either is missing/unparseable."""
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:
        return None


def api_host_review(conn, q, body=None):
    """Rule on a (sender, host) pairing. POST only.

    Reversible on purpose: pass `verdict: null` to put it back in the open list. A verdict
    is a statement about attention and judgment, and a wrong one has to be undoable - the
    same reasoning as un-acknowledging.
    """
    body = body or {}
    key = (body.get("sender_key") or "").strip()
    host = (body.get("host") or "").strip()
    if not key or not host:
        return {"ok": False, "error": "sender_key and host are both required"}
    verdict = body.get("verdict")
    if verdict is not None:
        verdict = str(verdict).strip().lower()
        if verdict not in ("cleared", "suspicious"):
            return {"ok": False, "error": "verdict must be 'cleared', 'suspicious', or null"}
    exists = conn.execute(
        "SELECT 1 FROM host_flags WHERE sender_key = ? AND host = ?", (key, host)).fetchone()
    if not exists:
        return {"ok": False, "error": "no such flagged pairing"}
    conn.execute(
        "UPDATE host_flags SET verdict = ?, verdict_note = ?, verdict_by = ?, verdict_at = ? "
        "WHERE sender_key = ? AND host = ?",
        (verdict, (body.get("note") or "").strip() or None,
         (body.get("by") or "owner").strip(),
         db.now_iso() if verdict else None, key, host))
    conn.commit()
    return {"ok": True, "sender_key": key, "host": host, "verdict": verdict}


def record_ack(conn, kind="message", message_id=None, sender=None, subject=None,
               account=None, note=None, on=True):
    """Record (or lift) an acknowledgement. THE one implementation, reachable without a UI.

    `INSERT INTO acks` used to appear in exactly one place - the HTTP handler below - so an
    acknowledgement could only be made by clicking in a browser. That is fine for a person at
    a screen and wrong for the operating model this plugin prescribes, where the thing
    maintaining the board day to day is a scheduled task with no UI and no session.

    The gap is not cosmetic. An item can be dealt with OFF-CHANNEL - answered in a call,
    decided in a meeting, delegated verbally - while the mail thread shows nothing, and a
    routine with no way to record that re-escalates it every single run. So a parallel
    markdown ledger gets invented, and then two stores answer "has the owner dealt with this?"
    - the sweep reading one, the dashboard reading the other, both behaving correctly, and
    disagreeing. A clean result from a broken instrument, arrived at from a new direction.

    The divergence runs the wrong way, too: off-channel resolutions are the single most
    valuable thing a human can tell a mail tool, because it can never infer them - and they
    were exactly the ones that could only be recorded in the store the dashboard ignores.

    The table, the key derivation and the annotation path all already existed. Only the door
    was missing.
    """
    kind = (kind or "message").strip()
    if kind not in ("message", "thread"):
        return {"ok": False, "error": "kind must be 'message' or 'thread'"}
    key = ack_key(kind, message_id, sender, subject, account)
    # An empty SHAPE is the dangerous case, not an empty key. "me@example.com|" would be a
    # perfectly well-formed thread key that matches every subject-less message in that
    # mailbox - one call silencing an unbounded set.
    if not key or key in ("|", "row:||") or (kind == "thread" and key.endswith("|")):
        return {"ok": False, "error": "nothing identifiable to acknowledge"}
    if on is False:
        # LIFTED BY EVERY IDENTITY, not just the preferred one - the mirror of the bug that
        # `ack_identities` exists to fix, and the more infuriating half. Deleting only the
        # Message-ID key would leave a legacy `row:` ack in place, so the row would still
        # render acknowledged: the owner clicks to undo, the API answers ok, and nothing
        # changes. A write that reports success and does nothing is worse than one that
        # fails, because there is no second attempt.
        ids = ack_identities(kind, message_id, sender, subject, account)
        cur = conn.execute(
            "DELETE FROM acks WHERE kind = ? AND key IN (%s)" % ",".join("?" * len(ids)),
            (kind,) + tuple(ids))
        conn.commit()
        return {"ok": True, "kind": kind, "key": key, "acked": False,
                "lifted": cur.rowcount}
    conn.execute(
        "INSERT INTO acks (kind, key, account, sender, subject, note, acked_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
        "note = excluded.note, acked_at = excluded.acked_at",
        (kind, key, account, sender, subject, (note or "").strip()[:400], db.now_iso()))
    conn.commit()
    return {"ok": True, "kind": kind, "key": key, "acked": True}


def api_ack(conn, q, body=None):
    """Acknowledge (or un-acknowledge) an item. POST only.

    `on: false` lifts it - an acknowledgement is a statement about attention, not a
    deletion, and a mistaken one has to be reversible.

    A thin wrapper over `record_ack`, deliberately. The first attempt at giving acks a
    headless door copied this body into the new function, which would have produced two
    implementations of the ack key derivation and the lift semantics - and every serious
    defect in this project so far has been one concept spelled two ways in two places.
    """
    body = body or {}
    return record_ack(conn, kind=body.get("kind") or "message",
                      message_id=body.get("message_id"), sender=body.get("sender"),
                      subject=body.get("subject"), account=body.get("account"),
                      note=body.get("note"), on=body.get("on") is not False)


# ------------------------------------------------------------- workflow actions
#
# Some mail carries a link that costs you something you cannot get back if you miss it: a
# questionnaire that must be completed before an appointment, or a video visit whose join
# link arrives ~30 minutes beforehand. Rule 17 already makes these top-priority in the
# report, but a report is a thing you read later - these need a surface that puts the
# actual link in front of you, and a push that does not wait.
#
# THE LINKS HERE ARE CLICKABLE, which is a deliberate exception to the viewer's rule that
# nothing is ever navigable. It is narrow and every condition must hold:
#   * the sender is a configured workflow address,
#   * DKIM passes for the configured domain (so the sender is not merely claiming to be
#     that organisation), and
#   * the link's own host is inside that domain.
# A message failing any of those is still shown - it just is not linkified, and it says so.
# Impersonating a clinic or a benefits office is a common and effective attack, so "it
# looked right" is not good enough to hand someone a live link to click.
# WHICH senders carry a workflow is configuration - one person's clinic is another
# person's school portal or payroll system. The mechanism (verify the sender, verify the
# destination, surface it, push it) is what generalises; the addresses never do.
def workflow_config():
    prot = load_protected()
    dom = prot["link_domain"]
    ok = (re.compile(r"^https://([a-z0-9.\-]+\.)?%s(/|$|\?)" % re.escape(dom), re.I)
          if dom else None)
    return prot["workflow_senders"], ok, dom
_ANCHOR = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
# The action link, not the boilerplate. Institutional mail also carries app-store links,
# profile links and terms - offering those beside a questionnaire link loses the real one.
_ACTION_TEXT = re.compile(
    r"start|begin|questionnaire|join|launch|connect|check.?in|complete|test your", re.I)
_WHEN = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s*(\d{1,2}:\d{2})?\s*([A-Z]{2,4})?|"
    r"((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+\w+\s+\d{1,2},?\s+\d{4})", re.I)


def _workflow_extract(raw, link_ok=None, domain=""):
    """Pull the actionable link and the appointment time out of one workflow message.

    This is the SERVER path: `raw` is the full RFC822 message, headers included, so the
    sender's signature is checkable and a link can earn the right to be clickable. The
    store path (`_workflow_extract_stored`) shares every line of the parsing below and
    differs only in what it can prove about the sender - which is nothing.
    """
    import email as _email
    from email import policy as _policy
    msg = _email.message_from_bytes(raw, policy=_policy.default)
    auth = str(msg.get("Authentication-Results") or "")
    frm = str(msg.get("From") or "")
    subject = " ".join(str(msg.get("Subject") or "").split())
    dkim_domain_ok = bool(domain and re.search(
        r"dkim=pass[^;]*header\.i=@[a-z0-9.\-]*" + re.escape(domain), auth, re.I))
    html, text = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            body = part.get_content()
        except Exception:
            continue
        if part.get_content_type() == "text/html" and html is None:
            html = body
        elif part.get_content_type() == "text/plain" and text is None:
            text = body
    return _workflow_parse(html, text, subject, frm, dkim_domain_ok, link_ok)


def _workflow_extract_stored(body_text, sender, subject, link_ok=None):
    """The same extraction, from the copy of the body the STORE already holds.

    Why this exists: the endpoint used to have exactly one source - a per-message IMAP
    fetch in its own subprocess - so a mailbox that could not answer made the panel silent
    about mail whose full text was already sitting in the local database. A throttled
    mailbox can spend more on CONNECT and SELECT alone than the whole request is allowed,
    and when that happens no share of the budget can succeed, however fairly it is divided:
    the floor is above the ceiling. Fair-sharing a budget only helps once a share can buy
    at least one operation. When it cannot, the answer is to stop needing the network.

    THE STORE CANNOT VOUCH FOR A SENDER, and that is the whole reason this is a separate
    function rather than a cheaper first argument. `messages` keeps the body and not the
    headers, so there is no Authentication-Results to read and DKIM is not merely failing -
    it is unaskable. `dkim_domain_ok` is therefore hard-wired False, which fails CLOSED:
    the item is surfaced, and its link is rendered as text with the reason said out loud.
    A stored body is enough to tell someone an action is waiting; it is never enough to
    hand them a live link to click, and the two must not be allowed to share one outcome.
    """
    body = body_text or ""
    looks_html = "<a " in body.lower() or "<html" in body.lower()
    return _workflow_parse(body if looks_html else None, body,
                           " ".join((subject or "").split()), sender or "",
                           False, link_ok)


def _workflow_parse(html, text, subject, frm, dkim_domain_ok, link_ok=None):
    """Links, primary action and appointment time - the half that does not care where the
    body came from. Kept in ONE place on purpose: a second spelling of this parsing is a
    second thing to get wrong, and the two sources must not disagree about what a message
    says merely because they disagree about who can prove it was sent."""
    links = []
    for url, label in _ANCHOR.findall(html or ""):
        label = " ".join(re.sub(r"<[^>]+>", " ", label).split())[:70]
        if not url.lower().startswith("http"):
            continue
        links.append({"url": url, "label": label,
                      "host_in_domain": bool(link_ok and link_ok.match(url)),
                      "action": bool(_ACTION_TEXT.search(label))})
    # plain-text fallback, for messages that carry the link as bare text
    if not links:
        for url in re.findall(r"https?://[^\s<>\"')\]]+", text or ""):
            links.append({"url": url, "label": "", "host_in_domain": bool(link_ok and link_ok.match(url)),
                          "action": False})

    primary = next((l for l in links if l["action"] and l["host_in_domain"]), None) \
        or next((l for l in links if l["host_in_domain"]), None)
    when = None
    m = _WHEN.search(subject) or _WHEN.search(
        " ".join(re.sub(r"<[^>]+>", " ", html or text or "").split())[:900])
    if m:
        when = " ".join(x for x in m.groups() if x)
    return {"from": frm, "subject": subject, "dkim_domain_ok": dkim_domain_ok, "auth_ok": dkim_domain_ok,
            "links": links, "primary": primary, "when": when}


_MDY = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
_LONG = re.compile(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def _days_since(run_date):
    """Whole days from a YYYY-MM-DD run date to today, or None if it will not parse."""
    from datetime import datetime, date
    try:
        d = datetime.strptime(str(run_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (date.today() - d).days


def _workflow_when_state(when, horizon_days):
    """Turn the appointment text into a decision: is this an ACTION yet?

    A visit 41 days out is calendar knowledge, not something to do - and putting it in a
    panel titled "needs you to do something" is how that panel stops being believed. The
    horizon matches a typical appointment cadence: a 14-day reminder, then the join link on
    the day, so inside 14 days is when it becomes actionable.

    An item with NO date (a screening questionnaire) is actionable immediately - there
    is nothing to wait for.
    """
    from datetime import datetime, date
    if not when:
        return {"state": "now", "days_until": None}
    d = None
    m = _MDY.search(when)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = date(yr, mo, da)
        except ValueError:
            d = None
    if d is None:
        m = _LONG.search(when)
        if m and m.group(1).lower() in _MONTHS:
            try:
                d = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                d = None
    if d is None:
        return {"state": "now", "days_until": None}
    delta = (d - datetime.now().date()).days
    if delta < 0:
        state = "past"
    elif delta == 0:
        state = "today"
    elif delta <= horizon_days:
        state = "soon"
    else:
        state = "upcoming"          # real, dated, and deliberately not shouting yet
    return {"state": state, "days_until": delta, "when_date": d.isoformat()}


def api_workflow_actions(conn, q):
    """Items that want you to DO something, with the link to do it.

    Deliberately scoped to the senders configured as carrying a workflow. Newsletters,
    surveys and bulletins from the same organisation are not actions, and including them
    would bury the ones that are.
    """
    try:
        days = max(1, min(400, int((q.get("days") or ["120"])[0])))
    except ValueError:
        days = 120
    # How close an appointment has to be before it counts as something to DO. 14 days
    # mirrors a common reminder cadence, and matches how people actually work: a visit
    # weeks out is not something you need shown to you until it is closer.
    try:
        horizon = max(0, min(365, int((q.get("horizon") or ["14"])[0])))
    except ValueError:
        horizon = 14
    senders, link_ok, domain = workflow_config()
    if not senders:
        # No workflow senders configured is a legitimate state (most people have none),
        # and it is reported as such rather than as an empty result that looks like a scan.
        return {"items": [], "days": days, "horizon": 14, "errors": [], "candidates": 0,
                "outstanding": 0, "upcoming": 0, "configured": False}
    rows_ = rows(conn.execute(
        "SELECT run_date, account, sender, subject, message_id, disposition FROM messages "
        "WHERE message_id IS NOT NULL AND message_id != '' ORDER BY run_date DESC"))
    acked_msg = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind='message'")}

    # WHOLE-REQUEST BUDGET, from a measured failure. Each message is read from the server in its
    # own subprocess with its own 45s timeout, and there are up to 25 of them - so the worst case
    # was ~19 minutes and nothing bounded the request as a whole. When a slow account made this
    # endpoint take minutes rather than seconds, the browser's fetch simply hung and the panel
    # rendered NOTHING - not even the reach block that exists precisely to say "I could not read
    # these." A per-item timeout is not a budget: ask what the worst case of the LOOP is, never of
    # the step. The honesty machinery below is worthless if the response never arrives, which is
    # the same last-layer failure the reach block was built for - a guarantee is only as strong as
    # its last layer. The budget is wall-clock across the whole request; whatever it does not
    # reach is reported as unread, never silently dropped.
    try:
        budget = max(5, min(600, int((q.get("budget") or ["25"])[0])))
    except ValueError:
        budget = 25
    deadline = time.monotonic() + budget

    # FAIR SHARE, from a measured failure. The budget above bounds the request, but it said
    # nothing about how the request is SHARED - each read was handed `min(45, left)`, i.e.
    # everything still on the clock. So the FIRST message could spend the lot: against a slow
    # mailbox one read consumed essentially the entire budget and every candidate behind it was
    # reported as "the budget ran out", leaving the endpoint to answer `outstanding: 0` having
    # read none of them.
    # That is this file's own lesson one layer in - bounding the whole does not bound a part's
    # claim on the whole - and it is worse than the hang it replaced in one specific way: the
    # response ARRIVES, so it looks like an answer. A candidate that cannot be read must cost
    # one slot, not all of them. The floor keeps a merely-slow-but-workable mailbox readable
    # instead of manufacturing timeouts that the two-sided control above exists to forbid.
    # FAIR ACROSS ITEMS IS NOT FAIR ACROSS ACCOUNTS - the same lesson one layer further out.
    # The share above divides the budget by the number of CANDIDATES, but what actually fails is
    # an ACCOUNT, and an account holding N candidates therefore collects N shares. When every
    # candidate lives in one throttled mailbox, "fair share" hands that mailbox 100% of the
    # budget anyway: it spends the lot issuing doomed reads, reads nothing, and emits one error
    # line per candidate - which reads as many problems when the truth is one.
    # A throttled box can cost more per read than the whole budget, and more than the
    # 45s per-item cap - so no share of any size could have succeeded.
    # So: consecutive timeouts on the SAME account condemn that account for the rest of the
    # request. Its remaining candidates are reported unread with the cause named once, and the
    # budget they would have burned is left for accounts that can still answer.
    # TWO, not one, and the existing control is why. A single slow message is NOT evidence that
    # its mailbox is bad - test_workflow_budget leg 3 holds exactly that case (one 30s message
    # in front of five instant ones) and condemning on the first timeout would have starved five
    # readable messages to punish one. Two in a row is the weakest evidence that actually
    # distinguishes "this message is slow" from "this mailbox is slow", and it still bounds the
    # waste at two shares instead of one per candidate.
    MIN_READ_S = 3.0
    STALL_AFTER = 2
    misses = {}           # account -> consecutive timeouts so far
    stalled = {}          # account -> the share its reads were given before it was condemned
    read_ok = 0
    cands, seen = [], set()
    for r in rows_:
        addr = (email_utils.parseaddr(r["sender"])[1] or "").lower()
        kind = senders.get(addr)
        if not kind or r["message_id"] in seen:
            continue
        seen.add(r["message_id"])
        cands.append((r, kind))
        if len(cands) >= 25:
            break

    # One query for every candidate's stored body, newest run first. `body_text` is carried on
    # ingest precisely so a message can be read without going back to the network - this
    # endpoint simply never asked. Rows written before that field existed carry no body and
    # fall through to the server path unchanged.
    stored_bodies = {}
    if cands:
        ids = [r["message_id"] for r, _ in cands]
        qmarks = ",".join("?" * len(ids))
        for row_ in conn.execute(
                "SELECT message_id, body_text FROM messages WHERE message_id IN (%s) "
                "AND body_text IS NOT NULL AND body_text != '' "
                "ORDER BY run_date ASC" % qmarks, ids):
            stored_bodies[row_["message_id"]] = row_["body_text"]
    read_from_store = 0

    items, errors = [], []
    tool = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
    for idx, (r, kind) in enumerate(cands):
        # STORE FIRST. The body we already have is the same body the server would send
        # back, and reading it costs no socket, no subprocess and no share of the budget -
        # so a mailbox that cannot answer can no longer make this panel silent about mail
        # whose text is sitting in the local database. What the store CANNOT supply is the
        # sender's signature (it keeps bodies, not headers), so a stored item is surfaced
        # with its link deliberately not clickable and the reason said out loud. Surfacing
        # and vouching are two different claims; this buys the first and not the second.
        info = None
        source = "server"
        stored = stored_bodies.get(r["message_id"])
        if stored:
            info = _workflow_extract_stored(stored, r["sender"], r["subject"], link_ok)
            source = "store"
            read_from_store += 1
        if info is None:
            if r["account"] in stalled:
                # Named once, as ONE cause. Paying a share to re-learn that a stalled mailbox is
                # still stalled buys nothing and spends budget belonging to the boxes that answer.
                errors.append({"subject": r["subject"], "account": r["account"],
                               "why": "not read - %s had already failed to answer within its %.0fs "
                                      "share, so this was not attempted" % (r["account"],
                                                                            stalled[r["account"]])})
                continue
            left = deadline - time.monotonic()
            if left < MIN_READ_S:
                # A share below the cost of one read purchases NOTHING. The old code clamped the
                # share with `left` and issued the read anyway - a 1s attempt that could not
                # possibly finish, reported as "the mailbox did not answer within its 1s share".
                # That sentence blames the mailbox for a budget this code never gave it, which is
                # a refusal and a failure sharing one outcome. Say which one it was.
                errors.append({"subject": r["subject"], "account": r["account"],
                               "why": "not read - the %ds budget was exhausted after %d completed "
                                      "read(s); this was not attempted" % (budget, read_ok)})
                continue
            # Recomputed every iteration, so time the fast reads gave back is redistributed to
            # whatever is still queued rather than being lost with the item that saved it.
            share = max(MIN_READ_S, left / max(1, len(cands) - idx))
            tmp = os.path.join(tempfile.gettempdir(),
                               "va_%s.eml" % abs(hash(r["message_id"])))
            try:
                p = subprocess.run(
                    [sys.executable, tool, "find", "--account", r["account"],
                     "--message-id", r["message_id"], "--out", tmp],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=min(45, max(MIN_READ_S, min(left, share))), creationflags=_NO_WINDOW)
                # THE SUBPROCESS CAME BACK, so the mailbox answered - whatever its verdict. Clearing
                # the streak here rather than on a successful extract is the difference between
                # "this account is unreachable" and "this message is not there", which are two
                # different truths that must not share one outcome. Resetting only on success let a
                # perfectly responsive box be condemned by a run of not-on-the-server answers.
                misses[r["account"]] = 0
                if p.returncode != 0 or not os.path.exists(tmp):
                    errors.append({"subject": r["subject"], "account": r["account"],
                                   "why": "not on the server"})
                    continue
                info = _workflow_extract(open(tmp, "rb").read(), link_ok, domain)
            except subprocess.TimeoutExpired:
                # Name the MAILBOX, not just the message. When a box goes slow every one of its
                # messages fails identically, and a reach block listing eleven subjects with no
                # common cause reads as eleven problems instead of one unreachable account - which
                # is the thing the reader has to know to judge what the panel's silence is worth.
                given = min(45, max(MIN_READ_S, min(left, share)))
                misses[r["account"]] = misses.get(r["account"], 0) + 1
                if misses[r["account"]] >= STALL_AFTER:
                    stalled[r["account"]] = given
                errors.append({"subject": r["subject"], "account": r["account"],
                               "why": "not read - %s did not answer within its %.0fs share of the "
                                      "%ds budget" % (r["account"], given, budget)})
                continue
            except Exception as e:
                # NEVER swallow this silently. The first version had a bare `continue` here and
                # a missing module-level import made EVERY message raise - so the panel showed
                # a clean, confident "0 actions" while the real answer was that it had not
                # managed to read a single one. A panel about time-critical mail must not be
                # able to report an all-clear it did not earn.
                errors.append({"subject": r["subject"], "account": r["account"],
                               "why": "%s: %s" % (type(e).__name__, e)})
                continue
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        read_ok += 1
        ws = _workflow_when_state(info["when"], horizon)
        items.append({
            "kind": kind[0], "kind_label": kind[1],
            "run_date": r["run_date"], "account": r["account"],
            "sender": r["sender"], "subject": info["subject"],
            "message_id": r["message_id"],
            "when": info["when"], "auth_ok": info["auth_ok"], "source": source,
            # WHY a link is not clickable, not merely THAT it is not. "sender could not be
            # verified" is true of a failed signature and of a body read from the store, and
            # those are different facts - one says the check ran and did not pass, the other
            # says the check was never askable. A reader deciding whether to trust a
            # health-care link deserves to be told which.
            "auth_note": ("read from the local store, which keeps the body and not the "
                          "headers - the sender signature cannot be checked offline"
                          if source == "store" else None),
            "state": ws["state"], "days_until": ws["days_until"],
            "when_date": ws.get("when_date"),
            # HOW LONG IT HAS BEEN SITTING HERE, for the items that have no date of their own.
            # A dateless item is actionable immediately - that is right, and it is also why
            # nothing ever ages one out. So a one-time token or a message notice stays on this
            # panel indefinitely and renders IDENTICALLY to something that landed this morning.
            # Observed in the field: an item months old sat in "needs you to do something"
            # beside one due in days, with nothing on screen telling them apart. That is the
            # exact erosion the horizon exists to prevent, arriving through the one door the
            # horizon does not cover. This does not hide or expire anything - the owner decides
            # that. It only refuses to let a long-stale item wear the same face as a fresh one.
            "days_waiting": _days_since(r["run_date"]),
            "primary": info["primary"], "links": info["links"][:8],
            "acked": r["message_id"] in acked_msg,
            # Clickable ONLY with a verified sender AND a destination inside the domain.
            "safe_to_click": bool(info["auth_ok"] and info["primary"]
                                  and info["primary"]["host_in_domain"]),
        })
    # Outstanding = not acknowledged, not past, and close enough to act on. `upcoming` is
    # deliberately NOT outstanding: it is real and it is kept, it just does not shout yet.
    # CLICKABILITY IS BOUGHT BACK FOR THE FEW ITEMS THAT NEED IT.
    #
    # Reading from the store fixed reach and cost something real: a stored body cannot prove who
    # sent it, so every store-sourced link is rendered as text. For a September appointment that
    # is a fair trade. For the day-of Video Connect join link it is not - that link arrives ~30
    # minutes before the visit and being able to press it is the entire point of this panel.
    #
    # So the budget changes jobs. It used to buy REACH for all 25 candidates and, against a slow
    # mailbox, fail to buy any. Now the store supplies reach for free and the budget is spent only
    # on the handful of items that are actionable AND whose link is in-domain - the only ones where
    # a verified sender changes what the page can do. That is a much smaller set than the candidate
    # list, so a mailbox slow enough to lose everything before can now still afford the one read
    # that matters. Anything that fails here simply stays as it was: surfaced, honest, not clickable.
    # ONE FAILURE PER ACCOUNT IS ENOUGH, the same lesson the main loop already learned. Two
    # outstanding items on one throttled box would otherwise buy two full timeouts to discover
    # the same fact twice, and the second one is spent after the box has already said no.
    upgraded = 0
    up_failed = set()
    for it in items:
        if it.get("source") != "store" or it["acked"]:
            continue
        if it["account"] in up_failed:
            continue
        if it["state"] not in ("now", "today", "soon"):
            continue
        p_ = it.get("primary")
        if not p_ or not p_["host_in_domain"]:
            continue                      # a verified sender still would not make it clickable
        left = deadline - time.monotonic()
        if left < MIN_READ_S:
            break
        tmp = os.path.join(tempfile.gettempdir(), "vaup_%s" % abs(hash(it["message_id"])))
        try:
            pr = subprocess.run(
                [sys.executable, tool, "find", "--account", it["account"],
                 "--message-id", it["message_id"], "--out", tmp],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=min(45, max(MIN_READ_S, left)), creationflags=_NO_WINDOW)
            if pr.returncode == 0 and os.path.exists(tmp):
                info2 = _workflow_extract(open(tmp, "rb").read(), link_ok, domain)
                if info2["auth_ok"] and info2["primary"]:
                    it.update({
                        "source": "server", "auth_ok": True, "auth_note": None,
                        "primary": info2["primary"], "links": info2["links"][:8],
                        "safe_to_click": bool(info2["primary"]["host_in_domain"]),
                    })
                    upgraded += 1
        except Exception:
            # A failed upgrade is NOT a failure of the item - it already has its body, its date
            # and its link from the store. Silence here would be wrong if it hid a missing item;
            # it cannot, because the item is already in `items` either way.
            up_failed.add(it["account"])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # The counters describe what the reader is LOOKING AT, not the order the code did things in.
    # An upgraded item is a server read by the time it reaches the page, and leaving it counted as
    # a stored one would make the provenance line under-report exactly the items whose links the
    # upgrade just made live.
    read_from_store -= upgraded

    outstanding = [i for i in items
                   if not i["acked"] and i["state"] in ("now", "today", "soon")]
    upcoming = [i for i in items if not i["acked"] and i["state"] == "upcoming"]
    # The client renders "not a <domain> host" and must not carry its own copy of what
    # that domain is - it is configuration, and a second spelling of it is a second thing
    # to get wrong the day someone changes it.
    # `read` is the honest headline and it used to be derivable only by counting `errors`.
    # "outstanding: 0" next to "read: 0 of 11" cannot be mistaken for a quiet morning; on its
    # own it reads exactly like one. `stalled_accounts` names the mailboxes whose silence is
    # the cause, so a reader is told the one real problem instead of eleven symptoms.
    return {"items": items, "lattice": "per-account", "days": days, "horizon": horizon, "errors": errors,
            "candidates": len(seen), "domain": domain, "budget": budget,
            "read": read_ok, "read_from_store": read_from_store, "upgraded": upgraded,
            "read_from_server": read_ok - read_from_store,
            "stalled_accounts": sorted(stalled),
            "outstanding": len(outstanding), "upcoming": len(upcoming)}


def api_account(conn, q):
    """Everything worth knowing about one mailbox.

    The account strip answers only "is it connected". That is the daily question, but it is
    not the interesting one - each mailbox you add has a different JOB, and what actually
    arrives in each is the evidence for whether that routing still holds.
    """
    addr = (q.get("account") or [""])[0].strip()
    if not addr:
        raise ValueError("account is required")

    latest = conn.execute(
        "SELECT a.*, r.run_date FROM account_status a JOIN runs r ON r.id = a.run_id "
        "WHERE a.account = ? ORDER BY r.run_date DESC LIMIT 1", (addr,)).fetchone()
    totals = conn.execute(
        "SELECT COUNT(*) triaged, "
        "SUM(disposition='trashed') trashed, "
        "SUM(disposition IN ('kept','surfaced','saved')) kept, "
        "MIN(run_date) first_run, MAX(run_date) last_run, "
        "COUNT(DISTINCT run_date) runs "
        "FROM messages WHERE account = ?", (addr,)).fetchone()
    by_concept = rows(conn.execute(
        "SELECT COALESCE(concept,'unmapped') concept, COUNT(*) n FROM messages "
        "WHERE account = ? GROUP BY 1 ORDER BY n DESC", (addr,)))
    for c in by_concept:
        c["key"] = concepts.key_of(c["concept"])

    # Senders folded the same way the rest of the dashboard folds them, so the numbers here
    # agree with the Top-senders panel instead of quietly disagreeing.
    fold = collections.Counter()
    for r in conn.execute("SELECT sender FROM messages WHERE account = ? "
                          "AND sender IS NOT NULL AND sender != ''", (addr,)):
        k = _sender_key(r["sender"])
        if k:
            fold[k] += 1
    top_senders = [{"sender": k, "n": n} for k, n in fold.most_common(10)]

    activity = rows(conn.execute(
        "SELECT run_date, COUNT(*) n, SUM(disposition='trashed') trashed "
        "FROM messages WHERE account = ? GROUP BY run_date ORDER BY run_date", (addr,)))

    attention = rows(conn.execute(
        "SELECT run_date, sender, subject, importance, message_id FROM messages "
        "WHERE account = ? AND importance IN ('action-needed','family','security',"
        "'financial') ORDER BY run_date DESC LIMIT 12", (addr,)))
    annotate_acks(conn, attention)

    # Health across every run, not just the last one - a box that fails intermittently is
    # invisible in a single snapshot, and one of these did exactly that once.
    health = rows(conn.execute(
        "SELECT a.status, COUNT(*) n FROM account_status a WHERE a.account = ? "
        "GROUP BY a.status ORDER BY n DESC", (addr,)))

    profile = conn.execute(
        "SELECT COUNT(*) senders FROM sender_profile").fetchone()
    return {
        "account": addr,
        "role": latest["role"] if latest else None,
        "status": latest["status"] if latest else None,
        "auth": latest["auth"] if latest else None,
        "inbox_count": latest["inbox_count"] if latest else None,
        "as_of": latest["run_date"] if latest else None,
        "error": latest["error"] if latest else None,
        "totals": dict(totals) if totals else {},
        "by_concept": by_concept,
        "top_senders": top_senders,
        "activity": activity,
        "attention": attention,
        "health": health,
        "profiled_senders": profile["senders"] if profile else 0,
    }


RULES_FILE = os.path.join(os.path.dirname(HERE), "rules-and-policies.md")
CONFIG_DIR = os.path.join(os.path.dirname(HERE), "config")
PROTECTED_FILE = os.path.join(CONFIG_DIR, "protected.local.json")


ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
DASHBOARD_FILE = os.path.join(CONFIG_DIR, "dashboard.local.json")

# NOT isolated by EMAIL_DASHBOARD_NO_LOCAL_CONFIG, deliberately, and the reason is worth a
# note because the first attempt at F25 did redirect these and broke a test that was already
# doing the right thing.
#
# Both paths hang off THIS FILE'S location, so a test that stands up a whole install in a temp
# directory - which is what test_separation.py does - is already isolated, and the config it
# writes there is config it MEANT to be read. Redirecting these under the flag took that away
# and failed eight of its assertions. Controlling the install directory is the strong form of
# isolation; a global "ignore local config" switch is the weak form, and where the strong form
# is available the weak one must not override it.


def load_dashboard_cfg():
    """dashboard.local.json, or {} - read fresh, never raising.

    One reader, because `api_features` already had its own and two of them would drift the
    moment either grew a key. An unreadable file falls back to the empty config, which is
    every optional thing off - the same fail-closed direction as everywhere else here.
    """
    try:
        with open(DASHBOARD_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def api_features(conn, q):
    """Which OPTIONAL panels this deployment has switched on.

    Steam sale tracking is a real feature and a personal one: it says something about how
    somebody spends their time, which a mail triage tool has no business assuming. So it is
    off unless asked for, and an absent config means off rather than on - the same
    fail-closed direction as everything else here, applied to taste instead of safety.

    Read fresh each call so toggling it does not need a restart, and never raises: an
    unreadable file falls back to every optional panel off, which is the harmless answer.
    """
    panels = {"steam": False}
    try:
        with open(DASHBOARD_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        for name, on in (cfg.get("panels") or {}).items():
            if not str(name).startswith("_"):
                panels[str(name)] = bool(on)
    except FileNotFoundError:
        pass
    except Exception as e:
        panels["_error"] = "%s: %s" % (type(e).__name__, e)
    # THE VERSION OF THE CODE THIS PROCESS IS ACTUALLY RUNNING, which is a different question
    # from what is on disk and is the one worth answering. `version` was imported when this
    # process started, so a server left running across an edit keeps reporting the OLD number
    # while the repo has moved on - and seeing that beside the title is how you catch a stale
    # server at a glance instead of wondering why a shipped change never appeared.
    newest, stale = _served_code_age()
    return {"panels": panels, "version": VERSION, "started": STARTED_AT,
            "newest_edit": newest, "stale": stale}


def _served_code_age():
    """The newest edit among the files THIS process serves, and whether it postdates start-up.

    Reported because a version number only catches staleness once somebody cuts a release, and
    the commoner case - anyone running from a working tree - is an edit that never got a
    version bump. A start time older than your last edit is the same failure and needs no
    release to have happened.

    The comparison is done HERE rather than shown as two timestamps for the reader to subtract.
    A panel that hands you two numbers and expects you to notice one is bigger is the same
    unhelpfulness as an uptime check answering "is something listening"; the useful answer is
    "this process is running code older than your last edit."

    Never raises and never blocks the page: an unreadable directory yields (None, False), which
    claims nothing rather than inventing an alarm.
    """
    try:
        newest = 0.0
        for folder in (HERE, os.path.join(HERE, "static")):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if not name.endswith((".py", ".js", ".html", ".css")):
                    continue
                if name.startswith("test_"):
                    continue          # a test is not code this process serves
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
                except OSError:
                    continue
        if not newest:
            return None, False
        iso = datetime.fromtimestamp(newest).replace(microsecond=0).isoformat()
        return iso, iso > STARTED_AT
    except Exception:                                                 # noqa: BLE001
        return None, False


def api_setup(conn, q):
    """What still needs doing before this install is useful, and how to do it.

    A FRESH INSTALL USED TO LOOK BROKEN RATHER THAN NEW. Every panel rendered an honest
    empty state - no runs, no senders, nothing to show - which is indistinguishable from a
    tool that is failing. The one thing it never said was the only thing a new user needs:
    you have no mailboxes yet, here is the next step.

    Reported as STATE PLUS AN ACTION, per step, derived from the same files the tool
    actually reads. Not a wizard that remembers where you got to - a wizard's memory can
    disagree with reality, and then it walks you past a step that silently did not take.
    Each check below re-derives its answer, so the panel is correct even if you edited the
    files by hand, and it disappears on its own once the answers are all yes.
    """
    steps = []

    # 1. mailboxes
    accounts, acc_err = [], None
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8-sig") as f:
            accounts = json.load(f).get("accounts") or []
    except FileNotFoundError:
        acc_err = "config/accounts.json does not exist yet"
    except Exception as e:
        acc_err = "config/accounts.json is unreadable (%s: %s)" % (type(e).__name__, e)
    steps.append({
        "key": "accounts", "title": "Connect a mailbox",
        "done": bool(accounts) and not acc_err,
        "detail": (acc_err or ("%d mailbox%s configured" % (len(accounts),
                   "" if len(accounts) == 1 else "es")) if accounts or acc_err
                   else "no mailboxes yet"),
        "action": "Run the onboard-mailbox skill, or ask your agent to add a mailbox.",
    })

    # 2. the guard - deliberately its own step, because it is the safety-critical one
    prot = load_protected()
    steps.append({
        "key": "protected", "title": "Say whose mail must never be auto-trashed",
        "done": bool(prot["configured"]),
        # The RESOLVED names, so the editor seeds from what the loader actually honours
        # rather than from what the file appears to say. Placeholders it ignores must never
        # show up in the editor looking like they are protecting somebody.
        "names": prot["names"],
        "detail": (prot["why"] or "%d protected name%s" % (len(prot["names"]),
                   "" if len(prot["names"]) == 1 else "s")),
        "action": "Edit config/protected.local.json and list the people, employers, banks "
                  "and correspondents you must never miss. Until then the dashboard "
                  "refuses to write any auto-trash rule at all - which is the safe "
                  "direction, not a failure.",
    })

    # 3. data
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    steps.append({
        "key": "runs", "title": "Ingest your first run",
        "done": n_runs > 0,
        "detail": ("%d run%s ingested" % (n_runs, "" if n_runs == 1 else "s")) if n_runs
                  else "no runs yet - every panel below is empty because nothing has been "
                       "swept, not because anything is wrong",
        "action": "Sweep with tools/mailtool.py fetch, then ingest the run JSON.",
    })

    # 4. THE ONE THE TOOL NEVER USED TO ASK FOR.
    #
    # Every step above could be green while the rules file still said "_Fill this in._" in
    # five places, and the dashboard would be full of dispositions derived from nobody's
    # judgment. Onboarding that reports itself finished in that state is lying about the
    # only thing that makes the output mean anything: whose rules it is applying.
    #
    # Not done until BOTH: no shipped placeholders survive, and no high-weight question is
    # still unanswered. The second half is what makes this step come back as the mailbox
    # changes, rather than being a box ticked once on day one.
    rules_path = RULES_FILE
    placeholders = _rules_placeholders(rules_path)
    try:
        import questions                                            # noqa: PLC0415
        pending, total = questions.generate(conn, rules_path=rules_path,
                                            protected=prot["names"], limit=50)
        heavy = [p for p in pending if p["weight"] >= 0.85]
    except Exception as exc:                     # a broken generator must not hide the step
        pending, total, heavy = [], 0, []
        placeholders = placeholders if placeholders is not None else 0
        # Loud on the console, quiet in the panel. A generator that raises must not take
        # the setup panel down with it, but it must not vanish either: "0 questions
        # waiting" and "the thing that counts questions is broken" look identical here.
        print("questions: generator failed, step shown without them: %r" % (exc,),
              file=sys.stderr)
    if placeholders is None:
        detail = "no rules file yet - the tool has never been told how you work"
    elif placeholders or heavy:
        bits = []
        if placeholders:
            bits.append("%d section%s still say \"fill this in\""
                        % (placeholders, "" if placeholders == 1 else "s"))
        if heavy:
            bits.append("%d question%s waiting that only you can answer"
                        % (len(heavy), "" if len(heavy) == 1 else "s"))
        detail = "; ".join(bits)
    elif pending:
        # DONE, AND STILL WITH THINGS TO ASK. Both halves have to be said: the step is
        # genuinely satisfied (nothing shipped is still a placeholder, nothing urgent is
        # unanswered) while the mailbox keeps producing questions worth a minute. Reporting
        # only the first half is what left thirteen real questions sitting behind a panel
        # that had already congratulated itself and hidden.
        detail = ("rules are yours, not the shipped defaults - %d more question%s waiting "
                  "whenever you want them" % (len(pending), "" if len(pending) == 1 else "s"))
    else:
        detail = "%d answered; nothing more to ask right now" % len(questions._answered(conn))
    steps.append({
        "key": "rules", "title": "Tell the tool how you work",
        # Advisory, not blocking. The guard in step 2 refuses rules while it is unset
        # because binning a bank's mail is unrecoverable; this one only shapes what gets
        # surfaced, and a tool that refuses to run until you have answered a questionnaire
        # is one nobody finishes installing.
        "done": bool(placeholders == 0 and not heavy),
        "advisory": True,
        "questions_waiting": len(pending),
        "detail": detail,
        "action": ("Ask your agent to \"ask me the setup questions\", or open the Questions "
                   "panel. They are generated from your own mailbox - each one comes with "
                   "the messages behind it, so they are answered from memory in seconds "
                   "rather than by thinking about policy in the abstract."),
    })

    return {"steps": steps,
            # `complete` deliberately ignores advisory steps: a permanently-incomplete
            # setup panel is one people learn to close, and then the two steps that are
            # genuinely load-bearing stop being read too.
            "complete": all(s["done"] for s in steps if not s.get("advisory")),
            "outstanding": [s["key"] for s in steps if not s["done"]]}


def api_protected_names(conn, q, body=None):
    """Write the protected-names list from the browser.

    THE SAFETY-CRITICAL FILE IS THE ONE MOST LIKELY TO BE LEFT AS SHIPPED PLACEHOLDERS,
    because filling it in meant opening a JSON file in an editor. That is where a tool like
    this loses the people it would help most, and it is the wrong place to lose them: while
    the list is empty the guard refuses every rule, so the tool is least useful exactly when
    someone is least equipped to fix it.

    ONLY the names are writable here. Concepts, workflow senders and the link domain are
    deliberately not - they are not what a new user needs on day one, and a write endpoint
    that can rewrite the whole guard is a bigger thing to defend than one that can append to
    a list of names.

    Everything else in the file is preserved byte-for-byte where it can be: the file is
    re-read, the one key is replaced, and the rest is written back as it was found.
    """
    body = body or {}
    names = body.get("names")
    if not isinstance(names, list):
        return {"ok": False, "error": "names must be a list"}
    clean, seen = [], set()
    for n in names:
        n = str(n).strip()
        # A leading underscore is how the template marks a line as commentary, so a name
        # starting with one would be silently ignored by the loader - refuse it here rather
        # than accept a name that will never match anything.
        if not n or n.startswith("_"):
            continue
        if n.lower() in seen:
            continue
        seen.add(n.lower())
        clean.append(n)
    if not clean:
        return {"ok": False,
                "error": "refusing to write an empty list - that would leave the guard "
                         "unconfigured, which it already is. Add at least one name."}

    try:
        with open(PROTECTED_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        return {"ok": False, "error": "%s is unreadable (%s: %s) - fix or delete it first"
                                      % (os.path.basename(PROTECTED_FILE),
                                         type(e).__name__, e)}
    if not isinstance(cfg, dict):
        return {"ok": False, "error": "protected config is not a JSON object"}

    cfg["protected_names"] = clean
    cfg.setdefault("protected_concepts", ["money (bills, receipts, banking)",
                                          "family & people", "account & security", "medical"])
    tmp = PROTECTED_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, PROTECTED_FILE)          # atomic: never a half-written guard
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "error": "could not write: %s: %s" % (type(e).__name__, e)}

    # Re-derive rather than report what we intended to write. The whole point of this file
    # is that the loader's opinion is the one that counts.
    prot = load_protected()
    return {"ok": True, "written": len(clean), "configured": prot["configured"],
            "names": prot["names"], "why": prot["why"]}


def load_protected():
    """WHO MATTERS TO YOU IS CONFIGURATION, NOT CODE.

    This was a regex hard-coded here, which meant one household's relatives were compiled
    into the program. Wrong for them if this ever ships as a plugin, and wrong in principle
    even if it never does - the mechanism is general, the names are personal, and the two
    should not be welded together.

    IT FAILS CLOSED. A missing or unreadable config yields `configured: False`, and every
    rule-writing path refuses outright. An absent guard must never be read as "nothing is
    protected" - that is precisely the direction in which family mail gets silenced, and it
    is the same reassuring-failure shape this lane keeps meeting.

    A PARSEABLE FILE IS NOT A CONFIGURED ONE. This returned `configured: True` for anything
    that was valid JSON, and the installer copied the template verbatim - so a fresh install
    reported itself armed while every name in it was a placeholder that matches no real
    sender. The guard presented as on and protected nobody: the reassuring failure, in the
    installer, in the one file whose entire job is to prevent it. An empty name list now
    reads as unconfigured too, and the placeholders themselves are `_`-prefixed upstream so
    a verbatim copy yields zero names. Either fix alone closes it; both are in place because
    this one is worth closing twice.
    """
    try:
        with open(PROTECTED_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        return {"configured": False, "why": "%s: %s" % (type(e).__name__, e),
                "names": [], "concepts": set(), "workflow_senders": {},
                "link_domain": "", "min_messages": 8}
    names = [str(n).strip().lower() for n in cfg.get("protected_names", [])
             # the template carries italic _explanatory_ lines; they are not names
             if str(n).strip() and not str(n).strip().startswith("_")]
    if not names:
        return {"configured": False,
                "why": "%s has no protected names yet - every entry is still a template "
                       "placeholder or the list is empty. Add the people, employers, banks "
                       "and correspondents whose mail must never be auto-trashed."
                       % os.path.basename(PROTECTED_FILE),
                "names": [], "concepts": set(cfg.get("protected_concepts") or []),
                "workflow_senders": {}, "link_domain": "", "min_messages": 8}
    return {
        "configured": True, "why": "",
        "names": names,
        "concepts": set(cfg.get("protected_concepts") or []),
        "workflow_senders": {k.lower(): tuple(v) for k, v in
                             (cfg.get("workflow_senders") or {}).items()},
        "link_domain": (cfg.get("workflow_link_domain") or "").strip().lower(),
        "min_messages": int(cfg.get("rule_min_messages") or 8),
    }


def protected_hit(prot, text):
    t = (text or "").lower()
    return any(n in t for n in prot["names"])


def protected_names_hit(prot, text):
    """Which protected entries matched - not merely whether one did.

    Same test as `protected_hit`, reported instead of collapsed. The match is a plain
    substring over the WHOLE sender string, which includes the address and the sending
    provider's domain, so a short entry can match a sender that merely contains those
    letters (a three-letter acronym inside an ordinary English word; an organisation name
    that another company's ESP domain happens to begin with). That breadth is deliberate -
    it fails toward keeping mail, which is the safe direction here, and narrowing it would
    also break entries that are meant to match inside a domain with no separator.

    What is NOT safe is a refusal that cannot say which entry fired. "This sender is
    protected" reads as a verdict about the sender; "this sender matches protected entry
    'x'" is a claim a reader can check, and an accidental match becomes visible the first
    time it happens instead of surviving as a rule that silently never executes.
    """
    t = (text or "").lower()
    return [n for n in prot["names"] if n in t]


def sender_rule_verdict(conn, key, category=None):
    """Is this sender - or this slice of it - safe to lock to auto-trash?

    Judged HERE, from the store, never from what the browser asserts. A click is a request;
    the entitlement to change standing policy has to be re-derived server-side or the guard
    is only as good as the page that called it.

    WHY A SLICE. Rules were keyed on SENDER, and mail does not arrive that way. Simulated
    across every sender in a real work store, with the disposition data corrected, the number
    of senders eligible for an auto-trash rule was ZERO - not few, none. The reason is
    structural rather than incidental: the highest-volume senders are notification services
    whose entire job is to multiplex many kinds of message through one address, so the volume
    that makes a sender worth ruling on is the same volume that guarantees the sender is
    mixed. One tracker address carried dozens of binnable status mails AND the handful of
    "a person named you" messages that were the whole basis of the standing work list.

    The guard was right to refuse it. `this sender is pure noise` is a FALSE STATEMENT about
    that address, and no amount of fixing the guard should ever make it pass. The rule engine
    was behaving correctly and was useless, because the only thing it could express was not
    true of any sender worth expressing it about. `rule_min_messages` then sealed it: below
    the threshold there is not enough evidence, and above it the sender is mixed.

    So a rule may now name (sender, category), which is a statement that can be true. Every
    check below is unchanged and simply runs against the slice: NARROWER evidence, not weaker
    evidence. A slice with any deliberately-kept mail is still refused, and the protected-name
    check deliberately stays at the whole-sender level - if a person is protected, no slice of
    their mail may be binned.

    The triage layer already separates this correctly - category, concept, importance and
    addressed_directly are all resolved per message. Only the rule layer collapsed them back
    onto one sender.
    """
    prot = load_protected()
    if not prot["configured"]:
        # No guard list, no rule writing. Refusing is the only safe reading of a missing
        # protection file; the alternative is a button that can silence anyone.
        return {"eligible": False, "configured": False, "category": category,
                "why": "no protected-sender config (config/protected.local.json): "
                       "refusing to write any auto-trash rule. " + prot["why"]}

    rows_ = rows(conn.execute(
        "SELECT sender, disposition, COALESCE(concept,'') concept, run_date, importance, "
        "COALESCE(category,'') category "
        "FROM messages WHERE sender IS NOT NULL AND sender != ''"))
    all_mine = [r for r in rows_ if _sender_key(r["sender"]) == key]
    if not all_mine:
        return {"eligible": False, "category": category,
                "why": "no messages recorded for that sender"}
    category = (category or "").strip() or None
    mine = ([r for r in all_mine if r["category"] == category] if category else all_mine)
    if not mine:
        return {"eligible": False, "category": category,
                "why": "no messages recorded for that sender under %r" % category}

    total = len(mine)
    binned = sum(1 for r in mine if r["disposition"] in db.DISPOSABLE)
    kept = sum(1 for r in mine if r["disposition"] in db.DELIBERATELY_KEPT)
    runs_ = len({r["run_date"] for r in mine})
    concepts_seen = {r["concept"] for r in mine if r["concept"]}
    variants = sorted({r["sender"] for r in all_mine})

    reasons = []
    if kept:
        reasons.append(f"kept or surfaced {kept} of {total} - not pure noise")
    if total < prot["min_messages"]:
        reasons.append(f"only {total} messages; {prot['min_messages']} needed")
    hit = concepts_seen & prot["concepts"]
    if hit:
        reasons.append("protected category: " + ", ".join(sorted(hit)))
    # WHOLE-SENDER, not the slice. A protected person does not become binnable one label at
    # a time, and this is the check where narrowing would be weakening rather than sharpening.
    if protected_hit(prot, key) or any(protected_hit(prot, v) for v in variants):
        reasons.append("on your protected-sender list")
    if any((r["importance"] or "") in ("action-needed", "family", "security", "financial")
           for r in mine):
        reasons.append("has been flagged as needing attention before")

    return {"eligible": not reasons, "why": "; ".join(reasons) or "",
            "category": category, "scope": "category" if category else "sender",
            "total": total, "binned": binned, "kept": kept, "runs": runs_,
            "sender_total": len(all_mine),
            "variants": variants, "concepts": sorted(concepts_seen)}


def sender_rule_slices(conn, key):
    """Every category this sender writes under, each with its own verdict.

    This is what makes the narrower scope usable rather than merely possible: the panel can
    show that one address is mostly status noise (eligible), partly bot chatter (the owner's
    call) and partly "a person named you" (protected, and refused for a stated reason) -
    rather than one button that never lights up and never says why.
    """
    rows_ = rows(conn.execute(
        "SELECT sender, COALESCE(category,'') category FROM messages "
        "WHERE sender IS NOT NULL AND sender != ''"))
    cats = collections.Counter(r["category"] for r in rows_
                               if _sender_key(r["sender"]) == key and r["category"])
    out = []
    for cat, n in cats.most_common():
        v = sender_rule_verdict(conn, key, cat)
        out.append({"category": cat, "n": n, "eligible": v.get("eligible", False),
                    "why": v.get("why", ""), "binned": v.get("binned", 0),
                    "kept": v.get("kept", 0),
                    "already_ruled": _already_ruled(key, cat)})
    return out


def api_sender(conn, q):
    """One sender's whole story - volume, rhythm, where they write, and what they link to."""
    key = (q.get("key") or [""])[0].strip().lower()
    if not key:
        raise ValueError("key is required")

    rows_ = rows(conn.execute(
        "SELECT run_date, account, sender, subject, reason, disposition, importance, "
        "message_id, COALESCE(concept,'unmapped') concept FROM messages "
        "WHERE sender IS NOT NULL AND sender != '' ORDER BY run_date DESC"))
    mine = [r for r in rows_ if _sender_key(r["sender"]) == key]
    if not mine:
        return {"key": key, "found": False}

    run_days = [x[0] for x in conn.execute(
        "SELECT DISTINCT run_date FROM runs ORDER BY run_date")]
    seen_days = {r["run_date"] for r in mine}
    activity = [{"run_date": d, "n": sum(1 for r in mine if r["run_date"] == d)}
                for d in run_days]

    idx = {d: i for i, d in enumerate(run_days)}
    pos = sorted({idx[d] for d in seen_days if d in idx})
    gaps = [b - a for a, b in zip(pos, pos[1:])]
    silence = (len(run_days) - 1 - pos[-1]) if pos else None
    quiet = bool(gaps and silence is not None and silence > max(gaps))

    by_concept = collections.Counter(r["concept"] for r in mine)
    by_account = collections.Counter(r["account"] for r in mine)
    hosts = rows(conn.execute(
        "SELECT host, messages FROM sender_hosts WHERE sender_key = ? "
        "ORDER BY messages DESC", (key,)))
    prof = conn.execute("SELECT messages FROM sender_profile WHERE sender_key = ?",
                        (key,)).fetchone()

    recent = mine[:12]
    annotate_acks(conn, recent)
    return {
        "key": key, "found": True,
        "total": len(mine),
        "binned": sum(1 for r in mine if r["disposition"] in db.DISPOSABLE),
        "kept": sum(1 for r in mine if r["disposition"] in db.DELIBERATELY_KEPT),
        "runs": len(seen_days), "first_seen": min(seen_days), "last_seen": max(seen_days),
        "silence": silence, "worst_gap": max(gaps) if gaps else None, "quiet": quiet,
        "variants": sorted({r["sender"] for r in mine}),
        "by_concept": [{"concept": k, "n": n} for k, n in by_concept.most_common()],
        "by_account": [{"account": k, "n": n} for k, n in by_account.most_common()],
        "hosts": hosts,
        "profile_messages": prof["messages"] if prof else 0,
        "profile_established": bool(prof and prof["messages"] >= PROFILE_MIN_MESSAGES),
        "activity": activity,
        "recent": recent,
        "rule": sender_rule_verdict(conn, key),
        # WHY THE BUTTON IS DARK, per slice. A whole-sender verdict on a notification address
        # is always "not pure noise" and always correct, and tells the owner nothing they can
        # act on. The breakdown says which part of this sender's mail could be ruled on and
        # which part is protected, which is the difference between a feature and a button.
        "rule_slices": sender_rule_slices(conn, key),
        "already_ruled": _already_ruled(key),
    }


def _read_rules():
    """Read the policy file WITHOUT touching its line endings.

    Plain open()/write() on Windows rewrites every LF as CRLF, so adding one row silently
    reformatted every line in the file - pure noise, and a whole-file diff for a
    one-line change. A file this important should come back byte-identical apart from the
    row that was actually added.
    """
    with open(RULES_FILE, encoding="utf-8", newline="") as f:
        raw = f.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw, nl


def _write_rules(lines, nl):
    with open(RULES_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines) + nl)


def _rule_marker(key, category=None):
    """The marker a dashboard-written rule carries.

    A bare `key` is the whole-sender form and stays exactly as it was, so rules written
    before scoped rules existed keep working and keep being liftable. A scoped rule appends
    the category. Two forms, one prefix - the alternative (re-keying the old ones) would have
    orphaned every existing rule from the button that lifts it, which is the acknowledgement
    defect all over again.
    """
    cat = (category or "").strip()
    return "<!-- dashboard-rule:%s%s -->" % (key, ("|" + cat) if cat else "")


def _already_ruled(key, category=None):
    try:
        raw, _ = _read_rules()
    except OSError:
        return False
    if _rule_marker(key, category) in raw:
        return True
    # A whole-sender rule already covers every slice of that sender. Reporting a slice as
    # unruled while the sender is locked would invite a second, redundant rule.
    return category is not None and _rule_marker(key) in raw


def api_sender_rule(conn, q, body=None):
    """Lock a sender to auto-trash by WRITING THE RULE, or lift it again.

    Rule 8 has always described this loop - borderline senders are listed as junk
    candidates, and once the owner confirms one it joins the Confirmed junk senders list
    and is auto-trashed from then on. It simply had no mechanism, so it never fired while
    the same senders accumulated hundreds of hand-triaged messages between them.

    THE ENTITLEMENT IS RE-DERIVED HERE. The browser says which sender; the server decides
    whether that sender may be locked, from the stored record. A protected category, any
    history of being kept, any past attention flag, or too little evidence all refuse -
    whatever the page claims.
    """
    body = body or {}
    key = (body.get("key") or "").strip().lower()
    category = (body.get("category") or "").strip() or None
    if not key:
        return {"ok": False, "error": "key is required"}
    # Eligibility FIRST, so the refusal names the reason that matters. Reading the rules
    # file first meant a fresh install refused with "cannot read the rules file" when the
    # real and more important answer was "you have not told me who is protected yet" -
    # a true refusal for a misleading reason is still a bad error message.
    verdict = sender_rule_verdict(conn, key, category)
    if body.get("on") is not False and not verdict["eligible"]:
        return {"ok": False, "error": "not eligible: " + verdict["why"], "verdict": verdict}

    try:
        text, nl = _read_rules()
    except OSError as e:
        return {"ok": False,
                "error": "no rules file yet (%s). Copy rules-and-policies.example.md to "
                         "rules-and-policies.md to start one." % type(e).__name__}
    marker = _rule_marker(key, category)

    if body.get("on") is False:
        if marker not in text:
            return {"ok": False, "error": "no dashboard-written rule for that sender"
                                          + (" under %r" % category if category else "")}
        kept = [ln for ln in text.splitlines() if marker not in ln]
        _write_rules(kept, nl)
        return {"ok": True, "key": key, "category": category, "ruled": False}

    if not verdict["eligible"]:
        return {"ok": False, "error": "not eligible: " + verdict["why"], "verdict": verdict}
    if marker in text:
        return {"ok": True, "key": key, "category": category, "ruled": True,
                "note": "already ruled"}

    today = db.now_iso()[:10]
    label = (body.get("label") or key)[:60]
    if category:
        # The scope is IN THE ROW, not only in the marker. A rules file is read by people,
        # and a row saying "auto-trash this sender" when the rule covers one label of their
        # mail is the kind of quiet overstatement that gets a rule lifted in a panic later.
        # The caveat is stated too: a scoped rule is only as good as the label, and the label
        # is assigned by the triager on mail that has not arrived yet.
        row = ("| %s - only mail labelled `%s` (auto-trash, confirmed from the dashboard) "
               "| %s | %d of %d messages under this label binned, none ever kept, across %d "
               "runs - locked on that evidence. Other mail from this sender is UNAFFECTED "
               "(%d messages in total). Depends on the label being assigned correctly to "
               "future mail. Lift it from the sender panel. %s |"
               % (label, category, today, verdict["binned"], verdict["total"],
                  verdict["runs"], verdict["sender_total"], marker))
    else:
        row = ("| %s (auto-trash, confirmed from the dashboard) | %s | Binned %d of %d "
               "messages across %d runs with none ever kept - locked on that evidence. "
               "Lift it from the sender panel. %s |"
               % (label, today, verdict["binned"], verdict["total"], verdict["runs"],
                  marker))

    lines = text.splitlines()
    # Append to the Confirmed junk senders table, immediately after its last row.
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("## Confirmed junk senders"))
    except StopIteration:
        return {"ok": False, "error": "could not find the Confirmed junk senders section"}
    end = start
    for i in range(start, len(lines)):
        if lines[i].startswith("|"):
            end = i
        elif lines[i].startswith("## ") and i > start:
            break
    lines.insert(end + 1, row)
    _write_rules(lines, nl)
    return {"ok": True, "key": key, "ruled": True, "verdict": verdict, "row": row}


def api_calendar(conn, q):
    """One cell per day: volume, and what the day was mostly ABOUT.

    The run history was a dropdown of dates - the least evocative possible rendering of
    everything this lane has done. As a grid it shows at a glance the quiet stretches, the
    spikes, and the weeks something was escalating: patterns no single run report can
    express and no table makes visible.

    KEYED ON WHEN MAIL ARRIVED, not on when a sweep ran (?by=swept for the other question).
    Both dates were stored from the beginning and only run_date was ever queried, so an
    onboarding intake - which triages months of existing mail in one session - rendered as
    a SINGLE tile, one run covering most of a year. The one thing a new user most wants to
    see, the shape of what they have been missing, was the one thing the view could not
    show. It was not a missing column; it was the wrong column.

    msg_day is derived on write rather than parsed here, because the raw msg_date is not
    one format: a live store holds ISO dates, RFC 2822 dates and NULLs in the same column,
    and grouping on the raw text buckets "Wed, 5 Aug 2026 ..." under its weekday.
    """
    by = (q.get("by") or ["arrived"])[0]
    col = "run_date" if by == "swept" else "COALESCE(msg_day, run_date)"
    days = rows(conn.execute(
        "SELECT %s day, COUNT(*) n, "
        "SUM(disposition='trashed') trashed, "
        "SUM(disposition IN ('kept','surfaced','saved')) kept "
        "FROM messages GROUP BY day ORDER BY day" % col))
    # dominant concept per day, so the tint means something rather than being decoration
    dom = {}
    for r in conn.execute(
            "SELECT %s day, COALESCE(concept,'unmapped') c, COUNT(*) n FROM messages "
            "GROUP BY day, c ORDER BY day, n DESC" % col):
        dom.setdefault(r["day"], concepts.key_of(r["c"]))
    # What actually earned attention that day - the reason to click a cell.
    #
    # The importance column has NINE spellings (action-needed, family, security, financial,
    # info, low, normal, fyi, routine) - the same one-concept-many-spellings drift that hit
    # the category labels and the sender strings. The first version matched only two of
    # them, so days whose sole notable item was a SECURITY notice or a FINANCIAL one did not
    # ring at all. Match the whole attention set, not the two that came to mind.
    ATTENTION = ("action-needed", "family", "security", "financial")
    acked_msg = acked_message_keys(conn)
    acked_thread = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'thread'")}
    act, open_act = collections.Counter(), collections.Counter()
    for r in conn.execute(
            "SELECT %s day, account, sender, subject, message_id, concept, importance "
            "FROM messages WHERE "
            "importance IN (%s)" % (col, ",".join("?" * len(ATTENTION))), ATTENTION):
        act[r["day"]] += 1
        # Acknowledged counts as handled at either scope - a thread ack covers this run's
        # instance of a recurring notice just as a message ack covers the single email.
        # `concept` and `importance` are selected because ack_covers needs them for the
        # family-escalation carve-out; without them a family emergency would count as
        # handled here while the open-items panel showed it as open.
        done = ack_covers(r, acked_msg, acked_thread)
        if not done:
            open_act[r["day"]] += 1
    for d in days:
        # `run_date` is kept in the payload so the client can keep selecting a RUN when a
        # cell is clicked; `day` is what the cell represents.
        d["run_date"] = d["day"]
        d["concept"] = dom.get(d["day"], "other")
        d["action"] = act.get(d["day"], 0)
        # What is still OUTSTANDING is the number that should drive the colour: a day whose
        # items that have all been seen is a day you can stop looking at.
        d["action_open"] = open_act.get(d["day"], 0)
    return {"days": days, "by": "swept" if by == "swept" else "arrived",
            "totals": {"runs": len(days),
                       "messages": sum(d["n"] for d in days),
                       "kept": sum(d["kept"] for d in days),
                       "trashed": sum(d["trashed"] for d in days)}}


# Strip the parts of a subject that CHANGE between otherwise-identical notices: dates,
# amounts, invoice/order numbers, counts. Without this, "Payment due 08/21" and "Payment due
# 09/21" look like two unrelated messages, which is precisely how a repeating notice hides.
# subject_shape lives in db.py now - it is what WRITES thread keys (acks and
# open_items both), and a reader with its own copy of the rule is how "Re: X"
# and "X" became two different threads in the first place. Re-exported here so
# every existing caller in this module is unchanged.
subject_shape = db.subject_shape
_SHAPE_SUBS = db._SHAPE_SUBS



def _days_between_dates(a, b):
    """Whole days from a to b, or None if either cannot be read. Never a guess."""
    from datetime import date                                      # noqa: PLC0415
    try:
        y1, m1, d1 = (int(x) for x in str(a)[:10].split("-"))
        y2, m2, d2 = (int(x) for x in str(b)[:10].split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except (ValueError, TypeError):
        return None


def _day_gaps(dates):
    """Calendar days between consecutive arrivals.

    A date that cannot be parsed drops out rather than contributing a made-up interval -
    one unreadable date in the middle would otherwise merge two real gaps into a third that
    never happened.
    """
    out = []
    for a, b in zip(dates, dates[1:]):
        n = _days_between_dates(a, b)
        if n is not None:
            out.append(n)
    return out


def api_repeats(conn, q):
    """The SAME thing arriving again and again, collapsed into one item.

    THE DROWNING MECHANISM, and the reason this exists. Every row in this dashboard is
    independent, so one issue arriving three days running renders as three unrelated
    low-priority lines. The payment-method expiry did exactly that on 08-01, 08-02 and
    08-03, and my own run report had to carry a HAND-WRITTEN note saying "day 3 of the same
    message" because nothing in the data expressed it. Seven unread dunning notices is how
    a subscription died once; every one of those seven looked like an isolated row.

    So: group by sender + subject SHAPE, count the notices, and say whether they are
    arriving faster. A thing on its fifth notice, accelerating, is not five pieces of noise
    - it is one signal that has been ignored, and it should get louder the longer it goes.
    """
    try:
        min_n = max(2, int((q.get("min") or ["3"])[0]))
    except ValueError:
        min_n = 3

    groups = collections.defaultdict(list)
    for r in conn.execute(
            "SELECT run_date, account, sender, subject, disposition, category, "
            "COALESCE(concept,'unmapped') concept, importance, message_id "
            "FROM messages WHERE sender IS NOT NULL AND sender != '' ORDER BY run_date"):
        shape = subject_shape(r["subject"])
        if not shape:
            continue
        groups[(_sender_key(r["sender"]), shape)].append(dict(r))

    run_days = [x[0] for x in conn.execute(
        "SELECT DISTINCT run_date FROM runs ORDER BY run_date")]
    idx = {d: i for i, d in enumerate(run_days)}
    last_i = len(run_days) - 1

    items = []
    for (skey, shape), rs in groups.items():
        # COUNT ARRIVALS, NOT LISTINGS. A message that stays in the inbox is re-listed by
        # every run while it is inside the fetch window, so run-dates count MY behaviour,
        # not the sender's. Measured: a bank's "credit card statement is available" spans 6
        # run dates and exactly ONE Message-ID - one statement, re-listed six times. This
        # panel would have announced "6th notice, accelerating" about a single email, which
        # is manufactured urgency inside the very view built to fight it. A bank's 6
        # rows are 3 real monthly statements, and monthly is not acceleration.
        ided = [r for r in rs if r["message_id"]]
        if len(ided) == len(rs):
            basis = "messages"                     # exact: one entry per real arrival
            first_run = {}
            for r in rs:
                first_run.setdefault(r["message_id"], r["run_date"])
            dates = sorted(set(first_run.values()))
            n_notices = len(first_run)
        else:
            # Some rows predate message linking, so arrivals cannot be separated from
            # re-listings. Fall back, and SAY SO rather than quoting a number that may be
            # counting the same message repeatedly.
            basis = "listings"
            dates = sorted({r["run_date"] for r in rs})
            n_notices = len(dates)
        if n_notices < min_n:
            continue
        pos = sorted({idx[d] for d in dates if d in idx})
        if len(pos) < min_n:
            continue

        # GAPS IN CALENDAR DAYS, NOT IN RUNS.
        #
        # "Arriving faster" is a claim about the world; counting the gap in RUNS made it
        # partly a claim about how often the tool was run. That was survivable while every
        # run was a daily sweep, and stopped being so when a historical intake staged one
        # run per arrival day from a single mailbox: this store went to 252 runs of which
        # ~51 were sweeps, and 139 covered exactly one account. Measured on a real
        # twelve-notice series, the same gaps read [20,5,3,4,4,2,2,3,155,2,2] against all
        # runs and [10,3,2,1,2,2,2,1,112,2,2] against that mailbox - roughly double, and
        # unevenly.
        #
        # Uneven is the dangerous part, because acceleration compares EARLY gaps against
        # RECENT ones: an intake concentrated in one period can manufacture an acceleration
        # that never happened, or hide one that did. Days are immune to all of it - they do
        # not care how many times anybody looked.
        #
        # Scoping per-mailbox (the fix `api_quiet` needed) would NOT have been right here:
        # it re-bases the same wrong unit.
        gaps = _day_gaps(dates)
        # Acceleration is only meaningful when the gaps are between real ARRIVALS. On the
        # approximate basis the gaps are partly my own re-listing cadence, so no claim is
        # made rather than a shaky one.
        accelerating = False
        silence = (_days_between_dates(dates[-1], run_days[-1]) if run_days else None) or 0
        med = statistics.median(gaps) if gaps else 0
        if basis == "messages" and len(gaps) >= 3:
            half = max(1, len(gaps) // 2)
            early = sum(gaps[:-half]) / max(1, len(gaps) - half)
            recent = sum(gaps[-half:]) / half
            accelerating = bool(recent * 1.5 < early)
            # ACCELERATION IS A CLAIM ABOUT THE PRESENT TENSE, and the arithmetic above only
            # looks at the gaps BETWEEN arrivals - so a series that stopped dead still
            # reported "accelerating" on the strength of how it behaved before it stopped.
            # This store had one at 246 days silent with a 4-day median, described as
            # arriving faster. The gap between the last notice and now is a gap too; a
            # series quiet for several of its own cycles is stalled, not speeding up.
            if med and silence > med * 2:
                accelerating = False
        # DORMANT rather than dropped. Most of what a repeats panel accumulates over a year
        # of history is series that ran their course - useful to have, ruinous to lead with,
        # because a live dunning notice buried under fifty finished ones is a live dunning
        # notice nobody sees.
        dormant = bool(silence > max(med * 3, 30))
        concept = collections.Counter(r["concept"] for r in rs).most_common(1)[0][0]
        weight = 3 if concept in ("money (bills, receipts, banking)", "account & security",
                                  "family & people", "medical") else 1
        last = rs[-1]
        items.append({
            "sender": last["sender"], "sender_key": skey,
            "subject": last["subject"], "shape": shape,
            "account": last["account"], "message_id": last["message_id"],
            "notices": n_notices, "basis": basis,
            "first_seen": dates[0], "last_seen": dates[-1],
            "runs_since_last": last_i - pos[-1],
            # Days, and SAID to be days. A bare number that changed meaning silently is how
            # a reader keeps trusting a figure that no longer says what they think.
            "days_since_last": _days_between_dates(dates[-1], run_days[-1])
            if run_days else None,
            "median_gap": statistics.median(gaps) if gaps else 0,
            "gap_unit": "days",
            "accelerating": accelerating,
            "dormant": dormant,
            "concept": concept, "concept_key": concepts.key_of(concept),
            "weight": weight,
            "still_open": last["disposition"] in db.DELIBERATELY_KEPT,
            "dispositions": sorted({r["disposition"] for r in rs}),
        })

    # LIVE FIRST, then weight, then accelerating, then sheer persistence. Dormant series
    # keep their place in the list rather than being hidden - they are real history, and a
    # series can wake up - but they never outrank something still arriving.
    items.sort(key=lambda it: (int(it["dormant"]), -it["weight"],
                               -int(it["accelerating"]), -it["notices"]))
    return {"items": items, "min_notices": min_n, "groups_examined": len(groups),
            "dormant": sum(1 for it in items if it["dormant"]),
            "live": sum(1 for it in items if not it["dormant"])}


# ---------------------------------------------------------------- quiet senders
MIN_OBS = 5           # appearances needed before this sender is claimed to have a rhythm
# 21, deliberately the same number the old runs-based threshold used: on a store that runs
# once a day the two are the same quantity, so this is a change of UNIT and not a quiet
# tightening of the bar riding along with it. The noise this panel was drowning in is
# suppressed by the two floors below, which is where that argument belongs.
MIN_SPAN_DAYS = 21    # calendar days they must span, so a 3-day burst is never a cadence

# A RATIO NEEDS A FLOOR UNDER IT, and this panel shipped without one.
#
# "5x its worst" sounds decisive and means nothing when the worst gap was two days: any
# sender that happens to write in bursts clears a multiple of a tiny number the moment it
# pauses. The screenshot that prompted this had a sender at 1.25x - which is not an anomaly,
# it is rounding - sitting in an alarm panel next to a bank that had genuinely vanished for
# eight months. A panel where the real finding and the arithmetic artefact look the same is
# a panel that gets ignored, and then the real one is lost with it.
#
# So a flag needs BOTH: meaningfully longer than its own worst gap, AND long enough in
# absolute terms to be worth a person's attention at all.
MIN_SILENCE_DAYS = 14
MIN_RATIO = 1.5

# Monthly senders need roughly this much history before a monthly rhythm is observable at
# all. Used to DERIVE the caveat rather than assert it: the panel used to state flatly that
# monthly billers could not qualify, which stopped being true the moment a year of arrival-
# dated history existed - and it said so while a monthly bank statement sat at the top of
# its own list. A hard-coded caveat is a claim that goes stale silently.
MONTHLY_OBSERVABLE_DAYS = 150

# Senders whose "rhythm" is really other people's behaviour. A friend posting less often is
# not a finding a mail tool should raise, and left in they dominate the list by sheer count.
# HIDDEN, NOT DROPPED: the count is reported and `?include=all` returns them, because
# suppression that cannot be seen is indistinguishable from having found nothing.
SOCIAL_CONCEPT = "social / platform notifications"

# Which categories weigh more when a sender goes quiet. DERIVED from the concept map
# rather than listed here, for two reasons.
#
# It was a hand-written list of raw labels, and hand-written label lists are how one
# mailbox's vocabulary ends up compiled into a published program - this pair carried a
# carrier name, a business-listing label and a monitoring subscription. Deriving them means
# the personal labels live in concepts.local.json, where they belong, and still weigh
# correctly here.
#
# It is also just right: a label added to "money" in the local file SHOULD weigh as money
# without anyone remembering to add it in a second place. Two lists of the same thing drift,
# which is the defect concepts.py exists to close.
def _labels_for(*concept_names):
    out = set()
    for name in concept_names:
        out.update(l.lower() for l in (concepts.CONCEPTS.get(name) or []))
    return out


MONEY_CATS = _labels_for("money (bills, receipts, banking)")
GUARD_CATS = _labels_for("account & security", "family & people", "medical",
                         "mail logistics")


def api_quiet(conn, q):
    """Senders that have gone QUIET - the one view that raises an alarm by seeing NOTHING.

    Every other panel is driven by mail that ARRIVED, so the whole class of "a biller
    stopped writing" is structurally invisible: a sender with nothing to say and a sender
    whose mail is going astray look identical. Seven unread dunning notices is how a
    subscription died once already, which is the precedent this exists to prevent.

    THE RULE IS SELF-CALIBRATING, deliberately. A sender is flagged only when its current
    silence exceeds its OWN WORST historical gap - "quieter than I have ever seen it". No
    universal threshold could work across a daily promo and a monthly statement, and a
    median-based rule fires constantly on senders that are simply bursty (measured: a
    median-gap rule flagged over half of all senders, which is noise, not signal).

    MEASURED IN CALENDAR DAYS. It used to count RUNS ELAPSED, on the reasoning that a day
    with no run is not evidence of silence - which is sound, and which stopped being the
    same quantity the moment a backfill existed. Runs are no longer evenly spaced in time:
    a year of arrival-dated history packs hundreds of them into the past while the present
    accumulates one a day, so a gap of "23 runs" in 2025 and "23 runs" in 2026 describe
    completely different amounts of the world. The panel then reported "silent 105 of 173
    runs", which is a true sentence about the store and tells a person nothing about their
    bank.

    The soundness of the original reasoning is kept where it belongs: the observation
    LATTICE still decides whether we looked, and only days on which this sender's mailbox
    was actually examined can contribute. What changed is the UNIT the answer is reported
    in - days, because that is what "gone quiet" means to the person reading it.

    THREE THINGS THIS DELIBERATELY DOES NOT DO:
      * It does not claim a cadence from a burst. MIN_OBS and MIN_SPAN_DAYS mean three
        consecutive days of mail is never mistaken for "arrives daily".
      * It does not treat a multiple as evidence on its own. See MIN_SILENCE_DAYS.
      * It does not assert what it cannot see. Whether a monthly rhythm is observable is
        DERIVED from the actual span of the window, not hard-coded - the hard-coded version
        went stale and told the reader monthly billers could not qualify while a monthly
        bank statement sat at the top of the list it was captioning.
    """
    # `account` is selected only if the table has it. A store older than the column - or a
    # test fixture that builds a minimal table - must still get a usable answer rather than
    # an exception, and without the column every run covers every sender, which is exactly
    # the old behaviour and correct for a store that has never been backfilled.
    has_account = any(r[1] == "account"
                      for r in conn.execute("PRAGMA table_info(messages)"))
    rows_all = rows(conn.execute(
        "SELECT sender, %s AS account, run_date, category FROM messages "
        "WHERE sender IS NOT NULL AND sender != ''"
        % ("account" if has_account else "''")))

    # WHICH RUNS ACTUALLY LOOKED AT WHICH MAILBOX.
    #
    # This panel says "I looked and saw nothing", and that sentence is only true of runs
    # that looked at the mailbox the sender writes to. It used to measure every sender
    # against EVERY run, which held while every run was a full sweep of all accounts - and
    # stopped holding the moment a historical intake existed.
    #
    # Measured on this store after the backfill: 252 runs, of which 51 were real sweeps and
    # 139 contained exactly ONE mailbox. A monthly biller in one account was counted silent
    # across every backfilled day drawn from a different account - so its gap grew by two
    # hundred runs while its behaviour did not change at all, and it was reported as 3.69x
    # its own worst silence. Nobody had looked. That is an absence asserted by an instrument
    # that never ran, which is the exact failure this whole project is organised against,
    # arriving through its own backfill feature.
    per_account_days = collections.defaultdict(set)
    for r in rows_all:
        if r["account"]:
            per_account_days[r["account"]].add(r["run_date"])
    # AND the days a run CONNECTED to that mailbox and found nothing worth recording.
    # Deriving the lattice from messages alone means a mailbox only counts as observed on
    # days it produced mail, which quietly shortens every silence measured against it - the
    # sender is being credited for days nobody can prove anyone looked, in the direction
    # that UNDER-reports. account_status is the table that actually records which accounts a
    # run reached. It is a union rather than a replacement because the arrival-day backfill
    # writes no account block at all (deliberately - see ingest --by-arrival), so on its own
    # it would erase every backfilled day from the window.
    try:
        for r in rows(conn.execute(
                "SELECT r.run_date, a.account FROM account_status a "
                "JOIN runs r ON r.id = a.run_id WHERE a.account IS NOT NULL")):
            per_account_days[r["account"]].add(r["run_date"])
    except Exception:
        pass

    # THE LATTICE COMES FROM `runs`, NOT FROM `messages`. The question this panel answers
    # is "when did I LOOK and see nothing", so the observation days are the days a run
    # happened - which is exactly what the runs table records. Deriving them from messages
    # instead silently drops any run that triaged nothing, shortening the very window the
    # silence is measured against and UNDER-reporting how long a sender has been gone.
    # Caught by the synthetic two-sided control, not by the live data (where every run
    # happens to have messages, so both sources agree and the bug is invisible).
    try:
        run_days = [r["run_date"] for r in rows(conn.execute(
            "SELECT DISTINCT run_date FROM runs ORDER BY run_date"))]
    except Exception:
        run_days = []
    if not run_days:
        run_days = sorted({r["run_date"] for r in rows_all})
    if len(run_days) < 2:
        return {"items": [], "reach": {"runs": len(run_days), "established": 0,
                                       "considered": 0}}
    idx = {d: i for i, d in enumerate(run_days)}
    last_i = len(run_days) - 1

    seen = collections.defaultdict(set)
    cats = collections.defaultdict(collections.Counter)
    variants = collections.defaultdict(set)
    sender_accounts = collections.defaultdict(set)
    for r in rows_all:
        k = _sender_key(r["sender"])
        # A message whose run_date is not in the lattice cannot be placed in time, so it
        # is skipped rather than guessed at - never fabricate a position on the timeline.
        if not k or r["run_date"] not in idx:
            continue
        seen[k].add(idx[r["run_date"]])
        cats[k][r["category"] or "?"] += 1
        variants[k].add(r["sender"])
        if r["account"]:
            sender_accounts[k].add(r["account"])

    items, established = [], 0
    for k, days in seen.items():
        # THE LATTICE IS PER SENDER, built from the runs that covered the mailbox(es) this
        # sender writes to. Positions are re-derived against that shorter sequence, so a
        # gap counts observations rather than calendar days on which somebody else's
        # mailbox was being backfilled.
        covered = set()
        for acct in sender_accounts.get(k, ()):
            covered |= per_account_days.get(acct, set())
        # No known mailbox means no basis for narrowing, so the full sequence stands. That
        # is the old behaviour, and it is the right fallback: narrowing to nothing would
        # make every sender vanish from the panel, which is silence about silence.
        lattice = [d for d in run_days if d in covered] if covered else list(run_days)
        if len(lattice) < 2:
            continue
        # The DATES this sender appeared on, and the last date its mailbox was looked at.
        # Everything below is calendar arithmetic on those, not index arithmetic on the
        # lattice - the lattice's job is to decide WHETHER we looked, not to supply a unit.
        seen_dates = sorted({run_days[i] for i in days if run_days[i] in covered}
                            or {run_days[i] for i in days})
        if len(seen_dates) < MIN_OBS:
            continue
        span_days = _days_between_dates(seen_dates[0], seen_dates[-1])
        if span_days is None or span_days < MIN_SPAN_DAYS:
            continue
        established += 1
        gaps = _day_gaps(seen_dates)
        if not gaps:
            continue
        worst = max(gaps)
        silence = _days_between_dates(seen_dates[-1], lattice[-1])
        if silence is None:
            continue
        # BOTH tests, not either. Longer than it has ever been quiet AND long enough to
        # matter - a sender whose worst gap was two days clears any multiple you like the
        # moment it pauses over a weekend, and a 1.25x reading is rounding, not an anomaly.
        if silence <= worst or silence < MIN_SILENCE_DAYS:
            continue
        if worst and silence < worst * MIN_RATIO:
            continue
        cat = cats[k].most_common(1)[0][0]
        items.append({
            "sender": k,
            "category": cat,
            "concept": concepts.concept_of(cat),
            "weight": 2 if cat in MONEY_CATS else (1.5 if cat in GUARD_CATS else 1.0),
            "silent_days": silence,
            "gap_unit": "days",
            # WHAT THE SILENCE IS OUT OF. Every sender is measured against a different
            # window now - the days its own mailbox was looked at - so a bare number beside
            # a global run count invites a division that is not true of anything.
            "observed_days": len(lattice),
            "window_days": _days_between_dates(lattice[0], lattice[-1]),
            "last_looked": lattice[-1],
            "worst_gap": worst,
            "median_gap": statistics.median(gaps),
            "ratio": round(silence / float(worst), 2) if worst else None,
            "observations": len(seen_dates),
            "last_seen": seen_dates[-1],
            "first_seen": seen_dates[0],
            "variants": sorted(variants[k]),
        })

    # Money and guard senders outrank promo noise at equal overdue-ness; within a weight,
    # the most anomalous first.
    items.sort(key=lambda it: (-it["weight"], -(it["ratio"] or 0)))
    show_all = (q.get("include", [""])[0] or "") == "all"
    hidden_social = sum(1 for it in items if it["concept"] == SOCIAL_CONCEPT)
    if not show_all:
        items = [it for it in items if it["concept"] != SOCIAL_CONCEPT]
    window = _days_between_dates(run_days[0], run_days[-1]) or 0
    return {
        "items": items,
        # Reported, never silently dropped: a hidden count is the difference between "we
        # found nothing else" and "we are not showing you the rest".
        "hidden_social": 0 if show_all else hidden_social,
        "reach": {
            "runs": len(run_days),
            "window_days": window,
            "first_run": run_days[0],
            "last_run": run_days[-1],
            "senders_total": len(seen),
            "established": established,
            "min_obs": MIN_OBS,
            "min_span_days": MIN_SPAN_DAYS,
            "min_silence_days": MIN_SILENCE_DAYS,
            "min_ratio": MIN_RATIO,
            # DERIVED, not asserted. The old hard-coded "monthly billers cannot qualify"
            # was true when written and false by the time anyone read it.
            "monthly_observable": window >= MONTHLY_OBSERVABLE_DAYS,
        },
    }


# A profile needs this many messages behind it before "I have never seen that host" is
# evidence of anything. Below it, an unknown host is reported as UNKNOWN rather than as
# suspicious - and, just as importantly, a KNOWN host is not treated as vouched for. If the
# first message from a sender is the phish, a one-message profile would let it write its
# own permission slip.
PROFILE_MIN_MESSAGES = 4


def sender_host_profile(conn, sender):
    """What hosts does this sender normally link to, and how much evidence is behind it?"""
    key = _sender_key(sender)
    if not key:
        return {"key": None, "messages": 0, "established": False, "hosts": {}}
    row = conn.execute("SELECT messages FROM sender_profile WHERE sender_key = ?",
                       (key,)).fetchone()
    n = row["messages"] if row else 0
    hosts = {r["host"]: r["messages"] for r in conn.execute(
        "SELECT host, messages FROM sender_hosts WHERE sender_key = ?", (key,))}
    return {"key": key, "messages": n, "established": n >= PROFILE_MIN_MESSAGES,
            "hosts": hosts}



def _backend_for_account(account):
    """Which backend serves this mailbox, per config - or None if it is not configured.

    Read here rather than inferred, so the viewer and `doctor` cannot disagree about what
    an account is. Failing to None means "carry on and try", which is the old behaviour and
    the safe direction: a misread config must not make a working mailbox unreadable.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import providers                                            # noqa: PLC0415
        with open(ACCOUNTS_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        for acct in cfg.get("accounts") or []:
            if str(acct.get("email", "")).lower() == account.lower():
                return providers.backend_of(acct)
    except Exception:
        return None
    return None


CRLF = "\r\n"


def _as_mime_bytes(stored):
    """A stored body as parseable MIME, whether the connector gave us MIME or plain text.

    Connectors differ: a few can hand over the raw message, most can only give the text or
    HTML body. Both are accepted, and the difference is resolved HERE rather than being a
    documented burden on every connector author - a seam only works if it is easy to write
    against.
    """
    text = stored if isinstance(stored, str) else str(stored)
    head = text.lstrip()[:2000].lower()
    if head.startswith(("received:", "from:", "message-id:", "mime-version:",
                        "content-type:", "date:", "subject:", "return-path:")):
        return text.encode("utf-8", "replace")            # already a whole message
    looks_html = any(t in head for t in ("<html", "<body", "<div", "<table", "<p>"))
    return ("MIME-Version: 1.0" + CRLF
            + "Content-Type: %s; charset=utf-8" % ("text/html" if looks_html
                                                   else "text/plain")
            + CRLF + CRLF + text).encode("utf-8", "replace")


def api_message(conn, q):
    """Fetch ONE message by Message-ID and return it already made safe.

    The browser never receives raw email markup. Sanitising happens here, server-side,
    because a client-side sanitiser can be bypassed by whatever renders it first and
    because the raw bytes would then already be in the page. What goes back is:

      * `text`   - the text/plain part, which is the DEFAULT view and cannot do anything
      * `html`   - a fully sanitised document, only if the caller asks for it
      * `report` - what was removed, so the reader can see the message's intent even when
                   nothing is displayed: 30 blocked images and 5 tracking hosts is itself
                   information about who is writing to you

    Opening a message here NEVER marks it read (mailtool find uses BODY.PEEK), and nothing
    in this path resolves a URL, loads a remote part, or follows a redirect.
    """
    import email as _email
    import subprocess
    import tempfile
    from email import policy as _policy

    mid = (q.get("message_id") or [""])[0].strip()
    account = (q.get("account") or [""])[0].strip()
    want_html = (q.get("html") or ["0"])[0] == "1"
    if not mid or not account:
        return {"ok": False, "error": "message_id and account are required"}

    # BRANCH ON THE BACKEND BEFORE SPAWNING ANYTHING.
    #
    # A connector account is a third case and it used to land in the first branch, so the
    # viewer rendered "not found in this mailbox" over an explanation that said, correctly,
    # that nothing here fetches this account. The headline contradicted its own detail -
    # and "not found" is not a finding when nothing was searched. That is the same
    # absence-reported-as-fact this endpoint was rewritten to remove, surviving in the one
    # place the tool has the MOST certainty about what happened, because the answer is
    # known before any subprocess runs.
    #
    # `doctor` already says NOT FETCHED HERE. Same vocabulary here.
    backend = _backend_for_account(account)

    # A STORED BODY IS USED WHATEVER THE BACKEND IS.
    #
    # This used to live inside the connector branch alone, on the reasoning that a connector
    # install has no fetcher and therefore needs it. True, and the wrong place to put it: the
    # value of a stored body has nothing to do with which backend the account uses. A message
    # is immutable, so re-fetching one buys no freshness - it buys a network round trip, a
    # subprocess, and a hard dependency on the mail still being where it was.
    #
    # The consequence was that a backfill of `body_text` across an IMAP store changed
    # NOTHING: every row had its body sitting in the column and every open still went to the
    # network to get a second copy. It would have kept working right up until it didn't, and
    # the failure would arrive years later against mail nobody can retrieve any more.
    #
    # Stored first, fetch second. The fetch is the fallback now, not the default.
    row = conn.execute(
        "SELECT web_link, body_text FROM messages WHERE message_id = ? "
        "AND account = ? ORDER BY id DESC LIMIT 1", (mid, account)).fetchone()
    stored_body = (row["body_text"] if row else None) or ""
    if stored_body.strip():
        prefetched = _as_mime_bytes(stored_body)
    elif backend == "connector":
        # NO STORED BODY AND NO FETCHER. Nothing here can go looking, and saying "not found"
        # would report an absence nobody searched for - the failure this endpoint was
        # rewritten to remove, in the one place the tool has the MOST certainty about what
        # happened. `doctor` says NOT FETCHED HERE; same vocabulary.
        prefetched = None
        return {
            "ok": False,
            "reason": "no_local_fetcher",
            "searched": False,
            "error": "this account has no local fetcher",
            "detail": ("Declared as fetched elsewhere, so nothing here went looking. "
                       "That is the configuration working, not a fault - and it says "
                       "nothing about whether the message exists."),
            "hint": ("Supply `body_text` at ingest to read messages in the sandboxed "
                     "viewer, or `web_link` to open them in your mail client."),
            "web_link": (row["web_link"] if row else None),
        }
    else:
        prefetched = None

    if prefetched is not None:
        # Nothing spawned, no socket opened, nothing written to disk: the body
        # was already in the store.
        raw = prefetched
    else:
        tool = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
        tmp = os.path.join(tempfile.gettempdir(), "mv_%s.eml" % abs(hash(mid + account)))
        try:
            # encoding is stated EXPLICITLY: subprocess(text=True) on Windows decodes with
            # cp1252, so any non-ascii byte in a subject or error line raises UnicodeDecodeError
            # and the whole request fails for a reason that has nothing to do with the mail.
            p = subprocess.run(
                [sys.executable, tool, "find", "--account", account,
                 "--message-id", mid, "--out", tmp],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, creationflags=_NO_WINDOW)
            # TWO OUTCOMES THAT MEAN OPPOSITE THINGS, and they used to share one message.
            #
            # "not found in this mailbox" was returned whether the tool searched and found
            # nothing, or never ran at all - no app registration, no token, bad config, network
            # down. On an install where the fetcher cannot connect, every row reported the mail
            # as absent while it sat in the inbox untouched, and the UI added "trashed mail is
            # recoverable for about 30 days" on top, inviting the reader to conclude it had been
            # deleted and might be gone. Two false statements about someone's data, in the
            # reassuring direction, from a lookup that never happened.
            #
            # `find` exits 3 for a real miss and something else when it could not get that far,
            # so the two are distinguishable. `detail` was always captured here and never shown;
            # on the unreachable path it is the only thing that says what actually went wrong.
            if p.returncode == 3:
                return {"ok": False, "reason": "not_found", "searched": True,
                        "error": "not found in this mailbox",
                        "detail": (p.stderr or "")[:400]}
            if p.returncode != 0 or not os.path.exists(tmp):
                return {"ok": False, "reason": "unreachable", "searched": False,
                        "error": "could not reach the mailbox - the message may still be there",
                        "detail": ((p.stderr or "") + (p.stdout or ""))[-600:],
                        "hint": "run `python tools/mailtool.py doctor` to see why the mail "
                                "backend is failing. Nothing was searched, so this says "
                                "nothing about whether the message still exists."}
            raw = open(tmp, "rb").read()
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timed out talking to the mail server"}
        except Exception as e:
            return {"ok": False, "error": "could not retrieve: %s" % e}
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    msg = _email.message_from_bytes(raw, policy=_policy.default)
    text_part, html_part, attachments = None, None, []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        if fn:
            # Attachments are LISTED, never decoded, never written, never served.
            attachments.append({"name": str(fn)[:120],
                                "type": part.get_content_type(),
                                "size": len(part.get_payload(decode=True) or b"")})
            continue
        try:
            body = part.get_content()
        except Exception:
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        ct = part.get_content_type()
        if ct == "text/plain" and text_part is None:
            text_part = body
        elif ct == "text/html" and html_part is None:
            html_part = body

    hdr_from = str(msg.get("From") or "")
    profile = sender_host_profile(conn, hdr_from)
    safe_html, report = (None, None)
    if html_part:
        cleaned, report = mailview.sanitize_html(html_part)
        if want_html:
            theme = (q.get("theme") or ["reader"])[0].strip().lower()
            if theme not in ("reader", "dark", "light"):
                theme = "reader"
            body = (mailview.render_reader(cleaned, sender=hdr_from, profile=profile)
                    if theme == "reader" else cleaned)
            safe_html = mailview.wrap_document(body, theme=theme)

    hdr = lambda n: str(msg.get(n) or "")
    return {
        "ok": True,
        "headers": {
            "from": hdr("From"), "to": hdr("To"), "subject": hdr("Subject"),
            "date": hdr("Date"), "reply_to": hdr("Reply-To"),
            "return_path": hdr("Return-Path"), "list_unsubscribe": hdr("List-Unsubscribe"),
            # The authentication verdict is the single most useful line for deciding
            # whether a message is really from who it claims - surface it, do not bury it.
            "authentication_results": hdr("Authentication-Results")[:600],
        },
        "text": text_part,
        "has_html": bool(html_part),
        "html": safe_html,
        "report": report,
        "attachments": attachments,
        "bytes": len(raw),
    }


def api_steam_sales(conn, q):
    # Active Steam wishlist sales (current knowledge). ?all=1 includes ended ones.
    include_ended = (q.get("all") or ["0"])[0] in ("1", "true", "yes")
    where = "" if include_ended else "WHERE active = 1"
    sales = rows(conn.execute(
        f"SELECT * FROM steam_sales {where} "
        "ORDER BY active DESC, discount_pct DESC, last_seen DESC"))
    active = [s for s in sales if s.get("active")]
    last_checked = max([s["last_checked"] for s in sales if s.get("last_checked")], default=None)
    return {"sales": sales, "active_count": len(active),
            "total_count": len(sales), "last_checked": last_checked}


def api_steam_refresh(conn, q):
    # Pull live prices from Steam's store API and retire ended sales, then return
    # the refreshed active set. Localhost-only; user-triggered from the panel.
    import steam_refresh
    cc = (q.get("cc") or ["us"])[0]
    result = steam_refresh.refresh(cc=cc)
    return {"refreshed": result, "result": api_steam_sales(conn, {})}


def api_whoami(conn, q):
    """Which dashboard is answering, and from where.

    The app name alone lets a launcher confirm the port is not somebody else's service. It
    is NOT enough for an installer: `start-dashboard.ps1` is deliberately polite and no-ops
    when the port already serves an email-dashboard, so a second install on the same machine
    started nothing, found the FIRST install answering, and reported success. Green, and
    about the wrong copy.

    `root` is the absolute path of the install that is actually serving, so a caller can
    check it got its own. Local-only by construction - this endpoint is unreachable off
    127.0.0.1 - so it discloses a path to someone who already has the filesystem.
    """
    return {"app": "email-dashboard", "name": "Email Routine Dashboard",
            "root": os.path.abspath(os.path.dirname(HERE)),
            "pid": os.getpid()}


def api_questions(conn, q):
    """What the tool still does not know about its owner, asked from their own mailbox.

    The generator lives in questions.py; this endpoint is the seam a skill and the dashboard
    both read, so a question asked in conversation and a question shown in the panel are the
    same question with the same id - answer it either way and it stops being asked.

    `total` is reported beside a capped list on purpose. A panel that shows six of twenty and
    says only "6" is the shape of understatement this project keeps finding: correct, and
    read as complete.
    """
    import questions                                              # noqa: PLC0415
    # RULES_FILE, not a second path lookup of my own. Two answers to "where are the rules?"
    # is one too many: the generator would suppress questions from one file while the rest
    # of the server wrote rules into another, and nothing would report the disagreement.
    rules = RULES_FILE
    try:
        items, total = questions.generate(
            conn, rules_path=rules, protected=load_protected()["names"],
            limit=int(q.get("limit", ["6"])[0] or 6))
    except sqlite3.OperationalError as exc:
        # A store from before this release has no answers table until init_db runs. Say so
        # rather than returning an empty list, which would read as "nothing to ask".
        return {"questions": [], "total": 0, "error": "store not migrated: %s" % exc}
    answered = len(questions._answered(conn))
    return {"questions": items, "total": total, "shown": len(items),
            "answered": answered,
            "placeholders_remain": _rules_placeholders(rules)}


def _rules_placeholders(path):
    """Sections of the rules file still carrying the shipped 'fill this in' text."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None                      # no file at all is a different state from an empty one
    return len(re.findall(r"_Fill this in\._", text, re.I))


def api_answer(conn, q, body=None):
    """Record an answer. POST only.

    Recording and APPLYING are separate on purpose, and this endpoint only records. Writing
    a rule the owner did not quite mean is the risky half of elicitation, so the text that
    would be written is shown and confirmed before anything touches the rules file - the
    same propose/dispose split apply_proposal.py already uses for mail.
    """
    from datetime import datetime                                  # noqa: PLC0415
    body = body or {}
    qid = (body.get("id") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not qid:
        return {"ok": False, "error": "no question id"}
    if not answer:
        # Deleting the row, not storing "": an unanswered question must stay askable.
        conn.execute("DELETE FROM answers WHERE question_id = ?", (qid,))
        conn.commit()
        return {"ok": True, "id": qid, "answered": False}
    # `written_to` is NOT taken from the caller any more. The page used to assert where the
    # answer would go and this recorded that claim verbatim, so the column read as "written"
    # for answers nothing had ever written. Measured live: twenty-one answers, every one
    # stamped `rules-and-policies.md`, and not one line in that file - the fold was a separate
    # command nobody had run. Somebody who sits down and answers twenty-one questions and
    # finds them ignored the next morning is entitled to be angry, and the record telling them
    # it worked is the part that makes it worse.
    conn.execute(
        "INSERT INTO answers (question_id, kind, question, evidence, answer, answered_at, "
        "written_to) VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(question_id) DO UPDATE SET "
        "answer = excluded.answer, answered_at = excluded.answered_at, written_to = NULL",
        (qid, body.get("kind"), body.get("question"),
         json.dumps(body.get("evidence") or {}, default=str), answer,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    applied, why, mine = _apply_answers_now(conn, qid)
    return {"ok": True, "id": qid, "answered": True, "applied": applied,
            "this_answer_wrote_a_rule": mine, "apply_note": why}


def _apply_answers_now(conn, qid=None):
    """Fold every recorded answer into the rules file NOW, and stamp only what landed.

    THE ANSWER IS THE RATIFICATION. `apply_answers.py` defaults to a dry run for a good
    reason - a rule nobody meant silently shapes every future sweep - but that gate exists to
    stop the AGENT writing rules unreviewed. Here the human has already reviewed: they read
    the question, saw the evidence, and typed the answer. Making them run a second command
    they were never told about turns their decision into a draft.

    Returns (count_written, note). Never raises: recording the answer is the important part
    and must survive a file that is read-only, missing, or open in an editor.
    """
    try:
        sys.path.insert(0, str(os.path.join(os.path.dirname(HERE), "tools")))
        import apply_answers as AA                                    # noqa: PLC0415
        block, skipped = AA.build_block(conn)
        with open(AA.RULES, encoding="utf-8", newline="") as f:
            raw = f.read()
        # The file's OWN line ending, or a single stray LF rewrites every line in the next diff.
        nl = "\r\n" if "\r\n" in raw else "\n"
        new = AA.splice(raw, block, nl)
        if new != raw:
            with open(AA.RULES, "w", encoding="utf-8", newline="") as f:
                f.write(new)
        # Stamp ONLY the rows that produced a line. An answer that correctly implies no rule
        # keeps written_to NULL, which is the truth about it, and the dashboard can then say
        # "recorded, implies no rule" rather than implying it was filed as policy.
        skipped_ids = {q for q, _ in skipped}
        n = 0
        for r in conn.execute("SELECT question_id FROM answers WHERE answer IS NOT NULL "
                              "AND TRIM(answer) != ''").fetchall():
            if r[0] in skipped_ids:
                continue
            conn.execute("UPDATE answers SET written_to = ? WHERE question_id = ?",
                         (str(AA.RULES.name), r[0]))
            n += 1
        conn.commit()
        # THE NOTE HAS TO BE ABOUT THE ANSWER THAT WAS JUST GIVEN, not about the file.
        #
        # It read "applied to rules-and-policies.md" unconditionally, so a person answering
        # "no - leave it as it is" was told a rule file had been written. `n` alone does not
        # fix that either: it counts every rule in the block, so the most conservative answer
        # available - the one that declines to hide anything - still returns a big number that
        # looks like it did something.
        #
        # So the reply says what THIS answer did, and 0 is reported as an ordinary outcome
        # rather than an error, because several answers legitimately imply no rule. Same defect
        # as the `written_to` column fixed above, surviving in the payload the person actually
        # reads: "your answer changed nothing" and "your answer was lost" must never look alike.
        mine = qid is not None and qid not in skipped_ids
        if qid is None:
            note = ("applied to %s" % AA.RULES.name) if n else "nothing to write"
        elif mine:
            note = "applied to %s (%d rule(s) now in force)" % (AA.RULES.name, n)
        else:
            note = ("recorded - this answer implies no rule, so nothing was written. The "
                    "other %d rule(s) in that file are unchanged." % n)
        return n, note, mine
    except Exception as e:                                            # noqa: BLE001
        return 0, "NOT applied (%s: %s) - your answer is recorded; run " \
                  "`python tools/apply_answers.py --write`" % (type(e).__name__, e), False


RESOLUTIONS = ("email", "off-channel", "declined", "expired")


def api_open_items(conn, q):
    """What is still outstanding, oldest first, with how long it has been outstanding.

    THE PANEL A BRIEF CANNOT BE. Every other view here answers "what arrived?" - which is
    the right question for a sweep and the wrong one for a person, because a task assigned
    three weeks ago arrived exactly once and has been invisible ever since. This is the only
    view whose contents get WORSE by being ignored, which is why it sorts by age rather
    than by importance: a two-day-old security item is less alarming than a three-week-old
    one nobody has touched.
    """
    state = (q.get("state", ["open"])[0] or "open").strip().lower()
    where = "" if state == "all" else "WHERE state = ?"
    args = () if state == "all" else (state,)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM open_items %s ORDER BY "
        "CASE state WHEN 'open' THEN 0 ELSE 1 END, first_seen ASC" % where, args)]
    today = datetime.now().date()
    # ACKNOWLEDGED AND STILL OPEN IS A REAL STATE, and it has to be visible or the panel
    # looks broken. An ack says "I have seen this" and an open item says "this is not done";
    # both can be true at once, and an owner looking at a row they know they acknowledged,
    # with nothing on screen admitting it, reasonably concludes the tool has lost track.
    acked_msg = acked_message_keys(conn)
    acked_thread = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'thread'")}
    # NOTHING IS GOING TO REMIND YOU ABOUT THIS ONE AGAIN. `days_since_seen` is the gap
    # between the newest run and the last run whose MAIL still carried this item, so it
    # separates the two ways an item can be open: one the sender keeps re-raising (a dunning
    # notice, a repeated alert - the mailbox itself will bring it back), and one that arrived
    # once and will never arrive again, where this list is now the only thing standing
    # between it and being forgotten.
    #
    # SAY EXACTLY THAT AND NOT MORE. `last_seen` advances only when the message reappears in
    # a run's mail (db.carry_open_items), and the daily fetch window is two days - so an item
    # nobody re-sends goes quiet after about three days BY CONSTRUCTION. This number is
    # therefore evidence about the SENDER's behaviour, not about whether the daily report
    # mentioned the item. The first draft of this comment claimed the latter; running it
    # against a real store is what caught the difference, because far more rows came back
    # "quiet" than that reading could survive. Nothing here records what a report said, so
    # nothing here may claim to.
    #
    # Written because an item can stay open and unacknowledged for weeks while the report
    # that used to mention it goes silent. This panel is the half of that failure the code
    # can actually see.
    #
    # MEASURED AGAINST THE NEWEST RUN, NOT AGAINST TODAY. If the sweep itself stops for a
    # week then nothing has been seen for a week, and dating this from the wall clock would
    # light up every row at exactly the moment the signal means nothing about the rows.
    newest_run = conn.execute("SELECT MAX(run_date) AS d FROM runs").fetchone()["d"]
    for r in rows:
        r["days_open"] = _days_between(r.get("first_seen"),
                                       r.get("resolved_at") or str(today))
        r["stale"] = bool(r["state"] == "open" and (r["days_open"] or 0) >= 14)
        gap = _days_between(r.get("last_seen") or r.get("first_seen"), newest_run)
        r["days_since_seen"] = gap
        # A closed item is SUPPOSED to stop appearing in runs, so silence about one is the
        # system working. Only an open item can go quiet.
        r["quiet"] = bool(r["state"] == "open" and (gap or 0) >= QUIET_AFTER_DAYS)
        # An open item stores its Message-ID in `key` only when kind == 'message'; the rest
        # of the identity is the same, so it goes through the ONE ack_covers implementation
        # with that field renamed rather than re-deriving the rule here (which is how the
        # family-escalation carve-out would otherwise have been missed on this panel).
        r["acknowledged"] = ack_covers(
            {"message_id": r["key"] if r["kind"] == "message" else None,
             "sender": r.get("sender"), "subject": r.get("subject"),
             "account": r.get("account"), "concept": r.get("concept"),
             "importance": r.get("importance")},
            acked_msg, acked_thread)
    # ACKNOWLEDGED LEAVES THE LIST. The distinction between "seen" and "done" is real and
    # the store still keeps both - but an owner may reasonably use acknowledging to mean
    # "I have dealt with this", and a panel that argues with its reader about what their own
    # gesture meant is a panel they stop using. The row is not deleted and not
    # resolved; it is just no longer counted as OUTSTANDING, and "show resolved" still has
    # it. Being right about a definition is worth less than being useful.
    acked_hidden = sum(1 for r in rows if r["state"] == "open" and r["acknowledged"])
    show_acked = (q.get("acked", ["0"])[0] or "0") == "1"
    if not show_acked:
        rows = [r for r in rows if not (r["state"] == "open" and r["acknowledged"])]
    n_open = sum(1 for r in rows if r["state"] == "open")
    ages = sorted(r["days_open"] for r in rows
                  if r["state"] == "open" and r["days_open"] is not None)
    # MEDIAN AGE, NOT LENGTH. A list whose median age climbs every week is being ignored
    # however short it is; one that churns is working however long it is. Length alone says
    # nothing, and a length target would push toward hiding things rather than closing them.
    median = ages[len(ages) // 2] if ages else 0

    # GROUPED BY WHO IS WAITING, because that is how the work actually gets done. Four asks
    # from one colleague is one conversation; four asks from four people is four.
    who = collections.Counter(
        _sender_key(r["sender"]) or (r["sender"] or "?")
        for r in rows if r["state"] == "open")
    return {
        "items": rows,
        "open": n_open,
        # Resolved-elsewhere reported separately, because it is the number that says the
        # tool is being told the truth. If it stays at zero while the open list grows,
        # people are closing things without a way to say so - and the list is on its way to
        # being ignored.
        "resolved_off_channel": sum(1 for r in rows
                                    if r.get("resolved_where") == "off-channel"),
        "oldest_days": max([r["days_open"] or 0 for r in rows if r["state"] == "open"],
                           default=0),
        "median_days": median,
        # The count that says "this list is being skimmed past" rather than "this list is
        # long". An item nobody has seen in a week is not being carried, it is being lost.
        "quiet": sum(1 for r in rows if r["state"] == "open" and r["quiet"]),
        "quiet_after_days": QUIET_AFTER_DAYS,
        "hidden_because_acknowledged": acked_hidden,
        "waiting_on_you_from": [{"who": k, "items": n} for k, n in who.most_common(8)],
        "resolutions": list(RESOLUTIONS),
        "state": state,
    }


def _days_between(a, b):
    from datetime import date                                      # noqa: PLC0415
    try:
        y1, m1, d1 = (int(x) for x in str(a)[:10].split("-"))
        y2, m2, d2 = (int(x) for x in str(b)[:10].split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except (ValueError, TypeError):
        # Unknown, not zero. A missing first_seen rendering as "0 days open" would make the
        # oldest item in the list look like the newest.
        return None


def api_resolve(conn, q, body=None):
    """Close an open item, or reopen one. POST only.

    `where` is the point of this endpoint. Most things that arrive by mail are finished
    somewhere this tool cannot see, and without somewhere to say so the only ways to clear
    an item are to lie about it or to leave it open forever. Both end with the list being
    ignored, which is the failure this whole tool is arguing against.
    """
    from datetime import datetime as _dt                           # noqa: PLC0415
    body = body or {}
    key = (body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "no key"}
    if not conn.execute("SELECT 1 FROM open_items WHERE key = ?", (key,)).fetchone():
        return {"ok": False, "error": "no open item with that key"}
    if body.get("open") is True:
        conn.execute("UPDATE open_items SET state = 'open', resolved_at = NULL, "
                     "resolved_where = NULL, resolved_note = NULL WHERE key = ?", (key,))
        conn.commit()
        return {"ok": True, "key": key, "state": "open"}
    where = (body.get("where") or "off-channel").strip().lower()
    # FOUR OUTCOMES, because three of them are not "done".
    #
    # A standing list whose only exit is completion becomes a graveyard, and a graveyard
    # teaches its reader to skim past the one live item. Reported from a live install: an
    # item nearly two hundred days old - a software-seat offer nobody was ever going to
    # take - with no way out that was not a lie.
    #
    #   email       finished here, in the mail
    #   off-channel finished somewhere this tool cannot see: a call, a chat, a corridor
    #   declined    a decision NOT to do it, which is a real answer and closes the item
    #   expired     the offer lapsed, the deadline passed, the moment is gone
    #
    # `moot` is the old spelling of `declined` and is still accepted so existing rows and
    # scripts keep working; it is not offered.
    if where == "moot":
        where = "declined"
    if where not in RESOLUTIONS:
        return {"ok": False,
                "error": "where must be one of %s - an unrecorded reason is how a "
                         "resolved list stops meaning anything" % ", ".join(RESOLUTIONS)}
    conn.execute(
        "UPDATE open_items SET state = 'resolved', resolved_at = ?, resolved_where = ?, "
        "resolved_note = ? WHERE key = ?",
        (_dt.now().isoformat(timespec="seconds"), where,
         (body.get("note") or "").strip()[:400] or None, key))
    conn.commit()
    return {"ok": True, "key": key, "state": "resolved", "where": where}


def api_scoreboard(conn, q):
    """The one number here that measures the OUTCOME rather than the activity.

    Everything else on this dashboard counts what the tool did - messages swept, rules
    written, items acknowledged - and all of it can rise while the thing the owner cares
    about gets worse. A reach is somebody giving up on the inbox and going elsewhere to
    another channel, which is the failure this whole tool exists to prevent, arriving with
    a timestamp.
    """
    import elsewhere                                                # noqa: PLC0415
    rows = [dict(r) for r in conn.execute(
        "SELECT sender, subject, COALESCE(msg_day, run_date) AS day FROM messages "
        "WHERE sender IS NOT NULL AND sender != ''")]
    out = elsewhere.scoreboard(rows, cfg=load_dashboard_cfg(),
                           protected=load_protected()["names"])
    # The guard list is the definition of "people who matter", so a scoreboard read while it
    # is empty is scoring against nobody. Say so rather than reporting a confident zero in
    # the column that carries the whole point.
    if not out["protected_known"]:
        out["who_matters_unknown"] = (
            "Nobody is on the protected list, so \"from people who matter\" cannot be "
            "counted. Fill it in and this column starts meaning something.")
    return out


API = {
    "/api/whoami": api_whoami,
    "/api/setup": api_setup,
    "/api/features": api_features,
    "/api/runs": api_runs,
    "/api/run": api_run,
    "/api/trash/stats": api_trash_stats,
    "/api/trash/list": api_trash_list,
    "/api/trash/senders": api_trash_senders,
    "/api/quiet": api_quiet,
    "/api/signins": api_signins,
    "/api/calendar": api_calendar,
    "/api/repeats": api_repeats,
    "/api/acks": api_acks,
    "/api/workflow-actions": api_workflow_actions,
    "/api/account": api_account,
    "/api/sender": api_sender,
    "/api/message": api_message,
    "/api/steam/sales": api_steam_sales,
    "/api/refusals": api_refusals,
    "/api/thin-evidence": api_thin_evidence,
    "/api/retention-shelf": api_retention_shelf,
    "/api/steam/refresh": api_steam_refresh,
    "/api/new-hosts": api_new_hosts,
    "/api/questions": api_questions,
    "/api/open-items": api_open_items,
    "/api/scoreboard": api_scoreboard,
}

# Writing endpoints are a SEPARATE table, reachable only via do_POST and only after the
# CSRF guards. Keeping them out of API means a GET can never trigger a write, however the
# URL is reached - a link, a prefetch, an image tag.
WRITE_API = {
    "/api/ack": api_ack,
    "/api/protected-names": api_protected_names,
    "/api/sender-rule": api_sender_rule,
    "/api/host-review": api_host_review,
    "/api/answer": api_answer,
    "/api/resolve": api_resolve,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """The only writing endpoint, and it is CSRF-guarded on purpose.

        This server binds 127.0.0.1, which people treat as "safe" - but localhost is
        reachable by ANY page the browser happens to be on. A hostile site cannot read the
        response (no CORS headers are sent) yet a plain form POST would still FIRE, so an
        unguarded write endpoint would let any web page mark the CEO's mail as
        acknowledged and silence it. Two cheap, independent guards:

          * a custom header, which a cross-origin request cannot set without a preflight
            this server never approves; and
          * an Origin/Referer check, so even a same-site-shaped request has to come from
            this page.

        Both must pass. The action is low-stakes, but "it's only localhost" is exactly the
        reasoning that leaves a writable endpoint open to the whole web.
        """
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            return self._send(404, {"error": "unknown endpoint"})
        if self.headers.get("X-Dashboard") != "1":
            return self._send(403, {"error": "missing dashboard header"})
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin and not re.match(r"https?://(127\.0\.0\.1|localhost)(:\d+)?/?", origin):
            return self._send(403, {"error": "cross-origin write refused"})
        handler = WRITE_API.get(path)
        if not handler:
            return self._send(404, {"error": "not a writable endpoint"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            return self._send(400, {"error": "bad JSON body: %s" % e})
        conn = db.connect()
        try:
            return self._send(200, handler(conn, parse_qs(parsed.query), payload))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        finally:
            conn.close()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            handler = API.get(path)
            if not handler:
                # SAY WHEN IT IS THE VERB, not the route. A GET on a write-only endpoint used
                # to answer "unknown endpoint", which reads as a missing guard at exactly the
                # moment somebody is anxious about the guard - a field tester hit this checking
                # /api/protected-names right after editing the protected list, and briefly
                # thought the protection had disappeared. The route exists; only the method is
                # wrong, and saying so ends that reading immediately.
                # LOCALHOST-ONLY CUTS ONE WAY AND NOT THE OTHER, and the difference decides
                # this line (owner, 2026-08-08: "this is a private website on localhost, it is
                # not ever intended to be publicly accessible").
                #
                # It DOES settle information disclosure. Naming an endpoint that exists tells
                # a remote attacker nothing, because there is no remote attacker: nobody off
                # this machine can reach the port to read the message. So the choice between a
                # vague 404 and a helpful 405 is decided purely on which one helps the person
                # reading it, and vagueness helps nobody here.
                #
                # It does NOT settle CSRF, and the guards above stay exactly as they are. Any
                # page in the owner's browser can issue requests to 127.0.0.1 - that is the
                # whole point of the header and Origin checks, and "it's only localhost" is
                # the reasoning that would remove them. Being unreachable from outside the
                # machine and being unreachable from a tab the owner happens to have open are
                # different properties, and only the first one is true.
                if path in WRITE_API:
                    return self._send(405, {
                        "error": "POST only", "endpoint": path,
                        "detail": "this endpoint exists but is write-only; GET is not "
                                  "supported. Nothing is wrong with your configuration."})
                return self._send(404, {"error": "unknown endpoint"})
            conn = db.connect()
            try:
                return self._send(200, handler(conn, parse_qs(parsed.query)))
            except ValueError as e:
                # A bad filter value is the CALLER's mistake, not a server fault. It gets a 400
                # that names what was wrong and what is valid, so an unknown filter can never be
                # mistaken for "no results".
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
            finally:
                conn.close()
        # static
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            self._send(200, f.read(), CONTENT_TYPES.get(ext, "application/octet-stream"))


class Server(ThreadingHTTPServer):
    # Don't set SO_REUSEADDR. On Windows that flag lets a second process bind an
    # already-used 127.0.0.1:PORT and silently steal traffic; we'd rather fail loudly
    # if the port is taken than coexist with (or hijack) another app's port.
    allow_reuse_address = False


def _registry_port(key, fallback):
    """Read a service's port from a shared port registry (system/ports.json under $MB_HOME),
    falling back to the given default if no registry is configured or it is unreadable.

    The registry location comes from the environment and nothing else. A default path here
    would be one machine's layout compiled into the program - dead for everyone else, and a
    small disclosure of the author's disk on a public repo."""
    try:
        import json, os
        mb = os.environ.get("MB_HOME", "")
        if not mb:
            return fallback
        with open(os.path.join(mb, "system", "ports.json"), encoding="utf-8") as f:
            for s in (json.load(f).get("services") or []):
                if s.get("key") == key and s.get("port"):
                    return int(s["port"])
    except Exception:
        pass
    return fallback


def main():
    ap = argparse.ArgumentParser()
    # Reserved port 9770 — kept well clear of the spatial/power worker band (8765)
    # and the GPU webgui (8081) so isolated worker instances never collide with it.
    ap.add_argument("--port", type=int, default=_registry_port("email-dashboard", 9770))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    db.init_db()
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"Could not bind {args.host}:{args.port} — is it already in use by another app? ({e})")
        raise SystemExit(1)
    print(f"Email dashboard running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        srv.shutdown()


if __name__ == "__main__":
    main()
