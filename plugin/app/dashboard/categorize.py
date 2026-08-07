"""
Shared classifier: map a free-text deletion/triage reason to a stable category.

The daily routine should set categories explicitly when it ingests, but this
keeps backfill (parsing the historical deletion journal) and any loosely-tagged
data consistent with one taxonomy.

The rule table lives in JSON config so no real sender names are baked into code.
Resolution order (first that exists wins):
  1. categorize.local.json    -- gitignored; the live, machine-specific rules
  2. categorize.example.json  -- committed; generic placeholder template
So a fresh checkout still runs on the example taxonomy, while a real deployment
drops in its own private categorize.local.json. Stdlib only.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_NAMES = ("categorize.local.json", "categorize.example.json")


def _load_config():
    names = _CONFIG_NAMES
    if os.environ.get("EMAIL_DASHBOARD_NO_LOCAL_CONFIG") == "1":
        # F25: fall through to the committed example, so a suite's result does not depend
        # on the owner's private rules. The example is in git and is the same everywhere.
        names = tuple(n for n in names if ".local." not in n)
    global _loaded_from
    for name in names:
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                _loaded_from = path
                return json.load(fh)
    raise FileNotFoundError(
        "no categorize config found; expected one of: " + ", ".join(_CONFIG_NAMES)
    )


# WHICH file the rules came from. Two candidates resolved by a first-match loop is exactly
# the shape where a caller believes it is running one set of rules and is running the other,
# and nothing in the module said which. Cheap to record, and the isolation test asserts on it.
_loaded_from = None
_CONFIG = _load_config()

# Order matters: first match wins. JSON arrays preserve order.
_RULES = [(r["category"], r["needles"]) for r in _CONFIG["rules"]]

# Friendly labels for the UI
LABELS = _CONFIG.get("labels", {})


def categorize(reason, subject=""):
    text = ((reason or "") + " " + (subject or "")).lower()
    for cat, needles in _RULES:
        for n in needles:
            if n in text:
                return cat
    return "other"
