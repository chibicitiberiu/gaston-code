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

`automation` is the one field whose default is NOT the restrictive value, and
the paragraph above is why rather than an exception to it: the rule is about
guesses nothing downstream can detect. A permissive guess here is detected and
refused by the client itself -- the only process that can move somebody's
mouse -- so the worst it costs is one round trip with a sentence attached. See
the note on the field.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- the enums -------------------------------------------------------------
#
# Mirrored in win/net/caps.h. A value the bridge does not recognise is a REFUSAL,
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

#: Whether the client's filesystem ALSO generates an 8.3 alias for every long
#: name. NOT "only 8.3 names are allowed" -- a Win9x VFAT volume declares
#: SHORTNAMES_83 and `maxcomp=255` at the same time, because it has both.
#: The length limit is `maxcomp`'s job; this says the SHAPE of a name matters
#: independently, because two long names can share one alias.
#:
#: The client has sent this since the handshake was written and the bridge
#: threw it away: it counted toward `declared` and had no field, so
#: `from_hello({'shortnames':'0'})` and `{'shortnames':'1'}` compared EQUAL.
#: Meanwhile the one place the bridge caps a name to 8.3 -- the per-
#: conversation folder -- carried a bare `FOLDER_MAX = 8`. A client asking in
#: writing for the restrictive rule, answered with an assumption.
SHORTNAMES_NONE = 0
SHORTNAMES_83 = 1

#: The longest name that survives on a volume that aliases. Eight, because
#: the alias is `STEM~N.EXT` and a name of 8 or fewer with no extension IS
#: its own alias -- nothing to collide with.
SHORTNAME_MAX = 8

#: How deep the client's own scanner walks. A CLIENT RULE like any other, and
#: the one that never got a field: it sat in `paths.MAX_DEPTH` as a bare 16
#: mirroring `win/sync/sync.h`'s RC_SYNC_MAX_DEPTH, with a forty-line comment
#: explaining that it belongs to that specific client's walk.
#:
#: Wrong in both directions when it is assumed. A client that walks deeper has
#: its trees refused for nothing; a client that walks SHALLOWER gets the silent
#: bug the comment describes -- the path is pushed, acked, and then never
#: reported by a scan again, so it reads as "deleted on the guest" forever.
MAXDEPTH_GASTON = 16

#: How many files the client's scanner counts before aborting.  The
#: default mirrors ``win/sync/sync.h``'s RC_SYNC_MAX_FILES for the same
#: reason MAXDEPTH_GASTON mirrors RC_SYNC_MAX_DEPTH: both are properties
#: of this client, and guessing them is how the too_many message ends up
#: quoting a number the real client no longer uses.
MAXFILES_GASTON = 2000

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

#: Outbound deflate capability.
#: 0: client never deflates outbound (default if undeclared)
#: 1: client may deflate outbound with persistent stream
DEFLATE_NONE = 0
DEFLATE_STREAM = 1

#: Whether this client can be DRIVEN -- its own screen read, clicked, typed
#: into, photographed, a window raised. It decides whether the `machine`
#: server offers `ui`, `click`, `keys`, `shot` and `focus` at all.
#:
#: Not a fact about the hardware. A Win98 box can obviously drive its own
#: screen; whether the person sitting at it wants their mouse moved and their
#: foreground stolen is a different question, and it is theirs to answer
#: (`[ui] automation` in gaston.ini). So this is the one capability a machine
#: declares about its OWNER rather than about itself.
#:
#: Room to grow, which is why it is an enum: a client that can photograph a
#: screen but not type into it, or enumerate windows without being able to
#: capture them, is a third and a fourth value and no new header.
AUTOMATION_NONE = 0
AUTOMATION_SCREEN = 1
#: SCREEN plus UIRUN: a whole list of steps in one request, run on the guest
#: with its own timeouts, the focus check and held-key release. A client that
#: declares it gets the `input` tool in place of `click`, `keys` and `focus`.
AUTOMATION_STEPS = 2

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


#: A Python codec name -> the charset label to put in a <meta> tag. IE5 on
#: the guest reads the exported transcript, and a document whose bytes and
#: whose declared charset disagree renders as mojibake in a file the user
#: keeps. The two used to be independently hardcoded -- `fold.encode` said
#: cp1252 and `html.py` said `windows-1252` -- correctly paired by accident
#: and wrong together on any other machine.
_HTML_CHARSET = {
    "utf-8": "utf-8", "latin-1": "iso-8859-1",
    # The CJK pages have conventional IANA names that browsers of the era
    # recognise; their `cpNNN` spellings are Python's, not the web's.
    "cp932": "shift_jis", "cp936": "gb2312", "cp949": "euc-kr",
    "cp950": "big5",
}


