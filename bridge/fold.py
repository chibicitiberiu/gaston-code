"""Unicode -> CP1252 folding for the Win98 display, and the trivial way back.

The guest is an ANSI Win32 application: it renders whatever byte it is handed
through a CP1252 font. Anything the model emits that CP1252 cannot represent
has to be transliterated here, on the machine that has full Unicode tables,
rather than in C on the guest.

CP1252 is meaningfully richer than ASCII and it is worth using: curly quotes,
en/em dashes, the ellipsis and the bullet all have real codepoints in it, so
ordinary model prose survives essentially intact. The work is in the tail --
arrows, box drawing, mathematical symbols and emoji.

Folding direction is deliberately asymmetric. Display text is folded (lossy,
best-effort, must never fail). Anything on the RPC path stays UTF-8 and is not
touched by this module.
"""

from __future__ import annotations

import unicodedata

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


def _fold_char(ch: str) -> str:
    # 1. A deliberate mapping wins, even when CP1252 could carry the character.
    #    Several entries here ARE representable -- curly quotes, the ellipsis,
    #    the bullet, nbsp -- and are overridden anyway because they render
    #    poorly in a fixed-pitch bitmap font on a 640x480 screen. Checking
    #    CP1252 first would silently skip every one of them.
    if ch in _EXPLICIT:
        return _EXPLICIT[ch]

    # 2. Anything else CP1252 can carry goes through untouched.
    try:
        ch.encode("cp1252")
        return ch
    except UnicodeEncodeError:
        pass

    # 3. Strip accents and try again -- catches most Latin-adjacent scripts.
    decomposed = unicodedata.normalize("NFKD", ch)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    if stripped != ch:
        out = "".join(_fold_char(c) for c in stripped)
        if out and out != _REPLACEMENT * len(out):
            return out

    # 4. Decorative characters are dropped rather than replaced.
    if unicodedata.category(ch) in _DROP_CATEGORIES:
        return ""

    # 5. Give up visibly. A '?' is honest; silently dropping text is not.
    return _REPLACEMENT


def fold(text: str) -> str:
    """Fold to a CP1252-representable string. Never raises."""
    if text.isascii():
        return text
    return "".join(_fold_char(c) for c in text)


def encode(text: str) -> bytes:
    """Fold and encode for the wire. Never raises.

    The second errors='replace' is belt and braces: _fold_char should have made
    every character encodable, but this is on the display path and a crash here
    would take out the transcript.
    """
    return fold(text).encode("cp1252", errors="replace")


def decode(data: bytes) -> str:
    """Guest -> bridge. Command output, window titles, filenames and typed
    input all arrive as CP1252 bytes.

    Note this is the ANSI codepage, not the OEM one. Output captured from
    COMMAND.COM is CP437 and must use decode_oem() instead -- conflating the
    two is the classic Win9x encoding bug.
    """
    return data.decode("cp1252", errors="replace")


def decode_oem(data: bytes, codepage: str = "cp437") -> str:
    """Console output from a spawned child process uses the OEM codepage."""
    return data.decode(codepage, errors="replace")
