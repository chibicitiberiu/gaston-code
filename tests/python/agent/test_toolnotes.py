"""The notes the agent is told about the machine's toolchains.

Detection decides WHEN a note is shown; these decide what it SAYS. The second
is the dangerous one: a detection rule that guesses wrong costs one unused
inventory line, and a note that is wrong costs the agent acting confidently
on folklore -- under an instruction not to go and check.

So the invariants here are about provenance and about not hardcoding a path
that detection exists to discover.
"""

import configparser
import pathlib

import conftest
import pytest

from bridge.agent.toolnotes import NOTES
from bridge.agent.toolrules import RULES

INI = pathlib.Path(conftest.REPO) / "win" / "tools_detect.ini"


def test_every_note_says_where_it_was_measured():
    """The day this becomes optional is the day the first unmeasured note
    arrives, and nothing downstream can tell the difference."""
    for name, note in NOTES.items():
        assert note.measured.strip(), name
        assert any(ch.isdigit() for ch in note.measured), (
            f"{name}: `measured` must name a version or a date, got "
            f"{note.measured!r}")


def test_no_note_hardcodes_a_path_that_detection_exists_to_find():
    """A note saying C:\\WATCOM contradicts the inventory line above it the
    moment someone installs to D:\\OW -- two authoritative sources
    disagreeing, and the more detailed one wrong."""
    for name, note in NOTES.items():
        assert "C:\\WATCOM" not in note.body, name
        assert "C:\\BC5" not in note.body, name


def test_a_body_with_a_root_placeholder_substitutes_it():
    out = NOTES["watcom"].render("D:\\OW")
    assert "D:\\OW\\SETVARS.BAT" in out
    assert "{root}" not in out


def test_a_body_without_a_placeholder_is_unharmed():
    """The shell note has no root -- rendering must not mangle it."""
    out = NOTES["command.com"].render("")
    assert "Too many parameters" in out
    assert "{root}" not in out


def test_the_shell_note_carries_the_facts_the_exec_description_should_not():
    """`exec`'s description is unconditional -- every machine gets it,
    including a 4DOS box where `&&` works and the NT 4 target CLAUDE.md says
    the tool surface must survive. Shell syntax belongs to the detected
    shell, which is what makes this design portable rather than a Win98
    special case."""
    body = NOTES["command.com"].body
    for fact in ("&&", "2>&1", "Syntax error"):
        assert fact in body, fact


def test_render_is_stripped_so_blocks_compose():
    for name, note in NOTES.items():
        out = note.render("X:\\T")
        assert not out.startswith("\n"), name
        assert not out.endswith("\n"), name


def test_a_note_is_immutable():
    """Shared, rendered per session. Nothing should be able to edit one in
    place and have it leak into the next conversation."""
    with pytest.raises(Exception):
        NOTES["watcom"].title = "something else"


# --- a note nothing can detect is a note nobody will ever read --------------

def test_every_note_is_keyed_on_something_the_bridge_can_detect():
    """The key is how a note gets selected. One that matches no rule is text
    that can never be shown, which is the quiet way for a measured fact to
    rot: nobody notices it is unreachable because nothing errors."""
    unreachable = sorted(set(NOTES) - set(RULES))
    assert not unreachable, f"notes with no detection rule: {unreachable}"


def test_every_note_is_keyed_on_something_the_guest_looks_for():
    """And the rule has to correspond to a section the SHIPPED INI asks
    about, or the guest never reports that name and the rule never fires."""
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cp.read(INI, encoding="latin-1")
    missing = sorted(set(NOTES) - set(cp.sections()))
    assert not missing, f"notes nothing is looked for: {missing}"


def test_cscript_note_renders_root_and_version():
    note = NOTES["cscript"]
    out = note.render(r"C:\WINDOWS\COMMAND\CSCRIPT.EXE", version="5.0")
    assert r"C:\WINDOWS\COMMAND\CSCRIPT.EXE" in out
    assert "Windows Script Host 5.0" in out
    assert "{root}" not in out
    assert "{version}" not in out


def test_cscript_note_warns_against_wscript():
    body = NOTES["cscript"].body
    assert "wscript" in body.lower()
    assert "modal dialogs" in body or "dialog" in body


