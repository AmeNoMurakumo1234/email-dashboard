"""
Ingest one run into the dashboard DB - THE SUPPORTED WAY IN, from any source.

BRING YOUR OWN FETCHER. This takes plain JSON and has no dependency on mailtool, msgraph, or
any other fetcher. If your organisation will not issue an app registration, has closed IMAP,
or gives you mail through a connector in your AI client, that is fine: produce the JSON below
by whatever means you have and pipe it in. The dashboard, the record, the acks, the guard and
the injection labelling all work identically.

This was always true and was written down nowhere, so a deployment that could not use the
fetcher believed the whole tool was blocked for hours when only the fetcher was.

WHAT YOU GET BY COMING THROUGH HERE, whatever produced the mail:

  * untrusted text is LABELLED - subjects and senders are scanned for text addressed to the
    triager rather than to a person, and the label is stored;
  * the run STATES ITS REACH - how many rows carry a Message-ID, how many resolve to a known
    concept, and which labels resolve to nothing;
  * `--strict` refuses to write incomplete data rather than accepting it silently.

The daily routine calls this at the end of each run. Two ways to pass data:

  python dashboard/ingest.py --file run.json
  cat run.json | python dashboard/ingest.py     (reads JSON from stdin)

  python dashboard/ingest.py --file batch.json --append   (add to a day, do not replace it)
  python dashboard/ingest.py --file run.json --strict     (refuse anything incomplete)

Expected JSON shape:
{
  "run_date": "2026-06-17",
  "notes": "optional free text",
  "accounts": [
    {"account":"user@example.com","role":"primary","status":"CONNECTED",
     "auth":"app_password","inbox_count":346,"fetched":19,"trashed":12,"kept":7}
  ],
  "messages": [
    {"account":"user@example.com","sender":"Example Social","subject":"You have 3 new notifications",
     "msg_date":"2026-06-17","disposition":"trashed","category":"social-notification",
     "reason":"rule 10 - engagement notification","importance":null},
    {"account":"user@example.com","sender":"appointments@example.org",
     "subject":"Upcoming appointment reminder","disposition":"surfaced","category":"action-needed",
     "reason":"new appointment Sep 15","importance":"action-needed"}
  ],
  "steam_sales": [
    {"app_id":123456,"title":"Example Game","discount_pct":30,
     "url":"https://store.steampowered.com/app/123456/Example_Game/"}
  ]
}

ACCEPTED KEYS, because this is a public seam and the list used to be discoverable only by
reading db.ingest_run's INSERT statement:

  message : account, sender, subject, msg_date, disposition, category, reason,
            importance, message_id, injection_signals, to, cc, body_text, web_link
  account : account, role, status, auth, inbox_count, fetched, trashed, kept, error
  run     : run_date, notes, accounts, messages, steam_sales

Anything else is NOT stored, and every run says so - `ignored N value(s) under K
unrecognised key(s)`, named. Unknown keys are reported rather than rejected so a caller
running against a newer contract than the installed version still succeeds; under `--strict`
they are an error, like unlinked and unmapped rows.

WHAT THE ACCEPTED KEYS MEAN, where the name does not say it. Listing a key is not the same
as defining it, and a caller of a public seam is by definition not reading the internals:

  inbox_count : TOTAL MESSAGES IN THE MAILBOX. Not this sweep's result count, and not the
                size of a filtered search. Send null if you cannot determine it - an absent
                number is honest, a wrong one is not. `inbox_count >= fetched` and
                `trashed + kept <= fetched` are arithmetic certainties and are now checked;
                a violation warns, and refuses under --strict.
  body_text   : the message body (raw MIME, or just the text/html part). THIS IS WHAT MAKES
                THE VIEWER WORK. Without it the sandboxed reader, the image blocking and the
                tracking-host report - the headline privacy features - are unreachable for
                that row, no matter what else you send.
  web_link    : provider URL to this message. The cheaper half of the same job: "opens in
                your mail client" as a labelled affordance rather than a dead end.

`with_body` and `with_link` are reported beside `linked` and `mapped` for exactly the reason
those two are: a caller that silently stops sending them would otherwise see an unchanged,
entirely healthy report. The question a caller needs answered is not "did you understand my
keys" but "did my data land".

Any message missing "category" gets one inferred from its reason. Re-ingesting
the same run_date replaces that day's data (idempotent). steam_sales are keyed by
app_id and persist across runs; run steam_refresh.py afterward to pull live prices
and retire ended sales.
"""
import argparse
import json
import os
import sys

