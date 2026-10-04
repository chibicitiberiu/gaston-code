"""Every detected toolchain says which of its programs are trusted.

Trust is a CLIENT fact (CLAUDE.md): the classifier lets a trusted program
run in the work folder without a second look, and which programs those are
is declared beside the tool in win/tools_detect.ini (`trustN=`), sent in
PREFS only for a tool that is found and enabled. A toolchain with none would
put every build in the riskiest tier, which is the nagging the execution
modes exist to end.

Interpreters and DEBUG are left out ON PURPOSE: they run whatever they are
given (the shell profile's `unbounded`), so one trust entry would be a
standing licence for anything.
"""
import configparser
import pathlib

import conftest

from bridge.agent import profiles

INI = pathlib.Path(conftest.REPO) / "win" / "tools_detect.ini"
UNTRUSTED = {"qbasic", "cscript", "debug"}


def _sections():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read_string(INI.read_text(encoding="latin-1"))
    return cp


def test_every_toolchain_names_its_trusted_programs():
    cp = _sections()
    for name in cp.sections():
        kind = (profiles.detect(name) or {}).get("kind", "")
        if kind != "toolchain" or name in UNTRUSTED:
            continue
        trusted = [cp[name][k] for k in cp[name] if k.startswith("trust")]
        assert trusted, f"[{name}] is a toolchain with no trustN="


def test_no_interpreter_is_trusted():
    cp = _sections()
    for name in UNTRUSTED & set(cp.sections()):
        assert not [k for k in cp[name] if k.startswith("trust")], name


def test_trust_keys_are_numbered_within_the_cap():
    cp = _sections()
    for name in cp.sections():
        for k in cp[name]:
            if k.startswith("trust"):
                assert 1 <= int(k[5:]) <= 8, (name, k)