def test_cscript_note_includes_batch_and_timeout_flags():
    body = NOTES["cscript"].body
    assert "//nologo" in body
    assert "//B" in body
    assert "//T:" in body


def test_the_jdk_note_names_the_1_1_absences_an_agent_reaches_for():
    """The version number alone does not help: a model writes `ArrayList`
    and `String.format` by reflex, javac 1.1 says `class ArrayList not
    found` or nothing useful, and the agent has been told not to go
    exploring. So the absences are named rather than implied."""
    body = NOTES["jdk"].body
    for absent in ("ArrayList", "HashMap", "Iterator", "generics",
                   "StringBuilder", "String.format", "regex", "Swing"):
        assert absent in body, absent
    # And what to use instead, which is the half that makes it actionable.
    for instead in ("Vector", "Hashtable", "Enumeration", "StringBuffer",
                    "AWT"):
        assert instead in body, instead


def test_the_jdk_note_separates_the_two_kinds_of_failure():
    """Both measured on the machine, 2026-10-01, and they differ -- which is
    worth more than blanket scepticism would have been.

    `javac` with no arguments prints its usage and exits 0; a real compile
    error sets errorlevel and prints `Bad.java:3: ';' expected.` with a
    caret and a count. So the agent is told the code IS trustworthy for the
    case it cares about, and told not to read a 0 from a rejected command
    line as a build.

    IT HAS TO NAME `if errorlevel 1`, not "the exit code": COMMAND.COM
    returns 0 for a failing child whatever happened, so the number that
    reaches this bridge through the shell cannot carry the distinction and
    the test pins the construct that can.
    """
    body = NOTES["jdk"].body
    assert "`if errorlevel 1`" in body
    assert "exits 0" in body


def test_the_jdk_note_renders_root_and_version():
    out = NOTES["jdk"].render(r"C:\JDK1.1.8", version="1.1.8")
    assert "JDK 1.1.8" in out
    assert r"C:\JDK1.1.8\bin\javac" in out
    assert "{root}" not in out and "{version}" not in out


def test_the_jdk_note_invokes_the_tools_by_full_path():
    r"""Measured 2026-10-01 on the real machine: PATH is
    `C:\WINDOWS;C:\WINDOWS\COMMAND` and nothing else, so the JDK is not on
    it and no variable points at it. A bare `javac hello.java` there is
    `Bad command or file name`, which names the program and not the
    environment -- and three sessions have already read that as "the
    compiler is not installed" and gone hunting the disk.
    """
    body = NOTES["jdk"].body
    assert r"{root}\bin\javac" in body
    assert r"{root}\bin\java Hello" in body
    assert "\n        javac " not in body, "a bare javac will not run there"


# --- which version a note is about, and what "closest" means ----------------
#
# THE PROBLEM, which was in the tree as a refusal before it was in the tree
# as a feature. `jdk.toml` lists only 1.1-era install paths and says why: its
# body is a list of what Java 1.1 lacks, and that list is "confidently wrong
# in front of a 1.2+ install where half of it came back (collections,
# Swing)". So the only defence against describing the wrong version was to
# REFUSE TO DETECT IT, and a JDK 1.3 on the machine was reported as no JDK.
# `msvc.toml` refuses from the other end, declining a `default` because its
# 16-bit markers would be stamped "6.0".
#
# Now a body can declare which version it was measured against and a profile
# can carry more than one, and the resolution says so when it is not the one
# asked for. BEING SILENTLY APPROXIMATE IS THE FAILURE, not being
# approximate: an agent cannot tell an exact note from a near one unless the
# note tells it.

from bridge.agent import toolnotes
from bridge.agent.toolnotes import Body, Note, covers, parse_version


def test_a_version_is_the_leading_run_of_dotted_integers():
    assert parse_version("1.1.3") == (1, 1, 3)
    assert parse_version("1.1") == (1, 1)
    assert parse_version("6.0") == (6, 0)
    assert parse_version("4.2") == (4, 2)
    assert parse_version("6") == (6,)
    assert parse_version("v1.2") == (1, 2)
    # A letter suffix is a release, not a component: 6.0a is 6.0's revision
    # and sorting it anywhere else would be inventing a number.
    assert parse_version("6.0a") == (6, 0)
    assert parse_version(" 1.1 ") == (1, 1)


