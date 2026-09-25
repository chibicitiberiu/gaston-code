GASTON CODE -- SECOND MACHINE
=============================

The identical program that ran on the 200MHz box. Nothing here
is new and nothing needs to be understood; the value is entirely
in running the SAME tests on different hardware.


WHY A SECOND MACHINE

One question cannot be answered from one machine. MSAA -- the
accessibility interface we read control names and roles through
-- costs roughly four times what plain Win32 costs for the same
answer, and we cannot tell whether that is because the 200MHz
CPU is slow or because the interface talks across a process
boundary and that is inherently expensive.

Those have opposite conclusions. If it is the CPU, a faster
machine makes the gap shrink and there is nothing more to do. If
it is the process boundary, the gap stays about the same however
fast the machine is, and the fix is a much larger piece of work.

Steps 1 and 2 measure how fast this machine actually is, so
everything after them can be read as a ratio.


HOW TO RUN

  Copy this folder onto the hard disk and run TEST3.BAT from
  there -- not from the USB stick or a CD, it writes its results
  beside itself.

  Please open Display Properties when it asks, as the other
  machine did. If only one of the two runs has a dialog on
  screen the comparison is worthless.


SEND BACK

  PROBE.LOG
  AUTOCHK.TXT

  An empty or missing AUTOCHK.TXT means it crashed; the crash
  dialog's details are just as useful as the file would be.
