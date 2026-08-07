---
name: onboard-mailbox
description: Use when adding a mailbox to the Email Routine Dashboard for the first time, connecting an email account, setting up IMAP credentials, or when the dashboard reports that no accounts are configured. Walks the whole path - app password or OAuth, credential storage, connection test, first sweep - without ever putting a password in a file.
---

# Onboarding a mailbox

The dashboard ships with **no accounts**. This is how the first one gets added, and it is
deliberately a conversation with the owner rather than something an agent completes alone.

## The rule that outranks convenience

**You never ask for, type, echo, or store a password in plain text.** Not in a config file,
not in a run log, not back to the user "to confirm". Credentials go into the OS credential
store via `tools/credstore.py` and are read from there at connect time. If a step seems to
require handling a password directly, that step is wrong — stop and say so.

For the same reason: **the owner creates the app password themselves**, in their own
browser, signed into their own account. You tell them where to click; you do not do it for
them and you do not need to see the result.

## Step 1 — decide what this mailbox is FOR

Ask before configuring. The answer becomes the mailbox's `role`, and the routine uses it to
decide what routine mail belongs where.

Common roles: `primary` (real-name, professional), `alias` (a pseudonymous identity),
`gaming`, `business`, `legacy` (kept for archives, rarely swept).

Ask plainly: *"What do you use this mailbox for, and is it in your real name or an alias?"*
Record the answer — it matters later, because a sender that belongs in one box and turns up
in another is a finding. (The role travels in the run JSON, not in `accounts.json`.)

## Step 2 — add the account to the config, FIRST

This comes before authentication, not after: for Microsoft, `auth-ms` reads `ms_client_id`
out of this same file, so it cannot run until the file exists.

Edit `config/accounts.json`:

```json
{
  "ms_client_id": "<app registration guid, Microsoft only>",
  "ms_authority": "common",
  "accounts": [
    { "email": "<address>", "provider": "gmail", "imap_host": "imap.gmail.com" }
  ]
}
```

Exactly three keys per account, and the names matter — the code reads `email`, not
`address`:

| key | value |
|---|---|
| `email` | the full address |
| `provider` | `microsoft` for Outlook/Hotmail/Live/365 (that exact word); anything else is treated as password-based |
| `imap_host` | **required for every account**, including Microsoft — `imap.gmail.com`, `outlook.office365.com`, or the provider's own |

There is no port setting. The connection is always implicit TLS on 993.

`ms_authority` selects who may sign in: `common` (personal + work/school, the default),
`organizations`, `consumers`, or a tenant GUID.

## Step 3 — get a credential the tool can use

**Gmail / Google Workspace** — app passwords require 2-Step Verification to be on already.
Direct them to their Google Account → Security → 2-Step Verification → App passwords, and
have them generate one *for mail*. Tell them to name it something they will recognise later
(e.g. `mail-routine`) so it can be revoked independently of everything else.

Then store it — **Gmail only**:

```
python tools/credstore.py set <address> app_password
```

Run in a terminal it prompts with the input hidden; piped, it reads stdin. **Let the owner
type it into that prompt themselves.** Never pass a credential as a command-line argument —
arguments land in shell history.

**Microsoft (Outlook / Hotmail / Live / Microsoft 365)** — OAuth2 device flow, no password,
and **no `credstore` step at all**: `auth-ms` stores its own tokens. Telling someone to run
`credstore set` afterwards only invites them to invent a credential to type in.

*Before the first Microsoft account on a deployment*, an administrator registers the app
**once** — not once per user:

