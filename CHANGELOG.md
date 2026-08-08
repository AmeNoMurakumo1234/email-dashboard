# Changelog

## 0.19.2 — a web link is a handle, not decoration

`backfill_bodies.py` required a Message-ID and called every other row **a permanent hole**.
Saying that out loud was the right instinct attached to the wrong arithmetic, which is worse
than saying nothing — it is a confident claim about what cannot be recovered.

On the install where this was reported, Message-IDs covered under a third of the rows while
`web_link` covered **all of them**. Every one of those "unreachable" rows was fetchable
through the identifier its own link already carried.

### Fixed — the fallback, and the corrected definition of a hole

A provider's web link embeds the provider's own message identifier. `handle_of()` prefers the
Message-ID — it is provider-independent and survives the message moving between folders — and
falls back to the link. A hole is now **a row with neither**, which is a different number, and
the tool reports the two sources separately so the claim can be checked rather than trusted.

### The encoding detail, because it fails in the worst possible pattern

`ItemID` in an OWA link is percent-encoded **standard base64** (`/` and `+`). Graph's own `id`
is **base64URL** (`-` and `_`). Hand the decoded standard form to Graph and a `/` reads as a
path separator: `ErrorInvalidIdMalformed`. Single-encoding to `%2F` does not survive, because
it is decoded again before it arrives; only double-encoding gets through, and that is a
workaround for a problem that disappears if you convert the alphabet instead:

```python
uid = item_id.replace("/", "-").replace("+", "_")
```

Roughly one id in twelve contains one of those characters. **A naive version works for the
first dozen messages and then fails** — it looks like it works, which is the distribution that
gets shipped. Percent-decoding happens first and the alphabet conversion second; the other
order rewrites the escape's own characters and yields an id that decodes to nothing.

### Not verified end to end

Every account on the install this was written on is IMAP, and IMAP has no web link — so no row
in that store has one to test against. The extraction and the conversion are tested against the
documented shapes and the reported failure; **the fetch that uses them is not**. Stated rather
than implied: "it should work" and "it was seen to work" are different claims, and only one of
them has been earned here.

### Also — a safety sentence that had gone stale

The module's docstring said it "only ever calls `find`", which stopped being true when the
fallback added a second call. Both use `BODY.PEEK`, so the read-only property still holds — but
a guarantee whose stated *reason* is out of date is worth correcting anyway, because the next
person to add a call will check the sentence rather than the code.

## 0.19.1 — the classifier that recognised nothing, and said nothing about it

### Fixed — the sign-in ledger was blind to credentials in flight

Every phrase in the sign-in vocabulary described a message **reporting that a sign-in already
happened**. None matched a message that **is the means of signing in** — a magic link, a
one-time code, a verification mail. A store holding fourteen authentication messages
classified all fourteen as `other`, and the panel reported zero sign-ins, zero anomalies, zero
everything.

And those are the *better* evidence of the two. **A magic link you did not request is the
intrusion attempt, arriving before anyone is in;** a sign-in notice arrives after. An OTP you
did not ask for is the same. The panel was discarding exactly the class it most needed.

`credential` is now its own kind rather than folded into `signin`, because the right routine
treatment differs: one you did request is noise, one you did not is an anomaly with nobody
signed in yet. One further gap closed on the way: `sign-in to` was in the vocabulary and
`log-in to` was not, while `new log-in` required the word "new" — a one-word hole that alone
accounted for ten of the fourteen.

### Fixed — a zero now says which kind of zero it is

This is the half that made the blindness dangerous rather than merely incomplete. Coverage
reported the reach of the **device parser** — carefully, with a good caveat about UNKNOWN never
meaning "known" — and said **nothing about the reach of the classifier**. So a vocabulary that
recognised nothing produced output identical to a mailbox that genuinely had no sign-in
activity: well-formed JSON, every field present, a careful note attached to the wrong number,
and a completely wrong answer.

`coverage.recognised` is the field that makes a zero legible. A zero beside a low `recognised`
count means the vocabulary did not understand this mailbox, **not** that nothing happened —
and those call for opposite responses. `other` is not a classification; it is the absence of
one, and counting it as coverage is what let a blind run look complete.

### Fixed — printing a stored subject killed the program on a Windows console

Subjects contain whatever a sender typed; a Windows console defaults to cp1252, which cannot
encode most emoji. Any entry point that printed one died mid-listing with a
`UnicodeEncodeError`, on a machine where nothing was wrong with the data or the tool. Measured
on one store: **170 of its distinct subjects** are not cp1252-encodable.

Reported **twice, against two different files** — the second one written *after* the first
report was closed. Fixing the file that was named instead of the shape of the defect is how
the same bug gets reported a third time. So: one helper, applied to every printing entry
point, and a test that **discovers** entry points rather than listing them. `errors="replace"`
is the half that matters — a console that cannot render a glyph should print `?` and keep
going, never abort a listing partway.

### Fixed — the roster inside the file that polices rosters

`test_livecheck.py` hard-coded the suites it checks, and named one the package does not ship.
On a clean install the assertion ran against a file that does not exist, Python reported a
missing file, and **that was reported as a failing suite** — the exact conflation 0.18.1
existed to remove, reproduced one level up inside the tool that enforces the distinction.

Now derived from which suites actually *call* the preflight (the call, not the import — a file
can import a helper and never use it). The missing suite is also shipped now; its absence was
an allowlist oversight.

## 0.19.0 — the body you already downloaded

A store ran for a year with `body_text` NULL on **every row**, and nothing looked wrong. The
viewer falls back to re-fetching each message on demand, so the sandboxed reader, the image
blocking and the tracking-host report all worked — right up until a message was no longer in
the mailbox. That is not a working feature; it is one that fails later, quietly, against mail
nobody can retrieve any more.

### Added — `fetch --with-body`, which costs nothing

`fetch` already pulls `BODY.PEEK[]` for every message and **throws the whole thing away** after
taking a 400-character snippet. Carrying it through adds no round trip, no session, and no
extra bytes on the wire. The expensive-looking fix had already been paid for.

Attachments are excluded on purpose: the viewer renders the body, and carrying a large PDF into
a SQLite column would make the store's size a function of what other people email you.

### Fixed — a stored body is used whatever the backend is

**This is the one that mattered, and without it everything above changes nothing.**

The stored body was only ever consulted inside the *connector* branch, on the reasoning that a
connector install has no fetcher and therefore needs it. True, and the wrong place for it: the
value of a stored body has nothing to do with which backend an account uses. A message is
immutable, so re-fetching buys no freshness — it buys a network round trip, a subprocess, and a
hard dependency on the mail still being where you left it.

So on an IMAP store, every row could have had its body sitting in the column and every single
open would still have gone to the network for a second copy. A backfill would have been pure
waste. Measured after the fix: opening a two-week-old message went from an IMAP round trip to
**0.03s from the store**.

The assertion in the new suite is therefore not "the right bytes come back" — they did before.
It is that **no subprocess runs**. That is the difference between a body that is stored and a
body that is merely *also* stored. The control matters as much: without a stored body, an IMAP
account must still fetch, or the message is simply unreachable.

### Added — `backfill_bodies.py`, for history

Resumable by construction (it only ever selects rows lacking a body), read-only against the
mailbox, and it names why each row it skipped was skipped — *"skipped 40"* on its own invites
the reader to assume the mail is gone when it might equally be a mailbox that would not
connect, and those call for opposite responses.

**Measured before building, and the measurement changed the plan.** The assumption was that old
mail would be gone and only a fix-forward was worth doing. A stratified sample across every
month came back **100% retrievable**, including binned mail over a year old — trash goes to the
provider's Trash folder and stays there.

*(The first version of that probe reported 0% across every month, including mail from three days
earlier that had visibly opened in the viewer an hour before. It was parsing `find`'s `--out`
file as JSON, which is the raw message. A zero from an instrument that never fired, produced
while measuring for exactly that class of defect — caught only by the contradiction with
something already seen.)*

Rows with no Message-ID can never be filled this way. That is reported as a permanent hole, not
a queue.

## 0.18.1 — a suite that did not run is not a suite that passed

Three suites drive the live dashboard over HTTP. That is the right way to test them — the
entitlement checks they cover live in the request handler, and calling the functions directly
would skip the guard a browser actually meets.

With no server running they behaved three different ways, all wrong. Two dumped a raw urllib
traceback, which reads as *"the ack guard is broken"* rather than *"nothing was listening on
the port."* One **hung until it was killed** — worse than either, because a hang reads as
slowness, so the honest answer never reaches anybody and in a batch it just eats the clock.

The good news, checked before anything was changed: none of them ever passed silently. The
count was never hollow. But "could not run" was being reported as "failed", and those are
different facts.

### Added — a preflight, and a third outcome in the runner

A short socket probe before a single request is made: if nothing is listening, print what is
wrong and how to start it, and exit **2**. Non-zero deliberately — skipping would let the
runner report ALL PASS over a suite that never executed a line, which is how a green board
comes to cover code nothing has run.

`tools/run_tests.py` now reports **COULD NOT RUN** separately from **FAILED**, names the
suites, and still fails the run. `EMAIL_DASHBOARD_BASE` overrides the port, so the guard can
be tested against one known to be dead — and so anyone running the dashboard elsewhere can
drive these against it.

### Fixed — the install test now refuses an occupied port instead of testing whatever is on it

`plugin/test_install.py` installs a plugin, starts a dashboard on a spare port, and asks that
port what it is. If something was **already** listening, the new server could not bind and
every probe answered from the other process. The whoami check caught it — but only at the end,
and only as a cryptic mismatch between two temp-directory names.

Not hypothetical: interrupting a test run leaks the dashboard it started, and the next run
minutes later fails exactly that way. An occupying process on this port has done real harm
here before, when a run bound to a leftover server and wrote over a real protected-sender
list. It now refuses up front and says what it found.

## 0.18.0 — escalate on anomaly, never on occurrence

0.17.0 stopped the same message being raised four days running. This is the other half: making
the messages that remain worth reading.

**An alert that fires on every login is not an alert, it is a log.** Logs are things you
consult; alerts are things you trust. Merging them destroys the channel *silently*, because
nothing is ever wrong — every notice is true, the reader simply learns that opening them never
pays, and by the time one matters that habit is already built.

### Added — a sign-in ledger, and anomalies that earn their card

Routine sign-ins get **one line**: *"6 routine sign-ins across 4 services."* The line is
rendered even at zero, because "six sign-ins, all routine" and "nobody looked" have to be
distinguishable.

