TOP := $(CURDIR)
include $(TOP)/make/ow.mk

# Two generated trees, both gitignored, and nothing generated outside them:
#
#   build/   compiler and linker output -- objects, executables, maps, the
#            per-file .err listings Open Watcom writes unless -fr= redirects
#            them, and the guest's own runtime log and ini beside its exe.
#   tmp/     scratch from the scripts that drive the VM -- screenshots, pids,
#            OCR intermediates.
#
# Before this, all of it lived beside the source: `ls win/` showed fourteen
# .o files, six .exe files, a .map, two stray .err listings and an orphaned
# transcript.o whose source had been deleted. `rm -rf build tmp` is now a
# complete clean with nothing left behind.
BUILD_DIRS = build tmp

# `help` is the first rule in this file, so without this it is also the
# default goal -- `make` printed the help and built nothing.
.DEFAULT_GOAL := all

.PHONY: all guest guest-checks check wine-smoke lint pylint-py test test-fast gates clean help

help:
	@echo "all         build the Win98 guest binary"
	@echo "check       static PE audit of the GUEST (imports, CMOV, frames, BSS)"
	@echo "wine-smoke  run the shipping C under Wine -- code, never the platform"
	@echo "test        python tests, all of them (~3 min)"
	@echo "test-fast   python tests except the Wine suites (~15s) -- the inner loop"
	@echo "lint        ruff + mypy on the Python, and the guest RichEdit grep"
	@echo "gates       run all verification gates together (check, test, lint)"
	@echo "clean       remove build/ and tmp/ entirely"

all: guest

guest:
	@$(MAKE) -C win all

# The audit that matters, against the binary that ships.
#
# This used to be `$(MAKE) -C spike check`, which audited spike/hello98.exe --
# a Phase-0 hello-world kept as a toolchain reference. So the top-level gate
# passed on a program with none of our code in it, and the real audit was only
# ever reachable as `make -C win check`. The spike has been deleted; this is
# what it was standing in front of.
check:
	@$(MAKE) -C win check

# The Wine gate. These suites run the SHIPPING guest C under Wine and diff its
# answers against the bridge's -- the only automated check that the two
# implementations of the classifier, the INI round-trip, the frame codec,
# sync transfer handlers and caps enums still agree.
#
# Boundaries and verification scope (Audit 04.3 & 04.11):
# - Code proof vs platform proof: Wine proves the C algorithmic logic and
#   cross-language data agreements; it does NOT prove FAT32 filesystem
#   semantics, MAX_PATH kernel clamping, or modal dialog behaviour on real
#   Windows 98. Those require real VM testing via tools/vm/vmship.sh and
#   tools/probe/*probe.py.
# - Headless C vs GUI: This automated gate covers headless RPC, networking,
#   data structures, and sync transfer handlers (~40% of the guest code).
#   The Win32 GUI layer (main.cpp, htmlview.cpp, workdlg.cpp, etc., ~60%)
#   has no headless runner and is checked via probes, contract tests
#   (test_frame_contract.py, test_guest_css.py), and manual VM acceptance.
#
# Wine proves the code and never the platform; the real gate is the guest.
#
# Selected by MARK, not by a list of filenames, because the list was wrong: it
# named eight files and there were ten, so test_guest_workcopy.py and
# test_tooldet_agreement.py were in no gate at all -- `make test` ran them
# among two thousand others and nothing else did. A list maintained by hand
# beside a directory that grows is the same silent gap the conftest docstring
# was written about, one level up.
wine-smoke: guest-checks
	@$(PY) -m pytest tests/python -m guest -q

