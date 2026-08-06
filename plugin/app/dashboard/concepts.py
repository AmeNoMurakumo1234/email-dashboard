"""The canonical concept list - the fix for the category drift.

THE DEFECT THIS CLOSES. Raw category labels drift. Left alone, a store accumulates several
times more labels than it has real concepts - each one invented as a reasonable looking call
at a time, never once collapsed back onto an existing name. The cost is not cosmetic, it is a
SILENT UNDERCOUNT: a query on the obvious label returns a confident, specific, wrong number
and nothing looks broken.

It is worst exactly where it matters most. On the store this was first measured against, the
concepts that mattered least - promos, social noise - had barely drifted, so their biggest
label still reached most of them. The concepts that mattered MOST had drifted furthest: for
money, security and medical, the single most obvious label reached only about a third of the
mail that actually belonged there.

So "show me every money item" answered with a third of the truth, stated as the whole. The
instrument drifted and never reported that it was drifting.

WHY THE REPAIR IS SAFE TO LAND. The ROUTINE now requires picking from the existing vocabulary,
and consecutive runs since have invented ZERO new labels. The bleeding stopped first: a moving
target cannot be migrated onto a fixed list.

THE DESIGN, and the two things it deliberately does NOT do:

  1. It does NOT destroy the original label. `category` stays exactly as written; `concept` is a
     new column beside it. Every row keeps its raw history and the migration is reversible - the
     same posture as "junk goes to Trash, never permanent-delete."

  2. It does NOT fold an unrecognised label into "other". An unknown label resolves to UNMAPPED and
     is meant to be VISIBLE. Guessing a bucket is how the original drift hid: a silent default made
     a wrong answer look like a clean one. Same lesson ingest.normalize_status already learned - an
     unrecognised status is passed through as unknown rather than assumed green.

Adding a label? Put it under the concept it belongs to here. If you find yourself wanting a
thirteenth concept, that is a real decision and it belongs to the owner, not to a run.
"""

UNMAPPED = "unmapped"

# concept -> the labels that mean it.
#
# GENERIC DEFAULTS ONLY, and that constraint is load-bearing rather than tidy. This map was
# once seeded straight from one real mailbox's drift audit, so the shipped program carried
# that mailbox's vocabulary - labels naming specific companies, subscriptions and life
# circumstances. Scrubbing those facts out of the COMMENTS had left them sitting in the DATA
# a few lines below, which is a good demonstration that a privacy fix aimed at prose is
# aimed at one layer of a file. This file's own docstring already said the rule; the
# defaults broke it.
#
# So the test for a label here is: would it be derivable from the concept's own name by
# someone who has never seen this mailbox? If it names a company, a carrier, a platform, a
# subscription, or a life circumstance, it belongs in concepts.local.json instead.
#
# These are STARTING defaults. Add the labels your own runs actually write - to the LOCAL
# file, which never leaves your machine.
CONCEPTS = {
    "social / platform notifications": [
        "social-notification", "social", "social-platform", "notification",
        "social-person", "digest", "content-digest", "job-alert",
    ],
    "marketing / promo": [
        "promo", "promotion", "marketing", "junk", "junk-locked", "confirmed-junk",
    ],
    "money (bills, receipts, banking)": [
        "receipt", "bill", "invoice", "payment", "financial", "financial-notice",
        "bank-statement", "banking", "credit", "statement",
    ],
    "account & security": [
        "security", "security-alert", "account-security", "security-noise", "account",
        "account-notice", "otp", "verification", "policy-notice", "policy-announcement",
    ],
    "newsletters": ["newsletter", "org-newsletter", "bulletin"],
    "medical": ["medical", "appointment", "appointment-reminder"],
    "family & people": ["family", "person"],
    "open questions / candidates": ["question", "junk-candidate"],
    "steam": ["steam-sale"],
    "mail logistics": ["shipping", "mail", "delivery", "tracking"],
    "calendar": ["calendar", "invite"],
    "other": ["other", "action-needed"],
}

