"""Blocks to HTML, written to IE5's constraints. The oracle.

This is the half of the old `html.py` that stage 3 MOVES TO THE CLIENT, kept
here for the length of the stage for one reason: it is the thing the guest's
renderer is proved against. B2 feeds the same records to both and compares the
output byte for byte, which is what turns "the guest emits the same HTML
MSHTML gets today" from an intention into a fact -- and what makes
regenerating the frame goldens safe.

It is deleted when pagination lands (stage 3c), not here. Deleting it in this
stage would mean trusting a new renderer on a platform that cannot be
debugged, with no second opinion.

NOTHING ELSE MAY IMPORT THIS once C2 lands. `test_no_markup_on_the_bridge.py`
walks the bridge for angle-bracket literals and allowlists exactly this file;
C5 deletes the test and the allowlist together.

THE THREE ESCAPE MODES, because getting this wrong is the likeliest
regression in the stage and the least visible:

  record prose    & < >        `<br>` between t lines   everything but code
  record literal  & < >        newlines kept in <pre>   code, toolresult
  local text      & < > "      the guest composes it    win/app/htmlview.cpp

`esc` does NOT escape a double quote -- it is html.escape(quote=False), and
user_turn('say "hi"') emits a raw quote today. The guest's esc_into escapes
four. A renderer that escaped `"` in record content would put &quot; where
this puts `"`, in the one place none of the five escaping tests looks.
"""

from __future__ import annotations

import html as _html

from .blocks import KINDS, MARK_CLOSE, MARK_OPEN, Block

#: Where a tool-result line is broken if nothing else breaks it. MEASURED, in
#: the client's default 640x480 window on the Athlon (2026-10-03): a result
#: box shows 57 characters of 8pt Courier New before its right edge. The
#: column was 75 -- taken from a 470px pane -- and then 64 from arithmetic,
#: and both scrolled. 56 leaves one to spare. A narrower window still
#: scrolls, and the stylesheet's height expression keeps that readable.
#:
#: This LEAVES with the renderer at C2: the client knows its own window width
#: at runtime and this one was measured against a default that a resize makes
#: wrong. It is not declared at the handshake either -- the bridge has no use
#: for a number it no longer wraps with.
TOOL_WRAP_COL = 56


def esc(text: str) -> str:
    """Escape for HTML. Everything the model emits goes through this.

    Not optional and not only about tidiness: a transcript that renders
    ``<script>`` from model output would be executing it.
    """
    return _html.escape(text, quote=False)


#: Mark letter -> the element IE5 gets. The only place in the bridge that
#: knows a run's letter means a tag, and the guest's view.tpl is the place
#: that will know it instead.
_SPAN_TAG = {"c": "code", "s": "b", "e": "i"}


def spans(text: str) -> str:
    """One marked line, as HTML. Escapes everything that is not a mark.

    The stack is a real stack and not a bitmask or a slot per kind, because a
    kind nests inside itself: "_a *b* c_" is <i>a <i>b</i> c</i>.

    An unclosed run at the end is closed here rather than left dangling. A
    LIVE update carries the open block mid-write, so a truncated one is
    normal and not an error -- and handing MSHTML an unbalanced <b> leaks the
    styling into everything appended after it.
    """
    out: list[str] = []
    stack: list[str] = []
    run: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == MARK_OPEN and i + 1 < n and text[i + 1] in _SPAN_TAG:
            out.append(esc("".join(run)))
            run = []
            tag = _SPAN_TAG[text[i + 1]]
            stack.append(tag)
            out.append("<" + tag + ">")
            i += 2
            continue
        if c == MARK_CLOSE and stack:
            out.append(esc("".join(run)))
            run = []
            out.append("</" + stack.pop() + ">")
            i += 1
            continue
        # A stray mark with no letter, or a close with nothing open, is
        # dropped rather than shown. It cannot come from the parser; it could
        # come from a record built by hand or cut in the wrong place.
        if c in (MARK_OPEN, MARK_CLOSE):
            i += 1
            continue
        run.append(c)
        i += 1
    out.append(esc("".join(run)))
    while stack:
        out.append("</" + stack.pop() + ">")
    return "".join(out)


