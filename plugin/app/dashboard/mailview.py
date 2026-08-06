"""Render an untrusted email safely enough to LOOK at.

Email is hostile input. This module assumes every byte of a message is written by an
attacker and is trying to (a) run code, (b) phone home, or (c) make a link look like it
goes somewhere it does not. Nothing here trusts the browser to save us - the browser
sandbox is the LAST layer, not the first.

THE LAYERS, outermost last:

  1. Plain text is the DEFAULT. The text/plain part of a message cannot execute or fetch
     anything. HTML is opt-in, per message, never automatic.
  2. Server-side allowlist sanitising (this module). Unknown tag -> dropped. Unknown
     attribute -> dropped. Dangerous element -> dropped WITH ITS CONTENTS so its text can
     never leak into the document as markup.
  3. Remote resources are neutralised HERE, not in the browser. Every img src, srcset,
     background, poster and every url() in a style is removed and COUNTED. This is the
     single most important protection in practice: a 1x1 tracking pixel needs no click and
     no script - it fires on render and tells the sender the address is live, when it was
     opened, and from what IP.
  4. Links are DEFANGED, not just escaped: the href is removed entirely so nothing is
     clickable, and the real destination is rendered as visible text beside the link text.
     A link whose text says one thing and whose href says another is the whole phishing
     game, and this makes the mismatch impossible to miss.
  5. The caller wraps the result in a CSP that permits no network at all, inside an
     <iframe sandbox> with no allow-scripts and no allow-same-origin. Even a total failure
     of layers 2-4 then executes nothing and reaches nothing.

Stdlib only. No network access of any kind happens in this file - it never resolves a URL,
never loads a remote part, never follows a redirect. It only ever transforms bytes it was
handed.
"""
import re
from html import escape
from html.parser import HTMLParser

# Structural / formatting tags that carry no behaviour.
ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "a", "b", "strong", "i", "em", "u", "s", "strike",
    "small", "big", "sub", "sup", "font", "center", "blockquote", "pre", "code", "tt",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "img", "figure", "figcaption", "abbr", "cite", "q", "mark", "wbr",
}

# Dropped ALONG WITH EVERYTHING INSIDE THEM. For <script>/<style> this is essential:
# emitting their text content would inject raw code into the document. <svg> and <math>
# are here because both are full foreign-content languages that can carry script.
DROP_WITH_CONTENT = {
    "script", "style", "iframe", "object", "applet", "form", "button",
    "select", "option", "textarea", "head", "title", "noscript",
    "svg", "math", "frameset", "template", "portal", "audio", "video",
    "canvas", "map", "marquee", "blink", "xml", "import",
}

# VOID elements that must be DROPPED WITHOUT SUPPRESSION (measured).
#
# These never have a closing tag, so treating them as containers means waiting for a
# </meta> that can never arrive - and everything after that point is swallowed in silence.
# A real newsletter went in at eighty thousand characters and came out at thirty-three,
# with the report
# cheerfully saying "nothing to strip", because one stray <meta> in the body ate the entire
# message. No error, no warning, just an empty panel: the exact failure shape this lane
# keeps meeting. They are dropped as tags, and their URL attributes are still counted.
DROP_VOID = {
    "meta", "link", "base", "input", "source", "track", "area", "embed", "frame",
    "param", "keygen", "command",
}

VOID = {"br", "hr", "img", "col", "wbr"}

# Per-tag attribute allowlist. Anything not listed is dropped, so a new attack surface
# (a new event handler, a new fetching attribute) fails CLOSED rather than passing through.
ALLOWED_ATTRS = {
    "a": {"title"},                       # href deliberately NOT allowed - see _defang
    "img": {"alt", "width", "height"},    # src deliberately NOT allowed
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "col": {"span"}, "colgroup": {"span"},
    "table": {"align"}, "tr": {"align"}, "div": {"align"}, "p": {"align"},
    "font": {"color", "size"},
    "abbr": {"title"}, "q": {"cite"}, "blockquote": {},
}
GLOBAL_ATTRS = {"style"}                  # scrubbed by _clean_style before it is kept

URL_ATTRS = {"src", "srcset", "background", "poster", "data", "href", "action",
             "formaction", "codebase", "cite", "longdesc", "usemap", "profile",
             "dynsrc", "lowsrc"}