Escalated individually, with the reason stated:

- **A change, not a login.** Password, 2FA, recovery address or phone, app password, access
  token, OAuth grant, passkey, trusted device, account closed. These are the *steps* of a
  takeover and differ from a sign-in in kind: a sign-in happens constantly and legibly, a
  recovery-address change happens twice a decade and locks you out.
- **The provider itself calling a device or location new or unrecognised** — worth more than
  anything we can infer, because they are comparing against their own history of the account.
- **A blocked, prevented or failed attempt.** Evidence somebody tried.
- **Novelty** — a device never seen for that service, or the first notice ever recorded for
  the service at all.
- **A financial or protected service**, derived from the store's own money-concept mail rather
  than a list somebody has to maintain.
- **The burst** — sign-ins across three or more services in one window. This is the one with
  no individual evidence at all: every message in it is unremarkable on its own, so it exists
  only *across* messages and a tool that judges one message at a time cannot see it by
  construction. It is exactly the pattern alarm fatigue guarantees a person will miss.

### Added — one event is one item

A single desktop setup produced six notices within minutes, across two sender addresses.
Notices from one service inside a short window collapse into one item, and the **reasons
union** rather than the first one winning — the interesting thing about that cluster was that
it contained both a new passkey *and* a new trusted device, and keeping one would turn a
collapse into a loss. Notices hours apart do **not** collapse: two sign-ins to one service in
a day are two events, and the second is the one that isn't you.

### The honesty constraints

**The baseline is the past; only the window is judged.** Older mail teaches the panel what
normal looks like and is never reported on. Without that split, the first run hands the owner
a wall of "first ever seen" — teaching them on day one that the panel cries wolf, which is the
failure it exists to prevent, reproduced by its own first run.

**The parser under-claims.** Device signatures come only from the subject line, most providers
don't include one, and the coverage is stated. An unparsed notice is **unknown**, never
"known". An early version took any capitalised word after "from" or "on" and pulled *required*
out of "[ACTION REQUIRED]", reporting it as a device never seen before — a fabricated anomaly
in the one panel whose entire value is being believed when it fires.

**Silence is the dangerous output**, so every escalation has a positive control. Sixteen
mutants, all caught. Two survived the first pass — nothing covered blocked attempts, and the
collapse test used routine sign-ins where the collapse never runs — and a third survived after
the fixtures were de-branded, because three addresses on one domain fold anyway. The
distinction that matters is one display name across *different* domains, and it now has a test
that can only pass one way.

### Fixed — the export gate's address pattern stopped at two domain labels

An address on a deeper domain was reported truncated, so the reserved-domain allowlist never
saw the part that made it safe. A false positive in the direction that trains people to wave
the gate through, which is the one kind of gate failure that spreads.

## 0.17.0 — already seen is not news

Reported by an owner, before any of it was measured: so much repeated security mail arriving
that a real alert would get ignored, because the habit of not looking had already been trained.

The store agreed, and the number is the whole story: **account-security listings outnumbered
the distinct messages behind them by about three to two.** Roughly two in five of the "alerts"
being read were a repeat of one already read. One provider notice was raised on four
consecutive days with nothing about it changed. The volume of *distinct* security mail was
about one message every other day — perfectly readable. It was the repetition that made the
channel unreadable.

### Fixed — a message already surfaced on an earlier run is not surfaced again

Mail still sitting in the inbox gets re-listed by every sweep. That is a fact about the
mailbox, not news, and it was never a *wrong* answer — which is exactly why nothing ever
caught it. It was a repetitive one, and repetition is how an alert channel trains its reader
to skip it, so that the one alert that matters arrives into a habit of not looking.

On the day this shipped the daily list fell by two thirds.

Carried items are **marked, counted and returned**, never dropped: the panel leads with what
is new and says *"11 items already surfaced on an earlier run are not repeated here — show
them."* A shorter list with no explanation would be the same silence this project argues
against everywhere else, just in the pleasant direction.

**Identity is the Message-ID, falling back to the account+sender+subject shape only when there
isn't one** — the same rule acknowledgements use, for the same reason: a later linking pass
must not silently change what counts as "already seen." That distinction is load-bearing here
in a way it is easy to miss. A *second* sign-in notice with an identical subject is a different
event, and keying the check on the subject alone would make it vanish at precisely the moment
somebody else is signing in to your account. There is a test for that case specifically.

Suppression is the dangerous kind of feature, because every test that it hides things is also
satisfied by a panel that hides everything. The suite is weighted the other way: a genuinely
new message, a second message with the same subject, a changed subject on the same thread, the
same subject from a different sender or in a different mailbox, and the held-back count itself
all have to survive. The binned list is untouched — suppression belongs to the attention queue;
the trashed list is a ledger, and a day's record of what it binned has to be complete.

### Changed — the "why" column gets the room it needs

It carried by far the most text — a paragraph of reasoning, and the whole argument for keeping
a record is that the reasoning survives — and had the least room per character on the row. The
space came from the account and sender columns, which are short, repetitive, and were wide
enough to wrap an address in the middle of a word. Row heights roughly halved.

## 0.16.0 — gone quiet, in days, with the right dates

The owner's verdict on the panel was *"still shows garbage data"*, and it was three separate
things wearing one coat.

### Fixed — the dates on the panel were wrong

The worst of the three, and it looked entirely plausible, which is why it survived a rewrite
of the same function. 0.12.2 started measuring each sender against its own shorter observation
window — then read `first_seen` and `last_seen` back out of the **full** run list, using a
position derived from the short one. Every date on the panel was wrong for every sender whose
mailbox wasn't in every run.

Measured on a live install: a bank's first-seen date was reported nearly a week before its real
one, and its last-seen date almost three months early. Nothing errored, nothing looked odd, and
the caption sat directly beneath a correctly-computed alarm.

### Changed — silence is measured in calendar days

It counted **runs elapsed**, on the sound reasoning that a day with no run is not evidence of
silence. That stopped being the same quantity the moment a backfill existed: a year of
arrival-dated history packs hundreds of runs into the past while the present accrues one a
day, so "23 runs" in 2025 and "23 runs" in 2026 describe different amounts of the world. The
panel reported *"Silent 105 of the 173 runs that looked at this mailbox"* — a true sentence
about the store that tells nobody anything about their bank.

The sound half is kept where it belongs: the observation **window** still decides whether
anyone looked, and only days this sender's own mailbox was examined can contribute. What
changed is the unit the answer is reported in. The threshold moved from 21 runs to 21 *days* —
the same number, so this is a change of unit and not a quiet tightening riding along with it.

A mailbox is now also counted as observed on days a run **connected to it and found nothing**,
not only on days it produced mail. Taking the window from messages alone credits a sender for
every day nobody can prove anyone looked — in the direction that under-reports silence, which
is the direction that loses a stopped biller.

### Added — a ratio needs a floor under it

*"5x its worst"* sounds decisive and means nothing when the worst gap was two days: any sender
that writes in bursts clears a multiple of a tiny number by pausing over a weekend. The panel
carried one row at 5x and another at **1.25x** — which is not an anomaly, it is rounding —
beside a bank that had genuinely vanished for six months. Where the real finding and the
arithmetic artefact look the same, the panel gets ignored, and the real one goes with it.

A flag now needs both: meaningfully longer than its own worst gap (1.5x), **and** long enough
in absolute terms to be worth a person's attention (14 days).

### Changed — social notifications are hidden, and the hiding is reported

A friend posting less often is not a finding a mail tool should raise, and by sheer count they
dominated the list. Hidden by default, **counted in the caption**, and `?include=all` returns
them — suppression that cannot be seen is indistinguishable from having found nothing.

### Fixed — the caption contradicted the list it was captioning

It stated flatly that *"monthly billers cannot qualify yet — the run history is too short."*
True when written; false once a year of arrival-dated history existed. It was still saying it
while a monthly bank statement sat at the top of the list underneath. Now derived from the
actual window.

### Fixed — repeats called a stalled series "arriving faster"

Acceleration is a claim in the present tense, and the arithmetic only ever compared the gaps
**between** arrivals — never the gap between the last one and now. A series with a 4-day
median, silent for 246 days, was badged *arriving faster*. The gap to the present is a gap
too; a series quiet for several of its own cycles is stalled, not speeding up.

Finished series are now marked **dormant** and sorted last rather than hidden — they are real
history and a series can wake up, but burying a live dunning notice under fifty completed ones
is how a live one goes unread.

## 0.15.0 — acknowledgement has a door

`INSERT INTO acks` appeared in exactly one place in the codebase: the dashboard's HTTP
handler. **An acknowledgement could only be made by clicking in a browser.**

That is fine for a person at a screen, and wrong for the operating model this plugin
prescribes — the skill describes an agent that maintains the board day to day, and on a real
deployment that agent is a scheduled task with no UI, no session and no browser. It could read
the `acks` table only by opening the SQLite file directly, and it could not write one at all.

The gap is not cosmetic, and what it *forces* is the point. Items get dealt with off-channel
constantly — answered in a call, decided in a meeting, delegated verbally — while the mail
thread shows nothing. A routine with no way to record that re-escalates the same item every
run. So a parallel markdown ledger gets invented, and one install's policy file said so in as
many words. Then two stores answer *"has the owner dealt with this?"*: the sweep reads the
markdown and stays quiet, the dashboard reads the table and raises the item. Both are behaving
correctly. The answers disagree.

And the divergence runs the wrong way. The UI writes to the table and never the markdown; the
routine writes the markdown and cannot write the table. So **off-channel resolutions — the one
thing a mail tool can never infer, and therefore the most valuable thing a human can tell it —
were exactly the ones that could only be recorded in the store the dashboard ignores.**

### Added — two headless doors onto the same record

```
python dashboard/ack.py --subject "..." --sender "..." --note "answered on the call"
python dashboard/ack.py --list
python dashboard/ack.py --message-id "<abc@example.com>" --lift
```

and an `acknowledgements` array accepted by `ingest.py` alongside `messages`, so a sweep
records what it *learned* in the same call that records what it *saw*. Both go through one
implementation — the first draft of this copied the handler's body into the new function,
which would have produced two spellings of the ack key derivation and the lift semantics, and
every serious defect in this project so far has been one concept spelled twice.

The `--note` is the part nothing can reconstruct later: **why** it is closed.

### Added — `--import-md`, so the workaround can retire

