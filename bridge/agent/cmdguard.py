"""What a command line reaches outside the mirrored folder.

This is not a classifier and must never become one. Four measurements say so:

  * There is no classifier in our loop. With `can_use_tool` installed, a
    documented blocked-by-default action (`git reset --hard` on a dirty repo)
    executed unchanged in both `default` and `auto` permission mode. The
    auto-mode classifier stands in for the user, and by wiring `can_use_tool`
    we already did.
  * A deterministic path check scored the same as Haiku on 54 hand-written
    cases: both 52/54, both zero dangerous-rated-safe, both with the same two
    false alarms. Two unrelated methods agreeing case-for-case says the
    residue is a property of the problem.
  * A local 7B answered ASK to everything sampled, including `dir c:\\`. A
    guard that always says ASK is indistinguishable from no guard.
  * No purpose-built model exists, and none can: guard models answer a
    command-ABSOLUTE question, and ours is machine-RELATIVE. `deltree /y
    c:\\work\\build` is safe and `deltree /y c:\\temp` is not, and the only
    difference is our sync configuration.

Anthropic's own documentation reaches the same place from the other side:
"a deny or ask rule covers the invocation Claude usually produces and isn't a
security boundary around the program."

So: no verdict here is ever a grant. `reason()` answers why a command may
reach outside the mirror, or None. None means "nothing found", NOT "safe" --
the caller still asks the user. The only thing this can do is turn a
would-be-silent call into a visible one.

WHAT IT PARSES is declared by the shell's profile, not assumed here. This
file owns the JUDGEMENT -- split, resolve, contain, decide -- and owns none
of the VOCABULARY or the GRAMMAR. A guard whose logic is data is a guard
anyone can edit into agreeing with them; a guard whose grammar is Python is a
guard that only works on one machine.

THE FILE USED TO BE CALLED `doscmd.py`, AND THE NAME WAS THE LEAST OF IT.
Moving the verb lists into `bridge/profiles/<shell>.toml` left four regexes
behind as Python fallbacks -- including `^[A-Za-z]:`, a drive letter -- so the
shell that had supposedly left was still deciding what a path looked like the
moment a second shell profile existed. Two further holes were measured at the
same time and are what the `caps` argument is for:

    del /WORK/x     root=/work   ->  None      (case-folded unconditionally)
    del /work/$T/x  root=/work   ->  None      (only %...% counted as a variable)

Both are the dangerous direction: a write outside the mirror, reported as
inside, with nothing shown to the user. `caps.case` had been negotiated at
the handshake and stored on the session for a year before anything read it
here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import profiles
from ..files import caps as C

log = logging.getLogger("bridge.cmdguard")


def _default_shell() -> str:
    """The shell to judge by when the caller does not say.

    Derived, never named here. If exactly one profile carries guard tables
    that is the shell this bridge knows and there is nothing to choose. With
    several, refusing to pick is the only honest answer -- and the caller
    SHOULD be saying which, because detection already reports it.
    """
    known = profiles.shells()
    if len(known) == 1:
        return known[0]
    if not known:
        log.error("no shell profile carries guard tables")
    else:
        log.error("%d shells have guard tables (%s); the caller did not say "
                  "which, so every command will be referred to the user",
                  len(known), ", ".join(sorted(known)))
    return ""


def _pat(table: dict, key: str) -> "re.Pattern | None":
    """A regex from the profile, or None. NO FALLBACK, deliberately.

    The fallbacks this replaces were the whole bug: with the verb lists
    degrading to empty and the regexes degrading to COMMAND.COM's, the guard
    kept confidently resolving drive letters for a shell it knew nothing
    about. A missing pattern now makes the grammar unusable, and an unusable
    grammar refers everything to the user.
    """
    raw = table.get(key)
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        log.error("shell profile has an unusable %s pattern (%s); the guard "
                  "will refer every command to the user", key, exc)
        return None


def _cpat(table: dict, key: str) -> dict:
    """A verb -> case-insensitive regex map from the profile."""
    out = {}
    for verb, raw in dict(table.get(key, {})).items():
        try:
            out[verb.lower()] = re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            log.error("shell profile %s.%s is not a usable pattern (%s); "
                      "ignoring it", key, verb, exc)
    return out


@dataclass(frozen=True)
class Grammar:
    """One shell's grammar and vocabulary, as declared by its profile.

    Everything here is a fact about somebody else's computer. Nothing here is
    a decision -- the decisions are below, and they read this.
    """

    name: str = ""

    # --- grammar ---------------------------------------------------------
    separators: "re.Pattern | None" = None
    redirect_write: "re.Pattern | None" = None
    redirect_read: "re.Pattern | None" = None
    drive: "re.Pattern | None" = None
    #: A token matching this contains something this cannot expand -- a
    #: variable, a home-directory shorthand, a substitution. It is therefore
    #: UNRESOLVABLE, which means "ask", not "safe". The old code hardcoded
    #: `"%" in t`, which is correct for COMMAND.COM and silently wrong for
    #: every shell whose sigil is `$` or `~`.
    variables: "re.Pattern | None" = None
    sep: str = ""
    altsep: str = ""
    quote: str = ""
    switch: str = ""
    switch_max: int = 0
    exts: tuple = ()

    # --- vocabulary ------------------------------------------------------
    read: frozenset = frozenset()
    write: frozenset = frozenset()
    src_dest: frozenset = frozenset()
    conditional_read: frozenset = frozenset()
    unbounded: frozenset = frozenset()
    devices: frozenset = frozenset()
    #: Verbs that REMOVE their sources as well as writing their destination.
    move_like: frozenset = frozenset()
    always_ask: dict = field(default_factory=dict)
    #: verb -> the form in which an `always_ask` verb only reads.
    safe_if: dict = field(default_factory=dict)
    #: verb -> the form in which an `always_ask` verb actually writes.
    #: Anything else is a read. (`chkdsk` repairs only with `/F`.)
    ask_only_if: dict = field(default_factory=dict)
    #: verb -> the form in which a `conditional_read` verb writes.
    write_if: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Can this grammar decide anything at all?

        A separator to split on and a separator to walk paths with are the
        irreducible minimum; without either, every "is this inside the
        mirror" answer would be invented. `drive` is optional on purpose --
        a shell with no drive letters simply omits it.
        """
        return bool(self.separators and self.sep)

    @property
    def seps(self) -> tuple:
        return (self.sep, self.altsep) if self.altsep else (self.sep,)


