"""Who gave up on email and went elsewhere to find you. The only outcome this tool can measure.

WHAT A REACH IS. Chat platforms send a missed-activity notice by mail - "<Name> sent you a
message", "you have unread messages". They look exactly like bot noise, they are usually
filed as bot noise, and they are the highest-signal mail in the box: one arriving from
somebody who matters means **that person already gave up on your inbox and went looking for
you somewhere else.** By the time it lands, this tool has already lost that round.

The owner it was reported to put it better than I would:

    "If I actually caught the email and responded to something before they message me on
    Teams, they can all live in shock and surprise."

That makes these notices a SCOREBOARD rather than noise. A month with fewer of them from the
people who matter is the tool working; a month with more is the tool failing, whatever the
inbox looks like.

WHY IT IS WORTH THE CODE. Everything else on this dashboard measures ACTIVITY - messages
swept, rules written, items acknowledged. All of that can go up while the thing the owner
actually cares about gets worse. This is the only number here that moves in the same
direction as the outcome, and it costs nothing to count because the mail is already stored.

TWO HONESTY RULES, both load-bearing:

  * A DROP IS NOT A WIN ON ITS OWN. Fewer reaches in a month with half the mail is not the
    tool working, it is a quiet month. Every count here is reported beside the volume it
    came from, and the rate is what the panel shows.

  * NOT MEASURED IS NOT ZERO. If nothing in this mailbox looks like a reach - no platform
    configured, none ever seen - the answer is "not measured", never "0 reaches, well done".
    A scoreboard that congratulates you for having no instrument is worse than no
    scoreboard.

CONFIGURABLE, NOT HARD-CODED TO ONE VENDOR. The defaults below cover the common platforms
by the SHAPE of the address rather than the name of the service; `elsewhere_senders` in
dashboard.local.json narrows it to an exact list for a workplace that needs one.
"""
import collections
import re

# WHAT AN AUTOMATED NOTIFIER LOOKS LIKE, rather than a list of platforms.
#
# The first version shipped the platform names, which was wrong twice over. It could only
# ever find the services someone had thought of - useless for the workplace running
# something in-house, which is exactly the deployment that most needs this - and a list of
# consumer brands sitting in source is indistinguishable from a list of somebody's actual
# correspondents, which is not a thing this project ships.
#
# The shape is what matters and it is near-universal: platforms send missed-activity mail
# from an address nobody can reply to. Combined with the subject test below it is both more
# general and more precise than naming vendors.
_AUTOMATED = re.compile(
    r"(?:^|[.@_+-])(?:no-?reply|do-?not-?reply|notifications?|notify|alerts?|mailer"
    r"|automated|updates?|team)(?:[.@_+-]|$)", re.I)

# IT NEEDS THE SUBJECT TO QUALIFY, NOT JUST THE SENDER. This is the correction that
# saved the whole feature from being meaningless: the first version matched on sender alone
# and reported 144 reaches on a real mailbox, every one of which was a broadcast - "Zach
# posted a new photo", "catch up on moments you've missed". Nobody was looking for anybody.
#
# The distinction is the entire point. A reach is somebody who needed a response from YOU
# and went to another channel to get it. A notification that a person you follow posted
# something is engagement with a feed, and counting it would have produced a confident
# scoreboard measuring how much social media the owner receives.
#
# Dedicated platforms (Teams, Slack) send only missed-activity mail, so their subjects match
# anyway; consumer platforms share one sender between messages and broadcasts, so the
# subject is the only thing that separates them.
# {1,60} and not {2,60}: the REACH happened whatever the sender is called. A one-character
# display name made the notice invisible entirely, when the honest answer is "somebody
# reached you and the name is too short to attribute" - which `who_reached` already returns,
# because its own length guard is separate from this one.
_DIRECT_CONTACT = (
    re.compile(r"^\s*(.{1,60}?)\s+sent\s+(?:you\s+)?an?\s+(?:message|chat)", re.I),
    re.compile(r"^\s*(.{1,60}?)\s+(?:messaged|pinged|dm'?d)\s+you", re.I),
    re.compile(r"^\s*(.{1,60}?)\s+mentioned you", re.I),
    re.compile(r"^\s*(.{1,60}?)\s+replied to (?:your|you)", re.I),
    re.compile(r"^\s*new message from\s+(.{1,60}?)\s*$", re.I),
    re.compile(r"missed (?:activity|messages?|chats?)(?: from| in)\s+(.{1,60}?)\s*$", re.I),
    re.compile(r"^\s*(.{1,60}?)\s+is waiting (?:for|on) (?:your|you)", re.I),
    # No name in these, but they are unambiguously "somebody wanted you".
    re.compile(r"^\s*you have (?:\d+ )?(?:new |unread )?(?:messages?|mentions?)\b", re.I),
    re.compile(r"^\s*missed (?:activity|messages?)\b", re.I),
)

