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

from ..files import caps as C
from . import profiles

log = logging.getLogger("bridge.cmdguard")


def _default_shell() -> str:
    """The shell to judge by when the caller does not say.

    Derived, never named here. If exactly one profile carries guard tables
    that is the shell this bridge knows and there is nothing to choose.

    THE LAST RESORT, and 02.5 is why it is only that now. This used to be
    reached for every guest reporting no tool inventory -- which every
    deployed guest does until reflashed -- so adding a second shell profile
    silently turned the guard off for all of them (`grammar_for("")` returned
    NOTHING).

    Fixed where it belongs: the machine reports which shells it HAS
    (`toolrules.shells_of`), the model names one on each `exec` call, and
    `PermissionDesk` judges by that name, falling back to the first shell
    reported. Nothing here picks a shell out of the profiles it ships any
    more.

    So this is reached only when the machine named no shell AND reported
    none. One installed profile is then the only candidate; several is
    genuinely unanswerable, and inventing an answer is the one thing this
    module may not do.
    """
    known = profiles.shells()
    if len(known) == 1:
        return known[0]
    if not known:
        log.error("no shell profile carries guard tables")
    else:
        log.error("%d shells have guard tables (%s), the machine reported "
                  "none and the call named none, so every command will be "
                  "referred to the user",
                  len(known), ", ".join(sorted(known)))
    return ""


def _pat(table: dict, key: str) -> re.Pattern | None:
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
    separators: re.Pattern | None = None
    redirect_write: re.Pattern | None = None
    redirect_read: re.Pattern | None = None
    drive: re.Pattern | None = None
    #: A token matching this contains something this cannot expand -- a
    #: variable, a home-directory shorthand, a substitution. It is therefore
    #: UNRESOLVABLE, which means "ask", not "safe". The old code hardcoded
    #: `"%" in t`, which is correct for COMMAND.COM and silently wrong for
    #: every shell whose sigil is `$` or `~`.
    variables: re.Pattern | None = None
    sep: str = ""
    altsep: str = ""
    quote: str = ""
    switch: str = ""
    switch_max: int = 0
    #: What a switch actually LOOKS like on this shell -- the vocabulary, not
    #: a length. See `Guard.is_switch` for what judging by length cost.
    switch_token: re.Pattern | None = None
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

    # --- how a bare name finds its program --------------------------------
    #: The shell looks in the current directory before PATH.
    cwd_first: bool = False
    #: Of `exts`, the ones that are lists of commands, read and judged line
    #: by line rather than trusted or distrusted by name.
    scripts: tuple = ()
    #: Words that run the rest of the line as a command of its own.
    prefixes: frozenset = frozenset()
    #: Names of the shell itself, which runs what follows `run_switch`.
    reentry: frozenset = frozenset()
    run_switch: str = ""
    #: `write` verbs that destroy what they name; of those, the ones that
    #: take a whole tree.
    removes: frozenset = frozenset()
    recursive: frozenset = frozenset()
    #: `conditional_read` verbs whose written form changes the machine.
    machine_state: frozenset = frozenset()

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
    def fs(k: str) -> frozenset:
        return frozenset(x.lower() for x in t.get(k, ()))
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
        switch_token=_pat(t, "switch_token"),
        exts=tuple(str(e).lower() for e in t.get("exts", ())),
        read=fs("read"), write=fs("write"), src_dest=fs("src_dest"),
        conditional_read=fs("conditional_read"), unbounded=fs("unbounded"),
        devices=fs("devices"), move_like=fs("move_like"),
        always_ask={k.lower(): v for k, v in t.get("always_ask", {}).items()},
        safe_if=_cpat(t, "safe_if"),
        ask_only_if=_cpat(t, "ask_only_if"),
        write_if=_cpat(t, "write_if"),
        cwd_first=bool(t.get("cwd_first", False)),
        scripts=tuple(str(e).lower() for e in t.get("scripts", ())),
        prefixes=fs("prefixes"), reentry=fs("reentry"),
        run_switch=str(t.get("run_switch", "")).lower(),
        removes=fs("removes"), recursive=fs("recursive"),
        machine_state=fs("machine_state"),
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
        r"""A switch, not a path.

        On COMMAND.COM `/` starts a switch AND is accepted as a path
        separator by everything that goes through CreateFileA, so
        `/windows/system/x` is a real path. An early draft discarded every
        `/`-prefixed token before extracting paths, which made that path not
        merely unresolvable but invisible -- the guard reported "writes
        inside mirror" for a command whose destination it had thrown away.

        BY VOCABULARY, NOT BY LENGTH, and that is 02.10: "short and
        separator-free" is not what makes a token a switch. Measured against
        the real profile with the mirror at C:\GASTON\OK, `/io.sys` (seven
        characters, no separator) was a switch by the old length rule, so
        `targets` dropped it -- leaving the SOURCE as the only path for
        `copy c:\gaston\ok\a.txt /io.sys`, which read as fully contained.
        `switch_token` says what this shell's switches actually look like (a
        letter or two, optionally `:value`), which rules `/io.sys` out on the
        dot while `/temp` still matches -- so `targets` also reports what it
        dropped.

        The length bound stays as the fallback for a profile that declares no
        vocabulary: it is the weaker rule, but it is better than treating
        every `/x` as a path.

        A shell with no switch prefix declares none and every token is a
        candidate path.
        """
        if not self.g.switch or not token.startswith(self.g.switch):
            return False
        rest = token[len(self.g.switch):]
        if any(s in rest for s in self.g.seps):
            return False
        if self.g.switch_token is not None:
            return bool(self.g.switch_token.match(token))
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

    def targets(self, verb: str, args: list[str]) -> tuple:
        r"""`(written_to, dropped_as_switches)` for this verb's arguments.

        The second half is 02.10's other half. A token dropped as a switch is
        invisible to every check below, and for a verb whose destination is
        POSITIONAL that is the difference between judging the destination and
        judging the source: `copy c:\ok\a.txt /temp` leaves one path, the
        source, and `src_dest`'s "one path means it writes into the working
        directory" branch then measured containment against it. The caller
        cannot tell that from an honest one-argument `copy a.txt` unless this
        says what it threw away.
        """
        dropped = [a for a in args if self.is_switch(a)]
        paths = [a for a in args if not self.is_switch(a)]
        if not paths:
            return [], dropped
        if verb in self.g.src_dest:
            # `copy a b` writes b. `copy a` writes into the working
            # directory, which is still a write and still has to be checked.
            return (paths[-1:] if len(paths) > 1 else paths), dropped
        # del/deltree/ren/md/rd/move: every argument
        return paths, dropped


