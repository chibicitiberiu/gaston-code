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

import posixpath
import re
from dataclasses import dataclass

# Win98 MAX_PATH, including the drive, the root prefix and the NUL. The check is
# always against the FULL path the guest would hold, never the relative part.
MAX_PATH = 260

# A DIRECTORY has to stop twelve characters earlier. CreateDirectory refuses a
# path within 12 of MAX_PATH so that an 8.3 name still fits inside it -- an
# ancient rule, still enforced, and not derivable from MAX_PATH.
#
# Found the honest way: the bridge measured 256 bytes, allowed it, and the
# guest answered `too_long` to the SYNCMKDIR. Letting the receiver be the
# limit check is exactly what the design says not to do -- the guest cannot
# create the file in order to report the problem, so the report has to come
# from here, with the path and the reason, before anything is sent.
MAX_DIR_PATH = MAX_PATH - 12

# VFAT and ext4 both stop here.
MAX_COMPONENT = 255

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


class _Default:
    """The Win9x rules, for a caller that has no client to ask.

    Duck-typed rather than imported from `caps`, because `caps` imports THIS
    module for the codepage table and a cycle here would be gratuitous.
    """

    acp = "cp1252"
    case = 1
    devices = 1
    trailing = 1
    illegal = 1
    eol = 1
    dirreserve = 12
    workreserve = 11
    maxpath = 260
    maxcomp = 255

    @property
    def maxdirpath(self):
        return self.maxpath - self.dirreserve

    @property
    def maxfilepath(self):
        return self.maxpath - self.workreserve


DEFAULT = _Default()


@dataclass(frozen=True)
class Skip:
    """Why a path cannot cross. Reported to the agent, never worked around."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


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
            f"{name!r} -- on Windows 9x this is the {stem.upper()} device in "
            f"every directory, whatever the extension. Opening it can block "
            f"forever.",
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

    `caps` is what the client said it is (`bridge/caps.py`). Every rule below
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


def case_collision(existing: dict[str, str], relpath: str,
                   *, caps=DEFAULT) -> str | None:
    """Does `relpath` collide, case-insensitively, with something already known?

    Returns the established path it collides with, or None.

    The NEWCOMER is refused, never both. "Refuse both" has no memory: it would
    retroactively break a path that has been syncing correctly for weeks because
    someone later added a file differing only in case.
    """
    key = canonical(relpath, caps=caps)
    other = existing.get(key)
    return other if other is not None and other != relpath else None


def join_guest(root: str, relpath: str) -> str:
    """Build the guest-side path. Separator conversion happens only here."""
    return root.rstrip("\\") + "\\" + relpath.replace("/", "\\")


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
# the transfer actually USE that representation.

#: What a Win32 GetACP() number means to Python. Only the pages a machine of
#: this era can actually be set to; anything else is refused rather than
#: guessed, because a wrong codepage is the one failure in this layer that
#: round-trips perfectly and corrupts silently.
_ACP_CODECS = {
    437: "cp437", 850: "cp850", 852: "cp852", 855: "cp855", 857: "cp857",
    858: "cp858", 860: "cp860", 861: "cp861", 862: "cp862", 863: "cp863",
    865: "cp865", 866: "cp866", 869: "cp869", 874: "cp874",
    932: "cp932", 936: "cp936", 949: "cp949", 950: "cp950",
    1250: "cp1250", 1251: "cp1251", 1252: "cp1252", 1253: "cp1253",
    1254: "cp1254", 1255: "cp1255", 1256: "cp1256", 1257: "cp1257",
    1258: "cp1258",
    65001: "utf-8",
}


def codec_for_acp(value: str | int | None) -> str | None:
    """A guest's reported ANSI codepage -> a Python codec name, or None.

    None means "I do not know what that is", and the caller must treat that as
    a reason to say so rather than to assume. Assuming cp1252 for an unknown
    number is exactly the silent path this function exists to close: the bridge
    would encode a name with the wrong table, the guest would store those
    bytes, report them back, and the bridge would decode with the same wrong
    table and see the name it sent. Self-consistent, undetectable, and the
    user's file is called something else on their own disk.
    """
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    name = _ACP_CODECS.get(n)
    if name is None:
        return None
    try:
        import codecs
        codecs.lookup(name)
    except LookupError:
        return None
    return name


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
