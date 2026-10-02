"""Put a control tree, and a screenshot, in front of a model.

Two jobs that look unrelated and are the same one: the whole reason the tree
exists is that it says more than a picture for a fraction of the tokens, so
the rendering has to actually be terse. A desktop is 83 windows and most of
them are scrollbars.

Rules the layout follows:

* the selector is the point of every line, because it is the only thing the
  model can act on, so it is never elided;
* a control with nothing to say -- no text, no name, no id -- gets one short
  line rather than a row of empty columns;
* anything that makes the answer incomplete is said at the top, not left for
  the reader to infer from a short list.
"""

from __future__ import annotations

import io
import logging

from ..agent import prompts
from .uirec import UiNode

log = logging.getLogger("bridge.uiview")

#: Beyond this a line is doing more harm than good in a transcript.
MAX_LABEL = 60


def _label(n: UiNode) -> str:
    s = n.text or n.name
    if not s:
        return ""
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\t", " ")
    s = " ".join(s.split())
    if len(s) > MAX_LABEL:
        s = s[:MAX_LABEL - 1] + "…"
    return s


def _depth(n: UiNode) -> int:
    return n.path.count("/")


def render_tree(nodes: list[UiNode], headers: dict) -> str:
    """One line per control, indented by depth."""
    if not nodes:
        if int(headers.get("self", "0") or 0):
            return (prompts.text("tools.ui.view.self_hidden",
                                 n=headers["self"])
                    + "\n" + prompts.text("tools.ui.view.empty"))
        return prompts.text("tools.ui.view.empty")

    out: list[str] = []
    walk = headers.get("walk", "")
    # Said first, because a short list read as a complete one is how an
    # agent concludes a control is not there.
    if walk == "deadline":
        out.append(prompts.text("tools.ui.view.incomplete"))
    elif walk == "maxnodes":
        out.append(prompts.text("tools.ui.view.at_limit", n=len(nodes)))
    if int(headers.get("dropped", "0") or 0):
        out.append(prompts.text("tools.ui.view.dropped", n=headers["dropped"]))
    if int(headers.get("too_deep", "0") or 0):
        out.append(prompts.text("tools.ui.view.too_deep",
                                n=headers["too_deep"]))
    # Not a defect and not a limit: a rule. It is said anyway, because the
    # absence of a window an agent was asked about reads as "not open", and
    # the agent then hunts for it instead of answering.
    if int(headers.get("self", "0") or 0):
        out.append(prompts.text("tools.ui.view.self_hidden",
                                n=headers["self"]))

    for n in nodes:
        bits: list[str] = []
        head = n.role or n.cls or ("element" if n.src == "a" else "window")
        label = _label(n)
        if label:
            bits.append(f'"{label}"')
        w = n.rect[2] - n.rect[0]
        h = n.rect[3] - n.rect[1]
        bits.append(f"{n.rect[0]},{n.rect[1]} {w}x{h}")
        if not n.enabled:
            bits.append("disabled")
        if n.focused:
            bits.append("focused")
        if n.text_timed_out:
            # Not the same as empty: one means it has nothing to say, the
            # other means it did not answer.
            bits.append("did-not-answer")
        if n.state:
            bits.append(n.state)
        if n.action:
            bits.append(f"does:{n.action}")

        line = "  " * _depth(n) + head + "  " + "  ".join(bits)
        if n.sel:
            line += f"  sel={n.sel}"
        # An accessible element has no selector -- it is not a window. What
        # addresses it is its NAME, passed as `input`'s `item` beside the
        # control's selector; said here in the form to copy.
        if n.src == "a" and label:
            line += f'  item="{label}"'

        out.append(line)

    detail = headers.get("enriched", "0") or "0"
    out.append(f"({len(nodes)} controls"
               + (f", {detail} asked about in detail"
                  if int(detail) else "")
               + ")")
    return "\n".join(out)


def to_png(bmp: bytes) -> bytes | None:
    """A guest BMP as a PNG.

    The guest sends a native-bpp DIB because that is a third the bytes of
    24bpp on an 8bpp display and costs it no conversion -- the conversion
    belongs here, per the rule that the guest transforms nothing.

    None rather than an exception when Pillow is missing: a bridge without
    it should say so and carry on, not fail the tool.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("no Pillow; a screenshot cannot be converted")
        return None
    try:
        src = Image.open(io.BytesIO(bmp))
        src.load()
        img: Image.Image = src
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        log.exception("a %d-byte capture would not decode as a BMP", len(bmp))
        return None
