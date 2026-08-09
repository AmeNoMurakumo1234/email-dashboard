"""The one place the version is written down.

WHY THIS FILE EXISTS AT ALL. The version used to live only in the exporter, which meant the
running dashboard did not know what it was - so the page could not tell you, and the only way
to find out what a live server was serving was to read the source it had loaded hours ago.

That is the wrong way round, because the failure this number exists to catch is exactly a
server running code from before your last edit. A dashboard was once found serving a whole
day's stale code: the process had started the previous evening, the files it served were
edited later that night, nobody restarted it, and a shipped feature never reached the page.
The uptime watchdog said green the entire time, because HTTP 200 answers "is something
listening", never "is it what you shipped".

SO THE NUMBER MUST COME FROM THE RUNNING PROCESS, not from a file the page reads. This module
is imported once at start-up and the value is served from memory, which means a stale server
keeps reporting its OLD version while the repo has moved on - and that mismatch, visible at a
glance beside the title, is the whole point. Baking the version into the HTML would defeat it:
static files are read per request, so a stale process would happily serve a fresh number and
tell you everything was fine.

The exporter reads this file rather than declaring its own copy. Two spellings of one version
is the drift this project keeps paying for.
"""

VERSION = "0.25.0"
