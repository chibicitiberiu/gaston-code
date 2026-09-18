"""What the client at the other end actually is.

The path rules in `paths.py` were Windows 9x rules with Windows 9x numbers
baked in: CP1252, MAX_PATH 260, the DOS device names, `< > : " \\ | ? *`,
trailing dots stripped, case-insensitive, CRLF. Every one of those is a
property of the CLIENT, and the bridge is meant to survive being pointed at
MS-DOS, Windows 3.1, NT 4 -- or something with no relation to any of them.

**One header per option, holding a small integer, and nothing packed.** Bits
would be a third of the bytes and would cost far more than they saved: at the
point where someone is reading a log to find out why a file did not sync,
`devices=1` answers the question and `caps=0x7F` does not. Ten headers is about
a hundred and twenty bytes, once per connection, on a link that carries
kilobytes of transcript per turn.

Every value is a literal on the guest except the codepage, which is one
`GetACP()` call. Nothing here requires the client to hold a table, compute
anything, or translate. It reports what it IS; the bridge holds what that
implies. That is the same split as everywhere else in this codebase.

### Room to grow, which is the other reason not to pack

A packed word has a fixed number of bits and every one of them is a binary
question. This has neither limit:

* **A new option is a new header.** A bridge that has not heard of it ignores
  it; a client that does not send it gets the documented default. Neither side
  needs to know the other's version, and there is no field to run out of.
* **A new value inside an option is a new constant.** `eol` already has three
  and could have ten. A bit can only ever answer yes or no, so the day a
  client is neither CRLF nor LF you are adding a second bit somewhere else and
  hoping the pair is read together.
* **An option that turns out to be a number stays a number.** `dirreserve` is
  "how many characters short of the limit a directory must stop" -- 12 on
  Win32, 0 on Unix, and whatever a future client says. As a flag it would have
  meant "Win32-style reservation, magnitude hard-coded on the bridge", and the
  first client with a different one would need a new field.
* **An unknown value is refused, not masked away.** With bits, a value the
  bridge does not understand is indistinguishable from the bit being clear --
  which is the permissive reading, arrived at silently. Here it stops the
  connection and says which field and what it received.

### The defaults are the dangerous-needs-evidence rule

An absent field means the deployed Win9x client, which is the only client that
has ever connected without declaring. So every default is the RESTRICTIVE
value, and a client gets a permissive rule only by asking for it in writing.
That is not symmetric politeness -- the two directions fail very differently:

* `devices=0` wrongly on a DOS box means the bridge pushes `aux.c`, and
  `CreateFileA` on AUX with nothing asserting DSR **blocks with no timeout**,
  wedging the guest's RPC worker.
* `devices=1` wrongly on Unix means one file is skipped with a printed reason.

Same shape for case folding: guessing `sensitive` on an insensitive filesystem
silently overwrites one file with another and then oscillates forever, while
guessing `fold` on a sensitive one refuses a newcomer out loud.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- the enums -------------------------------------------------------------
#
# Mirrored in win/caps.h. A value the bridge does not recognise is a REFUSAL,
# never a fallback to the default: an unrecognised value means the client knows
# something we do not, and guessing is how the permissive branch gets taken by
# accident.

CASE_SENSITIVE = 0          #: `README.md` and `readme.md` are two files
CASE_FOLD = 1               #: ...and one file. Case-preserving either way.

DEVICES_NONE = 0            #: no reserved device names
DEVICES_DOS = 1             #: CON PRN AUX NUL COM1-9 LPT1-9 CLOCK$, any ext

TRAILING_KEPT = 0           #: `notes.` and `notes` are two files
TRAILING_STRIPPED = 1       #: ...and one. Win32 strips silently.

ILLEGAL_POSIX = 0           #: only NUL and the separator
ILLEGAL_DOS = 1             #: also `< > : " \\ | ? *`

EOL_LF = 0
EOL_CRLF = 1
#: Classic Mac. Declared so the number stays reserved, and REFUSED at the
#: handshake -- see from_hello. `to_guest` would convert to lone CRs, but the
#: guest normalises only CRLF, so its crc_norm still contains them, the
#: compare-and-swap never matches, and every text file fails on every pass.
#: A refusal names the capability; the failure names corruption instead.
EOL_CR = 2

#: How many characters short of the path limit a DIRECTORY must stop. Win32
#: reserves twelve so an 8.3 name still fits inside; Unix reserves none. A
#: number rather than a flag because it is a number, and because a client with
#: a different reservation can say so without a new field.
DIRRESERVE_NONE = 0
DIRRESERVE_WIN32 = 12

#: How many characters short of the path limit a FILE must stop, so that the
#: client can still form its own working names beside it. The Win9x guest
#: writes `<path>.gaston-tmp` and renames the old file to `<path>.gaston-old`,
#: both eleven characters longer than the target.
#:
#: This is not symmetry with `dirreserve` for its own sake. Without it a path
#: of 251..257 bytes passes this check, and then BOTH working names truncate to
#: the same 259-byte string -- so the commit deletes its own temp file, moves
#: the user's file aside and straight back, and returns SUCCESS while the guest
#: still holds the old bytes. The bridge advances the baseline on that ack and
#: the next pass pulls the stale content back over the agent's edit. At 258 and
#: 259 it is worse: the temp name resolves to the target itself (Win32 strips a
#: trailing dot), so CREATE_ALWAYS truncates the user's file in place, at the
#: first chunk, before the compare-and-swap that exists to protect it ever
#: runs.
#:
#: Declared rather than assumed, like every other client rule: a client that
#: writes in place, or uses a shorter suffix, says so and gets the bytes back.
WORKRESERVE_NONE = 0
WORKRESERVE_GASTON = 11     #: len(".gaston-tmp") == len(".gaston-old")

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


@dataclass(frozen=True)
class Caps:
    """One client's rules. Built from HELLO; defaults are Win9x."""

    acp: str = "cp1252"
    case: int = CASE_FOLD
    devices: int = DEVICES_DOS
    trailing: int = TRAILING_STRIPPED
    illegal: int = ILLEGAL_DOS
    eol: int = EOL_CRLF
    dirreserve: int = DIRRESERVE_WIN32
    workreserve: int = WORKRESERVE_GASTON
    maxpath: int = 260
    maxcomp: int = 255
    #: Which manifest row format the client sends. 1 carries one checksum,
    #: computed for the client's OWN classification; 2 carries the normalised
    #: and the raw one side by side, which is what lets `.gitattributes`
    #: re-classify a path here without asking the client to scan again.
    #:
    #: A version rather than a sniff. The two formats cannot be told apart by
    #: counting fields -- a path may contain spaces, and the length-prefix
    #: makes a six-field row and a seven-field row both parse -- so guessing
    #: would mean reading a checksum as a path length on some trees and not
    #: others. This is the case the handshake exists for.
    manifest: int = 1
    #: True when the client declared any of this, rather than being assumed.
    declared: bool = False

    @property
    def maxdirpath(self) -> int:
        return self.maxpath - self.dirreserve

    @property
    def maxfilepath(self) -> int:
        """The limit for a FILE, leaving the client room for its working name.

        Directories had a reserve from the start and files did not, which is
        the whole of the bug in `workreserve`'s note above.
        """
        return self.maxpath - self.workreserve

    def summary(self) -> str:
        """For the log, because "why did that not sync" starts here."""
        how = "declared" if self.declared else "assumed (client did not say)"
        return (f"{how}: acp={self.acp} case={'fold' if self.case else 'exact'} "
                f"devices={'dos' if self.devices else 'none'} "
                f"eol={('lf', 'crlf', 'cr')[self.eol]} "
                f"maxpath={self.maxpath} maxdir={self.maxdirpath} "
                f"maxfile={self.maxfilepath} "
                f"maxcomp={self.maxcomp} manifest=v{self.manifest}")


