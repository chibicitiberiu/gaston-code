# The suites

One folder per layer. `tests/python/meta/test_suite_hygiene.py` enforces the
list, so a new folder is a decision rather than a directory that appeared.

| folder | what it covers |
|---|---|
| `agent/` | the tool surface, the command guard, the system prompt, sessions |
| `files/` | sync: paths, attrs, ignores, the diff table, the wire pass |
| `wire/` | the frame codec, compression, the codepage fold |
| `view/` | markdown to HTML, pagination |
| `server/` | the whole bridge over a real socket, Claude mocked |
| `guest/` | the SHIPPING guest C, run under Wine, diffed against the bridge |
| `meta/` | checks about the source tree rather than its behaviour |
| `tools/` | the scripts in `tools/` |

## Running them

    make test-fast     everything except guest/     ~15s
    make wine-smoke    guest/ only                  ~2.5 min, needs Open Watcom
    make test          both                         ~3 min

`guest/` is 144 tests of ~2790 and roughly 90% of the wall time, so the split
is worth knowing. It is selected by the `guest` **mark**, not by folder or
filename -- `make wine-smoke` is `-m guest`. Two suites were once missing from
a hand-written list in the Makefile and were in no gate at all for months,
which is what the mark and the hygiene check between them are there to stop.

`meta/` is the odd one and deliberately so. Nothing in it exercises the
bridge: it reads `win/*.cpp` for `HDR(...)` names, greps `bridge/` string
literals for an OS name, and parses these suites for their own wiring. They
are tests because a test is what runs, not because they are unit tests.

## Shared code

`conftest.py` -- `REPO` (the repo root, one spelling), `run_guest()` and
`guest_verdicts()` (the Wine invocation, which ten suites used to spell out),
`wine_path()`, and `needs_guest`, which carries both the `guest` mark and the
skipif.

`harness.py` -- one bridge, one guest, one socket. Four suites use it. It was
inside `test_server_integration.py` and the others imported it from there,
which worked only while every suite was in one directory.

Nothing else should be imported across suites. If two need the same thing, it
belongs in one of those two files.
