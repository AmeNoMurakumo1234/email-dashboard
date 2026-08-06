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
store via `tools/secrets.py` and are read from there at connect time. If a step seems to
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
in another is a finding.

## Step 2 — get a credential the tool can use

**Gmail / Google Workspace** — app passwords require 2-Step Verification to be on already.
Direct them to their Google Account → Security → 2-Step Verification → App passwords, and
have them generate one *for mail*. Tell them to name it something they will recognise later
(e.g. `mail-routine`) so it can be revoked independently of everything else.

**Outlook / Hotmail / Live** — these use OAuth2 device flow, not a password. Run:

```
python tools/mailtool.py auth-ms --account <address>
```

and read them the code and URL it prints. They approve it in their own browser. No password
ever reaches this machine.

**Anything else** — needs an IMAP host and port; ask for those and add them to
`config/accounts.json` alongside the address.

## Step 3 — store it without writing it down

```
python tools/secrets.py set --account <address>
```

This prompts for the credential and writes it to the OS credential store (DPAPI on
Windows). **Let the owner type it into that prompt themselves.** Do not accept it in chat
and do not pass it as a command-line argument — arguments land in shell history.

## Step 4 — add the account to the config

Edit `config/accounts.json`:

```json
{ "accounts": [
    { "address": "<address>", "role": "<role>", "provider": "gmail" }
] }
```

`provider` is `gmail`, `outlook`, or `imap` (with `host` and `port` for the last).

## Step 5 — prove it connects, and say what you actually checked

```
python tools/mailtool.py doctor
```

Every account should report `CONNECTED`. **Read the whole output, not the first lines** — a
partial read is how an intermittent failure gets missed. If the summary count disagrees
with the per-account statuses, trust the per-account statuses and say so.

If an account fails, the error names the cause. The usual ones: app password not created
for *mail*, 2-Step Verification not enabled, or a typo in the address.

## Step 6 — tell the tool who must never be auto-trashed

Open `config/protected.local.json` and fill in `protected_names` **before** the first sweep.
Ask directly:

> *"Whose mail must never be filtered or missed — family, your bank, your doctor, your
> employer? And is there anything you'd want pushed to your phone the moment it arrives?"*

This list is the guard behind every automatic rule the dashboard can write. It **fails
closed**: while it is empty, the dashboard refuses to write any auto-trash rule at all. That
is the safe direction, but it also means the tool is less useful until it is filled in.

## Step 7 — the first sweep

```
python tools/mailtool.py fetch --account <address> --days 2 --limit 200
```

Then triage per the routine, write the run JSON, and ingest it. The dashboard is empty
until a run is ingested — that is expected, not a fault.

## What "done" looks like

- `doctor` reports CONNECTED for the account, on two consecutive passes
- `protected.local.json` names the people who matter
- one run has been ingested and the dashboard shows it at `http://127.0.0.1:9770`
- **no password exists in any file, log, or shell history**

## If you are an agent doing this unattended

Don't. Steps 2, 3 and 6 need the owner: only they can create a credential, only they should
type it, and only they know who counts as family. If they are not available, set up what you
can, say plainly which steps are outstanding, and stop — a mailbox that is half-onboarded
and quietly filtering mail is worse than one that is not connected yet.
