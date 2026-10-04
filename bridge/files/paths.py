"""Path rules for the sync layer.

Names are the hard part of this feature, not the reconciliation. Five review
rounds found more defects here than anywhere else, so the rules are spelled out
rather than inferred.

THREE THINGS THAT ARE NOT NEGOTIABLE:

1. **Identity is raw bytes.** A path's identity is the bytes the owning side
   holds -- ANSI on the guest, whatever the filesystem gave us on Linux. Decoded
   strings are for display only. `fold.decode` is lossy by construction (it maps
   the five undefined CP1252 positions to U+FFFD, which are legal in a VFAT long
   name), so once decoded the bytes cannot be reconstructed to ask for the file
   back.

2. **Paths never go through `fold`.** `protocol._quote` calls `fold.encode`,
   which rewrites characters CP1252 can represent perfectly well: U+2044 becomes
   "/" -- a path separator invented from nothing -- and U+00B7 becomes ".".
   Paths therefore travel in frame BODIES, never headers.

3. **Anything that cannot cross is skipped with a reason, never renamed.**
   Silently turning `what?.txt` into `what_.txt` creates a file the agent did not
   ask for and can never find again.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass

from . import caps as C

# No local copy of MAX_PATH/MAX_DIR_PATH/MAX_COMPONENT here (03.20): `check`
# and `_component_ok` below read the negotiated `caps` object instead
# (`caps.maxpath`, `caps.maxdirpath`, `caps.maxcomp`). The directory reserve
# of 12 was found empirically -- the bridge allowed a 256-byte path and the
# guest answered `too_long` to the SYNCMKDIR, proof the receiver cannot be
# the limit check since it can't create the file to report the problem --
# which is why `check` below refuses a too-long directory BEFORE anything is
# sent, using `caps.dirreserve`/`caps.maxdirpath` rather than a hardcoded 12
# or 260.

# The guest's own walk (win/sync/sync.h RC_SYNC_MAX_DEPTH, win/sync/sync.cpp)
# refuses to recurse past this many directory levels and says NOTHING when it
# stops. A push deeper than this still lands and is acked, and the baseline
# advances -- but the next scan never visits it again, since the directory
# that would need recursing into to find it is past where the walk gives up.
# That reads as "deleted on the guest, kept here" forever, with nothing
# anywhere saying why (03.25).
#
# This is the only place it can be caught, before the first push -- the guest
# gives no signal when its walk gives up. `caps.maxdepth` is one MORE than
# the guest's own zero-based recursion depth: with RC_SYNC_MAX_DEPTH == 16
# the walk still lists the children of the 17th level (enumerated by the call
# listing their parent), so nothing is invisible until the 18th component.
#
# Declared (04.36), not assumed: lives in `caps.maxdepth`; this is only the
# default a client that says nothing is assumed to have.
MAX_DEPTH = C.MAXDEPTH_GASTON

# Reserved device names on Windows 9x. These are devices **in every directory**
# and **with any extension** -- `aux.c` is the AUX device, not a file, and
# `aux.h`/`con.py` are ordinary names in real source trees.
#
# CreateFileA on AUX or COM1 with no device asserting DSR BLOCKS WITH NO
# TIMEOUT, which would wedge the RPC worker -- the exact failure class already
# shipped and caught once in Stage B. NUL succeeds and discards, so a CRC
# read-back mismatches forever.
_RESERVED = frozenset(
    ["con", "prn", "aux", "nul", "clock$"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

# Legal on ext4, rejected or misinterpreted by CreateFileA.
_ILLEGAL_DOS = set('<>:"\\|?*')

# Universal, and NOT a client property. NUL ends a C string; 0x0A ends a
# manifest line, and the bridge splits the manifest body on "\n" before it ever
# reads an entry's length prefix -- so a newline in a name breaks the parse
# whatever the filesystem allows. "\\" stays illegal everywhere because the
# wire separator is "/" and a backslash must never arrive inside a component.
_ILLEGAL_ALWAYS = {chr(c) for c in range(0x20)} | {"\\"}


#: The Win9x rules, for a caller that has no client to ask. `caps.py` used to
#: import this module for the codepage table, which made a real `caps.Caps`
#: here a cycle; the table now lives in `caps.py` itself, so this is just the
#: real thing.
DEFAULT = C.CONSTRAINED


@dataclass(frozen=True)
class Skip:
    """Why a path cannot cross. Reported to the agent, never worked around."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def illegal_chars(caps=DEFAULT) -> set:
    """Every character this client cannot hold in a name.

    Public because `server.folder_name` had its own literal copy of this set
    -- `("/", "\\", ":", "..", "*", "?", '"', "<", ">", "|")` -- written out
    while `caps.illegal` sat negotiated and unread two modules away.
    """
    return _ILLEGAL_ALWAYS | (_ILLEGAL_DOS if caps.illegal else set())


