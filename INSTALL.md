# Installing

## Before you start — the thing that stops most installs

**Python 3.11+ must be on your PATH.** This is the most common failure here and the one the
installer cannot fix for you: on a managed or work machine you may not be permitted to
install software at all, and if that is the case, this tool is not installable there. The
installer checks first and stops with a plain message rather than failing later in a
confusing way.

```powershell
python --version    # if this errors, install Python, then open a NEW terminal and retry
```

Nothing else is required. The tool is standard-library only — no pip, no virtualenv, no
third-party code between your mailbox and you.

**Windows only, for now.** The launcher and login autostart are PowerShell and VBS, and the
credential store is Windows DPAPI. The Python itself is portable; those three are not.

## 1. Install

```powershell
powershell -ExecutionPolicy Bypass -File plugin\install.ps1
```

This creates `config/protected.local.json`, an empty database, and a hidden VBS entry in
your Startup folder so the dashboard is running whenever you log in. It adds **no
mailboxes** - that is deliberate.

## 2. Add a mailbox

Run the **onboard-mailbox** skill and follow it with your agent. It covers app passwords
(Gmail) and OAuth device flow (Outlook), stores the credential in the OS credential store,
and tests the connection.

You will be asked to create the app password yourself, in your own browser. The tool never
asks you to type a password into a chat, a file, or a command line.

## 3. Say who matters

Fill in `protected_names` in `config/protected.local.json` before the first sweep: family,
your bank, your doctor, your employer. This list is the guard behind every automatic rule
the dashboard can write, and it **fails closed** - while it is empty, no auto-trash rule can
be written at all.

## 4. First sweep

Run the **maintain-dashboard** skill. The dashboard is empty until a run is ingested.

## Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File plugin\install.ps1 -Uninstall
```

Removes the autostart entry. Your config, database and filed mail are left alone - delete
the folder yourself if you want them gone.
