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
import db

# WHAT BEING WRONG COSTS, as a hard floor above the weights. A weight is a judgement about
# how much a question is worth asking; STAKES is a statement about what happens if the owner
# answers it wrong, and no amount of "this one is really interesting" may float a noise
# question above a data-loss question. Five equally-weighted questions about senders you
# ignore were outranking a question about a rule that would bin work assigned to you.
STAKES = {"data-loss": 0, "safety": 1, "attention": 2, "noise": 3}

# WEIGHT IS ONLY MEANINGFUL WITHIN A STAKES BAND. It answers "how strong is the evidence for
# this particular question?", not "how important is this compared to a different kind of
# question" - stakes answers that, and it wins. Comparing weights across bands is what let a
# large pile of ignorable senders outrank a rule that would bin assigned work, and it is why
# a thinly-evidenced data-loss question still sorts above a strongly-evidenced noise one.


# Recipient-list markers that mean a PERSON was named, in mail an automated system sent.
# Some forges route mentions through a `mention@` sender address; trackers put the
# assignment in the recipient list rather than the subject. This is the evidence that
# only exists now that recipients are stored, and it is what makes the question below
# generatable at all.
_ASSIGNED_TO = re.compile(r"\bmentions?@|\bassign\w*@|\breviewer@", re.I)

# Enough history to be a pattern rather than a coincidence. Below this a "rule" would be
# fitted to noise, and a question the owner cannot answer confidently teaches them to skim.
MIN_EVIDENCE = 12
ATTENTION = ("action-needed", "family", "security", "financial")

# TWO STRENGTHS, because one of these phrases is evidence and the other is a growth hack.
#
# STRONG means a PERSON was named: someone assigned it, mentioned you, asked for your
# review. Bulk senders do not write these, because they are only true of one recipient.
_NAMED_STRONG = re.compile(
    r"\bassigned (?:you|to you)\b|\bmentioned you\b|\brequested your review\b"
    r"|\brequests? your (?:review|approval|input|attention)\b|\bwaiting on you\b"
    r"|\byour (?:approval|signature|response) (?:is )?(?:needed|required)\b",
    re.I)

# WEAK means urgent-sounding, and it is exactly what bulk senders put in subject lines. The
# very first thing this question surfaced on a real mailbox was a marketing blast headed
# "[Action Required] Looks like you have been ghosting us!" - a false positive sitting at
# the top of a list whose entire purpose is that its top item deserves attention. A weak
# marker only counts when something else corroborates it.
_NAMED_WEAK = re.compile(
    r"\bneeds? your\b|\baction required\b"
    r"|\bplease (?:review|approve|confirm|respond)\b", re.I)


