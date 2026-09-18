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

.PHONY: all guest guest-checks check wine-smoke lint test clean help

help:
	@echo "all         build the Win98 guest binary"
	@echo "check       static PE audit of the GUEST (imports, CMOV, frames, BSS)"
	@echo "wine-smoke  run the shipping C under Wine -- code, never the platform"
	@echo "test        python tests"
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

# The Wine gate, also repointed. It ran the hello-world's message loop for six
# seconds. These two suites run the SHIPPING sync.c and ini.c under Wine and
# diff their answers against the bridge's -- the only automated check that the
# two implementations of the classifier and the INI round-trip agree.
#
# Wine proves the code and never the platform; the real gate is the guest.
wine-smoke: guest-checks
	@$(PY) -m pytest tests/python/test_guest_agreement.py \
	                 tests/python/test_guest_config.py \
	                 tests/python/test_guest_cpp.py -q

# The binaries that run the SHIPPING guest C under Wine. `make test` builds
# them first, because the alternative is what happened during the audit: a
# `make clean` left them missing, 72 cross-language checks skipped in silence,
# and the run reported "2031 passed" as though nothing were wrong. Those
# checks are the only thing proving the guest's classifier and the bridge's
# still agree, and a disagreement there is permanent rather than noisy.
guest-checks:
	@$(MAKE) -C win synccheck inicheck cppcheck

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
lint:
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

clean:
	@$(MAKE) -C win clean
	@rm -rf $(BUILD_DIRS)
	@echo "clean: removed $(BUILD_DIRS)"
