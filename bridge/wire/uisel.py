"""Give every control a selector that resolves to it and nothing else.

This is the bridge's half of the addressing rule in ``docs/UIAUTOMATION.md``:
an HWND never leaves the guest, so the agent points at a control with a
selector, and the selector has to be one the guest's ``Resolver`` will
actually resolve. Getting it wrong is not a cosmetic failure -- an ambiguous
selector makes the guest refuse, and a *wrong* one clicks the wrong control.

The bridge is the only end that can do this. It holds the whole tree at once
and can ask "is ``Button[text="OK"]`` unique here?"; the guest streams one
record at a time and has nothing to compare against.

Three rules the generator follows, in order:

1. **The cheapest thing that is unique.** A class on its own, then a class
   with its text, then a control id.
2. **Then ancestry**, one ``>`` step at a time up the real parent chain.
   Direct-child steps only -- a descendant step would be shorter to write
   and much harder to be sure about.
3. **Then, and only then, an ordinal.** ``:nth(k)`` is a last resort because
   it is the one part that breaks when the desktop shifts under it, and
   because the guest's ambiguity error exists precisely so a human or a
   model chooses the ordinal deliberately.
"""

from __future__ import annotations

from .uirec import UiNode

#: How many ancestor steps to try before giving up and using an ordinal.
#: The measured tree is four to six deep and a selector rarely needs every
#: level; past three the selector is longer than the thing it identifies.
MAX_STEPS = 4

#: Text longer than this is not a useful discriminator -- it is a document
#: in a status bar -- and it costs tokens on every record.
MAX_TEXT = 40


def _quotable(s: str) -> bool:
    """Can this text go inside ``[text="..."]`` at all?

    The guest's parser takes a quoted run and stops at the closing quote, so
    a value containing one would truncate into something that still parses
    and means something else. Control characters have the same problem for a
    different reason: a newline in a selector is not representable, and an
    Edit control's contents are full of them.
    """
    if not s or len(s) > MAX_TEXT:
        return False
    return all(0x20 <= ord(c) < 0x7F and c not in '"\\' for c in s)


def _bare(cls: str) -> bool:
    """Can this class name be written bare, or does it need `[class=...]`?

    A Win32 dialog's class is literally ``#32770``, and a bare ``#`` in the
    grammar introduces a control id -- so the one class an agent most needs
    to name is the one that cannot be written plainly.
    """
    return bool(cls) and (cls[0].isalpha() or cls[0] == "_") and all(
        c.isalnum() or c in "_." for c in cls)


def _steps_for(n: UiNode) -> list[str]:
    """Ways to name this one node, cheapest first."""
    out: list[str] = []
    cls = n.cls if _quotable(n.cls) else ""
    label = n.text or n.name

    if cls:
        head = cls if _bare(cls) else f'[class="{cls}"]'
        out.append(head)
        if _quotable(label):
            out.append(f'{head}[text="{label}"]')
        if n.ctrl:
            out.append(f"{head}#{n.ctrl}")
    else:
        # No class means it is not a window -- an accessible element. The
        # grammar cannot name one yet (docs/UIAUTOMATION.md says so in as
        # many words), so it gets a role step that will not resolve and is
        # marked as such by the caller rather than silently looking valid.
        if n.role:
            out.append(f"[role={n.role}]")
    if n.ctrl and not cls:
        out.append(f"#{n.ctrl}")
    return out or [""]


def _step_matches(step: str, n: UiNode) -> bool:
    """Evaluate one generated step. Only the forms `_steps_for` emits."""
    cls, ctrl, text, role = step, None, None, None
    if "[text=" in cls:
        cls, rest = cls.split("[text=", 1)
        text = rest[1:rest.rindex('"')]
    elif cls.startswith("[role="):
        role = cls[6:-1]
        cls = ""
    if cls.startswith('[class="'):
        end = cls.index('"]')
        rest = cls[end + 2:]
        cls = cls[8:end]
        # `[class="X"]#7` -- the id hangs off the end rather than inside.
        if rest.startswith("#"):
            ctrl = int(rest[1:])
    elif "#" in cls:
        cls, num = cls.split("#", 1)
        ctrl = int(num)

    if cls and n.cls != cls:
        return False
    if ctrl is not None and n.ctrl != ctrl:
        return False
    if text is not None and (n.text or n.name) != text:
        return False
    if role is not None and n.role != role:
        return False
    return True


def _chain_matches(steps: list[str], n: UiNode, by_path: dict[str, UiNode]) -> bool:
    """`steps` is leaf-last, every combinator a direct child."""
    cur: UiNode | None = n
    for step in reversed(steps):
        if cur is None or not _step_matches(step, cur):
            return False
        cur = by_path.get(cur.parent) if cur.parent else None
    return True


def build_selectors(nodes: list[UiNode]) -> None:
    """Fill in ``sel`` on every node, in place.

    Order matters and is the walk's: ``:nth(k)`` counts matches in the order
    the guest's depth-first walk produces them, so the ordinals assigned here
    are only right because ``nodes`` is still in wire order.
    """
    by_path = {n.path: n for n in nodes}

    for n in nodes:
        chain: list[UiNode] = []
        cur: UiNode | None = n
        while cur is not None and len(chain) < MAX_STEPS:
            chain.append(cur)
            cur = by_path.get(cur.parent) if cur.parent else None

        best = ""
        for depth in range(len(chain)):
            # One step for the node, then its parent, then its parent's.
            for leaf in _steps_for(n):
                steps = [leaf]
                for anc in chain[1:depth + 1]:
                    steps.insert(0, _steps_for(anc)[0])
                hits = [m for m in nodes if _chain_matches(steps, m, by_path)]
                if len(hits) == 1:
                    best = " > ".join(steps)
                    break
                if not best and hits:
                    # Remember the tightest thing that at least matches us,
                    # so the ordinal below is attached to a real selector
                    # rather than to a bare class name.
                    best = " > ".join(steps)
            if best and len(
                    [m for m in nodes
                     if _chain_matches(best.split(" > "), m, by_path)]) == 1:
                break

        if not best:
            n.sel = ""
            continue

        hits = [m for m in nodes if _chain_matches(best.split(" > "), m, by_path)]
        if len(hits) == 1:
            n.sel = best
        else:
            # The ordinal is 1-based and counts in walk order, which is the
            # order the guest will rediscover it in.
            n.sel = f"{best}:nth({hits.index(n) + 1})"
