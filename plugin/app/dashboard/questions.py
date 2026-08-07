"""What this tool still does not know about its owner - asked from their own mailbox.

THE GAP THIS CLOSES. The plugin ships `rules-and-policies.example.md` with `_Fill this in._`
in five sections and `protected.example.json` as placeholders. It knows its rules are missing
and it never asks for them. A new user connects a mailbox, gets a dashboard full of
dispositions derived from nobody's judgment, and the placeholders sit there forever. As the
owner it was reported to put it: *"even the whole questionnaire process should have been
obvious out of the box without me asking for it."*

WHY GENERATED RATHER THAN A CHECKLIST, which is the whole point. A generic questionnaire is
close to worthless - "how should we treat bots?" is unanswerable in the abstract. What makes
a question worth answering is the evidence attached to it, and this tool is the only thing in
the room holding that evidence. In the reported deployment, grounded questions caught a rule
that would have BINNED WORK ASSIGNED TO THE OWNER, an escalation rule wrong in both
directions, a contact described as quarterly who is actually monthly, and a sender that looks
like bot noise and is the highest-signal mail in the box. Three would have made the routine
confidently wrong. The last inverted a rule.

RANKED BY WHAT BEING WRONG COSTS, not by volume. A personally-addressed message misfiled as
bot noise outranks four hundred promos, because the promos being wrong is an annoyance and
the other is lost work.

EVERY QUESTION MUST CARRY ITS EVIDENCE. If a question cannot show the rows behind it, it does
not belong here - it is a policy debate, and this file is for recall.

ANSWERING IS NOT ONLY CONVERSATION. Acknowledging things on the dashboard is already an
answer about attention, and it is read here as one: a sender whose mail is acknowledged over
and over is being seen and dismissed, which is a rule waiting to be written down.
"""
import collections
import json
import os
import re

import concepts

# Enough history to be a pattern rather than a coincidence. Below this a "rule" would be
# fitted to noise, and a question the owner cannot answer confidently teaches them to skim.
MIN_EVIDENCE = 12
ATTENTION = ("action-needed", "family", "security", "financial")

# Subjects that mean a PERSON is being asked for something, in the mail that automated
# senders produce. These are the ones a "bots are noise" rule silently destroys.
_PERSONAL_ASK = re.compile(
    r"\bassigned (?:you|to you)\b|\bmentioned you\b|\brequested your review\b"
    r"|\brequests? your (?:review|approval|input|attention)\b|\bwaiting on you\b"
    r"|\byour (?:approval|signature|response) (?:is )?(?:needed|required)\b"
    r"|\bneeds? your\b|\baction required\b|\bplease (?:review|approve|confirm|respond)\b",
    re.I)


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _ruled_keys(rules_path):
    """Sender keys that already carry a dashboard-written rule - do not ask twice."""
    try:
        with open(rules_path, encoding="utf-8-sig") as f:
            return set(re.findall(r"dashboard-rule:([^\s>]+)", f.read()))
    except OSError:
        return set()


def _answered(conn):
    """Question ids already answered, so a pass does not re-ask what was settled."""
    try:
        return {r["question_id"] for r in _rows(conn, "SELECT question_id FROM answers")}
    except Exception:
        return set()


