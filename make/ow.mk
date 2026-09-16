# Shared Open Watcom rules for Win32 targets that must run on Windows 98.
#
# Include from a Makefile:  include $(TOP)/make/ow.mk
#
# Three things here are non-obvious and were each found the hard way; see
# docs/PLAN.md "Toolchain facts" for the evidence.

WATCOM ?= /usr/bin/watcom
OWBIN   = $(WATCOM)/binl64

WCC   = $(OWBIN)/wcc386
WLINK = $(OWBIN)/wlink
WRC   = $(OWBIN)/wrc

# (1) wlink resolves `system nt_win` out of wlink.lnk, which it finds via PATH
#     -- NOT via $WATCOM, and NOT via its own directory (verified: wlink.lnk
#     lives in binl64 and an absolute-path invocation still failed). Setting
#     WLINK_LNK is stricter than mutating PATH and is immune to a second
#     Watcom install earlier on PATH.
#     WATCOM itself is still required: the system block expands $(%WATCOM) to
#     locate lib386/.
export WATCOM
export WLINK_LNK = $(OWBIN)/wlink.lnk

# (2) -bm selects the multi-threaded CRT. Every binary here runs a reader
#     thread and an RPC worker, and without it errno/strtok/stdio lock state
#     are process-global.
#     NOTE: -bm is necessary but not sufficient. OW tracks per-thread CRT data
#     in a list built by _beginthread/_beginthreadex; a thread started with raw
#     CreateThread never gets an entry and never releases one. Use
#     _beginthreadex (process.h), which also takes the non-NULL thread-id
#     pointer that Win9x requires -- CreateThread(..., lpThreadId=NULL) fails
#     with ERROR_INVALID_PARAMETER on 95/98 while NT accepts it.
#
# (3) WINVER/_WIN32_WINNT are pinned at 0x0400 deliberately. OW already
#     defaults to 0x0400, which is the only reason sizeof(NONCLIENTMETRICS) is
#     Win98-correct (winuser.h gates iPaddedBorderWidth on WINVER >= 0x0600).
#     Pinning stops someone raising it for an unrelated API and silently
#     breaking SPI_GETNONCLIENTMETRICS on the guest.
#     _WIN32_IE is deliberately NOT set: the 0x0400 default keeps TBSTYLE_*
#     (gated >= 0x0300) available. Lowering it would remove TBSTYLE_FLAT,
#     TBSTYLE_LIST and HDPA.
#
#     -5 targets Pentium scheduling and keeps CMOV out of the image (CMOV is
#     686+; the target is a P200MMX).

OW_CFLAGS = -bt=nt -bm -zq -otexan -5 -wx \
            -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 \
            -DWIN32_LEAN_AND_MEAN \
            -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt"

# osversion=4.0 stamps "Windows 4.00" into the PE header, which is the
# Win95/98/NT4 gate. Without it the loader refuses the binary on Win98.
OW_LFLAGS_GUI     = system nt_win option quiet, osversion=4.0
OW_LFLAGS_CONSOLE = system nt     option quiet, osversion=4.0

%.o: %.c
	$(WCC) $(OW_CFLAGS) -fo=$@ $<
