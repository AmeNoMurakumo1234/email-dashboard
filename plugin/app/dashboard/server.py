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
    messages = rows(conn.execute(
        "SELECT * FROM messages WHERE run_id = ? ORDER BY "
        "CASE disposition WHEN 'surfaced' THEN 0 WHEN 'kept' THEN 1 ELSE 2 END, account",
        (run_id,))) if run_id else []
    # Mark what has already been acknowledged. Acknowledged items are NOT removed - the row,
    # the reason and the paper trail all stay - they simply stop competing for attention,
    # which is the other half of the drowning problem.
    annotate_acks(conn, messages)

    surfaced = [m for m in messages if m["disposition"] in ("surfaced", "kept")]
    trashed = [m for m in messages if m["disposition"] == "trashed"]
    return {"run_date": date, "run": run, "accounts": accounts,
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
    return "%s|%s" % (_sender_key(sender) or "", subject_shape(subject))


def annotate_acks(conn, msgs):
    """Attach the ack keys and current state to each row, so the client never guesses."""
    acked_msg = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'message'")}
    acked_thread = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'thread'")}
    for m in msgs:
        km = ack_key("message", m.get("message_id"), m.get("sender"), m.get("subject"),
                     m.get("account"))
        kt = ack_key("thread", None, m.get("sender"), m.get("subject"))
        m["ack_key_message"], m["ack_key_thread"] = km, kt
        m["acked"] = bool(km in acked_msg or kt in acked_thread)
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


def api_ack(conn, q, body=None):
    """Acknowledge (or un-acknowledge) an item. POST only.

    `on: false` lifts it - an acknowledgement is a statement about attention, not a
    deletion, and a mistaken one has to be reversible.
    """
    body = body or {}
    kind = (body.get("kind") or "message").strip()
    if kind not in ("message", "thread"):
        return {"ok": False, "error": "kind must be 'message' or 'thread'"}
    key = ack_key(kind, body.get("message_id"), body.get("sender"), body.get("subject"),
                  body.get("account"))
    if not key or key in ("|", "row:||"):
        return {"ok": False, "error": "nothing identifiable to acknowledge"}
    if body.get("on") is False:
        conn.execute("DELETE FROM acks WHERE kind = ? AND key = ?", (kind, key))
        conn.commit()
        return {"ok": True, "kind": kind, "key": key, "acked": False}
    conn.execute(
        "INSERT INTO acks (kind, key, account, sender, subject, note, acked_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
        "note = excluded.note, acked_at = excluded.acked_at",
        (kind, key, body.get("account"), body.get("sender"), body.get("subject"),
         (body.get("note") or "").strip()[:400], db.now_iso()))
    conn.commit()
    return {"ok": True, "kind": kind, "key": key, "acked": True}


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
    return {"items": items, "days": days, "horizon": horizon, "errors": errors,
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

    return {"steps": steps,
            "complete": all(s["done"] for s in steps),
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


def sender_rule_verdict(conn, key):
    """Is this sender safe to lock to auto-trash, and is the evidence strong enough?

    Judged HERE, from the store, never from what the browser asserts. A click is a request;
    the entitlement to change standing policy has to be re-derived server-side or the guard
    is only as good as the page that called it.
    """
    prot = load_protected()
    if not prot["configured"]:
        # No guard list, no rule writing. Refusing is the only safe reading of a missing
        # protection file; the alternative is a button that can silence anyone.
        return {"eligible": False, "configured": False,
                "why": "no protected-sender config (config/protected.local.json): "
                       "refusing to write any auto-trash rule. " + prot["why"]}

    rows_ = rows(conn.execute(
        "SELECT sender, disposition, COALESCE(concept,'') concept, run_date, importance "
        "FROM messages WHERE sender IS NOT NULL AND sender != ''"))
    mine = [r for r in rows_ if _sender_key(r["sender"]) == key]
    if not mine:
        return {"eligible": False, "why": "no messages recorded for that sender"}
    total = len(mine)
    binned = sum(1 for r in mine if r["disposition"] == "trashed")
    runs_ = len({r["run_date"] for r in mine})
    concepts_seen = {r["concept"] for r in mine if r["concept"]}
    variants = sorted({r["sender"] for r in mine})

    reasons = []
    if binned != total:
        reasons.append(f"kept or surfaced {total - binned} of {total} - not pure noise")
    if total < prot["min_messages"]:
        reasons.append(f"only {total} messages; {prot['min_messages']} needed")
    hit = concepts_seen & prot["concepts"]
    if hit:
        reasons.append("protected category: " + ", ".join(sorted(hit)))
    if protected_hit(prot, key) or any(protected_hit(prot, v) for v in variants):
        reasons.append("on your protected-sender list")
    if any((r["importance"] or "") in ("action-needed", "family", "security", "financial")
           for r in mine):
        reasons.append("has been flagged as needing attention before")

    return {"eligible": not reasons, "why": "; ".join(reasons) or "",
            "total": total, "binned": binned, "runs": runs_,
            "variants": variants, "concepts": sorted(concepts_seen)}


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
        "binned": sum(1 for r in mine if r["disposition"] == "trashed"),
        "kept": sum(1 for r in mine if r["disposition"] != "trashed"),
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


def _already_ruled(key):
    try:
        raw, _ = _read_rules()
    except OSError:
        return False
    return ("<!-- dashboard-rule:%s -->" % key) in raw


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
    if not key:
        return {"ok": False, "error": "key is required"}
    # Eligibility FIRST, so the refusal names the reason that matters. Reading the rules
    # file first meant a fresh install refused with "cannot read the rules file" when the
    # real and more important answer was "you have not told me who is protected yet" -
    # a true refusal for a misleading reason is still a bad error message.
    verdict = sender_rule_verdict(conn, key)
    if body.get("on") is not False and not verdict["eligible"]:
        return {"ok": False, "error": "not eligible: " + verdict["why"], "verdict": verdict}

    try:
        text, nl = _read_rules()
    except OSError as e:
        return {"ok": False,
                "error": "no rules file yet (%s). Copy rules-and-policies.example.md to "
                         "rules-and-policies.md to start one." % type(e).__name__}
    marker = "<!-- dashboard-rule:%s -->" % key

    if body.get("on") is False:
        if marker not in text:
            return {"ok": False, "error": "no dashboard-written rule for that sender"}
        kept = [ln for ln in text.splitlines() if marker not in ln]
        _write_rules(kept, nl)
        return {"ok": True, "key": key, "ruled": False}

    if not verdict["eligible"]:
        return {"ok": False, "error": "not eligible: " + verdict["why"], "verdict": verdict}
    if marker in text:
        return {"ok": True, "key": key, "ruled": True, "note": "already ruled"}

    today = db.now_iso()[:10]
    label = (body.get("label") or key)[:60]
    row = ("| %s (auto-trash, confirmed from the dashboard) | %s | Binned %d of %d "
           "messages across %d runs with none ever kept - locked on that evidence. "
           "Lift it from the sender panel. %s |"
           % (label, today, verdict["binned"], verdict["total"], verdict["runs"], marker))

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
    """One cell per run day: volume, and what the day was mostly ABOUT.

    The run history was a dropdown of dates - the least evocative possible rendering of
    everything this lane has done. As a grid it shows at a glance the quiet stretches, the
    spikes, and the weeks something was escalating: patterns no single run report can
    express and no table makes visible.
    """
    days = rows(conn.execute(
        "SELECT run_date, COUNT(*) n, "
        "SUM(disposition='trashed') trashed, "
        "SUM(disposition IN ('kept','surfaced','saved')) kept "
        "FROM messages GROUP BY run_date ORDER BY run_date"))
    # dominant concept per day, so the tint means something rather than being decoration
    dom = {}
    for r in conn.execute(
            "SELECT run_date, COALESCE(concept,'unmapped') c, COUNT(*) n FROM messages "
            "GROUP BY run_date, c ORDER BY run_date, n DESC"):
        dom.setdefault(r["run_date"], concepts.key_of(r["c"]))
    # What actually earned attention that day - the reason to click a cell.
    #
    # The importance column has NINE spellings (action-needed, family, security, financial,
    # info, low, normal, fyi, routine) - the same one-concept-many-spellings drift that hit
    # the category labels and the sender strings. The first version matched only two of
    # them, so days whose sole notable item was a SECURITY notice or a FINANCIAL one did not
    # ring at all. Match the whole attention set, not the two that came to mind.
    ATTENTION = ("action-needed", "family", "security", "financial")
    acked_msg = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'message'")}
    acked_thread = {r["key"] for r in conn.execute(
        "SELECT key FROM acks WHERE kind = 'thread'")}
    act, open_act = collections.Counter(), collections.Counter()
    for r in conn.execute(
            "SELECT run_date, account, sender, subject, message_id FROM messages WHERE "
            "importance IN (%s)" % ",".join("?" * len(ATTENTION)), ATTENTION):
        act[r["run_date"]] += 1
        # Acknowledged counts as handled at either scope - a thread ack covers this run's
        # instance of a recurring notice just as a message ack covers the single email.
        done = (ack_key("message", r["message_id"], r["sender"], r["subject"],
                        r["account"]) in acked_msg
                or ack_key("thread", None, r["sender"], r["subject"]) in acked_thread)
        if not done:
            open_act[r["run_date"]] += 1
    for d in days:
        d["concept"] = dom.get(d["run_date"], "other")
        d["action"] = act.get(d["run_date"], 0)
        # What is still OUTSTANDING is the number that should drive the colour: a day whose
        # items that have all been seen is a day you can stop looking at.
        d["action_open"] = open_act.get(d["run_date"], 0)
    return {"days": days,
            "totals": {"runs": len(days),
                       "messages": sum(d["n"] for d in days),
                       "kept": sum(d["kept"] for d in days),
                       "trashed": sum(d["trashed"] for d in days)}}


# Strip the parts of a subject that CHANGE between otherwise-identical notices: dates,
# amounts, invoice/order numbers, counts. Without this, "Payment due 08/21" and "Payment due
# 09/21" look like two unrelated messages, which is precisely how a repeating notice hides.
_SHAPE_SUBS = [
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
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        # Acceleration is only meaningful when the gaps are between real ARRIVALS. On the
        # approximate basis the gaps are partly my own re-listing cadence, so no claim is
        # made rather than a shaky one.
        accelerating = False
        if basis == "messages" and len(gaps) >= 3:
            half = max(1, len(gaps) // 2)
            early = sum(gaps[:-half]) / max(1, len(gaps) - half)
            recent = sum(gaps[-half:]) / half
            accelerating = bool(recent * 1.5 < early)
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
            "median_gap": statistics.median(gaps) if gaps else 0,
            "accelerating": accelerating,
            "concept": concept, "concept_key": concepts.key_of(concept),
            "weight": weight,
            "still_open": last["disposition"] != "trashed",
            "dispositions": sorted({r["disposition"] for r in rs}),
        })

    # Loudest first: weighty concepts, then accelerating, then sheer persistence.
    items.sort(key=lambda it: (-it["weight"], -int(it["accelerating"]), -it["notices"]))
    return {"items": items, "min_notices": min_n, "groups_examined": len(groups)}


# ---------------------------------------------------------------- quiet senders
MIN_OBS = 5      # appearances needed before this sender is claimed to have a rhythm
MIN_SPAN = 21    # runs it must have spanned, so a 3-day burst is never called a cadence

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

    TWO THINGS THIS DELIBERATELY DOES NOT DO:
      * It does not claim a cadence from a burst. MIN_OBS/MIN_SPAN mean three consecutive
        days of mail is never mistaken for "arrives daily".
      * It does not pretend to cover monthly billers. The observation window is only as
        long as the run history, so a monthly sender contributes 2-3 observations and
        cannot qualify. The response says so in `reach` rather than letting a thin panel
        imply the money lane is being watched. The filing tree under bills/ and receipts/
        holds the real multi-month cadence and is the right source for that - not built yet.

    Gaps are counted in RUNS ELAPSED, never calendar days: a day with no run is not
    evidence of silence.
    """
    rows_all = rows(conn.execute(
        "SELECT sender, run_date, category FROM messages "
        "WHERE sender IS NOT NULL AND sender != ''"))

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
    for r in rows_all:
        k = _sender_key(r["sender"])
        # A message whose run_date is not in the lattice cannot be placed in time, so it
        # is skipped rather than guessed at - never fabricate a position on the timeline.
        if not k or r["run_date"] not in idx:
            continue
        seen[k].add(idx[r["run_date"]])
        cats[k][r["category"] or "?"] += 1
        variants[k].add(r["sender"])

    items, established = [], 0
    for k, days in seen.items():
        d = sorted(days)
        if len(d) < MIN_OBS or (d[-1] - d[0]) < MIN_SPAN:
            continue
        established += 1
        gaps = [b - a for a, b in zip(d, d[1:])]
        worst = max(gaps)
        silence = last_i - d[-1]
        if silence <= worst:
            continue
        cat = cats[k].most_common(1)[0][0]
        items.append({
            "sender": k,
            "category": cat,
            "weight": 2 if cat in MONEY_CATS else (1.5 if cat in GUARD_CATS else 1.0),
            "silent_runs": silence,
            "worst_gap": worst,
            "median_gap": statistics.median(gaps),
            "ratio": round(silence / float(worst), 2),
            "observations": len(d),
            "last_seen": run_days[d[-1]],
            "first_seen": run_days[d[0]],
            "variants": sorted(variants[k]),
        })

    # Money and guard senders outrank promo noise at equal overdue-ness; within a weight,
    # the most anomalous first.
    items.sort(key=lambda it: (-it["weight"], -it["ratio"]))
    return {
        "items": items,
        "reach": {
            "runs": len(run_days),
            "first_run": run_days[0],
            "last_run": run_days[-1],
            "senders_total": len(seen),
            "established": established,
            "min_obs": MIN_OBS,
            "min_span": MIN_SPAN,
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
        if p.returncode != 0 or not os.path.exists(tmp):
            return {"ok": False, "error": "not found in this mailbox",
                    "detail": (p.stderr or "")[:400]}
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
    # Lets a launcher confirm THIS port is the email dashboard (not a worker's
    # Location Editor that happens to be on it) before deciding to no-op.
    return {"app": "email-dashboard", "name": "Email Routine Dashboard"}


API = {
    "/api/whoami": api_whoami,
    "/api/setup": api_setup,
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
}

# Writing endpoints are a SEPARATE table, reachable only via do_POST and only after the
# CSRF guards. Keeping them out of API means a GET can never trigger a write, however the
# URL is reached - a link, a prefetch, an image tag.
WRITE_API = {
    "/api/ack": api_ack,
    "/api/protected-names": api_protected_names,
    "/api/sender-rule": api_sender_rule,
    "/api/host-review": api_host_review,
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