def test_a_version_that_is_not_a_number_is_not_guessed_at():
    """`VC98` is a directory name, `latest` is a wish, and the empty string
    is a machine that reported nothing. Mapping any of them onto a release
    would be the silent approximation this whole mechanism exists to stop."""
    for junk in ("", None, "VC98", "latest", "unknown", "a", "JDK1.1.3"):
        assert parse_version(junk) is None, junk


def test_the_jdks_own_self_report_does_not_parse_into_a_date():
    r"""Measured on the real machine, `java -version` says
    `JDK1.1.3/jws:97.08.28`. Parsed component by component that is
    `(1, 1, 3, 97, 8, 28)` -- a version from the future by two orders of
    magnitude, which would make every comparison here nonsense. The regex
    anchors at the start and stops at the first non-digit, so this string
    does not parse at all, which is the right answer: it is prose."""
    assert parse_version("JDK1.1.3/jws:97.08.28") is None
    # And the number on its own, which is what a caller would actually pass.
    assert parse_version("1.1.3/jws:97.08.28") == (1, 1, 3)


def test_a_shorter_version_covers_the_line_it_names():
    """A note that says it was measured on `1.1` is making a claim about the
    1.1 line -- which is what `jdk.toml`'s title already asserts in prose --
    so it answers for 1.1.3 and 1.1.8 with no qualification."""
    assert covers((1, 1), (1, 1, 3))
    assert covers((1, 1), (1, 1, 8))
    assert covers((1,), (1, 1, 8))


def test_the_coverage_is_one_directional():
    """`1.1.8` does NOT cover `1.1`. A body measured on one point release is
    not a statement about the line it came from, and treating it as one is
    how a 1.1.8-only fact gets told to a 1.1.3 machine without a word."""
    assert not covers((1, 1, 8), (1, 1))
    assert not covers((1, 1, 8), (1, 1, 3))


def test_trailing_zeros_are_the_same_number():
    """`6` and `6.0` are one release written two ways. A model retyping it
    from a prompt may write either and neither is a mistake."""
    assert covers((6,), (6, 0))
    assert covers((6, 0), (6,))
    assert covers((6, 0, 0), (6,))


def _note(*versions, base="", base_version=""):
    """A note documenting exactly `versions`, each body naming its own."""
    variants = tuple(Body(version=v, title=f"Fake {v}",
                          measured=f"a {v} machine, 2026-10-01",
                          body=f"Fake {{version}} at {{root}}: the {v} body.")
                     for v in versions)
    return Note(title="Fake", measured="a machine, 2026-10-01",
                body=base, for_version=base_version, variants=variants)


def test_an_exact_version_is_not_disclosed_as_anything():
    r = _note("1.1", "1.2").resolve("1.2")
    assert r.exact and r.direction == ""
    assert "the 1.2 body" in r.body.body


def test_a_point_release_resolves_to_the_line_that_documents_it():
    """1.1.8 asked, 1.1 measured: exact, because the note claimed the line.
    This is the case the shipped JDK profile will be in the day it declares
    a version, and it must not carry a warning -- a disclosure on a note
    that does fit teaches the agent to ignore the ones that do not."""
    r = _note("1.1", "1.2").resolve("1.1.8")
    assert r.exact and r.direction == ""
    assert "the 1.1 body" in r.body.body


def test_a_version_newer_than_anything_measured_is_the_dangerous_direction():
    """THE CASE THE JDK PROFILE REFUSED TO DETECT. 1.3 asked, 1.1 and 1.2
    measured: the newest measured one answers, and it is flagged `ahead` --
    the machine is ahead of the note, so the note's absences are absences of
    the past and `no ArrayList` is simply false there."""
    r = _note("1.1", "1.2").resolve("1.3")
    assert not r.exact
    assert r.direction == "ahead"
    assert r.body.version == "1.2"
    assert r.asked == "1.3"


def test_a_version_older_than_anything_measured_is_flagged_the_other_way():
    """1.0 asked, 1.1 and 1.2 measured. The note documents a LATER release,
    so what it says to use may not be there yet -- which fails at the
    compiler rather than silently, but still costs a round trip nobody
    needed to spend."""
    r = _note("1.1", "1.2").resolve("1.0")
    assert not r.exact
    assert r.direction == "behind"
    assert r.body.version == "1.1"


