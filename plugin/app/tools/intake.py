"""Backfill an existing mailbox in resumable batches. The harness only - triage stays human.

WHAT THIS IS FOR. The routine was built for a daily sweep of the last day or two. Pointing
it at a mailbox with years already in it is a different job: it takes many passes, a session
ends in the middle, and starting again from the top is both slow and wrong - it re-triages
what was already done. In the reported deployment the owner wrote three throwaway scripts to
paper over this. Two of the three needs are already closed (arrival dates are stored, and
`ingest.py --append` adds to a run instead of replacing it); what was left is the part that
remembers where it got to.

WHAT IT DELIBERATELY DOES NOT DO. It does not classify. Disposition, category and importance
are judgment, and a program that filled them in with defaults would produce a dashboard full
of confident labels nobody chose - the exact failure this release exists to fix. `next`
hands you a batch; you triage it; `done` records that the batch is finished.

PAGED BY UID RANGE, NOT BY OFFSET, and this is the whole reason to use it rather than a loop
around --offset. Offset counts back from the newest message, so anything arriving or being
deleted mid-intake shifts every later window: a message slides across a boundary and is
never fetched by any batch. Nothing errors, no count looks wrong, and the message is simply
absent. UIDs do not move, so a range covers what it says it covers - and a batch that
returns fewer messages than its range is width is reporting real deletions rather than
silently losing rows.

Usage:
    python tools/intake.py plan   --account EMAIL [--batch 200]
    python tools/intake.py next   --account EMAIL [--out FILE]
    python tools/intake.py done   --account EMAIL --batch N
    python tools/intake.py status [--account EMAIL]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "runs"
MAILTOOL = str(Path(__file__).resolve().parent / "mailtool.py")


def _state_path(account):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in account.lower())
    return STATE_DIR / ("intake-%s.json" % safe)


def _is_state(d):
    """Is this actually an intake progress file, or just JSON that shares the name?

    `status` globbed `intake-*.json` and handed every match to the reporter, which then
    crashed on the first one that was a fetched BATCH rather than a plan - and batches land
    in the same directory by default. A status command that dies on the contents of its own
    output directory is not a status command.
    """
    return isinstance(d, dict) and isinstance(d.get("batches"), list) and "account" in d


def load_state(account):
    try:
        with open(_state_path(account), encoding="utf-8") as f:
            d = json.load(f)
        return d if _is_state(d) else None
    except (OSError, ValueError):
        return None


def save_state(account, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(_state_path(account)) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _state_path(account))       # atomic: a killed session cannot corrupt it


def _mailtool(args):
    """Run mailtool and return its parsed JSON, or raise with what it actually said."""
    proc = subprocess.run([sys.executable, MAILTOOL] + args,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit("mailtool failed (exit %d):\n%s"
                         % (proc.returncode, (proc.stderr or proc.stdout).strip()[:2000]))
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise SystemExit("mailtool returned output that is not JSON:\n%s"
                         % proc.stdout.strip()[:2000])


def cmd_plan(args, run=None):
    """Work out the UID windows once, and write them down.

    The plan is fixed at plan time on purpose. Recomputing batches on every run would let a
    mailbox that is still receiving mail reshuffle the windows underneath a half-finished
    intake, which is the same class of bug as offset paging - silent, and invisible in any
    count. New mail that arrives during an intake is above the top of the plan and belongs
    to the daily sweep, not to the backfill.
    """
    run = run or _mailtool
    # ONE probe. It answers both questions at once: how many messages match, and what the
    # newest UID is. UIDs are not dense - deletions leave gaps - so the count says nothing
    # about the highest UID, and a plan built by dividing the count alone would stop short
    # of the top of the mailbox and silently never fetch the newest mail.
    days = str(args.days or 0)
    probe = run(["fetch", "--account", args.account, "--mailbox", args.mailbox,
                 "--days", days, "--limit", "1", "--no-snippets"])
    total = probe.get("total_matched") or 0
    if not total:
        print("nothing to intake: %s reports 0 messages"
              % ("the mailbox" if days == "0" else "that window"))
        return 1
    top = max((int(m["uid"]) for m in probe.get("messages", []) if m.get("uid")), default=0)

    # WHERE THE WINDOW STARTS, asked of the mailbox rather than guessed. `--days` bounds the
    # intake by DATE, but the batches page by UID - so the plan needs the UID of the oldest
    # message inside the window, which is the one at offset total-1. Computing a UID from a
    # date any other way means assuming UIDs advance evenly with time, and they do not:
    # a quiet December and a busy March consume the same UID space at very different rates.
    lo = 1
    if args.days:
        oldest = run(["fetch", "--account", args.account, "--mailbox", args.mailbox,
                      "--days", days, "--limit", "1", "--offset", str(total - 1),
                      "--no-snippets"])
        uids = [int(m["uid"]) for m in oldest.get("messages", []) if m.get("uid")]
        if uids:
            lo = min(uids)
        else:
            print("could not find the oldest message in that window; planning the whole "
                  "mailbox instead", file=sys.stderr)
    if not top:
        print("could not read a UID from the newest message; cannot plan a UID-ranged "
              "intake for this mailbox", file=sys.stderr)
        return 2

    batches = []
    # `start` is kept because the loop below CONSUMES lo. Without it the state file and the
    # printed summary both reported the range as top+1:top - a window of nothing - while the
    # batches themselves were correct. A plan that misdescribes itself is how a resume gets
    # debugged against the wrong numbers.
    start, lo = lo, lo
    # Windows over the UID SPACE, not over the message count. A window can come back light
    # (or empty) where messages were deleted years ago; that is correct and is reported,
    # not treated as an error.
    span = max(1, (top - start + 1) // max(1, -(-total // args.batch)))
    while lo <= top:
        hi = min(top, lo + span - 1)
        batches.append({"n": len(batches) + 1, "uid_range": "%d:%d" % (lo, hi),
                        "state": "pending", "fetched": None, "ingested": None})
        lo = hi + 1
    state = {"account": args.account, "mailbox": args.mailbox, "total_at_plan": total,
             "highest_uid": top, "lowest_uid": start, "days": args.days,
             "batch_size": args.batch, "batches": batches}
    save_state(args.account, state)
    print("planned %d batch(es) over UIDs %d:%d for %d message(s) in %s%s"
          % (len(batches), start, top, total, args.account,
             "" if not args.days else " (last %d days)" % args.days))
    print("state: %s" % _state_path(args.account))
    print("\nnext: python tools/intake.py next --account %s --out batch.json" % args.account)
    return 0


def cmd_next(args, run=None):
    run = run or _mailtool
    state = load_state(args.account)
    if not state:
        print("no intake planned for %s - run `intake.py plan` first" % args.account,
              file=sys.stderr)
        return 2
    todo = [b for b in state["batches"] if b["state"] != "done"]
    if not todo:
        print("intake complete: all %d batch(es) done" % len(state["batches"]))
        return 0
    b = todo[0]
    data = run(["fetch", "--account", args.account, "--mailbox", state["mailbox"],
                      "--uid-range", b["uid_range"], "--limit", str(args.limit or 10000)]
                     + (["--no-snippets"] if args.no_snippets else []))
    b["fetched"] = len(data.get("messages") or [])
    b["state"] = "fetched"
    save_state(args.account, state)

    out = args.out or ("runs/intake-batch-%d.json" % b["n"])
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    done = sum(1 for x in state["batches"] if x["state"] == "done")
    print("batch %d/%d  UIDs %s  ->  %d message(s)  ->  %s"
          % (b["n"], len(state["batches"]), b["uid_range"], b["fetched"], out))
    print("%d batch(es) already done, %d remaining"
          % (done, len(state["batches"]) - done - 1))
    print("\nTriage these, write a run JSON, then:")
    print("  python dashboard/ingest.py <run.json> --append")
    print("  python tools/intake.py done --account %s --batch %d" % (args.account, b["n"]))
    return 0


def cmd_done(args):
    state = load_state(args.account)
    if not state:
        print("no intake planned for %s" % args.account, file=sys.stderr)
        return 2
    for b in state["batches"]:
        if b["n"] == args.batch:
            if b["state"] == "pending":
                # Marking a batch done that was never fetched would leave a hole no later
                # pass ever revisits, and the summary would still read 100%.
                print("batch %d was never fetched - refusing to mark it done"
                      % args.batch, file=sys.stderr)
                return 1
            b["state"] = "done"
            b["ingested"] = args.count
            save_state(args.account, state)
            left = sum(1 for x in state["batches"] if x["state"] != "done")
            print("batch %d done. %d batch(es) remaining." % (args.batch, left))
            return 0
    print("no batch %d in the plan" % args.batch, file=sys.stderr)
    return 1


def cmd_status(args):
    if args.account:
        candidates = [args.account]
    else:
        # Read each file and ASK it whether it is a plan, rather than trusting the name.
        # The glob also matches fetched batches, triaged runs, and anything else somebody
        # drops here - none of which describe an intake.
        candidates = []
        for path in sorted(STATE_DIR.glob("intake-*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if _is_state(d):
                candidates.append(d["account"])
    seen, accounts = set(), []
    for a in candidates:
        if a not in seen:
            seen.add(a)
            accounts.append(a)
    if not accounts:
        print("no intake in progress")
        return 0
    for acct in accounts:
        state = load_state(acct)
        if not state:
            continue
        bs = state["batches"]
        done = [b for b in bs if b["state"] == "done"]
        fetched = sum(b["fetched"] or 0 for b in bs)
        ingested = sum(b["ingested"] or 0 for b in done)
        print("%s  %d/%d batches done" % (state["account"], len(done), len(bs)))
        # FETCHED AND INGESTED, SEPARATELY. They should match, and when they do not the
        # difference is the whole finding: messages pulled out of the mailbox that never
        # reached the store. One combined number would hide exactly that.
        print("  %d message(s) fetched, %d recorded as ingested%s"
              % (fetched, ingested,
                 "" if fetched == ingested else "   <-- these disagree; %d unaccounted for"
                 % (fetched - ingested)))
        print("  planned against %d message(s) at %s"
              % (state["total_at_plan"], "plan time"))
        nxt = [b for b in bs if b["state"] != "done"]
        if nxt:
            print("  next: batch %d, UIDs %s" % (nxt[0]["n"], nxt[0]["uid_range"]))
    return 0


def main(argv=None, run=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="work out the batches and write the progress file")
    p.add_argument("--account", required=True)
    p.add_argument("--mailbox", default="INBOX")
    p.add_argument("--batch", type=int, default=200)
    p.add_argument("--days", type=int, default=0,
                   help="bound the intake to the last N days (0 = the whole mailbox)")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("next", help="fetch the next unfinished batch")
    p.add_argument("--account", required=True)
    p.add_argument("--out")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-snippets", action="store_true")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("done", help="mark a fetched batch as triaged and ingested")
    p.add_argument("--account", required=True)
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--count", type=int, default=None,
                   help="how many messages actually reached the store")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("status", help="how far through an intake is")
    p.add_argument("--account")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    # `run` is threaded through rather than reached for globally so the tests can drive a
    # fake mailbox. Shelling out to a real IMAP server to prove that resume works would
    # test the network, not the resume.
    return args.fn(args, run) if args.fn in (cmd_plan, cmd_next) else args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
