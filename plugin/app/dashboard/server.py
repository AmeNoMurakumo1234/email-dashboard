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
from datetime import datetime
import os
import re
import statistics
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import concepts
import db
import mailview
from categorize import LABELS

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

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

    surfaced = [m for m in messages if m["disposition"] in db.DELIBERATELY_KEPT]
    trashed = [m for m in messages if m["disposition"] in db.DISPOSABLE]
    return {"run_date": date,
        "accounts_as_of": accounts_as_of, "run": run, "accounts": accounts,
            "surfaced": surfaced, "trashed": trashed,
            "totals": {"fetched": run.get("fetched", 0), "trashed": run.get("trashed", 0),
                       "kept": run.get("kept", 0), "otp": run.get("otp", 0)}}


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
        m["acked"] = bool(any(i in acked_msg for i in ids) or kt in acked_thread)
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
    """Pull the actionable link and the appointment time out of one workflow message."""
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

    seen, items, errors = set(), [], []
    tool = os.path.join(os.path.dirname(HERE), "tools", "mailtool.py")
    for r in rows_:
        addr = (email_utils.parseaddr(r["sender"])[1] or "").lower()
        kind = senders.get(addr)
        if not kind or r["message_id"] in seen:
            continue
        seen.add(r["message_id"])
        if len(items) >= 25:
            break
        tmp = os.path.join(tempfile.gettempdir(),
                           "va_%s.eml" % abs(hash(r["message_id"])))
        try:
            p = subprocess.run(
                [sys.executable, tool, "find", "--account", r["account"],
                 "--message-id", r["message_id"], "--out", tmp],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=45)
            if p.returncode != 0 or not os.path.exists(tmp):
                errors.append({"subject": r["subject"], "why": "not on the server"})
                continue
            info = _workflow_extract(open(tmp, "rb").read(), link_ok, domain)
        except Exception as e:
            # NEVER swallow this silently. The first version had a bare `continue` here and
            # a missing module-level import made EVERY message raise - so the panel showed
            # a clean, confident "0 actions" while the real answer was that it had not
            # managed to read a single one. A panel about time-critical mail must not be
            # able to report an all-clear it did not earn.
            errors.append({"subject": r["subject"], "why": "%s: %s" % (type(e).__name__, e)})
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        ws = _workflow_when_state(info["when"], horizon)
        items.append({
            "kind": kind[0], "kind_label": kind[1],
            "run_date": r["run_date"], "account": r["account"],
            "sender": r["sender"], "subject": info["subject"],
            "message_id": r["message_id"],
            "when": info["when"], "auth_ok": info["auth_ok"],
            "state": ws["state"], "days_until": ws["days_until"],
            "when_date": ws.get("when_date"),
            "primary": info["primary"], "links": info["links"][:8],
            "acked": r["message_id"] in acked_msg,
            # Clickable ONLY with a verified sender AND a destination inside the domain.
            "safe_to_click": bool(info["auth_ok"] and info["primary"]
                                  and info["primary"]["host_in_domain"]),
        })
    # Outstanding = not acknowledged, not past, and close enough to act on. `upcoming` is
    # deliberately NOT outstanding: it is real and it is kept, it just does not shout yet.
    outstanding = [i for i in items
                   if not i["acked"] and i["state"] in ("now", "today", "soon")]
    upcoming = [i for i in items if not i["acked"] and i["state"] == "upcoming"]
    # The client renders "not a <domain> host" and must not carry its own copy of what
    # that domain is - it is configuration, and a second spelling of it is a second thing
    # to get wrong the day someone changes it.
    return {"items": items, "lattice": "per-account", "days": days, "horizon": horizon, "errors": errors,
            "candidates": len(seen), "domain": domain,
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
    return {"panels": panels}


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
            "SELECT %s day, account, sender, subject, message_id FROM messages WHERE "
            "importance IN (%s)" % (col, ",".join("?" * len(ATTENTION))), ATTENTION):
        act[r["day"]] += 1
        # Acknowledged counts as handled at either scope - a thread ack covers this run's
        # instance of a recurring notice just as a message ack covers the single email.
        done = (any(k in acked_msg for k in
                    ack_identities("message", r["message_id"], r["sender"], r["subject"],
                                   r["account"]))
                or ack_key("thread", None, r["sender"], r["subject"],
                           r["account"] if "account" in r.keys() else None)
                in acked_thread)
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
    if backend == "connector":
        row = conn.execute(
            "SELECT web_link, body_text FROM messages WHERE message_id = ? "
            "AND account = ? ORDER BY id DESC LIMIT 1", (mid, account)).fetchone()
        stored_body = (row["body_text"] if row else None) or ""
        # THE SANITISING READER, ON AN INSTALL WITH NO FETCHER. If the connector supplied a
        # body at ingest there is nothing to fetch: the text-first view, the blocked
        # images and the tracking-host report are the whole point of this tool and were
        # unreachable for every row on this class of install. Fed into the SAME
        # parse-and-sanitise path below rather than a second one - a second rendering route
        # is a second place for image blocking to be subtly different, and the one nobody
        # tests is the one that leaks.
        prefetched = _as_mime_bytes(stored_body) if stored_body.strip() else None
        if prefetched is None:
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
                timeout=60)
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
    conn.execute(
        "INSERT INTO answers (question_id, kind, question, evidence, answer, answered_at, "
        "written_to) VALUES (?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET "
        "answer = excluded.answer, answered_at = excluded.answered_at",
        (qid, body.get("kind"), body.get("question"),
         json.dumps(body.get("evidence") or {}, default=str), answer,
         datetime.now().isoformat(timespec="seconds"), body.get("written_to")))
    conn.commit()
    return {"ok": True, "id": qid, "answered": True}


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
    for r in rows:
        r["days_open"] = _days_between(r.get("first_seen"),
                                       r.get("resolved_at") or str(today))
        r["stale"] = bool(r["state"] == "open" and (r["days_open"] or 0) >= 14)
        ids = ack_identities("message",
                             r["key"] if r["kind"] == "message" else None,
                             r.get("sender"), r.get("subject"), r.get("account"))
        r["acknowledged"] = bool(
            any(i in acked_msg for i in ids)
            or ack_key("thread", None, r.get("sender"), r.get("subject"),
                       r.get("account")) in acked_thread)
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
    "/api/calendar": api_calendar,
    "/api/repeats": api_repeats,
    "/api/acks": api_acks,
    "/api/workflow-actions": api_workflow_actions,
    "/api/account": api_account,
    "/api/sender": api_sender,
    "/api/message": api_message,
    "/api/steam/sales": api_steam_sales,
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