def generate(conn, rules_path=None, protected=None, limit=6):
    """Every question worth asking, best first. Returns a list of question dicts.

    `protected` is the guard list as the loader actually resolves it. Passed in rather than
    read here so this module never has to decide what counts as configured - the loader
    already answers that, and two answers to "is the guard set?" is one too many.

    `limit` is a deliberate cap and a deliberate confession: the reported deployment answered
    fourteen questions in a couple of minutes because they were THEIRS. Thirty generic ones
    would have been closed unread. Everything above the cap keeps for a later pass - the
    drift question exists so nothing is lost by asking few.
    """
    from server import _sender_key                                   # noqa: PLC0415

    answered = _answered(conn)
    ruled = _ruled_keys(rules_path) if rules_path else set()
    qs = []

    # ---------------------------------------------------------------- per-sender evidence
    per = collections.defaultdict(lambda: {
        "n": 0, "kept": 0, "trashed": 0, "attention": 0, "direct": 0, "direct_unread": [],
        "days": set(), "display": "", "concepts": collections.Counter()})
    for r in _rows(conn,
                   "SELECT sender, disposition, COALESCE(importance,'') importance, "
                   "COALESCE(concept,'') concept, COALESCE(msg_day, run_date) day, "
                   "subject, addressed_directly, recipient_count "
                   "FROM messages WHERE sender IS NOT NULL AND sender != ''"):
        key = _sender_key(r["sender"])
        if not key:
            continue
        p = per[key]
        p["n"] += 1
        p["display"] = p["display"] or r["sender"]
        p["days"].add(r["day"])
        if r["disposition"] == "trashed":
            p["trashed"] += 1
        else:
            p["kept"] += 1
        if r["importance"] in ATTENTION:
            p["attention"] += 1
        if r["concept"]:
            p["concepts"][r["concept"]] += 1
        if r["addressed_directly"] == 1:
            p["direct"] += 1
            if len(p["direct_unread"]) < 3:
                p["direct_unread"].append(r["subject"])
        # A personal ask from an automated sender, regardless of recipient data - this is
        # the signal that survives on an install with no recipient information at all.
        if r["disposition"] == "trashed" and _PERSONAL_ASK.search(r["subject"] or ""):
            p.setdefault("asks", []).append(r["subject"])

    # ---- 1. THE DANGEROUS ONE: a sender treated as noise that sometimes asks you directly
    for key, p in per.items():
        if key in ruled or p["n"] < MIN_EVIDENCE:
            continue
        asks = p.get("asks") or []
        if not asks and not (p["direct"] and p["trashed"] > p["kept"]):
            continue
        qid = f"personal-ask:{key}"
        if qid in answered:
            continue
        binned = p["trashed"]
        qs.append({
            "id": qid, "kind": "personally_addressed", "weight": 1.0,
            "question": (f"{p['display'][:60]} is mostly binned here ({binned} of {p['n']}), "
                         f"but some of its mail is addressed to you personally or asks you "
                         f"to do something. Should a rule that bins this sender make an "
                         f"exception for those?"),
            "why_it_matters": ("A bots-are-noise rule that misses this bins work assigned to "
                               "you, unread. It is the one wrong answer here that destroys "
                               "something rather than merely annoying you."),
            "evidence": {"messages": p["n"], "binned": binned,
                         "addressed_to_you": p["direct"],
                         "examples": (asks or p["direct_unread"])[:3]},
            "options": ["yes - surface anything addressed to me or asking me to act",
                        "yes - and never auto-trash this sender at all",
                        "no - all of it is noise, including those"],
            "writes": "rules-and-policies.md",
        })

    # ---- 2. High volume, never kept, never flagged: the cheapest large win
    for key, p in per.items():
        qid = f"never-actioned:{key}"
        if (key in ruled or qid in answered or p["n"] < MIN_EVIDENCE
                or p["kept"] or p["attention"] or p.get("asks")):
            continue
        top = (p["concepts"].most_common(1) or [("", 0)])[0][0]
        # A sender whose mail is mostly MONEY or SECURITY gets the same question with the
        # stakes said out loud. "0 kept, 0 flagged" is a fact about how the mail was
        # triaged, not about whether it mattered - a year of ignored statements from a bank
        # looks identical to a year of ignored promos right up until the one that is a
        # fraud alert. The evidence cannot tell those apart, so the person has to.
        risky = any(w in top.lower() for w in ("money", "bank", "financ", "security", "bill"))
        qs.append({
            "id": qid, "kind": "sender_disposition",
            # Ranked ABOVE the ordinary version: getting this one wrong is expensive, and a
            # question you never reach is a question that defaults to whatever we assumed.
            "weight": 0.8 if risky else 0.7,
            "question": (f"You have {p['n']} messages from {p['display'][:60]} across "
                         f"{len(p['days'])} day{'' if len(p['days']) == 1 else 's'} and have "
                         f"never kept or acted on one. "
                         f"How should it be handled?"),
            "why_it_matters": (
                ("This sender's mail is mostly %s. Never having acted on it is not the "
                 "same as it never mattering - a year of ignored statements and a year of "
                 "ignored promos look identical here, until one of them is a fraud alert. "
                 "Worth a moment before binning." % top) if risky else
                ("Answerable from the evidence in seconds, and it is the single largest "
                 "source of noise in most mailboxes.")),
            "evidence": {"messages": p["n"], "kept": 0, "ever_flagged": 0,
                         "days_seen": len(p["days"]),
                         "mostly": top or "?"},
            "options": (["bin it but keep it searchable",
                         "it matters sometimes - keep asking me",
                         "auto-trash it from now on", "leave it as it is"] if risky else
                        ["auto-trash it from now on", "bin it but keep it searchable",
                         "it matters sometimes - keep asking me", "leave it as it is"]),
            "writes": "rules-and-policies.md",
        })

    # ---- 3. Who must never be missed. Answering this ARMS the safety guard.
    #
    # Skipped once the guard is populated. Asking a question whose answer is already on
    # disk is how a panel teaches its reader that the panel is not paying attention - and
    # this is the question that most needs to be taken seriously when it does appear.
    if "escalation-contacts" not in answered and not (protected or []):
        humans = [p for p in per.values()
                  if p["attention"] and p["n"] >= 2][:8]
        qs.append({
            "id": "escalation-contacts", "kind": "escalation_contacts", "weight": 0.95,
            "question": ("Whose mail must never be filtered or missed - family, your "
                         "employer, your bank, your doctor? Names or addresses; a surname "
                         "is enough."),
            "why_it_matters": ("This is the guard behind every automatic rule the dashboard "
                               "can write, and while it is empty the tool refuses to write "
                               "any rule at all. Answering this is the one that makes the "
                               "rest safe."),
            "evidence": {"already_protected": None,
                         "senders_you_have_flagged_before":
                             [h["display"][:50] for h in humans]},
            "options": [],                      # free text: names
            "writes": "config/protected.local.json",
        })

    # ---- 4. A whole concept that has never mattered
    conc = _rows(conn,
                 "SELECT COALESCE(concept,'unmapped') c, COUNT(*) n, "
                 "SUM(CASE WHEN COALESCE(importance,'') IN ('action-needed','family',"
                 "'security','financial') THEN 1 ELSE 0 END) att "
                 "FROM messages GROUP BY c HAVING n >= ? ORDER BY n DESC", (MIN_EVIDENCE * 3,))
    for c in conc:
        qid = f"concept-never-actioned:{c['c']}"
        if c["att"] or qid in answered or c["c"] == "unmapped":
            continue
        qs.append({
            "id": qid, "kind": "concept_never_actioned", "weight": 0.4,
            "question": (f"\"{c['c']}\" accounts for {c['n']} messages and none has ever "
                         f"needed you. Treat the whole category as background?"),
            "why_it_matters": "A category-level answer settles many senders at once.",
            "evidence": {"messages": c["n"], "ever_flagged": 0},
            "options": ["yes - background, never surface it",
                        "mostly, but surface anything addressed to me",
                        "no - I do want to see these"],
            "writes": "rules-and-policies.md",
        })

    # ---- 5. THE VOCABULARY HAS A HOLE. Proposed from the data rather than guessed for them.
    unmapped = _rows(conn,
                     "SELECT category, COUNT(*) n FROM messages "
                     "WHERE COALESCE(concept,'unmapped') = 'unmapped' AND category != '' "
                     "GROUP BY category ORDER BY n DESC LIMIT 12")
    if unmapped and "concept-gap" not in answered:
        qs.append({
            "id": "concept-gap", "kind": "concept_gap", "weight": 0.85,
            "question": (f"{len(unmapped)} label(s) your runs use do not belong to any "
                         f"concept, so they are invisible in the concept view. What do "
                         f"they mean?"),
            "why_it_matters": ("The shipped concept list is deliberately generic and is "
                               "personal-mail shaped. A work mailbox usually needs its own "
                               "concepts - a direct question from a colleague, or a build "
                               "that broke - and the tool should learn yours rather than "
                               "assume a set. An unmapped label is invisible in the way "
                               "this project keeps warning about: the rollup still "
                               "balances and the concept view is quietly wrong."),
            "evidence": {"labels": [{"label": u["category"], "messages": u["n"]}
                                    for u in unmapped]},
            "options": [],                      # free text: label -> concept
            "writes": "dashboard/concepts.local.json",
        })

    # ---- 6. ACKS ARE ANSWERS. Repeatedly dismissing something is a rule not yet written.
    acked = _rows(conn,
                  "SELECT sender, COUNT(*) n FROM acks WHERE sender IS NOT NULL "
                  "AND sender != '' GROUP BY sender HAVING n >= 3 ORDER BY n DESC LIMIT 5")
    for a in acked:
        key = _sender_key(a["sender"]) or a["sender"]
        qid = f"repeatedly-acked:{key}"
        if qid in answered or key in ruled:
            continue
        qs.append({
            "id": qid, "kind": "repeatedly_acknowledged", "weight": 0.6,
            "question": (f"You have acknowledged mail from {a['sender'][:60]} "
                         f"{a['n']} times. It keeps arriving and you keep dismissing it - "
                         f"should it stop being surfaced at all?"),
            "why_it_matters": ("Acknowledging is already an answer about attention. Asking "
                               "it out loud turns a repeated chore into a rule."),
            "evidence": {"times_acknowledged": a["n"]},
            "options": ["stop surfacing it", "keep surfacing - I want to see each one",
                        "surface it less often"],
            "writes": "rules-and-policies.md",
        })

    # ---- 7. What is each mailbox FOR. Cheap, and the routine already assumes an answer.
    accts = _rows(conn, "SELECT DISTINCT account FROM messages WHERE account LIKE '%@%'")
    if len(accts) > 1 and "mailbox-roles" not in answered:
        qs.append({
            "id": "mailbox-roles", "kind": "mailbox_role", "weight": 0.5,
            "question": ("What is each of these mailboxes for? One phrase each is enough."),
            "why_it_matters": ("Mail that belongs in one box and turns up in another is "
                               "itself a finding, and the tool cannot notice that without "
                               "knowing what each box is for."),
            "evidence": {"mailboxes": [a["account"] for a in accts]},
            "options": [],
            "writes": "rules-and-policies.md",
        })

    qs.sort(key=lambda q: -q["weight"])
    return qs[:limit], len(qs)