`python dashboard/ack.py --import-md <ledger.md> --dry-run` reads a markdown list of closed
items and records each line it can match against a message in the store. Lines it cannot place
are **named and refused, not invented** — an acknowledgement stored against an identity no row
has would silence nothing and report success, which is the same failure one level up.

Then the markdown becomes a human-readable export of the table rather than a second database
that argues with it.

## 0.14.0 — the guard can say yes

Two reported defects that turned out to be one problem. The auto-trash guard was refusing
everything, on every install checked, with reasons that were individually correct — and
`REFUSED 6 of 6` with stacked, specific, sound reasoning is indistinguishable from a healthy
guard doing its job.

### Added — `would_trash`, because "I would bin this and cannot" had no way to be said

The valid dispositions were `trashed` / `surfaced` / `kept` / `saved`. A read-only triage —
which this plugin's own skill **mandates**, and which is the only thing a connector install
can do, having no fetcher to act with — cannot honestly write `trashed`, because nothing
happened to the mail. So `kept` got written. And `kept` in this tool's vocabulary means *the
routine decided to keep this*: a positive judgment about the sender.

The guard's "not pure noise" rule refuses any sender with kept mail. So on a read-only or
connector install, **no sender could ever clear it** — not because anything was protected,
but because there was no correct value to write. The read-only discipline this project
insists on poisoned the guard this project relies on, on day one, permanently, for every
install that followed the instructions.

The rule itself was never wrong. Flipping one vendor's history to what it would have looked
like if the routine had ever been able to act, and re-running the identical proposal, cleared
the eligible message — **and still refused the injection-shaped message from the same
sender.** The guard discriminates per message. It was being fed a history that could only
say no.

`would_trash` means **judged disposable, not acted on.** It is evidence about the sender
without being a claim about the mailbox. Along with it:

- one vocabulary, in one place: `DISPOSABLE` and `DELIBERATELY_KEPT`, because `disposition
  != 'trashed'` — true of the string, false of the meaning — was the spelling that would have
  quietly readmitted the new value as "kept" in six different readers, including the one that
  decides what lands on the standing work list.
- `disposition` was **never validated at all**. A typo landed in the store counting as
  neither binned nor kept, present in the row count and absent from every total that mattered,
  with the totals still balancing against each other. Now warns, and refuses under `--strict`.
- an unrecognised value is no longer evidence of anything. It used to fall into the `else`
  branch of the applier's history and become a positive judgment about the sender — a
  protection asserted on the strength of a spelling mistake.

### Added — the applier says when the guard cannot pass anything

A guard that is refusing and a guard that is *incapable of not refusing* now read differently.
If no sender in the store has a single message recorded as disposable, the applier says so and
names the cause — the same courtesy `doctor` extends with `NOT CONFIGURED` and the scoreboard
extends with *"not measured is not zero"*.

### Changed — a rule may name (sender, **category**), because mail does not arrive by sender

This is the half that decides whether the feature is usable, and fixing the disposition
problem above changed nothing about it. Simulated across every sender on a real work store
with the data corrected: **senders eligible for an auto-trash rule — 0.** Not few. None.

The reason is structural. The highest-volume senders are notification services, and their
entire job is to multiplex many kinds of message through one address: status noise, bot
chatter, and the handful of messages where a person named you and assigned you something. The
volume that makes a sender worth ruling on is the same volume that guarantees the sender is
mixed. `rule_min_messages` then seals it — below the threshold there is not enough evidence,
and above it the sender is mixed. The window where a sender is both high-volume and uniformly
noise was empty.

**The guard was right to refuse.** *This sender is pure noise* is a false statement about such
an address, and no fix should ever make it pass. The engine was behaving correctly and was
useless, because the only thing it could express was untrue of everything worth expressing it
about.

So a rule may now name a slice, which is a statement that can be true. Every existing check
runs unchanged against the slice: **narrower evidence, not weaker evidence.** A slice with any
deliberately-kept mail is still refused, `rule_min_messages` still applies to the slice (so
slicing cannot be used to duck the evidence threshold), and the protected-name check
deliberately stays at the whole-sender level — a protected person does not become binnable one
label at a time.

The triage layer already resolved category, concept, importance and `addressed_directly` per
message. Only the rule layer collapsed them back onto one sender.

The sender panel now shows the breakdown, because that is the feature rather than a detail of
it: which labels could be ruled on, and for each one that could not, **why**. A button that
never lights up and never says why is indistinguishable from one that is broken. The rules
file records the scope in the row itself, not only in the marker — including the caveat that a
label rule is only as good as the label, which is assigned to future mail by the same triage.

Rules written before this release keep their original marker and stay liftable. Re-keying them
would have orphaned every existing rule from the button that lifts it, which is the
acknowledgement defect from 0.9.0 all over again.

## 0.13.0 — did my data land?

Five defects, one shape: **input the seam accepts and then never mentions again.** `ingest.py`
is documented as the supported entry point from any source, which means its callers are by
definition not reading the internals — so anything it quietly drops, quietly mis-stores, or
quietly leaves uncovered is invisible until long after the source data is gone.

### Fixed — `account_status` is a set, and the writer treated it as a log

One mailbox, one entry in `accounts.json`, and a panel reading **"4/4 connected"** with four
cards each holding a fraction of the day's traffic.

`record_run(append=True)` gave the `runs` row a proper accumulate path and gave
`account_status` an unconditional `INSERT` — fifteen lines apart, in the same function, under
the same flag. The only thing that had ever held it to one row per account was the `DELETE` in
the *non-append* branch, which append correctly skips because that branch also wipes the day's
messages.

Self-concealing in a specific way: every per-card number was **real**, so nothing looked
corrupted — it looked like a multi-mailbox install that was working. And it bit precisely the
deployments that sweep more than once a day, which is what this plugin's own guidance tells
them to do. A single daily sweep never saw it.

Now an upsert on `(run_id, account)`: counters **sum**, snapshot fields **overwrite** — summing
an inbox size is meaningless, and a stale `CONNECTED` must never survive a later `FAILED`. A
collapse migration folds existing duplicates (counters summed, snapshots from the latest) and
then creates the unique index the table always needed. The index is created in `init_db` after
the collapse, never in `SCHEMA`: `executescript` runs before any migration, so a
`CREATE UNIQUE INDEX` there would abort the entire schema on exactly the stores that hold
duplicates — the ones that need fixing.

Same class as the acknowledgement-key defect in 0.9.0: a fix applied in one place and not to
the parallel structure beside it, invisible because each half was individually correct.

### Added — `inbox_count` has a stated meaning, and impossible rows are refused

`inbox_count >= fetched` and `trashed + kept <= fetched` are arithmetic certainties. Neither
was checked, so a row reading `inbox 1 / fetched 5` ingested with `ok: true` and every count
"correct", for a mailbox that actually held 265.

It is the one key in the accepted list whose **name does not define it**, and on a connector
install there are two plausible integers in scope both called some variant of "count". So both
halves are fixed: the contract is now written down where the keys are listed —

> `inbox_count` — total messages in the mailbox, **not** this sweep's result count. Send `null`
> if you cannot determine it; an absent number is honest, a wrong one is not.

— and the arithmetic is checked. Warns always; refuses under `--strict`, where an impossible
count is a stronger signal than anything `--strict` already rejects, because it cannot be a
difference of opinion.

### Added — `with_body` and `with_link`, beside `linked` and `mapped`

`linked` says a row can be re-**found**. Nothing said whether it could be **read**.

`body_text` and `web_link` are what make the sandboxed viewer, the image blocking and the
tracking-host report reachable — the headline privacy features — and they appeared in no
report at all. A caller that silently stopped sending them saw an unchanged, entirely healthy
result. On two installs checked, the great majority of rows carried neither — on one of them,
every single row. Every ingest had returned `ok: true`.

Both are now counted in the report and in the JSON result, and both are finally listed in the
ACCEPTED KEYS block a caller actually reads — they were accepted by the store and undocumented
at the seam, so sending them looked unsupported.

### Added — `--by-arrival` says what it discarded

It hard-codes `accounts=[]`, and the discard is probably **right**: an arrival-day run
describes when mail *arrived*, and asserting `CONNECTED, inbox 900` for a day on which nothing
connected would be a lie about a sweep that never happened. Doing it silently is not right.

Because `accounts` is a **recognised** key, the discard sailed straight past the
unrecognised-key report — the one mechanism built to catch exactly this — and the run
truthfully printed `ignored 0 unrecognised keys` while having ignored something. Now reported
on stderr and as `discarded` in the result, the same courtesy `replaced` already extends.

### Fixed — a unit test could fail because of the owner's configuration

`test_concept_drift` hard-coded the label `inner-circle-fyi` and asserted it was UNMAPPED as
its control. That control holds only while no map on the machine has taught that label — and
this plugin's own guidance tells owners to teach exactly that kind of label. So the test for
*"teaching the map repairs the store"* failed on installs where the owner had taught the map:
**the feature under test and the thing that broke the test were the same action.**

It failed in the useless direction too — green on a bare install, red on a configured one. A
suite that is expected to have one failure is a suite that no longer means anything.

Two mechanisms now, and a suite that keeps both honest:

- `tools/run_tests.py`, which **discovers** suites rather than listing them and reports the
  count and roster every time. There was no runner; suites were run by hand, so the number of
  suites was whatever anyone remembered it to be.
- `--no-local-config`, which makes the config loaders behave as if no `*.local.json` existed.
  Run the suite both ways: any suite whose **result differs** is reading live user config.

The first attempt at this fix redirected `server.py`'s config paths under the same flag and
broke eight assertions in a test that had been isolating correctly all along — by standing up
its own install directory. Controlling the install directory is the strong form of isolation;
a global switch is the weak form, and where the strong form is available the weak one must not
override it.

### Fixed — the export gate did not scan the file that is edited by hand every release

The builder deliberately does not own `CHANGELOG.md`, `README.md` or `INSTALL.md`, because a
build must never delete work it did not create. The consequence went unnoticed until it nearly
cost something: files the program does not **write** were also files it did not **scan**, so the
one public artefact edited by hand every release was the only one outside the boundary.

A release note describing a defect is exactly where a count measured from somebody's real
mailbox gets typed in as evidence — which is precisely what happened while writing this entry,
and it was caught by hand rather than by the gate. Not owning a file and not checking it are
different decisions. All three are now scanned, and a hit refuses the build like any other.

## 0.12.3 — repeat cadence in calendar days, not runs

