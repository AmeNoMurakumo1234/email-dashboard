# Email Routine Dashboard

A local, private triage board for your own mail.

An agent sweeps your mailboxes on a schedule and files what it finds; this is where the
result lives - what was binned and why, what was kept, and the few things that actually
need you. It runs entirely on your machine, binds to localhost, and ships with **no
mailboxes and no credentials**. You add your own.

## What it does that a mail client does not

- **Explains itself.** Every message carries the reason it was binned or kept. The record
  is auditable, not a black box.
- **Opens mail without letting it phone home.** Messages render in a sandboxed reader that
  strips scripts and blocks every remote image, so tracking pixels never fire. Links are
  defanged and show where they really go.
- **Judges links by what a sender NORMALLY does.** It learns each sender's usual hosts, so
  a familiar name suddenly linking somewhere new stands out - the shape of a spoof of
  someone you already trust.
- **Alarms by seeing nothing.** A biller that has gone quiet, or a notice arriving for the
  fifth time and getting faster, are both things no inbox will ever tell you.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File plugin\install.ps1
```

Creates local config from templates, an empty database, and a hidden login autostart, then
opens the dashboard at http://127.0.0.1:9770. Use `-NoAutostart` to skip the autostart and
`-Uninstall` to remove it.

Then run the **onboard-mailbox** skill to add your first account.

## Requirements

Windows, Python 3.11+, and nothing else - the tool is standard-library only. There are no
third-party packages between your mailbox and you.

## Privacy

Mail metadata lives in a local SQLite file: senders, subjects, and the reasoning behind each
decision. Nothing is uploaded, and the reader makes no outbound request of any kind. The
database is **not encrypted**, so keep it on a machine you control.

## Licence

MIT.
