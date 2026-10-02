"""Selectors on the bridge side: splitting one the agent wrote, and
suggesting one when the guest says several controls matched.

The grammar itself lives on the guest (win/automation/select.h) and is not
re-implemented here. This module only needs to find step boundaries, and to
write selectors the guest's parser accepts.
"""
from __future__ import annotations

from dataclasses import dataclass


def split_steps(selector: str) -> list[str]:
    """The steps of a selector, in order.

    A step ends at a top-level `>` OR at whitespace -- `A B` is two steps
    (any descendant), exactly as the guest counts them. The first version of
    this split on `>` alone, so for `A B` it blamed the whole selector when
    the guest said step 1 was missing.

    Neither separates inside `[...]` or inside quotes: `Button[text="a > b"]`
    and `[text~="save as"]` are one step each.
    """
    steps: list[str] = []
    cur: list[str] = []
    quote = ""
    bracket = 0

    def flush() -> None:
        if cur:
            steps.append("".join(cur))
            cur.clear()

    for ch in selector:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'" and bracket:
            quote = ch
            cur.append(ch)
            continue
        if ch == "[":
            bracket += 1
        elif ch == "]" and bracket:
            bracket -= 1
        if not bracket and (ch == ">" or ch.isspace()):
            flush()
            continue
        cur.append(ch)
    flush()
    return steps


@dataclass(frozen=True)
class Candidate:
    """One of the controls that matched when one was wanted, with the two
    ancestors worth narrowing by. Any of these may be empty."""
    cls: str
    text: str
    parent_cls: str
    parent_text: str
    root_cls: str
    root_text: str


def decode_candidate(value: str) -> Candidate | None:
    """Six `<len>:<bytes>` fields (win/ui/uirec.h, rc_uirec_candidate).

    None for anything malformed: a candidate is advice, and a bad one is
    dropped rather than allowed to take the error message down with it."""
    fields: list[str] = []
    i = 0
    try:
        while i < len(value) and len(fields) < 6:
            colon = value.index(":", i)
            n = int(value[i:colon])
            start = colon + 1
            fields.append(value[start:start + n])
            i = start + n
    except ValueError:
        return None
    if len(fields) != 6 or i != len(value):
        return None
    return Candidate(*fields)


def strip_mnemonic(caption: str) -> str:
    """A caption as the person reads it: a lone `&` gone, `&&` one `&`."""
    out: list[str] = []
    i = 0
    while i < len(caption):
        ch = caption[i]
        if ch == "&":
            i += 1
            if i < len(caption) and caption[i] == "&":
                out.append("&")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _step(cls: str, text: str) -> str:
    """One selector step for a class and caption, as a glob prefix.

    A PREFIX glob because the guest may have cut the caption to fit a header,
    and a cut caption is still a prefix of the real one. A quote the caption
    contains picks the other quote; a caption holding both is left out --
    the class alone is still a valid step, if a less specific one."""
    cls_part = cls if cls and _bare_class(cls) else (
        f'[class="{cls}"]' if cls else "*")
    plain = strip_mnemonic(text).strip()
    if not plain:
        return cls_part
    for q in ('"', "'"):
        if q not in plain:
            return f"{cls_part}[text~={q}{plain}*{q}]"
    return cls_part


def _bare_class(cls: str) -> bool:
    return all(c.isalnum() or c in "_-." for c in cls)


def suggest(c: Candidate, leaf: str) -> str:
    """A selector that names this candidate through its top-level window.

    `leaf` is the last step of what the agent wrote, kept as written: it
    already matches the control, and only the ancestry needs adding. The
    root is joined with a SPACE (any descendant) -- naming the window is what
    tells two "OK" buttons apart, and the depth between them is noise."""
    if c.root_cls or c.root_text:
        return f"{_step(c.root_cls, c.root_text)} {leaf}"
    if c.parent_cls or c.parent_text:
        return f"{_step(c.parent_cls, c.parent_text)} {leaf}"
    return ""


def render_candidates(headers: dict, target: str) -> str:
    """The candidate lines for an `ambiguous` reply, or "" when the guest
    sent none (an older client sends only the count)."""
    steps = split_steps(target) if target else []
    leaf = steps[-1] if steps else ""
    lines: list[str] = []
    for key in ("cand1", "cand2", "cand3"):
        c = decode_candidate(headers.get(key, ""))
        if not c:
            continue
        where = ", ".join(
            f"{cls} \"{strip_mnemonic(txt)}\"" if txt else cls
            for cls, txt in ((c.parent_cls, c.parent_text),
                             (c.root_cls, c.root_text)) if cls)
        line = f"- {c.cls} \"{strip_mnemonic(c.text)}\""
        if where:
            line += f" in {where}"
        hint = suggest(c, leaf) if leaf else ""
        if hint:
            line += f" -- try `{hint}`"
        lines.append(line)
    return "\n".join(lines)