# Broadcast shapes that share a sender with the real thing, listed so the exclusion is
# visible rather than implied by what the patterns above happen not to match.
_BROADCAST = re.compile(
    r"\bposted\b|\bcommented\b|\bshared\b|\bliked\b|\breacted\b|\bwent live\b"
    r"|catch up on|\bstories\b|\bsuggested\b|\bmemories\b|\bbirthday\b", re.I)


def configured_senders(cfg):
    """The sender patterns the owner named, if any.

    An empty tuple means "use the shape test", which is the broad default. A list means the
    owner has told us exactly which senders count, and only those do. The distinction is
    reported, because a shape test that finds nothing means "your platform does not look
    like the usual thing" while a chosen list that finds nothing means "your platform is
    configured and quiet" - different findings.
    """
    raw = (cfg or {}).get("elsewhere_senders")
    if isinstance(raw, list) and raw:
        return tuple(str(x).strip().lower() for x in raw if str(x).strip()), True
    return (), False


def went_elsewhere(sender, subject, patterns=()):
    """Did somebody give up on this inbox and reach the owner on another channel?

    Two independent halves, both required:

      * the sender is an automated notifier (or one the owner named), which excludes a
        colleague's own mail saying "I sent you a message on Teams" - that is email WORKING;
      * the subject says a PERSON wanted them, which excludes broadcasts. Matching on the
        sender alone reported 144 of these on a real mailbox and every one was "X posted a
        new photo". Nobody was looking for anybody.
    """
    low = (sender or "").lower()
    if patterns:
        if not any(p in low for p in patterns):
            return False
    elif not _AUTOMATED.search(low):
        return False
    subj = subject or ""
    if _BROADCAST.search(subj):
        return False
    return any(pat.search(subj) for pat in _DIRECT_CONTACT)


def who_reached(subject):
    """The person named in the notice, or None.

    None rather than a guess. An unattributed reach still counts toward the total - the
    platform told you somebody wanted you - but it cannot be scored against the protected
    list, and pretending otherwise would put a name on the wrong person.
    """
    for pat in _DIRECT_CONTACT:
        m = pat.search(subject or "")
        if m:
            # Not every shape names anybody. "You have 3 unread messages" is a real reach
            # and there is nobody in it, so those patterns carry no capture group - and
            # calling group(1) on them raised IndexError, which took out the whole endpoint
            # rather than returning the honest "reached by someone, name unknown".
            if not m.groups():
                return None
            name = re.sub(r"\s+", " ", m.group(1)).strip(" -:–—")
            # "Carlos just messaged you" captured "Carlos just". The adverb belongs to the
            # verb, not to the person, and a name with one glued on will never match a
            # protected list - so the reach would count and the person behind it would not.
            name = re.sub(r"\s+(?:just|also|has|have|recently|now)$", "", name, flags=re.I)
            # Platform boilerplate that survives the pattern. A "name" of "Someone" or a
            # bare channel marker is not a person.
            if 1 < len(name) <= 60 and name.lower() not in (
                    "someone", "a user", "somebody", "you", "new"):
                return name
    return None


def _matches_protected(name, protected):
    low = (name or "").lower()
    return bool(low) and any(p.lower() in low for p in protected if p)


