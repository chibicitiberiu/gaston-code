"""Execution modes: what each category of call does, and the decision itself.

A MODE IS A TABLE. Every category from `categories.py` gets one of four
values -- Off, Ask, Check, Allow -- and a call takes the strictest value among
the categories it lands in. That is the whole policy; everything subtle lives
in the classifier, where it can be tested as a table too.

  Off     the tool is not offered, and a call that arrives anyway is refused
  Ask     the bar
  Check   the second model (`review.py`) decides; the bar only when unsure
  Allow   it happens

OURS, NOT THE CLI'S. The SDK runs in `default` -- or `plan` for Plan first --
and every decision about the person's machine is made here. The CLI's own
`auto` skipped `can_use_tool` for some tools (measured 2026-10-02) and
refuses Haiku outright; `acceptEdits` approved edits the bridge never saw.
Both made the rules view a description of somebody else's behaviour.

THE PRESETS ARE THE PLAN'S TABLE (docs/OPTIONS-PLAN.md), and a full-matrix
agreement test holds the client's copy to this one, because the client shows
them and must not show different ones.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from . import categories as K
from . import prompts

log = logging.getLogger("bridge.policy")

OFF = "off"
ASK = "ask"
CHECK = "check"
ALLOW = "allow"
VALUES = (OFF, ASK, CHECK, ALLOW)
#: Not a mode value: the call is refused whatever the mode says.
REFUSE = "refuse"

_RANK = {ALLOW: 0, CHECK: 1, ASK: 2, OFF: 3}


def stricter(a: str, b: str) -> str:
    return a if _RANK.get(a, 2) >= _RANK.get(b, 2) else b


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    #: The SDK's permission mode under it: "default", or "plan".
    sdk: str = "default"
    values: Mapping = field(default_factory=dict)
    builtin: bool = True

    def value(self, cat: str) -> str:
        # A category this mode predates asks. Never Allow by omission.
        return self.values.get(cat, ASK)


def _row(files_work, files_else, files_sys, run_work, run_built, run_else,
         run_sys, read, purge, screen_see, screen_drive, bridge_shell,
         web) -> dict:
    return dict(zip(K.CATS, (files_work, files_else, files_sys, run_work,
                             run_built, run_else, run_sys, read, purge,
                             screen_see, screen_drive, bridge_shell, web),
                    strict=True))


_A, _C, _Y = ASK, CHECK, ALLOW
#: (id, sdk mode, values in K.CATS order). Labels come from policy.toml.
_PRESETS = (
    ("manual", "default",
     _row(_A, _A, _A, _A, _A, _A, _A, _Y, _A, _Y, _A, _A, _A)),
    ("workfolder", "default",
     _row(_Y, _A, _A, _Y, _A, _A, _A, _Y, _A, _Y, _A, _A, _Y)),
    ("plan", "plan",
     _row(_A, _A, _A, _A, _A, _A, _A, _Y, _A, _Y, _A, _A, _A)),
    ("auto", "default",
     _row(_Y, _C, _C, _Y, _Y, _C, _C, _Y, _C, _Y, _C, _C, _Y)),
)

PRESET_ORDER = tuple(p[0] for p in _PRESETS)
DEFAULT = "manual"

#: The CLI's mode names, as conversations saved before 2026-10-04 carry them.
#: Mapped when a row is READ, never rewritten in place: an older bridge may
#: still be reading the same file, and rolling back is running it.
LEGACY = {"default": "manual", "acceptEdits": "workfolder",
          "plan": "plan", "auto": "auto"}
#: And back, for the legacy `mode` key a newer bridge keeps writing so a
#: rollback finds something it understands.
AS_LEGACY = {"manual": "default", "workfolder": "acceptEdits",
             "plan": "plan", "auto": "auto"}


def preset(mode_id: str) -> Mode | None:
    for pid, sdk, values in _PRESETS:
        if pid == mode_id:
            return Mode(pid, prompts.text(f"policy.mode.{pid}.label"), sdk,
                        values, True)
    return None


def presets() -> list:
    return [m for m in (preset(p) for p in PRESET_ORDER) if m is not None]


def lookup(mode_id: str, user: Mapping | None = None) -> Mode | None:
    """A mode by id: a preset, or one of the client's own."""
    mode_id = LEGACY.get(mode_id, mode_id)
    found = preset(mode_id)
    if found is not None:
        return found
    return (user or {}).get(mode_id)


