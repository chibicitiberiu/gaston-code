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

from . import paths as P

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
EOL_CR = 2                  #: classic Mac. Declared, not yet implemented.

SHORTNAMES_NONE = 0
SHORTNAMES_83 = 1           #: 8.3 aliases exist alongside long names

#: How many characters short of the path limit a DIRECTORY must stop. Win32
#: reserves twelve so an 8.3 name still fits inside; Unix reserves none. A
#: number rather than a flag because it is a number, and because a client with
#: a different reservation can say so without a new field.
DIRRESERVE_NONE = 0
DIRRESERVE_WIN32 = 12


@dataclass(frozen=True)
class Caps:
    """One client's rules. Built from HELLO; defaults are Win9x."""

    acp: str = "cp1252"
    case: int = CASE_FOLD
    devices: int = DEVICES_DOS
    trailing: int = TRAILING_STRIPPED
    illegal: int = ILLEGAL_DOS
    eol: int = EOL_CRLF
    shortnames: int = SHORTNAMES_83
    dirreserve: int = DIRRESERVE_WIN32
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

    def summary(self) -> str:
        """For the log, because "why did that not sync" starts here."""
        how = "declared" if self.declared else "assumed (client did not say)"
        return (f"{how}: acp={self.acp} case={'fold' if self.case else 'exact'} "
                f"devices={'dos' if self.devices else 'none'} "
                f"eol={('lf', 'crlf', 'cr')[self.eol]} "
                f"maxpath={self.maxpath} maxdir={self.maxdirpath} "
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
    acp = P.codec_for_acp(h.get("acp"))
    if h.get("acp") and acp is None:
        raise CapsError(
            f"the client reports ANSI codepage {h['acp']!r}, which this bridge "
            f"has no mapping for. A wrong codepage encodes every filename with "
            f"the wrong table, round-trips perfectly, and leaves mangled names "
            f"on the user's own disk with nothing reporting it -- so this is "
            f"the one field that is never guessed.")

    declared = any(k in h for k in (
        "acp", "case", "devices", "trailing", "illegal", "eol", "shortnames",
        "dirreserve", "maxpath", "maxcomp", "manifest"))

    return Caps(
        acp=acp or "cp1252",
        case=_enum(h, "case", (CASE_SENSITIVE, CASE_FOLD), CASE_FOLD),
        devices=_enum(h, "devices", (DEVICES_NONE, DEVICES_DOS), DEVICES_DOS),
        trailing=_enum(h, "trailing", (TRAILING_KEPT, TRAILING_STRIPPED),
                       TRAILING_STRIPPED),
        illegal=_enum(h, "illegal", (ILLEGAL_POSIX, ILLEGAL_DOS), ILLEGAL_DOS),
        eol=_enum(h, "eol", (EOL_LF, EOL_CRLF, EOL_CR), EOL_CRLF),
        shortnames=_enum(h, "shortnames", (SHORTNAMES_NONE, SHORTNAMES_83),
                         SHORTNAMES_83),
        dirreserve=_num(h, "dirreserve", DIRRESERVE_WIN32, 0, 64),
        maxpath=_num(h, "maxpath", 260, 64, 65536),
        maxcomp=_num(h, "maxcomp", 255, 12, 65536),
        manifest=_num(h, "manifest", 1, 1, 2),
        declared=declared,
    )


#: A client that has never heard of any of this. Not a default anyone gets by
#: accident -- it is here so tests and future clients can name it.
POSIX = Caps(acp="latin-1", case=CASE_SENSITIVE, devices=DEVICES_NONE,
             trailing=TRAILING_KEPT, illegal=ILLEGAL_POSIX, eol=EOL_LF,
             shortnames=SHORTNAMES_NONE, dirreserve=DIRRESERVE_NONE,
             maxpath=4096, maxcomp=255, manifest=2, declared=True)

WIN9X = Caps(manifest=2, declared=True)