The repeats panel measured gaps in **runs**, which stopped meaning anything once a historical
backfill existed. Checked against a real recurring-notice series, the same sender's gaps came
out roughly twice as large measured in runs as measured against the runs that actually covered
its mailbox — and it matters more here than in the quiet panel, because *accelerating* compares
early gaps to recent ones, so an intake concentrated in one period can manufacture or hide an
acceleration outright.

Copying the 0.12.2 fix would have been wrong: gaps in *runs* are the wrong unit for a claim
about the world however you scope them. Repeats are now measured in **calendar days**, and
every item states `days_since_last` and its `gap_unit`.

One earlier attempt at this was **reverted rather than shipped** — building the per-mailbox
lattice from grouped rows collapses it onto the sender's own arrivals, so every gap becomes 1
and the acceleration branch cannot fire at all. It failed its positive control, which is what
positive controls are for.

## 0.12.2 — a backfill must not manufacture silence

**A monthly biller was reported as several times its own worst silence without its behaviour
changing at all.**

After a historical intake a "run" no longer means "a sweep of every mailbox": most of the runs
in a backfilled store were never sweeps, and the majority of those contained exactly **one**
mailbox, because a backfill batch comes from a single account. Every statistic that counted
runs as observations was then asserting an absence nobody had looked for — the project's own
named failure mode, arriving through the tool's own backfill.

Each sender is now measured against the runs that covered **its own mailbox**, and every row
states its denominator (`observed_runs`). A sender whose mailbox cannot be determined keeps the
full sequence: not knowing is no basis for narrowing.

## 0.12.1 — acknowledged leaves the list

**Acknowledged no longer counts as outstanding.** The seen/done distinction is real, and it
was defended twice before the owner overruled it twice — which makes it their call. If
acknowledging is how you say you have dealt with something, a panel that argues with you
about what your own gesture meant is a panel you stop opening.

The distinction survives where it costs nothing: the row keeps its `open` state and its
paper trail, it is **not** marked resolved (nobody said *how* it was dealt with), and
`?acked=1` still returns it. Only what the panel claims is outstanding changed — and it says
what it hid: *"1 acknowledged, not counted"*. A count quietly dropping to zero is the same
silence this project argues against, in the pleasant direction.

**Account status is one shape.** It was a narrow single-column strip that grew into a wide
two-column grid, so opening details shoved the record sideways and the eye had to re-find
everything. Two columns always, one width always — "show details" adds detail, not geometry.

**The gap beside the record is gone.** It takes the remaining column rather than hugging its
content.

## 0.12.0 — the chrome gets out of the way

Measured at 1280×720: **230 pixels above the working area**, down from 342 and from 638
before any of this. The two panels people actually read get **463**.

### Changed — the attention panels are modals, opened by header chips

Inline they were the worst of both: they ate the vertical space the mail needed **and** were
too short to use. A four-row window onto an outstanding list is not a list — you cannot
decide anything through it, and it costs you the mail underneath for the privilege.

A chip costs nothing when it has nothing to say, and opens a 1100px modal with the height
caps lifted when it does. The whole reason to open one is to see all of it.

### Changed — the scoreboard is one line in the header

It was a tall box with air around it in the middle of the band. It is a number and a
direction; that is one line. The months compared and the volume caveat live behind
*"what is this?"*.

### Fixed — account status on a historical run

It said *"nothing recorded for this run"* on every backfilled day. Correct, and useless: a
historical run has no account status because nothing connected to a mailbox that day, but
whether your mailboxes are reachable is a fact about **now**. It falls back to the most
recent status and **labels it** — `9/9 connected, as of 2026-08-07`. An old answer with its
date beats a blank panel; an old answer without its date would be worse than both.

## 0.11.2 — dates that mean what they say

### Fixed — a historical batch belongs to the days it happened on

Ingesting a backfill put all of it into **today**, so a year of old mail joined today's
summary and a refund notice from last September was reported as this morning's news. The
message viewer had the same fault from the other end: it showed the sweep date rather than
the arrival date.

- **`ingest.py --by-arrival`** stages one run per arrival day — the run that would have
  found the message. Mail with **no readable date is refused**, not filed under today,
  because guessing is exactly how a year-old message becomes today's news.
- The viewer shows the arrival date, and still names the sweep date when the two differ.
  Reading something eleven months late is a real fact about the tool.

Worth naming why this survived: the calendar had already been keyed on arrival since 0.5.2
and looked correct throughout. One view had been fixed and the others had not — and the
fixed one was the one being watched.

### Added — `ingest.py --no-open-items`

History is not a to-do list. A year-deep backfill would land scores of nine-month-old
`action-needed` entries on the standing list — on its first read, which is the read that
decides whether anyone opens it again.

Reported as **suppressed**, never as zero: "opened: 0 because nothing needed a person" and
"opened: 0 because we were told not to look" are different facts.

### Added — `intake.py plan --days`

So a one-year intake does not page a decade of UID space. The batches page by UID and
`--days` bounds by date, so the plan **asks the mailbox** which UID the window starts at
rather than deriving it — UIDs do not advance evenly with time, and a quiet December and a
busy March consume the same span at very different rates.

### Fixed — layout

- **The gap between the record and the scoreboard.** The band's last column was flexible,
  which put a large empty bordered area between two panels — and that reads as something
  that failed to render, not as spare room. Every column hugs its content now and the band
  packs left, so the leftover is ordinary page background at the edge.
- **The record is the column that yields** when the band is tight, because it can scroll
  sideways and the tile cannot. With a year of history its natural width outgrew the row
  and grid took the space from the scoreboard instead, wrapping its text into a tower
  taller than everything else in the band.
- **The two attention panels stretch to a common height.** Side by side at different
  heights, one of them looks broken.
- **Account status scrollbars hidden**, as everywhere else here — and the horizontal one was
  only showing because the vertical one had narrowed the box enough to make the chips
  overflow. A scrollbar caused by a scrollbar.
- **"could not load" on the scoreboard** is now "needs a dashboard restart", with the reason.
  The overwhelmingly likely cause is a browser holding new static files while the process
  answering them predates the endpoint, and the fix is a restart rather than a bug report.

## 0.11.1 — acknowledged items, sticky headers, and a tile that explained nothing

### Fixed — an item you had already dismissed should not reappear as a task

`backfill_open_items` never looked at the acks table, so it seeded the standing list with
mail the owner had already acknowledged — the tool arguing with its own record of their
judgment. A `--since` window cannot catch that: the item is recent, it is just already
handled. On a real store the corrected version skips 16 of 21.

The distinction it was fumbling stays: an ack says **seen**, and seeing is not doing, so
acknowledging does **not** close an open item. But opening one for mail already dismissed is
wrong, and **both states are now visible** — the row carries an `acknowledged` badge, and the
viewer no longer promises the message "will stop being surfaced" while the open list goes on
surfacing it. That copy was simply false.

### Fixed — sticky panel headers

They had a background but not the geometry: the panel's 18px top padding left an uncovered
band above the header, and its 22px side padding left one down each edge, so rows scrolled
visibly through the gaps. The scroller gives up its top padding and the header carries it,
stretching over the horizontal padding with negative margins. `.panel h2` also outspecifies
`.sticky`, which is why one panel kept leaking after the other was fixed.

### Fixed — the side column explained nothing

A legend divorced from its chart is a list of words, so it is back inside the record. The
scoreboard tile says on its face what it counts, and can no longer render as a title over an
empty box when its fetch fails — silence looking like a result, in the one place this project
argues hardest against it.

The record is wider, and says **"57 days"** rather than "57 runs": it is keyed on arrival, so
each square is a day mail came in, and calling those runs overstates how often the tool ran.

## 0.11.0 — room to work, and a number that measures the outcome

### Changed — the chrome gives the mail back its screen

Measured at 1600x900 before this: header, the attention panels, the KPI row, the record and
the account grid were five full-width rows stacked one under another, and they used **638
vertical pixels before the two working panels started**. Those got 408 and the page then
scrolled — about two emails at a time on a laptop.

Stacked, a top region costs the **sum** of its sections. Side by side it costs the tallest.

- One **top band**: counts, who is connected, the record, then the legend and the
  scoreboard, in that order.
- One **attention row**: the setup, workflow, still-open and new-host panels share a line
  instead of each taking one, and each caps its list and scrolls inside it — an outstanding
  list can no longer push the mail off the screen.
- The working area's floor is expressed against the viewport. A fixed 408px was itself
  causing the scroll it had been written to prevent.

| viewport | above the panels | panels get | page scrolls |
|---|---|---|---|
| 1920×1080 | 393 (was 638) | 642 | no |
| 1366×768 | 350 | 391 | no |
| 1280×720 | 322 | 370 | no |

Roughly seven sender rows visible on a laptop where two fitted before, and no page scroll at
any of these sizes — a page that scrolls by even a few pixels moves every control under the
cursor between looking at it and clicking it.

Three things found while measuring, each of which looked like a bug rather than a small
chart: the record's panel was being sized by its own **heading text**, so it drew a box half
of which was empty; the legend, squeezed into a narrow column, wrapped to five lines and
became the tallest thing in the band while 350px sat unused beside it; and **show details**
laid eight accounts across a 340px panel, wrapping every address one character per line.
That view takes width now, not height.

### Fixed — an open item you can actually open

The **Still open** rows named a subject and a sender and nothing else, so clearing one meant
first working out which of several mailboxes it came from and finding it by hand. They now
show the account and open the message in the sandboxed viewer on click. Asking for a
judgement while withholding the evidence is not a panel, it is a quiz.

### Added — "Before they went elsewhere"

The only number on this dashboard that measures the **outcome** rather than the activity.
Everything else counts what the tool did, and all of it can rise while the thing its owner
cares about gets worse.

A *reach* is somebody giving up on the inbox and going to another channel to find you. One
from a person who matters means they had already given up before the notice even arrived.

It is detected by **shape, not by brand**: mail from an unrepliable address whose subject
says a person wanted you. That works for platforms nobody has heard of — which is the
deployment that most needs it — and `elsewhere_senders` narrows it to an exact list where
the shape test is wrong.

The first version matched on sender alone and reported 144 of these on a real mailbox. Every
one was a broadcast: *"X posted a new photo"*, *"catch up on moments you've missed"*. Nobody
was looking for anybody. **A reach needs the subject to qualify.**

Three honesty rules, each with a test:

- **Not measured is not zero.** A mailbox with nothing matching says so; a confident `0`
  from an instrument that has never fired is congratulation for having no instrument.
- **A quiet month is not a win.** The rate per hundred messages is what moves, and the
  volume behind it travels with the verdict.