def inline(text: str) -> str:
    """Markdown source straight to HTML, in one step.

    The old path, kept because the shim's callers and three tests name it.
    After C2 nothing on the bridge renders, and this is where the two halves
    are still visible as one function: parse to marks, then draw them.
    """
    from .blocks import clean, runs
    return spans(runs(clean(text)))


def fold_long_lines(text: str, width: int = TOOL_WRAP_COL) -> str:
    """Break lines too long for the column, HERE, because the guest cannot.

    IE5 has neither `pre-wrap` nor `word-wrap`, so a `pre` holding one long
    line cannot wrap at all -- and `overflow: auto` on the box around it then
    draws a horizontal scrollbar INSIDE a box whose height is exactly one
    line, so the scrollbar sits on top of the only text there is. Observed:
    a block that was nothing but a scrollbar.

    The `pre-wrap`/`word-wrap` pair stays in the stylesheet for a guest new
    enough to honour it, but it cannot be relied on and this does not rely on
    it.

    Breaks at a space when there is one in the last third of the line, so
    prose reads normally; hard-breaks otherwise, because a 300-character path
    with no spaces in it is the case that started this.

    `width` of 0 or less is REFUSED rather than looped on: rfind(" ", 0, 0)
    is -1, cut becomes 0, and `line = line[0:]` never advances -- so the
    obvious guard-free version hangs on the first long line. The guest's
    renderer takes a wrapcol parameter from its window, so zero is reachable
    there in a way it never was here.
    """
    if width <= 0:
        return text
    out = []
    for line in text.split("\n"):
        while len(line) > width:
            cut = line.rfind(" ", width * 2 // 3, width)
            if cut <= 0:
                cut = width
                out.append(line[:cut])
            else:
                out.append(line[:cut])
                cut += 1            # the space itself is consumed by the break
            line = line[cut:]
        out.append(line)
    return "\n".join(out)


def _prose(block: Block, joiner: str = "<br>") -> str:
    return joiner.join(spans(t) for t in block.texts())


def _clip(dropped: int) -> str:
    return ('<div class="clip">-- shown to you in full; ' + str(dropped)
            + " more characters were sent to the model --</div>")


def _code(block: Block) -> str:
    lang = next((t for tag, t in block.payload if tag == "l"), "")
    body = esc("\n".join(block.texts()))
    label = f'<div class="lang">{esc(lang)}</div>' if lang else ""
    return f'<div class="codewrap">{label}<pre>{body}</pre></div>'


def _table(block: Block) -> str:
    header = block.attrs.get("header", 0)
    rows: list[list[str]] = []
    for tag, text in block.payload:
        if tag == "r":
            rows.append([])
        elif tag == "c" and rows:
            rows[-1].append(text)

    out = ["<table>"]
    for i, r in enumerate(rows):
        cell = "th" if (header and i == 0) else "td"
        out.append("<tr>" + "".join(f"<{cell}>{spans(c)}</{cell}>"
                                    for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _toolresult(block: Block, wrapcol: int = TOOL_WRAP_COL) -> str:
    err = " err" if block.attrs.get("error") else ""
    body = fold_long_lines("\n".join(block.texts()), wrapcol)
    out = f'<div class="toolresult{err}"><pre>{esc(body)}</pre>'
    dropped = block.attrs.get("clipped", 0)
    if dropped:
        out += _clip(dropped)
    return out + "</div>"


def _heading(block: Block) -> str:
    level = block.attrs.get("level", 3)
    return f"<h{level}>{_prose(block)}</h{level}>"


def _tool(block: Block) -> str:
    # esc, not inline. A tool summary is an argument list -- a path, a glob, a
    # command -- and running markdown over it would turn `*.c` into the start
    # of an emphasis run. This is what the old tool_call did and the division
    # is worth stating, because it is not obvious from the kind names which
    # side of it a kind falls on:
    #
    #   inline()  para, quote, heading, bullet, numbered, table cells
    #   esc()     user, notice, error, tool, toolresult, code, thinking
    #
    # The first group is text a person wrote prose in. The second is text that
    # is already literal, or that something else composed.
    name = next((t for tag, t in block.payload if tag == "n"), "")
    return ('<div class="tool"><span class="toolname">' + esc(name)
            + "</span> " + esc("\n".join(block.texts())) + "</div>")


#: One entry per kind in `blocks.KINDS`, which the vocabulary test asserts.
#: A dispatch table rather than a chain of ifs because the guest's renderer is
#: one too -- a function-pointer table keyed by kind -- and the two being the
#: same shape is what makes B2's comparison a comparison of behaviour rather
#: than of structure.
_RENDER = {
    "para":          lambda b: "<p>" + _prose(b) + "</p>",
    "code":          _code,
    "heading":       _heading,
    "bullet":        lambda b: "<ul>" + "".join(f"<li>{spans(x)}</li>"
                                                for x in b.texts()) + "</ul>",
    "numbered":      lambda b: "<ol>" + "".join(f"<li>{spans(x)}</li>"
                                                for x in b.texts()) + "</ol>",
    "quote":         lambda b: "<blockquote>" + _prose(b) + "</blockquote>",
    "rule":          lambda b: "<hr>",
    "table":         _table,
    "user":          lambda b: '<div class="you">' + "<br>".join(
                         esc(t) for t in b.texts()) + "</div>",
    "notice":        lambda b: '<div class="notice">' + esc(
                         "\n".join(b.texts())) + "</div>",
    "error":         lambda b: '<div class="err">' + esc(
                         "\n".join(b.texts())) + "</div>",
    "tool":          _tool,
    "toolresult":    _toolresult,
    "thinking":      lambda b: '<div class="thinking">' + esc(
                         "\n".join(b.texts())) + "</div>",
    "clip":          lambda b: _clip(b.attrs.get("dropped", 0)),
}


def render(block: Block, wrapcol: int = TOOL_WRAP_COL) -> str:
    """One block, as the HTML MSHTML gets today.

    An unknown kind renders as nothing rather than raising. The guest does the
    same, for the same reason: a kind this build has not heard of is a bridge
    newer than the client, and a transcript missing one block beats a
    transcript that stopped.

    `wrapcol` mirrors rc_render's third argument deliberately: the guest
    takes the column from its window, export uses a fixed one, and the
    equality proof needs to vary it on both sides at once. It was a DEFAULT
    ARGUMENT on fold_long_lines, which looks equivalent and is not -- a
    default is bound when the function is defined, so the module constant
    could not be overridden at all and the proof silently compared 56
    against whatever it had asked for.
    """
    fn = _RENDER.get(block.kind)
    if fn is None:
        return ""
    # A kind that DECLARES payload tags and carries none has nothing to
    # draw -- the old _render's `if not lines: return ""`, which the
    # paginator's empty guard still depends on. A kind with no tags to carry
    # is a different thing: `rule` is an <hr> and `clip` is its own sentence.
    if not block.payload and KINDS[block.kind][1]:
        return ""
    if block.kind == "toolresult":
        return _toolresult(block, wrapcol)
    return fn(block)


# --- the permission card ---------------------------------------------------
#
# The one composite kind, and the one whose payload ORDER matters: the
# request puts its argument table last while the rules view puts its
# paragraphs after the table, so a renderer that emitted by tag in a fixed
# order would get one of the two wrong.
#
# `h` names WHICH head, and carries no English. An earlier draft of the plan
# made it the head TEXT, which would have put "Permission required" and
# "Answered" on the wire -- breaking the rule that no furniture crosses it,
# and unable to serve rules_view anyway, whose head comes from
# prompts.text("policy.view.head"). So the client holds the strings. Until
# C2 the client is this file.
#
# The bold labels inside the card are furniture for the same reason: `w` and
# `p` carry only their text and "Check this one:" lives here.
#
# Card text goes through spans() rather than esc(). For text with no marks
# the two are identical -- spans escapes everything between marks and there
# are none -- so this changes nothing that exists today and lets a card
# carry emphasis where it genuinely has some. The over-budget fallback does,
# and rendering its message as a plain paragraph was the one byte-level
# difference the A8 conversion introduced.

#: Which head. The settleable card ships BOTH and lets the stylesheet choose
#: -- .permdone .permhead is display:none and .perm .permdonehead is too --
#: so answering is a class flip on the container and never a DOM text
#: rewrite. See rc_html::settle_perm.
#:
#: "view" ships one head and no done-head, because a rules view is never
#: answered. Its text duplicates bridge/prompts/policy.toml's
#: `policy.view.head` DELIBERATELY: it is card furniture like "Check this
#: one:", so it belongs to whoever draws the card, and the test in
#: tests/python/agent/test_permissions_gaston.py keeps the two in step the
#: way test_guest_css.py keeps the class names in step.
_PERM_HEAD = {
    "ask": ('<div class="permhead">Permission required</div>'
            '<div class="permdonehead">Answered</div>'),
    "view": '<div class="permhead">What can run without asking</div>',
}


def _permission(block: Block) -> str:
    out: list[str] = []
    settleable = block.attrs.get("settleable", 1)
    out.append('<div class="perm" id="perm">' if settleable
               else '<div class="perm">')

    items = block.payload
    i = 0
    n = len(items)
    open_list = open_table = False

    while i < n:
        tag, text = items[i]
        # A run ends when the tag leaves its category, not merely when a
        # non-run tag arrives: a step list followed straight by a table
        # would otherwise nest the table inside the <ol>.
        if tag != "s" and open_list:
            out.append("</ol>")
            open_list = False
        if tag not in ("k", "v") and open_table:
            out.append("</table>")
            open_table = False

        if tag == "h":
            # A head this build has not heard of is simply absent, rather
            # than the literal word at the top of a permission prompt. A
            # bridge newer than the client is the case, and a card missing
            # its heading still says what it is asking.
            out.append(_PERM_HEAD.get(text, ""))
        elif tag == "b":
            lead = "<p><b>" + spans(text) + "</b>"
            if i + 1 < n and items[i + 1][0] == "d":
                # The em dash is FURNITURE and cannot be payload: fold turns
                # a literal one into two hyphens even though cp1252 has the
                # character, so carrying it would change what is on screen.
                lead += " &mdash; " + spans(items[i + 1][1])
                i += 1
            out.append(lead + "</p>")
        elif tag == "d":
            out.append("<p>" + spans(text) + "</p>")  # a stray one
        elif tag == "w":
            out.append('<p class="permwarn"><b>Check this one:</b> '
                       + spans(text) + "</p>")
        elif tag == "p":
            out.append("<p><b>What it touches:</b> " + spans(text) + "</p>")
        elif tag == "t":
            out.append("<p>" + spans(text) + "</p>")
        elif tag == "s":
            if not open_list:
                out.append("<ol>")
                open_list = True
            out.append("<li>" + spans(text) + "</li>")
        elif tag == "k":
            if not open_table:
                out.append("<table>")
                open_table = True
            value = items[i + 1][1] if (i + 1 < n and items[i + 1][0] == "v") \
                else ""
            if i + 1 < n and items[i + 1][0] == "v":
                i += 1
            out.append("<tr><th>" + spans(text) + "</th><td>" + spans(value)
                       + "</td></tr>")
        i += 1

    if open_list:
        out.append("</ol>")
    if open_table:
        out.append("</table>")
    out.append("</div>")
    return "".join(out)


_RENDER["permission"] = _permission
_RENDER["exportheading"] = lambda b: (
    '<div class="exporthead"><h1>'
    + esc(next((t for tag, t in b.payload if tag == "h"), "") or "Conversation")
    + "</h1>"
    + "".join('<div class="notice">' + esc(t) + "</div>"
              for tag, t in b.payload if tag == "t" and t)
    + "</div>")
