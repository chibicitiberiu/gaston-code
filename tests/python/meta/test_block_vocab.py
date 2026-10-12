"""The block vocabulary is a contract between four files in three languages.

    bridge/view/blocks.py        KINDS and ATTRS -- what may be sent
    bridge/view/htmlrender.py    _RENDER -- what the oracle draws
    win/view/render.cpp          KINDS[] -- what the guest draws
    win/resources/view.tpl       the markup each one uses

Nothing links them. A kind added on one side and not another is not an
error anywhere: the block simply renders as nothing, with no log line on the
bridge and one easily-missed line on the guest. That is the same shape
test_guest_css.py exists for, and it takes the same answer -- both sides'
tables compared, with a count asserted so a dropped case fails loudly.

In meta/ rather than guest/ because it reads the tree. It needs no Wine, no
Watcom and no built binary, so it cannot be one of the checks that skips
silently.
"""

from __future__ import annotations

import os
import re

import conftest

from bridge.view import blocks as B
from bridge.view.htmlrender import _RENDER

RENDER_CPP = os.path.join(conftest.REPO, "win", "view", "render.cpp")
TEMPLATES_CPP = os.path.join(conftest.REPO, "win", "view", "templates.cpp")
TEMPLATES_H = os.path.join(conftest.REPO, "win", "view", "templates.h")
VIEW_TPL = os.path.join(conftest.REPO, "win", "resources", "view.tpl")
RENDER_H = os.path.join(conftest.REPO, "win", "view", "render.h")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def guest_kinds() -> set:
    """The kinds win/view/render.cpp dispatches on."""
    src = read(RENDER_CPP)
    table = re.search(r"static const KindRow KINDS\[\] = \{(.*?)\n\};", src,
                      re.S)
    assert table, "the KindRow table moved; this test reads it by shape"
    found = set(re.findall(r'\{\s*"([a-z]+)"\s*,', table.group(1)))
    assert found, "no kinds matched inside the table"
    return found


def guest_template_keys() -> list:
    """The KEYS array in templates.cpp, in order."""
    src = read(TEMPLATES_CPP)
    table = re.search(r"static const char \*const KEYS\[TPL_COUNT\] = \{"
                      r"(.*?)\n\};", src, re.S)
    assert table, "the KEYS array moved; this test reads it by shape"
    return re.findall(r'"([a-z_0-9]+)"', table.group(1))


def enum_names() -> list:
    """The TplKey enum in templates.h, in order, lowercased to match."""
    src = read(TEMPLATES_H)
    body = re.search(r"enum TplKey\s*\{(.*?)\n\};", src, re.S)
    assert body, "the TplKey enum moved"
    names = re.findall(r"\bTPL_([A-Z_0-9]+)\b", body.group(1))
    return [n.lower() for n in names if n not in ("COUNT",)]


def tpl_values() -> dict:
    """key -> value, as GetPrivateProfileStringA would hand them back:
    whitespace stripped from both ends."""
    out = {}
    for line in read(VIEW_TPL).splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def tpl_file_keys() -> list:
    """The keys actually in win/resources/view.tpl, in order."""
    out = []
    for line in read(VIEW_TPL).splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("["):
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            out.append(key)
    return out


# --- the kinds ---------------------------------------------------------


def test_both_renderers_know_the_same_kinds():
    assert set(B.KINDS) == guest_kinds()


def test_the_oracle_knows_the_same_kinds():
    """htmlrender is what B2 compares the guest against, so a kind it
    cannot draw is a kind the proof cannot cover."""
    assert set(B.KINDS) == set(_RENDER)


def test_the_count_is_asserted_so_a_dropped_kind_fails_loudly():
    """Without a number, deleting a kind from BOTH sides passes every
    assertion above while quietly removing it from the vocabulary."""
    assert len(B.KINDS) == 17, sorted(B.KINDS)


def test_no_kind_is_named_after_an_element():
    """The exit condition is that a client writing to a teletype can consume
    this vocabulary without discarding or parsing anything. `pre` fails
    that; `code` passes. Same for the rest: the bridge says what a thing is
    and the client picks the tag."""
    tags = {"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
            "li", "div", "span", "br", "hr", "b", "i", "em", "strong",
            "blockquote", "tr", "td", "th"}
    assert not (set(B.KINDS) & tags), set(B.KINDS) & tags