- **The month in progress is not a data point.** Six days of one month against all of the
  last reports a collapse every time the page is opened early, and the collapse is the
  calendar.

## 0.10.0 — a connector install can actually read its mail

0.8.0 let an account **declare** that something else fetches it, and `doctor` reported that
honestly instead of red. But declaring it did not make a single message openable, so on that
class of install the sandboxed reader, the image blocking and the tracking-host report — the
headline privacy features — were unreachable for every row.

### Fixed — the viewer no longer says "not found" about something it never searched

Opening a row on a connector account rendered **not found in this mailbox** above a detail
that correctly explained nothing had gone looking. The headline contradicted its own
explanation, in the one place the tool has the *most* certainty about what happened —
`providers.backend_of()` knows the answer before any subprocess is spawned.

It now branches on the backend first and reports **"this account has no local fetcher"**,
which is the same vocabulary `doctor` already uses. An absence is only reportable by an
instrument that ran.

### Added — `body_text` and `web_link`

Two new accepted fields, and the same instinct that added recipients: **carry more of what
the fetcher saw.**

- **`body_text`** — raw MIME, or just the text or HTML body; either is accepted, so the
  difference is handled by the tool rather than by every connector author. With it the
  **sandboxed reader works with no fetch at all**, and it goes through the *same*
  parse-and-sanitise path as a fetched message. A second rendering route would be a second
  place for image blocking to be subtly different, and the one nobody tests is the one that
  leaks.
- **`web_link`** — an explicit *"open in your mail client"* button, labelled as leaving the
  sandbox, because it does: the provider renders it, images and tracking included. Offered,
  never used silently. But *opens somewhere* beats *cannot open*.

### Added — two more ways to close an open item

A standing list whose only exit is completion becomes a graveyard, and a graveyard teaches
its reader to skim past the one live item. Reported from a live install: an item nearly two
hundred days old — a software-seat offer nobody was ever going to take — with no exit that
was not a lie.

Alongside **done here** and **done elsewhere**:

- **not doing this** — a decision, which is a real answer and closes the item
- **expired** — the offer lapsed, the deadline passed, the moment is gone

`moot` still works for anything written against the previous release.

### Added — median age, and who is waiting

`/api/open-items` reports **median age** beside oldest. A list that churns is working however
long it is; one whose median age climbs every week is being ignored however short. Length
alone says nothing, and chasing a length target pushes toward hiding things rather than
closing them.

It also groups by **who is waiting on you**. Four asks from one colleague is one
conversation; four from four people is four. The owner acts by person.

## 0.9.0 — things derived once, and the silence around them

Every fix here is the same shape: a value computed at one moment, trusted forever, and
nothing saying it had gone stale. All four were invisible — correct counts, `ok: true`, and
a wrong answer on screen.

### Fixed — an acknowledgement no longer evaporates when its row is linked

`ack_key` returned the Message-ID when a row had one and a `row:` identity when it did not.
Both correct, and **computed from row state that changes.** The moment a linking pass gave a
row its Message-ID, every acknowledgement stored under the old key stopped matching.

Reported from a live install: dozens of items acknowledged, a linking pass minutes later,
and every one of them rendered as unacknowledged again. The table still held them. The API
still returned them. Only the rendering was wrong, and it was noticed only because the dots
changed colour.

An acknowledgement is the one thing in this store that is **the owner's own judgment** rather
than the agent's inference — everything else can be recomputed from the mailbox. Losing it
silently is the highest-cost failure available here.

Matching is now on the **set of identities** a row is known by, expanded from both ends: each
stored ack contributes its key *and* the identity derivable from the account, sender and
subject it recorded. So it survives a row gaining a Message-ID **and** losing one (a re-ingest
from a source that does not carry them). **No migration is needed** — orphaned acks start
matching again on upgrade, with nothing guessed and nothing rewritten.

Un-acknowledging lifts every identity too. Deleting only the preferred key left a legacy ack
in place, so the row stayed acknowledged: click undo, get `ok`, nothing changes.

### Fixed — the stored `concept` no longer drifts away from the map

`concept` is resolved once at ingest and frozen. The map it resolves against is **edited later
by design** — the shipped map is generic and the onboarding skill tells you to add your own
labels as you meet them. Nothing re-derived it, so it drifted as a direct consequence of using
the tool as documented.

On a real install almost every row still read `unmapped` long after a full local map had been
written, while `test_concepts.py` reported ALL PASS — that test resolves live, the dashboard
reads the column. Two instruments, opposite answers, and the one the user sees is the stale
one.

It is now reconciled on every `init_db`, and says how many rows it repaired. There is
deliberately **no fingerprint gate**: the first version skipped the sweep when the map hash
matched, which meant add-a-label, run, remove-it, run left the rows in between asserting a
mapping the owner had withdrawn — an optimisation that reintroduced the exact staleness it
was added to prevent.

### Fixed — the question generator was being starved

Three separate problems, all of which made it ask less and worse:

- **`surfaced` was counted as `kept`.** The volume question is suppressed by "has anything
  ever been kept?", so on any install whose routine *surfaces* rather than bins, the single
  largest lever on the inbox could never be raised at all. Invisible from a mailbox that
  trashes. Evidence now reports `surfaced_to_you` and `auto_binned` apart, because "put in
  front of you and ignored" is a stronger fact than "binned automatically and ignored".
- **The escalation question made you introspect** — *"family, your employer, your bank"* —
  asked of a work mailbox whose real answer was colleagues and an accountant, while the tool
  was already holding the list of senders whose mail had been flagged before. It now offers
  that list as a multi-select. Recognition is faster and more accurate than recall.
- **The most dangerous question did not exist.** `assigned_work_at_risk`: messages under a
  category that is mostly binned, where a *person* named you — a mention, a review request,
  an assignment. Mechanically derivable now that recipients are stored, and being wrong about
  it loses work rather than adding noise.

### Added — stakes outrank weight

`STAKES` (`data-loss` → `safety` → `attention` → `noise`) is a hard floor above the weights,
and weight is now only meaningful **within** a band. A thinly-evidenced question about a rule
that would bin assigned work outranks the strongest-evidenced question about a pile of
promos, however large the pile.

Because that signal takes the top of the list by design, a false positive there is expensive:
the first thing it surfaced on a real mailbox was a marketing blast headed *"[Action Required]
Looks like you have been ghosting us!"*. Urgency phrases that bulk senders write now need
corroboration — the message was addressed to you directly, or the recipient list carries a
mention marker. Phrases only true of one recipient ("assigned you", "mentioned you") still
stand alone.

### Added — `ingest.py` names what it dropped

The supported public seam accepted unknown keys silently, with `ok: true` and every count
correct. A typo (`messageId`) produced a row that ingested cleanly and was quietly unopenable;
a connector author supplying something real the schema lacks got nothing back at all.

Every run now reports `ignored N value(s) under K unrecognised key(s)`, named — the same move
already made for `linked N/M` and `mapped N/M`. Reported rather than rejected, so a caller on
a newer contract than the installed version still succeeds; under `--strict` it is an error,
consistent with unlinked and unmapped rows. **The accepted field list is now documented** in
the docstring beside the shape example, and lives next to the code that consumes it.

### Added — `EMAIL_DASHBOARD_DB`

Names the store, so a caller can redirect one without editing `db.py`.

## 0.8.0 — reach

Three gaps that all had the same shape: the tool could only see the one path it was built
for, and everything outside it was either invisible or reported as broken.

### Fixed — the provider is checked BEFORE the socket is opened

`connect()` opened an IMAP connection on its first line and looked at the account's provider
afterwards. So every account had to name an `imap_host` whether or not it would ever use
one, and a tenant with IMAP disabled — the usual hardening step after a phishing incident,
and the reason some installs cannot use IMAP at all — failed at the connection before
authentication was even attempted. The error described the socket rather than the
arrangement. `msgraph.py` shipped in the same release and `connect()` could not reach it:
the path was IMAP end to end.

`provider` is now a real choice, and the rules live in one place (`tools/providers.py`) that
both the fetcher and the dashboard read:

| `provider` | who fetches | needs |
|---|---|---|
| `gmail` / `microsoft` / `imap` | this tool, over IMAP | `imap_host` |
| `graph` | `tools/msgraph.py` | `ms_client_id`, **no `imap_host`** |
| `connector` | something else; you pipe JSON into `ingest.py` | nothing |

An unrecognised provider is refused rather than defaulted to IMAP — dialling the wrong
backend produces a confusing error, while "I do not know what 'grpah' is" is actionable.

### Added — a connector is a declarable mailbox, not an absence

There was nowhere to say "this mailbox is fetched by my AI client's connector", so people
left it out of `accounts.json` — which means nothing in the tool knew it existed. `doctor`
could not report it, the setup panel could not count it, and the only record of the
arrangement was in someone's head.

Declared, it reports as **`CONNECTOR`** — its own state, never `FAILED`, because it is
working exactly as configured. A red row against a mailbox that is fine teaches its reader
to stop reading red rows. `doctor` now reports `connected`, `checked` and `not_fetched_here`
separately rather than folding them into one reassuring number.

### Added — `doctor` and `fetch` delegate to Graph instead of pointing at it

They used to answer "use msgraph.py", which is a direction, not an answer, and meant no
single command could describe an install that mixes backends. Flags are translated
explicitly, and **a flag with no equivalent stops the command** rather than being dropped —
a `fetch` that quietly ignores one of its own filters returns the wrong messages and looks
like it worked.

### Added — things stay open until someone says otherwise

A brief is a delta. A task assigned three weeks ago appeared in exactly one brief and then
vanished, because every run reports what *arrived*. Deltas cannot carry an obligation.

`open_items` is a standing list that survives between runs. Anything needing a person opens
an item; later runs age it rather than duplicating it; a recurring notice is **one** item,
keyed by the same thread rule the acks use, so a reply prefix does not split an obligation
from itself.

**Not the same as an ack**, and the difference is the point: an ack says "I have seen this",
and seeing something is not doing it.

The **Still open** panel sorts by AGE, not importance — it is the only list on the board that
gets worse by being ignored, so a three-week-old item nobody has touched outranks today's.

### Added — resolved off-channel is a first-class outcome

Most things that arrive by mail are finished on a call or in a chat. With nowhere to say so,
the only ways to clear an item are to lie about it or leave it open forever — and both end
with the list being ignored, which is the failure the panel exists to prevent. Three
outcomes: **done here**, **done elsewhere**, **no longer relevant**. Resolving is a state,
not a delete; the row and its paper trail stay, and it can be reopened.