#: A grammar that knows nothing. Every command is referred to the user.
NOTHING = Grammar()


def _grammar(shell: str) -> Grammar:
    t = profiles.shell(shell)
    if not t:
        log.error("no guard tables for shell %r; every command will be "
                  "referred to the user", shell)
        return Grammar(name=shell)
    fs = lambda k: frozenset(x.lower() for x in t.get(k, ()))
    return Grammar(
        name=shell,
        separators=_pat(t, "separators"),
        redirect_write=_pat(t, "redirect_write"),
        redirect_read=_pat(t, "redirect_read"),
        drive=_pat(t, "drive"),
        variables=_pat(t, "variables"),
        sep=str(t.get("sep", "")),
        altsep=str(t.get("altsep", "")),
        quote=str(t.get("quote", "")),
        switch=str(t.get("switch", "")),
        switch_max=int(t.get("switch_max", 0)),
        exts=tuple(str(e).lower() for e in t.get("exts", ())),
        read=fs("read"), write=fs("write"), src_dest=fs("src_dest"),
        conditional_read=fs("conditional_read"), unbounded=fs("unbounded"),
        devices=fs("devices"), move_like=fs("move_like"),
        always_ask={k.lower(): v for k, v in t.get("always_ask", {}).items()},
        safe_if=_cpat(t, "safe_if"),
        ask_only_if=_cpat(t, "ask_only_if"),
        write_if=_cpat(t, "write_if"),
    )


