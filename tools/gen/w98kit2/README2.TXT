GASTON CODE -- ROUND TWO
========================

Six steps. Shorter than the first kit because most of it is
answered; what is left either failed or needs the real machine.

  1  AUTOCHK.EXE -- crashed last time with an invalid page fault.
     The cause was a 64KB buffer on the stack, which steps over
     Win98's guard page; Linux and Wine commit the stack up front
     so nothing here could have found it. Fixed, plus two more the
     same audit turned up once it was pointed at this binary.

  2-3  Where MSAA's time goes, and three different ways of walking
     the same desktop. On this machine MSAA cost 1886ms for 103
     nodes -- 35 times the whole rest of the walk -- and the
     emulator says a batched traversal is 5x cheaper per node.
     Whether that holds on real hardware is the question.

  4  WaitForGone timed out here last time and worked on the
     emulator. Killing a process does not promptly destroy its
     windows on 9x, and this run says whether that is what it was.

  5-6  NEEDS A DIALOG OPEN. These compare what a control's class
     and style bits say about it against what MSAA charges for the
     same answer. Last time the desktop was all application
     windows and only 7 of 18 could be classified, which tells us
     nothing -- the controls this is about live inside dialogs.


HOW TO RUN

  Copy this folder onto the hard disk and run TEST2.BAT from
  there. Not from the USB stick if it is slow, and not from a CD:
  it writes its results beside itself.


SEND BACK

  PROBE.LOG     steps 2 to 6
  AUTOCHK.TXT   step 1

  An empty or missing AUTOCHK.TXT means it crashed again, and the
  crash dialog's details are just as useful as the file.
