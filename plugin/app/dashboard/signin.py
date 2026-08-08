"""Escalate on ANOMALY, never on occurrence. (rule 26)

An alert that fires on every login is not an alert, it is a log. Logs are things you consult;
alerts are things you trust. Merging them is what destroys the channel - and the destruction
is silent, because nothing is ever WRONG. Each individual notice is true. The reader simply
learns that opening them never pays, and by the time one matters that habit is already built.

Reported exactly that way by an owner before it was measured: so much repeated security mail
arriving that a real alert would be ignored.

WHAT THIS MODULE WILL AND WILL NOT CLAIM

The honest constraint is that we can only read what the provider chose to put in the subject
line. Some name the device, most do not. So there is no pretending: `coverage()` reports how
many notices yielded a device signature, and an unparsed one is never counted as "known
device". Where no signature is available the novelty test falls back to the SERVICE - the
first sign-in notice ever seen from a service is worth a look; the fortieth is not.

That fallback is the whole design in miniature. A test that cannot run must not return "fine".

THE ONE THAT MATTERS MOST is not any single message: it is the BURST. Three services in one
short window is the shape of someone walking a credential list, and every message in it looks
individually boring. It is precisely the pattern that alarm fatigue guarantees you will miss,
which is why it is computed across messages rather than within them.

Stdlib only, no I/O, no config reads - everything comes in as arguments so the caller decides
what the store and the clock say. See test_signin.py.
"""
import collections
import re

# ---------------------------------------------------------------------------------------
# Regexes are built from plain strings and compiled here. Backslash escapes in this project
# have been mangled once already by a shell heredoc - a batch of literal backspace characters
# that compiled fine and matched nothing - so the word boundaries are written as explicit
# alternation on non-word characters where it matters, and the patterns stay simple enough
# to read.
# ---------------------------------------------------------------------------------------

# A CHANGE, not a login. These are the steps of an account takeover, and they are categorically
# different from someone signing in: a sign-in is something that happens constantly and legibly,
# while a recovery-address change is a thing that happens twice a decade and locks you out.
# Never suppressed, never collapsed into the ledger.
_CHANGE = re.compile(
    "|".join((
        "password (was |has been )?(changed|reset|updated)",
        "new password",
        "changed your password",
        "two[- ]factor",
        "2fa",
        "recovery (code|email|phone|address)",
        "backup code",
        "passkey",
        "security key",
        "trusted device (was |has been )?added",
        "app password",
        "personal access token",
        "oauth application",
        "identity was just linked",
        "was linked to your",
        "added to your account",
        "phone number (was |has been )?(added|changed|removed)",
        "email (address )?(was |has been )?(added|changed|removed)",
        "account (has been )?(closed|suspended|locked|disabled|deleted)",
    )), re.I)

# The provider itself calling the device new or unrecognised. Worth more than anything we can
# infer, because they are comparing against their own history of the account.
_PROVIDER_SAYS_NEW = re.compile(
    "|".join((
        "new device", "a new device", "unrecognized device", "unrecognised device",
        "unfamiliar device", "new location", "unusual location", "unusual activity",
        "unusual sign", "suspicious", "we don.t recognize", "we do not recognize",
    )), re.I)

# Something was STOPPED. Different from a successful sign-in in the direction that matters.
_BLOCKED = re.compile(
    "|".join((
        "blocked", "prevented", "denied", "failed (sign|log|login|attempt)",
        "unsuccessful", "we stopped", "was not you",
    )), re.I)

# A routine sign-in notice. Deliberately narrow: it has to look like a login event and nothing
# more, because everything ELSE in this file escalates and this is the only bucket that goes
# quiet.
_SIGNIN = re.compile(
    "|".join((
        "new sign[- ]?in", "new log[- ]?in", "signed in", "sign[- ]?in to",
        "sign[- ]?in notice", "sign[- ]?in$", "was used to sign in",
        "we noticed a new sign", "account was accessed",
        # `sign[- ]?in to` was here and `log[- ]?in to` was not, while `new log[- ]?in`
        # required the word "new" - a one-word gap that alone accounted for ten of the
        # fourteen missed messages on the store where this was found.
        "log[- ]?in to", "logged in to",
    )), re.I)

