"""The class names AND container ids the guest hard-codes must exist on the bridge.

The bridge renders the transcript and owns the stylesheet, which the guest
receives whole in the DOC frame. But the guest builds three blocks itself --
its own echo of what you typed, a notice, an error -- and hard-codes their
class names.

That is a contract between two files in two languages with nothing linking
them. Rename `.notice` on the bridge and the guest's notices silently become
unstyled text: no error, no log line, nothing to notice until someone looks
at a screenshot.

This is a pure-Python test on purpose -- it reads the C header rather than
running the guest, so it works on a machine with no Open Watcom and no Wine
and cannot be one of the checks that skips silently.
"""

import os
import re

import conftest

from bridge.view import html as H

HERE = conftest.REPO
HEADER = os.path.join(HERE, "win", "app", "htmlview.h")


def guest_classes() -> dict:
    src = open(HEADER, encoding="utf-8").read()
    total = len(re.findall(r'#define\s+RC_CSS_\w+', src))
    found = dict(re.findall(r'#define\s+(RC_CSS_\w+)\s+"([^"]+)"', src))
    assert total > 0, "no RC_CSS_* defines found; did the header move?"
    assert len(found) == total, (
        f"matched {len(found)} of {total} RC_CSS_* defines; check regex or header syntax"
    )
    return found


def test_every_class_the_guest_emits_is_in_the_bridge_s_stylesheet():
    css = H.document()
    for macro, name in guest_classes().items():
        for cls in name.split():
            assert f".{cls}" in css, (
                f"{macro} names class \"{cls}\" and the bridge's stylesheet has no "
                f"`.{cls}` rule -- the guest's blocks would render unstyled, "
                f"silently")


def test_the_guest_and_the_bridge_agree_on_what_each_class_means():
    """Not just that the name exists, but that both sides use it for the same
    kind of block -- otherwise an error would render as a notice."""
    names = guest_classes()
    # Through the renderer, because the constructors return structure now and
    # the class name is the renderer's business. Stage 3 A7; at C2 the
    # renderer is the guest's and this half of the test becomes a check
    # against view.tpl instead.
    from bridge.view.htmlrender import render
    assert names["RC_CSS_NOTICE"] in render(H.notice("x"))
    assert names["RC_CSS_ERROR"] in render(H.error("x"))
    assert names["RC_CSS_USER"] in render(H.user_turn("x"))


def guest_slots() -> dict:
    src = open(HEADER, encoding="utf-8").read()
    found = dict(re.findall(r'#define\s+(RC_SLOT_\w+)\s+"([a-z0-9-]+)"', src))
    assert found, "no RC_SLOT_* defines found; did the header move?"
    return found


def test_the_container_ids_the_guest_looks_up_exist_in_the_skeleton():
    """03.21: the guest finds #t and #l by literal in a document the bridge wrote.

    This one is worse than an unstyled block. `find_id` returning NULL is not
    an error anywhere -- every append then goes nowhere and the transcript is
    blank forever, on a machine with no console to say so.
    """
    doc = H.document()
    for macro, ident in guest_slots().items():
        assert f'id="{ident}"' in doc, (
            f'{macro} is "{ident}" and the bridge\'s skeleton has no '
            f'element with that id -- the guest would find nothing and every '
            f'append would land in no element at all')


def test_the_two_slots_are_distinct():
    """They do different jobs: one accumulates, one is replaced wholesale.

    If both macros ever named the same element, finished blocks and the
    in-progress block would share a container and each new live update would
    wipe the transcript above it.
    """
    slots = guest_slots()
    assert slots["RC_SLOT_DONE"] != slots["RC_SLOT_LIVE"]


# --- the other direction ----------------------------------------------------
#
# Everything above walks the classes the GUEST names. A class the BRIDGE
# invents is invisible to all of it, which is how `.thinking` came to be
# emitted for as long as the class had existed with no rule anywhere:
# presentation.py built `<div class="{style}">` from the style NAME, so
# there was no literal for anything to grep and no macro for anything to
# sweep. Found while planning stage 3, fixed in A10.
#
# After C2 the renderer is the guest's and this reads win/resources/view.tpl
# instead of htmlrender.py. The assertion does not change.