SAFE_SCHEME = re.compile(r"^(https?|mailto):", re.I)
CSS_DANGER = re.compile(
    r"url\s*\(|expression\s*\(|@import|behavior\s*:|-moz-binding|javascript\s*:|"
    r"vbscript\s*:|data\s*:|@charset|@font-face", re.I)

# LAYOUT-HOSTILE PROPERTIES, dropped in a reader view.
#
# Absolute/fixed positioning has no legitimate job in a message you are only reading, and
# it has two bad ones: it stacks elements on top of each other once the surrounding table
# widths have been stripped (which is what turned real messages into unreadable piles of
# overlapping text), and it is the mechanism for overlaying invisible text on visible text,
# which is a clickjacking/spoofing trick. Dropping the whole family costs nothing here and
# makes the render behave like a document instead of a collapsed layout.
CSS_LAYOUT_HOSTILE = re.compile(
    r"^\s*(position|top|left|right|bottom|z-index|float|clear|transform|translate|"
    r"rotate|scale|perspective|clip|clip-path|mix-blend-mode|filter)\s*:", re.I)


def _clean_style(value):
    """Keep only declarations with no fetching or scripting capability.

    A style attribute is the quietest exfiltration channel in an email:
    style="background:url(https://tracker/i.png?u=<id>)" needs no script and no click.
    Any declaration containing a url(), an @import, an expression() or a scheme is
    dropped whole - partial rewriting of CSS is a game that is easy to lose.
    """
    kept = []
    for decl in value.split(";"):
        if not decl.strip():
            continue
        if CSS_DANGER.search(decl):
            continue
        if CSS_LAYOUT_HOSTILE.match(decl):
            continue
        # A declaration carrying a quote or an angle bracket is never legitimate CSS here -
        # it is someone trying to close the attribute early and start a new one
        # (style='x:1" onload="evil()'). Escaping already makes that inert, but keeping the
        # payload on screen as CSS is pointless, so drop the declaration outright and let
        # the reader see nothing rather than a defused bomb.
        if any(ch in decl for ch in ('"', "'", "<", ">", "\\")):
            continue
        kept.append(decl.strip())
    return "; ".join(kept)


