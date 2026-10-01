"""Every sentence the agent reads, loaded from `bridge/prompts/*.toml`.

Prose does not live in code. A module that builds a prompt chooses which
fragment and supplies the substitutions; it does not carry the words. The rule
is the same one `bridge/profiles/` already answers for facts about a machine,
and it exists for the same reason: a sentence in a `.py` file can only be
changed by editing the program, and `if x: prompt += "..."` scatters the text
of a prompt across the branches that assemble it.

What stays in code, deliberately: log and exception messages, which are read
by a developer next to the line that raised them; protocol vocabulary, which
is wire grammar rather than prose; and docstrings, which are the reasoning and
belong beside what they explain.

Substitution is `str.format`, so a template's placeholders are named and a
literal brace would have to be doubled. There are none today and a template
that needs one should think twice.
"""

from __future__ import annotations

import logging
import pathlib
import tomllib

log = logging.getLogger("bridge.prompts")

DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"

_cache: dict | None = None


def _load() -> dict:
    out: dict = {}
    if not DIR.is_dir():
        log.error("no prompt directory at %s; the agent will be told nothing "
                  "about the machine it is acting for", DIR)
        return out
    for path in sorted(DIR.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                out[path.stem] = tomllib.load(fh)
        except Exception as exc:
            # By name, always. A prompt file that silently stops loading is a
            # system prompt that silently loses a paragraph, and the only
            # symptom is an agent that has forgotten something it was told.
            log.error("prompt file %s did not parse (%s); skipping it",
                      path.name, exc)
    if not out:
        log.error("no prompt files loaded from %s", DIR)
    return out


def all() -> dict:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def text(key: str, /, **ctx) -> str:
    """One fragment, substituted. `key` is `<file>.<table>...<name>`.

    Raises rather than returning a placeholder: a missing fragment means the
    agent is about to be told less than it should be, and the quiet version of
    that is a prompt with a hole in it that nothing reports.

    `key` IS POSITIONAL-ONLY, and the `/` is load-bearing. `**ctx` holds the
    placeholders a fragment names, and several of them come straight from a
    guest frame's headers -- so the moment a fragment wanted `{key}`, which is
    the natural name for the key a `[Name]` token failed to be, the call raised
    `TypeError: text() got multiple values for argument 'key'`. At runtime, in
    the refusal path, which is where nobody looks until it fires. The `/` makes
    the parameter unreachable by name instead of renaming the placeholder and
    leaving the collision for the next header.
    """
    node: object = all()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"no prompt fragment {key!r} (missing {part!r})")
        node = node[part]
    if not isinstance(node, str):
        raise KeyError(f"prompt fragment {key!r} is not text")
    return node.format(**ctx) if ctx else node


def reload() -> None:
    """Drop the cache. For tests that write a prompt file and read it back."""
    global _cache
    _cache = None
