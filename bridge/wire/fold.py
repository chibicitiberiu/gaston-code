"""Unicode -> the guest's display codepage, and the trivial way back.

The guest is an ANSI application: it renders whatever byte it is handed
through the font for its own ANSI codepage. Anything the model emits that the
codepage cannot represent has to be transliterated here, on the machine that
has full Unicode tables, rather than in C on the guest.

The codepage is meaningfully richer than ASCII and it is worth using: on
CP1252 curly quotes, en/em dashes, the ellipsis and the bullet all have real
codepoints, so ordinary model prose survives essentially intact. The work is
in the tail -- arrows, box drawing, mathematical symbols and emoji.

Folding direction is deliberately asymmetric. Display text is folded (lossy,
best-effort, must never fail). Anything on the RPC path stays UTF-8 and is not
touched by this module.

WHICH CODEPAGE IS NEGOTIATED, AND THIS MODULE USED TO IGNORE IT. `caps.py`
reads GetACP() off HELLO, maps it through a 28-entry table, and refuses
rather than guesses; `paths.py` and `syncrun.py` honour it on every filename.
The display path did not -- `cp1252` was a literal at six places here and two
more in `protocol.py` -- so a guest reporting acp=1251 connected fine and got
this:

    filename on the wire -> b'\xcf\xf0\xe8\xe2\xe5\xf2.txt'   round-trips: True
    the same text as PROSE -> b'??????.txt'

Its files synced perfectly and its own filenames were unreadable in every
transcript line, permission prompt and export.

A CONTEXT VARIABLE RATHER THAN A PARAMETER, and the reason is the shape of
the call graph rather than convenience. The codepage is fixed for the life of
a connection and is needed at about forty-five frame-building call sites,
nearly all of which are short English strings where every candidate codepage
agrees because they are ASCII. Threading an argument through all of them
would be a large diff whose only load-bearing part is the handful that carry
model prose, filenames and titles. `use()` is called once per connection and
asyncio tasks inherit the context they were created in, so the few paths that
run outside it -- a pre-handshake refusal, where no codepage has been
declared yet -- correctly get the restrictive default.
"""

from __future__ import annotations

import contextvars
import unicodedata

from ..files import caps as C

#: What a client that has not declared anything is assumed to render. Same
#: value `caps.Caps.acp` defaults to, for the same reason: it is the
#: conservative guess for the machine this was built for, and it is an
#: assumption rather than a fact.
DEFAULT_CODEC = C.Caps.acp

#: The OEM codepage a console child writes on a client that has not said.
#: NOT the same question as the ANSI one -- win/net/net.cpp says so in as
#: many words ("The console codepage is a different question with a different
#: answer") and then never sent an answer. A shell profile declares it.
DEFAULT_OEM = "cp437"

_codec: contextvars.ContextVar = contextvars.ContextVar(
    "guest_display_codec", default=DEFAULT_CODEC)
_oem: contextvars.ContextVar = contextvars.ContextVar(
    "guest_console_codec", default=DEFAULT_OEM)


def use(codec: str = "", *, oem: str = ""):
    """Bind this connection's codepages. Returns the tokens to restore with.

    Called once per connection, after the handshake has read them off HELLO.
    An empty argument leaves that codepage alone, so a client that declares
    an ANSI page and no console page keeps the default for the second.
    """
    return (_codec.set(codec) if codec else None,
            _oem.set(oem) if oem else None)


def codec() -> str:
    """This connection's display codepage."""
    return _codec.get()


def oem_codec() -> str:
    """This connection's console codepage."""
    return _oem.get()