def component_ok(name: str, caps=DEFAULT) -> Skip | None:
    """Is this a name the client can hold? `None` means yes.

    The public face of the check `check()` runs per component, for callers
    that have one name rather than a path -- naming a conversation's folder,
    say. Reserved devices, illegal characters, trailing dots and length all
    come from `caps`, so there is one answer to this question rather than a
    second opinion per caller.
    """
    return _component_ok(name, caps)


def _component_ok(name: str, caps=DEFAULT) -> Skip | None:
    if not name:
        return Skip("empty path component")
    if name in (".", ".."):
        return Skip("relative component", name)

    # Reserved check is on the stem, because the extension does not save you.
    stem = name.split(".", 1)[0].lower()
    if caps.devices and stem in _RESERVED:
        return Skip(
            "reserved device name",
            f"{name!r} -- {stem.upper()} is a device on the connected "
            f"machine, in every directory and whatever the extension. "
            f"Opening it can block forever.",
        )

    illegal = _ILLEGAL_ALWAYS | (_ILLEGAL_DOS if caps.illegal else set())
    bad = sorted({c for c in name if c in illegal})
    if bad:
        return Skip("illegal character", f"{name!r} contains {bad!r}")

    # Windows silently strips these, so `notes.` and `notes` become one file --
    # a silent overwrite followed by permanent oscillation.
    if caps.trailing and name[-1] in (".", " "):
        return Skip("trailing dot or space",
                    f"{name!r} -- Windows strips it, colliding with {name.rstrip('. ')!r}")

    # The limit is on the bytes the GUEST'S filesystem stores, which are its
    # ANSI codepage, not UTF-8. Measuring in UTF-8 over-counts for cp1252
    # (`café.md` is 9 bytes there and 10 in UTF-8) and under-counts nothing --
    # but for a Shift-JIS or UTF-8 client it is wrong in the other direction,
    # which would let a name through that the guest cannot hold.
    try:
        n = len(name.encode(caps.acp, "surrogateescape"))
    except (UnicodeEncodeError, LookupError):
        # Not representable at all. Fall through: the codepage check below
        # gives a far more useful reason than "too long".
        n = len(name)
    if n > caps.maxcomp:
        return Skip("component too long", f"{n} bytes in {caps.acp}")
    return None


