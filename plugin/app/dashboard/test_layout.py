"""The structure that keeps the working panels on screen.

WHAT THIS IS GUARDING. Measured at 1600x900 before the rearrangement: header, the two
attention panels, the KPI row, the record and the account grid were five full-width rows
stacked one under another, and they consumed 638 vertical pixels before the two panels
people actually read even started. Those got 408 and the page scrolled - about two emails
at a time on a laptop.

Stacked, a top region costs the SUM of its sections. Side by side it costs the tallest one.
That is the whole change, and it is a STRUCTURAL fact rather than a stylistic one: putting
any of these sections back at the top level of <body> silently restores the old behaviour,
with nothing failing and nothing to see except a shorter panel.

A layout cannot be fully tested without a browser, and the measurements above were taken in
one. What is asserted here is the arrangement those measurements depend on.
"""
import os
import re
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")
CSS = os.path.join(HERE, "static", "style.css")


class Tree(HTMLParser):
    """Just enough DOM: every element's tag, id, class and parent chain."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes = [], []
        self.void = {"meta", "link", "br", "img", "input", "hr"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        node = {"tag": tag, "id": a.get("id", ""), "class": a.get("class", ""),
                "parents": [n["id"] or n["class"] for n in self.stack],
                "parent_ids": [n["id"] for n in self.stack]}
        self.nodes.append(node)
        if tag not in self.void:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break


def tree():
    with open(INDEX, encoding="utf-8") as f:
        t = Tree()
        t.feed(f.read())
    return t.nodes


def css(strip_comments=False):
    with open(CSS, encoding="utf-8") as f:
        text = f.read()
    if strip_comments:
        # A rule block cannot be found by matching to the first `}` when a COMMENT inside it
        # contains one - and this file's comments quote CSS, so they do. That is what made
        # this test fail against correct code: it read a truncated block and reported the
        # wrong declaration as the effective one.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return text


class TheTopBandHoldsWhatItShould(unittest.TestCase):

    def setUp(self):
        self.nodes = tree()

    def in_band(self, ident):
        for n in self.nodes:
            if (n["id"] == ident or ident in n["class"].split()) and \
                    any("topband" in (p or "") for p in n["parents"]):
                return True
        return False

    def test_the_four_columns_are_inside_the_band(self):
        for ident in ("kpis", "accountPanel", "heatPanel", "band-side"):
            self.assertTrue(self.in_band(ident),
                            "%s is outside .topband, which puts it back on its own "
                            "full-width row and undoes the whole change" % ident)

    def test_they_are_in_the_owners_order(self):
        """counts, who is connected, the record, then legend and scoreboard."""
        order, seen = [], set()
        for n in self.nodes:
            if not any("topband" in (p or "") for p in n["parents"]):
                continue
            for ident in ("kpis", "accountPanel", "heatPanel", "band-side"):
                if (n["id"] == ident or ident in n["class"].split()) and ident not in seen:
                    seen.add(ident)
                    order.append(ident)
        self.assertEqual(order, ["kpis", "accountPanel", "heatPanel", "band-side"])

    def test_nothing_else_sits_between_the_header_and_the_working_area(self):
        """The rule that keeps this from creeping back: a new full-width section at the
        top of <body> costs its whole height, every time, on every screen."""
        allowed = {"header", "alerts", "topband", "split", "footer"}
        top = [n for n in self.nodes
               if n["tag"] in ("section", "div", "header", "footer", "aside")
               and not n["parents"]]
        for n in top:
            name = n["tag"] if n["tag"] in ("header", "footer") else \
                (n["class"].split() or [n["id"]])[0]
            if name.startswith("modal") or "modal" in n["class"]:
                continue          # overlays cost no layout height
            self.assertIn(name, allowed,
                          "%r is a new full-width row above the working area" % name)


class TheAttentionRowIsCapped(unittest.TestCase):

    def test_the_conditional_panels_share_one_row(self):
        nodes = tree()
        for ident in ("setupPanel", "wfPanel", "openPanel", "hostPanel"):
            found = [n for n in nodes if n["id"] == ident]
            self.assertTrue(found, ident)
            self.assertTrue(any("alerts" in (p or "") for p in found[0]["parents"]),
                            "%s is outside .alerts, so two of them showing at once "
                            "stacks again" % ident)

    def test_each_list_caps_its_height_and_scrolls(self):
        """An outstanding list that grows without bound pushes the mail off the screen -
        which turns the panel that exists to stop things being missed into the reason."""
        body = css(strip_comments=True)
        rule = re.search(r"\.alerts #wfList[^{]*\{([^}]*)\}", body)
        self.assertIsNotNone(rule, "no cap rule for the alert lists")
        self.assertIn("max-height", rule.group(1))
        self.assertIn("overflow-y", rule.group(1))

    def test_the_row_disappears_when_every_panel_is_hidden(self):
        self.assertIn(".alerts:not(:has(> section:not([hidden])))", css(),
                      "an empty attention row must cost no height at all")


class TheWorkingAreaFloorIsViewportAware(unittest.TestCase):

    def test_the_split_floor_is_not_a_fixed_pixel_value(self):
        """A fixed 408px was itself causing the page scroll it was written to prevent: once
        the chrome above shrank, 408 no longer fitted a 768px laptop."""
        # The WHOLE block, because it carries two min-height declarations: `0` to let the
        # children shrink and scroll, then the floor. Reading the first one and calling it
        # the floor is how this test failed against correct code - the check has to look at
        # what the browser would actually apply, which is the last one to win.
        block = re.search(r"^\.split\s*\{(.*?)\}", css(strip_comments=True),
                          re.S | re.M)
        self.assertIsNotNone(block, "the split has no rule block at all")
        floors = re.findall(r"min-height:\s*([^;]+);", block.group(1))
        self.assertTrue(floors, "the split has no min-height at all")
        self.assertIn("vh", floors[-1],
                      "the floor must be expressed against the viewport, or a short "
                      "screen scrolls the page instead of shrinking the panel")

    def test_short_screens_get_their_own_rules(self):
        body = css()
        for query in ("max-height: 850px", "max-height: 760px"):
            self.assertIn(query, body,
                          "no rules for %s - the small-laptop case is the one this was "
                          "all for" % query)

    def test_the_record_scales_rather_than_being_clipped_on_short_screens(self):
        """Clipping it would hide months of history to save pixels."""
        m = re.search(r"@media \(max-height: 760px\)\s*\{(.*?)\n\}", css(), re.S)
        self.assertIsNotNone(m)
        self.assertIn(".topband .heat-wrap svg", m.group(1))
        self.assertNotIn("overflow-y: hidden", m.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