def test_the_lower_body_answers_even_when_a_higher_one_is_numerically_nearer():
    """1.9 asked, 1.1 and 2.0 measured. Arithmetic would say 2.0 -- 0.1 away
    against 0.8 -- and the rule says 1.1, because it is the greatest version
    at or below the one in play. 2.0 is a release line this machine has not
    reached, and a note from it would name things that are not there."""
    r = _note("1.1", "2.0").resolve("1.9")
    assert r.body.version == "1.1"
    assert r.direction == "ahead"


def test_a_minor_gap_is_not_scored_across_a_major_boundary():
    r"""THE BUG THE FIRST IMPLEMENTATION HAD, pinned so it cannot come back.

    2.0 asked, 1.1.3 and 1.2.0 measured. Scoring the component-wise absolute
    difference major-first compares minor gaps of 1 and 2 and picks 1.1.3 --
    the FURTHER of the two from 2.0 by any reading, because minor numbering
    restarts at a major boundary and the subtraction does not know that.
    Ordinary version ordering has no such hole."""
    r = _note("1.1.3", "1.2.0").resolve("2.0")
    assert r.body.version == "1.2.0"
    assert r.direction == "ahead"


def test_the_older_body_wins_when_one_sits_either_side():
    """1.1 asked, 1.0 and 1.2 measured -- one minor either side. The older
    one wins: it describes a SUBSET of what is there, so its wrongness costs
    the agent opportunity, where the newer one's costs it a failed build.
    Both are disclosed, which is why this is a preference and not a
    guarantee."""
    r = _note("1.0", "1.2").resolve("1.1")
    assert r.body.version == "1.0"
    assert r.direction == "ahead"


def test_the_patch_level_decides_only_inside_one_line():
    """1.1.4 asked. 1.2.9 is a different line and loses to 1.1.0 whatever
    the patch numbers are."""
    r = _note("1.1.0", "1.2.9").resolve("1.1.4")
    assert r.body.version == "1.1.0"


def test_a_note_that_claims_no_version_claims_no_mismatch_either():
    """EXACTLY TODAY'S BEHAVIOUR, which is the contract for the new keys
    being optional. No `for_version`, no variants: any version asked gets
    the one body with nothing said about fit, because there is nothing to
    compare it against. Inventing a warning here would put a caveat on
    every note in the tree for a key nobody has filled in yet."""
    n = _note(base="Only body, {version} at {root}.")
    for asked in ("", "1.1", "6.0", "99"):
        r = n.resolve(asked)
        assert r.exact, asked
        assert r.direction == ""
        assert r.understood


def test_a_version_argument_that_cannot_be_placed_is_dropped_and_said_so():
    """Not an error and not a guess. The note that would have come back
    without the argument comes back, and `understood` is how the caller
    knows to say the argument went nowhere."""
    r = _note("1.1", "1.2", base="Base body.").resolve("latest")
    assert r.understood is False
    assert r.exact
    assert r.body.body == "Base body."


def test_no_version_in_play_is_the_base_body():
    r = _note("1.1", "1.2", base="Base body.").resolve("")
    assert r.exact and r.understood and r.asked == ""
    assert r.body.body == "Base body."


def test_a_profile_that_is_all_variants_still_answers_without_a_version():
    """An empty base would otherwise render an empty note -- a tool call
    that returns a header and nothing. The newest measured body stands in."""
    r = _note("1.1", "1.2").resolve("")
    assert r.body.version == "1.2"


def test_the_versions_a_note_documents_are_listed_lowest_first():
    """Printed back to the agent when its version argument made no sense, so
    the order has to be the one a human would write."""
    assert _note("1.2", "1.1", "1.10").versions == ("1.1", "1.2", "1.10")


def test_the_base_body_is_a_candidate_when_it_declares_a_version():
    n = Note(title="Fake", measured="a 1.1 machine, 2026-10-01",
             body="Base, {version}.", for_version="1.1",
             variants=(Body("1.2", "Fake 1.2", "a 1.2 machine, 2026-10-01",
                            "Variant, {version}."),))
    assert n.versions == ("1.1", "1.2")
    assert n.resolve("1.1").body.body == "Base, {version}."
    assert n.resolve("1.2").body.body == "Variant, {version}."


# --- the profile keys, as TOML, because that is what somebody will write ----