# Characters with no CP1252 representation that appear often enough in model
# output to be worth a deliberate mapping rather than a decomposition guess.
_EXPLICIT: dict[str, str] = {
    # Quotes and dashes that CP1252 lacks (it has ' ' " " - - but not these)
    "―": "-",      # horizontal bar
    "−": "-",      # minus sign
    "‐": "-",      # hyphen
    "‑": "-",      # non-breaking hyphen
    "〜": "~",      # wave dash
    # Spaces
    " ": " ",      # nbsp -- CP1252 has it, but a literal space is safer
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "",       # zero-width space
    "﻿": "",       # BOM
    # Arrows: extremely common in model explanations
    "←": "<-", "→": "->", "↔": "<->",
    "⇐": "<=", "⇒": "=>", "⇔": "<=>",
    "↑": "^", "↓": "v",
    # Box drawing -- model output uses these for trees and tables
    "─": "-", "━": "-", "│": "|", "┃": "|",
    "┌": "+", "┐": "+", "└": "+", "┘": "+",
    "├": "+", "┤": "+", "┬": "+", "┴": "+", "┼": "+",
    "═": "=", "║": "|",
    "╴": "-", "╶": "-", "╷": "|", "╵": "|",
    # Block elements, used for bar charts and progress
    "█": "#", "▉": "#", "▊": "#", "▋": "#",
    "▌": "#", "▍": "#", "▎": "#", "▏": "#",
    "░": ".", "▒": ":", "▓": "#",
    # Bullets and list marks
    "•": "*",      # CP1252 has 0x95, but '*' reads better in a fixed font
    "●": "*", "○": "o", "▪": "*", "▫": "o",
    "‣": ">", "⁃": "-",
    # Check marks and crosses -- very common in model checklists
    "✓": "[x]", "✔": "[x]",
    "✗": "[ ]", "✘": "[ ]",
    "✘️": "[ ]",
    "❌": "[x]", "✅": "[x]",
    # Maths and logic
    "≠": "!=", "≤": "<=", "≥": ">=",
    "×": "x", "÷": "/",
    "∞": "inf", "≈": "~=", "≡": "==",
    "∑": "sum", "∏": "prod", "√": "sqrt",
    "∈": "in", "∉": "not in", "∅": "empty",
    "∧": "and", "∨": "or", "¬": "not",
    # The CP1252 0x80-0x9F range.
    #
    # These all have real CP1252 codepoints, so the passthrough below would
    # keep them -- and on Win98 that is wrong. 0x80-0x9F is where Windows put
    # the extra typographic characters, and the stock bitmap UI font has no
    # glyphs there: an em dash rendered as a vertical bar and a euro sign as a
    # capital I. Everything from 0xA0 up is Latin-1 and renders correctly, so
    # this range is the whole of the problem. Verified on the guest.
    "\u2014": "--",   # em dash
    "\u2013": "-",    # en dash
    "\u20ac": "EUR",  # euro
    "\u0192": "f",    # florin
    "\u02c6": "^",    # modifier circumflex
    "\u2030": "o/oo", # per mille
    "\u0160": "S", "\u0161": "s",    # S/s caron
    "\u017d": "Z", "\u017e": "z",    # Z/z caron
    "\u0152": "OE", "\u0153": "oe",  # OE/oe ligature
    "\u02dc": "~",    # small tilde
    "\u2122": "(tm)",
    "\u0178": "Y",    # Y diaeresis
    # Typography
    "…": "...",    # CP1252 has 0x85; '...' is clearer in a bitmap font
    "′": "'", "″": '"',
    "«": "<<", "»": ">>",
    "‹": "<", "›": ">",
    # Misc symbols that turn up in tool output
    "‘": "'", "’": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"',
    "†": "+", "‡": "++",
    "·": ".", "‧": ".",
    "⁄": "/",
}

# Emoji and pictographs get dropped rather than transliterated. A model writing
# a rocket into a status line is decoration; turning it into ":rocket:" is
# noisier than removing it.
_DROP_CATEGORIES = {"So", "Sk", "Cf", "Cs", "Co", "Cn"}

_REPLACEMENT = "?"


