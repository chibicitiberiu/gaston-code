"""What a DOS command line reaches outside the mirrored folder.

This is not a classifier and must never become one. Four measurements say so:

  * There is no classifier in our loop. With `can_use_tool` installed, a
    documented blocked-by-default action (`git reset --hard` on a dirty repo)
    executed unchanged in both `default` and `auto` permission mode. The
    auto-mode classifier stands in for the user, and by wiring `can_use_tool`
    we already did.
  * A deterministic path check scored the same as Haiku on 54 hand-written
    DOS cases: both 52/54, both zero dangerous-rated-safe, both with the same
    two false alarms. Two unrelated methods agreeing case-for-case says the
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

WHAT IT PARSES. COMMAND.COM, not bash: no $(), no backticks, no process
substitution. That is what makes the question decidable at all. Note `;` is
NOT a separator in COMMAND.COM -- it was in an early draft, borrowed from a
shell we do not target, and it split `echo Meeting at 3;4 PM` into nonsense.
"""

from __future__ import annotations

import logging
import re

from . import profiles

log = logging.getLogger("bridge.doscmd")

#: Separators COMMAND.COM recognises -- or rather, the ones a command might
#: CONTAIN. Measured on the guest since this was written: `&`, `&&` and `||`
#: are NOT separators to Win9x COMMAND.COM. It answers `Too many parameters
#: - &` and runs nothing, seen in three separate sessions.
#:
#: They stay in this pattern anyway, and the reason is worth keeping straight:
#: this is the GUARD's splitter, not a shell. Its job is to make sure no
#: fragment of a command escapes inspection, so splitting on something that
#: is not a separator can only OVER-split, which fails safe. Removing them
#: would mean a command containing `&` got inspected as one long string.
#: THE TABLES COME FROM A PROFILE; THE LOGIC STAYS HERE. Which verbs read,
#: which write, which device names are reserved -- those are facts about
#: somebody else's computer and live in `bridge/profiles/<shell>.toml`. How a
#: command is split, resolved and judged is this file's job and is not
#: configurable, because a guard whose logic is data is a guard anyone can
#: edit into agreeing with them.
#:
#: Selected by SHELL NAME, and the name is NOT written here. While these
#: tables were literals in this module the tool surface was portable and its
#: guard was not: point the bridge at NT 4 or a 4DOS box and it would still
#: have judged commands by one shell's rules. Adding a shell is now adding a
#: file -- and naming the fallback in code would have put that shell back in
#: Python by the back door.
#:
#: So the fallback is derived: if exactly one profile carries guard tables,
#: that is the shell this bridge knows and there is nothing to choose. With
#: several, a caller that does not say which gets none, and every command is
#: referred to the user -- the safe direction, and loudly enough to notice.
def _default_shell() -> str:
    known = profiles.shells()
    if len(known) == 1:
        return known[0]
    if not known:
        log.error("no shell profile carries guard tables")
    else:
        log.error("%d shells have guard tables (%s); a caller must say which",
                  len(known), ", ".join(sorted(known)))
    return ""


def _tables(shell: str = "") -> dict:
    shell = shell or _default_shell()
    if not shell:
        return {}
    t = profiles.shell(shell)
    if not t:
        # Not a crash, and not silence either. With no tables every command
        # falls through to "I cannot classify this", which asks the user --
        # the safe direction. Saying so once is what stops that reading as
        # the model having become suspicious of everything.
        log.error("no guard tables for shell %r; every command will be "
                  "referred to the user", shell)
    return t


_T = _tables()


def _fs(key: str) -> frozenset:
    return frozenset(_T.get(key, ()))


def _re(key: str, fallback: str) -> "re.Pattern":
    return re.compile(_T.get(key, fallback))


#: Fragments a command might be split on -- see the profile for why `&` is in
#: here although this shell does not honour it.
_SEPARATORS = _re("separators", r"&&|\|\||[&|]")

#: Redirection. `<` is an INPUT redirect -- a read -- and an early draft
#: called it a write, which produced the reason string "redirect outside
#: the working copy" for a command that only read.
_REDIR_WRITE = _re("redirect_write", r"(>>?)\s*(\S+)")
_REDIR_READ = _re("redirect_read", r"(<)\s*(\S+)")
_DRIVE = _re("drive", r"^[A-Za-z]:")