`ingest.py` reports `opened` and `still_open_seen` on every run. `opened: 0` is a claim —
either nothing needed a person today or the carry-forward is not running, and those must
never read the same.

Upgrading an install with history: `dashboard/backfill_open_items.py` seeds the list from a
bounded window, dry by default. Deliberately not automatic — backfilling everything would
produce a list that is mostly stale on its first read, which is one nobody opens twice.

### Changed

- `subject_shape` moved into `db.py`, where thread keys are written. Two implementations of
  "the same thread" is what 0.5.2 was spent removing, and open items was about to add a third.
- `ingest_run()` returns the carry-forward stats instead of leaving them in a module global.

## 0.7.0 — onboarding that asks

The tool shipped `rules-and-policies.md` with `_Fill this in._` in five places and
`protected.example.json` full of placeholders. **It knew its rules were missing and it never
asked for them.** You could complete every setup step and have a dashboard full of
dispositions derived from judgment nobody chose. As the report put it: *even the whole
questionnaire process should have been obvious out of the box without me asking for it.*

### Added — questions generated from your own mailbox

`dashboard/questions.py` and `GET /api/questions`. Not a checklist. A generic questionnaire
is close to worthless — "how should we treat bots?" is unanswerable in the abstract — and
what makes a question worth answering is the evidence attached to it, which this tool is the
only thing in the room holding.

Every question carries the rows behind it and is **ranked by what being wrong would cost**,
not by volume:

| kind | asks about |
|---|---|
| `personally_addressed` | a sender you treat as noise that sometimes asks *you* to act |
| `sender_disposition` | volume with no history of ever mattering |
| `escalation_contacts` | who must never be missed — arms the guard directly |
| `concept_gap` | labels your runs use that belong to no concept, so they are invisible |
| `concept_never_actioned` | a whole category that has never needed you |
| `repeatedly_acknowledged` | something you keep dismissing by hand |
| `mailbox_role` | what each mailbox is *for* |

A message addressed to you personally and filed as bot noise outranks four hundred promos,
because the promos being wrong is an annoyance and the other is lost work.

**Senders whose mail is mostly money or security are asked about differently** — the stakes
are stated, and "auto-trash it" is not offered first. Never having acted on a year of
statements is not the same as that mail never mattering, and the evidence cannot tell those
apart.

### Added — recipients, so the most valuable question can be asked at all

`to` and `cc` are now captured by both fetchers and stored, with a derived
`addressed_directly` and `recipient_count`. Without them the tool cannot ask *"these three
were addressed to you personally — same rule?"*, which is the question that stops a bots
rule from binning work assigned to its owner.

An install with no recipient data still gets the question: it also fires on subjects that
ask a person to do something. **Unknown is stored as unknown, never as zero.**

### Added — answers become rules, with a look first

`tools/apply_answers.py`. Recording an answer and acting on it are different risks, so they
are different programs. The default is a dry run; `--write` is the only thing that touches
your file.

Everything it writes lives inside one marked block, so prose above and below is never
touched, re-running updates rather than duplicates, and `--revert` removes every elicited
rule in one gesture with no residue. Each rule records the evidence and date it came from —
a year later the file can still tell a rule you chose from a rule someone guessed.

Answers that imply no rule are **reported, not dropped**. "It matters sometimes — keep asking
me" is a real answer whose correct effect on the rules file is nothing at all.

### Added — resumable intake for a mailbox with years in it

`tools/intake.py`. Plan, fetch a batch, triage it, mark it done, resume tomorrow. It does not
classify — a program that filled in dispositions with defaults would produce exactly the
confident labels nobody chose that this release exists to remove.

**Paged by UID range, not by offset.** Offset counts back from the newest message, so mail
arriving or being deleted mid-intake shifts every later window and a message slides across a
boundary and is never fetched by any batch — nothing errors, no count looks wrong, and the
message is simply absent. A batch is retired only by an explicit `done`; a session killed
between fetching and ingesting gets that batch offered again, because a duplicated row is
visible and a skipped batch is not.

### Added — a fourth setup step, and a permanent way back to it

"Tell the tool how you work" joins the setup panel, outstanding while placeholders survive or
a high-weight question is unanswered. It is **advisory**: the guard refuses rules while it is
unset because binning a bank's mail is unrecoverable, but a tool that refuses to run until
you finish a questionnaire is one nobody finishes installing.

The dashboard header carries a question count whenever any are waiting — independent of the
setup panel, which removes itself once the install is sound. Without that, questions would be
offered on day one and never again, while the mailbox kept producing new ones.

### Changed — onboarding asks which mail route you have, before writing any config

A new Step 0: connector, app registration, IMAP app password, or "I don't know" — with what
each costs. The connector is often the *only* route an organisation will sanction and it was
the one route the skill never mentioned, so people were pushed toward the hardest path and
some concluded the tool would not work for them at all. Answering (a) skips three steps.

Both skills now offer the questions rather than waiting to be asked. Most people will not
think to ask an email tool to interview them.

### Fixed

- `db.connect()` / `init_db()` take an optional path and connection. They previously always
  opened the real store, so a fixture could not be built without touching live data.
- The rules-file path had two independent lookups that could disagree; there is now one.

## 0.6.0 — the guarantees move to `ingest`, so they work without a fetcher

Every safety property in this tool was bolted to the fetcher. Read-only was an `if` inside
`mailtool`; injection labelling happened in `mailtool` and `msgraph`; linkage and
concept-mapping were nobody's job at all. On an install that cannot run a fetcher — no app
registration, IMAP closed at the tenant, mail arriving through a connector — **none of it
ran, and nothing said so.** As the report put it: *the defense is not disabled, it is simply
never reached, which is worse, because there is no signal that it is absent.*

`ingest.py` already accepted plain JSON from any source. That makes it the seam, and the
guarantees now live there.

### `ingest.py` is a supported entry point, and now says so

**Bring your own fetcher.** It takes plain JSON with no dependency on `mailtool`, `msgraph`,
or anything else. If your organisation will not issue an app registration, has closed IMAP,
or gives you mail through a connector, produce the JSON however you can and pipe it in —
the dashboard, the record, the acks, the guard and the labelling all work identically.

This was always true and written down nowhere, so a deployment that could not use the fetcher
believed the whole tool was blocked for hours when only the fetcher was.

### Added — read-only is a property of the RUN, not of one module

`tools/runmode.py`. Whatever touches a mailbox asks the same question and gets the same
refusal, and `test_backend_parity.py` fails if a mutating entry point stops asking. On the
reporting install, the read-only phase was being enforced by a tool allowlist written into a
prompt — *an instruction to a model, not a refusal by a program*, which is the weaker class of
control this project spends its documentation arguing against.

### Added — untrusted text is labelled at ingest, whatever produced it

`untrusted.annotate_all()` runs at the universal entry points — ingest **and** the applier —
rather than in the fetchers alone. A hand-written run JSON, a connector export and an IMAP
sweep all get the same treatment, because none is more trustworthy than the sender. The label
is now **stored**, so it outlives the run and stays visible instead of evaporating.

One bug found doing it: `annotate` read `from` but not `sender`, so wherever the latter was in
use — which is the store's own spelling — the display name was never examined. That is the
field an impersonation actually forges.

### Added — a seeded self-test, because a clean report was a zero with no evidence

```
python tools/untrusted.py --selftest
```

Fires every seeded injection case and confirms ordinary mail stays quiet. Nothing shipped
that could prove the detector fires at all, so "no signals found" and "the detector is broken"
looked identical from outside — the exact thing this project's own rule forbids everywhere
else.

### Added — every run states its reach

```
linked  N/N messages carry a Message-ID
mapped  N/N messages resolve to a known concept
flagged 1/N messages carry injection signals
```

- **linked** — a row ingested without a `message_id` is silently unopenable forever, and the
  consequence appears much later in the viewer, by which time the source data is gone.
- **mapped** — a label that resolves to nothing is invisible in the way this project keeps
  warning about: the rollup still balances, the counts still look right, and the concept view
  is quietly wrong. Unmapped labels are now **named**, with a pointer to where they belong.
- `--strict` refuses to write incomplete data at all, for an intake where finding out later
  means the data cannot be reconstructed.

### Fixed — `ingest` reported success while deleting the previous batches

Re-ingesting a `run_date` replaces that day wholesale. Correct for a daily sweep; a footgun
for an intake done in batches, where every batch had to re-send everything already ingested
for that date or the earlier rows were silently deleted — and the return value reported the
count it had just written, which looked exactly like success.

`--append` adds to a day instead, and the return now states **both** numbers:
`{"written": 20, "replaced": 240}`.

## 0.5.2 — four things the dashboard stated confidently and got wrong

From a defect report written by using the tool rather than reading it. Every item below was
reproduced here before it was fixed.

### Fixed — acknowledging a thread silenced one person in it

A thread key included the **sender**, so every participant in one conversation got a distinct
key. Acknowledging a four-participant thread acknowledged exactly one quarter of it: the API
returned `ok`, the row rendered as acknowledged, the item disappeared — and everyone else's
messages kept arriving. Acking was O(participants), and the participant set grows *after* you
act, so a busy thread could never be fully acknowledged.

**A thread is a subject, not a person.** The key is now the subject shape scoped to the
mailbox. The trade-off is stated in the code and runs the other way — two senders whose
subjects reduce to the same shape now share a thread — which is the less bad error only
because it is *visible* the moment you act, where the old failure was silent. The real fix is
a thread id from `References`/`In-Reply-To`, and the store does not carry one yet.

Existing acknowledgements are migrated automatically, since the acks table already stores the
account, sender and subject each key was derived from. Left alone they would simply have
stopped matching, quietly returning handled items to the attention list.

### Fixed — the same bug was splitting the repeat-collapsing view

`subject_shape()` had no rule for `Re:` / `Fwd:`, so an original and its own replies did not
share a shape **even from one sender**. That function also feeds the repeats view — the one
whose comment calls it *the drowning mechanism* — so a notice was being split from its own
follow-ups. One missing rule, two features quietly wrong. Reply and forward prefixes are now
stripped as a chain (`Re: Re: Fwd:` is one match), in several languages, and the colon is
required so "Re-engineering the process" is untouched.

### Fixed — "not found in this mailbox" when the tool never reached the mailbox

One branch collapsed two conditions that mean opposite things: the tool searched and the
message is genuinely absent, and the tool **could not run at all** — no app registration, no
token, bad config. On an install where the fetcher cannot connect, every row reported the mail
as absent while it sat in the inbox untouched, and the UI added *"trashed mail is recoverable
for about 30 days"* on top — inviting the reader to conclude it had been deleted and might be
gone.