class _Sanitizer(HTMLParser):
    def __init__(self, extra_void=()):
        # convert_charrefs=False so entities pass through us and get re-escaped rather
        # than being decoded into live characters we then re-emit unexamined.
        super().__init__(convert_charrefs=False)
        self.drop_void = DROP_VOID | set(extra_void)
        self.out = []
        self.open_stack = []
        self.suppress_depth = 0
        self.suppress_tag = None
        self.report = {
            "images_blocked": 0, "links_defanged": 0, "scripts_removed": 0,
            "styles_removed": 0, "frames_removed": 0, "remote_refs_stripped": 0,
            "tags_dropped": 0, "attrs_dropped": 0, "external_hosts": [],
        }
        self._hosts = set()

    # ---- helpers -------------------------------------------------------------
    def _note_host(self, url):
        m = re.match(r"\s*[a-z]+://([^/\s\"'>]+)", url or "", re.I)
        if m:
            h = m.group(1).lower()
            if h not in self._hosts and len(self._hosts) < 40:
                self._hosts.add(h)
                self.report["external_hosts"].append(h)

    def _defang(self, attrs):
        """Pull the destination out of a link so it can be SHOWN instead of followed."""
        for k, v in attrs:
            if k.lower() == "href" and v:
                self._note_host(v)
                return v.strip()
        return None

    # ---- parser callbacks ----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.suppress_depth:
            if tag == self.suppress_tag:
                self.suppress_depth += 1
            return
        if tag in self.drop_void:
            # No closing tag exists, so there is nothing to suppress - drop the tag itself
            # and account for anything it was fetching.
            self.report["tags_dropped"] += 1
            for k, v in attrs:
                if k.lower() in URL_ATTRS and v:
                    self.report["remote_refs_stripped"] += 1
                    self._note_host(v)
            return
        if tag in DROP_WITH_CONTENT:
            self.suppress_depth = 1
            self.suppress_tag = tag
            if tag in ("script", "noscript"):
                self.report["scripts_removed"] += 1
            elif tag == "style":
                self.report["styles_removed"] += 1
            elif tag in ("iframe", "frame", "frameset", "object", "embed", "applet",
                         "portal", "canvas", "svg", "math"):
                self.report["frames_removed"] += 1
            else:
                self.report["tags_dropped"] += 1
            # count any remote reference this element was carrying, before discarding it
            for k, v in attrs:
                if k.lower() in URL_ATTRS and v:
                    self.report["remote_refs_stripped"] += 1
                    self._note_host(v)
            return
        if tag not in ALLOWED_TAGS:
            self.report["tags_dropped"] += 1
            return                       # drop the TAG but keep its text content

        href = self._defang(attrs) if tag == "a" else None
        safe = []
        allowed = ALLOWED_ATTRS.get(tag, set()) | GLOBAL_ATTRS
        for k, v in attrs:
            k = k.lower()
            if k.startswith("on") or k.startswith("data-") or k not in allowed:
                if k in URL_ATTRS and v:
                    self.report["remote_refs_stripped"] += 1
                    self._note_host(v)
                    if k in ("src", "srcset", "background", "poster") and tag == "img":
                        pass          # counted as images_blocked below
                if k not in allowed:
                    self.report["attrs_dropped"] += 1
                continue
            if k == "style":
                v = _clean_style(v or "")
                if not v:
                    continue
            safe.append((k, v))

        if tag == "img":
            self.report["images_blocked"] += 1
            # Render a visible placeholder instead of the image. Never emit a src.
            alt = ""
            for k, v in safe:
                if k == "alt":
                    alt = v or ""
            label = escape(alt)[:80] or "image"
            self.out.append(
                '<span class="mv-img" title="remote image blocked">'
                + "[blocked image: " + label + "]</span>")
            return

        attr_s = "".join(
            ' %s="%s"' % (k, escape(v or "", quote=True)) for k, v in safe)
        # Carry the destination forward on an INERT data attribute. There is still no href,
        # so nothing is navigable and the sandbox has nothing to act on - but reader mode
        # needs to know where a link pointed in order to judge it against the sender's own
        # domain, and re-deriving that from the displayed text would be guesswork.
        if tag == "a" and href and SAFE_SCHEME.match(href):
            attr_s += ' data-mv-href="%s"' % escape(href[:400], quote=True)
        self.out.append("<%s%s>" % (tag, attr_s))
        if tag not in VOID:
            self.open_stack.append(tag)
        if tag == "a" and href:
            self.report["links_defanged"] += 1
            self._pending_href = href

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.suppress_depth:
            if tag == self.suppress_tag:
                self.suppress_depth -= 1
                if self.suppress_depth == 0:
                    self.suppress_tag = None
            return
        if tag in VOID or tag not in ALLOWED_TAGS:
            return
        if tag == "a" and getattr(self, "_pending_href", None):
            # Show where it REALLY went, right next to the words that claimed otherwise.
            # But only ECHO a destination that is a real one: a javascript:/data:/vbscript:
            # href is not a place, and printing its payload back to the reader as though it
            # were an address is both misleading and a way to smuggle a long crafted string
            # onto the screen. Name the scheme, drop the payload.
            href = self._pending_href
            if SAFE_SCHEME.match(href):
                # LEAD WITH THE HOST. The point of showing the destination is spotting a
                # link whose words and target disagree, and that judgement is made on the
                # HOST - but tracking links run to 200+ characters of opaque token, which
                # buries the one part worth reading and makes the message unreadable. Show
                # host first, then a short tail, and keep the whole thing in the tooltip.
                m = re.match(r"\s*([a-z]+)://([^/\s?#]+)([^\s]*)", href, re.I)
                if m:
                    host, rest = m.group(2), m.group(3) or ""
                    tail = (rest[:24] + "…") if len(rest) > 24 else rest
                    label = host + tail
                else:
                    label = href[:60]
                shown = ('<span title="%s">[%s]</span>'
                         % (escape(href[:400], quote=True), escape(label)))
            else:
                scheme = (href.split(":", 1)[0] or "?")[:20]
                shown = "[UNSAFE LINK REMOVED - " + escape(scheme).lower() + ": scheme]"
            self.out.append('</a><span class="mv-url">' + shown + "</span>")
            self._pending_href = None
            if tag in self.open_stack:
                self.open_stack.reverse()
                self.open_stack.remove(tag)
                self.open_stack.reverse()
            return
        if tag in self.open_stack:
            self.out.append("</%s>" % tag)
            self.open_stack.reverse()
            self.open_stack.remove(tag)
            self.open_stack.reverse()

    def handle_data(self, data):
        if self.suppress_depth:
            return                       # script/style text never reaches the document
        self.out.append(escape(data))

    # Entities are re-emitted AS ENTITIES, not escaped into visible text. Escaping the
    # ampersand turned every &nbsp; in a message into the literal string "&nbsp;" on
    # screen, which peppered real mail with garbage. Re-emitting them is safe: an entity in
    # TEXT position is parsed as character data, never as markup, so &lt;script&gt; renders
    # the characters "<script>" for a human to read and cannot open a tag. The name is
    # whitelisted to word characters so nothing exotic gets through the gap.
    def handle_entityref(self, name):
        if not self.suppress_depth:
            self.out.append("&%s;" % name if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,31}", name)
                            else escape("&" + name + ";"))

    def handle_charref(self, name):
        if not self.suppress_depth:
            self.out.append("&#%s;" % name if re.fullmatch(r"[xX]?[0-9A-Fa-f]{1,8}", name)
                            else escape("&#" + name + ";"))

    def handle_comment(self, data):
        pass                             # comments can hide conditional markup - drop

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass                             # CDATA and friends