def check(relpath: str, *, guest_root: str, caps=DEFAULT,
          is_dir: bool = False) -> Skip | None:
    """Can this relative path exist on the client? None means yes.

    `relpath` uses "/" as the separator regardless of side; the client joins
    with its own separator, which is why "\\" is illegal everywhere -- it must
    never arrive inside a component.

    `caps` is what the client said it is (`bridge/files/caps.py`). Every rule below
    that varies between clients reads it; the ones that do not vary are stated
    as constants and are deliberately NOT negotiable.

    `is_dir` applies the client's directory reservation -- twelve characters on
    Win32, so an 8.3 name still fits inside; none on Unix.
    """
    # A drive spec is "X:" followed by a separator or nothing -- NOT any colon
    # in the second position. `a:b.txt` is an illegal-character case, and
    # reporting it as "absolute path" would send someone looking for the wrong
    # bug.
    if relpath.startswith("/") or re.match(r"^[A-Za-z]:([\\/]|$)", relpath):
        return Skip("absolute path", relpath)

    parts = relpath.split("/")
    for part in parts:
        bad = _component_ok(part, caps)
        if bad is not None:
            return bad

    # 03.25: past MAX_DEPTH + 1 components the guest's own walk would visit
    # this path once, on the pass that first lists its parent, and then never
    # again -- see MAX_DEPTH above. Refusing here, before the first push, is
    # the only point where that is catchable: the guest gives no signal when
    # its walk gives up, so there is nothing to react to afterwards, and a
    # path that slipped through would look identical to an ordinary file right
    # up until the SECOND pass quietly stopped seeing it.
    if len(parts) > caps.maxdepth + 1:
        return Skip(
            "path too deep",
            f"{len(parts)} components under {guest_root!r}; the machine's "
            f"own scanner does not recurse past {caps.maxdepth + 1}, so "
            f"anything deeper would reach it on a push and then never be "
            f"reported by a scan again")

    # The length that matters is what the guest would hold: its own BYTES,
    # prefix included. `len()` on the decoded string is the same number only
    # for a single-byte codepage -- on a CP932 guest a path of 200 characters
    # can be 300 bytes and would sail past this check.
    #
    # rstrip("/\\") rather than "\\": the separator is the guest's, and a root
    # written with a forward slash is a perfectly ordinary thing for a Unix
    # client to report.
    try:
        root_n = len(guest_root.rstrip("/\\").encode(caps.acp,
                                                     "surrogateescape"))
        rel_n = len(relpath.encode(caps.acp, "surrogateescape"))
    except (UnicodeEncodeError, LookupError):
        root_n, rel_n = len(guest_root.rstrip("/\\")), len(relpath)
    full = root_n + 1 + rel_n + 1
    limit = caps.maxdirpath if is_dir else caps.maxfilepath
    if full > limit:
        if is_dir and caps.dirreserve:
            why = (f" (a directory stops {caps.dirreserve} short of "
                   f"{caps.maxpath}, so a short name still fits inside it)")
        elif not is_dir and caps.workreserve:
            # Not padding: below this the machine's own working names collide
            # with the file or with each other, and the write reports success
            # having changed nothing -- or destroys the file outright. See
            # caps.WORKRESERVE_GASTON.
            why = (f" (a file stops {caps.workreserve} short of "
                   f"{caps.maxpath}, so the machine can still write "
                   f"{'{name}'}.gaston-tmp beside it)")
        else:
            why = ""
        return Skip(
            "path too long",
            f"{full} bytes once prefixed with {guest_root!r}; the limit is "
            f"{limit} including the NUL" + why,
        )

    # The single most common reason a Linux tree cannot cross. Checked LAST so
    # the more specific reasons win when several apply.
    try:
        relpath.encode(caps.acp)
    except (UnicodeEncodeError, LookupError):
        return Skip(
            "not representable on the guest",
            f"{relpath!r} has no form in {caps.acp}, the machine's codepage",
        )
    return None


def canonical(relpath: str, *, caps=DEFAULT) -> str:
    """The matching key. NOT a path to write to.

    The guest's filesystem is case-insensitive and case-preserving, so two
    spellings are one file there and two files on Linux. The key folds them
    together; each side's DISPLAY spelling is carried separately in the manifest
    and is what actually gets written.

    Folding is per component, on the bytes, through the guest's ANSI table --
    not `str.lower()` on a decoded string, which collides (the undefined CP1252
    positions all decode to U+FFFD, so distinct byte-names become one key and one
    of them silently stops syncing), and not ASCII-only lowering, which fails the
    other way (`CAFÉ.MD` and `café.md` do not match for us but DO match for
    Win32, so both push and the second silently overwrites the first).
    """
    # A case-SENSITIVE client has nothing to fold, and folding would be
    # actively wrong there: `Makefile` and `makefile` are two files on a Unix
    # box, and collapsing them to one key makes one of them permanently
    # invisible -- the silent-loss shape this module exists to avoid.
    if not caps.case:
        return relpath
    out = []
    for part in relpath.split("/"):
        try:
            raw = part.encode(caps.acp)
            # Win32's ANSI case folding is byte-wise over the codepage.
            out.append(raw.decode(caps.acp).lower())
        except (UnicodeEncodeError, LookupError):
            out.append(part.lower())
    return "/".join(out)