Two false statements about someone's data, in the reassuring direction, from a lookup that
never happened. The branches are now separate: an unreachable backend says *"could not reach
the mailbox — the message may still be there"*, surfaces the `detail` that was always captured
and never shown, and **never prints the retention line**, which is an inference no search
earned.

### Fixed — a backfill collapsed onto a single day

`msg_date` was stored from the beginning and queried nowhere — `run_date` appeared 61 times in
the server, `msg_date` zero. So an onboarding intake, which triages months of existing mail in
one session, rendered as **one tile**. The single most valuable thing a new user wants to see
is the shape of what they have been missing, and it was exactly what the view could not show.

The record now keys on when mail **arrived** (`?by=swept` asks the other question). This is
not the one-line `GROUP BY` it looks like: `msg_date` holds ISO dates, RFC 2822 dates and
NULLs in the same column, and grouping on the raw text buckets `Wed, 5 Aug 2026 ...` under its
weekday. The day is derived on write into `msg_day` — raw value kept as evidence, derived
value trusted by queries, exactly as `concept` sits beside `category` — and backfilled for
existing rows.

### Fixed — `doctor` sent people down a road that dead-ends

It called `connect()` and reported whatever threw last, so an install with no app registration
was told to run `auth-ms` — which cannot work without the registration, and for which there is
already a good specific error one step further on. `doctor` now validates configuration first
and reports the **first** thing wrong rather than the last thing that threw, including
"provider is graph, which this tool does not speak". With no accounts at all it says it checked
nothing rather than reporting `0 of 0`.

### Added — `install.ps1 -Upgrade`

Upgrading by copying a new version over the old one is the obvious thing to do and does not
re-run the installer, so config files a later version added never appeared — `concepts.local
.json` was simply absent, and the check needing it reported "not exercised" rather than
failing. Files a later version retired never left either: a stale `tools/secrets.py` survived
an overlay and went on shadowing the standard library module a newer backend had started
importing.

`-Upgrade` seeds what is missing, removes what is retired from an explicit manifest, and runs
the database migrations. It touches no existing config, no autostart entry, and does not
restart anything.

## 0.5.1 — the injection guard was not wired into the Graph backend

### Fixed — a guard that could not fire, for a whole release

`untrusted.annotate()` was wired into the IMAP backend and **not into the Graph one**. Both
fetch attacker-written text; only one labelled it.

The consequence compounded, and that is the real severity: `apply_proposal.py` refuses to bin
anything carrying `injection_signals` — the right guard — but a Graph-fetched message never
had the field, **so the guard silently never fired for Graph users.** It failed open, in
exactly the case it was built for, with no error, no warning and no log line.

Two things sharpened it. **Graph is the Microsoft 365 path**, so the deployments most likely
to adopt this were the ones getting none of the injection protection, while the IMAP path —
the one heading for retirement in business tenants — was the protected one. And the 0.5.0
architecture made it invisible: `untrusted.py` and `apply_proposal.py` are coherent enough
that a reader assumes coverage is universal, and every docstring says "the triage agent reads
sender names, subjects and snippets" with no backend qualifier.

Reproduced before fixing: a deliberately hostile message through the Graph fetch path carried
**no** `injection_signals` and **no** `_UNTRUSTED` envelope, while the detector found five
distinct signals in the same text.

**The fix that matters more than the wiring** is `tools/test_backend_parity.py`. The bug class
is "a second implementation of an interface misses a cross-cutting concern", and it will recur
every time a backend is added. The suite drives one hostile fixture through **every** backend
from a table and asserts identical labelling — adding a backend without adding it to that
table is itself the failure. It also checks the negative half, because a parity test that only
checked the hostile case would pass just as happily on a backend that stamped the label onto
everything, which would be worse than useless. Mutation-tested: removing the call from either
backend fails the suite.

### Fixed — the test suite could not run on a clean clone

`msgraph.py` read `accounts.json` at import, so the module was unimportable without config and
the Graph suite crashed on a fresh checkout — you had to install before you could test, which
is backwards. Both backends now load config lazily; a missing file surfaces at the command
that needs it, where it means something, rather than at `import`. Verified on a tree with no
config at all: 12 tests, exit 0.

### Fixed — the installer could report success about someone else's dashboard

`start-dashboard.ps1` is deliberately polite and no-ops when the port already serves an
email-dashboard. So a *second* install on the same machine started nothing, got the **first**
install's reply, and reported success — green, about the wrong copy, in exactly the case where
someone is testing a new version and wants to know it came up.

`/api/whoami` now reports the `root` of the install that is answering, and the installer
checks it and warns plainly when a different install holds the port. The install test asserts
it too.

## 0.5.0 — say which day you are looking at, and stop assuming what people play

### Fixed — you had to hunt the page to find which day was selected

The selected run date is the fact every other number on the page depends on, and it was the
quietest thing in the header: 13px grey text reading "showing run for 2026-08-06". Switching
days meant searching for the answer.

- The date is now **the loudest thing in the header** — large, accent-coloured, with a tag
  saying whether it is the **latest** run or an **older run** you deliberately went back to.
  That second state was previously invisible, which is the one worth knowing.
- **The heatmap shows which cell is selected.** The grid is the main way to change days and
  had no selected state at all. A ring is drawn outside the cell — never clipped by a
  neighbour, never fighting a fill for contrast — and it moves rather than redrawing the
  grid, so it is cheap enough to update on every change.
- The run picker now reads as holding a real selection rather than sitting there looking like
  an empty control.

All three stay in agreement: clicking a day in the grid moves the ring, updates the header
date and its tag, and syncs the picker.

### Changed — the Steam panel is optional, and off by default

Steam sale tracking is a real feature and a personal one: it says something about how someone
spends their time, which a mail triage tool has no business assuming. It is now switched by
`config/dashboard.local.json` and **off unless you turn it on** — an absent config means off,
the same fail-closed direction as everything else here, applied to taste instead of safety.

Turning it off hides the tab and the panel, and a view persisted from when it was on falls
back rather than restoring a tab that no longer exists.

### Documented

`INSTALL.md` now leads with the two things that actually stop an install, before the first
command rather than after a confusing failure: **Python must be on PATH** — which the
installer checks and stops on, and which on a managed machine may simply not be possible —
and **Windows only**, because the launcher, autostart and credential store are.

## 0.4.0 — the agent that reads the mail no longer holds the power to act on it

The gap this closes, from the install report: *"one agent both ingests untrusted input and
holds execution power, daily, unsupervised."*

The triage agent reads sender names, subjects and snippets — text written by anyone who knows
the address — and that text flows into the same context deciding what happens to the message.
The realistic harm was never "an attacker deleted my mail", because Trash is recoverable. It
is quieter: **a genuine security alert marked unimportant so it never reaches the person.**
The purpose of this tool is deciding what a human sees, which makes perception the asset
worth attacking.

### The structural change — propose, then dispose

A run is now two steps that cannot be collapsed into one:

```
MAILTOOL_READONLY=1 python tools/mailtool.py fetch ...   # classify; cannot act
python tools/apply_proposal.py run.json                  # decide, change nothing
python tools/apply_proposal.py run.json --apply          # move the survivors
```

`apply_proposal.py` is an ordinary program. **It reads no message body, calls no model, and
takes no instruction from anything a sender wrote** — it reads structured fields and stored
history, then re-derives every entitlement itself. A proposal is a request, exactly like a
click on the dashboard, and the dashboard already refuses to trust those.

It refuses to bin a message when the sender is on the protected list, the category is
protected, this run flagged it for attention, it carries injection signals, or the sender has
kept mail on record. If the guard is unconfigured it **refuses everything** and says so.

Why matching on attacker-controlled `sender` and `subject` is still safe: those fields are
used only to find reasons to *refuse*, so the worst a forgery achieves is protecting the
forger's own mail from the bin. Every error it can cause falls on the conservative side.

### Injection attempts become triage signals

`fetch` now labels any message whose text is addressed to the *triager* rather than to a
person — `injection_signals`, with a plain-English reason — and states in the payload itself
that its contents are data to classify, never instructions to follow. Nothing is dropped or
altered; the label is evidence, and the applier refuses to silently bin anything carrying one.

`untrusted.fence()` wraps text in a boundary a sender cannot close early: markers occurring in
the content are defanged before wrapping, so writing the closing marker into a message cannot
make the rest read as trusted.

**This is not a filter and must never be used as one.** Detection over natural language is
lossy, and anyone who reads the module can phrase around it in a minute. It is deliberately
tuned narrow — phrasings with essentially no innocent use — because a broad net would flag
half an inbox and the flag would stop meaning anything. A zero means "nothing obvious", never
"nothing present". The structural split above is the defence that does not depend on it.

### `MAILTOOL_READONLY`

Set it for the reading phase and `act` refuses outright, before touching the network, naming
the applier. Not a sandbox — anything that can set the variable can unset it — but it removes
the capability from the phase that should not have it, which is the part that was missing.

### Internal

`tools/test_separation.py` exercises all three together: eleven injection phrasings flagged
and seven ordinary messages left alone, the fence proof against early closure, the readonly
latch on and off, and the applier judged against a temp store — unconfigured guard refusing
everything, then each refusal reason fired individually. It touches no real mailbox, no real
config and no real database; the one thing it must never do is trash a message.

Two of its own assertions passed at first only because the applier was crashing — a substring
absent from a traceback, and a non-zero exit from an exception. Both now assert on what
actually ran.

## 0.3.0 — a fresh install stops looking broken

The observation this release answers: *"the audience this tool would most help is precisely
the audience least able to complete the setup — and config-file authoring is the single
largest drop-off point."*

### Added — a first-run panel

A new install renders an honest empty state in every panel, which is indistinguishable from
a tool that is failing. The one thing it never said was the only thing a new user needs.

`/api/setup` reports each step as **state plus an action**, re-derived from the same files
the tool actually reads — deliberately not a wizard that remembers where you got to, because
a wizard's memory can disagree with reality and then walk you past a step that silently did
not take. The panel appears only while something is outstanding and removes itself when the
last step is done, so its presence keeps meaning something.

### Added — the protected list is editable in the browser

This is the safety-critical file and, until now, the one most likely to be left as shipped
placeholders — because filling it in meant opening JSON in an editor. That is the wrong place
to lose someone: while the list is empty the guard refuses every rule, so the tool is least
useful exactly when its owner is least equipped to fix it.

