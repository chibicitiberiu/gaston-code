"""The `input` tool's step list: checked here, run on the guest.

The agent sends JSON -- a list where a string is text to type (with the
`[Ctrl+S]` chord syntax `keys` always had) and an object is any other step.
This module turns that into the UIRUN body the guest executes
(win/ui/uisteps.cpp), and refuses, BEFORE anything is sent, every list that
would do something other than what it says:

  * a key name that is not in the frozen table, or newer than the guest's;
  * a key or button held and never released, or released and never held --
    the guest releases anything left down whatever happens, but a list that
    relies on that is a list whose author lost track, and saying so teaches;
  * a timeout outside its bounds: refused, not clamped, so the agent learns
    the bound instead of getting a different wait from the one it asked for;
  * text the guest's code page cannot hold, which would otherwise be typed
    as something else.

Each refusal names the step, counted from 1 as the agent wrote them.

There is no branching and there are no variables, on purpose. A failed wait
stops the list and that is the only condition; anything smarter is the
agent's job between calls. That line is what keeps this from becoming a
scripting language nobody can review in a permission prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..wire import fold
from . import keynames as K

#: The guest's own ceiling (RC_RUN_MAX in uisteps.cpp). A string step can
#: expand to several lines -- one per chord and per run of text -- and it is
#: the LINES that are counted.
MAX_LINES = 64
#: One run of text, after encoding.
MAX_TEXT = 2048
#: The whole body. Well under a frame's 64KB, so the reply fits beside it.
MAX_BODY = 16384

STEP_TIMEOUT_DEFAULT = 30_000
STEP_TIMEOUT_MAX = 120_000
SCRIPT_TIMEOUT_DEFAULT = 120_000
SCRIPT_TIMEOUT_MAX = 600_000
SETTLE_MAX = 5_000
WAIT_MAX = STEP_TIMEOUT_MAX

#: Every object key a step may carry, so a misspelt one is refused rather
#: than silently ignored -- `"timout": 5000` would otherwise run with the
#: default and the agent would never know.
_TARGET_KEYS = {"item", "item_nth", "text", "text_nth", "edge", "at", "in"}
_KINDS = {
    "focus": set(),
    "click": _TARGET_KEYS | {"button", "double"},
    "close": set(),
    "down": _TARGET_KEYS | {"where"},
    "up": _TARGET_KEYS | {"where"},
    "move": _TARGET_KEYS,
    "move_by": set(),
    "wait": set(),
    "wait_for": set(),
    "wait_gone": set(),
    "wait_text": {"text"},
    "idle": set(),
    "type": {"in"},
}


#: Which modifier a held key counts as, for the Ctrl+Alt+Delete refusal.
_MOD_FAMILY = {"lctrl": "ctrl", "rctrl": "ctrl", "lalt": "alt",
               "ralt": "alt", "altgr": "alt"}


class StepError(ValueError):
    """A list refused before anything was sent. `index` is 1-based."""

    def __init__(self, index: int, message: str):
        super().__init__(f"step {index}: {message}" if index else message)
        self.index = index


@dataclass
class Compiled:
    """What goes on the wire, and what the permission bar says."""

    body: bytes
    timeout_ms: int
    settle: int
    #: Wire line -> the 1-based step the agent wrote, for reporting back.
    origin: list[int] = field(default_factory=list)
    #: One plain-language phrase per step, for the person approving it.
    phrases: list[str] = field(default_factory=list)


def _esc(value: bytes) -> bytes:
    out = bytearray()
    for b in value:
        if b < 0x20 or b in (0x25, 0x7F):
            out += b"%%%02X" % b
        else:
            out.append(b)
    return bytes(out)


def _line(verb: str, fields: dict[str, bytes]) -> bytes:
    parts = [verb.encode("ascii")]
    for k, v in fields.items():
        parts.append(k.encode("ascii") + b"=" + _esc(v))
    return b"\t".join(parts)


def _int(index: int, name: str, v, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise StepError(index, f"`{name}` must be a whole number of "
                               f"milliseconds, not {v!r}")
    if not lo <= v <= hi:
        raise StepError(index, f"`{name}` is {v}; it must be between {lo} "
                               f"and {hi}")
    return v


def _pair(index: int, name: str, v) -> tuple[int, int]:
    if (not isinstance(v, (list, tuple)) or len(v) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool)
                       for n in v)):
        raise StepError(index, f"`{name}` must be two whole numbers, [x, y]")
    return int(v[0]), int(v[1])


class _Compiler:
    def __init__(self, *, encode, keys_version: int):
        self.encode = encode
        self.keys_version = keys_version
        self.lines: list[bytes] = []
        self.origin: list[int] = []
        self.phrases: list[str] = []
        self.held: dict[str, int] = {}

    # --- pieces ------------------------------------------------------

    def emit(self, index: int, verb: str, **fields) -> None:
        """One wire line. Every string goes out in the GUEST's code page --
        a selector naming a German caption is compared there against bytes
        in that code page, and UTF-8 would never match it."""
        if len(self.lines) >= MAX_LINES:
            raise StepError(index, f"the list is longer than the {MAX_LINES} "
                                   f"actions one call may carry -- split it")
        wire: dict[str, bytes] = {}
        for k, v in fields.items():
            if v is None or v == "" or v == b"":
                continue
            if isinstance(v, bool):
                v = "1" if v else "0"
            if isinstance(v, int):
                v = str(v)
            if isinstance(v, str):
                v = self.text_bytes(index, v)
            wire[k] = v
        self.lines.append(_line(verb, wire))
        self.origin.append(index)

    def text_bytes(self, index: int, text: str) -> bytes:
        try:
            raw = self.encode(text)
        except UnicodeEncodeError as exc:
            bad = text[exc.start:exc.end]
            raise StepError(index, f"{bad!r} cannot be typed: the machine's "
                                   f"code page has no such character") from None
        if len(raw) > MAX_TEXT:
            raise StepError(index, f"the text is {len(raw)} bytes; one step "
                                   f"may type {MAX_TEXT}")
        return raw

    def key(self, index: int, name: str) -> str:
        c = K.canonical(name)
        if c is None:
            raise StepError(index, f"{name!r} is not a key name. Names: "
                                   f"{', '.join(K.all_names())}")
        if K.KEYS[c] > self.keys_version:
            raise StepError(index, f"{name!r} is newer than this machine's "
                                   f"client knows")
        return c

    def target(self, index: int, step: dict, kind: str) -> dict:
        """The position fields of a pointer step. Preference order is the
        tool description's: control, item, text, offset, screen pixels."""
        where = step[kind]
        out: dict = {}
        if isinstance(where, dict):
            if set(where) != {"screen"}:
                raise StepError(index, f"`{kind}` takes a selector, or "
                                       f'{{"screen": [x, y]}}')
            x, y = _pair(index, "screen", where["screen"])
            out["screen"] = f"{x},{y}"
            if step.get("in"):
                out["sel"] = str(step["in"])
        elif isinstance(where, str) and where.strip():
            out["sel"] = where
        elif where is None and kind in ("down", "up"):
            pass                        # a button where the pointer is now
        else:
            raise StepError(index, f"`{kind}` needs a selector naming a "
                                   f"control")
        if "item" in step:
            out["item"] = str(step["item"])
            if "item_nth" in step:
                out["inth"] = _int(index, "item_nth", step["item_nth"], 1,
                                   999)
        if "text" in step:
            if "item" in step:
                raise StepError(index, "`item` and `text` name two different "
                                       "places; give one")
            out["text"] = self.text_bytes(index, str(step["text"]))
            if "text_nth" in step:
                out["tnth"] = _int(index, "text_nth", step["text_nth"], 1,
                                   999)
            if step.get("edge") not in (None, "start", "end"):
                raise StepError(index, '`edge` is "start" or "end"')
            if step.get("edge") == "end":
                out["edge"] = "end"
        if "at" in step:
            if "item" in step or "text" in step:
                raise StepError(index, "`at` is an offset from the control's "
                                       "corner; it does not combine with "
                                       "`item` or `text`")
            x, y = _pair(index, "at", step["at"])
            out["at"] = f"{x},{y}"
        if "screen" in out and len(out) > (2 if "sel" in out else 1):
            raise StepError(index, "screen pixels take no item, text or at")
        return out

    # --- steps -------------------------------------------------------

    def string(self, index: int, s: str, timeout: int | None,
               in_sel: str = "") -> None:
        """Text with `[Chord]` tokens, split into type and tap lines.

        The syntax `keys` always had: `[Name]` or `{Name}` for a key,
        `[Ctrl+Shift+S]` for a chord, `[[` and `{{` for a literal bracket."""
        run: list[str] = []
        said: list[str] = []

        def flush():
            if run:
                text = "".join(run)
                self.emit(index, "type", text=self.text_bytes(index, text),
                          timeout=timeout, **{"in": in_sel})
                said.append(_quote(text))
                run.clear()

        i = 0
        while i < len(s):
            ch = s[i]
            if ch in "[{" and i + 1 < len(s) and s[i + 1] == ch:
                run.append(ch)
                i += 2
                continue
            if ch in "[{":
                close = "]" if ch == "[" else "}"
                j = s.find(close, i + 1)
                if j < 0 or j == i + 1:
                    raise StepError(index, f"an unclosed or empty {ch} at "
                                           f"character {i + 1}; write {ch}{ch} "
                                           f"for a literal one")
                flush()
                body = s[i + 1:j]
                mods, key = self.chord(index, body)
                self.emit(index, "tap", key=key, mods=mods or None,
                          timeout=timeout, **{"in": in_sel})
                said.append(body)
                i = j + 1
                continue
            run.append(ch)
            i += 1
        flush()
        if not said:
            raise StepError(index, "an empty string types nothing")
        self.phrases.append("type " + " + ".join(said))

    def chord(self, index: int, body: str) -> tuple[int, str | bytes]:
        parts = body.split("+")
        # `[Ctrl++]`: a trailing empty part is the plus key itself.
        if len(parts) > 1 and parts[-1] == "" and parts[-2] == "":
            parts = [*parts[:-2], "+"]
        *mods, key = parts
        bits = 0
        for m in mods:
            b = K.MODIFIERS.get(m.strip().lower())
            if b is None:
                raise StepError(index, f"{m!r} in [{body}] is not a modifier "
                                       f"(Ctrl, Alt, Shift)")
            bits |= b
        if len(key) == 1:
            return bits, self.text_bytes(index, key)
        c = self.key(index, key)
        if c == "delete" and bits & 6 == 6:
            raise StepError(index, "Ctrl+Alt+Delete is never sent")
        return bits, c

    def hold(self, index: int, step: dict, kind: str) -> None:
        """`down`/`up`: a key, or a mouse button -- which may say `where`
        (a selector, or {"screen": [x, y]}) and the target fields, to press
        or release at a place rather than where the pointer already is."""
        name = step[kind]
        if not isinstance(name, str) or not name.strip():
            raise StepError(index, f"`{kind}` names a key or a mouse button")
        n = name.strip().lower()
        button = K.BUTTONS.get(n)
        placed = any(k in step for k in _TARGET_KEYS)
        if button:
            fields: dict = {}
            if "where" in step:
                spec = {k: v for k, v in step.items() if k in _TARGET_KEYS}
                spec["move"] = step["where"]
                fields = self.target(index, spec, "move")
            elif placed:
                raise StepError(index, "say `where` the button goes, as a "
                                       "selector or {\"screen\": [x, y]}")
            self.balance(index, kind, n)
            self.emit(index, "bd" if kind == "down" else "bu", button=button,
                      timeout=step.get("timeout"), **fields)
            self.phrases.append(
                f"{'press' if kind == 'down' else 'release'} the {button} "
                f"button" + (f" on {_where(fields)}" if fields else ""))
            return
        if "where" in step or placed:
            raise StepError(index, "a key has no position; only a mouse "
                                   "button takes `where`")
        c = self.key(index, name)
        if c == "delete" and kind == "down" and {"ctrl", "alt"} <= {
                _MOD_FAMILY.get(h, h) for h in self.held}:
            raise StepError(index, "Ctrl+Alt+Delete is never sent")
        self.balance(index, kind, c)
        self.emit(index, "kd" if kind == "down" else "ku", key=c,
                  timeout=step.get("timeout"))
        self.phrases.append(f"{'hold' if kind == 'down' else 'release'} {c}")

    def balance(self, index: int, kind: str, name: str) -> None:
        if kind == "down":
            if name in self.held:
                raise StepError(index, f"{name} is already held (step "
                                       f"{self.held[name]})")
            self.held[name] = index
        else:
            if name not in self.held:
                raise StepError(index, f"{name} is released but was never "
                                       f"held")
            del self.held[name]

    def step(self, index: int, step) -> None:
        if isinstance(step, str):
            self.string(index, step, None)
            return
        if not isinstance(step, dict) or not step:
            raise StepError(index, "a step is a string to type or an object "
                                   "naming one action")
        kinds = [k for k in step if k in _KINDS]
        if len(kinds) != 1:
            raise StepError(index, "a step names exactly one action: "
                                   + ", ".join(sorted(_KINDS)))
        kind = kinds[0]
        extra = set(step) - {kind, "timeout"} - _KINDS[kind]
        if extra:
            raise StepError(index, f"`{kind}` does not take "
                                   f"{', '.join(sorted(extra))}")
        timeout = None
        if "timeout" in step:
            timeout = _int(index, "timeout", step["timeout"], 1,
                           STEP_TIMEOUT_MAX)
        sel = step[kind]

        if kind == "type":
            if not isinstance(sel, str):
                raise StepError(index, "`type` takes the text to type")
            self.string(index, sel, timeout, str(step.get("in") or ""))
        elif kind in ("focus", "close", "wait_for", "wait_gone", "wait_text"):
            if not isinstance(sel, str) or not sel.strip():
                raise StepError(index, f"`{kind}` needs a selector")
            verb = {"focus": "focus", "close": "close", "wait_for": "waitfor",
                    "wait_gone": "waitgone", "wait_text": "waittext"}[kind]
            fields: dict = {"sel": sel, "timeout": timeout}
            if kind == "wait_text":
                if not isinstance(step.get("text"), str) or not step["text"]:
                    raise StepError(index, "`wait_text` needs the `text` to "
                                           "wait for")
                fields["text"] = self.text_bytes(index, step["text"])
            self.emit(index, verb, **fields)
            self.phrases.append({
                "focus": f"bring {sel} to the front",
                "close": f"close {sel}",
                "wait_for": f"wait for {sel}",
                "wait_gone": f"wait for {sel} to go",
                "wait_text": f"wait for {sel} to say "
                             f"{_quote(step.get('text', ''))}"}[kind])
        elif kind == "click":
            fields = self.target(index, step, kind)
            button = step.get("button", "left")
            if button not in ("left", "right", "middle"):
                raise StepError(index, '`button` is "left", "right" or '
                                       '"middle"')
            if self.held_buttons():
                raise StepError(index, "a click while a mouse button is held "
                                       "would release it; use `up` first")
            self.emit(index, "click", button=button,
                      count=2 if step.get("double") else None,
                      timeout=timeout, **fields)
            self.phrases.append(
                f"{'double-' if step.get('double') else ''}"
                f"{'' if button == 'left' else button + '-'}click "
                f"{_where(fields)}")
        elif kind == "move":
            fields = self.target(index, step, kind)
            self.emit(index, "mv", timeout=timeout, **fields)
            self.phrases.append(f"move the pointer to {_where(fields)}")
        elif kind == "move_by":
            dx, dy = _pair(index, "move_by", sel)
            self.emit(index, "mvr", d=f"{dx},{dy}", timeout=timeout)
            self.phrases.append(f"move the pointer by {dx},{dy}")
        elif kind in ("down", "up"):
            self.hold(index, {**step, kind: sel}, kind)
        elif kind == "wait":
            ms = _int(index, "wait", sel, 0, WAIT_MAX)
            self.emit(index, "wait", ms=ms, timeout=timeout)
            self.phrases.append(f"pause {ms} ms")
        elif kind == "idle":
            ms = _int(index, "idle", sel, 1, SETTLE_MAX)
            self.emit(index, "idle", ms=ms, timeout=timeout)
            self.phrases.append(f"wait for the screen to settle ({ms} ms)")

    def held_buttons(self) -> bool:
        return any(h in K.BUTTONS for h in self.held)


