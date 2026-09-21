"""Every permission mode on the menu has something to say for itself.

`auto` shipped on `MODE_ORDER` for a release with no entry in either
`permissions._MODE_LABELS` or `permissions._MODE_MEANS`, so pressing Rules in
Auto mode rendered `<b>Mode: auto</b> &mdash; ` -- the raw token and an empty
explanation, on the one screen whose whole job is answering "what can run
without asking?".

The existing coverage could not see it: `test_the_rules_view_follows_the_mode`
sets `plan`, asserts "Mode: Plan", and is parameterised over nothing. These
walk `MODE_ORDER` itself, so the next mode added cannot ship without a
description either.

The two tables are deliberately duplicated rather than imported (permissions
is imported BY the server, and an import back would be a cycle) -- which is
exactly why they need a test to hold them together.
"""

import pytest

from bridge import server
from bridge.agent import permissions as perms


@pytest.mark.parametrize("mode", server.MODE_ORDER)
def test_every_mode_has_a_menu_label(mode):
    assert server.MODE_LABEL.get(mode), mode


@pytest.mark.parametrize("mode", server.MODE_ORDER)
def test_every_mode_has_a_rules_view_label(mode):
    assert perms._MODE_LABELS.get(mode), mode


@pytest.mark.parametrize("mode", server.MODE_ORDER)
def test_every_mode_has_a_rules_view_meaning(mode):
    means = perms._MODE_MEANS.get(mode, "")
    assert means.strip(), mode
    # A sentence, not a word. The empty string was the bug; a placeholder
    # would be the same bug wearing a value.
    assert len(means) > 30, mode


@pytest.mark.parametrize("mode", server.MODE_ORDER)
def test_the_two_label_tables_agree(mode):
    assert server.MODE_LABEL[mode] == perms._MODE_LABELS[mode], mode


@pytest.mark.parametrize("mode", server.MODE_ORDER)
def test_the_rules_view_names_the_mode_and_explains_it(mode):
    html = perms.rules_view(mode, [], guest_root="C:\\WORK")
    assert "Mode: " + server.MODE_LABEL[mode] in html
    # The bug rendered "</b> &mdash; </p>" -- the dash with nothing after it.
    assert "&mdash; </p>" not in html