#: What `Root.check` found. Not a bool (02.11): a bare False turned every
#: refusal into "writes outside the mirrored folder", including for
#: `copy x c:\gaston\ok\con` -- inside the mirror, but CON is a device in
#: every directory. That is a false and checkable claim, and this module's
#: own note says what it costs: "someone who learns the guard's phrasing is
#: wrong stops reading it".
INSIDE = ""
OUTSIDE = "outside"
DEVICE = "device"
UNRESOLVED = "unresolved"


class Root:
    """The mirrored folder, normalised once."""

    def __init__(self, guard: Guard, guest_root: str) -> None:
        self.guard = guard
        self.raw = (guest_root or "").strip()
        self.norm = (guard.norm(self.raw).rstrip(guard.g.sep)
                     if self.raw and guard.g.usable else "")

    def check(self, token: str) -> str:
        """`INSIDE`, or which of the three ways this path is not.

        Refuses outright when there is no root. An early draft asserted the
        opposite -- "no mirror, so everything is outside it, so every exec
        asks" -- and the code did the reverse, because "".startswith("") is
        true: with an empty root, `del \\io.sys` and `deltree /y \\windows`
        were both "contained". That is the default state of any sync-disabled
        deployment, and it failed OPEN.
        """
        if not self.norm:
            return OUTSIDE
        g = self.guard.g
        t = self.guard.norm(token)
        if g.variables is not None and g.variables.search(t):
            # A variable this cannot expand. NOT "outside": it may well be
            # inside, and saying so would be a guess stated as a finding.
            return UNRESOLVED
        if ".." in t.split(g.sep):
            return UNRESOLVED       # escaped a relative base
        if not t.startswith(self.norm):
            return OUTSIDE
        tail = t[len(self.norm):]
        # Empty or separator-prefixed only: C:\WORK must not match
        # C:\WORK2, C:\WORKBACKUP or C:\WORK.OLD.
        if tail and not tail.startswith(g.sep):
            return OUTSIDE
        if self.guard.leaf_is_device(t):
            return DEVICE
        return INSIDE

    def contains(self, token: str) -> bool:
        """`check(token) == INSIDE`. Kept because "is it inside" is a
        question worth being able to ask in one word."""
        return self.check(token) == INSIDE

    def fault(self, token: str, name: str, outside: str,
              verb: str = "writes") -> str | None:
        """One finished sentence for a path that is not INSIDE, or None.

        `name` is the token as the MODEL wrote it, not as `norm` rewrote it
        -- shouting a path back in upper case loses information for no gain.
        `outside` is the caller's own sentence for the ordinary case, with
        one `%s` for the path, because "writes outside the mirrored folder:
        x" and "removes x, which is outside the mirrored folder" are the
        same finding in two grammars and both were already on screen. The
        other two cases are this module's to word, because until 02.11
        neither had any words of its own.
        """
        bad = self.check(token)
        if bad == INSIDE:
            return None
        if bad == DEVICE:
            return ("%s %s, which is a device name reserved in every "
                    "directory -- so it is not a file in the mirrored folder "
                    "even though the path looks like one" % (verb, name))
        if bad == UNRESOLVED:
            return "%s %s, which cannot be resolved" % (verb, name)
        return outside % (name,)