def _quote(text: str) -> str:
    t = text.replace("\n", "\\n")
    return f'"{t[:40]}..."' if len(t) > 40 else f'"{t}"'


def _where(fields: dict) -> str:
    if "screen" in fields:
        return f"screen point {fields['screen']}"
    out = fields.get("sel", "")
    if "item" in fields:
        out += f" item {_quote(fields['item'])}"
    if "text" in fields:
        raw = fields["text"]
        text = raw.decode("cp1252", "replace") if isinstance(raw, bytes) \
            else raw
        out += f" at the text {_quote(text)}"
    if "at" in fields:
        out += f" + {fields['at']}"
    return out


def compile_steps(steps, *, timeout=None, settle=None, encode=None,
                  keys_version: int = K.LATEST) -> Compiled:
    """Check `steps` and encode them, or raise StepError naming the step.

    `encode` turns text into the guest's code-page bytes and must raise
    UnicodeEncodeError rather than approximate (wire.fold.encode_exact)."""
    if encode is None:
        encode = lambda s: fold.encode_exact(s, "cp1252")  # noqa: E731
    if not isinstance(steps, list) or not steps:
        raise StepError(0, "`steps` is a list of at least one step")
    script = SCRIPT_TIMEOUT_DEFAULT
    if timeout is not None:
        script = _int(0, "timeout", timeout, 1, SCRIPT_TIMEOUT_MAX)
    settle_ms = 0
    if settle is not None:
        settle_ms = _int(0, "settle", settle, 0, SETTLE_MAX)

    c = _Compiler(encode=encode, keys_version=keys_version)
    for i, step in enumerate(steps, 1):
        c.step(i, step)
    if c.held:
        name, where = next(iter(c.held.items()))
        raise StepError(where, f"{name} is held here and nothing releases "
                               f"it; add an `up` for it")
    body = b"\n".join(c.lines) + b"\n"
    if len(body) > MAX_BODY:
        raise StepError(0, f"the list encodes to {len(body)} bytes; one call "
                           f"may carry {MAX_BODY} -- split it")
    return Compiled(body=body, timeout_ms=script, settle=settle_ms,
                    origin=c.origin, phrases=c.phrases)


def summary(steps) -> str:
    """The plain-language line for the permission bar: what every step will
    DO, in order, so approving one call is approving what it says. Never
    raises -- a list that will be refused still needs a line."""
    try:
        return " · ".join(compile_steps(steps).phrases)
    except (StepError, TypeError, ValueError):
        n = len(steps) if isinstance(steps, list) else 0
        return f"{n} screen steps (malformed)"


def parse_reply(body: bytes) -> tuple[list[dict], bool]:
    """The guest's per-step lines, and whether it released held input."""
    rows: list[dict] = []
    released = False
    for raw in body.split(b"\n"):
        if not raw:
            continue
        if raw == b"released=1":
            released = True
            continue
        row: dict = {}
        for part in raw.split(b"\t"):
            k, _, v = part.partition(b"=")
            row[k.decode("ascii", "replace")] = _unesc(v)
        rows.append(row)
    return rows, released


def _unesc(v: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(v):
        if v[i] == 0x25 and i + 2 < len(v):
            try:
                out.append(int(v[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(v[i])
        i += 1
    return bytes(out)
