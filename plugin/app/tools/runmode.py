"""May this run change the mailbox? A property of the RUN, not of one module.

WHY THIS IS NOT INSIDE mailtool.py ANY MORE. The read-only latch was a single `if` in
mailtool's `act`, which is a good control in the wrong place: it protected exactly one
backend. On an install where mailtool cannot connect - no app registration, IMAP disabled at
the tenant, a connector used instead - the project's central safety mechanism was simply
absent, and nothing said so. A field report put it exactly right: the read-only phase was
being enforced by a tool allowlist written into a prompt, which is *an instruction to a
model, not a refusal by a program* - the weaker class of control this project spends its
documentation arguing against.

So the question moves up. Whatever touches a mailbox - mailtool, Graph, a connector, a
backend nobody has written yet - asks the same question here and gets the same refusal,
and `test_backend_parity.py` fails if a backend's mutating entry point does not ask.

WHAT THIS IS NOT. Not a sandbox. Anything that can set the variable can unset it, and a
backend that never calls `enforce()` is not stopped by it. It removes the capability from
the phase that should not have it and makes bypassing it deliberate rather than available -
the structural defence is the propose/dispose split in apply_proposal.py, which does not
depend on this at all.

    MAILTOOL_READONLY=1     # the reading phase: classify, propose, never mutate
"""
import os

ENV = "MAILTOOL_READONLY"
# "" / "0" / "false" mean OFF, so an empty variable left by a shell does not silently latch
# every run into read-only and make the applier look broken.
_OFF = ("", "0", "false", "no", "off")


class ReadOnlyRefusal(RuntimeError):
    """Raised instead of mutating a mailbox while the run is read-only."""


def is_readonly():
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def enforce(action="modify mail", backend="this backend"):
    """Refuse, loudly and identically, whichever backend is asking.

    Called at the TOP of any command that moves, deletes, flags or sends - before a socket
    is opened, before a token is fetched, before anything the refusal would have to undo.
    """
    if not is_readonly():
        return
    raise ReadOnlyRefusal(
        f"REFUSED: {ENV} is set, so this run may not {action}.\n"
        f"  {backend} was asked to change the mailbox during a read-only phase.\n"
        f"  The reading phase classifies and writes a proposal; tools/apply_proposal.py\n"
        f"  applies it after re-deriving every entitlement from the store and the\n"
        f"  protected list. If you need to act, run that - do not unset this to get past it."
    )


def describe():
    """One line for a run report, so the mode is stated rather than assumed."""
    return (f"read-only ({ENV} set): this run may not change any mailbox"
            if is_readonly() else
            f"read-write ({ENV} not set): mailbox changes are permitted")