# --- content classification ------------------------------------------------
#
# Cross-side equality is (type, class, crc). A file's CLASS decides whether its
# CRC is taken on normalised or raw bytes, so the two sides must agree or they
# compare the outputs of different functions forever. The class therefore
# travels in the manifest, computed by whichever side owns the file.

CLASS_TEXT = "t"        # uniform line endings; compared and stored normalised
CLASS_EXACT = "b"       # binary, mixed endings, or lone CR; byte-exact


def crc_for(cls: str, crc_norm: int, crc_raw: int) -> int:
    """Which of the two checksums a class is compared under.

    The mirror of `RC_CRC_FOR` in `win/sync/sync.h`, whose comment says why it is a
    macro there: *"One place, because getting it wrong in one of the four call
    sites is a permanent disagreement, not a glitch."* Python then open-coded
    the same expression at four sites and had no such place. This is it.

    Permanent rather than noisy, because the two sides would be comparing the
    outputs of different functions: the file never converges, and every pass
    rewrites it.
    """
    return crc_norm if cls == CLASS_TEXT else crc_raw


def classify(data: bytes) -> str:
    """Which comparison class this content belongs to.

    A NUL ANYWHERE means byte-exact. Not "in the first 8KB" -- a PDF opens with
    `%PDF-` and can run kilobytes of ASCII before its first stream, and
    normalising one shifts every xref offset. The file is already being read for
    its CRC, so scanning all of it is free.

    Uniform text requires EVERY `\\r` to be followed by `\\n`. That is not
    fussiness: normalisation must be idempotent or the algorithm oscillates.
    `N("\\r\\r\\n") == "\\r\\n"` and `N("\\r\\n") == "\\n"`, so a `\\r\\r\\n` file --
    an ordinary Windows artefact -- would pull, push, pull, push forever,
    rewriting the user's file each time. Lone CR and `\\r\\r\\n` are byte-exact,
    and then `N` has no `\\r` in its output and is idempotent by construction.

    Mixed endings are byte-exact for a different reason: we cannot know which
    the user wanted, and converting would silently rewrite their file while the
    normalised CRC stayed identical, so nothing would report it.
    """
    if b"\x00" in data:
        return CLASS_EXACT
    i = data.find(b"\r")
    while i != -1:
        if i + 1 >= len(data) or data[i + 1] != 0x0A:
            return CLASS_EXACT          # lone CR, or \r\r\n
        i = data.find(b"\r", i + 2)
    has_crlf = b"\r\n" in data
    has_bare_lf = re.search(rb"(?<!\r)\n", data) is not None
    if has_crlf and has_bare_lf:
        return CLASS_EXACT              # mixed
    return CLASS_TEXT


def normalise(data: bytes, cls: str) -> bytes:
    """The bytes a CRC is taken over, and the form stored on the bridge."""
    return data.replace(b"\r\n", b"\n") if cls == CLASS_TEXT else data


#: The native ending for each `caps.eol` value. A tuple indexed by the enum, so
#: adding a client convention is one entry rather than a new branch in three
#: different functions.
_EOL = (b"\n", b"\r\n", b"\r")


def to_guest(data: bytes, cls: str, caps=DEFAULT, *, eol: int | None = None) -> bytes:
    """Bridge form -> client form. Normalise FIRST, then expand.

    Expanding directly would turn `a\\r\\nb` into `a\\r\\r\\nb` and add another CR
    on every pass, forever. The client must also write in BINARY mode -- text
    mode would translate on top of what we already did and reproduce exactly
    that.

    For an LF-native client this is the identity and the whole class-`t`
    machinery costs nothing. That is what a PDP running Unix does with the
    line-ending layer: nothing at all.
    """
    if cls != CLASS_TEXT:
        return data
    flat = data.replace(b"\r\n", b"\n")
    # `eol` overrides the client's convention for ONE path, which is what
    # `.gitattributes` `eol=lf` asks for: the file still compares as text, so
    # an ending-only difference is not an edit, but what lands on the client is
    # what the tree said rather than what the client prefers.
    want = caps.eol if eol is None else eol
    native = _EOL[want] if 0 <= want < len(_EOL) else b"\n"
    return flat if native == b"\n" else flat.replace(b"\n", native)