def _reload_with(tmp_path, monkeypatch, toml_text):
    """Load one synthetic profile and rebuild NOTES from it."""
    (tmp_path / "faketool.toml").write_text(toml_text, encoding="utf-8")
    from bridge.agent import profiles as P
    monkeypatch.setattr(P, "DIR", tmp_path)
    monkeypatch.setattr(P, "ALL", P._load())
    return toolnotes._build()


_DETECT = '[detect]\nkind = "toolchain"\n'


def test_the_profile_keys_are_for_version_and_note_variant(tmp_path, monkeypatch):
    """The whole of the new surface, written the way a profile author will
    write it. Two keys: `for_version` under `[note]`, and `[[note.variant]]`
    repeated."""
    built = _reload_with(tmp_path, monkeypatch, _DETECT + '''
[note]
title = "Fake 1.1"
measured = "a 1.1 machine, 2026-10-01"
for_version = "1.1"
body = "Fake {version}: the 1.1 facts."

[[note.variant]]
version = "1.2"
title = "Fake 1.2"
measured = "a 1.2 machine, 2026-10-01"
body = "Fake {version}: the 1.2 facts."
''')
    n = built["faketool"]
    assert n.versions == ("1.1", "1.2")
    assert n.resolve("1.2").body.title == "Fake 1.2"
    assert n.resolve("1.1.1").exact
    assert n.resolve("1.4").direction == "ahead"


def test_a_variant_with_no_provenance_is_dropped_not_given_the_bases(
        tmp_path, monkeypatch):
    """DETECTION MAY GUESS; NOTES MAY NOT. Letting a variant inherit the
    base's `measured` would have it claim a machine it was never seen on,
    which is laundering exactly the field that exists to stop that. One
    version's knowledge is the right thing to lose."""
    built = _reload_with(tmp_path, monkeypatch, _DETECT + '''
[note]
title = "Fake"
measured = "a 1.1 machine, 2026-10-01"
for_version = "1.1"
body = "The 1.1 facts."

[[note.variant]]
version = "1.2"
body = "The 1.2 facts, measured nowhere."
''')
    assert built["faketool"].versions == ("1.1",)


def test_a_variant_whose_version_is_not_a_version_is_dropped(
        tmp_path, monkeypatch):
    """Nothing could ever resolve to it, so it is unreachable text --
    the quiet way a measured fact rots."""
    built = _reload_with(tmp_path, monkeypatch, _DETECT + '''
[note]
title = "Fake"
measured = "a machine, 2026-10-01"
body = "Base."

[[note.variant]]
version = "VC98"
measured = "a machine, 2026-10-01"
body = "Unreachable."
''')
    assert built["faketool"].variants == ()


def test_a_bad_variant_does_not_cost_the_rest_of_the_profile(
        tmp_path, monkeypatch):
    """Same posture `profiles` takes for a file that will not parse: raising
    during the handshake costs the user their machine, and skipping costs
    one version's knowledge."""
    built = _reload_with(tmp_path, monkeypatch, _DETECT + '''
[note]
title = "Fake"
measured = "a machine, 2026-10-01"
body = "Base."

[[note.variant]]
version = "1.2"
measured = "a 1.2 machine, 2026-10-01"

[[note.variant]]
version = "1.3"
measured = "a 1.3 machine, 2026-10-01"
body = "The 1.3 facts."
''')
    assert built["faketool"].versions == ("1.3",)


def test_every_variant_in_the_tree_says_where_it_was_measured():
    """Vacuous today -- no shipped profile has a variant yet -- and that is
    the point of writing it now rather than when the first one lands."""
    for name, note in NOTES.items():
        for v in note.variants:
            assert v.measured.strip(), f"{name} {v.version}"
            assert any(ch.isdigit() for ch in v.measured), f"{name} {v.version}"


def test_the_shipped_profiles_declare_the_versions_they_were_measured_on():
    """Succeeds test_no_shipped_profile_declares_a_version_yet, which held the
    optionality contract while the Python side waited for a profile to use it.
    jdk.toml and msvc.toml declared `for_version` on 2026-10-01.

    That is what turns jdk.toml's own comment -- that this note is confidently
    wrong in front of a 1.2+ install -- from a reason to REFUSE TO DETECT such
    an install into a reason to qualify the answer.

    Variants stay empty on purpose: nobody has sat in front of a second
    release of anything long enough to measure one, and a variant without its
    own `measured` is dropped rather than handed the base's, because
    inheriting provenance is the one thing this module exists to prevent."""
    claiming = {n: note.for_version for n, note in NOTES.items()
                if note.for_version}
    assert claiming == {"jdk": "1.1", "msvc": "6.0"}, claiming
    with_variants = sorted(n for n, note in NOTES.items() if note.variants)
    assert with_variants == [], with_variants