# The binaries that run the SHIPPING guest C under Wine. `make test` builds
# them first, because the alternative is what happened during the audit: a
# `make clean` left them missing, 72 cross-language checks skipped in silence,
# and the run reported "2031 passed" as though nothing were wrong. Those
# checks are the only thing proving the guest's classifier and the bridge's
# still agree, and a disagreement there is permanent rather than noisy.
#
# netcheck and probe joined this list once test_guest_net.py and
# test_guest_probe.py existed to drive them (03.53) -- before that they built
# clean and nothing ran them, which is the same silent gap this list exists
# to close. capscheck joined once test_guest_caps.py existed to diff its
# output against bridge/files/caps.py (03.12).
# workcheck and toolcheck were missing here for the same reason they were
# missing from wine-smoke. They built anyway, because `make -C win all` builds
# everything -- so after a `make clean; make test` their two suites would hit
# guest_exe's loud failure, which is the good outcome of a bad list.
guest-checks:
	@$(MAKE) -C win gaston gremote gupdate synccheck inicheck idcheck inifilecheck appcheck windowcheck wirecheck execcheck uicheck cppcheck rpccheck framecheck netcheck autocheck \
	                probe capscheck workcheck toolcheck fscheck

# RichEdit 2.0 was what Win98 shipped, and this gated on the 3.0-only messages
# that compile clean and fail on the guest. The transcript moved to MSHTML, so
# there is no RichEdit left to lint and this cannot fail -- a green check that
# proves nothing, which is worse than no check. Kept ONLY because the guest
# could regain a RichEdit control (an input box, a dialog) and the trap would
# be live again the same day.
#
# Not in `help`, and not part of any gate. See docs/audit/ for the list of
# checks that should exist instead.
#
# The extension list is load-bearing and was WRONG for half a day. The guest
# became C++ on 2026-09-18 and this grep still said `--include='*.c'` only, so
# the sentence above -- that the trap goes live again the same day a RichEdit
# control comes back -- had quietly stopped being true for every file in the
# tree. A check whose own comment overstates it is worse than no check. Any
# grep-based gate added here must list *.cpp.
RE3_ONLY = EM_SETTEXTEX EM_GETTEXTEX EM_GETTEXTLENGTHEX \
           EM_SETTABSTOPS EM_SETMARGINS
# --- Python: lint + types ---------------------------------------------------
#
# Both, in one target, because the two find different things and a project that
# runs only one of them believes it is checked. ruff finds the dead import and
# the blocking call in an async function; mypy finds the None that is not
# guarded. Neither finds the other's.
#
# Config lives in pyproject.toml so this target and every editor agree.
pylint-py:
	@echo "=== ruff (lint) ==="
	@$(PY) -m ruff check bridge/ gremote/ tools/ tests/
	@echo "=== mypy (types) ==="
	@$(PY) -m mypy

lint: pylint-py
	@fail=0; \
	for m in $(RE3_ONLY); do \
	  if grep -rn --include='*.c' --include='*.cpp' --include='*.h' "\b$$m\b" win/ 2>/dev/null; then \
	    echo "  ^ $$m is RichEdit 3.0 only; Win98 ships 2.0"; fail=1; \
	  fi; \
	done; \
	if [ $$fail -eq 0 ]; then echo "lint: no RichEdit 3.0-only messages"; fi; \
	exit $$fail

# The venv is where claude-agent-sdk and pytest live; the system python has
# neither, and falling back to it turns "tests pass" into "no module named
# pytest" at the exact moment you want the tests.
PY = $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

test: guest-checks
	@if [ -d tests/python ] && ls tests/python/*.py >/dev/null 2>&1; then \
	  $(PY) -m pytest tests/python -q; \
	else echo "test: no tests yet"; fi

# The inner loop. Ten suites run the guest's C under Wine; they are 144 tests
# of 2746 and ~156s of ~172s, so deselecting them turns a three-minute wait
# into fifteen seconds. Not a gate -- `make test` is -- but the difference
# between running the tests after every edit and running them after every
# third one.
# Ensures gaston.exe is present so test_import_gate.py runs in full on clean checkouts
guest-gaston:
	@$(MAKE) -C win gaston

test-fast: guest-gaston
	@$(PY) -m pytest tests/python -m "not guest" -q

# Run all verification gates together: static PE audit (imports, CMOV, frames,
# BSS budget ceiling), the full python test suite (including guest Wine checks),
# and static syntax lints.
gates: check test lint
	@echo "All verification gates passed."

clean:
	@$(MAKE) -C win clean
	@rm -rf $(BUILD_DIRS)
	@echo "clean: removed $(BUILD_DIRS)"