def _run(html, extra_void=()):
    s = _Sanitizer(extra_void=extra_void)
    try:
        s.feed(html or "")
        s.close()
    except Exception:
        # A parser blow-up must NEVER fall back to returning the raw input.
        return None, s
    while s.open_stack:                    # close anything the message left open
        s.out.append("</%s>" % s.open_stack.pop())
    return "".join(s.out), s


def sanitize_html(html):
    """Return (safe_html, report). Never raises on malformed input.

    SELF-HEALING AGAINST AN UNCLOSED DROP. If a drop-with-content element never closes,
    every byte after it is swallowed and the result looks like a legitimately empty
    message - silent, total content loss with a clean-looking report. That happened for
    real with <meta>. Rather than trust the tag lists to be perfect forever, detect the
    condition (still suppressing at EOF) and re-parse once with the offending tag treated
    as void. The report says it happened so the recovery is never invisible.
    """
    out, s = _run(html)
    # ITERATIVE, not a single retry: suppress_depth can be >1 and several DIFFERENT tags
    # can each be mis-classified, in which case fixing one just hands the document to the
    # next one. Loop until the parse completes with nothing left suppressed, bounded so a
    # pathological message cannot spin.
    if out is not None and s.suppress_depth > 0 and s.suppress_tag:
        healed = []
        cur_out, cur_s = out, s
        for _ in range(8):
            tag = cur_s.suppress_tag
            # Keep going while ANYTHING is still suppressed, and do NOT stop just because
            # one retry did not immediately grow the output: when several tags are each
            # mis-classified, neutralising the first simply hands the document to the next
            # one, and an improvement-only loop gives up on the very first handoff. Stop
            # on no-new-culprit instead, which is what actually guarantees progress.
            if cur_s.suppress_depth <= 0 or not tag or tag in healed:
                break
            healed.append(tag)
            out2, s2 = _run(html, extra_void=tuple(healed))
            if out2 is None:
                break
            cur_out, cur_s = out2, s2
        if len(cur_out) > len(out):
            cur_s.report["recovered_unclosed"] = ",".join(healed)
            return cur_out, cur_s.report
    if out is None:
        return ("<p><i>[this message could not be parsed safely and was not "
                "rendered]</i></p>", s.report)
    return out, s.report


CSP = ("default-src 'none'; img-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
       "frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'; "
       "connect-src 'none'; font-src 'none'; media-src 'none'")


BASE_CSS = (
    "html,body{margin:0;padding:12px;"
    "font:14px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}"
    "img{max-width:100%}table{max-width:100%}"
    ".mv-img{display:inline-block;border-style:dashed;border-width:1px;"
    "font-size:12px;padding:1px 6px;border-radius:3px;}"
    ".mv-url{font-size:11px;word-break:break-all;}"
    "a{text-decoration:underline;}"
)

LIGHT_CSS = (
    "html,body{background:#fff;color:#111;}"
    ".mv-img{background:#eee;border-color:#bbb;color:#666;}"
    ".mv-url{color:#b45309;}"
    "a{color:#1d4ed8;}"
)
# "As sent" leaves the sender's own colours alone, so it does NOT need the hidden-idiom
# guard - nothing is being forced visible there. It is the honest comparison view.