# --- the three tiers, explicit ----------------------------------------------
#
# THE ORDER IS THE USER'S, 2026-10-01: "same version, same minor version,
# closest (lower first)". It was two outcomes before -- exact, or nearest --
# and that collapsed two different events into one. A body measured on 1.1.3
# answering a 1.1.8 machine is a patch release apart; a 1.1 note answering a
# 1.3 machine is the case `jdk.toml` refused to detect 1.2+ at all because
# of. Giving the first the second's shouted banner is how an agent learns to
# skim the banner.
#
# TIER 2 CHANGES ANSWERS, which is why it is a tier and not an emergent
# property of tier 3. "Lower first" on its own hands a 1.1.2 machine a 1.0.9
# note when a 1.1.5 note was sitting there, because 1.0.9 is below and
# therefore wins. Same major.minor comes first, and the tests below assert
# the TIER as well as the body -- the tier is what decides which warning the
# agent reads, and a note picked right with the wrong warning attached is
# still the failure this exists to prevent.

from bridge.agent.toolnotes import TIER_EXACT, TIER_NEAREST, TIER_PATCH, line


def test_a_line_is_the_major_and_the_minor():
    assert line((1, 1, 8)) == (1, 1)
    assert line((1, 1)) == (1, 1)
    # A bare major is that major's .0 line, so `6` and `6.0` share one.
    assert line((6,)) == (6, 0)
    assert line((6, 0)) == (6, 0)
    assert line((1, 2, 0)) == (1, 2)


def test_tier_one_is_the_same_version_however_it_is_written():
    assert _note("1.1", "1.2").resolve("1.2").tier == TIER_EXACT
    assert _note("6").resolve("6.0").tier == TIER_EXACT
    assert _note("6.0").resolve("6").tier == TIER_EXACT


def test_the_prefix_rule_stays_in_tier_one_and_is_not_softened():
    """A profile writing `for_version = "1.1"` is declaring the 1.1 LINE,
    which is what jdk.toml's title already says in prose. Answering a machine
    on 1.1.3 with it is exact, and qualifying it would put a caveat on the
    one shipped profile that uses the key -- the whole point of which was to
    stop refusing to detect 1.2+."""
    n = _note("1.1", "1.2")
    for asked in ("1.1", "1.1.0", "1.1.3", "1.1.8", "1.1.8.1"):
        r = n.resolve(asked)
        assert r.tier == TIER_EXACT, asked
        assert r.direction == "", asked
        assert r.body.version == "1.1", asked


def test_tier_two_is_a_patch_release_apart():
    """Both numbers name a patch level and they differ."""
    r = _note("1.1.3", "1.2.0").resolve("1.1.8")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.3"
    assert r.direction == "ahead"


def test_asking_about_a_line_a_body_is_more_specific_than_is_tier_two():
    """The other half of `covers` being one-directional. `1.1` covers
    `1.1.3`; `1.1.3` does NOT cover `1.1`, because a body measured on one
    point release is not a statement about the line it came from. It is the
    nearest thing there is, so it answers -- quietly qualified."""
    r = _note("1.1.3").resolve("1.1")
    assert r.tier == TIER_PATCH
    assert r.direction == "behind"


def test_a_different_minor_is_tier_three_even_when_it_is_adjacent():
    """1.1 and 1.2 are one minor apart and a whole collections framework
    apart. Adjacency on paper is not closeness in a toolchain, which is the
    entire reason tier 2 stops at the minor."""
    r = _note("1.1").resolve("1.2")
    assert r.tier == TIER_NEAREST
    assert r.direction == "ahead"