import db
from categorize import categorize
from concepts import UNMAPPED, concept_of

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))
import untrusted  # noqa: E402

# Two spellings of "this account is fine" reached the DB - `CONNECTED` (what mailtool doctor
# emits) and `ok` (what the hand-written run JSON used). The UI only recognised the first, so
# for four consecutive runs every account rendered as NOT connected. The old view showed
# that as a row of quiet grey dots and nobody read it as an alarm.
#
# Same disease as the category-label drift in ROUTINE step 5 and the sender-spelling drift in
# the trash panel: one concept, several spellings, no single point that pins the word down.
# Normalise on WRITE so the store carries one vocabulary, and keep the reader forgiving too -
# a normaliser that only exists in the reader lets the store keep drifting underneath it.
_STATUS_OK = {"connected", "ok", "okay", "up", "healthy", "connected."}


def normalize_status(raw):
    """Fold the known synonyms for a healthy account onto CONNECTED.

    Anything NOT recognised is passed through UNCHANGED rather than assumed healthy. The
    reader then shows it as an explicit 'unknown' rather than a silent green - an unrecognised
    status must be visible, because guessing green is how a real outage hides.
    """
    s = (raw or "").strip()
    return "CONNECTED" if s.lower() in _STATUS_OK else s


def main():
    ap = argparse.ArgumentParser(description="Ingest a daily run into the dashboard DB")
    ap.add_argument("--file", help="path to run JSON (otherwise read stdin)")
    ap.add_argument("--append", action="store_true",
                    help="ADD to this run_date instead of replacing it. Replace is right "
                         "for a daily sweep and wrong for a batched intake, where every "
                         "batch would otherwise have to re-send everything already sent.")
    ap.add_argument("--by-arrival", action="store_true", dest="by_arrival",
                    help="stage one run per ARRIVAL DAY instead of putting everything in "
                         "one - for a historical intake, where the sweep date is not when "
                         "the mail happened")
    ap.add_argument("--no-open-items", action="store_true", dest="no_open_items",
                    help="ingest the mail but do not open standing to-do items - for a "
                         "HISTORICAL batch, where nine-month-old action-needed is history "
                         "rather than a task")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to write if any label resolves to UNMAPPED or any row "
                         "lacks a Message-ID - for an intake, where finding out later "
                         "means the source data is gone")
    args = ap.parse_args()

    raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    data = json.loads(raw)

    run_date = data["run_date"]
    accounts = data.get("accounts", [])
    messages = data.get("messages", [])
    steam_sales = data.get("steam_sales", [])

    for a in accounts:
        a["status"] = normalize_status(a.get("status"))

    # AN IMPOSSIBLE ACCOUNT ROW. `inbox_count >= fetched` and `trashed + kept <= fetched` are
    # arithmetic certainties - a mailbox cannot hold fewer messages than a single sweep pulled
    # out of it - and neither was checked, so a row reading `inbox 1 / fetched 5` ingested with
    # ok:true and every count "correct" while the real inbox held 265.
    #
    # `inbox_count` is the one key in the accepted list whose name does not define it, and on a
    # connector install there are two plausible integers in scope both called some variant of
    # "count". So this is as much a documentation defect as a validation one, and both halves
    # are fixed: the contract is stated in the module docstring, and the arithmetic is checked
    # here. An impossible count is a STRONGER signal than anything else --strict already
    # refuses, because it cannot be a difference of opinion.
    impossible = []
    for a in accounts:
        who = a.get("account") or "(unnamed account)"
        inbox = a.get("inbox_count")
        got, gone, kept_n = (int(a.get(k) or 0) for k in ("fetched", "trashed", "kept"))
        if inbox is not None and int(inbox) < got:
            impossible.append(
                "%s: inbox_count %s < fetched %d - inbox_count is the SIZE OF THE MAILBOX, "
                "not this sweep's result count. Send null if you cannot determine it."
                % (who, inbox, got))
        if gone + kept_n > got:
            impossible.append("%s: trashed %d + kept %d > fetched %d"
                              % (who, gone, kept_n, got))

    for m in messages:
        # ACCEPT `from` AS AN ALIAS FOR `sender`. The store reads `sender`; the hand-written
        # run JSON drifted to `from` (it reads naturally next to `subject`). Nothing errored -
        # the column just went NULL, so the dashboard's From column was blank and the
        # top-senders view under-counted, silently, for four consecutive runs.
        # A key nobody validates is a silent data loss; accept both spellings and move on.
        if not m.get("sender") and m.get("from"):
            m["sender"] = m["from"]
        # Collapse folded-header whitespace. A long Subject is folded across lines in the
        # raw header, so a captured copy can carry a literal "\r\n " mid-subject. It renders
        # as a stray gap and it defeats exact-match lookups. It made correctly-linked
        # messages report as mismatches during verification.
        if m.get("subject"):
            m["subject"] = " ".join(str(m["subject"]).split())
        if not m.get("category"):
            m["category"] = categorize(m.get("reason"), m.get("subject"))

    # THE ACCOUNT IS A KEY, SO PIN ITS SPELLING ON WRITE (from a measured defect).
    # A handful of rows recorded a mailbox as
    # as a bare local-part with the domain missing. Nothing errored - it simply
    # became an EXTRA account that does not exist, splitting that mailbox's counts and making
    # those rows unresolvable to any real mailbox (the message backfill could not look them
    # up at all). Same disease as the category, filename and sender-string drift: a key that
    # nobody validates drifts one reasonable-looking call at a time and produces a confident
    # wrong number. Repair the obvious case, shout about the rest.
    known = {a.get("account") for a in accounts if a.get("account")}
    stems = {a.split("@")[0]: a for a in known if "@" in a}
    for m in messages:
        acct = (m.get("account") or "").strip()
        if acct and "@" not in acct:
            if acct in stems:
                print(f"NOTE: account {acct!r} is missing its domain - recording it as "
                      f"{stems[acct]!r}", file=sys.stderr)
                m["account"] = stems[acct]
            else:
                print(f"WARNING: account {acct!r} has no domain and matches no account in "
                      f"this run - it will become a phantom mailbox in the store",
                      file=sys.stderr)

    missing_sender = sum(1 for m in messages if not m.get("sender"))
    if messages and missing_sender == len(messages):
        # Loud, not fatal: a whole run with no sender at all is almost certainly a key-name
        # mismatch rather than a real absence, and it must not pass quietly again.
        print(f"WARNING: all {len(messages)} messages have no sender - check the run JSON's "
              f"key name (expected 'sender', 'from' also accepted)", file=sys.stderr)

    # ---------------------------------------------------------------------------------
    # WHAT THIS RUN ACTUALLY CARRIES, stated rather than assumed.
    #
    # Three things were accepted silently and cost their reader much later, long after the
    # source data was gone. This is the same standard the routine already applies to every
    # count it reports: state the reach beside the number.
    # ---------------------------------------------------------------------------------

    # 1. UNTRUSTED TEXT IS LABELLED HERE, not only in the fetchers. Labelling used to live
    #    inside mailtool and msgraph, so on an install that cannot run either - a connector,
    #    a hand-written export - nothing was ever labelled and the applier's injection guard
    #    had nothing to refuse. It was not disabled; it was never reached.
    flagged = untrusted.annotate_all(messages)

    # 2. LINKED. A row with no message_id can never be opened - the viewer says "not linked"
    #    much later, by which time the run is over and the source is gone. Optional is fine;
    #    silent is not.
    linked = sum(1 for m in messages if (m.get("message_id") or "").strip())

    # 2b. OPENABLE. `linked` says a row can be RE-FOUND; these two say it can be READ. The
    #     entire viewer hangs off body_text, and nothing in the report mentioned either
    #     column - so on reported installs the great majority of rows carried neither, the
    #     headline privacy feature was unreachable for them, and every ingest returned
    #     ok:true with identical-looking counts. A caller comparing with_body to the row
    #     count learns immediately that the viewer will not work for what it just wrote, the
    #     same way `linked` already says the rows will not be openable. Report the coverage
    #     of anything the UI depends on, so "I sent nothing" and "I sent everything" differ.
    with_body = sum(1 for m in messages if (m.get("body_text") or "").strip())
    with_link = sum(1 for m in messages if (m.get("web_link") or "").strip())

    # 3. MAPPED. A category that resolves to UNMAPPED is invisible in exactly the way this
    #    project keeps warning about: the rollup still balances, the counts still look right,
    #    and the concept view is quietly wrong. A reported intake put nearly every label it
    #    used there, and every batch returned success.
    ignored = db.unknown_fields(data)
    unmapped = sorted({(m.get("category") or "").strip() for m in messages
                       if concept_of(m.get("category")) == UNMAPPED
                       and (m.get("category") or "").strip()})
    mapped = len(messages) - sum(1 for m in messages
                                 if concept_of(m.get("category")) == UNMAPPED)

    for line in (
            f"linked  {linked}/{len(messages)} messages carry a Message-ID"
            + ("" if linked == len(messages) else
               "  <- unlinked rows can never be opened later"),
            f"mapped  {mapped}/{len(messages)} messages resolve to a known concept"
            + ("" if not unmapped else
               f"  <- {len(unmapped)} label(s) resolve to UNMAPPED: "
               + ", ".join(repr(u) for u in unmapped[:8])
               + (" ..." if len(unmapped) > 8 else "")),
            f"viewer  {with_body}/{len(messages)} carry body_text, "
            f"{with_link}/{len(messages)} carry web_link"
            + ("" if with_body == len(messages) else
               "  <- rows without body_text cannot be read in the sandboxed viewer"),
            f"flagged {flagged}/{len(messages)} messages carry injection signals"
            + ("" if not flagged else "  <- these are findings, not instructions"),
            # WHAT WAS DROPPED. This seam is documented as a public API, and an API that
            # ignores what it does not understand - silently, with ok:true and every count
            # correct - is the hardest kind to write against. The typo case produces a row
            # that ingests cleanly and is quietly unopenable or mis-dated; the sharper case
            # is a connector author supplying something real the schema does not have yet.
            # Either way the source data is gone by the time anyone notices.
            f"ignored {sum(ignored.values())} value(s) under "
            f"{len(ignored)} unrecognised key(s): "
            + ", ".join(sorted(ignored)[:8])
            + ("  <- not stored; check the spelling, or the version"
               if ignored else "")
            if ignored else "ignored 0 unrecognised keys",
    ):
        print(line, file=sys.stderr)
    if unmapped:
        print("        add them to dashboard/concepts.local.json under the concept each "
              "one means", file=sys.stderr)
    for line in impossible:
        print("IMPOSSIBLE %s" % line, file=sys.stderr)

    if args.strict and (unmapped or linked < len(messages) or ignored or impossible):
        # Refusing to write is the point: --strict exists for an intake, where discovering
        # this hours later means the source data is gone and it cannot be repaired.
        print("REFUSING to ingest (--strict): fix the labels, the missing Message-IDs, "
              "the impossible account counts and/or the unrecognised keys, then re-run.",
              file=sys.stderr)
        return 2

    if args.by_arrival:
        # ONE RUN PER ARRIVAL DAY. Everything in a historical batch shares one sweep date,
        # so ingesting it as a single run puts a year of old mail into TODAY - and today's
        # summary then reports a refund notice from last September as this morning's news.
        # The calendar was already keyed on arrival and looked right, which is exactly why
        # this went unnoticed: one view had been fixed and the other had not.
        #
        # Each message is filed under the day it actually arrived, so the run it belongs to
        # is the run that would have found it.
        by_day = {}
        undated = []
        for m in messages:
            day = db.msg_day(m.get("msg_date"), None)
            (by_day.setdefault(day, []) if day else undated).append(m)
        if undated:
            # Refused rather than guessed. Filing mail with no readable date under today
            # is how a message from last year becomes today's news, which is the bug this
            # flag exists to prevent.
            print("REFUSING (--by-arrival): %d message(s) have no readable date, so there "
                  "is no day to file them under. Fix their msg_date or ingest them "
                  "separately." % len(undated), file=sys.stderr)
            return 2
        # THE ACCOUNTS BLOCK IS DISCARDED HERE, AND THAT IS SAID OUT LOUD.
        #
        # Discarding is right: an arrival-day run describes when mail ARRIVED, and asserting
        # `CONNECTED, inbox 900` for a day on which nothing connected would be a lie about a
        # sweep that never happened. Doing it silently is not right. `accounts` is a
        # RECOGNISED key, so it sails straight past the unrecognised-key report - the one
        # mechanism built to catch exactly this - and the run truthfully prints `ignored 0
        # unrecognised keys` while having ignored something. A caller reading that line
        # concludes everything landed.
        if accounts:
            print("  note: --by-arrival does not record account status - %d account block(s) "
                  "discarded. Arrival-day runs describe when mail ARRIVED, not a sweep that "
                  "connected." % len(accounts), file=sys.stderr)
        total_written = opened_all = 0
        for day in sorted(by_day):
            rid, rep, st = db.ingest_run(
                day, accounts=[], messages=by_day[day], notes=data.get("notes"),
                append=True, open_items=not args.no_open_items)
            total_written += len(by_day[day])
            opened_all += st["opened"]
        print(json.dumps({
            "ok": True, "mode": "by-arrival", "runs_touched": len(by_day),
            "days": [min(by_day), max(by_day)],
            "written": total_written, "linked": linked, "mapped": mapped,
            "with_body": with_body, "with_link": with_link,
            "unmapped_labels": unmapped, "ignored_keys": sorted(ignored),
            # In the RESULT as well as on stderr, so a caller can see it without scraping -
            # the same courtesy `replaced` already extends for the wiped-day case.
            "discarded": {"accounts": len(accounts)} if accounts else {},
            "impossible_accounts": impossible,
            "injection_flagged": flagged, "opened": opened_all,
            "open_items_suppressed": bool(args.no_open_items),
        }))
        return 0

    run_id, replaced, open_stats = db.ingest_run(
        run_date, accounts=accounts, messages=messages, notes=data.get("notes"),
        steam_sales=steam_sales, append=args.append,
        open_items=not args.no_open_items)
    if args.no_open_items:
        print("opened  0/%d  <- SUPPRESSED for a historical batch; nothing was added to "
              "the standing list" % len(messages), file=sys.stderr)
    print(json.dumps({
        "ok": True, "run_id": run_id, "run_date": run_date,
        "accounts": len(accounts),
        # WRITTEN AND REPLACED, not one number that looks like success either way.
        # Re-ingesting a run_date replaces that day wholesale, which is right for a daily
        # sweep and a footgun for a batched intake: every batch had to re-send everything
        # already ingested for that date or the earlier rows were silently deleted, and the
        # return value reported the count it had just written, which looked like success.
        "written": len(messages), "replaced": replaced, "mode": "append" if args.append
        else "replace",
        "linked": linked, "mapped": mapped, "unmapped_labels": unmapped,
        "with_body": with_body, "with_link": with_link,
        "ignored_keys": sorted(ignored), "discarded": {},
        "impossible_accounts": impossible,
        "injection_flagged": flagged,
        "steam_sales": len(steam_sales),
        "trashed": sum(1 for m in messages if m.get("disposition") == "trashed"),
        "kept": sum(1 for m in messages if m.get("disposition") in ("kept", "surfaced")),
        # CARRIED FORWARD. A brief is a delta, so a task assigned three weeks ago used to
        # appear in exactly one brief and then vanish. `opened` is what this run added to
        # the standing list; `still_open_seen` is how many already-open items this run saw
        # again. Both printed, because "0 opened" is a claim - it means either nothing
        # needed a person today or the carry-forward is not running, and those must not
        # look the same.
        # SUPPRESSED IS NOT THE SAME AS NONE. "opened: 0" from a run that was told not to
        # open anything, and "opened: 0" from a run where nothing needed a person, are
        # different facts - and this project's whole argument is that a zero is a claim.
        "opened": open_stats["opened"],
        "still_open_seen": open_stats["still_open_seen"],
        "open_items_suppressed": open_stats["suppressed"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