def names_a_person(subject, recipients, addressed_directly=None):
    """Was a PERSON named here, or does it merely sound urgent?

    Strong markers stand alone; weak ones need corroboration - the message was addressed to
    this mailbox directly, or the recipient list carries a mention marker. Being generous
    here costs more than being strict, because this signal outranks everything by design:
    a false positive does not just add a bad row, it takes the top of the list.
    """
    subject, recipients = subject or "", recipients or ""
    if _NAMED_STRONG.search(subject) or _ASSIGNED_TO.search(recipients):
        return True
    return bool(_NAMED_WEAK.search(subject) and addressed_directly == 1)


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
        "n": 0, "kept": 0, "surfaced": 0, "trashed": 0, "attention": 0, "direct": 0,
        "direct_unread": [], "days": set(), "display": "",
        "concepts": collections.Counter()})
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
        # SURFACED IS NOT KEPT, and conflating them starved this whole feature. `kept` is
        # a decision that the mail matters; `surfaced` is the tool showing it and the owner
        # doing nothing. They were counted together, and since the suppression test below
        # is "has anything ever been kept?", ANY sender an install merely surfaces became
        # permanently ineligible. On a routine that surfaces rather than bins - which is a
        # perfectly ordinary way to run this - the volume question could never fire at all.
        #
        # It was invisible from here because the mailbox this was written against trashes.
        # A field report of "the generator never asks about volume with no signal", on an
        # install where one sender was 29% of the mail, is what surfaced it. Surfaced and
        # ignored is EVIDENCE FOR the question, not against it.
        d = (r["disposition"] or "").strip().lower()
        if d == "trashed":
            p["trashed"] += 1
        elif d == "kept":
            p["kept"] += 1
        else:
            p["surfaced"] += 1
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
        if r["disposition"] in db.DISPOSABLE and names_a_person(
                r["subject"], None, r["addressed_directly"]):
            p.setdefault("asks", []).append(r["subject"])

    # ---- 0. THE ONE WHOSE WRONG ANSWER DESTROYS WORK.
    #
    # Not "this sender is mostly noise" (that is question 1) but "this CATEGORY is mostly
    # binned, and some of the mail in it is an assignment to you". The distinction matters
    # because the danger comes from a RULE rather than from a habit: a rule written about
    # `bot-github-issue` or `notifications` disposes of every future message under that
    # label, including the ones where a person named the owner.
    #
    # This is the question a hand-built questionnaire caught and this generator did not, on
    # an install where GitHub mention mail and tracker assignments both sat under a
    # category the rules bin. It is mechanically derivable now that recipients are stored,
    # and being wrong about it loses work rather than adding noise.
    at_risk = collections.defaultdict(lambda: {"n": 0, "binned": 0, "hits": []})
    for r in _rows(conn,
                   "SELECT COALESCE(category,'') category, subject, disposition, "
                   "addressed_directly, COALESCE(recipients,'') recipients FROM messages "
                   "WHERE COALESCE(category,'') != ''"):
        named = names_a_person(r["subject"], r["recipients"], r["addressed_directly"])
        if not named:
            continue
        a = at_risk[r["category"]]
        a["n"] += 1
        # DISPOSABLE, not just trashed. This question exists to catch a category that is
        # being disposed of while carrying assignments to the owner - and on a read-only
        # install the disposal is a would_trash, so keying on `trashed` alone would silence
        # the one question here whose wrong answer destroys work, on exactly the installs
        # that cannot act and therefore rely most on being asked.
        if r["disposition"] in db.DISPOSABLE:
            a["binned"] += 1
            if len(a["hits"]) < 3:
                a["hits"].append(r["subject"])
    for category, a in at_risk.items():
        qid = f"assigned-at-risk:{category}"
        # Only when mail of this shape is ACTUALLY being disposed of. A category where
        # assignments are already surfaced is working correctly and needs no question.
        if qid in answered or not a["binned"]:
            continue
        total = _rows(conn, "SELECT COUNT(*) n FROM messages WHERE category = ?",
                      (category,))[0]["n"]
        qs.append({
            "id": qid, "kind": "assigned_work_at_risk",
            # Scaled by evidence within the band: six binned assignments is a stronger
            # case than one. One is still enough to outrank every noise question there is.
            "weight": min(1.0, 0.4 + 0.1 * a["binned"]),
            "stakes": "data-loss",
            "question": (f"{a['binned']} message(s) under \"{category}\" named you "
                         f"personally - a mention, a review request or an assignment - and "
                         f"were binned. Should mail in this category that names you be "
                         f"kept out of any auto-trash rule?"),
            "why_it_matters": ("A rule about a category applies to every future message in "
                               "it. This is the shape that loses work rather than adding "
                               "noise: the messages look like bot traffic, and one of them "
                               "is somebody waiting on you."),
            "evidence": {"category": category, "in_category": total,
                         "named_you": a["n"], "of_those_binned": a["binned"],
                         "examples": a["hits"]},
            "options": ["yes - never bin anything in this category that names me",
                        "yes - and surface those immediately",
                        "no - these are notifications, I see them elsewhere"],
            "writes": "rules-and-policies.md",
        })

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
            "id": qid, "kind": "personally_addressed", "weight": 1.0, "stakes": "data-loss",
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
        shown = p["surfaced"]
        top = (p["concepts"].most_common(1) or [("", 0)])[0][0]
        # A sender whose mail is mostly MONEY or SECURITY gets the same question with the
        # stakes said out loud. "0 kept, 0 flagged" is a fact about how the mail was
        # triaged, not about whether it mattered - a year of ignored statements from a bank
        # looks identical to a year of ignored promos right up until the one that is a
        # fraud alert. The evidence cannot tell those apart, so the person has to.
        risky = any(w in top.lower() for w in ("money", "bank", "financ", "security", "bill"))
        qs.append({
            "id": qid, "kind": "sender_disposition", "stakes": "noise",
            # Ranked ABOVE the ordinary version: getting this one wrong is expensive, and a
            # question you never reach is a question that defaults to whatever we assumed.
            # Volume raises it further - within the noise band, where it cannot outrank
            # anything that loses work no matter how large the pile gets.
            "weight": min(0.95, (0.8 if risky else 0.7) + 0.05 * (p["n"] // 50)),
            "question": (
                f"You have {p['n']} messages from {p['display'][:60]} across "
                f"{len(p['days'])} day{'' if len(p['days']) == 1 else 's'}"
                + (f", {shown} of them put in front of you," if shown else "")
                + f" and have never kept or acted on one. How should it be handled?"),
            "why_it_matters": (
                ("This sender's mail is mostly %s. Never having acted on it is not the "
                 "same as it never mattering - a year of ignored statements and a year of "
                 "ignored promos look identical here, until one of them is a fraud alert. "
                 "Worth a moment before binning." % top) if risky else
                ("Answerable from the evidence in seconds, and it is the single largest "
                 "source of noise in most mailboxes.")),
            # `surfaced` reported separately from `trashed`: "12 of these were put in
            # front of you and none mattered" is a different and stronger fact than "12
            # were binned automatically and none mattered".
            "evidence": {"messages": p["n"], "kept": 0, "ever_flagged": 0,
                         "surfaced_to_you": shown, "auto_binned": p["trashed"],
                         "days_seen": len(p["days"]), "mostly": top or "?"},
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
        # LET THE EVIDENCE CARRY THE QUESTION. The first version asked "family, your
        # employer, your bank, your doctor?" - which is personal-mail shaped, and was asked
        # of a work mailbox whose real answer was four colleagues and an accountant. It
        # made the owner introspect and type names, while the tool was ALREADY HOLDING the
        # list: the senders whose mail they have flagged before. Offering that list as a
        # multi-select turns a minute of recall into ten seconds of recognition, and
        # recognition is both faster and more accurate.
        humans = sorted([v for v in per.values() if v["attention"] and v["n"] >= 2],
                        key=lambda v: -v["attention"])[:10]
        names = [h["display"][:60] for h in humans]
        qs.append({
            "id": "escalation-contacts", "kind": "escalation_contacts", "weight": 0.95, "stakes": "safety",
            "question": (
                (f"These {len(names)} senders have needed your attention before. Which of "
                 f"them must never be filtered or missed?")
                if names else
                ("Whose mail must never be filtered or missed? Names or addresses - a "
                 "surname is enough, and it is matched as a substring.")),
            "why_it_matters": ("This is the guard behind every automatic rule the dashboard "
                               "can write, and while it is empty the tool refuses to write "
                               "any rule at all. Answering this is the one that makes the "
                               "rest safe."),
            "evidence": {"already_protected": None,
                         "flagged_before": len(names),
                         "senders_you_have_flagged_before": names},
            # The list itself, as choices. Free text stays available for everyone the
            # mailbox has not seen yet - a bank you have never had a problem with will not
            # be in this list, and is exactly the kind of thing the guard is for.
            "options": names,
            "multi": True,
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
            "id": qid, "kind": "concept_never_actioned", "weight": 0.4, "stakes": "noise",
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
            "id": "concept-gap", "kind": "concept_gap", "weight": 0.85, "stakes": "attention",
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
            "id": qid, "kind": "repeatedly_acknowledged", "weight": 0.6, "stakes": "attention",
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
            "id": "mailbox-roles", "kind": "mailbox_role", "weight": 0.5, "stakes": "attention",
            "question": ("What is each of these mailboxes for? One phrase each is enough."),
            "why_it_matters": ("Mail that belongs in one box and turns up in another is "
                               "itself a finding, and the tool cannot notice that without "
                               "knowing what each box is for."),
            "evidence": {"mailboxes": [a["account"] for a in accts]},
            "options": [],
            "writes": "rules-and-policies.md",
        })

    # Stakes first, weight second. A question whose wrong answer destroys something
    # outranks every question whose wrong answer is merely noise, however large the pile.
    qs.sort(key=lambda q: (STAKES.get(q.get("stakes"), 2), -q["weight"]))
    return qs[:limit], len(qs)