# CREDENTIAL IN FLIGHT: a magic link, a one-time code, a verification mail. Its own kind,
# not folded into _SIGNIN, because the right routine treatment differs - a magic link you DID
# request is noise, and one you did not is an anomaly with nobody signed in yet.
#
# The first version of this module had no such category, and the omission was not a gap at the
# edge. Every phrase in _SIGNIN describes a message REPORTING that a sign-in already happened;
# none matches a message that IS the means of signing in. So a store holding fourteen
# authentication messages classified all fourteen as `other` and the panel reported zero
# sign-ins, zero anomalies, zero everything - while its coverage note spoke confidently about
# the device parser.
#
# And these are the better evidence of the two. A magic link you did not request is the
# intrusion ATTEMPT, arriving before anyone is in; a sign-in notice arrives after. An OTP you
# did not ask for is the same. The panel was discarding exactly the class it most needed.
_CREDENTIAL = re.compile(
    "|".join((
        "magic link", "secure link", "sign[- ]?in link", "log[- ]?in link",
        "link to (log|sign)[- ]?in", "one[- ]?time (code|password|passcode|link)",
        "verification code", "security code", "login code", "sign[- ]?in code",
        "access code", "temporary .{0,16}code", "your code is", "code to (log|sign)[- ]?in",
        "confirm your email", "verify your email", "email verification",
        "authentication code", "2fa code", "otp",
    )), re.I)

# A receipt for a grant the owner just made. The body says there is nothing to do, and it
# arrives BECAUSE they clicked the button.
_CONSENT = re.compile(
    "|".join((
        "you shared some .* account data",
        "shared some .* data with",
        "you granted .* access",
        "access to your .* account was granted",
    )), re.I)

# Terms, policy and product announcements that land in the security pile because they mention
# an account. No event happened. Not an alert by any reading.
_POLICY = re.compile(
    "|".join((
        "terms of (use|service)", "legal agreement", "privacy (policy|settings)",
        "policy & controls", "policy and controls", "upcoming change to your",
        "we.re making some changes", "updates to our", "storage policy",
        "help strengthen the security",
    )), re.I)

# Device signatures, where a provider bothers to include one.
#
# RECOGNISED, NOT GUESSED. The first version took any capitalised word after "from" or "on",
# which pulled "required" out of "[ACTION REQUIRED]" and reported it as a device never seen
# before for GitHub - a fabricated anomaly, in the panel whose entire job is to be believed
# when it fires. A signature has to contain a token that actually names a platform or a
# browser; anything else yields None, which means UNKNOWN and is counted as such.
#
# Under-parsing is the safe direction here: an unknown device falls back to service-level
# novelty, while a wrong one invents an alert.
_PLATFORM = ("windows", "macos", "mac os", "mac", "linux", "ubuntu", "android", "iphone",
             "ipad", "ios", "chromebook", "chrome os", "playstation", "xbox", "mac mini",
             "macbook", "imac", "pixel", "galaxy")
_BROWSER = ("chrome", "chromedesktop", "edge", "edgedesktop", "firefox", "safari", "opera",
            "brave", "chromium", "webkit")
_DEVICE = re.compile(
    "(?:from|on|using)\\s+([A-Za-z][A-Za-z0-9 ()._-]{2,40})", re.I)

ANOMALY = "anomaly"
SIGNIN = "signin"
CREDENTIAL = "credential"
CONSENT = "consent"
POLICY = "policy"
OTHER = "other"