def scoreboard(rows, cfg=None, protected=(), today=None):
    """Reaches per month, beside the volume they came from.

    `rows` are dicts with sender, subject and a day (YYYY-MM-DD). Taken as plain data
    rather than a connection so this is testable without a store and reusable by anything
    that has the messages in hand.

    `today` names the current date so the month in progress can be marked as such. Passed in
    rather than read from the clock, because a trend that changes depending on when the page
    is opened is not a trend.
    """
    patterns, chosen = configured_senders(cfg)
    protected = [p for p in (protected or []) if p]

    months = collections.defaultdict(
        lambda: {"reaches": 0, "from_people_who_matter": 0, "messages": 0, "who": []})
    total_reaches = 0
    for r in rows:
        day = str(r.get("day") or "")[:7]
        if len(day) != 7:
            continue
        m = months[day]
        m["messages"] += 1
        if not went_elsewhere(r.get("sender"), r.get("subject"), patterns):
            continue
        total_reaches += 1
        m["reaches"] += 1
        name = who_reached(r.get("subject"))
        if name and _matches_protected(name, protected):
            m["from_people_who_matter"] += 1
            if name not in m["who"]:
                m["who"].append(name)

    if today is None:
        from datetime import date                                  # noqa: PLC0415
        today = date.today()
    this_month = str(today)[:7]

    series = []
    for month in sorted(months):
        m = months[month]
        series.append({
            "month": month,
            "reaches": m["reaches"],
            "from_people_who_matter": m["from_people_who_matter"],
            "messages": m["messages"],
            # PER HUNDRED MESSAGES, because the raw count moves with how busy the month was.
            # A quiet month is not the tool working.
            "rate": round(100.0 * m["reaches"] / m["messages"], 2) if m["messages"] else None,
            "who": m["who"][:6],
            # THE MONTH IN PROGRESS IS NOT A DATA POINT. Six days of August against a full
            # July reports a collapse every time the page is opened early in a month, and
            # the collapse is the calendar, not the tool.
            "partial": month == this_month,
        })

    measured = total_reaches > 0
    return {
        "measured": measured,
        "month_in_progress": this_month,
        # Said explicitly, because "0 reaches" from an instrument that has never fired once
        # is congratulation for having no instrument.
        "why_not": None if measured else (
            "No mail in this store matches a another channel reaching you, so there is "
            "nothing to score yet. %s"
            % ("Your `elsewhere_senders` list is configured and none of it has been seen - "
               "which may itself be the answer." if chosen else
               "If your workplace uses a platform the defaults do not cover, set "
               "`elsewhere_senders` in config/dashboard.local.json.")),
        "senders_configured": chosen,
        "months": series,
        "total": total_reaches,
        "protected_known": len(protected),
        "trend": _trend(series),
    }


def _trend(series):
    """Last full month against the one before it, as a direction and a reason.

    Only ever compares COMPLETE months against each other and says which two it used. A
    trend line that silently includes a month that is three days old reports a collapse
    every time it is looked at early.
    """
    usable = [s for s in series if s["rate"] is not None and not s["partial"]]
    if len(usable) < 2:
        return {"direction": "unknown",
                "detail": ("needs two COMPLETE months before a direction means anything; "
                           "the month in progress is deliberately not counted")}
    prev, last = usable[-2], usable[-1]
    if last["rate"] < prev["rate"]:
        direction = "better"
    elif last["rate"] > prev["rate"]:
        direction = "worse"
    else:
        direction = "flat"
    return {
        "direction": direction,
        "from": prev["month"], "to": last["month"],
        "from_rate": prev["rate"], "to_rate": last["rate"],
        "detail": ("%s: %s per hundred messages in %s, %s in %s"
                   % (direction, prev["rate"], prev["month"], last["rate"], last["month"])),
        # The caveat travels WITH the verdict rather than living in a footnote nobody reads.
        "caveat": ("Fewer reaches in a much quieter month is not the tool working. "
                   "%d messages in %s against %d in %s."
                   % (prev["messages"], prev["month"], last["messages"], last["month"])),
    }
