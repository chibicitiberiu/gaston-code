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
    #: This conversation's own working folder, absolute, on the BRIDGE.
    #:
    #: Empty means the shared one (`Bridge.cwd`), which is what every
    #: conversation used before per-chat folders existed and is still what a
    #: chat created without naming one gets. When set it is a SUBFOLDER OF
    #: THE MIRROR, not a new mirror: one mirror, one baseline, one watcher
    #: (BACKLOG.md §7) -- the conversation moves inside the tree rather than
    #: the tree moving.
    work_dir: str = ""
    #: The same folder as the GUEST spells it -- `C:\Gaston\LISTFILE`.
    #:
    #: Empty until the conversation has been spoken in. A folder is named
    #: after the conversation, and a conversation has no name until the CLI
    #: titles it from the first message, so anything chosen before then would
    #: be a guess the user would have to live with. The toolbar shows
    #: "<unset>" for exactly that long.
    work_guest: str = ""
    #: The first thing the user said in this conversation.
    #:
    #: Kept because the FOLDER IS NAMED FROM IT. A folder is named after the
    #: conversation, and the CLI titles a conversation from its first message
    #: -- but the folder is committed at the first TOOL CALL, which can
    #: happen inside that same first turn, before any title exists. The
    #: alternative was a placeholder folder renamed afterwards, which is a
    #: new tree_id and a new baseline for a tree sync has already scanned.
    first_prompt: str = ""
    #: A conversation the guest has OPENED but not yet spoken in.
    #:
    #: Switching chats replays stored history and builds no SDK client, so
    #: between the switch and the first message `claude` is None and this
    #: holds the id the client will resume from. `conv_id` is the question
    #: "which conversation is this" that everything else should ask, because
    #: the answer lives in one of these two places depending on a state the
    #: caller does not care about.
    pending_resume: str = ""
    #: The permission mode the next session is built with, and the one the
    #: live client is currently in. Per guest for the same reason model and
    #: effort are: a switch, a new session and the effort fork all rebuild
    #: the Session, and a mode the user chose must not quietly revert to
    #: "default" because the object holding it was thrown away.
    perm_mode: str = "default"
