"""Make stdout able to print a stored subject line. One line, needed everywhere.

Subjects contain whatever a sender typed - emoji, accents, CJK - and a Windows console
defaults to cp1252, which cannot encode most of it. Any entry point that prints stored text
therefore dies mid-listing with a UnicodeEncodeError and a stack trace, on a machine where
nothing is wrong with the data or the tool.

Measured on one store: 170 of the distinct subjects it holds are not cp1252-encodable.
Emoji in marketing subject lines and social notifications are not an edge case; they are the
majority of a modern inbox.

`errors="replace"` is the half that matters. A console that cannot render a glyph should
print `?` and keep going - never abort a listing partway through. Losing one character is a
cosmetic problem; losing the rest of the output is a functional one, and the two were being
treated the same.

Reported twice against two different files before it was fixed as a class - first against
`ack.py --list`, then against `backfill_bodies.py --dry-run`, which was WRITTEN AFTER the
first report. Fixing the file that was named rather than the shape of the defect is how the
same bug gets reported three times.

    from consoleio import safe_console
    safe_console()
"""
import sys


def safe_console():
    """Reconfigure stdout/stderr to UTF-8 with replacement. Idempotent and never raises.

    Never raises, deliberately: this is called at the top of entry points, and a helper that
    could abort a program before it starts - over the ENCODING of its output - would be a
    worse bug than the one it fixes. If a stream cannot be reconfigured, printing is no worse
    off than it was.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return True
