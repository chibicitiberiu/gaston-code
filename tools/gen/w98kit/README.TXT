GASTON CODE -- WINDOWS 98 CAPABILITY TESTS
=========================================

WHAT THIS IS

  A single console program that asks this machine a set of
  questions that cannot be answered anywhere else. Everything so
  far has been measured on an emulated Win98 running on a modern
  CPU, which is fine for "does this work" and worthless for "how
  fast". This run is the only source of the second answer.


HOW TO RUN IT

  1. Copy this whole folder onto the machine's hard disk. Do not
     run it from a floppy, a CD or a USB stick -- it writes its
     results beside itself, and on removable media that is both
     slower than the disk it is trying to measure and one unplug
     away from losing the log.

     Below, X: is whichever letter this kit arrived on. DIR X:\
     and check before typing anything that starts with it.
  2. Close anything you care about. Explorer and a DOS box are
     enough. See SAFETY below for why.
  3. Open an MS-DOS Prompt, change to this folder, and run:

         TEST.BAT

  4. Follow it. One step near the end asks you to leave a browser
     window open; everything before that is unattended.


WHAT TO SEND BACK

  From this folder, after it finishes:

      PROBE.LOG     every number the run produced
      S24.BMP       one strip of your screen, 24-bit
      S8.BMP        the same strip, 256 colours
      S8R.BMP       the same strip, 256 colours, compressed by GDI

  The three images are the point of the exercise, not decoration.
  S8 and S8R must decode to exactly the same picture. If they do
  not, a design decision that has already been taken is wrong, and
  better to find that now.


SAFETY

  It reads. It writes PROBE.LOG and three BMPs into its own
  folder and nothing else, anywhere.

  Four things are worth knowing:

  * Three steps START NOTEPAD, watch it for a few seconds, and
    close it again. They are testing whether this machine will
    tell one program about another program's windows, which needs
    a second program to exist. Nothing is typed into it.

  * One step loads HOOKDLL.DLL and asks Windows to map it into
    other running programs. That is the whole question that step
    asks -- whether injection is possible here. The DLL counts how
    many processes it reached and does nothing else; it imports
    only USER32 and KERNEL32, so it cannot fail to load inside
    somebody else's program. The hook is removed before the step
    ends.

  * One step walks every VISIBLE window on the desktop and asks
    each one for its name and contents. It asks politely, with a
    timeout, and it skips hidden windows -- an earlier version did
    not skip them and left the Start menu stuck open, which is
    what the skipping is for. Still: an application that has
    stopped responding can make this step wait.

  * One step reads the "foreground lock" setting, sets it to zero
    to see whether that is allowed, and puts your original value
    back. It prints all three numbers so you can check it did.

  It installs nothing, registers nothing, starts no service, and
  leaves nothing running when it exits.


IF SOMETHING GOES WRONG

  The program appends to PROBE.LOG as it goes, so if a step
  crashes the log still holds everything up to that point. Send it
  anyway -- a crash at a known step is itself an answer.

  If nothing runs at all and no PROBE.LOG appears, that is the
  most useful failure of the lot: it means the executable would
  not load, and the machine has told us something about itself
  that no amount of emulation would have.

GREMOTE.EXE -- OPTIONAL, AND ONLY IF THIS MACHINE IS ON A NETWORK

  Everything above runs with nobody watching but you, and hands
  its answers back as files. GREMOTE is the other way round: it
  opens a port so the machine can be driven and measured from
  another computer, which is how every later question gets asked
  without carrying a disk each time.

  It is NOT started by TEST.BAT and nothing here installs it. To
  try it:

      COPY X:\GREMOTE.EXE C:\GASTON
      COPY X:\GUPDATE.EXE C:\GASTON
      COPY X:\GREMOTE.INI C:\GASTON\gremote.ini
      C:\GASTON\GREMOTE.EXE

  GUPDATE.EXE goes beside it and is not optional if you want a
  newer build sent over the network later: GREMOTE refuses to
  replace itself unless the updater is in its own directory. One
  file now, or a walk back to this keyboard later.

  Check gremote.ini before starting it: `secret` is a password in
  plain text on a trusted network, and `allow` is the range of
  addresses that may connect. Both matter -- whoever has the
  secret can run anything on this machine. The copy in this kit
  already carries both if it was built for a particular box; if
  `secret` still says vm-secret, it was not, and it should not be
  started on a network until that is changed.

  To have it start at every boot, before anyone logs on:

      REGEDIT /S X:\GREMOTE.REG

  Deliberately not automatic, and worth undoing with REGEDIT when
  the measurements are finished: it opens a port on this machine.

CORPUS.TXT AND FOCUSPRB.EXE

  CORPUS.TXT is not a document. It is 210K of this project's own
  text and code, and the deflate step times itself against it --
  real prose rather than the repeating block it used to use,
  which compressed 164 to 1 and flattered the result badly. Leave
  it beside PROBE.EXE; the step says so if it is missing.

  FOCUSPRB.EXE asks whether Windows lets a program raise somebody
  else's window. It needs TWO Notepads open with one in front of
  the other, prints a few lines to the screen and writes nothing,
  so run it from a DOS box with > OUT.TXT if you want the answer
  as a file.