def default_mode(guest) -> str:
    """The mode a new chat starts in: the client's choice, else Ask first."""
    prefs = getattr(guest, "prefs", None)
    want = str(getattr(prefs, "general_mode", "") or "")
    if want and lookup(want, getattr(prefs, "modes", None)) is not None:
        return want
    return DEFAULT


def mode_of(guest) -> Mode:
    """The mode this guest's conversation is in, never None."""
    user = getattr(getattr(guest, "prefs", None), "modes", None)
    m = lookup(str(getattr(guest, "mode_id", "") or DEFAULT), user)
    return m if m is not None else preset(DEFAULT)  # type: ignore[return-value]


def summary(mode: Mode) -> str:
    """One or two sentences on what a mode does, for the notice and the
    rules view. Fixed for a preset; generated for the client's own."""
    if mode.builtin:
        return prompts.text(f"policy.mode.{mode.id}.summary")
    allowed = [c for c in K.CATS if mode.value(c) == ALLOW]
    checked = [c for c in K.CATS if mode.value(c) == CHECK]
    parts = []
    if allowed:
        parts.append(prompts.text("policy.summary.allows", cats=", ".join(
            prompts.text(f"policy.cat.{c}") for c in allowed)))
    if checked:
        parts.append(prompts.text("policy.summary.checks", cats=", ".join(
            prompts.text(f"policy.cat.{c}") for c in checked)))
    if not parts:
        parts.append(prompts.text("policy.summary.asks"))
    return " ".join(parts)


# --- the decision -----------------------------------------------------------

#: Categories a per-chat grant may lift. Never the system tier, a permanent
#: delete or an unknown program: one click must not buy a standing licence
#: for those (BACKLOG 11b, and the shell profile's `unbounded` note).
GRANTABLE = (K.FILES_WORK, K.RUN_WORK, K.RUN_BUILT, K.SCREEN_DRIVE)


@dataclass(frozen=True)
class Decision:
    value: str
    #: The finding that decided it, for the bar, the checker and the log.
    hit: K.Hit | None = None
    #: Every finding that took the strictest value.
    hits: tuple = ()

    @property
    def why(self) -> str:
        return self.hit.rule if self.hit is not None else ""


def _granted(hit: K.Hit, grants: Iterable) -> bool:
    if hit.cat not in GRANTABLE or hit.doubt:
        return False
    for g in grants or ():
        cat, _, subject = str(g).partition(":")
        if cat == hit.cat and (not subject or subject == hit.subject):
            return True
    return False


def decide(mode: Mode, verdict: K.Verdict, grants: Iterable = ()) -> Decision:
    """What happens to a classified call under `mode`.

    Strictest wins. A doubtful finding takes the stricter of its tier's
    "elsewhere" and "system" values -- it may be either -- and is never
    Off on doubt alone: a call this cannot place is asked about, not
    silently refused.
    """
    if verdict.refuse:
        return Decision(REFUSE, K.Hit("", verdict.refuse))
    if verdict.exempt:
        return Decision(ALLOW)
    if verdict.plan_exit:
        return Decision(ASK)
    if not verdict.hits:
        return Decision(ASK)                  # a tool nobody classified
    best = ALLOW
    deciding: list = []
    for h in verdict.hits:
        v = mode.value(h.cat)
        if h.doubt and h.cat in K.DOUBT_OF:
            v = stricter(v, mode.value(K.DOUBT_OF[h.cat]))
            if v == OFF:
                v = ASK
        if v in (ASK, CHECK) and _granted(h, grants):
            v = ALLOW
        if _RANK[v] > _RANK[best]:
            best, deciding = v, [h]
        elif v == best:
            deciding.append(h)
    return Decision(best, deciding[0] if deciding else None, tuple(deciding))
