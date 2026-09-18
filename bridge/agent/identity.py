"""GuestIdentity: which machine this is, and which conversation is live.

The audit's "Identity" `GuestSession` group (docs/audit/02-bridge-python.md
#2): `sess_id`, `hello`, `claude`, `model`, `effort`. The plainest of the
four groups #2.2 named -- nothing here is its own actor with a lock or an
RPC namespace the way Link/View/Sync/Prompts are, it is simply the facts
`_attach`, `_make_session` and the Model/Effort menus read and write. Moved
onto its own type only so `GuestSession` stops being the thing that holds
every group's fields directly, following the same field-plus-forwarded-
properties pattern `link`/`view`/`sync`/`perms` already established --
`GuestSession.sess_id`/`hello`/`claude`/`model`/`effort` forward here so
nothing outside server.py had to change shape.
docs/audit/02-bridge-python.md 02.42.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .session import Session


@dataclass
class GuestIdentity:
    #: A stable per-connection handle. Set once, here, and never overwritten
    #: again -- it used to be re-pointed at whichever Claude session happened
    #: to be live: "a bridge handle" on a fresh connection, "the SDK session"
    #: after a new-session or an effort fork, and simply stale after a
    #: switch, which never touched it at all. Nothing reads it for anything
    #: but READY's header and logging, which is exactly why it was free to
    #: drift (docs/audit/02-bridge-python.md #02.24) -- the SDK's own id
    #: already travels in SWITCHED and in the `init` system message.
    sess_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    #: The machine facts from HELLO, kept so every session this guest opens
    #: gets the same description of the computer it is acting for.
    hello: dict = field(default_factory=dict)
    #: The live Claude Agent SDK session, or None before the first one is
    #: built.
    claude: Session | None = None
    #: Chosen from the Model and Effort menus. Held per guest rather than per
    #: process so a reconnect or an effort fork keeps the choice.
    model: str = ""
    effort: str = ""