class CapsError(Exception):
    """A declaration the bridge cannot honour. Refuse; never guess."""


def _enum(h: dict, key: str, allowed: tuple, default: int) -> int:
    raw = h.get(key)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise CapsError(f"{key}={raw!r} is not a number") from None
    if v not in allowed:
        raise CapsError(
            f"{key}={v} is not a value this bridge knows ({sorted(allowed)}). "
            f"Refusing rather than falling back: an unrecognised value means "
            f"the client knows something we do not, and the guess would be "
            f"the permissive one.")
    return v


def _num(h: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = h.get(key)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise CapsError(f"{key}={raw!r} is not a number") from None
    if not lo <= v <= hi:
        raise CapsError(f"{key}={v} is outside {lo}..{hi}")
    return v


def from_hello(h: dict) -> Caps:
    """Read a client's declaration. Raises rather than guessing."""
    acp = codec_for_acp(h.get("acp"))
    if h.get("acp") and acp is None:
        raise CapsError(
            f"the client reports ANSI codepage {h['acp']!r}, which this bridge "
            f"has no mapping for. A wrong codepage encodes every filename with "
            f"the wrong table, round-trips perfectly, and leaves mangled names "
            f"on the user's own disk with nothing reporting it -- so this is "
            f"the one field that is never guessed.")

    declared = any(k in h for k in (
        "acp", "case", "devices", "trailing", "illegal", "eol", "shortnames",
        "dirreserve", "workreserve", "maxpath", "maxcomp", "manifest"))

    return Caps(
        acp=acp or "cp1252",
        case=_enum(h, "case", (CASE_SENSITIVE, CASE_FOLD), CASE_FOLD),
        devices=_enum(h, "devices", (DEVICES_NONE, DEVICES_DOS), DEVICES_DOS),
        trailing=_enum(h, "trailing", (TRAILING_KEPT, TRAILING_STRIPPED),
                       TRAILING_STRIPPED),
        illegal=_enum(h, "illegal", (ILLEGAL_POSIX, ILLEGAL_DOS), ILLEGAL_DOS),
        # EOL_CR is a DECLARED value we cannot honour, so it is refused here
        # rather than failing per file forever. `to_guest` would convert to
        # lone CRs, but the guest's normaliser folds only CRLF -- so its
        # crc_norm still contains those CRs, the compare-and-swap never
        # matches, and every text file comes back crc_mismatch on every pass.
        # Loud, but the log points at corruption rather than at a capability.
        eol=_enum(h, "eol", (EOL_LF, EOL_CRLF), EOL_CRLF),
        dirreserve=_num(h, "dirreserve", DIRRESERVE_WIN32, 0, 64),
        workreserve=_num(h, "workreserve", WORKRESERVE_GASTON, 0, 64),
        maxpath=_num(h, "maxpath", 260, 64, 65536),
        maxcomp=_num(h, "maxcomp", 255, 12, 65536),
        manifest=_num(h, "manifest", 1, 1, 2),
        declared=declared,
    )


#: A client that has never heard of any of this. Not a default anyone gets by
#: accident -- it is here so tests and future clients can name it.
POSIX = Caps(acp="latin-1", case=CASE_SENSITIVE, devices=DEVICES_NONE,
             trailing=TRAILING_KEPT, illegal=ILLEGAL_POSIX, eol=EOL_LF,
             dirreserve=DIRRESERVE_NONE,
             # Not a Windows rule: the `.gaston-tmp` convention is OURS, so a
             # POSIX client that commits by temp-and-rename needs the same room
             # even though nothing about its filesystem is constrained.
             workreserve=WORKRESERVE_GASTON,
             maxpath=4096, maxcomp=255, manifest=2, declared=True)

WIN9X = Caps(manifest=2, declared=True)
