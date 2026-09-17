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
_ILLEGAL = set('<>:"\\|?*') | {chr(c) for c in range(0x20)}


@dataclass(frozen=True)
class Skip:
    """Why a path cannot cross. Reported to the agent, never worked around."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def _component_ok(name: str) -> Skip | None:
    if not name:
        return Skip("empty path component")
    if name in (".", ".."):
        return Skip("relative component", name)

    # Reserved check is on the stem, because the extension does not save you.
    stem = name.split(".", 1)[0].lower()
    if stem in _RESERVED:
        return Skip(
            "reserved device name",
            f"{name!r} -- on Windows 9x this is the {stem.upper()} device in "
            f"every directory, whatever the extension. Opening it can block "
            f"forever.",
        )

    bad = sorted({c for c in name if c in _ILLEGAL})
    if bad:
        return Skip("illegal character", f"{name!r} contains {bad!r}")

    # Windows silently strips these, so `notes.` and `notes` become one file --
    # a silent overwrite followed by permanent oscillation.
    if name[-1] in (".", " "):
        return Skip("trailing dot or space",
                    f"{name!r} -- Windows strips it, colliding with {name.rstrip('. ')!r}")

    if len(name.encode("utf-8", "replace")) > MAX_COMPONENT:
        return Skip("component too long", f"{len(name)} characters")
    return None


def check(relpath: str, *, guest_root: str, acp: str = "cp1252") -> Skip | None:
    """Can this relative path exist on the guest? None means yes.

    `relpath` uses "/" as the separator regardless of side; the guest joins with
    backslashes itself, which is why "\\" is on the illegal list -- it must never
    arrive inside a component.
    """
    # A drive spec is "X:" followed by a separator or nothing -- NOT any colon
    # in the second position. `a:b.txt` is an illegal-character case, and
    # reporting it as "absolute path" would send someone looking for the wrong
    # bug.
    if relpath.startswith("/") or re.match(r"^[A-Za-z]:([\\/]|$)", relpath):
        return Skip("absolute path", relpath)

    parts = relpath.split("/")
    for part in parts:
        bad = _component_ok(part)
        if bad is not None:
            return bad

    # The length that matters is what the guest would hold, prefix included.
    full = len(guest_root.rstrip("\\")) + 1 + len(relpath) + 1
    if full > MAX_PATH:
        return Skip(
            "path too long",
            f"{full} bytes once prefixed with {guest_root!r}; the limit is "
            f"{MAX_PATH} including the NUL",
        )

    # The single most common reason a Linux tree cannot cross. Checked LAST so
    # the more specific reasons win when several apply.
    try:
        relpath.encode(acp)
    except (UnicodeEncodeError, LookupError):
        return Skip(
            "not representable on the guest",
            f"{relpath!r} has no form in {acp}, the guest's ANSI codepage",
        )
    return None


def canonical(relpath: str, *, acp: str = "cp1252") -> str:
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
    out = []
    for part in relpath.split("/"):
        try:
            raw = part.encode(acp)
            # Win32's ANSI case folding is byte-wise over the codepage.
            out.append(raw.decode(acp).lower())
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


def to_guest(data: bytes, cls: str) -> bytes:
    """Bridge form -> guest form. Normalise FIRST, then expand.

    Expanding directly would turn `a\\r\\nb` into `a\\r\\r\\nb` and add another CR
    on every pass, forever. The guest must also write in BINARY mode -- text
    mode would translate on top of what we already did and reproduce exactly
    that.
    """
    if cls != CLASS_TEXT:
        return data
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def to_bridge(data: bytes, cls: str) -> bytes:
    """Guest form -> bridge form."""
    return normalise(data, cls)


def case_collision(existing: dict[str, str], relpath: str,
                   *, acp: str = "cp1252") -> str | None:
    """Does `relpath` collide, case-insensitively, with something already known?

    Returns the established path it collides with, or None.

    The NEWCOMER is refused, never both. "Refuse both" has no memory: it would
    retroactively break a path that has been syncing correctly for weeks because
    someone later added a file differing only in case.
    """
    key = canonical(relpath, acp=acp)
    other = existing.get(key)
    return other if other is not None and other != relpath else None


def join_guest(root: str, relpath: str) -> str:
    """Build the guest-side path. Separator conversion happens only here."""
    return root.rstrip("\\") + "\\" + relpath.replace("/", "\\")


def rel_from_posix(root: str, path: str) -> str:
    """Bridge-side absolute path -> the wire's relative form."""
    rel = posixpath.relpath(path, root)
    return rel.replace("\\", "/")