def rendered_classes() -> set:
    """Every class the renderer can put in the document."""
    from bridge.view import blocks as B
    from bridge.view.htmlrender import _PERM_HEAD, render

    samples = [
        B.Block("para", {}, [("t", "x")]),
        B.Block("code", {}, [("l", "c"), ("t", "x")]),
        B.Block("heading", {"level": 3}, [("t", "x")]),
        B.Block("bullet", {}, [("t", "x")]),
        B.Block("numbered", {}, [("t", "x")]),
        B.Block("quote", {}, [("t", "x")]),
        B.Block("rule"),
        B.Block("table", {"header": 1}, [("r", ""), ("c", "x")]),
        B.Block("user", {}, [("t", "x")]),
        B.Block("notice", {}, [("t", "x")]),
        B.Block("error", {}, [("t", "x")]),
        B.Block("tool", {}, [("n", "e"), ("t", "x")]),
        B.Block("toolresult", {"error": 0, "clipped": 3}, [("t", "x")]),
        B.Block("toolresult", {"error": 1}, [("t", "x")]),
        B.Block("thinking", {}, [("t", "x")]),
        B.Block("clip", {"dropped": 3}),
        B.Block("exportheading", {}, [("h", "t"), ("t", "w")]),
    ] + [B.Block("permission", {"settleable": s},
                 [("h", h), ("b", "x"), ("d", "y"), ("w", "z"), ("p", "p"),
                  ("t", "t"), ("s", "s"), ("k", "k"), ("v", "v")])
         for s, h in ((1, "ask"), (0, "view"))]

    assert {b.kind for b in samples} == set(B.KINDS), (
        "a kind with no sample here is a kind whose classes nothing checks")

    out = set()
    for b in samples:
        for m in re.findall(r'class="([^"]+)"', render(b)):
            out.update(m.split())
    for head in _PERM_HEAD.values():
        for m in re.findall(r'class="([^"]+)"', head):
            out.update(m.split())
    return out


def test_every_class_the_renderer_emits_has_a_rule():
    """The mirror of the first test, and the one that was missing.

    A class with no rule is not an error anywhere: the block renders, it
    just looks like whatever is around it. `.thinking` looked exactly like
    the model's answer for the whole life of the feature.
    """
    # Both sheets, because an export gets WIRE_CSS + EXPORT_CSS and the
    # `exportheading` kind only ever appears there. Checking only the live
    # sheet would report a false positive for it; checking only the union
    # would miss a LIVE class that had slipped into the export-only sheet,
    # so the split is asserted too, below.
    # The repo's export.css, because the bridge's copy went at C3 with
    # export_document. The live sheet is still the bridge's until C4.
    css = H.STYLESHEET + repo_css("export.css")
    missing = sorted(c for c in rendered_classes() if f".{c}" not in css)
    assert not missing, (
        f"the renderer emits {missing} and no stylesheet has a rule for "
        f"them -- those blocks render as whatever is around them, with no "
        f"error and no log line")


def test_only_the_export_heading_depends_on_the_export_only_sheet():
    """The live view never gets EXPORT_CSS, so a class that drifted into it
    would be unstyled on screen and fine in a saved file -- which is the
    hardest version of this bug to notice, because the export looks right."""
    live_only = {c for c in rendered_classes()
                 if f".{c}" not in H.STYLESHEET}
    assert live_only == {"exporthead"}, (
        f"{sorted(live_only - {'exporthead'})} are emitted into the live "
        f"view but only styled in EXPORT_CSS")


def test_the_two_sweeps_overlap_where_they_should():
    """Both sides name .notice, .err and .you, which is the whole point of
    the first test -- it is a contract, not a coincidence. If this ever
    fails, one of the two sweeps has stopped seeing what it was written
    for."""
    shared = {c for c in guest_classes().values() for c in c.split()}
    assert {"notice", "err", "you"} <= shared
    assert {"notice", "err", "you"} <= rendered_classes()


# --- two copies, briefly ----------------------------------------------------
#
# Stage 3 B3 ships win/resources/view.css and export.css beside the exe, and
# the bridge keeps STYLESHEET and EXPORT_CSS until C4 deletes DOC. So there
# ARE two copies of the stylesheet for the length of four commits -- exactly
# the drift the generator in mktools-iso.sh existed to prevent, and its
# comment said so: "two copies of a stylesheet drift".
#
# The generator could not survive, because after C4 the bridge has no
# stylesheet to generate from. This test is what stands in its place, and it
# DIES WITH THE BRIDGE'S COPY at C4 rather than being carried forward: once
# there is one copy there is nothing to compare.

