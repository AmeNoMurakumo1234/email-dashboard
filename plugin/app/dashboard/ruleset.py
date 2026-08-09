"""Rules as a Pareto frontier: nothing is deleted, and what is dominated says so.

THE PROBLEM THIS SOLVES, in the owner's words: people contradict themselves, and an agent
derives rules the person never saw and therefore cannot knowingly contradict. Both produce the
same end state - two rules about the same mail, disagreeing, with nothing recording which one
governs. The routine then follows whichever line it happened to read last.

The old cure was an ordered precedence list. That is a total order imposed on things that are
not totally ordered, and it hides the interesting case rather than showing it.

THE MODEL. A rule is a point in three dimensions:

  SPECIFICITY  global < concept < sender      "different concepts stay in their own lanes"
  TIME         older  < newer                 a person's latest word is their word
  PROVENANCE   ai     < human                 a human ruling outranks an inference

A dominates B when they COLLIDE - when they are about overlapping mail - and A is at least as
strong on every axis and strictly stronger on at least one. Rules that do not collide are never
compared, which is what keeps unrelated lanes from fighting.

WHAT MAKES THIS WORTH THE TROUBLE is the pair that does NOT resolve. An OLD HUMAN rule against
a NEW AI rule: the human wins provenance, the machine wins recency, neither dominates, and both
stay on the frontier. That is not a gap in the model - it is the model refusing to quietly
overrule a person with an inference, or to ignore something learned since. It is exactly the
case that needs a human, and the frontier is what makes it visible instead of arbitrary.

NOTHING IS DELETED. A dominated rule keeps its place in the record with a pointer to what
dominates it, so the history of a decision survives and "why did this stop applying?" has an
answer. Only its authority is gone.

Stdlib only, no I/O. See test_ruleset.py.
"""

SPECIFICITY = {"global": 0, "concept": 1, "sender": 2}
PROVENANCE = {"ai": 0, "human": 1}


def rule(rid, text, scope="global", key="", source="ai", date="", evidence=""):
    """One rule as a point. `scope` is global/concept/sender; `key` names the lane."""
    if scope not in SPECIFICITY:
        raise ValueError("scope must be one of %s" % sorted(SPECIFICITY))
    if source not in PROVENANCE:
        raise ValueError("source must be 'human' or 'ai'")
    return {"id": rid, "text": text, "scope": scope, "key": (key or "").strip().lower(),
            "source": source, "date": date or "", "evidence": evidence}


def collides(a, b, member_of=None):
    """Are these two rules about overlapping mail?

    Same lane always collides. A SENDER rule also collides with a CONCEPT rule when that
    sender's mail lives in that concept - which is the case the flat model kept missing:
    "treat mail logistics as background" and "always surface the post" are not two unrelated
    opinions, they are two rules about the same messages.

    `member_of` maps a sender key to the concept it mostly sits in. Without it, sender and
    concept rules are treated as separate lanes - the conservative reading, since inventing a
    containment nobody established would fabricate conflicts.
    """
    if a["scope"] == "global" or b["scope"] == "global":
        return True                       # a global rule is about everything, by definition
    if a["scope"] == b["scope"]:
        return a["key"] == b["key"]
    sender, concept = (a, b) if a["scope"] == "sender" else (b, a)
    if not member_of:
        return False
    return (member_of(sender["key"]) or "").strip().lower() == concept["key"]


def dominates(a, b, member_of=None):
    """A dominates B: they collide, A is >= on every axis and > on at least one."""
    if a["id"] == b["id"] or not collides(a, b, member_of):
        return False
    axes = (
        (SPECIFICITY[a["scope"]], SPECIFICITY[b["scope"]]),
        (a["date"], b["date"]),
        (PROVENANCE[a["source"]], PROVENANCE[b["source"]]),
    )
    return all(x >= y for x, y in axes) and any(x > y for x, y in axes)


def analyse(rules, member_of=None):
    """Split a rule set into what governs, what is superseded, and what needs a person.

    Returns {"frontier": [...], "dominated": [(rule, [dominators])], "conflicts": [(a, b)]}.

    `conflicts` are colliding pairs where NEITHER dominates - the old-human-versus-new-ai case
    and its siblings. They are reported rather than resolved, because resolving them is the
    one thing this module must not do on its own.
    """
    rules = list(rules)
    dominated, frontier = [], []
    for r in rules:
        over = [o for o in rules if dominates(o, r, member_of)]
        (dominated.append((r, over)) if over else frontier.append(r))

    conflicts = []
    for i, a in enumerate(frontier):
        for b in frontier[i + 1:]:
            if collides(a, b, member_of) and not dominates(a, b, member_of) \
                    and not dominates(b, a, member_of):
                conflicts.append((a, b))
    return {"frontier": frontier, "dominated": dominated, "conflicts": conflicts}


def explain(result):
    """A plain reading of an analyse() result. Conflicts first - they are the only part that
    asks anything of anybody."""
    out = []
    for a, b in result["conflicts"]:
        out.append("NEEDS YOU: %r and %r are about the same mail and neither outranks the "
                   "other (%s/%s vs %s/%s)."
                   % (a["text"][:60], b["text"][:60], a["source"], a["date"] or "undated",
                      b["source"], b["date"] or "undated"))
    for r, over in result["dominated"]:
        out.append("superseded: %r -> by %r"
                   % (r["text"][:60], ", ".join(o["text"][:40] for o in over)))
    return out
