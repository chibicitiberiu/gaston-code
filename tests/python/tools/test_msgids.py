"""tools/check/msgids.py: two private window messages never share a number.

Written after core/app.cpp's quit message and net.h's WM_RC_FRAME were both
WM_APP + 1. The script runs in the build; these pin what it reads, and above
all that a spelling it cannot read FAILS instead of passing unseen.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "msgids", ROOT / "tools" / "check" / "msgids.py")
msgids = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msgids)


def run(tmp_path, files: dict[str, str]) -> int:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return msgids.main(["msgids.py", str(tmp_path)])


def test_the_clash_that_prompted_it_fails(tmp_path):
    assert run(tmp_path, {
        "net/net.h": "#define WM_RC_FRAME      (WM_APP + 1)   /* frame */\n",
        "core/app.cpp": "static const UINT WM_APP_QUIT = WM_APP + 1;\n",
    }) == 1


def test_distinct_ids_pass(tmp_path):
    assert run(tmp_path, {
        "a.h": "#define WM_A (WM_APP + 1)\n#define WM_B (WM_APP + 2)\n",
        "b.cpp": "static const UINT WM_C = WM_APP + 0x100;\n",
    }) == 0


def test_bases_differ(tmp_path):
    """WM_USER + 1 and WM_APP + 1 are different numbers."""
    assert run(tmp_path, {
        "a.h": "#define WM_A (WM_APP + 1)\n#define WM_B (WM_USER + 1)\n",
    }) == 0


def test_hex_and_decimal_are_the_same_number(tmp_path):
    assert run(tmp_path, {
        "a.h": "#define WM_A (WM_APP + 16)\n#define WM_B (WM_APP + 0x10)\n",
    }) == 1


def test_enum_members_are_read(tmp_path):
    assert run(tmp_path, {
        "a.h": "enum {\n    WM_A = WM_APP + 5,\n    WM_B = WM_APP + 5\n};\n",
    }) == 1


def test_an_unreadable_spelling_fails_rather_than_being_skipped(tmp_path):
    assert run(tmp_path, {
        "a.cpp": "UINT msg = WM_APP + base + 3;\n",
    }) == 1


def test_comments_mentioning_an_offset_are_not_definitions(tmp_path):
    assert run(tmp_path, {
        "a.h": "/* net.h owns WM_APP+1 and\n * WM_APP + 2 */\n"
               "#define WM_A (WM_APP + 1) // WM_APP + 1 again\n",
    }) == 0


def test_third_party_is_not_ours(tmp_path):
    assert run(tmp_path, {
        "a.h": "#define WM_A (WM_APP + 1)\n",
        "third_party/x.h": "#define WM_X (WM_APP + 1)\n",
    }) == 0
