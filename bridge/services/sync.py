"""SyncService: file synchronization between the host and the guest machine.

Manages sync passes (Pass), status override, baseline persistence, latching/halts
on conflict, conflict resolution, stray edits detection, and SYNC* frame routing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from ..files import manifest
from ..files import paths as P
from ..files.sync import AckResult, Baseline, GetResult, ScanResult
from ..files.syncrun import Pass, SyncError
from ..files.synctransport import SyncTransport
from ..status import SyncStatus
from ..view import html as H
from ..files import caps as C
from ..wire.protocol import Frame, halt, haltend, status_frame

if TYPE_CHECKING:
    from ..server import GuestSession
    from .config import BridgeConfig

log = logging.getLogger("bridge.sync")

TRANSIENT_ABORT_GRACE = 3
STRAY_SCAN_BUDGET = 4000


class SyncService:
    """Manages host-guest file synchronization and conflict reconciliation."""

    TRANSIENT_ABORT_GRACE = TRANSIENT_ABORT_GRACE
    STRAY_SCAN_BUDGET = STRAY_SCAN_BUDGET

    def __init__(
        self,
        config: BridgeConfig,
        connection: Any,
        workspace: Any,
        presentation: Any = None,
        *,
        spawn: Any = None,
        base_paths: dict | None = None,
    ) -> None:
        self.config = config
        self.connection = connection
        self.workspace = workspace
        self.presentation = presentation
        self.spawn = spawn or asyncio.create_task
        self.base_paths = {} if base_paths is None else base_paths

    def _get_guest(self, guest: GuestSession | None) -> GuestSession | None:
        if guest is not None:
            return guest
        return getattr(self.connection, "guest", None) or getattr(
            self.connection, "_guest", None
        )

    def _mirror_dir(self, guest: GuestSession | None) -> str:
        if self.workspace is not None:
            return self.workspace.mirror_dir(guest)
        if guest is not None and getattr(guest, "work_dir", None):
            return guest.work_dir
        if hasattr(self.connection, "_mirror_dir"):
            return self.connection._mirror_dir(guest)
        return ""

    async def set_status_override(
        self, guest: GuestSession, text: str
    ) -> None:
        """Send a STATUS frame to the guest."""
        try:
            await self.connection.send(guest, status_frame(text))
        except Exception as exc:
            log.debug("could not send status frame: %s", exc)

    async def ensure_sync_flush(
        self, guest: GuestSession | None = None, why: str = "mcp command"
    ) -> dict:
        """Run sync and block until finished, displaying 'waiting for sync...' status on the guest."""
        g = self._get_guest(guest)
        if g is None or not g.connected or not self.config.sync_dir:
            return {}
        if not self._mirror_dir(g):
            return {}

        run_fn = getattr(self.connection, "run_sync", self.run_sync)
        await self.set_status_override(g, "waiting for sync...")
        try:
            return await run_fn(why, wait=True, guest=g)
        finally:
            await self.set_status_override(g, "")

    def current_guest_root(self, guest: GuestSession | None = None) -> str:
        """What guest is mirroring right now, or \"\" while mirroring nothing."""
        g = self._get_guest(guest)
        if g is not None and getattr(g, "work_guest", None):
            return g.work_guest
        return ""

    def base_path(self, guest: GuestSession | None = None) -> str:
        """The baseline file for the root guest is currently mirroring."""
        state_dir = self.config.sync_state
        if not state_dir and self.config.sync_dir:
            state_dir = os.path.abspath(
                os.path.join(self.config.sync_dir, os.pardir, ".gaston-state")
            )
        if not state_dir:
            return ""
        legacy = os.path.join(state_dir, "base.json")
        root = self.current_guest_root(guest)
        if not root:
            return legacy
        hit = self.base_paths.get(root)
        if hit:
            return hit

        recorded = None
        try:
            with open(legacy, encoding="utf-8") as fh:
                recorded = json.load(fh).get("guest_root", "")
        except (OSError, ValueError):
            recorded = None

        if recorded is None or recorded == "" or recorded == root:
            path = legacy
        else:
            import hashlib

            slug = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
            path = os.path.join(
                state_dir, "base-%s.json" % slug[:10]
            )
            log.info(
                "baseline for %s is %s (%s belongs to %s)",
                root,
                os.path.basename(path),
                "base.json",
                recorded,
            )
        self.base_paths[root] = path
        return path

    def load_baseline(self, guest: GuestSession | None = None) -> Baseline:
        """Load the baseline for the current root."""
        return Baseline.load(self.base_path(guest))

    def baseline_lock(self, guest: GuestSession | None = None):
        """The lock that serialises writers to base.json."""
        g = self._get_guest(guest)
        return g.sync_lock if g is not None else contextlib.nullcontext()

    async def raise_halt(
        self, guest: GuestSession, line: str, paths: Any = ()
    ) -> None:
        """Put the Continue bar up. Idempotent while one is already showing."""
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if sc is None:
            return
        hid = sc.raise_halt(line, paths)
        if hid is None:
            return
        await self.connection.send(guest, halt(hid, line))

    async def clear_halt(self, guest: GuestSession) -> None:
        """Take the Continue bar down because the reason went away."""
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if sc is None:
            return
        hid = sc.clear_halt()
        if hid is None:
            return
        await self.connection.send(guest, haltend(hid))

    async def reconcile_halt(
        self, guest: GuestSession, base: Any = None
    ) -> None:
        """Ensure the halt bar matches base.latched."""
        if not self.config.sync_dir:
            return
        if base is None:
            try:
                base = self.load_baseline(guest)
            except Exception:
                return
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if sc is None:
            return
        if base.latched and not sc.halt_id:
            n = len(base.latched)
            await self.raise_halt(
                guest,
                f"Stopped: {n} conflicting edit{'' if n == 1 else 's'}. "
                f"Your copy was kept.",
                paths=base.latched.keys(),
            )
        elif not base.latched and sc.halt_id:
            await self.clear_halt(guest)

    async def report_abort(
        self, guest: GuestSession, reason: str, *, bug: bool = False
    ) -> None:
        """Say ONCE that a whole pass refused to run."""
        log.warning("sync pass aborted: %s", reason)
        state = self.base_path(guest)
        agent_text = (
            f"SYNC HAS STOPPED -- {reason} No file is being copied in either "
            f"direction until this is resolved. Do not tell the user a file "
            f"has reached their machine; it has not."
        )
        try:
            base = self.load_baseline(guest)
        except Exception:
            base = None
        if base is not None:
            if reason in base.announced:
                base.aborted = reason
                return
            base.aborted = reason
            base.announced.append(reason)
            base.pending_notice_agent.append(agent_text)
            try:
                base.save(state)
            except Exception as exc:
                log.warning("could not record the abort: %s", exc)

        what = (
            "File synchronisation stopped because of a fault in the "
            "bridge, not because of anything on your machine."
            if bug
            else "File synchronisation has stopped and is not copying "
            "anything in either direction."
        )
        text = (
            f"{what}\n\n{reason}\n\nNothing has been changed on either "
            f"side. Files edited here and there will both be kept, and "
            f"will reconcile once this is resolved."
        )
        if self.presentation:
            await self.presentation.write_block(guest, H.notice(text))
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if sc is not None and agent_text not in sc.agent_notices:
            sc.agent_notices.append(agent_text)

    async def run_sync(
        self,
        why: str,
        *,
        wait: bool = False,
        guest: GuestSession | None = None,
    ) -> dict:
        """One pass, serialised against every other trigger."""
        g = self._get_guest(guest)
        if g is None or not g.connected or not self.config.sync_dir:
            return {}
        mirror = self._mirror_dir(g)
        if not mirror:
            log.debug("nothing mirrored yet; skipping sync (%s)", why)
            return {}
        if not wait and g.sync_lock.locked():
            log.debug("sync already running; coalescing (%s)", why)
            return {}

        async with g.sync_lock:
            g.sync_active = True
            try:
                transport = SyncTransport(g)
                p = Pass(
                    transport,
                    g.caps,
                    mirror,
                    self.base_path(g),
                )
                got = await p.run()
                g.guest_sync_root = p.guest_root
                if hasattr(self.connection, "guest_sync_root"):
                    self.connection.guest_sync_root = p.guest_root
                g.last_sync_reports = list(got.get("reports", []))
                if hasattr(self.connection, "_last_sync_reports"):
                    self.connection._last_sync_reports = g.last_sync_reports

                log.info(
                    "sync (%s): %s",
                    why,
                    {
                        k: v
                        for k, v in got.items()
                        if k not in ("reports", "notices")
                    },
                )
                for line in got.get("new_reports", []):
                    log.warning("sync: %s", line)
                delivered = []
                for note in got.get("notices", []):
                    if self.presentation:
                        await self.presentation.write_block(g, H.notice(note))
                    delivered.append(note)

                sc = getattr(g, "synccoord", None) or getattr(
                    self.connection, "synccoord", None
                )
                if sc is not None:
                    for note in got.get("agent_notices", []):
                        if note not in sc.agent_notices:
                            sc.agent_notices.append(note)

                if got.get("new_conflicts"):
                    n = got["new_conflicts"]
                    await self.raise_halt(
                        g,
                        f"Stopped: {n} conflicting edit"
                        f"{'' if n == 1 else 's'}. Your copy was kept.",
                        paths=got.get("conflict_paths", ()),
                    )

                await self.reconcile_halt(g, base=p.base)
                p.notices_delivered(delivered)
                setattr(g, "transient_aborts", 0)
                if hasattr(self.connection, "_transient_aborts"):
                    self.connection._transient_aborts = 0
                return got
            except SyncError as exc:
                transient_aborts = getattr(g, "transient_aborts", 0)
                if getattr(exc, "transient", False):
                    transient_aborts += 1
                    setattr(g, "transient_aborts", transient_aborts)
                    if hasattr(self.connection, "_transient_aborts"):
                        self.connection._transient_aborts = transient_aborts
                    if transient_aborts <= self.TRANSIENT_ABORT_GRACE:
                        log.info(
                            "sync: %s (retrying; %d of %d)",
                            exc,
                            transient_aborts,
                            self.TRANSIENT_ABORT_GRACE,
                        )
                        return {"retry": str(exc)}
                await self.report_abort(g, str(exc))
                return {"error": str(exc)}
            except Exception as exc:
                log.exception("sync pass FAILED (not a refusal): %s", exc)
                await self.report_abort(
                    g,
                    f"an internal error in the sync layer: {exc}",
                    bug=True,
                )
                return {"error": str(exc)}
            finally:
                g.sync_active = False

    async def sync_resolve(
        self,
        path: str,
        action: str,
        guest: GuestSession | None = None,
    ) -> str:
        """Lift one halt. Recorded in the baseline BEFORE it takes effect."""
        if not self.config.sync_dir:
            return "File synchronisation is not switched on for this session."
        async with self.baseline_lock(guest):
            return self.sync_resolve_locked(path, action, guest)

    def sync_resolve_locked(
        self,
        path: str,
        action: str,
        guest: GuestSession | None = None,
    ) -> str:
        """Execute resolution against the locked baseline."""
        g = self._get_guest(guest)
        try:
            base = self.load_baseline(g)
        except Exception as exc:
            return f"The baseline could not be read ({exc}); nothing changed."

        if action == "accept-tree":
            base.accept_tree = True
            base.save(self.base_path(g))
            log.info("sync: whole-tree revert accepted by the agent")
            return (
                "The next pass will go ahead without the whole-tree "
                "check. Its rows are unchanged, so the machine's version "
                "of each path wins where it moved -- which is what was "
                "being refused. Nothing has moved yet."
            )

        caps = (
            g.caps
            if g is not None and g.caps is not None
            else C.from_hello({})
        )
        key = P.canonical(path.replace("\\", "/").strip("/"), caps=caps)
        state = self.base_path(g)

        if action == "release":
            if key not in base.latched:
                return (
                    f"{path} is not withheld. Nothing to release; if a "
                    f"write is not arriving, call sync_status -- it is "
                    f"more likely a skipped name than a conflict."
                )
            why = base.latched.pop(key)
            base.save(state)
            log.info("sync: %s released by the agent (was: %s)", key, why)
            if not base.latched and g is not None:
                self.spawn(self.clear_halt(g))
            return (
                f"Released {path}. Your next write to it will be copied to "
                f"the user's machine on the next pass. If they change it "
                f"again in the meantime, it will conflict again -- which "
                f"is correct, not a failure."
            )

        if action == "accept-revert":
            prev = base.prev.pop(key, None)
            was_latched = base.latched.pop(key, None) is not None
            if prev is None and not was_latched:
                return (
                    f"{path} is not being held as a reverted push. "
                    f"Nothing changed -- call sync_status to see what is "
                    f"actually withheld."
                )
            base.save(state)
            log.info("sync: %s revert accepted by the agent", key)
            if not base.latched and g is not None:
                self.spawn(self.clear_halt(g))
            return (
                f"Accepted the machine's version of {path}. The next pass "
                f"will copy it here, replacing what was written. The "
                f"previous content is not kept -- if it is still wanted, "
                f"read the file after the pull and re-apply the change."
            )

        if action != "forget":
            return (
                f"Unknown action {action!r}; use release, accept-revert "
                f"or forget."
            )

        removed = (
            base.tombs.pop(key, None) is not None
            or base.rows.pop(key, None) is not None
        )
        base.latched.pop(key, None)
        base.save(state)
        if not removed:
            return f"{path} was not tracked; nothing to forget."
        log.info("sync: %s forgotten by the agent", key)
        return (
            f"Stopped tracking {path}. If it reappears on either side it "
            f"will be treated as new."
        )

    async def release_all_latches(
        self, only: Any = None, guest: GuestSession | None = None
    ) -> int:
        """Lift the write refusals the user has just answered."""
        if not self.config.sync_dir:
            return 0
        g = self._get_guest(guest)
        async with self.baseline_lock(g):
            try:
                base = self.load_baseline(g)
            except Exception as exc:
                log.warning("could not release latches: %s", exc)
                return 0
            answered = set(only) if only is not None else set(base.latched)
            n = 0
            for key in answered:
                if base.latched.pop(key, None) is not None:
                    n += 1
            if n:
                base.save(self.base_path(g))
            left = len(base.latched)
            if left:
                log.info(
                    "sync: %d path(s) latched after the release; they "
                    "were raised after the bar went up and will raise "
                    "their own",
                    left,
                )
            return n

    async def sync_frame(self, guest: GuestSession, f: Frame) -> None:
        """Route one SYNC* reply to whoever is waiting for it."""
        sid = f.headers.get("id", "")
        entry = guest.sync_pending.get(sid)
        if entry is None:
            log.debug("sync frame %s for %s, which nobody awaits", f.type, sid)
            return

        if f.type == "SYNCENT":
            if entry.kind != "scan":
                log.warning(
                    "SYNCENT for %s, which is a %s -- ignoring",
                    sid,
                    entry.kind,
                )
                return
            rows, display = manifest.parse_rows(f.body, guest.caps)
            entry.rows.update(rows)
            entry.display.update(display)

        elif f.type == "SYNCEND":
            fut = entry.future
            if not fut.done():
                fut.set_result(
                    ScanResult(
                        status=SyncStatus.parse(f.headers.get("status", "ok")),
                        files=int(f.headers.get("files", "0")),
                        tree_id=f.headers.get("tree_id", ""),
                        root=f.headers.get("root", ""),
                        rows=entry.rows,
                        display=entry.display,
                    )
                )

        elif f.type in ("SYNCPUTACK", "SYNCMKDIRACK"):
            fut = entry.future
            if not fut.done():
                fut.set_result(
                    AckResult(
                        status=SyncStatus.parse(f.headers.get("status", "ok")),
                        crc=int(f.headers.get("crc", "0")),
                    )
                )

        elif f.type == "SYNCDAT":
            if entry.kind != "get":
                log.warning(
                    "SYNCDAT for %s, which is a %s -- ignoring",
                    sid,
                    entry.kind,
                )
                return
            entry.data.extend(f.body)

        elif f.type == "SYNCDATEND":
            fut = entry.future
            if not fut.done():
                fut.set_result(
                    GetResult(
                        status=SyncStatus.parse(f.headers.get("status", "ok")),
                        crc=int(f.headers.get("crc", "0")),
                        cls=f.headers.get("cls", P.CLASS_TEXT),
                        data=bytes(entry.data or b""),
                    )
                )

    def fail_syncs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight sync transfer, synchronously."""
        if guest is None:
            return
        for sid, entry in list(guest.sync_pending.items()):
            fut = entry.future
            if not fut.done():
                fut.set_exception(SyncError(why))
        guest.sync_pending.clear()
        guest.sync_active = False

    async def sweep_sync(self) -> None:
        """The backstop trigger for periodic sync passes."""
        while True:
            await asyncio.sleep(self.config.sync_sweep_s)
            guest = self._get_guest(None)
            if self.config.sync_dir and guest is not None:
                try:
                    await self.run_sync("the sweep timer expired")
                except Exception:
                    pass

    def edits_outside_the_mirror(
        self, guest: GuestSession | None = None, limit: int = 20
    ) -> tuple[list[str], bool]:
        """Files under cwd but outside the sync root, newest first."""
        g = self._get_guest(guest)
        cwd = self.config.cwd or os.getcwd()
        mirror_dir = (
            self._mirror_dir(g) if g else ""
        ) or self.config.sync_dir
        if not mirror_dir or os.path.realpath(cwd) == os.path.realpath(
            mirror_dir
        ):
            return [], False
        mirror = os.path.realpath(mirror_dir)
        budget = getattr(
            self.connection,
            "STRAY_SCAN_BUDGET",
            getattr(type(self), "STRAY_SCAN_BUDGET", self.STRAY_SCAN_BUDGET),
        )
        found: list[tuple[float, str]] = []
        seen = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(cwd):
            real = os.path.realpath(dirpath)
            if real == mirror or real.startswith(mirror + os.sep):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d != "__pycache__"
            ]
            for name in filenames:
                seen += 1
                if seen > budget:
                    truncated = True
                    break
                full = os.path.join(dirpath, name)
                try:
                    found.append(
                        (os.stat(full).st_mtime, os.path.relpath(full, cwd))
                    )
                except OSError:
                    continue
            if truncated:
                break
        found.sort(reverse=True)
        return [p for _, p in found[:limit]], truncated

    def sync_status_text(self, guest: GuestSession | None = None) -> str:
        """Everything the agent cannot otherwise find out about sync state."""
        if not self.config.sync_dir:
            return (
                "File synchronisation is not switched on for this session. "
                "Nothing you write here reaches the user's machine, and "
                "there is no tool that would copy it."
            )
        g = self._get_guest(guest)
        mirror = self._mirror_dir(g) if g is not None else ""
        if not mirror:
            return (
                "This conversation does not have a folder on the user's "
                "machine yet, so nothing is being copied in either "
                "direction. That is the normal state of a conversation "
                "that has not used a tool -- it is not a fault and there "
                "is nothing to diagnose. A folder is created the moment "
                "you use one, and you will be told its path then."
            )
        try:
            base = self.load_baseline(g)
        except Exception as exc:
            return (
                f"The synchronisation baseline could not be read ({exc}). "
                f"No pass will run until that is fixed; tell the user."
            )

        guest_sync_root = getattr(g, "guest_sync_root", None) or getattr(
            self.connection, "guest_sync_root", None
        )
        work_guest = getattr(g, "work_guest", None)
        out = [
            f"Mirrored: {mirror} here <-> "
            f"{work_guest or guest_sync_root or '(not reported yet)'}"
            f" on the machine.",
            f"Last pass: generation {base.generation}."
            + (f"  Bound to tree {base.tree_id}." if base.tree_id else ""),
        ]

        if base.aborted:
            out.append("")
            out.append(
                "SYNCHRONISATION IS STOPPED. No file is being copied "
                "in either direction, and nothing below this line has "
                "moved since it stopped."
            )
            out.append(f"  Reason: {base.aborted}")
            out.append(
                "  Tell the user. Do not report any file as having "
                "reached their machine while this is the case."
            )

        try:
            from ..files import attrs as A

            at = A.load(mirror)
        except Exception:
            at = None
        if at is not None and len(at):
            where = (
                ", ".join(sorted(os.path.relpath(f, mirror) for f in at.sources))
                or "?"
            )
            out.append(
                f"\nLine-ending and text rules: {len(at)} in force, from "
                f"{where}. `-text`/`binary` means the two sides hold identical "
                f"bytes and nothing is converted; `eol=lf` or `eol=crlf` sets "
                f"what lands on the machine for that path. These are the "
                f"tree's own instructions -- do not override them to make a "
                f"file look tidier."
            )

        if base.latched:
            out.append(
                "\nREFUSING YOUR WRITES -- these conflicted and the "
                "user's copy won. Your version is preserved under "
                f"{mirror.rstrip('/')}/../.gaston/conflict/ with "
                "the common ancestor beside it. Read it, merge, ask the "
                "user whether they have finished with the file, and "
                "only then call sync_resolve:"
            )
            for k, why in sorted(base.latched.items()):
                out.append(f"  {k} -- {why}")

        reports = getattr(g, "last_sync_reports", None) or getattr(
            self.connection, "_last_sync_reports", None
        )
        if reports:
            out.append(
                "\nFrom the last pass (skips are permanent until the "
                "name or the path changes -- a skipped file NEVER "
                "reaches the user, however many times you write it):"
            )
            for line in reports:
                out.append(f"  {line}")

        if base.tombs:
            out.append(
                f"\nDeleted on both sides and not tracked: "
                f"{len(base.tombs)} path(s)."
            )
        if base.ignored:
            out.append(
                f"Out of scope (.gitignore/.gastonignore): "
                f"{len(base.ignored)} path(s), frozen at their last "
                f"agreed content."
            )

        stray, stray_truncated = self.edits_outside_the_mirror(g)
        if stray:
            out.append(
                "\nEDITED HERE BUT OUTSIDE THE MIRRORED FOLDER, so "
                "these have not reached the user and never will where "
                "they are:"
            )
            out.extend(f"  {p}" for p in stray)
            if stray_truncated:
                budget = getattr(
                    self.connection,
                    "STRAY_SCAN_BUDGET",
                    getattr(type(self), "STRAY_SCAN_BUDGET", self.STRAY_SCAN_BUDGET),
                )
                out.append(
                    f"  (stopped after {budget} files "
                    f"outside the mirror; there may be more)"
                )

        if len(out) == 2:
            out.append(
                "\nNothing outstanding: no conflicts, no skips, "
                "nothing withheld."
            )
        return "\n".join(out)

    # Private method aliases for bridge facade
    _run_sync = run_sync
    _ensure_sync_flush = ensure_sync_flush
    _sync_resolve_locked = sync_resolve_locked
    _base_path = base_path
    _current_guest_root = current_guest_root
    _load_baseline = load_baseline
    _baseline_lock = baseline_lock
    _release_all_latches = release_all_latches
    _raise_halt = raise_halt
    _clear_halt = clear_halt
    _reconcile_halt = reconcile_halt
    _sync_frame = sync_frame
    _fail_syncs = fail_syncs
    _sweep_sync = sweep_sync
    _set_status_override = set_status_override
    _report_abort = report_abort
    _edits_outside_the_mirror = edits_outside_the_mirror
