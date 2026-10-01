"""OptionsService: models, commands, effort and mode menu options management.

Coordinates the CLI's available models and slash commands, the effective model/effort
configuration, permission modes, and emitting OPTION frames to the guest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..guest import GuestSession
from ..view import html as H
from ..wire.protocol import Frame, option, optionsend
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

# Permission modes offered to the guest, in menu order.
MODE_ORDER = ["default", "acceptEdits", "plan", "auto"]
MODE_LABEL = {
    "default": "Manual",
    "acceptEdits": "Accept edits",
    "plan": "Plan",
    "auto": "Auto",
}
MODE_CAVEAT = {
    "auto": (
        "Measured on this bridge: with the permission prompt installed, "
        "the model's own danger classifier is not consulted -- "
        "`git reset --hard` on a repo with uncommitted work ran "
        "unchanged in Auto and in Manual. Auto still asks you; it does "
        "not add a second opinion."
    ),
}

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
        """What model this guest's session actually runs on."""
        fallback = self.config.model if self.config else ""
        return guest.model or fallback or ""

    def effective_effort(self, guest: GuestSession) -> str:
        """Effort's counterpart to `effective_model`."""
        fallback = self.config.effort if self.config else ""
        return guest.effort or fallback or ""

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
            mode=guest.perm_mode,
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

        for mode in MODE_ORDER:
            await self.connection.send(
                guest,
                option(
                    "mode",
                    mode,
                    MODE_LABEL.get(mode, mode),
                    mode == guest.perm_mode,
                ),
            )

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
        """Change the permission mode live.

        BACKLOG 18: there is no SDK-reported list of which permission modes a
        given model accepts (unlike `supportedEffortLevels` for effort), so
        the menu cannot honestly refuse to offer one up front without
        hardcoding a per-model guess -- exactly the kind of table
        `send_options`'s effort handling goes out of its way to avoid. So the
        fix here is the other honest option: accept the refusal, put
        `guest.perm_mode` back to the mode actually in force, and resend the
        menu so the guest agrees with the bridge instead of showing a tick
        next to a mode that was never applied.
        """
        if mode not in MODE_ORDER:
            log.warning("ignoring unknown permission mode %r", mode)
            return
        was = guest.perm_mode
        guest.perm_mode = mode
        self.remember_settings(guest)
        label = MODE_LABEL.get(mode, mode)
        if guest.claude:
            try:
                await guest.claude.set_permission_mode(mode)
            except Exception as exc:
                # A model refusing a mode it cannot run (Haiku has no Auto)
                # is an expected outcome, not a bridge bug -- one line naming
                # the model and mode, not a traceback. `guest.claude`'s own
                # `_opts.permission_mode` was never updated on this path (the
                # assignment there runs only after the await succeeds), so
                # the Session and `guest.perm_mode` are both put back in
                # sync with what is actually running.
                log.warning(
                    "permission mode -> %s refused for model %s: %s",
                    mode, self.effective_model(guest) or "(default)", exc,
                )
                guest.perm_mode = was
                self.remember_settings(guest)
                if self.write_block:
                    await self.write_block(
                        guest,
                        H.error(
                            "Could not set permission mode to " + label + ": "
                            + str(exc)[:160]
                        ),
                    )
                await self.send_options(guest)
                return
        log.info("permission mode -> %s", mode)
        note = "Permission mode set to " + label + "."
        caveat = MODE_CAVEAT.get(mode)
        if caveat:
            note += " " + caveat
        if self.write_block:
            await self.write_block(guest, H.notice(note))
        await self.send_options(guest)
