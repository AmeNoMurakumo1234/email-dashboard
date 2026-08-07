"""Which backend speaks to which mailbox. One answer, read by everything that asks.

THE BUG THIS EXISTS TO KILL. `connect()` opened an IMAP socket on its first line, before it
had looked at the account's provider at all:

    conn = imaplib.IMAP4_SSL(acct["imap_host"])   # unconditional
    if acct["provider"] == "microsoft": ...       # XOAUTH2 over that same socket

So every account had to supply an IMAP host whether or not it would ever use one, and a
tenant with IMAP disabled - the common hardening step after a phishing incident, and the
original reason the reporting install could not use IMAP at all - failed at the socket
before authentication was even attempted. `msgraph.py` shipped in the same release and
`connect()` could not reach it: the path was IMAP end to end.

THE OTHER HALF IS THE SAME BUG. An organisation whose blessed path is a mail connector in
its AI client had nowhere to say so. Leaving the account out of `accounts.json` is what
people actually did, which means nothing in the tool knows the mailbox exists: `doctor`
cannot report it, the setup panel cannot count it, and the only record of the arrangement is
in someone's head. A supported path you cannot declare is not really supported.

So a provider is now a first-class choice with three answers, and every one of them is
legitimate:

    imap       this tool opens the connection (password, or Microsoft XOAUTH2)
    graph      tools/msgraph.py fetches over Microsoft Graph
    connector  something else fetches; you pipe JSON into dashboard/ingest.py

`connector` HAS NO FETCHER AND THAT IS NOT AN ERROR. It must never be reported as FAILED.
A red row for a mailbox that is working exactly as configured teaches its reader to ignore
red rows, and this tool's whole argument is that its signals mean something.
"""

IMAP = "imap"
GRAPH = "graph"
CONNECTOR = "connector"

# What people actually write, mapped to what the code dispatches on. `microsoft` stays IMAP
# because that is what it has always meant here - IMAP with XOAUTH2 - and silently
# re-pointing existing installs at a different backend on upgrade would be a change of
# behaviour disguised as a rename.
ALIASES = {
    "": IMAP,
    "gmail": IMAP, "google": IMAP, "imap": IMAP, "generic": IMAP,
    "microsoft": IMAP, "outlook": IMAP, "office365": IMAP, "o365": IMAP,
    "graph": GRAPH, "msgraph": GRAPH, "microsoft-graph": GRAPH,
    "connector": CONNECTOR, "mcp": CONNECTOR, "manual": CONNECTOR,
    "external": CONNECTOR, "ingest": CONNECTOR, "byo": CONNECTOR,
}

LABEL = {
    IMAP: "IMAP",
    GRAPH: "Microsoft Graph (tools/msgraph.py)",
    CONNECTOR: "connector / bring-your-own-fetcher (dashboard/ingest.py)",
}


def backend_of(acct):
    """The backend for this account, or None if the provider string is unrecognised.

    None rather than a default. Guessing IMAP for a typo'd provider is how an account ends
    up being dialled by the wrong code with a confusing error, and "I do not know what
    'grpah' is" is both true and immediately actionable.
    """
    return ALIASES.get(str(acct.get("provider") or "").strip().lower())


def uses_imap(acct):
    return backend_of(acct) == IMAP


def fetches_itself(acct):
    """Can THIS TOOL pull mail for this account? False for a connector, and that is fine."""
    return backend_of(acct) in (IMAP, GRAPH)


def problems(acct, cfg=None):
    """Everything wrong with this account's config, per backend. Cheapest checks first.

    Config only - no socket is opened here. A connection error on a misconfigured account
    describes a symptom of the misconfiguration rather than the misconfiguration itself,
    and sends its reader down the wrong road.
    """
    cfg = cfg or {}
    out = []
    if not acct.get("email"):
        out.append("no \"email\" key - the config uses `email`, not `address`")

    raw = str(acct.get("provider") or "").strip()
    backend = backend_of(acct)
    if backend is None:
        out.append(
            "\"provider\": %r is not one this tool knows. Use \"imap\" (or \"gmail\" / "
            "\"microsoft\"), \"graph\" for Microsoft Graph, or \"connector\" if something "
            "else fetches your mail and you pipe it into dashboard/ingest.py." % raw)
        return out

    if backend == IMAP:
        if not str(acct.get("imap_host") or "").strip():
            out.append("no \"imap_host\" - required for every account this tool connects "
                       "over IMAP, including Microsoft ones (outlook.office365.com). If "
                       "IMAP is closed at your tenant, use \"provider\": \"graph\" or "
                       "\"connector\" instead - neither needs a host.")
        if raw.lower() in ("microsoft", "outlook", "office365", "o365") \
                and not str(cfg.get("ms_client_id") or "").strip():
            out.append(
                "provider is %r but there is no top-level \"ms_client_id\". Microsoft "
                "sign-in needs an Entra app registration - ONE per deployment. Without it "
                "`auth-ms` cannot run either, so authenticating is not the next step; "
                "creating or obtaining the registration is." % raw)
    elif backend == GRAPH:
        if not str(cfg.get("ms_client_id") or "").strip():
            out.append(
                "provider is \"graph\" but there is no top-level \"ms_client_id\". Graph "
                "needs an Entra app registration - run `python tools/msgraph.py auth "
                "--account %s` and it will print the full setup."
                % (acct.get("email") or "<address>"))
        # imap_host is NOT required, and is not an error if present - people migrating from
        # IMAP leave it behind, and refusing the config over a harmless leftover key would
        # be the tool being difficult about something that costs nothing.
    elif backend == CONNECTOR:
        # Nothing is required. Declaring the mailbox IS the configuration: it tells doctor
        # the account exists and is fetched elsewhere, so its absence from a sweep is a
        # known state rather than a hole nobody can see.
        pass
    return out


def status_of(acct, cfg=None):
    """(status, detail) for `doctor`, WITHOUT opening a connection.

    Returns None for accounts this tool must actually dial to know about - the caller
    connects those itself. A connector account never gets dialled and never should.
    """
    probs = problems(acct, cfg)
    if probs:
        return "NOT CONFIGURED", probs[0]
    if backend_of(acct) == CONNECTOR:
        return ("CONNECTOR",
                "declared as fetched elsewhere - this tool does not connect to it, and "
                "that is the configuration working. Feed it with dashboard/ingest.py.")
    return None, None