def to_bridge(data: bytes, cls: str) -> bytes:
    """Guest form -> bridge form."""
    return normalise(data, cls)


def rel_from_posix(root: str, path: str) -> str:
    """Bridge-side absolute path -> the wire's relative form."""
    rel = posixpath.relpath(path, root)
    return rel.replace("\\", "/")


# --- the wire --------------------------------------------------------------
#
# A path's identity is the bytes the GUEST holds, and the guest speaks its ANSI
# codepage. Encoding a path as UTF-8 on the way out is not a detail: `café.md`
# goes out as `caf\xc3\xa9.md`, CreateFileA reads those bytes through CP1252,
# and the guest ends up with a file called `cafÃ©.md`. The next manifest reports
# that name, the bridge has never seen it, so it pulls it -- asking for
# `cafÃ©.md` encoded as UTF-8, which is a third name that exists nowhere. The
# tree grows a filename per pass and the original never syncs again.
#
# `check()` already refuses anything the codepage cannot carry. These two make
# the transfer actually USE that representation. The codepage table itself
# (`codec_for_acp`) lives in `caps.py` now -- it is codepage knowledge, not
# path knowledge, and was only ever here to dodge an import cycle that no
# longer exists.


def wire_encode(relpath: str, *, acp: str = "cp1252") -> bytes:
    """A relative path, as the bytes the guest will receive.

    `surrogateescape` because `wire_decode` produces lone surrogates for the
    five undefined CP1252 positions, which are legal in a VFAT long name. They
    have to survive the trip back or the file can never be asked for again.
    """
    return relpath.encode(acp, "surrogateescape")


def wire_decode(raw: bytes, *, acp: str = "cp1252") -> str:
    """Guest bytes -> the string we key and display on.

    NOT `errors="replace"`. That maps every undefined byte to U+FFFD, so two
    distinct names collapse into one key -- one of them silently stops syncing
    -- and neither can be encoded back to ask the guest for the file.
    `surrogateescape` is exactly reversible by `wire_encode`.
    """
    return raw.decode(acp, "surrogateescape")


# --- conversation folder naming & guest path helpers ------------------------

FOLDER_MAX_LONG = 32


def folder_max(caps=None) -> int:
    """How long this client's conversation folder may be."""
    caps = caps if caps is not None else DEFAULT
    if caps.shortnames:
        return C.SHORTNAME_MAX
    return min(caps.maxcomp, FOLDER_MAX_LONG)


def folder_name(raw: str, caps=None) -> str:
    """A folder name that is safe on both sides, or "" if nothing is left."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    caps = caps if caps is not None else DEFAULT
    bad = illegal_chars(caps) | {"/", ":", ".."}
    if any(b in raw for b in bad):
        return ""
    text = raw.upper() if caps.shortnames else raw
    out = "".join(c for c in text
                  if c.isascii() and (c.isalnum() or c in "-_"))
    out = out.strip("-_")[:folder_max(caps)]
    return "" if out and component_ok(out, caps) else out


def title_folder(title: str, caps=None) -> str:
    """A folder name from a conversation title, reduced not refused."""
    keep = []
    for ch in (title or ""):
        keep.append(ch if (ch.isascii() and (ch.isalnum() or ch in "-_"))
                    else " ")
    return folder_name("".join(keep).replace(" ", ""), caps)


def sep_of(root: str) -> str:
    """The separator the guest's path uses."""
    return "\\" if "\\" in root else "/"


def guest_path(root: str, name: str) -> str:
    r"""`<mirror root><sep><name>`, spelled the way the guest spells it."""
    if not root:
        return ""
    if not name:
        return root
    sep = sep_of(root)
    return root.rstrip("\\/") + sep + name


def sub_of(work_dir: str, sync_dir: str) -> str:
    """A conversation's folder name relative to the mirror, or ""."""
    if not work_dir or not sync_dir:
        return ""
    try:
        rel = posixpath.relpath(work_dir.replace("\\", "/"),
                                sync_dir.replace("\\", "/"))
    except ValueError:
        return ""
    return "" if rel in (".", "") or rel.startswith("..") else rel


_ABS_DRIVE = re.compile(r"^[A-Za-z]:\\")
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\])")


