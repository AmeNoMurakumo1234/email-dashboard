"""
Refresh live prices for tracked Steam wishlist sales, and retire ended ones.

For every ACTIVE sale in the steam_sales table, query Steam's public store API
(no key needed) for the current price_overview:

  https://store.steampowered.com/api/appdetails?appids=<id>&cc=us&filters=price_overview

- discount_percent > 0  -> still on sale: store real prices (initial/final/discount).
- discount_percent == 0, or no price_overview (sale gone / app now free/delisted)
  -> mark the sale ENDED (active=0, ended_at=today) so it drops out of the panel.

This is what makes the dashboard's Steam panel show "current knowledge of actual
sales": the email tells us a sale STARTED; this tells us it's still real and when
it stops. Run it after ingest.py each routine run (and any time you want fresh
prices). Stdlib only.

  python dashboard/steam_refresh.py [--all] [--date YYYY-MM-DD] [--cc us]

--all    also re-check sales already marked ended (e.g. to confirm/repair state)
--date   the date to stamp on newly-ended sales (defaults to today, local)
--cc     Steam country/currency code for pricing (default us)
"""
import argparse
import json
import os
import re
import urllib.request
from datetime import date as _date

import db

# Stored subjects contain whatever a sender typed, and a Windows console defaults to
# cp1252 - so printing one used to abort the whole listing with a UnicodeEncodeError.
try:
    from consoleio import safe_console
except ImportError:  # running from another cwd
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from consoleio import safe_console
safe_console()


API = "https://store.steampowered.com/api/appdetails?appids={app}&cc={cc}&filters=price_overview"
STORE = "https://store.steampowered.com/app/{app}/?cc={cc}&l=english"
UA = "email-dashboard-steam-refresh/1.0 (localhost)"

_HERE = os.path.dirname(os.path.abspath(__file__))
_COOKIE_CONFIG_NAMES = ("steam_refresh.local.json", "steam_refresh.example.json")


def _load_store_cookie():
    """Load Steam's public age-gate cookie from config (NOT a login/session token).

    Resolution: steam_refresh.local.json (gitignored) then steam_refresh.example.json
    (committed placeholder). The cookie only clears Steam's mature-content interstitial
    so the store page renders its "Offer ends ..." countdown; it carries no auth/session
    data. Returns "" when only the placeholder is present, in which case no Cookie header
    is sent and age-gated titles simply get no scraped end date (already tolerated).
    """
    for name in _COOKIE_CONFIG_NAMES:
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cookie = (json.load(fh).get("store_cookie") or "").strip()
            # Any unreplaced placeholder disables the cookie entirely. Matching the common
            # prefix rather than one exact token means adding a second placeholder to the
            # template cannot silently start sending "AGE_GATE_DATE" to Steam as if it
            # were a real value.
            return "" if "AGE_GATE" in cookie else cookie
    return ""


# Browser-ish UA + (optional) public age-gate cookie so the store page renders the
# purchase block / "Offer ends ..." countdown instead of an age-check interstitial.
STORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) email-dashboard-steam-refresh/1.0",
}
_STORE_COOKIE = _load_store_cookie()
if _STORE_COOKIE:
    STORE_HEADERS["Cookie"] = _STORE_COOKIE
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"]) if m}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})  # also accept 3-letter abbrevs


def fetch_price(app_id, cc="us", timeout=15):
    """Return the price_overview dict for app_id, or None if not on sale / unavailable."""
    url = API.format(app=int(app_id), cc=cc)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    entry = (payload or {}).get(str(app_id)) or {}
    if not entry.get("success"):
        return None
    return (entry.get("data") or {}).get("price_overview")  # None when no active discount


def _parse_offer_end(text, today=None):
    """Parse Steam's 'Offer ends <Month> <Day>' countdown text into an ISO date.

    Steam shows the end as a year-less date (e.g. 'Offer ends June 25'); we attach
    the current year, rolling to next year if that date already looks past (handles
    a Dec->Jan sale boundary). 'Offer ends in N hours/minutes' (the imminent-end
    form) -> ends today. Returns an ISO date string, or None if nothing parseable.
    """
    today = today or _date.today()
    m = re.search(r"Offer ends\s+([A-Za-z]+)\s+(\d{1,2})", text)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            day = int(m.group(2))
            year = today.year
            # Steam omits the year. The only real ambiguity is the year-end wrap:
            # in late December a sale can run into early January of next year.
            if today.month == 12 and mon == 1:
                year += 1
            try:
                return _date(year, mon, day).isoformat()
            except ValueError:
                return None
    if re.search(r"Offer ends in\s", text):  # ends within ~48h: Steam shows a live countdown
        return today.isoformat()
    return None


def fetch_sale_end(app_id, cc="us", timeout=20):
    """Scrape the store page's purchase countdown -> ISO end date, or None."""
    url = STORE.format(app=int(app_id), cc=cc)
    req = urllib.request.Request(url, headers=STORE_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    # the base-game countdown line, e.g. "SPECIAL PROMOTION! Offer ends June 25"
    m = re.search(r'game_purchase_discount_countdown[^>]*>([^<]*)<', html)
    snippet = m.group(1) if m else html
    return _parse_offer_end(snippet)


def refresh(include_ended=False, ended_date=None, cc="us"):
    ended_date = ended_date or _date.today().isoformat()
    sales = db.list_steam_sales(active_only=not include_ended)
    results = {"checked": 0, "still_on_sale": 0, "ended": 0, "errors": 0}
    for s in sales:
        app_id = s["app_id"]
        results["checked"] += 1
        checked = db.now_iso()
        try:
            po = fetch_price(app_id, cc=cc)
        except Exception as e:
            results["errors"] += 1
            print(f"  ! {app_id} {s.get('title') or ''}: {e}")
            continue
        if po and (po.get("discount_percent") or 0) > 0:
            db.update_steam_price(
                app_id,
                discount_pct=po.get("discount_percent"),
                price_initial=po.get("initial"),
                price_final=po.get("final"),
                currency=po.get("currency"),
                price_initial_fmt=po.get("initial_formatted"),
                price_final_fmt=po.get("final_formatted"),
                checked_iso=checked,
            )
            # Steam's price API has no end date; the store page's countdown does.
            try:
                sale_ends = fetch_sale_end(app_id, cc=cc)
            except Exception:
                sale_ends = None   # store page unavailable/age-gated -> leave end unknown
            db.update_steam_end(app_id, sale_ends)
            results["still_on_sale"] += 1
            print(f"  = {app_id} {s.get('title') or ''}: {po.get('discount_percent')}% off "
                  f"{po.get('initial_formatted')} -> {po.get('final_formatted')}"
                  f"{' · ends ' + sale_ends if sale_ends else ''}")
        else:
            db.mark_steam_ended(app_id, ended_date, checked)
            results["ended"] += 1
            print(f"  x {app_id} {s.get('title') or ''}: sale ended -> dropped from panel")
    return results


def main():
    ap = argparse.ArgumentParser(description="Refresh Steam sale prices; retire ended sales")
    ap.add_argument("--all", action="store_true", help="also re-check already-ended sales")
    ap.add_argument("--date", help="date to stamp on newly-ended sales (default: today)")
    ap.add_argument("--cc", default="us", help="Steam country/currency code (default us)")
    args = ap.parse_args()
    db.init_db()
    res = refresh(include_ended=args.all, ended_date=args.date, cc=args.cc)
    print(json.dumps({"ok": True, **res}))


if __name__ == "__main__":
    main()