One name per line, written atomically, every other key in the file preserved. The endpoint
carries the same CSRF guard as the rule writer, refuses an empty list, and refuses
underscore-prefixed placeholders — which the loader ignores, so accepting them would store
names that could never match anything. Only the names are writable; concepts, workflow
senders and the verification domain are not, because a write endpoint that can rewrite the
whole guard is a bigger thing to defend than one that appends to a list.

After saving, the panel re-renders from what the server **re-derived**, never from what was
typed. The loader's opinion is the one that counts.

### Fixed

- **"showing run for null."** With no runs, the client sent the literal string `"null"` as a
  date and the server echoed it back as though it had been looked up. `_resolve_date` now
  returns a run that exists or nothing at all, and the client sends no date when it has
  none. Small, but the same shape as every other defect here: an answer stated with more
  confidence than the lookup behind it — and it was sitting one line above the panel written
  to stop a fresh install looking broken.

### Internal

The install test now drives the whole first-run path on a genuinely fresh tree: setup
reports incomplete and names all three steps, `/api/run` invents no date under three
different queries, and the guard writer is exercised refusals-first — no header,
cross-origin, empty list, placeholder names — before the real write, then checked for
dedupe, casing, and that every other key in the file survived.

## 0.2.0 — a Microsoft Graph backend, for the mailboxes IMAP cannot reach

### Added — `provider: "graph"`

IMAP is a dead end for a large share of Microsoft 365 organisations. Tenants disable it as a
hardening step, usually right after a phishing incident; it is an admin-only setting a normal
employee can neither inspect nor change; and asking IT to reopen it is asking them to reverse
an incident response. Graph is not gated by that switch, consents once for a whole tenant,
and is a much easier approval — because granting it does not require reopening a protocol
someone deliberately closed.

`tools/msgraph.py` mirrors `mailtool.py`'s command surface — `auth`, `doctor`, `fetch`,
`body`, `find` — and emits the same JSON, so `ingest.py`, the dashboard and the skills consume
either backend unchanged. Set `provider` to `graph` on an account; no `imap_host` is needed.

> ### ⚠ Not yet verified against a live tenant
>
> Every branch is covered by tests, but those tests use a fake transport. **No request in
> this backend has ever reached graph.microsoft.com, no token has ever been issued, and no
> real mailbox has ever been read.** Unknown specifically: whether the PKCE round trip
> completes against Entra, whether `$skip` paging behaves as assumed, and whether a real 429
> is shaped the way the tests assume. Expect one debugging session. **IMAP remains the
> verified path**, and `auth` says so on every run until that changes.

**Read-only by design.** The token requests `Mail.Read`, so the tenant refuses a write even
if a bug attempted one — a guarantee enforced outside this code, which is worth more than any
check inside it. There is deliberately no `act` command. Note Graph has no move-only scope:
"archive the noise" and "permanently delete anything" arrive as the same grant, which is why
widening it should be a decision rather than a default.

**Auth is authorization-code + PKCE, not device code.** Device code is the flow used in
device-code phishing and security teams alert on it specifically; generating that signal for a
legitimate mail tool is an unforced cost. PKCE runs through the system browser, so it inherits
whatever session and Conditional Access state already lets the user's webmail work.

### Throttling, treated as a first-class concern

Graph frequently answers 429 with **no `Retry-After`**, so the fallback path is the common
path rather than a corner case — and inside a per-user penalty window a sub-10-second retry
essentially never succeeds while every 429 *extends* the penalty. A textbook 1s/2s/4s backoff
therefore makes the block longer. So:

- a hard **10-second floor** on fallback backoff, with jitter and a ceiling;
- the server's `Retry-After` obeyed exactly when present;
- **throttled** and **failed** as distinct exception types — a 403 will never become a 200,
  and retrying it is pointless and looks like an attack;
- a run-level circuit breaker counting throttle-outs **cumulatively**, because real throttling
  interleaves with successes and a consecutive-only counter never trips;
- a per-run request cap, since a paging-loop bug is the real ban risk, not daily volume;
- and the one that matters most: **a throttled sweep is labelled `"complete": false`, carries
  an explicit warning, and exits non-zero**, so a partial view can never be ingested as a full
  look. An absence in a sample is not an all-clear.

### Also in this release

- **`find` reports duplicates.** `internetMessageId` is not reliably unique within a mailbox —
  a self-CC, a forwarded loop, or one message filed in two folders all produce copies. It now
  fetches several, returns the most recent deliberately, and says how many exist.
- **A size ceiling on `body`.** `/$value` returns full MIME including attachments; an
  unbounded read is one large attachment away from taking the process down. Configurable with
  `--max-body-mb`.
- **Graph errors exit cleanly with guidance**, never a traceback. A 403 — the likeliest first
  run, when the registration lacks `Mail.Read` or consent was never granted — names the
  probable causes and uses a distinct exit code from "throttled, come back later".
- **Token writes are one transaction** rather than three separate encrypt-decrypt cycles of
  the whole credential store, which could previously leave an access token with no matching
  refresh token.
- **`ms_authority` now means the same thing to both backends.** Two backends reading one
  config key and disagreeing about its default is a bug that only appears when someone
  switches provider.
- The onboarding skill documents the two registration choices that cause almost every
  first-run failure: the redirect URI platform must be **"Mobile and desktop applications"**,
  not "Web", and the permission is **`Mail.Read`** — `Mail.ReadBasic` looks safer and is
  useless, because it strips message bodies.

### Internal

The Graph client takes its transport, token source, clock and credential store as
constructor arguments. Its tests inject fakes instead of reassigning module globals — which
matters because the first version did the latter, one test left a stub in place, and the next
test failed for a reason that had nothing to do with the code under test. The 401 path also
*writes* to the credential store, so running that suite wrote into the real store of whoever
ran it. The suite now touches no network, no credential store and no wall clock, and runs in
under a fifth of a second instead of ninety.

## 0.1.1 — the install path actually works

0.1.0 could not be installed. Everything below came from one careful defect report against a
real install on Windows 11 / Python 3.14, connecting a Microsoft 365 work mailbox.

### Fixed — blocking

- **`install.ps1` resolved the app tree one directory too high.** It took the parent of its
  own folder, but it ships *beside* `app/`, so every path pointed at a directory that does
  not exist and the install failed. It now fails immediately and names the layout if the
  tree is not where it expects.
- **The installer wrote `accounts.json` with a UTF-8 BOM.** Windows PowerShell 5.1's
  `Set-Content -Encoding UTF8` means *with* BOM, and `json.load` raises on one — so the
  config was unreadable on every fresh install under 5.1 while working under PowerShell 7.
  Written BOM-less now, and every config reader accepts `utf-8-sig` so an editor cannot
  break the tool with three invisible bytes.

### Fixed — safety

- **The installer defeated the fail-closed protection guard.** It copied
  `protected.example.json` verbatim, and the guard treated any file that parsed as
  configured — so a fresh install reported itself armed while its five placeholder names
  matched no real sender. Closed at both ends: the template's placeholders are now
  `_`-prefixed and ignored, *and* an empty name list reports `configured: false` with a
  reason. An absent guard must never read as "nothing is protected".

### Fixed — Microsoft accounts

- **OAuth was hard-coded to `/consumers/`**, which accepts personal accounts only, so no
  work or school mailbox could ever sign in. The authority is now `ms_authority` in
  `accounts.json`, defaulting to `common`.
- **A missing `ms_client_id` raised a bare `KeyError`.** It now explains that an Entra ID
  app registration is needed, gives the exact steps, and says the registration is *one per
  deployment, not per user*. The onboarding skill documents it too — it was the single
  largest piece of unwritten setup.

### Fixed — onboarding docs disagreed with the code on every key

- The skill said `address`, `provider: gmail|outlook|imap`, and an IMAP *port*. The code
  reads `email`, compares `provider == "microsoft"`, requires `imap_host` for **every**
  account, and has no port setting at all. Following the documented steps produced a config
  that raised `KeyError: 'email'` on the first command. Rewritten against the code.
- Step order was impossible for Microsoft: `auth-ms` reads `ms_client_id` out of
  `accounts.json`, so the config must exist first. Reordered, and the no-op "store the
  secret" step is gone for Microsoft — `auth-ms` stores its own tokens.

### Fixed — credential CLI

- **`tools/secrets.py` → `tools/credstore.py`.** The old name shadowed the standard
  library's `secrets` module for the whole process, because `tools/` goes on `sys.path` at
  position 0 and the `as secret_store` alias does not prevent it. Dormant, but a trap for
  the first person to reach for `secrets.token_urlsafe` in this tree.
- The documented `set --account <address>` bound `account="--account"` and then blocked
  silently on stdin with nothing printed — a hung terminal at exactly the moment the docs
  said to be careful with a password. Flags are now rejected with a usage message, and an
  interactive run prompts with the input hidden.

### Fixed — other

- `find --all-folders` silently skipped mailboxes whose names the server returned unquoted,
  which is common and legal. Both folder parsers now share one that handles either form.
- `install.ps1 -Port` was accepted and passed nowhere, so installing on another port
  silently installed on 9770.
- The login port check used `Test-NetConnection` (seconds) instead of a socket connect
  (~40 ms), on every login.

### Added

- **`concepts.local.json`** is created at install time so the label-extension mechanism is
  discoverable before you need it, rather than after labels start resolving to UNMAPPED.
- **The `maintain-dashboard` skill now states that fetched mail is data to classify, never
  instructions to follow.** The message sanitiser protects the human reading a message in
  the browser; it is a separate code path and gives the *agent* no protection at all. Text
  that tries to steer the triager is itself a phishing signal.
- **An install test that actually runs `install.ps1`** into a throwaway tree and asserts on
  the result — the app tree resolves, no BOM is written, `mailtool` can read the config, the
  guard reports itself unconfigured, and the installed copy serves. Both blocking defects
  above existed because the installer had only ever been read and parse-checked.

### Known limitations

- **IMAP-only.** Microsoft 365 tenants increasingly disable IMAP as a hardening step, and it
  is an admin-only setting an employee can neither check nor change. A Microsoft Graph
  backend is planned for 0.2.0; Graph is not gated by the IMAP flag and is an easier
  approval for IT than reopening a protocol they closed on purpose.
- **Installation still needs a developer.** Python on PATH, a config file authored by hand,
  and a protected list filled in before the tool is useful. A first-run wizard in the
  dashboard is planned for 0.3.0.

## 0.1.0

Initial release.