_READ = _fs("read")
_WRITE = _fs("write")
_SRC_DEST = _fs("src_dest")
_CONDITIONAL_READ = _fs("conditional_read")
_UNBOUNDED = _fs("unbounded")
_DEVICES = _fs("devices")

#: Always ask, whatever the arguments: either the damage is not named by any
#: argument, or the command changes what a path MEANS and so invalidates
#: every later verdict. The value is the reason shown to the user.
_ALWAYS_ASK: dict = dict(_T.get("always_ask", {}))


def _split_args(part: str) -> list[str]:
    """Tokenise respecting double quotes.

    `part.split()` breaks `copy "C:\\WORK\\Chat Log.txt" ...` into four
    tokens, and the guard then reported `Log.txt"` as a path that is outside
    the mirror. The verdict was right by accident and the REASON shown to the
    user was nonsense -- which is worse than useless, because someone who
    learns the guard's phrasing is wrong stops reading it.
    """
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in part:
        if ch == '"':
            quoted = not quoted
            continue
        if ch.isspace() and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _norm(token: str) -> str:
    """A guest path in comparable form: upper, backslashes, `.`/`..` resolved.

    NOT os.path. POSIX semantics are wrong for these strings by construction
    -- os.path.realpath("C:\\WINDOWS") on Linux yields "$CWD/C:\\WINDOWS",
    i.e. it reports a guest system path as living INSIDE the working copy,
    which is the dangerous direction to be wrong in. `off_limits` carries
    that latent today.

    Resolving `..` is the whole point: `C:\\WORK\\..\\..\\WINDOWS\\WIN.INI`
    starts with the root and its tail starts with a separator, so a prefix
    test alone calls it contained. A red team found nine variants of exactly
    that and nothing else.
    """
    t = token.strip().strip('"').upper().replace("/", "\\")
    drive = ""
    if _DRIVE.match(t):
        drive, t = t[:2], t[2:]
    rooted = t.startswith("\\")

    out: list[str] = []
    for part in t.split("\\"):
        if not part or part == ".":
            continue
        if part == "..":
            if out:
                out.pop()
            elif rooted:
                continue            # `..` at the root is the root
            else:
                out.append("..")    # escapes a relative base; unresolvable
            continue
        out.append(part)
    return drive + ("\\" if rooted else "") + "\\".join(out)


def _looks_like_path(token: str) -> bool:
    return bool(_DRIVE.match(token) or "\\" in token or "/" in token)


def _leaf_is_device(path: str) -> bool:
    leaf = path.rsplit("\\", 1)[-1]
    return leaf.split(".", 1)[0].lower() in _DEVICES


class _Root:
    """The mirrored folder, normalised once."""

    def __init__(self, guest_root: str) -> None:
        self.raw = (guest_root or "").strip()
        self.norm = _norm(self.raw).rstrip("\\") if self.raw else ""

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
        t = _norm(token)
        if "%" in t:
            return False            # %COMSPEC%, %WINDIR% -- unresolvable here
        if ".." in t.split("\\"):
            return False            # escaped a relative base
        if not t.startswith(self.norm):
            return False
        tail = t[len(self.norm):]
        # Empty or separator-prefixed only: C:\WORK must not match
        # C:\WORK2, C:\WORKBACKUP or C:\WORK.OLD.
        if tail and not tail.startswith("\\"):
            return False
        return not _leaf_is_device(t)


def _is_switch(token: str) -> bool:
    """A DOS switch, not a path.

    `/` starts a switch AND is accepted as a path separator by everything
    that goes through CreateFileA, so `/windows/system/x` is a real path. An
    early draft discarded every `/`-prefixed token before extracting paths,
    which made that path not merely unresolvable but invisible -- the guard
    reported "writes inside mirror" for a command whose destination it had
    thrown away. A switch is short and has no separator in it.
    """
    if not token.startswith("/"):
        return False
    return "\\" not in token and "/" not in token[1:] and len(token) <= 8


def _resolve(token: str, cwd: str) -> str:
    """An argument as an absolute guest path, or "" if it cannot be one."""
    t = token.strip().strip('"')
    if not t:
        return ""
    if _DRIVE.match(t) or t.startswith("\\") or t.startswith("/"):
        return t
    if not cwd:
        # The bridge is never told the guest's working directory: there is no
        # CWD on the wire in either direction, each EXEC is a fresh
        # `command.com /c`, and an empty cwd makes the child inherit the GUI
        # process's directory -- a value nobody here has ever seen. So a
        # relative path with no cwd is genuinely unresolvable.
        return ""
    return cwd.rstrip("\\") + "\\" + t


