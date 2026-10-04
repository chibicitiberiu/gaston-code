"""Every execution mode on the menu has something to say for itself.

`auto` once shipped on `MODE_ORDER` with no label and no meaning, so the
rules view rendered `<b>Mode: auto</b> &mdash; ` -- the raw token and an
empty explanation, on the one screen whose whole job is answering "what can
run without asking?". The tables that bug lived in are gone (the modes are
policy.py's now, their words policy.toml's), and these walk the presets so
the next one added cannot ship without a label, a summary, a value for every
category and a row in the rules view.
"""

import pytest

from bridge import server
from bridge.agent import categories as K
from bridge.agent import permissions as perms
from bridge.agent import policy


@pytest.mark.parametrize("mode_id", server.MODE_ORDER)
def test_every_mode_has_a_label_and_a_summary(mode_id):
    m = policy.preset(mode_id)
    assert m is not None and m.label.strip()
    # A sentence, not a word. The empty string was the bug; a placeholder
    # would be the same bug wearing a value.
    assert len(policy.summary(m)) > 30


@pytest.mark.parametrize("mode_id", server.MODE_ORDER)
def test_every_mode_decides_every_category(mode_id):
    m = policy.preset(mode_id)
    assert set(m.values) == set(K.CATS)
    assert set(m.values.values()) <= set(policy.VALUES)


@pytest.mark.parametrize("mode_id", server.MODE_ORDER)
def test_the_rules_view_names_the_mode_and_every_category(mode_id):
    m = policy.preset(mode_id)
    html = perms.rules_view(m, None, guest_root="C:\\WORK")
    assert m.label in html
    assert "&mdash; </p>" not in html
    for cat in K.CATS:
        assert server_text(f"policy.cat.{cat}") in html


def server_text(key):
    from bridge.agent import prompts
    return prompts.text(key)


def test_every_category_has_words():
    from bridge.agent import prompts
    for cat in K.CATS:
        assert prompts.text(f"policy.cat.{cat}").strip()


def test_legacy_names_all_map_to_a_preset():
    for old, new in policy.LEGACY.items():
        assert policy.preset(new) is not None, old
    assert set(policy.AS_LEGACY) == set(server.MODE_ORDER)