_CACHE: dict = {}


def grammar_for(shell: str = "") -> Grammar:
    """The grammar for a shell, built once. `""` asks for the default."""
    shell = shell or _default_shell()
    if not shell:
        return NOTHING
    if shell not in _CACHE:
        _CACHE[shell] = _grammar(shell)
    return _CACHE[shell]


class Guard:
    """One shell's grammar plus one machine's path semantics.

    The split matters and it took a bug to see it. A shell's grammar comes
    from its profile; whether `/WORK` and `/work` are the same directory does
    NOT -- that is a property of the filesystem, declared by the client at the
    handshake in `caps.case`, and a shell profile has no business asserting
    it. The same COMMAND.COM grammar is correct on a FAT volume and on a
    case-sensitive one; only the comparison changes.
    """

    def __init__(self, grammar: Grammar, caps=None) -> None:
        self.g = grammar
        self.caps = caps if caps is not None else C.CONSTRAINED
        #: Fold only where the machine says names fold. Folding on a
        #: case-sensitive filesystem calls `/WORK/x` contained in a mirror
        #: rooted at `/work` -- a different directory, reported as the same
        #: one, with nothing shown to the user.
        self.fold = bool(getattr(self.caps, "case", C.CASE_FOLD))

    # --- text --------------------------------------------------------------

    def _case(self, s: str) -> str:
        return s.upper() if self.fold else s

    def strip_quotes(self, token: str) -> str:
        return token.strip(self.g.quote) if self.g.quote else token

    def split_args(self, part: str) -> list[str]:
        """Tokenise, respecting the shell's quote characters.

        `part.split()` breaks `copy "C:\\WORK\\Chat Log.txt" ...` into four
        tokens, and the guard then reported `Log.txt"` as a path that is
        outside the mirror. The verdict was right by accident and the REASON
        shown to the user was nonsense -- which is worse than useless,
        because someone who learns the guard's phrasing is wrong stops
        reading it.
        """
        out: list[str] = []
        cur: list[str] = []
        quote = ""
        for ch in part:
            if quote:
                if ch == quote:
                    quote = ""
                    continue
                cur.append(ch)
                continue
            if ch in self.g.quote:
                quote = ch
                continue
            if ch.isspace():
                if cur:
                    out.append("".join(cur))
                    cur = []
                continue
            cur.append(ch)
        if cur:
            out.append("".join(cur))
        return out

    # --- paths -------------------------------------------------------------

    def norm(self, token: str) -> str:
        """A guest path in comparable form: `.`/`..` resolved, one separator.

        NOT os.path. POSIX semantics are wrong for these strings by
        construction -- os.path.realpath("C:\\WINDOWS") on Linux yields
        "$CWD/C:\\WINDOWS", i.e. it reports a guest system path as living
        INSIDE the working copy, which is the dangerous direction to be wrong
        in. `off_limits` carries that latent today.

        Resolving `..` is the whole point: `C:\\WORK\\..\\..\\WINDOWS\\WIN.INI`
        starts with the root and its tail starts with a separator, so a prefix
        test alone calls it contained. A red team found nine variants of
        exactly that and nothing else.
        """
        sep = self.g.sep
        t = self._case(self.strip_quotes(token.strip()))
        if self.g.altsep:
            t = t.replace(self.g.altsep, sep)
        drive = ""
        if self.g.drive is not None:
            m = self.g.drive.match(t)
            if m:
                drive, t = t[:m.end()], t[m.end():]
        rooted = t.startswith(sep)

        out: list[str] = []
        for part in t.split(sep):
            if not part or part == ".":
                continue
            if part == "..":
                if out:
                    out.pop()
                elif rooted:
                    continue            # `..` at the root is the root
                else:
                    out.append("..")    # escapes a relative base
                continue
            out.append(part)
        return drive + (sep if rooted else "") + sep.join(out)

    def leaf_is_device(self, path: str) -> bool:
        leaf = path.rsplit(self.g.sep, 1)[-1]
        return leaf.split(".", 1)[0].lower() in self.g.devices

    def is_switch(self, token: str) -> bool:
        """A switch, not a path.

        On COMMAND.COM `/` starts a switch AND is accepted as a path
        separator by everything that goes through CreateFileA, so
        `/windows/system/x` is a real path. An early draft discarded every
        `/`-prefixed token before extracting paths, which made that path not
        merely unresolvable but invisible -- the guard reported "writes
        inside mirror" for a command whose destination it had thrown away. A
        switch is short and has no separator in it.

        A shell with no switch prefix declares none and every token is a
        candidate path.
        """
        if not self.g.switch or not token.startswith(self.g.switch):
            return False
        rest = token[len(self.g.switch):]
        if any(s in rest for s in self.g.seps):
            return False
        return len(token) <= self.g.switch_max

    def resolve(self, token: str, cwd: str) -> str:
        """An argument as an absolute guest path, or "" if it cannot be one."""
        t = self.strip_quotes(token.strip())
        if not t:
            return ""
        if self.g.drive is not None and self.g.drive.match(t):
            return t
        if any(t.startswith(s) for s in self.g.seps):
            return t
        if not cwd:
            # The bridge is never told the guest's working directory: there
            # is no CWD on the wire in either direction, each EXEC is a fresh
            # shell invocation, and an empty cwd makes the child inherit the
            # GUI process's directory -- a value nobody here has ever seen.
            # So a relative path with no cwd is genuinely unresolvable.
            return ""
        return cwd.rstrip(self.g.sep) + self.g.sep + t

    # --- verbs -------------------------------------------------------------

    def stem_of(self, word: str) -> tuple:
        """`(leaf, stem)` for a verb. A pathed invocation names a program."""
        leaf = word
        for s in self.g.seps:
            leaf = leaf.rsplit(s, 1)[-1]
        stem = leaf
        for ext in self.g.exts:
            if leaf.endswith(ext):
                stem = leaf[: -len(ext)]
                break
        return leaf, stem

    def targets(self, verb: str, args: list[str]) -> list[str]:
        """Which arguments this verb WRITES to."""
        paths = [a for a in args if not self.is_switch(a)]
        if not paths:
            return []
        if verb in self.g.src_dest:
            # `copy a b` writes b. `copy a` writes into the working
            # directory, which is still a write and still has to be checked.
            return paths[-1:] if len(paths) > 1 else paths
        return paths            # del/deltree/ren/md/rd/move: every argument