# --- the attributes ----------------------------------------------------


def test_every_attribute_a_kind_declares_has_a_range():
    for kind, (attrs, _tags) in B.KINDS.items():
        for a in attrs:
            assert a in B.ATTRS, f"{kind}.{a} has no range in ATTRS"


def test_every_range_belongs_to_some_kind():
    """An attribute nothing can carry is a range nobody checks."""
    used = {a for attrs, _ in B.KINDS.values() for a in attrs}
    assert set(B.ATTRS) == used, set(B.ATTRS) ^ used


def test_the_vocabulary_names_no_attribute_nothing_fills():
    """`depth` and `start` were in an early draft. _BULLET captures the
    indent and the marker and the parser discards both -- there is no
    nesting in the output and never has been -- so listing them would have
    had this test bless a field nothing writes."""
    assert "depth" not in B.ATTRS
    assert "start" not in B.ATTRS


# --- the versions ------------------------------------------------------


def test_both_sides_declare_the_same_block_version():
    src = read(RENDER_H)
    m = re.search(r"#define RC_BLOCKS_VERSION\s+(\d+)", src)
    assert m, "RC_BLOCKS_VERSION moved"
    assert int(m.group(1)) == B.VERSION


def test_the_template_file_declares_the_version_the_code_expects():
    src = read(RENDER_H)
    m = re.search(r"#define RC_VIEW_VERSION\s+(\d+)", src)
    assert m, "RC_VIEW_VERSION moved"
    want = int(m.group(1))
    got = [line.split("=", 1)[1].strip()
           for line in read(VIEW_TPL).splitlines()
           if line.strip().startswith("version")]
    assert got and int(got[0]) == want, got


# --- the templates -----------------------------------------------------


def test_the_enum_the_array_and_the_file_hold_the_same_keys_in_order():
    """All three, in order, because the array is indexed BY the enum: a key
    inserted into one and appended to another would not fail to compile, it
    would silently render every block after it with the wrong markup."""
    keys = guest_template_keys()
    assert keys == enum_names(), (keys, enum_names())
    assert keys == tpl_file_keys(), (
        set(keys) ^ set(tpl_file_keys()) or "same names, different order")


def test_the_template_count_is_asserted():
    assert len(guest_template_keys()) == 41, len(guest_template_keys())


def test_every_kind_has_the_templates_it_needs():
    """A kind whose template is missing renders through the fallback, which
    is legible and wrong. Mapped by hand because the C side chooses its keys
    in code -- which is the point of the format, and the cost of it."""
    needs = {
        "para": ["para"],
        "heading": ["heading"],
        "quote": ["quote"],
        "rule": ["rule"],
        "bullet": ["bullet", "item"],
        "numbered": ["numbered", "item"],
        "code": ["code", "lang", "pre"],
        "table": ["table", "row", "cell", "headcell"],
        "user": ["user"],
        "notice": ["notice"],
        "error": ["error"],
        "thinking": ["thinking"],
        "tool": ["tool"],
        "toolresult": ["toolresult", "toolresult_err", "pre", "clip"],
        "clip": ["clip"],
        "exportheading": ["exporthead", "exporthead_title",
                          "exporthead_when"],
        "permission": ["perm", "perm_plain", "permhead_ask",
                       "permhead_view", "permlead", "permlead_desc",
                       "permwarn", "permprev", "permpara", "permsteps",
                       "permstep", "permtable", "permrow"],
    }
    assert set(needs) == set(B.KINDS), set(needs) ^ set(B.KINDS)
    have = set(guest_template_keys())
    for kind, keys in needs.items():
        missing = [k for k in keys if k not in have]
        assert not missing, f"{kind} needs {missing}"