def repo_css(name: str) -> str:
    path = os.path.join(HERE, "win", "resources", name)
    with open(path, encoding="cp1252") as f:
        return f.read()


def rules_of(css: str) -> list:
    """The rules, normalised. Comments go, because the two copies differ in
    them ON PURPOSE -- the file beside the exe explains itself to whoever is
    editing it, the bridge's to whoever maintains the bridge -- and so does
    whitespace between and inside rules, which CSS does not treat as
    content and which a reformatted copy would otherwise fail on.

    What is left is the thing that must not drift: the selectors and the
    declarations, in order."""
    out = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = re.sub(r"\s+", " ", out)
    return [r.strip() + "}" for r in out.split("}") if r.strip()]


def test_the_shipped_stylesheet_is_the_bridge_s_modulo_comments():
    assert rules_of(repo_css("view.css")) == rules_of(H.STYLESHEET)


def test_the_export_sheet_holds_only_what_the_live_view_lacks():
    """It was compared against the bridge's EXPORT_CSS until C3, when that
    went with export_document -- the guest renders its own export now and
    there is no second copy left to drift from.

    What is still worth asserting is the SPLIT: export.css must hold only
    rules the live sheet does not, or an export and the screen disagree
    about something the live view was supposed to own."""
    live = {r.split("{")[0].strip() for r in rules_of(repo_css("view.css"))}
    export = {r.split("{")[0].strip()
              for r in rules_of(repo_css("export.css"))}
    assert export and not (export & live), export & live


def test_the_shipped_sheets_declare_the_version_the_code_expects():
    """A stale view.css against a newer client is the state a hand-updated
    box gets into, so the client says so. The marker has to be where the
    client reads it."""
    src = open(os.path.join(HERE, "win", "view", "render.h"),
               encoding="utf-8").read()
    want = re.search(r"#define RC_VIEW_VERSION\s+(\d+)", src)
    assert want, "RC_VIEW_VERSION moved"
    for name in ("view.css", "export.css"):
        got = re.search(r"view-version:\s*(\d+)", repo_css(name))
        assert got, f"{name} carries no view-version marker"
        assert got.group(1) == want.group(1), name


# --- the charset the two sides declare --------------------------------------


def test_the_guest_and_the_bridge_name_the_same_charset_for_a_codepage():
    """The <meta> must name the table the bytes were written with.

    From stage 3 B4 the guest composes its own document and derives the name
    from GetACP(); the bridge derives it from the codec it folds with. Two
    derivations of one fact, in two languages, with nothing linking them --
    and the failure is mojibake rather than an error, which is the kind
    nobody reports as a bug because it looks like a font problem.

    The CJK entries are the ones that matter: their IANA names are not their
    numbers, and `windows-932` is not a charset any browser of the era
    knows.
    """
    from bridge.files.caps import html_charset

    src = open(os.path.join(HERE, "win", "app", "htmlview.cpp"),
               encoding="utf-8").read()
    body = re.search(r"static const char \*acp_charset\(\)\s*\{(.*?)\n\}",
                     src, re.S)
    assert body, "acp_charset moved; this test reads it by shape"

    guest = dict(re.findall(r'case\s+(\d+):\s*return\s+"([^"]+)";',
                            body.group(1)))
    assert guest, "no cases matched"
    for acp, name in guest.items():
        assert html_charset(f"cp{acp}") == name, (acp, name)

    # And the default arm, which is where cp1252 lands.
    assert "windows-%u" in body.group(1)
    assert html_charset("cp1252") == "windows-1252"


def test_the_skeleton_the_guest_writes_holds_the_slots_it_looks_up():
    """find_id returning NULL is not an error anywhere: every append then
    goes nowhere and the transcript is blank forever, on a machine with no
    console to say so. The bridge's skeleton was checked for this; the
    guest's has to be too, and from B4 the guest's is the one that runs at
    cold start."""
    src = open(os.path.join(HERE, "win", "app", "htmlview.cpp"),
               encoding="utf-8").read()
    tail = re.search(r'static const char SKELETON_TAIL\[\] =(.*?);', src,
                     re.S)
    assert tail, "SKELETON_TAIL moved"
    for macro in guest_slots():
        assert macro in tail.group(1), (
            f"{macro} is looked up by find_id but is not in the skeleton "
            f"the guest writes")