def service_of(sender):
    """The service a notice is about, folded across the spellings one provider uses.

    Deliberately the DISPLAY name rather than the address: one provider sent the 7/29 cluster
    from four different no-reply addresses in the same minute, so keying on the address would
    have made one event look like four services - which is the exact shape the burst rule
    treats as an emergency. A false burst would be the worst possible bug in this file.
    """
    s = (sender or "").strip()
    if not s:
        return ""
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', s)
    name = (m.group(1) if m else s).strip().strip('"').lower()
    if "@" in name:                                   # a bare address: use its domain stem
        dom = name.split("@")[-1].split(">")[0]
        # RFC 2606 reserves test/example/invalid/localhost, so they are never the name of a
        # service - they are only ever fixture scaffolding, and leaving them in made every
        # test-domain sender fold onto the single service "test".
        parts = [p for p in dom.split(".")
                 if p not in ("com", "org", "net", "co", "io",
                              "test", "example", "invalid", "localhost")]
        name = parts[-1] if parts else dom
    # Drop the corporate suffixes that make one service look like several.
    name = re.sub(r"\b(inc|llc|ltd|team|support|communications|payments|accounts?|"
                  r"security|no[- ]?reply|notifications?)\b", " ", name)
    return " ".join(name.split()).strip(" .,-") or (sender or "").strip().lower()


def device_of(subject):
    """A device/browser/OS signature, or None. None means UNKNOWN, never 'known'."""
    if not subject:
        return None
    for m in _DEVICE.finditer(subject):
        sig = " ".join(m.group(1).split()).strip(" .,").lower()
        # It only counts if it NAMES something. Prose after "on" or "from" is not a device,
        # and a signature we merely suspect is worse than none: it fabricates novelty.
        if any(tok in sig for tok in _PLATFORM) or any(tok in sig for tok in _BROWSER):
            return sig
    return None


def classify(subject, sender=""):
    """What KIND of security message this is, and why. Never reads the store.

    Order matters and is argued, not incidental: a change event that also mentions a device
    is a change event, and a consent receipt that mentions signing in is still a receipt.
    The escalating readings are tested first, so an ambiguous subject escalates rather than
    going quiet. That is the fail-safe direction for this particular file.
    """
    s = subject or ""
    signals = []
    if _BLOCKED.search(s):
        signals.append("an attempt was blocked or failed")
    if _CHANGE.search(s):
        signals.append("a CHANGE to the account, not a sign-in")
    if _PROVIDER_SAYS_NEW.search(s):
        signals.append("the provider itself calls the device or location new")
    if signals:
        return ANOMALY, signals
    if _CONSENT.search(s):
        return CONSENT, ["a receipt for access the owner granted"]
    # BEFORE _SIGNIN, because a magic link's subject often contains "log in to" and the
    # credential reading is the more specific - and the more useful - one of the two.
    if _CREDENTIAL.search(s):
        return CREDENTIAL, []
    if _SIGNIN.search(s):
        return SIGNIN, []
    if _POLICY.search(s):
        return POLICY, ["terms or policy announcement - no account event"]
    return OTHER, []