def test_a_variant_pair_agrees_about_everything_but_its_one_difference():
    """Duplicated wrappers are exactly the drift risk BACKLOG 13 is about,
    and a variant is a second key rather than a conditional precisely so
    that this test can be written."""
    tpl = tpl_values()

    # toolresult / toolresult_err differ only by the extra class.
    assert tpl["toolresult_err"] == tpl["toolresult"].replace(
        'class="toolresult"', 'class="toolresult err"')
    # perm / perm_plain differ only by the id.
    assert tpl["perm_plain"] == tpl["perm"].replace(' id="perm"', "")
    # permlead / permlead_desc share their opening.
    assert tpl["permlead_desc"].startswith("<p><b>{lead}</b>")
    assert tpl["permlead"].startswith("<p><b>{lead}</b>")


def test_the_id_the_card_carries_is_the_one_the_guest_looks_for():
    """rc_html::settle_perm finds the card by this id to collapse it. If
    view.tpl and render.h disagreed, an answered prompt would stay a full
    argument table forever -- which is the state settle_perm was written to
    end."""
    src = read(RENDER_H)
    m = re.search(r'#define RC_ID_PERM\s+"([a-z]+)"', src)
    assert m, "RC_ID_PERM moved"
    assert f'id="{m.group(1)}"' in tpl_values()["perm"]


def test_at_most_one_children_hole_per_template():
    """The format's one rule. Two would need the caller to say which, which
    is a parser, which is what this format exists not to be."""
    for key, value in tpl_values().items():
        assert value.count("{}") <= 1, f"{key} has {value.count('{}')}"


def test_no_value_begins_or_ends_with_a_space():
    """GetPrivateProfileStringA strips whitespace from both ends, so a
    meaningful space there is silently lost. This is why the em dash before
    a permission description lives inside permlead_desc rather than as a
    suffix on the key before it."""
    raw = {}
    for line in read(VIEW_TPL).splitlines():
        line = line.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("["):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        raw[key.strip()] = value
    for key, value in raw.items():
        # What the file holds after the `=`, minus the one conventional
        # space of alignment. If stripping changes anything beyond that,
        # the author meant a space the API will eat.
        assert value == " " + value.strip() or value == value.strip(), (
            f"{key} has leading or trailing space the profile API will "
            f"silently drop: {value!r}")


def test_the_pending_echo_s_class_and_id_are_both_load_bearing():
    """rc_html::settle_pending finds the element BY ID and writes the
    `user` class over the pending one. So view.tpl's `pending` has to carry
    RC_ID_PENDING, and the class it is replaced with has to be the one in
    the `user` template -- otherwise an acknowledged message either never
    settles or settles into an unstyled paragraph.

    Three files, nothing linking them: win/app/htmlview.h names the macros,
    view.tpl writes the markup, and htmlview.cpp does the swap.
    """
    hdr = read(os.path.join(conftest.REPO, "win", "app", "htmlview.h"))
    pend_id = re.search(r'#define RC_ID_PENDING\s+"([a-z]+)"', hdr)
    user_cls = re.search(r'#define RC_CSS_USER\s+"([a-z ]+)"', hdr)
    assert pend_id and user_cls, "the macros moved"

    tpl = tpl_values()
    assert f'id="{pend_id.group(1)}"' in tpl["pending"], tpl["pending"]
    assert user_cls.group(1) in tpl["pending"], (
        "settle_pending writes this class over the pending one, so the "
        "element must already be in that family")
    assert user_cls.group(1) in tpl["user"]


def test_the_five_runtime_classes_stay_in_c():
    """Not every RC_CSS_* moved into view.tpl, and which ones did is the
    distinction worth asserting: a class the code WRITES at runtime --
    put_className on an element that already exists -- is a DOM operation,
    not markup, and has nowhere to live in a template.

    settle_perm writes permdone, settle_pending writes you. Both must stay
    named in C, and both must also appear in the stylesheet, which
    test_guest_css.py checks from the other side.
    """
    hdr = read(os.path.join(conftest.REPO, "win", "app", "htmlview.h"))
    src = read(os.path.join(conftest.REPO, "win", "app", "htmlview.cpp"))
    for macro in ("RC_CSS_USER", "RC_CSS_PERMDONE"):
        assert re.search(rf"#define {macro}\s+\"", hdr), macro
        assert f"to_bstr({macro})" in src, (
            f"{macro} is defined but nothing writes it; if the DOM "
            f"operation went, the macro should have gone with it")