def html_charset(codec: str) -> str:
    """What to declare in a <meta> tag for text encoded with `codec`."""
    name = (codec or "").lower()
    if name in _HTML_CHARSET:
        return _HTML_CHARSET[name]
    # `cp1252` -> `windows-1252`; the IANA name for the Windows ANSI pages.
    # Everything else (cp437, cp866, cp932...) is accepted under its own
    # name by every browser that can render it at all.
    if name.startswith("cp") and name[2:].isdigit() and 1250 <= int(name[2:]) <= 1258:
        return "windows-" + name[2:]
    # The assumed client's label, derived rather than repeated: `Caps.acp`
    # is the one place that says what a silent client is taken to be.
    return name or html_charset(Caps.acp)


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
    #: The codepage a console child writes, which is NOT the ANSI one: this
    #: machine is cp1252/cp437 and a Russian one is cp1251/cp866. EMPTY means
    #: the client did not say, and the caller falls back to what the shell's
    #: profile declares -- a profile can say THAT this shell's children write
    #: OEM, but only the machine knows WHICH OEM it is set to.
    oemcp: str = ""
    case: int = CASE_FOLD
    devices: int = DEVICES_DOS
    trailing: int = TRAILING_STRIPPED
    illegal: int = ILLEGAL_DOS
    eol: int = EOL_CRLF
    shortnames: int = SHORTNAMES_83
    maxdepth: int = MAXDEPTH_GASTON
    maxfiles: int = MAXFILES_GASTON
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
    deflate: int = DEFLATE_NONE
    #: THE ONE DEFAULT THAT IS NOT THE RESTRICTIVE VALUE, and the paragraph
    #: above about `devices` is the reason rather than an exception to it.
    #: That rule exists because the permissive guess on a path rule corrupts
    #: in silence and nothing downstream can detect it. Nothing like that is
    #: reachable here: the only process that can move somebody's mouse is the
    #: one at their keyboard, and it refuses every screen op on its own
    #: authority when the setting is off, whatever this bridge believes
    #: (`rc_uirpc_handle`). The header decides whether five tools are DECLARED;
    #: the guest decides whether anything HAPPENS.
    #:
    #: With that settled, the rule the default has to answer to is caps.py's
    #: other one -- "an absent field means the deployed client, which is the
    #: only client that has ever connected without declaring" -- and the
    #: deployed client drives a screen and has done since Phase 6. Defaulting
    #: to NONE would mean a bridge updated ahead of its client silently loses
    #: five tools on a machine that can use all of them, with no error
    #: anywhere and the agent reporting it cannot see the screen. The other
    #: way round costs one refused round trip with a sentence attached, plus
    #: the 1,576 tokens the five schemas occupy.
    automation: int = AUTOMATION_SCREEN
    #: Which version of the frozen key-name table (agent/keynames.py) the
    #: guest resolves. 0 for a client that never said -- it has only the
    #: named keys inside `keys`' own bracket syntax, and no `input` tool.
    keys: int = 0
    #: The operations added with `input` (win/net/caps.h RC_CAP_OPS): FSLS
    #: with a file answering as itself, the fs mutations, PSLAUNCH and live
    #: memory on PSRESULT. 0 for a client that never said: none of their
    #: tools are offered, because an FSLS sent to a client that predates it
    #: is answered by nobody and `list_dir` waited out four minutes.
    ops: int = 0
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
        return (f"{how}: acp={self.acp} "
                f"oem={self.oemcp or 'not stated'} case={'fold' if self.case else 'exact'} "
                f"devices={'reserved' if self.devices else 'none'} "
                f"eol={('lf', 'crlf', 'cr')[self.eol]} "
                f"maxpath={self.maxpath} maxdir={self.maxdirpath} "
                f"maxfile={self.maxfilepath} "
                f"maxcomp={self.maxcomp} maxdepth={self.maxdepth} "
                f"maxfiles={self.maxfiles} "
                f"shortnames={'8.3' if self.shortnames else 'long'} "
                f"manifest=v{self.manifest} "
                f"deflate={'stream' if self.deflate else 'none'} "
                f"automation={('off', 'screen', 'steps')[self.automation]}")


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
    oemcp = codec_for_acp(h.get("oemcp"))
    if h.get("oemcp") and oemcp is None:
        raise CapsError(
            f"the client reports OEM codepage {h['oemcp']!r}, which this "
            f"bridge has no mapping for. Refused for the same reason as the "
            f"ANSI page: a console decoded with the wrong table turns every "
            f"`dir` into mojibake with nothing reporting it.")
    if h.get("acp") and acp is None:
        raise CapsError(
            f"the client reports ANSI codepage {h['acp']!r}, which this bridge "
            f"has no mapping for. A wrong codepage encodes every filename with "
            f"the wrong table, round-trips perfectly, and leaves mangled names "
            f"on the user's own disk with nothing reporting it -- so this is "
            f"the one field that is never guessed.")

    declared = any(k in h for k in (
        "acp", "oemcp", "case", "devices", "trailing", "illegal", "eol",
        "shortnames", "maxdepth",
        "dirreserve", "workreserve", "maxpath", "maxcomp", "manifest",
        "maxfiles", "deflate", "automation", "keys", "ops"))

    return Caps(
        acp=acp or Caps.acp,
        oemcp=oemcp or "",
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
        shortnames=_enum(h, "shortnames", (SHORTNAMES_NONE, SHORTNAMES_83),
                         SHORTNAMES_83),
        # A number, for the same reason `dirreserve` is one: a client with a
        # different walk says so without anyone adding a field. The floor is 1
        # because a client that cannot recurse at all still has a root.
        maxdepth=_num(h, "maxdepth", MAXDEPTH_GASTON, 1, 4096),
        maxfiles=_num(h, "maxfiles", MAXFILES_GASTON, 1, 1000000),
        dirreserve=_num(h, "dirreserve", DIRRESERVE_WIN32, 0, 64),
        workreserve=_num(h, "workreserve", WORKRESERVE_GASTON, 0, 64),
        maxpath=_num(h, "maxpath", 260, 64, 65536),
        maxcomp=_num(h, "maxcomp", 255, 12, 65536),
        manifest=_num(h, "manifest", 1, 1, 2),
        deflate=_enum(h, "deflate", (DEFLATE_NONE, DEFLATE_STREAM), DEFLATE_NONE),
        # Refused rather than guessed, like every other enum here, and this
        # one has a reason of its own: a value we do not know means the client
        # can be driven in a way this bridge has no tool for, and both guesses
        # are wrong -- NONE throws away a capability it has, SCREEN offers five
        # tools shaped for a screen it may not have.
        automation=_enum(h, "automation", (AUTOMATION_NONE, AUTOMATION_SCREEN,
                                           AUTOMATION_STEPS),
                         AUTOMATION_SCREEN),
        # A version, so a number with a ceiling: a guest claiming a table
        # newer than this bridge knows is refused like any unknown enum,
        # because the names it would accept are names nobody here can check.
        keys=_num(h, "keys", 0, 0, 1),
        ops=_num(h, "ops", 0, 0, 1),
        declared=declared,
    )


