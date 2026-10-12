"""OptionsService: models, commands, effort and mode menu options management.

Coordinates the CLI's available models and slash commands, the effective model/effort
configuration, permission modes, and emitting OPTION frames to the guest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..agent import policy, review
from ..agent.prompts import text as prompts_text
from ..guest import GuestSession
from ..view import html as H
from ..wire.protocol import Frame, option, optionsend, prefsack
from .config import BridgeConfig

log = logging.getLogger("bridge.options")

# Effort levels, in the order they should appear. Which of these a given model
# actually offers comes from the CLI, not from here.
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
EFFORT_LABEL = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Maximum",
}

# Execution modes offered to the guest, in menu order: the presets, then the
# client's own (bridge/agent/policy.py). The CLI's permission-mode names are
# no longer on the wire -- they were a description of somebody else's
# behaviour (see policy.py's module note).
MODE_ORDER = list(policy.PRESET_ORDER)

# Slash commands offered on the Commands menu, in menu order.
COMMAND_MENU = ["/compact", "/rename", "/init"]


class OptionsService:
    """Manages models, slash-commands, permission modes, and options delivery."""

    _models_cache: list[dict] | None = None
    _commands_cache: list[dict] | None = None

    def __init__(
        self,
        connection: Any,
        conv: Any | None = None,
        config: BridgeConfig | None = None,
        write_block: Callable[..., Any] | None = None,
        set_effort: Callable[..., Any] | None = None,
    ) -> None:
        self.connection = connection
        self.conv = conv
        self.config = config
        self.write_block = write_block
        self.set_effort = set_effort

    def clear_cache(self) -> None:
        """Clear cached CLI models and slash-commands."""
        self._models_cache = None
        self._commands_cache = None

    async def _models(self, guest: GuestSession) -> list[dict]:
        """The CLI's model list, fetched once per process (04.14)."""
        if self._models_cache is None:
            if not guest.claude:
                return []
            models = await guest.claude.server_models()
            if not models:
                return []
            self._models_cache = models
        return self._models_cache

    async def _commands(self, guest: GuestSession) -> list[dict]:
        """The CLI's slash-command list, cached like `_models`."""
        if self._commands_cache is None:
            if not guest.claude:
                return []
            cmds = await guest.claude.server_commands()
            if not cmds:
                return []
            self._commands_cache = cmds
        return self._commands_cache

    def effective_model(self, guest: GuestSession) -> str:
        """What model this guest's session actually runs on: the chat's own
        choice, else the client's default (Options), else the bridge's."""
        prefs = getattr(guest, "prefs", None)
        fallback = self.config.model if self.config else ""
        return (guest.model or getattr(prefs, "general_model", "")
                or fallback or "")

    def effective_effort(self, guest: GuestSession) -> str:
        """Effort's counterpart to `effective_model`."""
        prefs = getattr(guest, "prefs", None)
        fallback = self.config.effort if self.config else ""
        return (guest.effort or getattr(prefs, "general_effort", "")
                or fallback or "")

    async def on_prefs(self, guest: GuestSession, f: Frame) -> None:
        """The client's settings, re-sent because the person pressed OK.

        Answered with PREFSACK, never ERROR (see `prefsack`). What each
        setting does on arrival: defaults for new chats wait for a new chat;
        page size applies to the next page; sync settings to the next pass;
        lists and modes at once -- except that the protected list never
        shrinks inside a session (`prefs.floor`), and a chat whose mode was
        just deleted falls back to the default without forgetting it.
        """
        from .. import prefs as P
        acp = getattr(getattr(guest, "caps", None), "acp", "") or "cp1252"
        try:
            new = P.parse(f.body or b"", acp)
        except P.PrefsError as exc:
            log.warning("prefs refused: %s", exc)
            await self.connection.send(guest, prefsack(False, exc.key,
                                                       exc.why))
            return
        old = getattr(guest, "prefs", None) or P.DEFAULT
        new, held = P.floor(old, new)
        guest.prefs = new
        if held and self.write_block:
            await self.write_block(guest, H.notice(prompts_text(
                "policy.protect_held", places=", ".join(held))))
        if policy.lookup(guest.mode_id, new.modes) is None:
            gone = guest.mode_id
            guest.mode_id = policy.default_mode(guest)
            guest.grants = set()
            log.info("mode %s is gone; this chat now uses %s",
                     gone, guest.mode_id)
            if self.write_block:
                await self.write_block(guest, H.notice(prompts_text(
                    "policy.mode_gone", now=policy.mode_of(guest).label)))
        await self.connection.send(guest, prefsack(True))
        await self.send_options(guest)

    def remember_settings(
        self, guest: GuestSession, session_id: str | None = None
    ) -> None:
        """Persist the model, effort and mode chosen for this conversation."""
        conv_id = session_id or guest.conv_id
        if not conv_id or self.conv is None:
            return
        self.conv.remember(
            conv_id,
            model=guest.model,
            effort=guest.effort,
            mode=policy.AS_LEGACY.get(guest.mode_id, "default"),
            mode_id=guest.mode_id,
            grants=sorted(getattr(guest, "grants", ()) or ()),
            grants_machine=getattr(guest, "machine", "") or "",
        )

    async def send_options(self, guest: GuestSession) -> None:
        """The model and effort menus, with the one in use marked."""
        if not guest.claude and self._models_cache is None:
            return
        models = await self._models(guest)

        def resolved_of(value: str) -> str:
            for m in models:
                if m.get("value") == value:
                    return m.get("resolvedModel") or value
            return value

        in_use = resolved_of(self.effective_model(guest) or "default")

        chosen = None
        for m in models:
            value = m.get("value") or ""
            if value == "default":
                continue        # an alias, not a model
            label = m.get("displayName") or value
            current = (m.get("resolvedModel") or value) == in_use
            if current and chosen is None:
                chosen = m
            # `description` rides as its own header rather than folding into
            # `label`, which also drives the toolbar button -- see `option`'s
            # docstring in wire/protocol.py.
            await self.connection.send(
                guest,
                option("model", value, label, current, desc=m.get("description") or ""),
            )

        # Effort is a property of the model: Haiku has none at all, so an
        # Effort menu offering five levels would be lying.
        levels = (chosen or {}).get("supportedEffortLevels") or []
        if not levels:
            await self.connection.send(
                guest,
                option(
                    "effort",
                    "",
                    "(not available for this model)",
                    False,
                    disabled=True,
                ),
            )
        else:
            for lvl in EFFORT_ORDER:
                if lvl not in levels:
                    continue
                await self.connection.send(
                    guest,
                    option(
                        "effort",
                        lvl,
                        EFFORT_LABEL.get(lvl, lvl),
                        lvl == self.effective_effort(guest),
                    ),
                )

        await self.connection.send(
            guest,
            option(
                "work",
                guest.work_guest,
                guest.work_guest or "<unset>",
                False,
                desc=guest.work_guest,
            ),
        )

        user = getattr(getattr(guest, "prefs", None), "modes", None) or {}
        current = policy.mode_of(guest).id
        for m in [*policy.presets(), *user.values()]:
            await self.connection.send(
                guest, option("mode", m.id, m.label, m.id == current))

        have = {}
        for c in await self._commands(guest):
            n = c.get("name")
            if isinstance(n, str) and n:
                have["/" + n] = c.get("argumentHint") or ""
        for cmd in COMMAND_MENU:
            if cmd not in have:
                continue
            hint = have[cmd]
            label = (cmd + " " + hint) if hint else cmd
            await self.connection.send(
                guest, option("command", cmd, label, False, hint=hint)
            )
        await self.connection.send(guest, optionsend())

    async def on_setmodel(self, guest: GuestSession, f: Frame) -> None:
        model = f.headers.get("model", "")
        was = guest.model
        guest.model = model
        self.remember_settings(guest)
        log.info("model -> %s", model or "(default)")
        if guest.claude:
            try:
                await guest.claude.set_model(model)
            except Exception as exc:
                log.exception("model change failed")
                guest.model = was
                self.remember_settings(guest)
                if self.write_block:
                    await self.write_block(
                        guest,
                        H.error("Could not change the model: " + str(exc)[:160]),
                    )
                await self.send_options(guest)
                return
        if self.write_block:
            await self.write_block(guest, H.notice("Model set to " + model))
        await self.send_options(guest)

    async def on_seteffort(self, guest: GuestSession, f: Frame) -> None:
        if self.set_effort:
            await self.set_effort(guest, f.headers.get("effort", ""))

    async def on_setmode(self, guest: GuestSession, f: Frame) -> None:
        await self.set_mode(guest, f.headers.get("mode", ""))

    async def set_mode(self, guest: GuestSession, mode: str) -> None:
        """Change the conversation's execution mode.

        Bookkeeping, mostly: the desk reads the mode on every call, so the
        new table applies to the next one. The SDK hears about it only at
        the Plan boundary -- the one place its own permission mode differs
        between modes -- and a refusal there is put back the way BACKLOG 18
        settled: accept it, restore what is in force, resend the menu.
        """
        user = getattr(getattr(guest, "prefs", None), "modes", None)
        new = policy.lookup(mode, user)
        if new is None:
            log.warning("ignoring unknown execution mode %r", mode)
            await self.send_options(guest)
            return
        old = policy.mode_of(guest)
        was, was_before = guest.mode_id, guest.mode_before_plan
        guest.mode_id = new.id
        if new.sdk == "plan" and old.sdk != "plan":
            guest.mode_before_plan = old.id
        # A grant was given under the old table; it means nothing under the
        # new one, and must not carry a licence across.
        guest.grants = set()
        self.remember_settings(guest)
        if guest.claude and new.sdk != old.sdk:
            try:
                await guest.claude.set_permission_mode(new.sdk)
            except Exception as exc:
                log.warning("execution mode -> %s refused by the CLI for %s: "
                            "%s", new.id,
                            self.effective_model(guest) or "(default)", exc)
                guest.mode_id, guest.mode_before_plan = was, was_before
                self.remember_settings(guest)
                if self.write_block:
                    await self.write_block(guest, H.error(
                        "Could not switch to " + new.label + ": "
                        + str(exc)[:160]))
                await self.send_options(guest)
                return
        log.info("execution mode -> %s", new.id)
        if any(new.value(c) == policy.CHECK for c in new.values):
            review.warm()
        if self.write_block:
            await self.write_block(guest, H.notice(
                new.label + ": " + policy.summary(new)))
        await self.send_options(guest)