> [entra.microsoft.com](https://entra.microsoft.com) → App registrations → New registration
> → Supported account types matching your `ms_authority` → Authentication → **Allow public
> client flows: Yes** → API permissions → Microsoft Graph → Delegated → `offline_access`,
> plus `IMAP.AccessAsUser.All` from Office 365 Exchange Online.

Put the Application (client) ID in `ms_client_id`. It identifies the app, not the person, so
it is not a secret and belongs in `accounts.json` rather than the credential store. One
registration serves everyone; if it is missing, `auth-ms` says so and repeats these steps.

Then:

```
python tools/mailtool.py auth-ms --account <address>
```

Read them the code and URL it prints; they approve it in their own browser.

**Note for Microsoft 365 organisations:** many tenants disable IMAP as a hardening step —
usually right after a phishing incident — and it is an admin-only setting a normal employee
can neither check nor change. If `doctor` fails with a login error on a work mailbox, suspect
this first, and accept that the answer may legitimately be that IMAP is not coming back.

For those mailboxes there is a second backend: **`provider: "graph"`**, which talks to
Microsoft Graph instead of IMAP. It is not gated by the IMAP switch, consents once for a
whole tenant, and is an easier approval for IT than reopening a protocol they closed on
purpose. Set `provider` to `graph` (no `imap_host` needed) and use `tools/msgraph.py` in
place of `mailtool.py` — same commands, same JSON:

```
python tools/msgraph.py auth   --account <address>
python tools/msgraph.py doctor
python tools/msgraph.py fetch  --account <address> --days 2 --limit 200
```

The app registration differs from the IMAP one in two ways that cause almost all first-run
failures: the redirect URI platform must be **"Mobile and desktop applications"**, not
"Web" — Web demands a client secret and rejects the flow, failing late and without naming
the cause — and the delegated permission is **`Mail.Read`** plus `offline_access` on
Microsoft Graph. `Mail.ReadBasic` looks safer and is useless: it strips message bodies, so
no snippets and no link extraction. Redirect URI is `http://localhost` with no port.

**The Graph backend has not yet been confirmed against a live tenant.** Every branch is
tested, but with a fake transport — no request has reached Graph and no real mailbox has
been read. Treat the first run as a debugging session, and report what happens. IMAP remains
the verified path.

It is also **read-only by design**: the token requests `Mail.Read`, so a bug cannot move or
delete anything even if it tried — the refusal is enforced by Microsoft, outside this code.
There is deliberately no `act` command.

**Anything else** — set `provider` to anything other than `microsoft` and give the
provider's `imap_host`; store the password under the field `password` (or `app_password`,
which is tried first).

### If none of those are available to you — bring your own fetcher

**`ingest.py` is a supported entry point, not an internal detail.** It takes plain JSON from
any source and has no dependency on the fetchers at all:

```
cat run.json | python dashboard/ingest.py --append
```

If the organisation will not issue an app registration, has closed IMAP, or gives you mail
through a connector in your AI client, produce that JSON however you can and pipe it in. The
dashboard, the record, the acks, the protected guard and the injection labelling all work
identically — **only the fetcher is unavailable, not the tool.** Say this early rather than
letting someone discover it after `doctor` reports `FAILED`.

The shape is documented at the top of `dashboard/ingest.py`. The fields that matter for each
message: `account`, `sender`, `subject`, `msg_date`, `disposition`, `category`, `reason`,
`importance`, and **`message_id`** — that last one is what lets a row be opened later, and a
row without it can never be linked afterwards. `ingest` reports `linked N/M` on every run so
you find out immediately rather than months later.

## Step 4 — prove it connects, and say what you actually checked

```
python tools/mailtool.py doctor
```

Every account should report `CONNECTED`. **Read the whole output, not the first lines** — a
partial read is how an intermittent failure gets missed. If the summary count disagrees
with the per-account statuses, trust the per-account statuses and say so.

If an account fails, the error names the cause. The usual ones: app password not created
for *mail*, 2-Step Verification not enabled, or a typo in the address.

## Step 5 — tell the tool who must never be auto-trashed

Open `config/protected.local.json` and fill in `protected_names` **before** the first sweep.
Ask directly:

> *"Whose mail must never be filtered or missed — family, your bank, your doctor, your
> employer? And is there anything you'd want pushed to your phone the moment it arrives?"*

This list is the guard behind every automatic rule the dashboard can write. It **fails
closed**: while it is empty — or still holding only the shipped placeholders — the dashboard
reports itself UNCONFIGURED and refuses to write any auto-trash rule at all. That is the safe
direction, but it also means the tool is less useful until it is filled in.

The template's placeholder names are deliberately `_`-prefixed and therefore ignored, so
copying the file verbatim protects nobody **and says so**. An earlier build reported
`configured: true` on that same untouched copy — armed-looking and inert, which is the exact
failure this file exists to prevent. Strip the leading underscore from each line you fill in.

## Step 6 — the first sweep

```
python tools/mailtool.py fetch --account <address> --days 2 --limit 200
```

Then triage per the routine, write the run JSON, and ingest it. The dashboard is empty
until a run is ingested — that is expected, not a fault.

## What "done" looks like

- `doctor` reports CONNECTED for the account, on two consecutive passes
- `protected.local.json` names the people who matter, and `/api/whoami` no longer reports it unconfigured
- one run has been ingested and the dashboard shows it at `http://127.0.0.1:9770`
- **no password exists in any file, log, or shell history**

## If you are an agent doing this unattended

Don't. Steps 3 and 5 need the owner: only they can create a credential, only they should
type it, and only they know who counts as family. If they are not available, set up what you
can, say plainly which steps are outstanding, and stop — a mailbox that is half-onboarded
and quietly filtering mail is worse than one that is not connected yet.