# DARK IS THE DEFAULT because this panel sits inside a dark dashboard and a full-width
# sheet of white is genuinely unpleasant to open at night.
#
# It is done with !important overrides rather than by rewriting the message's CSS: an
# author stylesheet with !important beats a non-important inline style, so the message
# cannot fight the theme, and the sanitiser stays a security component instead of also
# becoming a re-colouring engine. `color:inherit` on every descendant is the load-bearing
# rule - marketing mail is full of near-black text set for a white background, and without
# it half of every message would be dark-on-dark and effectively invisible. Losing the
# sender's palette costs nothing here: the images are blocked placeholders anyway, and
# anyone who wants it as designed can flip to "as sent".
# KEEP DELIBERATELY-INVISIBLE TEXT INVISIBLE.
#
# Almost every marketing mail carries a "preheader": a block of text hidden with
# color:transparent, font-size:0, opacity:0 or max-height:0 so the inbox preview shows it
# but the message body does not. Forcing `color: inherit` to fix dark-on-dark ALSO resurrects
# that hidden text, and because it is usually positioned in the header it lands directly on
# top of the real content - the first dark build rendered messages as unreadable piles of
# overlapping text, worse than the untouched light version. Anything the sender marked
# invisible stays invisible, and it is matched on the inline style because that is where
# email hides things.
# NOTE the narrowness of these selectors. The first version matched any style containing
# the word "transparent", which also matches `background:transparent` - a completely
# ordinary declaration on layout containers - so entire sections of real messages were
# hidden. A Steam mail rendered as one line of "Trouble viewing this message?" and nothing
# else. Only COLOUR-transparent counts as hiding text.
HIDDEN_IDIOMS = (
    '[style*="opacity:0"],[style*="opacity: 0"],'
    '[style*="font-size:0"],[style*="font-size: 0"],'
    '[style*="max-height:0"],[style*="max-height: 0"],'
    '[style*="color:transparent"],[style*="color: transparent"],'
    '[style*="display:none"],[style*="display: none"]'
    "{display:none !important;}"
)

DARK_CSS = (
    "html,body{background:#0f141a;color:#dbe3ea;}"
    "body *{background-color:transparent !important;color:inherit !important;"
    "border-color:#2b3542 !important;}"
    "a,a *{color:#7cb0ff !important;}"
    ".mv-url,.mv-url *{color:#f0b429 !important;}"
    ".mv-img{background:#1b232c !important;border-color:#3a4653 !important;"
    "color:#93a3b5 !important;}"
    "hr{border-color:#2b3542 !important;}"
    # Give the reader a document, not a collapsed table layout: the width/height attributes
    # that held these grids apart were stripped as attack surface, so tables need to behave
    # like flow content or they overlap and clip.
    "table,tbody,tr,td,th{display:block !important;width:auto !important;"
    "max-width:100% !important;height:auto !important;}"
    "td,th{padding:0 !important;}"
    + HIDDEN_IDIOMS
)


# --------------------------------------------------------------------------- reader mode
#
# THE POINT: stop trying to render the sender's LAYOUT safely, and render their CONTENT
# well instead.
#
# Faithful rendering is a fight that cannot be won here. The things that hold an email
# layout together - table widths, absolute positioning, background images, spacer GIFs -
# are exactly the things a sanitiser has to remove, so what is left collapses, and every
# patch (forced colours, block tables, hidden-idiom rules) breaks a different message.
# Every fix so far has traded one broken render for another.
#
# Reader mode sidesteps it. Take the already-sanitised markup, throw away ALL of the
# sender's styling, and re-emit a linear document in the dashboard's own typography. There
# is then nothing left to fight: no inline CSS to scrub, no geometry to preserve, no theme
# war. The attack surface shrinks at the same moment the render gets better, which is the
# whole reason security and presentation stopped being opposed.
#
# "As sent" remains for the rare message where the design carries meaning.

BLOCK_TAGS = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "thead", "tbody", "ul", "ol", "hr",
              "figure", "figcaption", "dl", "dt", "dd", "caption"}
INLINE_KEEP = {"b", "strong", "i", "em", "u", "s", "code", "mark", "sub", "sup", "small"}


