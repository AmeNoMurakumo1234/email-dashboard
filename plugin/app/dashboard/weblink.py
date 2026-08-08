"""A `web_link` carries a durable message handle. Recovering it is not guesswork. (F33)

`backfill_bodies.py` reported rows without a Message-ID as a permanent hole. The honesty was
right and the conclusion was wrong: a provider's web link embeds the provider's own identifier
for that message, and that identifier is exactly what a fetch needs.

Reported from an install where `message_id` covered under a third of the rows while `web_link`
covered every one of them - so keying recovery on the Message-ID alone declared most of the
store unreachable, when every one of those rows was fetchable.

So the rule is *"a row with neither a Message-ID nor a web_link is a permanent hole"*, and the
fallback is worth having.

THE ENCODING DETAIL, because it is not obvious and it fails in the worst possible pattern.

`ItemID` inside an OWA `webLink` is percent-encoded **standard base64** - the alphabet with
`/` and `+`. Microsoft Graph's own `id` field is **base64URL** - `-` and `_`. Hand the decoded
standard form to a Graph read and a `/` inside the id is parsed as a path separator, and the
request fails with `ErrorInvalidIdMalformed`. Single-encoding to `%2F` does not help, because
it is decoded again before it reaches Graph; only double-encoding (`%252F`) gets through, and
that is a workaround for a problem that disappears if you convert the alphabet instead.

Roughly one id in twelve contains one of those two characters. So a naive implementation works
for the first several messages and then fails - which is the worst distribution there is,
because it looks like it works.

NOT VERIFIED AGAINST A LIVE MAILBOX HERE. Every account on the install this was written on is
IMAP, and IMAP has no web link, so no row in that store has one to test with. The conversion
and the extraction are tested against the documented shapes and the reported failure; the
end-to-end fetch is not. Said plainly rather than implied, because "it should work" and "it
was seen to work" are different claims and only one of them was earned.
"""
import re
from urllib.parse import unquote, urlparse, parse_qs

# ItemID appears as a query parameter on OWA deep links. Matched case-insensitively because
# the casing varies between the `ItemID` on a readmail link and the `itemid` some clients emit.
_ITEMID = re.compile(r"[?&]itemid=([^&#]+)", re.I)


def to_base64url(std):
    """Standard base64 -> base64URL. The one line the whole fallback depends on.

    `/` and `+` are legal in standard base64 and illegal, in the sense that matters, inside a
    URL path segment. Converting the alphabet is not an escaping trick; it is the identifier
    written the way the API expects to read it.
    """
    return (std or "").replace("/", "-").replace("+", "_")


def item_id(web_link):
    """The provider's own message id from a web link, in the form an API will accept.

    Returns None when there is no identifier in the link - which is a real answer, not a
    failure. A link that merely opens a mailbox is not a handle for a message.
    """
    if not web_link:
        return None
    m = _ITEMID.search(str(web_link))
    if not m:
        # Some links carry it as a path segment rather than a query parameter. Parsed rather
        # than pattern-matched so a query string that happens to contain a slash cannot be
        # mistaken for one.
        try:
            path = urlparse(str(web_link)).path
        except ValueError:
            return None
        parts = [p for p in path.split("/") if p]
        # An id is long and base64-ish; a path segment like "mail" or "inbox" is not.
        for p in reversed(parts):
            cand = unquote(p)
            if len(cand) >= 40 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", cand):
                return to_base64url(cand)
        return None
    raw = m.group(1)
    # Percent-decoded FIRST, then the alphabet converted. Doing it the other way round would
    # convert the '%2F' escapes' own characters and produce an id that decodes to nothing.
    return to_base64url(unquote(raw))


def handle_of(row):
    """The best durable handle for a row: ('message_id', v) or ('item_id', v) or None.

    Message-ID first, deliberately. It is provider-independent and survives the message being
    moved between folders, while an item id is scoped to one provider's API. The web link is
    the fallback, not the preference - but a fallback that reaches the other two thirds of a
    store is the difference between a repair tool and a partial one.
    """
    mid = str((row or {}).get("message_id") or "").strip()
    if mid:
        return ("message_id", mid)
    iid = item_id((row or {}).get("web_link"))
    if iid:
        return ("item_id", iid)
    return None