#: A client that has never heard of any of this. Not a default anyone gets by
#: accident -- it is here so tests and future clients can name it.
POSIX = Caps(acp="latin-1", case=CASE_SENSITIVE, devices=DEVICES_NONE,
             trailing=TRAILING_KEPT, illegal=ILLEGAL_POSIX, eol=EOL_LF,
             oemcp="latin-1", shortnames=SHORTNAMES_NONE, maxdepth=4096,
             maxfiles=1000000,
             dirreserve=DIRRESERVE_NONE,
             # Not a Windows rule: the `.gaston-tmp` convention is OURS, so a
             # POSIX client that commits by temp-and-rename needs the same room
             # even though nothing about its filesystem is constrained.
             workreserve=WORKRESERVE_GASTON,
             maxpath=4096, maxcomp=255, manifest=2, deflate=DEFLATE_STREAM,
             # A PDP running Unix has no screen for anyone to click on, and
             # naming that here is what keeps the five screen tools out of a
             # session with a client that is not a desktop at all.
             automation=AUTOMATION_NONE,
             declared=True)

#: What a client that declares nothing is assumed to be. Named for the
#: ASSUMPTION, not for an operating system: the values are the conservative
#: ones -- case-folding, reserved device names, CRLF, MAX_PATH 260 -- and a
#: client that is none of those things is expected to say so at handshake.
#: It was called WIN9X, which made a default read as a platform and made
#: `DEFAULT = WIN9X` in paths.py look like the bridge deciding what the
#: machine was rather than what it would assume absent a declaration.
CONSTRAINED = Caps(manifest=2, declared=True)

#: The old name, kept so nothing outside this file had to change in the same
#: commit. Prefer CONSTRAINED.
WIN9X = CONSTRAINED