# Short machine-friendly keys for URLs and API params, so the UI never has to pass
# "money (bills, receipts, banking)" through a query string.
CONCEPT_KEYS = {
    "social / platform notifications": "social",
    "marketing / promo": "promo",
    "money (bills, receipts, banking)": "money",
    "account & security": "security",
    "newsletters": "newsletters",
    "medical": "medical",
    "family & people": "family",
    "open questions / candidates": "questions",
    "steam": "steam",
    "mail logistics": "logistics",
    "calendar": "calendar",
    "other": "other",
}

# FILE A LABEL BY ITS ROWS, NEVER BY A WORD IN ITS NAME.
#
# The first version of this map inherited three labels that all began with the same word and
# filed all three under MONEY on the strength of it. Reading the actual rows showed one was a
# newsletter and two were account notices; not one of them was about money. They would have
# been miscounted as money - inside the very fix built to stop money from being miscounted.
#
# A mapping is an instrument too, and an instrument nobody checked against ground truth
# reports a confident wrong number. So when you add a label to your local file, open the mail
# behind it first. The word in the label is what somebody once guessed; the rows are what is
# actually true.
#
# (The three labels themselves were one mailbox's vocabulary and now live in
# concepts.local.json, which is where anything naming a real service belongs.)
#
# "content-digest" appears under two concepts in the original audit table (newsletters and social).
# It is resolved here to social/platform notifications - every message actually carrying it is a
# platform digest. Recording the ambiguity rather than silently picking, because a note like this
# is the difference between a decision and an accident.
_AMBIGUOUS = {"content-digest": "social / platform notifications"}

_LABEL_TO_CONCEPT = {}
for _concept, _labels in CONCEPTS.items():
    for _l in _labels:
        _LABEL_TO_CONCEPT.setdefault(_l, _concept)
_LABEL_TO_CONCEPT.update(_AMBIGUOUS)


def _load_local_labels():
    """Extend the map from concepts.local.json, which never leaves this machine.

    The labels a mailbox actually accumulates are personal - how you tier the people who
    write to you is not something to compile into a program and publish. The shipped map
    holds generic defaults; anything specific to YOUR mail belongs here.

    Extend-only, by design. A local file may add labels to a concept and may name a new
    concept, but it cannot remove a shipped one - so a typo here degrades to an unmapped
    label, which is VISIBLE, rather than silently unmapping labels that used to resolve.
    A missing file is the normal case and is not an error.
    """
    import json as _json, os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "concepts.local.json")
    try:
        with open(path, encoding="utf-8") as f:
            local = _json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:                                   # malformed: say so, loudly
        import sys as _sys
        print("concepts.local.json ignored (%s: %s)" % (type(e).__name__, e),
              file=_sys.stderr)
        return 0
    added = 0
    for concept, labels in (local.get("concepts") or {}).items():
        if str(concept).startswith("_"):
            continue
        CONCEPTS.setdefault(concept, [])
        for label in labels or []:
            lab = str(label).strip().lower()
            if not lab or lab.startswith("_"):
                continue
            if lab not in CONCEPTS[concept]:
                CONCEPTS[concept].append(lab)
            if _LABEL_TO_CONCEPT.setdefault(lab, concept) == concept:
                added += 1
    for concept, key in (local.get("concept_keys") or {}).items():
        if not str(concept).startswith("_"):
            CONCEPT_KEYS.setdefault(concept, key)
    return added


LOCAL_LABELS_ADDED = _load_local_labels()


def concept_of(label):
    """Resolve a raw category label to its canonical concept.

    An unknown label returns UNMAPPED - never "other". The caller is expected to SHOW that,
    not swallow it.
    """
    if not label:
        return UNMAPPED
    return _LABEL_TO_CONCEPT.get(label.strip().lower(), UNMAPPED)


def key_of(concept):
    return CONCEPT_KEYS.get(concept, concept)


def concept_for_key(key):
    for concept, k in CONCEPT_KEYS.items():
        if k == key:
            return concept
    return None


def all_concepts():
    return list(CONCEPTS.keys())


def known_labels():
    return set(_LABEL_TO_CONCEPT)