def _fold_char(ch: str, cp: str) -> str:
    # 1. A deliberate mapping wins, even when CP1252 could carry the character.
    #    Several entries here ARE representable -- curly quotes, the ellipsis,
    #    the bullet, nbsp -- and are overridden anyway because they render
    #    poorly in a fixed-pitch bitmap font on a 640x480 screen. Checking
    #    CP1252 first would silently skip every one of them.
    if ch in _EXPLICIT:
        return _EXPLICIT[ch]

    # 2. Anything else the codepage can carry goes through untouched.
    try:
        ch.encode(cp)
        return ch
    except UnicodeEncodeError:
        pass

    # 3. Strip accents and try again -- catches most Latin-adjacent scripts.
    decomposed = unicodedata.normalize("NFKD", ch)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    if stripped != ch:
        out = "".join(_fold_char(c, cp) for c in stripped)
        if out and out != _REPLACEMENT * len(out):
            return out

    # 4. Decorative characters are dropped rather than replaced.
    if unicodedata.category(ch) in _DROP_CATEGORIES:
        return ""

    # 5. Give up visibly. A '?' is honest; silently dropping text is not.
    return _REPLACEMENT


def fold(text: str, cp: str = "") -> str:
    """Fold to a string the codepage can represent. Never raises.

    ASCII short-circuits, which is also why the un-threaded call sites are
    safe: every codepage in `caps._ACP_CODECS` is an ASCII superset, so an
    English string folds and encodes identically under all of them.
    """
    if text.isascii():
        return text
    cp = cp or codec()
    return "".join(_fold_char(c, cp) for c in text)


def encode(text: str, cp: str = "") -> bytes:
    """Fold and encode for the wire. Never raises.

    The second errors='replace' is belt and braces: _fold_char should have made
    every character encodable, but this is on the display path and a crash here
    would take out the transcript.
    """
    cp = cp or codec()
    return fold(text, cp).encode(cp, errors="replace")


def unrepresentable(text: str, cp: str = "") -> str | None:
    """The first character the codepage cannot carry, or None.

    For naming the offending character in a refusal. Checked one character at
    a time rather than by catching UnicodeEncodeError on the whole string,
    because the exception's `start` offset is into the string we are about to
    stop using and the caller wants the character, not an index.
    """
    cp = cp or codec()
    for ch in text:
        try:
            ch.encode(cp)
        except UnicodeEncodeError:
            return ch
    return None


def encode_exact(text: str, cp: str = "") -> bytes:
    """Encode for the RPC path: no transliteration, and a refusal rather than
    a rewrite. Raises UnicodeEncodeError.

    This is the counterpart to encode() and the module docstring's rule --
    "Anything on the RPC path stays UTF-8 and is not touched by this module"
    -- made available rather than merely asserted. EXEC used encode() for
    three phases, which meant the bridge silently REWROTE the command between
    the moment anything inspected it and the moment the guest ran it:

        typed:  del c:\\work\\<U+00B7><U+00B7>\\<U+00B7><U+00B7>\\windows\\win.ini
        ran:    del c:\\work\\..\\..\\windows\\win.ini

    U+00B7 folds to '.', U+2502 folds to '|', U+00BB folds to '>>' and U+2044
    folds to '/' -- a path separator, a pipe and a redirection invented from
    nothing. paths.py:16 already warns about exactly this for sync paths.

    Folding is right for prose, where the alternative to an approximate glyph
    is no glyph. It is wrong for anything executable, where the alternative to
    an approximate command is a command the user did not write.
    """
    return text.encode(cp or codec())


def decode(data: bytes, cp: str = "") -> str:
    """Guest -> bridge. Command output, window titles, filenames and typed
    input all arrive as CP1252 bytes.

    Note this is the ANSI codepage, not the OEM one. Output captured from
    COMMAND.COM is CP437 and must use decode_oem() instead -- conflating the
    two is the classic Win9x encoding bug.
    """
    return data.decode(cp or codec(), errors="replace")


def decode_oem(data: bytes, codepage: str = "") -> str:
    """Console output from a spawned child process uses the OEM codepage.

    A DIFFERENT QUESTION FROM THE ANSI ONE, which is why it has its own
    context variable: a Russian machine is cp1251 for its GUI and cp866 for
    its console, and conflating the two is the classic Win9x encoding bug.
    """
    return data.decode(codepage or oem_codec(), errors="replace")
