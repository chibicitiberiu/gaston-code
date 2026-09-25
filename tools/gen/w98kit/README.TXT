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
     run it from a floppy or CD -- it writes its results beside
     itself and needs somewhere writable.
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

  Two things are worth knowing:

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