class Root:
    """The mirrored folder, normalised once."""

    def __init__(self, guard: Guard, guest_root: str) -> None:
        self.guard = guard
        self.raw = (guest_root or "").strip()
        self.norm = (guard.norm(self.raw).rstrip(guard.g.sep)
                     if self.raw and guard.g.usable else "")

    def contains(self, token: str) -> bool:
        """Is this path inside the mirror? Unknown shapes answer False.

        Refuses outright when there is no root. An early draft asserted the
        opposite -- "no mirror, so everything is outside it, so every exec
        asks" -- and the code did the reverse, because "".startswith("") is
        true: with an empty root, `del \\io.sys` and `deltree /y \\windows`
        were both "contained". That is the default state of any sync-disabled
        deployment, and it failed OPEN.
        """
        if not self.norm:
            return False
        g = self.guard.g
        t = self.guard.norm(token)
        if g.variables is not None and g.variables.search(t):
            return False            # a variable this cannot expand
        if ".." in t.split(g.sep):
            return False            # escaped a relative base
        if not t.startswith(self.norm):
            return False
        tail = t[len(self.norm):]
        # Empty or separator-prefixed only: C:\WORK must not match
        # C:\WORK2, C:\WORKBACKUP or C:\WORK.OLD.
        if tail and not tail.startswith(g.sep):
            return False
        return not self.guard.leaf_is_device(t)