def _targets(verb: str, args: list[str]) -> list[str]:
    """Which arguments this verb WRITES to."""
    paths = [a for a in args if not _is_switch(a)]
    if not paths:
        return []
    if verb in _SRC_DEST:
        # `copy a b` writes b. `copy a` writes into the working directory,
        # which is still a write and still has to be checked.
        return paths[-1:] if len(paths) > 1 else paths
    return paths            # del/deltree/ren/md/rd/move: every argument


def reason(command: str, *, cwd: str = "", guest_root: str = "") -> str | None:
    """Why this command line may reach outside the mirror, or None.

    None means "nothing found", NOT "safe". Nothing here is ever a grant.

    Anything unresolvable answers a reason. That is the whole discipline: the
    failure mode of a miss is one extra prompt, and the failure mode of a
    false "contained" is a write nobody was asked about.
    """
    if not command or not command.strip():
        return None

    root = _Root(guest_root)
    if not root.norm:
        return ("no folder is mirrored from this machine, so nothing on it "
                "can be restored")

    cwd_norm = _norm(cwd) if cwd else ""

    for raw in _SEPARATORS.split(command):
        part = raw.strip()
        if not part:
            continue

        # Redirection first: a write target is a write wherever it appears,
        # and it is checked even for a read-only verb -- `dir c:\ > c:\x`
        # writes c:\x.
        for _, target in _REDIR_WRITE.findall(part):
            resolved = _resolve(target, cwd_norm)
            if not resolved:
                return "writes to %s, which cannot be resolved" % target
            if not root.contains(resolved):
                return "writes outside the mirrored folder: %s" % target

        words = _split_args(part)
        if not words:
            continue
        verb = words[0].strip('"').lower()
        # A pathed invocation names the program, not a builtin.
        leaf = verb.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        stem = leaf.rsplit(".", 1)[0] if leaf.endswith((".exe", ".com", ".bat"))\
            else leaf

        if stem in _ALWAYS_ASK:
            # Two read-only forms are worth not nagging about -- and they
            # must CONTINUE, not fall through: neither verb is in _READ or
            # _WRITE, so a `pass` here dropped them into the unknown-program
            # branch and `fdisk /status` reported as unreadable.
            upper = part.upper()
            if stem == "fdisk" and "/STATUS" in upper:
                continue
            if stem == "chkdsk" and "/F" not in upper:
                continue
            return "%s: %s" % (leaf, _ALWAYS_ASK[stem])

        if stem in _UNBOUNDED:
            return ("%s runs whatever it is given, so what it touches cannot "
                    "be known from this command line" % leaf)

        args = [w for w in words[1:]]

        if stem in _WRITE:
            targets = _targets(stem, args)
            if not targets:
                return "%s with no destination this can resolve" % leaf
            for t in targets:
                resolved = _resolve(t, cwd_norm)
                if not resolved:
                    return "writes to %s, which cannot be resolved" % t
                if not root.contains(resolved):
                    return "writes outside the mirrored folder: %s" % t
            # `move` removes its source as well as writing its destination.
            if stem == "move":
                for t in [a for a in args if not _is_switch(a)][:-1] or []:
                    resolved = _resolve(t, cwd_norm)
                    if not resolved or not root.contains(resolved):
                        return "removes %s, which is outside the mirrored " \
                               "folder" % t
            continue

        if stem in _CONDITIONAL_READ:
            # `attrib x` reports; `attrib -r x` sets. Only the second writes.
            if not re.search(r"[-+][RSHArsha]", part):
                continue
            targets = [a for a in args if not _is_switch(a)]
            for t in targets:
                resolved = _resolve(t, cwd_norm)
                if not resolved or not root.contains(resolved):
                    return ("changes attributes outside the mirrored "
                            "folder: %s" % t)
            continue

        if stem in _READ:
            continue            # reads are not guarded; see the module note

        # An unknown verb is an arbitrary program: a .bat the agent wrote, a
        # compiler, anything. It is the irreducible residue -- the same hole
        # Anthropic documents for `npx` and `devbox run` -- and the honest
        # answer is to ask.
        return ("%s is a program this cannot read, so what it touches is "
                "unknown" % leaf)

    return None
