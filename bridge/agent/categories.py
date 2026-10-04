"""What a tool call touches, in the categories an execution mode decides on.

THE ONE ANALYSIS. A mode (bridge/agent/policy.py) says Off, Ask, Check or
Allow per category; this says which categories a call lands in and the rule
that put it there, so the bar, the checker and the transcript all explain the
same finding. `cmdguard` judges whether a command reaches outside the mirror;
this goes further and says where -- the work folder, somewhere else, or a
place the client declared protected -- and what kind of act it is.

A CALL CAN LAND IN SEVERAL CATEGORIES and the strictest value wins.
`copy a.c C:\\WINDOWS\\X` is a read and a system write; `delete permanent=true`
of a protected file is both a permanent delete and a system change. Taking
the strictest is what makes adding a category safe: it can only ever raise
what a call needs.

DOUBT IS NOT A CATEGORY, IT IS A FLAG ON ONE. A path this cannot resolve --
`%WINDIR%`, `...\\x`, a quote that never closes -- may be inside the work
folder or may be the boot sector, so it takes the stricter of "elsewhere" and
"system" (`policy.value_of`). Saying either one would be a guess stated as a
finding, which is the thing cmdguard's own note forbids.

NO OS FACTS LIVE HERE (CLAUDE.md). Protected places, trusted programs and
dangerous programs come from the client (`Lists`); how a command line is
spelled comes from the shell's profile through `cmdguard.Grammar`. This file
owns the judgement only.

PURE, AND IT ASKS FOR WHAT IT CANNOT SEE. Whether `wcl386` means the compiler
or a `WCL386.BAT` the agent wrote into the work folder depends on what is in
the folder, and what a `.bat` does depends on its lines. `classify` never
does I/O: it returns `wants` -- folders to list, files to read -- and the desk
fetches them from the guest and asks again with `Ctx.listings`/`Ctx.files`
filled in. From the guest, not the mirror: a file an `exec` just wrote is on
the machine before it is in the mirror, and judging the mirror's older copy
would judge a file that is not the one about to run.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..files.paths import glob_match
from . import cmdguard, profiles, prompts

# --- the categories, in the order the dialog shows them ---------------------

FILES_WORK = "files_work"
FILES_ELSE = "files_else"
FILES_SYS = "files_sys"
RUN_WORK = "run_work"
RUN_BUILT = "run_built"
RUN_ELSE = "run_else"
RUN_SYS = "run_sys"
READ = "read"
PURGE = "purge"
SCREEN_SEE = "screen_see"
SCREEN_DRIVE = "screen_drive"
BRIDGE_SHELL = "bridge_shell"
WEB = "web"

CATS = (FILES_WORK, FILES_ELSE, FILES_SYS, RUN_WORK, RUN_BUILT, RUN_ELSE,
        RUN_SYS, READ, PURGE, SCREEN_SEE, SCREEN_DRIVE, BRIDGE_SHELL, WEB)

#: Doubt's two candidates, per family.
DOUBT_OF = {FILES_ELSE: FILES_SYS, RUN_ELSE: RUN_SYS}

#: How deep a script may `call` another before the rest is doubt.
MAX_DEPTH = 3

#: What a script or response file may be before it is not read at all (and
#: is doubt). A build script is a few hundred bytes.
MAX_SCRIPT = 64 * 1024

# Tool families. Bare verbs; the `mcp__machine__` prefix is added below.
_MACHINE = "mcp__machine__"
_READS = ("get_file", "list_dir")
_STATE = ("drives", "ps")
_EXEMPT = ("toolnote", "sync_status")
_WRITES = ("put_file", "mkdir")
_BRIDGE_EDITS = ("Write", "Edit", "NotebookEdit")
_BRIDGE_READS = ("Read", "Glob", "Grep", "LS")
_WEB = ("WebFetch", "WebSearch")
PLAN_EXIT = "ExitPlanMode"


@dataclass(frozen=True)
class Hit:
    """One finding: this call lands in `cat`, because of `rule`.

    `source` names where the rule came from, so the person can find it:
    "client" for a list the machine declared, "shell:<name>" for the shell
    profile, "" for a judgement this file made from the call itself.
    `doubt` means the path could not be resolved -- see the module note.
    """

    cat: str
    rule: str
    source: str = ""
    doubt: bool = False
    #: A thing a per-chat grant may name: the exact program for RUN_BUILT,
    #: the window's process for SCREEN_DRIVE. Empty: the category is enough.
    subject: str = ""


@dataclass
class Verdict:
    hits: list = field(default_factory=list)
    #: (kind, guest path, key) the desk must fetch before this is final: "ls" a
    #: folder, "get" a file. Empty means the verdict is complete.
    wants: list = field(default_factory=list)
    #: Outside the matrix entirely -- bridge-local reads that touch nothing
    #: of the person's.
    exempt: bool = False
    #: The plan-approval bar. Always asks, whatever the mode says.
    plan_exit: bool = False
    #: Refused whatever the mode says: an invariant, not a permission.
    refuse: str = ""
    #: guest path -> sha1 of the script text this verdict judged.
    scripts: dict = field(default_factory=dict)

    @property
    def cats(self) -> set:
        return {h.cat for h in self.hits}

    @property
    def doubtful(self) -> bool:
        return any(h.doubt for h in self.hits)

    def add(self, cat: str, rule: str, source: str = "", *,
            doubt: bool = False, subject: str = "") -> None:
        h = Hit(cat, rule, source, doubt, subject)
        if h not in self.hits:
            self.hits.append(h)

    def want(self, kind: str, path: str, key: str = "") -> None:
        """Ask the desk for `path`. `key` is how `classify` will look the
        answer up -- its own normal form -- so `fill` needs no grammar."""
        item = (kind, path, key or path)
        if item not in self.wants:
            self.wants.append(item)


@dataclass(frozen=True)
class Lists:
    """What the client declared. All of it is the client's (CLAUDE.md).

    `protect` are path globs in the machine's own spelling, already
    expanded and with their short aliases, matched by `paths.glob_match`.
    `danger` maps a program's stem to why it is dangerous; `trust` is the
    programs a mode may run without a second look.
    """

    protect: tuple = ()
    danger: dict = field(default_factory=dict)
    trust: frozenset = frozenset()


@dataclass
class Ctx:
    caps: object = None
    #: The machine's shells, the default first (`toolrules.shells_of`).
    shells: tuple = ()
    #: This conversation's folder on the machine; "" when none is committed.
    work_root: str = ""
    #: Its copy on this side, for the bridge's own Write/Edit; "" if none.
    bridge_dir: str = ""
    lists: Lists = field(default_factory=Lists)
    #: The tools' own resolver (GuestSession.on_guest): a path as the agent
    #: wrote it, made absolute the way the tool will make it. None: absent.
    resolve: Callable | None = None
    #: Fetched for `wants`: normalised guest path -> bytes, None if it could
    #: not be read; normalised folder -> frozenset of upper-cased names.
    files: dict = field(default_factory=dict)
    listings: dict = field(default_factory=dict)


def _t(key: str, **kw) -> str:
    return prompts.text("policy.rule." + key, **kw)


# --- the generic splitter for a shell this bridge has no grammar for --------
#
# Used ONLY for the danger scan, which must run whatever the grammar: 4DOS
# has no `[shell]` table, so without this `format c:` under 4DOS was "doubt"
# and an Auto mode that allows doubt-as-elsewhere would run it unseen.
_ANY_SEP = re.compile(r"[&|;\r\n]+")


class _Classifier:
    def __init__(self, ctx: Ctx, shell: str) -> None:
        self.ctx = ctx
        self.shell = shell
        self.g = cmdguard.grammar_for(shell)
        self.guard = cmdguard.Guard(self.g, ctx.caps)
        self.root = cmdguard.Root(self.guard, ctx.work_root)
        self.v = Verdict()
        self._dangers = self._danger_table()
        self._protect = [self._norm(p) for p in ctx.lists.protect if p]

    # --- vocabulary ----------------------------------------------------------

    def _danger_table(self) -> dict:
        """stem -> (reason, source, grammar it belongs to).

        The UNION over every shell the machine has, plus the client's own
        list. A dangerous program is dangerous whichever shell started it,
        and a shell whose profile carries no table (4DOS) must not be the
        way around the one that does.
        """
        out: dict = {}
        names = set(self.ctx.shells or ()) | {self.shell} | set(
            profiles.shells())
        for name in sorted(n for n in names if n):
            g = cmdguard.grammar_for(name)
            for stem, why in g.always_ask.items():
                out.setdefault(stem, (str(why), "shell:" + name, g))
        for stem, why in (self.ctx.lists.danger or {}).items():
            out[str(stem).lower()] = (str(why), "client", None)
        return out

    def _norm(self, path: str) -> str:
        return self.guard.norm(path) if self.g.usable else path

    # --- where a path is --------------------------------------------------

    def protected(self, resolved: str) -> str:
        """The client pattern this path matches, or ""."""
        t = self._norm(resolved)
        for raw, pat in zip(self.ctx.lists.protect, self._protect,
                            strict=False):
            if glob_match(pat, t, self.g.sep, self.guard.fold):
                return raw
        return ""

    def tier(self, token: str, cwd: str) -> tuple[str, str]:
        """("work" | "else" | "sys" | "doubt" | "root", why) for a path.

        "root" is the work folder ITSELF -- inside by `Root.check`, which
        treats an empty tail as contained, and exactly the one path whose
        removal takes everything in the folder with it.
        """
        g = self.g
        if not g.usable:
            return "doubt", token
        if g.quote and any(token.count(q) % 2 for q in g.quote):
            return "doubt", token               # a quote that never closes
        raw = self.guard.strip_quotes(token.strip())
        resolved = self.guard.resolve(raw, cwd)
        if not resolved:
            return "doubt", token
        if g.variables is not None and g.variables.search(resolved):
            return "doubt", token
        # `...` is two levels up on some shells and a name on others; a
        # path that means different things to us and to the machine is one
        # we cannot place.
        if any(p and set(p) == {"."} and len(p) >= 3
               for p in resolved.replace(g.altsep or g.sep, g.sep)
               .split(g.sep)):
            return "doubt", token
        if resolved.startswith(g.sep * 2):
            return "else", resolved          # another machine's share
        hit = self.protected(resolved)
        if hit:
            return "sys", hit
        where = self.root.check(resolved)
        if where == cmdguard.INSIDE:
            if self._norm(resolved).rstrip(g.sep) == self.root.norm:
                return "root", resolved
            return "work", resolved
        if where == cmdguard.UNRESOLVED:
            return "doubt", token
        return "else", resolved              # OUTSIDE, or a DEVICE name

    def files_hit(self, token: str, cwd: str, *, removes: bool = False,
                  verb: str = "") -> None:
        """A write, move or delete of `token`."""
        where, what = self.tier(token, cwd)
        if where == "work":
            leaf = what.rsplit(self.g.sep, 1)[-1].lower()
            if leaf in (".gastonignore", ".gitignore"):
                # Writing these changes what syncs, which is a change to
                # the rules, not to the work.
                self.v.add(FILES_ELSE, _t("sync_rules", path=token))
            else:
                self.v.add(FILES_WORK, _t("in_work", path=token))
            if any(p.upper() == ".GASTON"
                   for p in self._norm(what).split(self.g.sep)):
                self.v.refuse = _t("gaston", path=token)
        elif where == "root":
            if removes:
                self.v.add(FILES_ELSE, _t("work_root", path=token))
            else:
                self.v.add(FILES_WORK, _t("in_work", path=token))
        elif where == "sys":
            self.v.add(FILES_SYS, _t("protected", path=token, pattern=what),
                       "client")
        elif where == "doubt":
            self.v.add(FILES_ELSE, _t("unresolved", path=token), doubt=True)
        else:
            self.v.add(FILES_ELSE, _t("elsewhere", path=token))

    # --- shell command lines ---------------------------------------------

    def command(self, line: str, cwd: str, depth: int = 0,
                floor_built: bool = False, best_effort: bool = False) -> None:
        g = self.g
        if not line.strip():
            return
        if not g.usable:
            # No grammar: the danger scan still runs, then everything else
            # is doubt.
            for part in _ANY_SEP.split(line):
                words = part.split()
                if words and self._danger(words, part):
                    continue
            if not best_effort:
                self.v.add(RUN_ELSE, _t("no_grammar", shell=self.shell),
                           doubt=True)
            return
        assert g.separators is not None
        for raw in g.separators.split(line):
            part = raw.strip()
            if part:
                self.fragment(part, cwd, depth, best_effort)

    def _danger(self, words: list, part: str) -> bool:
        """The danger scan on one command's first word. True if it decided
        (dangerous, or the read-only form of a dangerous program)."""
        verb = words[0].strip('"').lower()
        if self.g.usable:
            _, stem = self.guard.stem_of(verb)
        else:
            # No grammar to say what a separator or an extension is: take
            # the last component on any common separator and drop one dot
            # suffix. Over-matching here can only ask more.
            stem = re.split(r"[\\/:]", verb)[-1].rsplit(".", 1)[0]
        found = self._dangers.get(stem)
        if found is None:
            return False
        why, source, g = found
        if g is not None:
            safe = g.safe_if.get(stem)
            if safe is not None and safe.search(part):
                self.v.add(READ, _t("safe_form", program=stem), source)
                return True
            only = g.ask_only_if.get(stem)
            if only is not None and not only.search(part):
                self.v.add(READ, _t("safe_form", program=stem), source)
                return True
        self.v.add(RUN_SYS, _t("danger", program=stem, why=why), source)
        return True

    def _peel(self, words: list) -> list:
        """Take off the words that start another command: `call x`,
        `lh mouse`, `start /w prog`, `command /c format c:`."""
        g = self.g
        while words:
            first = self.guard.strip_quotes(words[0]).lower().lstrip("@")
            _, stem = self.guard.stem_of(first)
            if stem in g.prefixes:
                words = words[1:]
                while words and self.guard.is_switch(words[0]):
                    words = words[1:]
                continue
            if (stem in g.reentry and len(words) > 1
                    and words[1].lower() == g.run_switch):
                words = words[2:]
                continue
            if stem == "if" and len(words) > 1:
                # `if [not] errorlevel N x`, `if [not] exist P x`, `if a==b x`
                rest = words[1:]
                if rest and rest[0].lower() == "not":
                    rest = rest[1:]
                if rest and rest[0].lower() in ("errorlevel", "exist"):
                    rest = rest[2:]
                elif rest and "==" in rest[0]:
                    rest = rest[1:]
                elif len(rest) > 2 and rest[1] == "==":
                    rest = rest[3:]
                words = rest
                continue
            break
        return words

    def fragment(self, part: str, cwd: str, depth: int,
                 best_effort: bool = False) -> None:
        g = self.g
        if part.lstrip().startswith(":"):
            return                                    # a label
        # Redirection first: a write target is a write whatever the verb.
        if g.redirect_write is not None:
            for _, target in g.redirect_write.findall(part):
                if self.guard.leaf_is_device(self._norm(target)) and \
                        target.strip().lower() in ("nul", "nul:"):
                    continue
                self.files_hit(target, cwd)
        words = self._peel(self.guard.split_args(part))
        if not words:
            return
        if self._danger(words, part):
            return
        verb = self.guard.strip_quotes(words[0]).lower().lstrip("@")
        leaf, stem = self.guard.stem_of(verb)
        args = list(words[1:])
        pathed = any(s in verb for s in g.seps) or bool(
            g.drive is not None and g.drive.match(verb))
        builtin = not pathed and leaf == stem

        if builtin and stem in g.unbounded:
            if not best_effort:
                self.v.add(RUN_ELSE, _t("unbounded", program=leaf),
                           "shell:" + self.shell)
            return
        if builtin and stem in g.write:
            self._write_verb(stem, part, args, cwd)
            return
        if builtin and stem in g.conditional_read:
            writes = g.write_if.get(stem)
            if writes is None or not writes.search(part):
                self.v.add(READ, _t("reads", program=leaf))
            elif stem in g.machine_state:
                self.v.add(RUN_ELSE, _t("machine_state", program=leaf),
                           "shell:" + self.shell)
            else:
                for t in [a for a in args if not self.guard.is_switch(a)]:
                    self.files_hit(t, cwd)
            return
        if builtin and stem in g.read:
            if not best_effort:
                self.v.add(READ, _t("reads", program=leaf))
            return
        self._program(verb, leaf, stem, args, cwd, depth, best_effort)

    def _write_verb(self, stem: str, part: str, args: list,
                    cwd: str) -> None:
        g = self.g
        targets, dropped = self.guard.targets(stem, args)
        paths = [a for a in args if not self.guard.is_switch(a)]
        if not targets or (dropped and (stem in g.src_dest
                                        or stem in g.move_like)
                           and len(paths) < 2):
            self.v.add(FILES_ELSE, _t("no_destination", program=stem),
                       doubt=True)
            return
        removes = stem in g.removes or stem in g.move_like
        for t in targets:
            self.files_hit(t, cwd, removes=removes, verb=stem)
            if stem in g.removes and self._bulk(stem, t, cwd):
                self.v.add(PURGE, _t("bulk_delete", path=t))
        if stem in g.move_like:
            for t in paths[:-1]:
                self.files_hit(t, cwd, removes=True, verb=stem)

    def _bulk(self, stem: str, token: str, cwd: str) -> bool:
        """A delete through the shell is permanent. A recursive one, or one
        whose pattern is everything, is judged as a permanent delete."""
        if stem in self.g.recursive:
            return True
        leaf = self.guard.strip_quotes(token).rsplit(self.g.sep, 1)[-1]
        leaf = leaf.rsplit(self.g.altsep, 1)[-1] if self.g.altsep else leaf
        return leaf in ("*", "*.*")

    # --- programs ------------------------------------------------------

    def _listing(self, folder: str) -> frozenset | bool | None:
        """The folder's names, None if it could not be listed, or False if
        it has not been fetched yet (and is now wanted)."""
        key = self._norm(folder)
        if key not in self.ctx.listings:
            self.v.want("ls", folder, key)
            return False
        return self.ctx.listings[key]

    def _file(self, path: str) -> bytes | bool | None:
        key = self._norm(path)
        if key not in self.ctx.files:
            self.v.want("get", path, key)
            return False
        return self.ctx.files[key]

    def _program(self, verb: str, leaf: str, stem: str, args: list,
                 cwd: str, depth: int, best_effort: bool) -> None:
        g = self.g
        where = "else"
        found = ""                      # the program's path, when known
        cwd_where = self.root.check(cwd) if cwd else cmdguard.OUTSIDE
        pathed = any(s in verb for s in g.seps) or bool(
            g.drive is not None and g.drive.match(verb))
        if pathed:
            resolved = self.guard.resolve(verb, cwd)
            if not resolved:
                if not best_effort:
                    self.v.add(RUN_ELSE, _t("unresolved", path=verb),
                               doubt=True)
                return
            w = self.root.check(resolved)
            where = "work" if w == cmdguard.INSIDE else "else"
            found = resolved
        elif cwd_where == cmdguard.INSIDE and g.cwd_first:
            names = self._listing(cwd)
            if names is False:
                return                  # asked for; judged on the next pass
            if names is None:
                self.v.add(RUN_ELSE, _t("unlisted", folder=cwd), doubt=True)
                return
            cands = [leaf.upper()] if leaf != stem else [
                (stem + e).upper() for e in g.exts]
            for c in cands:
                if c in names:
                    where = "work"
                    found = cwd.rstrip(g.sep) + g.sep + c
                    break

        if where == "work":
            self._built(found, leaf, args, cwd, depth, best_effort)
            return
        if best_effort:
            return
        if cwd_where != cmdguard.INSIDE:
            self.v.add(RUN_ELSE, _t("runs_elsewhere", program=leaf,
                                    folder=cwd or "?"))
            return
        if stem not in self.ctx.lists.trust:
            self.v.add(RUN_ELSE, _t("untrusted", program=leaf))
            return
        self.v.add(RUN_WORK, _t("trusted", program=leaf), "client")
        self._trusted_args(args, cwd, depth)

    def _built(self, path: str, leaf: str, args: list, cwd: str,
               depth: int, best_effort: bool) -> None:
        """A program or script found in the work folder."""
        ext = os.path.splitext(leaf)[1].lower() or os.path.splitext(
            path)[1].lower()
        if ext in self.g.scripts:
            self._script(path, cwd, depth)
            return
        if not best_effort:
            self.v.add(RUN_BUILT, _t("built", program=leaf),
                       subject=self._norm(path))

    def _script(self, path: str, cwd: str, depth: int) -> None:
        """Every line of a script in the work folder, the strictest wins,
        and never less than RUN_BUILT."""
        self.v.add(RUN_BUILT, _t("script", program=path),
                   subject=self._norm(path))
        if depth >= MAX_DEPTH:
            self.v.add(RUN_ELSE, _t("too_deep", program=path), doubt=True)
            return
        data = self._file(path)
        if data is False:
            return
        if data is None or len(data) > MAX_SCRIPT:
            self.v.add(RUN_ELSE, _t("unread", program=path), doubt=True)
            return
        self.v.scripts[self._norm(path)] = hashlib.sha1(data).hexdigest()
        text = data.decode("latin-1")
        for line in text.splitlines():
            self.command(line, cwd, depth + 1)

    def _trusted_args(self, args: list, cwd: str, depth: int) -> None:
        """What a trusted program's own options name. A compiler's output
        switch (`-fe=C:\\...`) is a write the shell grammar never sees."""
        g = self.g
        for a in args:
            raw = self.guard.strip_quotes(a)
            if raw.startswith("@"):
                self._response(raw[1:], cwd, depth)
                continue
            for cand in self._path_parts(raw):
                where, what = self.tier(cand, cwd)
                if where == "sys":
                    self.v.add(RUN_SYS, _t("arg_protected", path=cand,
                                           pattern=what), "client")
                elif where == "else":
                    self.v.add(RUN_ELSE, _t("arg_elsewhere", path=cand))
                elif where == "doubt" and self._looks_like_path(cand):
                    self.v.add(RUN_ELSE, _t("unresolved", path=cand),
                               doubt=True)
        # A make-style tool runs the recipes in its script. Read the one in
        # the work folder, best effort: it can only RAISE the verdict, to a
        # dangerous program or a path outside -- an unknown program in a
        # recipe is the trusted tool's business, said on the honesty line.
        names = self._listing(cwd)
        if isinstance(names, frozenset) and "MAKEFILE" in names:
            self._recipes(cwd.rstrip(g.sep) + g.sep + "MAKEFILE", cwd, depth)
        for i, a in enumerate(args):
            if a.lower() in ("-f", "/f") and i + 1 < len(args):
                self._recipes(self.guard.resolve(args[i + 1], cwd) or "",
                              cwd, depth)

    def _looks_like_path(self, token: str) -> bool:
        g = self.g
        return any(s in token for s in g.seps) or bool(
            g.drive is not None and g.drive.match(token))

    def _path_parts(self, token: str) -> list:
        """The candidates in one argument: the whole of it if it is not a
        switch, and whatever follows `=` or sits at a drive spec inside it
        (`-fe=C:\\X`, `/Fe:C:\\X`)."""
        g = self.g
        out = []
        if not self.guard.is_switch(token) and not token.startswith("-"):
            out.append(token)
        if "=" in token:
            out.append(token.split("=", 1)[1])
        if g.drive is not None:
            for i in range(1, len(token)):
                if g.drive.match(token[i:]) and token[i - 1] in "=:,":
                    out.append(token[i:])
        seen, uniq = set(), []
        for c in out:
            if c and c not in seen and (self._looks_like_path(c)
                                        or c is token):
                seen.add(c)
                uniq.append(c)
        return uniq

    def _response(self, name: str, cwd: str, depth: int) -> None:
        path = self.guard.resolve(name, cwd)
        if not path or self.root.check(path) != cmdguard.INSIDE:
            self.v.add(RUN_ELSE, _t("arg_elsewhere", path="@" + name),
                       doubt=not path)
            return
        data = self._file(path)
        if data is False:
            return
        if data is None:
            self.v.add(RUN_ELSE, _t("unread", program=path), doubt=True)
            return
        self._trusted_args(data.decode("latin-1").split(), cwd, depth + 1)

    def _recipes(self, path: str, cwd: str, depth: int) -> None:
        if not path or depth >= MAX_DEPTH:
            return
        data = self._file(path)
        if not isinstance(data, bytes) or len(data) > MAX_SCRIPT:
            return
        self.v.scripts[self._norm(path)] = hashlib.sha1(data).hexdigest()
        for line in data.decode("latin-1").splitlines():
            if line[:1] in (" ", "\t") and line.strip():
                self.command(line.strip().lstrip("@-+"), cwd, depth + 1,
                             best_effort=True)


def _resolve(ctx: Ctx, path: str) -> str | None:
    if ctx.resolve is None:
        return path
    return ctx.resolve(path)


def classify(ctx: Ctx, tool: str, args: dict | None) -> Verdict:
    """Which categories `tool(args)` lands in. See the module note."""
    args = args if isinstance(args, dict) else {}
    if tool == PLAN_EXIT:
        v = Verdict()
        v.plan_exit = True
        return v
    if tool in _BRIDGE_READS:
        v = Verdict()
        v.add(READ, _t("bridge_read"))
        return v
    if tool in _BRIDGE_EDITS:
        v = Verdict()
        target = str(args.get("file_path") or args.get("notebook_path") or "")
        inside = False
        if ctx.bridge_dir and target:
            try:
                root = os.path.realpath(ctx.bridge_dir)
                inside = os.path.commonpath(
                    [root, os.path.realpath(target)]) == root
            except (OSError, ValueError):
                inside = False
        if inside:
            v.add(FILES_WORK, _t("in_work", path=target))
        else:
            v.add(FILES_ELSE, _t("elsewhere", path=target))
        return v
    if tool == "Bash":
        v = Verdict()
        v.add(BRIDGE_SHELL, _t("bridge_shell"))
        return v
    if tool in _WEB:
        v = Verdict()
        v.add(WEB, _t("web"))
        return v
    if not tool.startswith(_MACHINE):
        return Verdict()                       # unknown: the policy asks
    verb = tool[len(_MACHINE):]
    named = str(args.get("shell") or "").strip()
    shell = named if named and named.lower() != "none" else (
        (ctx.shells or ("",))[0])
    c = _Classifier(ctx, shell)
    v = c.v

    if verb in _EXEMPT:
        v.exempt = True
        return v
    if verb in _STATE:
        v.add(READ, _t("state"))
        return v
    if verb == "ui" or verb == "shot":
        v.add(SCREEN_SEE, _t("screen_see"))
        return v
    if verb == "input":
        v.add(SCREEN_DRIVE, _t("screen_drive"))
        return v
    if verb == "kill":
        v.add(RUN_ELSE, _t("kill", pid=args.get("pid", "?")))
        return v

    def path_of(key: str) -> str:
        raw = str(args.get(key) or "").strip()
        if not raw:
            return ""
        got = _resolve(ctx, raw)
        return got if got is not None else raw

    if verb in _READS:
        p = path_of("path")
        if p.startswith((c.g.sep or "\\") * 2):
            v.add(FILES_ELSE, _t("share", path=p))
        else:
            v.add(READ, _t("reads_file", path=p or "?"))
        return v
    if verb in _WRITES or verb == "sync_resolve":
        p = path_of("path")
        if verb == "sync_resolve" and not p:
            v.add(FILES_WORK, _t("sync_resolve"))
            return v
        c.files_hit(p or "?", ctx.work_root)
        return v
    if verb == "copy":
        c.files_hit(path_of("to") or "?", ctx.work_root)
        return v
    if verb == "move":
        c.files_hit(path_of("path") or "?", ctx.work_root, removes=True)
        c.files_hit(path_of("to") or "?", ctx.work_root)
        return v
    if verb in ("delete", "rmdir"):
        c.files_hit(path_of("path") or "?", ctx.work_root, removes=True)
        if verb == "delete" and args.get("permanent"):
            v.add(PURGE, _t("permanent", path=path_of("path")))
        return v
    if verb in ("exec", "launch"):
        cmd = str(args.get("command") or "")
        asked = str(args.get("cwd") or "").strip()
        if asked:
            cwd = _resolve(ctx, asked)
            if cwd is None:
                v.add(RUN_ELSE, _t("unresolved", path=asked), doubt=True)
                return v
        else:
            cwd = ctx.work_root
        if not c.g.usable:
            c.command(cmd, cwd)            # the danger scan, then doubt
            return v
        if verb == "launch":
            words = c.guard.split_args(cmd) if c.g.usable else cmd.split()
            if not words:
                v.add(RUN_ELSE, _t("untrusted", program="?"))
                return v
            if not c._danger(words, cmd):
                verbw = c.guard.strip_quotes(words[0]).lower()
                leaf, stem = c.guard.stem_of(verbw)
                c._program(verbw, leaf, stem, list(words[1:]), cwd, 0, False)
            return v
        if named.lower() == "none":
            # Spawned directly: no builtins, no redirection, one program.
            words = c.guard.split_args(cmd) if c.g.usable else cmd.split()
            if words and not c._danger(words, cmd):
                verbw = c.guard.strip_quotes(words[0]).lower()
                leaf, stem = c.guard.stem_of(verbw)
                c._program(verbw, leaf, stem, list(words[1:]), cwd, 0, False)
            return v
        c.command(cmd, cwd)
        return v
    return v                                   # a machine tool not listed: ask


def fill(ctx: Ctx, wanted: list, results: dict) -> None:
    """Record fetched `wants` on the context, keyed the way `classify`
    looks them up. `results` maps (kind, path) -> bytes / names / None."""
    for kind, path, key in wanted:
        got = results.get((kind, path))
        if kind == "ls":
            ctx.listings[key] = (frozenset(n.upper() for n in got)
                                 if got is not None else None)
        else:
            ctx.files[key] = got


__all__ = ["CATS", "Ctx", "Hit", "Lists", "Verdict", "classify", "fill"]
