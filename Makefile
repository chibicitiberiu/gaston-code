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

.PHONY: all spike check wine-smoke lint test clean help

help:
	@echo "spike       build the reference Win32 GUI binary"
	@echo "check       static PE audit (version gate, imports, CMOV, size)"
	@echo "wine-smoke  run under Wine -- code sanity only, NOT a Win98 proof"
	@echo "lint        grep for RichEdit 3.0-only messages Win98 lacks"
	@echo "test        python tests"
	@echo "clean       remove build/ and tmp/ entirely"

all: spike

spike:
	@$(MAKE) -C spike ow

check:
	@$(MAKE) -C spike check

wine-smoke:
	@$(MAKE) -C spike wine-smoke

# RichEdit 2.0 is what ships with Win98. Open Watcom's richedit.h defaults to
# _RICHEDIT_VER 0x0300 and -- crucially -- gates NOTHING on it, so every 3.0-only
# message compiles clean and fails on the guest. #define does not help; only a
# lint does.
# Confirmed 3.0-only against the RichEdit version-delta table. Several
# messages that LOOK modern are in fact 2.0 and are deliberately NOT listed:
# EM_SETUNDOLIMIT, EM_SETPALETTE and EM_AUTOURLDETECT are all 2.0, and
# EM_SETUNDOLIMIT in particular is one we rely on.
RE3_ONLY = EM_SETTEXTEX EM_GETTEXTEX EM_GETTEXTLENGTHEX \
           EM_SETTABSTOPS EM_SETMARGINS
lint:
	@fail=0; \
	for m in $(RE3_ONLY); do \
	  if grep -rn --include='*.c' --include='*.h' "\b$$m\b" win/ 2>/dev/null; then \
	    echo "  ^ $$m is RichEdit 3.0 only; Win98 ships 2.0"; fail=1; \
	  fi; \
	done; \
	if [ $$fail -eq 0 ]; then echo "lint: no RichEdit 3.0-only messages"; fi; \
	exit $$fail

# The venv is where claude-agent-sdk and pytest live; the system python has
# neither, and falling back to it turns "tests pass" into "no module named
# pytest" at the exact moment you want the tests.
PY = $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

test:
	@if [ -d tests/python ] && ls tests/python/*.py >/dev/null 2>&1; then \
	  $(PY) -m pytest tests/python -q; \
	else echo "test: no tests yet"; fi

clean:
	@$(MAKE) -C spike clean
	@$(MAKE) -C win clean
	@rm -rf $(BUILD_DIRS)
	@echo "clean: removed $(BUILD_DIRS)"