def test_the_same_line_beats_a_lower_body_from_another_line():
    """THE CASE THAT MAKES TIER 2 A TIER. 1.1.2 asked, with 1.0.9 and 1.1.5
    measured.

    Tier 3's rule on its own picks 1.0.9: it is at or below 1.1.2, so it
    wins, and the agent is handed a note from the PREVIOUS minor line while
    a note from its own line sat right there. Tier 2 restricts to the 1.1
    line first and picks 1.1.5 -- slightly ahead of the machine, and labelled
    as a patch release rather than a generation.

    Delete tier 2 and this test fails on both the body and the tier, which is
    the whole reason it is written as two assertions."""
    r = _note("1.0.9", "1.1.5").resolve("1.1.2")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.5"
    assert r.direction == "behind"


def test_an_absurd_patch_number_does_not_pull_the_answer_across_a_line():
    """Patch 50 is not a release any 1998 toolchain shipped; the numbers are
    chosen to separate the rules rather than to be realistic. Under a plain
    sum of differences 1.2.49 scores 1 + 1 = 2 against 1.1.0's 50, so
    arithmetic picks the other LINE. The line decides first, so it does
    not."""
    r = _note("1.1.0", "1.2.49").resolve("1.1.50")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.0"


def test_tier_two_takes_the_highest_patch_at_or_below_the_one_asked():
    """Same rule as tier 3, run on one line's worth of candidates: the
    greatest at or below. 1.1.7 answering 1.1.8 is a better note than 1.1.0
    answering it, and both are below, so the higher one wins."""
    r = _note("1.1.0", "1.1.7").resolve("1.1.8")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.7"


def test_inside_the_line_the_lower_patch_still_wins_over_a_higher_one():
    """1.1.5 asked with 1.1.3 and 1.1.7 measured -- two either side, and the
    gap is the same size. Same reason it goes down in tier 3: an older body
    describes a subset of what is there, where a newer one names things that
    are not."""
    r = _note("1.1.3", "1.1.7").resolve("1.1.5")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.3"


def test_tier_three_is_the_greatest_version_at_or_below():
    """And when nothing is below, the lowest above. The argument for
    preferring below is why tier 2 exists at all: what breaks a build is
    crossing a release line."""
    r = _note("1.1", "2.0").resolve("1.9")
    assert r.tier == TIER_NEAREST
    assert r.body.version == "1.1"
    up = _note("4.0", "6.0").resolve("3.1")
    assert up.tier == TIER_NEAREST
    assert up.body.version == "4.0"
    assert up.direction == "behind"


def test_the_tiers_are_tried_in_order_not_scored_together():
    """1.1.9 asked, 1.1.0 and 1.2.0 measured. 1.2.0 is one patch away if you
    count that way and 1.1.0 is nine, and 1.1.0 still answers -- the line
    decides before any of that is looked at."""
    r = _note("1.1.0", "1.2.0").resolve("1.1.9")
    assert r.tier == TIER_PATCH
    assert r.body.version == "1.1.0"


def test_exact_still_means_tier_one_for_every_caller_that_only_asks_that():
    n = _note("1.1.3", "1.2.0")
    assert n.resolve("1.1.3").exact
    assert not n.resolve("1.1.8").exact
    assert not n.resolve("2.0").exact
    assert n.resolve("").exact
    assert n.resolve("latest").exact


def test_the_shipped_profiles_land_in_the_tiers_they_should():
    """Against the real `for_version` values committed on 2026-10-01: jdk
    "1.1", msvc "6.0". The 1.1 line is claimed, so every 1.1.x is tier 1 and
    1.2 upwards is tier 3 -- there is no patch-level body to land in tier 2
    yet, and that is a fact about the tree rather than about the rule."""
    jdk = NOTES["jdk"]
    for asked in ("1.1", "1.1.3", "1.1.8"):
        assert jdk.resolve(asked).tier == TIER_EXACT, asked
    for asked in ("1.2", "1.3", "1.4.2"):
        r = jdk.resolve(asked)
        assert r.tier == TIER_NEAREST, asked
        assert r.direction == "ahead", asked

    msvc = NOTES["msvc"]
    for asked in ("6", "6.0", "6.0a", "6.0.1"):
        assert msvc.resolve(asked).tier == TIER_EXACT, asked
    for asked, direction in (("4.2", "behind"), ("5.0", "behind"),
                             ("7.0", "ahead")):
        r = msvc.resolve(asked)
        assert r.tier == TIER_NEAREST, asked
        assert r.direction == direction, asked
