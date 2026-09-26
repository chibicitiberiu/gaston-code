"""Every control gets a selector that resolves to it and nothing else.

This is the bridge's half of the addressing rule: an HWND never leaves the
guest, so if the selector is wrong the agent clicks the wrong control. An
ambiguous one is merely refused by the guest; a *confidently wrong* one is
the failure the whole design is shaped around.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from bridge.wire.uirec import UiNode, NF_VISIBLE, NF_ENABLED  # noqa: E402
from bridge.wire import uisel  # noqa: E402


def n(path, cls, text="", ctrl=0, parent=None, role="", src="w"):
    if parent is None:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
    return UiNode(src=src, path=path, parent=parent, ctrl=ctrl, child=0,
                  rect=(0, 0, 10, 10), flags=NF_VISIBLE | NF_ENABLED,
                  cls=cls, text=text, role=role)


def resolves_to(nodes, sel):
    """Every node the generated selector matches, the way the guest would."""
    by_path = {x.path: x for x in nodes}
    steps = sel.split(":nth(")[0].split(" > ")
    hits = [m for m in nodes if uisel._chain_matches(steps, m, by_path)]
    if ":nth(" in sel:
        k = int(sel.split(":nth(")[1].rstrip(")"))
        return [hits[k - 1]] if 0 < k <= len(hits) else []
    return hits


def test_every_selector_resolves_to_exactly_its_own_node():
    """The property that matters. Not 'a selector was produced'."""
    tree = [
        n("0", "#32770", "Save As"),
        n("0/0", "Static", "File name:"),
        n("0/1", "Edit", "", ctrl=1001),
        n("0/2", "Button", "OK", ctrl=1),
        n("0/3", "Button", "Cancel", ctrl=2),
    ]
    uisel.build_selectors(tree)
    for node in tree:
        assert node.sel, node.path
        assert resolves_to(tree, node.sel) == [node], (node.path, node.sel)


def test_a_dialog_class_is_addressable():
    """`#32770` written bare parses as a CONTROL ID, so the one class an
    agent most needs to name needs the attribute form."""
    tree = [n("0", "#32770", "Save As")]
    uisel.build_selectors(tree)
    assert tree[0].sel == '[class="#32770"]'


def test_the_cheapest_unique_thing_wins():
    """A lone Button needs nothing but its class."""
    tree = [n("0", "#32770", "Dlg"), n("0/0", "Button", "OK", ctrl=1)]
    uisel.build_selectors(tree)
    assert tree[1].sel == "Button"


def test_text_disambiguates_before_ancestry():
    tree = [
        n("0", "#32770", "Dlg"),
        n("0/0", "Button", "OK", ctrl=1),
        n("0/1", "Button", "Cancel", ctrl=2),
    ]
    uisel.build_selectors(tree)
    assert tree[1].sel == 'Button[text="OK"]'
    assert tree[2].sel == 'Button[text="Cancel"]'


def test_ancestry_disambiguates_identical_controls():
    """Two dialogs, each with an OK button, identical in every field except
    who their parent is."""
    tree = [
        n("0", "Shell_TrayWnd", "Taskbar"),
        n("1", "#32770", "Save As"),
        n("1/0", "Button", "OK", ctrl=1),
        n("2", "#32770", "Print"),
        n("2/0", "Button", "OK", ctrl=1),
    ]
    uisel.build_selectors(tree)
    for node in tree:
        assert resolves_to(tree, node.sel) == [node], (node.path, node.sel)
    #: The two OK buttons must not have been given the same selector.
    assert tree[2].sel != tree[4].sel


def test_an_ordinal_is_the_last_resort_and_is_correct():
    """Three controls alike all the way to the root. Nothing but position
    tells them apart, so :nth is right -- and it must count in WALK order,
    because that is the order the guest will rediscover them in."""
    tree = [n("0", "#32770", "Dlg")] + [
        n(f"0/{i}", "Button", "") for i in range(3)
    ]
    uisel.build_selectors(tree)
    ords = [t.sel for t in tree[1:]]
    assert all(":nth(" in s for s in ords), ords
    assert [int(s.split(":nth(")[1].rstrip(")")) for s in ords] == [1, 2, 3]
    for node in tree[1:]:
        assert resolves_to(tree, node.sel) == [node], (node.path, node.sel)


def test_text_with_a_quote_is_not_put_in_a_selector():
    """The guest's parser stops at the closing quote, so a value containing
    one truncates into something that still parses and means something
    else."""
    tree = [n("0", "#32770", 'He said "no"'), n("0/0", "Button", 'say "hi"')]
    uisel.build_selectors(tree)
    assert '"hi"' not in tree[1].sel
    assert resolves_to(tree, tree[1].sel) == [tree[1]]


def test_multiline_text_is_not_put_in_a_selector():
    """An Edit control's contents arrive with newlines in them and a
    selector cannot carry one."""
    tree = [n("0", "#32770", "Dlg"), n("0/0", "Edit", "line one\nline two")]
    uisel.build_selectors(tree)
    assert "\n" not in tree[1].sel
    assert resolves_to(tree, tree[1].sel) == [tree[1]]


def test_a_very_long_caption_is_not_used():
    tree = [n("0", "#32770", "Dlg"), n("0/0", "Static", "x" * 300)]
    uisel.build_selectors(tree)
    assert len(tree[1].sel) < 60, tree[1].sel


def test_an_accessible_element_gets_no_false_promise():
    """The grammar cannot name a list item yet. A selector that looked
    plausible and did not resolve would be worse than an empty one."""
    tree = [n("0", "SysListView32", "Files"),
            n("0/7", "", "", parent="0", role="listitem", src="a")]
    uisel.build_selectors(tree)
    assert tree[0].sel == "SysListView32"
