"""gremote's two halves, pinned against each other.

`test_frame_contract.py` does this for the bridge and the client, by parsing
their tables out of the source. It reads a hardcoded list of files and knows
nothing of `win/remote/` or `gremote/`, so until this existed the server's op
table and the Python that drives it could drift apart silently -- and the
failure would be a timeout, which reads as a network problem.

Two directions, both one-way and both deliberate:

* every op the Python client SENDS must be in the guest's table, or the guest
  answers `unknown_op` to something the caller thinks is supported;
* every refusal code the tests ASSERT ON must be one the guest can actually
  produce, or a test is pinning a spelling that no longer exists and will go
  on passing for the wrong reason.

The reverse of each is not asserted. The guest is allowed ops the Python has
no wrapper for yet, and codes nothing tests -- that is what a lane arriving
before its client looks like.

Nothing here runs a guest binary. It reads the tree.
"""

import ast
import pathlib
import re

import conftest

REPO = pathlib.Path(conftest.REPO)
OPTABLE = REPO / "win" / "remote" / "optable.cpp"
REMOTE_CPP = sorted((REPO / "win" / "remote").glob("*.cpp"))
CLIENT_PY = sorted((REPO / "gremote").glob("*.py"))
TESTS_PY = sorted((REPO / "tests" / "python" / "remote").glob("test_*.py"))

#: Answered by the Session itself rather than through the op table, because
#: they are about the connection and not about the machine.
SESSION_OPS = {"AUTH", "PING"}

#: Sent by the transport, not by an op wrapper.
TRANSPORT_TYPES = {"ERR", "OK", "HELLO"}


def guest_ops() -> set[str]:
    """The op names in G_OP_TABLE, which is the guest's whole vocabulary."""
    text = OPTABLE.read_text()
    body = text[text.index("G_OP_TABLE[]"):]
    body = body[:body.index("};")]
    ops = set(re.findall(r'\{\s*"([A-Z]+)"', body))
    assert len(ops) > 10, f"only found {ops}; has the table's shape changed?"
    return ops


def guest_codes() -> set[str]:
    """Every refusal the server can send.

    Scraped from the handlers rather than from a header, for the same reason
    test_guest_agreement.py scrapes syncrpc.cpp: a token that lives as a
    string literal at the point it is sent is one this can see, and one
    hidden behind a mapping in a library is not.

    THE PATTERN LIST IS THE THING THAT HAS TO GROW. The first version of this
    looked only at `.err("...")` and missed six real tokens -- `bad_secret`
    behind refuse(), `denied` behind a `code = ` assignment, `is_file` inside
    a ternary. It failed loudly, which is the right outcome and the reason it
    is written down here: a scraper that quietly under-collects turns this
    test from a contract into a formality.
    """
    codes: set[str] = set()
    interesting = re.compile(r'err\(|errWith\(|refuse\(|code\s*=|return\s+"')
    for path in REMOTE_CPP:
        for line in path.read_text().splitlines():
            if not interesting.search(line):
                continue
            # Token-shaped literals only: a wire code is lower snake case
            # with no spaces, which no log sentence on these lines is.
            codes |= {m for m in re.findall(r'"([a-z][a-z_]{2,})"', line)}
    assert len(codes) > 15, f"only found {sorted(codes)}; shape changed?"
    return codes


def python_ops() -> set[str]:
    """Frame types the client puts on the wire."""
    ops: set[str] = set()
    for path in CLIENT_PY:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or \
                    not isinstance(first.value, str):
                continue
            name = ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            if name in ("request", "stream", "Frame"):
                ops.add(first.value)
    return ops - TRANSPORT_TYPES


def asserted_codes() -> set[str]:
    """Refusal codes the suite pins."""
    codes: set[str] = set()
    for path in TESTS_PY:
        text = path.read_text()
        codes |= set(re.findall(r'\.code\s*==\s*"([a-z_]+)"', text))
        for group in re.findall(r'\.code\s+in\s+\(([^)]*)\)', text):
            codes |= set(re.findall(r'"([a-z_]+)"', group))
        codes |= set(re.findall(r'get\("code"\)\s*==\s*"([a-z_]+)"', text))
    assert codes, "no refusal codes are pinned at all"
    return codes


def test_every_op_the_client_sends_is_one_the_guest_knows():
    """Otherwise the guest answers `unknown_op` to something the caller
    believes in, and the caller finds out at the end of its budget."""
    unknown = python_ops() - guest_ops() - SESSION_OPS
    assert not unknown, (
        f"gremote/ sends {sorted(unknown)}, which win/remote/optable.cpp "
        f"does not list. Add the row, or stop sending it.")


def test_every_refusal_the_tests_pin_is_one_the_guest_can_send():
    """A test asserting a spelling the guest no longer produces goes on
    passing for the wrong reason -- it is pinning the test's own idea of the
    vocabulary, not the machine's."""
    invented = asserted_codes() - guest_codes()
    assert not invented, (
        f"tests/python/remote pins {sorted(invented)}, which nothing in "
        f"win/remote/ sends. Either the code changed or the test is "
        f"asserting a spelling that never existed.")


def test_the_continuation_flag_is_only_on_the_frames_that_continue():
    """DATA and END route PAST the lane's busy check, to the request already
    open on it. Any other op marked that way would skip the one-at-a-time
    rule entirely, which is the lane's whole purpose."""
    text = OPTABLE.read_text()
    body = text[text.index("G_OP_TABLE[]"):]
    body = body[:body.index("};")]
    continuing = {op for op, flag in
                  re.findall(r'\{\s*"([A-Z]+)"\s*,\s*\w+\s*,\s*(true|false)',
                             body)
                  if flag == "true"}
    assert continuing == {"DATA", "END"}, continuing


def test_the_control_lane_holds_only_what_may_run_on_the_reader():
    """The bar is not "is it quick" but "can it block and can it re-enter".
    An op added here runs on the thread that carries the heartbeat, and on
    9x a hung 16-bit app holding Win16Mutex blocks USER and GDI from every
    32-bit thread."""
    text = OPTABLE.read_text()
    body = text[text.index("G_OP_TABLE[]"):]
    body = body[:body.index("};")]
    control = {op for op, lane in
               re.findall(r'\{\s*"([A-Z]+)"\s*,\s*(G_\w+)', body)
               if lane == "G_CONTROL"}
    assert control == {"AUTH", "PING", "CANCEL", "SYSINFO", "PS", "KILL",
                       "REBOOT"}, control