def on_guest(path: str, base: str) -> str | None:
    r"""`path` as the guest should be asked for it, or None if it cannot be.

    An agent writes `proj\a.txt` and means its own folder. The guest
    resolves a relative path against ITS process's current directory,
    which is wherever the client was started from -- on the Athlon,
    2026-10-03, gremote's own install folder, where the agent then created,
    moved and deleted files. So a relative path is joined to `base`, the
    conversation's folder on the machine, HERE.

    None when there is no base to join to, and for the drive-relative
    `C:foo`, whose meaning is the guest's per-drive current directory --
    something nobody on this side knows. A path starting `\` is taken on
    the base's drive.
    """
    p = (path or "").strip()
    # A POSIX absolute path is the PROXY's, never the guest's. Turned into
    # `\tmp\...` it named a folder on the guest's C: that nothing should
    # create -- and put_file then made it (measured on the VM, 2026-10-04).
    # GuestSession.on_guest maps one that is inside the mirror first.
    if p.startswith("/"):
        return None
    p = p.replace("/", "\\")
    if not p:
        return None
    if _ABS_DRIVE.match(p) or p.startswith("\\\\"):
        return p
    if len(p) == 2 and p[1] == ":" and p[0].isalpha():
        return p + "\\"                # the drive itself: its root
    if _DRIVE_RELATIVE.match(p):
        return None
    if not base or not _ABS_DRIVE.match(base):
        return None
    if p.startswith("\\"):
        return ntpath.normpath(base[:2] + p)
    return ntpath.normpath(base.rstrip("\\") + "\\" + p)


def conflict_dir(workdir: str) -> str:
    """Where a conflict in the tree mirrored at `workdir` is staged.

    One level ABOVE the working directory, so a staged copy never syncs back
    to the machine it was staged away from -- and then under the working
    directory's own name. Without that last part every conversation under
    one mirror staged into the same `.gaston/conflict/<path>/`, and two
    conversations each with a `notes.txt` shared one folder of `mine.N`s:
    numbering kept them from overwriting each other, not from being
    mistaken for each other.
    """
    w = workdir.rstrip("/").rstrip(os.sep) if workdir else workdir
    return os.path.join(os.path.dirname(w), ".gaston", "conflict",
                        os.path.basename(w))


def glob_match(pattern: str, path: str, sep: str, fold: bool) -> bool:
    r"""Does `path` match `pattern`, component by component?

    `*` and `?` stay inside one component; a component that is exactly `**`
    matches any number of them, including none. Both sides are expected in
    the same normal form (the caller runs them through the shell grammar's
    `norm`, which resolves `..` and folds separators), and nothing here knows
    what a drive letter is: `C:` is a component like any other.

    GRAMMAR-AGNOSTIC ON PURPOSE. The patterns are the client's (CLAUDE.md:
    the client declares OS facts), and the bridge matches them as opaque
    text. `fnmatch` would do for one component but treats `[` as a class,
    and a folder named `[old]` is a real folder.
    """
    if not sep:
        return False
    if fold:
        pattern, path = pattern.upper(), path.upper()
    pp = [c for c in pattern.split(sep) if c]
    tp = [c for c in path.split(sep) if c]

    def one(p: str, t: str) -> bool:
        # Iterative wildcard match with backtracking on the last `*`.
        i = j = 0
        star, mark = -1, 0
        while j < len(t):
            if i < len(p) and (p[i] == "?" or p[i] == t[j]):
                i += 1
                j += 1
            elif i < len(p) and p[i] == "*":
                star, mark = i, j
                i += 1
            elif star >= 0:
                i = star + 1
                mark += 1
                j = mark
            else:
                return False
        while i < len(p) and p[i] == "*":
            i += 1
        return i == len(p)

    # Memoised over (pattern index, path index): `**` makes this a search,
    # and a pattern with several of them must not go exponential.
    memo: dict = {}

    def walk(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(pp):
            out = j == len(tp)
        elif pp[i] == "**":
            out = walk(i + 1, j) or (j < len(tp) and walk(i, j + 1))
        else:
            out = j < len(tp) and one(pp[i], tp[j]) and walk(i + 1, j + 1)
        memo[key] = out
        return out

    return walk(0, 0)