def deployment_gap(*, guest_root: str = "", shell: str = "",
                   caps=None) -> str | None:
    """Why this bridge cannot judge ANY command here, or None.

    A PROPERTY OF THE DEPLOYMENT, NOT OF A COMMAND (02.4). Both sentences
    below used to come back from `reason()` as findings about the command
    line itself, so a bridge started without `--sync-dir` prefixed EVERY
    exec prompt with "no folder is mirrored from this machine..." -- 73
    characters in front of a bar that draws one clipped line, so the user saw
    the warning and nothing of the command being approved.

    A warning that fires on 100% of calls is the defect this module's own
    note describes ("someone who learns the guard's phrasing is wrong stops
    reading it"), plus it removes the one piece of information the decision
    rests on. So the caller says this ONCE, when the connection is in this
    state, and the per-command answer stays about the command.
    """
    guard = Guard(grammar_for(shell), caps)
    if not guard.g.usable:
        return ("the shell on this machine is not one this bridge has rules "
                "for, so what a command touches cannot be worked out here")
    if not Root(guard, guest_root).norm:
        return ("no folder is mirrored from this machine, so nothing a "
                "command changes on it can be restored")
    return None


def drive_root_write(path: str, *, shell: str = "", caps=None) -> str | None:
    r"""Why writing to this path puts a file in the top level of a disk, or None.

    NOT ABOUT THE MIRROR, and that is why it is separate from `reason` and
    from `deployment_gap`. A machine with no mirror at all still has a drive
    root, and the root is still the wrong place for a file: nothing sweeps
    it, no installer owns it, and what lands there outlives the conversation
    that put it there. On 2026-10-01 an agent on real hardware -- told
    correctly that nothing was mirrored, and told nothing at all about where
    to write instead -- `put_file`d straight into `C:\`. `docs/REMOTE.md`
    §"Where things go on a target machine" exists because the QEMU guest's
    `C:\` had already become a midden the same way.

    THE SAME GRAMMAR AS EVERYTHING ELSE HERE: the drive pattern and the
    separator come from the shell's profile, so this says `C:\X.TXT` on
    COMMAND.COM and `/x.txt` on a POSIX machine without either spelling
    being written down in Python. An unusable grammar answers None, like
    every other judgement in this file -- and like every other judgement in
    this file, None is "nothing found", never a grant.

    A RELATIVE PATH ANSWERS None TOO. The bridge is never told the guest's
    working directory (see `Guard.resolve`), so `x.txt` may or may not land
    in a root and saying either would be inventing the answer.

    This warns; it does not refuse. `C:\AUTOEXEC.BAT` and `C:\CONFIG.SYS`
    are real files a person may genuinely want edited, and a path check
    cannot tell one of those from a scratch file -- only the person reading
    the prompt can. So it does what the rest of this module does: turns a
    would-be-silent call into a visible one.
    """
    guard = Guard(grammar_for(shell), caps)
    g = guard.g
    if not g.usable:
        return None
    t = guard.norm(path)
    if not t:
        return None
    drive = ""
    if g.drive is not None:
        m = g.drive.match(t)
        if m:
            drive, t = t[:m.end()], t[m.end():]
    if not t.startswith(g.sep):
        return None                 # relative: unresolvable, so unjudgeable
    leaf = t[len(g.sep):]
    if not leaf or g.sep in leaf:
        return None                 # the root itself, or inside some folder
    # SHORT ON PURPOSE. The bar hands a warning 68 characters
    # (`permdesk.BAR_BYTES` less the command's share), so a long sentence is
    # a sentence nobody reads the end of -- and the one word that has to
    # survive the clip is where the file is going.
    return ("writes %s into the top level of %s, not into any folder -- is "
            "that where it belongs?" % (t, drive + g.sep))