class _Reader(HTMLParser):
    """Flatten sanitised markup into a linear list of blocks.

    Cells of one row are joined into a single line: email layouts routinely split one
    sentence across three <td>s ("MIDWEEK DEAL!", "-40%", "$49.99"), and treating each as
    its own paragraph is what makes a reader view look shredded.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks = []          # (kind, html)
        self.buf = []             # current inline run
        self.kind = "p"
        self.list_depth = 0
        self._a_open = False
        self._skip_depth = 0

    # -- block plumbing ----------------------------------------------------
    def _flush(self, kind=None):
        html = "".join(self.buf).strip()
        self.buf = []
        # drop runs that are only punctuation/whitespace left behind by stripped layout
        text = re.sub(r"<[^>]+>", "", html)
        text = re.sub(r"&[#a-zA-Z0-9]+;", " ", text).strip()
        if html and (text or "mv-img" in html):
            self.blocks.append((kind or self.kind, html))
        self.kind = "p"

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("br",):
            self._flush()
            return
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.kind = "h"
            elif tag == "li":
                self.kind = "li"
            elif tag == "blockquote":
                self.kind = "quote"
            elif tag == "pre":
                self.kind = "pre"
            elif tag == "hr":
                self.blocks.append(("hr", ""))
            return
        if tag == "a":
            d = dict((k.lower(), v) for k, v in attrs)
            self.buf.append('<a data-href="%s">' % escape(d.get("data-mv-href") or "",
                                                          quote=True))
            self._a_open = True
            return
        if tag in INLINE_KEEP:
            self.buf.append("<%s>" % tag)
            return
        if tag == "span":
            cls = dict((k.lower(), v) for k, v in attrs).get("class", "") or ""
            # The mv-url span is the sanitiser's inline "[where this really goes]" note.
            # Reader mode carries that on the link itself, so the span would be a duplicate
            # - suppress it and its text entirely rather than printing the URL twice.
            if "mv-url" in cls:
                self._skip_depth = getattr(self, "_skip_depth", 0) + 1
                return
            if "mv-" in cls:
                self.buf.append('<span class="%s">' % escape(cls, quote=True))
            return

    def handle_endtag(self, tag):
        tag = tag.lower()
        if getattr(self, "_skip_depth", 0):
            if tag == "span":
                self._skip_depth -= 1
            return
        if tag in BLOCK_TAGS:
            self._flush()
            return
        if tag == "a" and self._a_open:
            self.buf.append("</a>")
            self._a_open = False
            return
        if tag in INLINE_KEEP:
            self.buf.append("</%s>" % tag)
        elif tag == "span":
            self.buf.append("</span>")

    def handle_data(self, data):
        if getattr(self, "_skip_depth", 0):
            return
        self.buf.append(escape(data))

    def handle_entityref(self, name):
        if getattr(self, "_skip_depth", 0):
            return
        self.buf.append("&%s;" % name if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,31}", name)
                        else "")

    def handle_charref(self, name):
        if getattr(self, "_skip_depth", 0):
            return
        self.buf.append("&#%s;" % name if re.fullmatch(r"[xX]?[0-9A-Fa-f]{1,8}", name)
                        else "")

    def close(self):
        super().close()
        self._flush()


def _host(url):
    m = re.match(r"\s*[a-z]+://([^/\s?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def _reg_domain(host):
    """Crude registrable domain: enough to tell 'same outfit' from 'somewhere else'."""
    parts = (host or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


def render_reader(safe_body, sender="", profile=None):
    """Turn sanitised markup into a clean linear document.

    Links are judged, not merely printed. THREE outcomes, in order of evidence:

      * the sender has an ESTABLISHED profile and has used this host before -> quiet. This
        is what stops the false alarms: a bank's url1719.example-bank.org and
        Facebook's facebook.com are normal FOR THEM, which a domain comparison cannot know.
      * the sender has an established profile and has NEVER used this host -> flagged. This
        is the one that matters, because impersonating a trusted sender is the attack that
        actually happens, and a domain check is blind to it.
      * no profile worth the name -> fall back to the domain comparison and say so, rather
        than pretending thin evidence is a clean bill of health.
    """
    r = _Reader()
    try:
        r.feed(safe_body or "")
        r.close()
    except Exception:
        return "<p><i>[this message could not be re-rendered for reading]</i></p>"

    sender_dom = _reg_domain(_host("http://" + (sender.split("@")[-1].strip(">") or "")))
    # Only an ESTABLISHED profile is allowed to make a host quiet. A thin one is treated as
    # no profile at all, so an attacker cannot earn trust by simply being the first mail
    # this lane ever saw from that name.
    known = set((profile or {}).get("hosts") or ()) if (profile or {}).get("established") \
        else set()
    out, in_list = [], False
    seen = set()
    for kind, html in r.blocks:
        # annotate links now that we know the sender's domain
        def _link(m):
            href = m.group(1)
            h = _host(href)
            if not h:
                return "<a>"
            if known:
                # Evidence beats heuristic: judge against what this sender ACTUALLY uses.
                cls = "lk" if h in known else "lk new"
                label = h if h not in known else ""
            else:
                # No profile yet - fall back to the domain comparison, and mark it as the
                # weaker test it is so a quiet link is not mistaken for a vouched-for one.
                off = sender_dom and _reg_domain(h) != sender_dom
                cls = "lk off" if off else "lk"
                label = h if off else ""
            return '<a class="%s" title="%s" data-h="%s">' % (
                cls, escape(href[:400], quote=True), escape(label))
        body = re.sub(r'<a data-href="([^"]*)">', _link, html)
        # a block repeated verbatim is layout scaffolding, not content
        key = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", body)).strip().lower()
        if key and key in seen and len(key) < 60:
            continue
        if key:
            seen.add(key)
        if kind == "li":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % body)
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if kind == "hr":
            out.append("<hr>")
        elif kind == "h":
            out.append("<h3>%s</h3>" % body)
        elif kind == "quote":
            out.append("<blockquote>%s</blockquote>" % body)
        elif kind == "pre":
            out.append("<pre>%s</pre>" % body)
        else:
            out.append("<p>%s</p>" % body)
    if in_list:
        out.append("</ul>")
    return "".join(out) or "<p><i>[this message has no readable content once its layout is removed]</i></p>"


READER_CSS = (
    "html,body{background:#0f141a;color:#dbe3ea;}"
    ".rd{max-width:70ch;margin:0 auto;font-size:15px;line-height:1.62;}"
    ".rd p{margin:0 0 13px;}"
    ".rd h3{font-size:17px;margin:22px 0 9px;color:#fff;line-height:1.35;}"
    ".rd ul{margin:0 0 13px 0;padding-left:20px;}"
    ".rd li{margin:0 0 5px;}"
    ".rd hr{border:0;border-top:1px solid #2b3542;margin:20px 0;}"
    ".rd blockquote{margin:0 0 13px;padding:2px 0 2px 12px;"
    "border-left:2px solid #2b3542;color:#aab6c2;}"
    ".rd pre{white-space:pre-wrap;background:#151c24;padding:9px;border-radius:5px;}"
    # A link that stays on the sender's own domain is unremarkable and stays quiet; one
    # that leaves it carries its host, because that is the phishing tell.
    ".rd a{color:#7cb0ff;text-decoration:underline;}"
    # amber = off the sender's domain, but judged only by the weak domain test
    ".rd a.off{color:#f0b429;}"
    ".rd a.off::after{content:' [' attr(data-h) ']';font-size:11px;opacity:.85;}"
    # red = this sender has an established history and has NEVER used this host. That is
    # the one worth stopping on, so it is the only one that gets a loud treatment.
    ".rd a.new{color:#ff8f8f;background:#2a1618;padding:0 3px;border-radius:3px;}"
    ".rd a.new::after{content:' [NEW HOST: ' attr(data-h) ']';font-size:11px;"
    "font-weight:600;}"
    ".rd .mv-img{display:inline-block;background:#1b232c;border:1px dashed #3a4653;"
    "color:#8fa0b1;font-size:11px;padding:1px 6px;border-radius:3px;}"
    ".rd .mv-url{display:none;}"     # reader mode carries the host on the link itself
)


def wrap_document(safe_body, theme="dark"):
    """Wrap sanitised markup in a document that itself permits no network and no script.

    Belt and braces with the sanitiser: even if something slipped through above, this CSP
    gives it nowhere to go. The caller puts this in an <iframe sandbox srcdoc> with no
    allow-scripts and no allow-same-origin, which is the third independent barrier.

    theme: "reader" (default - the sender's layout discarded, content re-rendered in our
    own typography), "dark" (their layout, our palette), or "light" (as sent).
    """
    if theme == "reader":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='Content-Security-Policy' content=\"" + CSP + "\">"
            "<style>" + BASE_CSS + READER_CSS + "</style></head>"
            "<body><div class='rd'>" + safe_body + "</div></body></html>")
    skin = LIGHT_CSS if theme == "light" else DARK_CSS
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"" + CSP + "\">"
        "<style>" + BASE_CSS + skin + "</style></head><body>" + safe_body + "</body></html>")