def _minutes_between(a, b):
    """Whole minutes between two ISO-ish timestamps, or None if either is unreadable."""
    from datetime import datetime
    def parse(x):
        t = str(x or "").strip().replace("Z", "+00:00")
        for cut in (19, 16, 10):
            try:
                return datetime.fromisoformat(t[:cut])
            except (ValueError, TypeError):
                continue
        return None
    pa, pb = parse(a), parse(b)
    if not pa or not pb:
        return None
    return abs(int((pb - pa).total_seconds() // 60))


def ledger(rows, burst_services=3, burst_days=1, cluster_minutes=15, financial=(),
           baseline=()):
    """Split security notices into what needs a person and what needs a line.

    `rows` are dicts with at least sender, subject, and one of msg_date / msg_day. Everything
    the caller knows comes in as an argument - no store reads, no config reads, no clock.

    `baseline` is OLDER mail, used to learn what is normal and never reported on. Without it
    every service in the history reads as "first ever seen" the first time this runs, which
    would hand the owner a wall of novelty exactly once and teach them, on day one, that this
    panel cries wolf - reproducing the failure it exists to fix. What is normal is learned
    from the past; only the window is judged.

    Returns anomalies (each with its reasons), the routine ledger, a summary line's worth of
    counts, and `coverage`, which states how much of this was actually derivable rather than
    letting silence read as an all-clear.
    """
    # Services are folded to lower case, so a caller-supplied financial list must be too.
    # It is normalised HERE rather than demanded of every caller: the list is the sort of
    # thing that ends up hand-written in a config file, and a capital letter silently turning
    # off an escalation is exactly the class of failure this module exists to prevent.
    financial = {str(f).strip().lower() for f in (financial or ())}

    seen_services = set()
    seen_devices = collections.defaultdict(set)
    for r in baseline or ():
        svc = service_of(r.get("sender"))
        if svc:
            seen_services.add(svc)
        dev = device_of(r.get("subject"))
        if dev:
            seen_devices[svc].add(dev)

    anomalies, routine, consent, policy = [], [], [], []
    parsed_device = 0
    # How many messages the classifier PLACED. `other` is not a classification, it is
    # the absence of one, and counting it as coverage is what let a blind vocabulary
    # report a confident zero.
    recognised = 0
    by_day = collections.defaultdict(set)

    ordered = sorted(rows, key=lambda r: str(r.get("msg_day") or r.get("msg_date") or ""))
    for r in ordered:
        subject = r.get("subject") or ""
        service = service_of(r.get("sender"))
        day = str(r.get("msg_day") or r.get("msg_date") or "")[:10]
        kind, signals = classify(subject, r.get("sender"))
        if kind != OTHER:
            recognised += 1
        dev = device_of(subject)
        if dev:
            parsed_device += 1

        item = dict(r)
        item.update({"service": service, "device": dev, "kind": kind,
                     "reasons": list(signals)})

        if kind in (SIGNIN, ANOMALY, CREDENTIAL):
            by_day[day].add(service)
            # NOVELTY. With a device signature, novelty is about (service, device). Without
            # one it falls back to the service itself - the first notice ever seen from a
            # service is worth a look, the fortieth is not - because a check that cannot run
            # must not quietly answer "fine".
            if service and service not in seen_services:
                item["reasons"].append("first security notice ever recorded for this service")
            elif dev and dev not in seen_devices[service]:
                item["reasons"].append("device never seen before for this service: %s" % dev)
            if service and service in financial:
                item["reasons"].append("a financial or protected service")
            if service:
                seen_services.add(service)
            if dev:
                seen_devices[service].add(dev)

        if kind == ANOMALY or (kind in (SIGNIN, CREDENTIAL) and item["reasons"]):
            item["kind"] = ANOMALY
            anomalies.append(item)
        elif kind in (SIGNIN, CREDENTIAL):
            routine.append(item)
        elif kind == CONSENT:
            consent.append(item)
        elif kind == POLICY:
            policy.append(item)

    # THE BURST, computed ACROSS messages because that is the only place it exists. Three
    # services inside a short window is the shape of someone working through a credential
    # list, and every message in it looks individually unremarkable - which is exactly why a
    # tool that only ever judges one message at a time will never see it, and exactly what
    # the reader's fatigue would have cost them.
    days = sorted(by_day)
    bursts = []
    for i, d in enumerate(days):
        window = set()
        for d2 in days[i:]:
            if _days_apart(d, d2) is None or _days_apart(d, d2) > burst_days - 1:
                break
            window |= by_day[d2]
        if len(window) >= burst_services:
            bursts.append({"from": d, "services": sorted(window)})
    for b in bursts:
        for it in anomalies + routine:
            if it.get("service") in b["services"] and \
                    str(it.get("msg_day") or it.get("msg_date") or "")[:10] >= b["from"]:
                if "sign-ins across %d services" % len(b["services"]) not in " ".join(
                        it["reasons"]):
                    it["reasons"].append(
                        "part of a burst: sign-ins across %d services from %s"
                        % (len(b["services"]), b["from"]))
    if bursts:
        promoted = [it for it in routine if it["reasons"]]
        routine = [it for it in routine if not it["reasons"]]
        for it in promoted:
            it["kind"] = ANOMALY
        anomalies.extend(promoted)

    # CORROBORATION COLLAPSES. One desktop setup produced six messages inside a few minutes,
    # from two senders. That is one event, and reporting it six times is the same disease
    # this whole file treats.
    anomalies = _collapse(anomalies, cluster_minutes)

    total = len(ordered)
    return {
        "anomalies": anomalies,
        "routine": routine,
        "consent": consent,
        "policy": policy,
        "summary": {
            "signins": len(routine) + sum(1 for a in anomalies if a["kind"] == ANOMALY),
            "routine": len(routine),
            "services": len({it["service"] for it in routine if it["service"]}),
            "anomalies": len(anomalies),
            "consent": len(consent),
            "policy": len(policy),
            "credentials": sum(1 for it in routine + anomalies
                               if it.get("kind") == CREDENTIAL
                               or classify(it.get("subject") or "")[0] == CREDENTIAL),
        },
        # NOT MEASURED IS NOT ZERO - AND THE FIRST VERSION APPLIED THAT TO THE WRONG THING.
        #
        # It reported the reach of the DEVICE PARSER, carefully, while saying nothing about
        # the reach of the CLASSIFIER. So on a store whose subjects the vocabulary did not
        # recognise, the panel returned zero sign-ins, zero anomalies, zero everything - and
        # the output was indistinguishable from a mailbox that genuinely had no sign-in
        # activity. Well-formed JSON, every field present, careful caveat attached to the
        # wrong number, answer completely wrong. This project's own named failure, inside the
        # module written to prevent it.
        #
        # `recognised` is the number that makes a zero legible: it says whether nothing
        # HAPPENED or nothing was UNDERSTOOD, and those call for opposite responses.
        "coverage": {
            "messages": total,
            "recognised": recognised,
            "unrecognised": total - recognised,
            "device_parsed": parsed_device,
            "note": ("`recognised` is how many messages the classifier placed at all. A zero "
                     "beside a LOW recognised count means the vocabulary did not understand "
                     "this mailbox, NOT that nothing happened. Device signatures come from "
                     "the subject line only and most providers omit one - an unparsed notice "
                     "is UNKNOWN, never 'known'."),
        },
        "bursts": bursts,
    }


def _days_apart(a, b):
    from datetime import date
    try:
        y1, m1, d1 = (int(x) for x in str(a)[:10].split("-"))
        y2, m2, d2 = (int(x) for x in str(b)[:10].split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except (ValueError, TypeError):
        return None


def _collapse(items, minutes):
    """Fold notices about ONE event into one item, keeping every reason.

    Same service, within `minutes` of each other. The reasons union rather than the first one
    winning, because the interesting thing about that cluster was that it contained BOTH a
    new passkey and a new trusted device - and keeping only one of those would turn a
    collapse into a loss.
    """
    out = []
    for it in items:
        target = None
        for prev in out:
            if prev.get("service") != it.get("service"):
                continue
            gap = _minutes_between(prev.get("msg_date"), it.get("msg_date"))
            if gap is None:
                # No readable timestamps: same service and same DAY is as close as we can
                # honestly get. Refusing to collapse at all would leave the reported cluster
                # unfixed for every backfilled row, which is most of them.
                same_day = (str(prev.get("msg_day") or "")[:10]
                            == str(it.get("msg_day") or "")[:10]
                            and str(it.get("msg_day") or "") != "")
                if not same_day:
                    continue
            elif gap > minutes:
                continue
            target = prev
            break
        if target is None:
            it = dict(it)
            it["collapsed"] = 1
            it["also"] = []
            out.append(it)
        else:
            target["collapsed"] += 1
            if it.get("subject") and it["subject"] != target.get("subject"):
                target["also"].append(it["subject"])
            for why in it.get("reasons", []):
                if why not in target["reasons"]:
                    target["reasons"].append(why)
    return out
