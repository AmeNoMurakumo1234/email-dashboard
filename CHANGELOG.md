# Changelog

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