def reason(command: str, *, cwd: str = "", guest_root: str = "",
           shell: str = "", caps=None, announce_gaps: bool = True) -> str | None:
    """Why this command line may reach outside the mirror, or None.

    None means "nothing found", NOT "safe". Nothing here is ever a grant.

    Anything unresolvable answers a reason. That is the whole discipline: the
    failure mode of a miss is one extra prompt, and the failure mode of a
    false "contained" is a write nobody was asked about.

    `shell` names the profile whose grammar to judge by -- detection already
    reports it, and `toolrules.inventory` sorts shells first for exactly this.
    `caps` is the client's declared path semantics; omitting it assumes the
    restrictive default, which is the safe direction but is an assumption.

    `announce_gaps=False` says the caller has already reported the two
    DEPLOYMENT-level gaps itself (see `deployment_gap`) and does not want
    them repeated on every command. The verdict is unchanged either way:
    neither answer was ever a grant, and the caller asks the user regardless.
    """
    if not command or not command.strip():
        return None

    gap = deployment_gap(guest_root=guest_root, shell=shell, caps=caps)
    if gap is not None:
        return gap if announce_gaps else None

    guard = Guard(grammar_for(shell), caps)
    g = guard.g
    root = Root(guard, guest_root)
    cwd_norm = guard.norm(cwd) if cwd else ""

    # `deployment_gap` above already returned unless `guard.g.usable`, which
    # requires exactly this to be set -- same shell, same cached Grammar, so
    # what it already checked holds here too. mypy cannot see across the
    # earlier call, so this narrows what deployment_gap has already proven.
    assert g.separators is not None
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
                bad = root.fault(resolved, target,
                                 "writes outside the mirrored folder: %s")
                if bad:
                    return bad

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
            targets, dropped = guard.targets(stem, args)
            if not targets:
                return "%s with no destination this can resolve" % leaf
            # THE DESTINATION WAS SWALLOWED. 02.10: a verb whose destination
            # is positional, one surviving path, and something discarded as a
            # switch -- `copy c:\ok\a.txt /temp` -- is indistinguishable
            # from an honest one-argument `copy a.txt`, and the branch below
            # would measure containment against the SOURCE and pass. Say the
            # same thing `del` already says when every argument vanishes.
            if (dropped and (stem in g.src_dest or stem in g.move_like)
                    and len([a for a in args
                             if not guard.is_switch(a)]) < 2):
                return ("%s with no destination this can resolve (%s could be "
                        "a switch or a path)" % (leaf, dropped[-1]))
            for t in targets:
                resolved = guard.resolve(t, cwd_norm)
                if not resolved:
                    return "writes to %s, which cannot be resolved" % t
                bad = root.fault(resolved, t,
                                 "writes outside the mirrored folder: %s")
                if bad:
                    return bad
            # A `move`-like verb removes its sources as well as writing.
            if stem in g.move_like:
                for t in [a for a in args if not guard.is_switch(a)][:-1]:
                    resolved = guard.resolve(t, cwd_norm)
                    if not resolved:
                        return "removes %s, which cannot be resolved" % t
                    bad = root.fault(
                        resolved, t,
                        "removes %s, which is outside the mirrored folder",
                        verb="removes")
                    if bad:
                        return bad
            continue

        if stem in g.conditional_read:
            # `attrib x` reports; `attrib -r x` sets. Only the second writes.
            writes = g.write_if.get(stem)
            if writes is None or not writes.search(part):
                continue
            for t in [a for a in args if not guard.is_switch(a)]:
                resolved = guard.resolve(t, cwd_norm)
                if not resolved:
                    return ("changes attributes on %s, which cannot be "
                            "resolved" % t)
                bad = root.fault(
                    resolved, t,
                    "changes attributes outside the mirrored folder: %s",
                    verb="changes attributes on")
                if bad:
                    return bad
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
