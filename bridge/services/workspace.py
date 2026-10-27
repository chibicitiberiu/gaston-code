"""WorkspaceService: conversation work directories and guest path mapping.

Manages work directory naming, commitment at first tool call, path mapping
between host mirror and guest paths, and SETWORK frame handling.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from typing import Any

from ..agent import sessions as sesslist
from ..files import ignores as I
from ..files import paths as P
from ..guest import GuestSession, work_root_on_guest
from ..view import html as H
from ..wire.protocol import Frame
from .config import BridgeConfig

log = logging.getLogger("bridge.workspace")


class WorkspaceService:
    """Manages workspace directories and path mapping for guest sessions."""

    def __init__(
        self,
        config: BridgeConfig,
        conv: Any | None = None,
        write_block: Callable[..., Any] | None = None,
        send_options: Callable[..., Any] | None = None,
        spawn_sync: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.conv = conv
        self.write_block = write_block
        self.send_options = send_options
        self.spawn_sync = spawn_sync

    def stored_work_dir(self, session_id: str) -> str:
        """The folder a stored conversation was started in, if it is ours.

        Only a folder INSIDE the mirror is accepted.
        """
        base = self.config.sync_dir
        if not base:
            return ""
        for r in sesslist.listing():
            if r.get("id") != session_id:
                continue
            cwd = r.get("cwd") or ""
            if not cwd:
                return ""
            try:
                rel = os.path.relpath(cwd, base)
            except ValueError:
                return ""
            if rel in (".", "") or rel.startswith(".."):
                return ""
            return os.path.join(base, rel)
        return ""

    def mirror_dir(self, guest: GuestSession) -> str:
        """The directory on THIS side that pairs with the guest's work folder,
        or \"\" while this conversation has not committed to one.
        """
        return guest.work_dir or ""

    def work_dir(self, guest: GuestSession) -> str:
        """Where the agent works for THIS conversation."""
        return guest.work_dir or self.config.cwd or os.getcwd()

    def seed_ignores(self, path: str) -> bool:
        """Put the starter ignore list in a folder we have just made.

        The list is written INTO the mirror rather than held in this
        program, so it reaches the machine with the next pass and the
        person there can open it. See `files/ignores.py`.
        """
        try:
            return I.seed(path)
        except OSError:
            log.exception("could not write the ignore list in %s", path)
            return False

    def retarget_perms(self, guest: GuestSession) -> None:
        """Point the permission desk at this conversation's folder."""
        desk = getattr(guest, "perms", None)
        if desk is not None:
            desk._cwd = self.work_dir(guest)

    def guest_home(self, guest: GuestSession) -> str:
        r"""The work root the CLIENT declared, spelled its way -- `C:\WORK`.

        The parent of every conversation folder on that machine, and the
        bridge never chooses it: it is what the user put in the client's own
        INI, created there at startup, and announced in HELLO.
        """
        return (guest.hello or {}).get("sync_root") or guest.guest_sync_root or ""

    def guest_path(self, guest: GuestSession, name: str) -> str:
        r"""`<work root><sep><name>`, spelled the way the guest spells it."""
        return P.guest_path(self.guest_home(guest), name)

    def guest_work_root(self, guest: GuestSession) -> str:
        r"""Where commands run, spelled the way the guest spells it."""
        return work_root_on_guest(guest)

    def sub_of(self, guest: GuestSession) -> str:
        """This conversation's folder name relative to the mirror, or \"\"."""
        # sync_dir is None when sync is switched off, and then nothing is
        # mirrored, so nothing is a subfolder of it.
        if not self.config.sync_dir:
            return ""
        return P.sub_of(guest.work_dir, self.config.sync_dir)

    async def ensure_work_dir(self, guest: GuestSession) -> None:
        """Commit this conversation to a folder, if it has not already."""
        if guest.work_guest:
            if guest.conv_id and self.conv is not None:
                name = (
                    P.folder_name(
                        guest.work_guest.replace("\\", "/").rsplit("/", 1)[-1],
                        guest.caps,
                    )
                    or "WORK"
                )
                self.conv.set_folder(guest.conv_id, name, guest.work_guest)
            return
        await self.name_work_dir(guest)
        if guest.work_guest and self.send_options:
            await self.send_options(guest)

    # --- naming a conversation's folder ---------------------------------------
    #
    # TWO FOLDERS, AND ONLY ONE OF THEM NEEDS A MIRROR. Collapsing them is
    # 2026-10-01's bug: `name_work_dir` opened with `if not self.config.sync_dir:
    # return`, so a bridge started without `--sync-dir` left `work_guest` empty
    # as well as `work_dir`, the agent was told nothing was mirrored and no
    # destination, and it put a file in the root of C: on real hardware.
    #
    #   * `work_dir` is a tree ON THIS SIDE, and it only means anything with a
    #     mirror behind it: a local directory, a baseline, and a bidirectional
    #     pass that propagates deletions. That half stays gated on `sync_dir`,
    #     and adopting the machine's root into it uninvited is still the one
    #     option here that can lose a byte.
    #   * `work_guest` is a path ON THE MACHINE. It needs none of that. The
    #     client already creates the folder it is told to use, parents and all
    #     (`win/app/workdir.cpp` `rc_workdir_apply`), under the home the user
    #     configured and which it declares in HELLO -- so a conversation can
    #     have its own folder over there with nothing on this side involved.

    def _caps(self, guest: GuestSession):
        """This client's rules, or the restrictive default it is assumed to
        have before HELLO has been read.

        `GuestSession.caps` is None until the handshake, and the helpers in
        `files/paths.py` all take None and fall back -- except `canonical`,
        which dereferences it. Resolved once here so none of the naming path
        has to remember which.
        """
        return guest.caps if guest.caps is not None else P.DEFAULT

    def _fold(self, guest: GuestSession, name: str) -> str:
        r"""One folder name as the CLIENT would match it.

        `ConvStore.taken()` compares raw strings, which is Python's rule and
        not the machine's. On a case-folding volume `Notes` and `NOTES` are
        ONE directory, so two conversations would be handed the same folder
        and the second one's files would land on top of the first one's. That
        is loss rather than untidiness, so the comparison is made under the
        client's own declared `case` rule.
        """
        return P.canonical(name, caps=self._caps(guest))

    def taken_names(self, guest: GuestSession) -> set:
        """Every folder name a conversation already owns, folded.

        THE REGISTRY IS THE CONVERSATION STORE, not a directory listing on
        the machine. Two reasons. The question that matters is "does another
        conversation own this name", which only this side can answer; and
        asking the machine would cost a round trip on every first tool call
        -- on a 200MHz box over a serial-speed link -- to answer a different
        question, and still race the person at the keyboard.
        """
        if self.conv is None:
            return set()
        return {self._fold(guest, n) for n in self.conv.taken()}

    def title_source(self, guest: GuestSession) -> str:
        """The text the folder is named after: this conversation's title.

        `first_prompt` rather than the stored title where there is one,
        because the folder is committed at the first TOOL CALL, which can
        happen inside the first turn -- before the CLI has titled anything.
        """
        if guest.first_prompt:
            return guest.first_prompt
        for r in sesslist.listing():
            if r.get("id") == guest.conv_id:
                return r.get("title") or ""
        return ""

    def choose_name(self, guest: GuestSession, source: str) -> str:
        """The folder name this conversation should get, or \"\".

        Reads the store and the mirror and mutates nothing, so the system
        prompt can ask what the name WILL be before the folder exists.

        EVERY RULE COMES FROM THE CLIENT'S DECLARATION. `P.title_folder` ->
        `P.folder_name` and `P.check` apply `caps.illegal` (FAT refuses
        `< > : " \\ | ? *`), `caps.devices` (`CON`, `PRN`, `AUX`, `NUL`,
        `COM1`..`LPT9`, `CLOCK$` -- devices in EVERY directory on DOS, and
        the extension does not save you), `caps.trailing`, `caps.maxcomp` and
        `caps.shortnames`; `P.folder_max` caps the length at 8 on a volume
        that aliases and at `min(maxcomp, 32)` on one that does not. None of
        it is written out again here.
        """
        caps = self._caps(guest)
        base = self.config.sync_dir
        home = self.guest_home(guest)
        taken = self.taken_names(guest)

        # TWO STEMS, IN ORDER OF PREFERENCE.
        #
        # The title first: a folder the user recognises is the whole point of
        # naming one after the conversation.
        #
        # The conversation id second, because a title is free text and real
        # ones reduce to nothing a FAT volume will hold -- "???", "....", a
        # line of Cyrillic on a cp1252 machine, or `CON`, which is a device
        # and not a name. Before this, every one of those produced NO folder
        # at all, and an agent with no destination is the agent that wrote
        # into C:\.
        stems = []
        from_title = P.title_folder(source, caps)
        if from_title:
            stems.append(from_title)
        from_id = P.folder_name(
            (guest.conv_id or "").replace("-", "")[:P.folder_max(caps)], caps)
        if from_id and from_id not in stems:
            stems.append(from_id)

        # A COLLISION IS RESOLVED BY A NEW NAME, never by sharing the folder.
        # The tail REPLACES the end of the stem rather than extending it, so
        # the result still fits the component limit -- on an 8.3 volume
        # `MEETINGN` + `2` would be nine characters and the volume would make
        # its own name up.
        limit = P.folder_max(caps)
        for stem in stems:
            for n in range(1, 100):
                tail = "" if n == 1 else str(n)
                name = stem[: limit - len(tail)] + tail
                # THE WHOLE PATH, not just the component. `P.check` re-runs
                # the component rules (the tail changed the name, and a stem
                # that was fine can be truncated into one that is not) and
                # adds the two this could not see: `caps.maxpath` less
                # `caps.dirreserve` once the declared work root is in front of
                # it -- twelve characters short on Win32, so an 8.3 name still
                # fits INSIDE the folder -- and whether the name survives the
                # machine's codepage at all. A folder the client cannot create
                # leaves the agent back where it started.
                if P.check(name, guest_root=home, caps=caps,
                           is_dir=True) is not None:
                    continue
                if self._fold(guest, name) in taken:
                    continue
                if base and os.path.exists(os.path.join(base, name)):
                    continue
                return name
        return ""

    def planned_guest_dir(self, guest: GuestSession) -> str:
        r"""Where this conversation's folder on the machine WILL be, or \"\".

        The system prompt is built when the session is, and the folder is
        committed at the first tool call -- the name comes from the
        conversation's title and there is no title until the user has spoken.
        So the prompt cannot name a folder that exists.

        It can name the one that is coming, and with no mirror that is what
        the agent actually needs: every file reaching the machine is a
        `put_file` to a path the agent types, and an agent with no path types
        `C:\`. The chosen name is remembered on the guest, so the folder that
        gets made is the folder the prompt promised.
        """
        if guest.work_guest:
            return guest.work_guest
        if not self.guest_path(guest, "x"):
            return ""           # the machine declared no home; see below
        if not guest.work_planned:
            name = self.choose_name(guest, self.title_source(guest))
            if not name:
                return ""
            guest.work_planned = name
        return self.guest_path(guest, guest.work_planned)

    async def name_work_dir(self, guest: GuestSession) -> None:
        """Give this conversation its folder. See the note above."""
        if guest.work_guest or not guest.conv_id:
            return
        if not self.guest_path(guest, "x"):
            # THE MACHINE DECLARED NO HOME, and nothing is invented. A path
            # chosen by this side would be a directory on somebody's real
            # disk that nobody asked for, which is the same mistake as the
            # drive root one step tidier. The agent is told plainly instead
            # (machine.toml `no_sync_undeclared` / `no_sync_where_unknown`).
            log.info(
                "no work directory for %s: the machine declared no home "
                "folder, so there is nowhere on it that was agreed",
                guest.conv_id)
            return

        name = guest.work_planned or self.choose_name(
            guest, self.title_source(guest))
        if not name:
            log.warning(
                "no work directory for %s: every name this title and this "
                "conversation id can produce is already owned by another "
                "conversation", guest.conv_id)
            return
        guest.work_planned = name

        # THE MIRROR HALF, and the only half that needs `--sync-dir`. When it
        # fails there is no tree to pair with and sharing stays off, which is
        # the state this function already had a name for.
        base = self.config.sync_dir
        path = ""
        seeded = False
        if base:
            path = os.path.join(base, name)
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                log.exception("could not create %s", path)
                return
            guest.work_dir = path
            seeded = self.seed_ignores(path)

        # THE MACHINE HALF, unconditional. `send_options` (the caller) puts
        # this in the OPTION `work` row of the next batch, and the client's
        # OPTIONSEND handler hands it to `rc_workdir_apply`, which creates
        # it parents and all and makes it that side's live work root.
        guest.work_guest = self.guest_path(guest, name)
        if self.conv is not None:
            self.conv.set_folder(guest.conv_id, name, guest.work_guest)
        self.retarget_perms(guest)
        log.info(
            "work directory for %s: %s (%s)",
            guest.conv_id,
            path or "(nothing on this side -- no --sync-dir)",
            guest.work_guest,
        )
        if self.write_block:
            text = ("This conversation works in " + guest.work_guest
                    + " on your machine.")
            if seeded:
                text += (" What stays behind is listed in .gastonignore"
                         " there; Work > Edit ignore list opens it.")
            await self.write_block(guest, H.notice(text))
        if not path:
            # No notice for the agent here: `with_sync_preamble` only runs
            # with a mirror, and it would reach the agent one turn late
            # anyway. The destination is in the system prompt from the start
            # -- see `planned_guest_dir`.
            return
        note = (
            f"This conversation's folder now exists: write in {path} "
            f"here, which is mirrored to {guest.work_guest} on the "
            f"user's machine. Anything written outside it stays on this proxy."
        )
        if note not in guest.synccoord.agent_notices:
            guest.synccoord.agent_notices.append(note)

    async def on_setwork(self, guest: GuestSession, f: Frame) -> None:
        """The user picked a folder on their machine for this conversation."""
        path = (f.headers.get("path") or "").strip()
        if not path:
            return

        name = P.folder_name(
            path.replace("\\", "/").rsplit("/", 1)[-1], guest.caps
        )
        if not name:
            name = (
                P.folder_name(guest.conv_id[:8], guest.caps)
                if guest.conv_id
                else "WORK"
            )
        # The mirror half again, and the same split as `name_work_dir`: with
        # no `--sync-dir` the folder the user picked is still their working
        # directory ON THEIR MACHINE, it just has nothing on this side pairing
        # with it. Refusing the pick outright -- which this did until
        # 2026-10-01 -- left the conversation with no destination at all and
        # told the user their own choice was unusable.
        base = self.config.sync_dir
        local = ""
        seeded = False
        if base:
            local = os.path.join(base, name)
            try:
                os.makedirs(local, exist_ok=True)
            except OSError as exc:
                log.exception("could not create %s", local)
                if self.write_block:
                    await self.write_block(
                        guest,
                        H.error(
                            "Could not create the folder on this side: "
                            + str(exc)[:160]
                        ),
                    )
                return
            guest.work_dir = local
            seeded = self.seed_ignores(local)

        guest.work_guest = path
        guest.work_planned = name
        if guest.conv_id and self.conv is not None:
            self.conv.set_folder(guest.conv_id, name, path)
        self.retarget_perms(guest)
        log.info(
            "work directory for %s: %s (guest: %s)",
            guest.conv_id or "<initial>",
            local,
            path,
        )
        if self.write_block:
            text = "This conversation works in " + path + " on your machine."
            if seeded:
                text += (" What stays behind is listed in .gastonignore"
                         " there; Work > Edit ignore list opens it.")
            await self.write_block(guest, H.notice(text))

        if not guest.conv_id and guest.claude:
            with contextlib.suppress(Exception):
                await guest.claude.close()
            guest.claude = None

        # BOTH NOTES NAME `local`, so both need one. With no mirror there is
        # nothing on this side to name and no channel to say it on --
        # `with_sync_preamble` only runs with a `sync_dir` -- so what the
        # agent knows comes from the system prompt, which reads `work_guest`
        # the next time a session is built.
        if local and guest.conv_id:
            note = (
                f"THE WORK DIRECTORY HAS CHANGED. This conversation now "
                f"works in {local} here, mirrored to {path} on the user's "
                f"machine. The folder you were using before is no longer "
                f"mirrored -- anything written there from now on stays on "
                f"this proxy, however well the write appears to succeed."
            )
            if f.headers.get("staged") == "merge":
                note += (
                    f"\n\nThe user asked YOU to merge the files they brought "
                    f"across. Copies whose names clashed are in "
                    f"{os.path.join(local, 'Gaston-Incoming')}; the versions that "
                    f"were already here are in place beside it. Reconcile them "
                    f"file by file, say what you changed, and ask before "
                    f"discarding either side. Do not delete anything in "
                    f"Gaston-Incoming until the user agrees the merge is right."
                )
            if note not in guest.synccoord.agent_notices:
                guest.synccoord.agent_notices.append(note)
        elif local and f.headers.get("staged") == "merge":
            note = (
                f"The user asked YOU to merge the files they brought "
                f"across into {path}. Copies whose names clashed are in "
                f"{os.path.join(local, 'Gaston-Incoming')}; the versions that "
                f"were already here are in place beside it. Reconcile them "
                f"file by file, say what you changed, and ask before "
                f"discarding either side. Do not delete anything in "
                f"Gaston-Incoming until the user agrees the merge is right."
            )
            if note not in guest.synccoord.agent_notices:
                guest.synccoord.agent_notices.append(note)

        if self.send_options:
            await self.send_options(guest)
        if self.config.sync_dir and self.spawn_sync:
            self.spawn_sync("the work directory was set", guest)