def reason(command: str, *, cwd: str = "", guest_root: str = "",
           shell: str = "", caps=None) -> str | None:
    """Why this command line may reach outside the mirror, or None.

    None means "nothing found", NOT "safe". Nothing here is ever a grant.

    Anything unresolvable answers a reason. That is the whole discipline: the
    failure mode of a miss is one extra prompt, and the failure mode of a
    false "contained" is a write nobody was asked about.

    `shell` names the profile whose grammar to judge by -- detection already
    reports it, and `toolrules.inventory` sorts shells first for exactly this.
    `caps` is the client's declared path semantics; omitting it assumes the
    restrictive default, which is the safe direction but is an assumption.
    """
    if not command or not command.strip():
        return None

    guard = Guard(grammar_for(shell), caps)
    g = guard.g
    if not g.usable:
        return ("the shell on this machine is not one this bridge has rules "
                "for, so what this command touches cannot be known")

    root = Root(guard, guest_root)
    if not root.norm:
        return ("no folder is mirrored from this machine, so nothing on it "
                "can be restored")

    cwd_norm = guard.norm(cwd) if cwd else ""

    for raw in g.separators.split(command):
        part = raw.strip()
        if not part:
            continue

        # Redirection first: a write target is a write wherever it appears,
        # and it is checked even for a read-only verb -- `dir c:\ > c:\x`
        # writes c:\x.
        if g.redirect_write is not None:
            for _, target in g.redirect_write.findall(part):
                resolved = guard.resolve(target, cwd_norm)
                if not resolved:
                    return "writes to %s, which cannot be resolved" % target
                if not root.contains(resolved):
                    return "writes outside the mirrored folder: %s" % target

        words = guard.split_args(part)
        if not words:
            continue
        verb = guard.strip_quotes(words[0]).lower()
        leaf, stem = guard.stem_of(verb)

        if stem in g.always_ask:
            # Read-only forms are worth not nagging about -- and they must
            # CONTINUE, not fall through: such a verb is in neither `read`
            # nor `write`, so a `pass` here dropped it into the
            # unknown-program branch and `fdisk /status` reported as
            # unreadable.
            safe = g.safe_if.get(stem)
            if safe is not None and safe.search(part):
                continue
            only = g.ask_only_if.get(stem)
            if only is not None and not only.search(part):
                continue
            return "%s: %s" % (leaf, g.always_ask[stem])

        if stem in g.unbounded:
            return ("%s runs whatever it is given, so what it touches cannot "
                    "be known from this command line" % leaf)

        args = list(words[1:])

        if stem in g.write:
            targets = guard.targets(stem, args)
            if not targets:
                return "%s with no destination this can resolve" % leaf
            for t in targets:
                resolved = guard.resolve(t, cwd_norm)
                if not resolved:
                    return "writes to %s, which cannot be resolved" % t
                if not root.contains(resolved):
                    return "writes outside the mirrored folder: %s" % t
            # A `move`-like verb removes its sources as well as writing.
            if stem in g.move_like:
                for t in [a for a in args if not guard.is_switch(a)][:-1]:
                    resolved = guard.resolve(t, cwd_norm)
                    if not resolved or not root.contains(resolved):
                        return "removes %s, which is outside the mirrored " \
                               "folder" % t
            continue

        if stem in g.conditional_read:
            # `attrib x` reports; `attrib -r x` sets. Only the second writes.
            writes = g.write_if.get(stem)
            if writes is None or not writes.search(part):
                continue
            for t in [a for a in args if not guard.is_switch(a)]:
                resolved = guard.resolve(t, cwd_norm)
                if not resolved or not root.contains(resolved):
                    return ("changes attributes outside the mirrored "
                            "folder: %s" % t)
            continue

        if stem in g.read:
            continue            # reads are not guarded; see the module note

        # An unknown verb is an arbitrary program: a .bat the agent wrote, a
        # compiler, anything. It is the irreducible residue -- the same hole
        # Anthropic documents for `npx` and `devbox run` -- and the honest
        # answer is to ask.
        return ("%s is a program this cannot read, so what it touches is "
                "unknown" % leaf)

    return None
