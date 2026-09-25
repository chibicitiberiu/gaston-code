@ECHO OFF
CLS
ECHO ==============================================================
ECHO   Gaston Code -- round two, the outstanding questions
ECHO ==============================================================
ECHO.
ECHO  Shorter than last time. Everything here is either something
ECHO  that failed, or a comparison that only the real machine can
ECHO  settle.
ECHO.
ECHO  Same rules: reads the machine, writes into this folder only,
ECHO  installs nothing. One step starts Notepad and closes it.
ECHO.
PAUSE
IF EXIST PROBE.LOG DEL PROBE.LOG
IF EXIST AUTOCHK.TXT DEL AUTOCHK.TXT
CLS
ECHO  PART ONE -- no dialog needed.
ECHO.
ECHO  [1/6] the library's own checks  -- THIS CRASHED LAST TIME
AUTOCHK.EXE > AUTOCHK.TXT
IF NOT EXIST PROBE.LOG GOTO FIRST
:FIRST
ECHO  [2/6] where MSAA's time actually goes
PROBE.EXE msaacost
ECHO  [3/6] three traversals of the same desktop
PROBE.EXE msaafast
ECHO  [4/6] WaitForControl / WaitForGone   (starts Notepad)
PROBE.EXE waitfor
CLS
ECHO ==============================================================
ECHO   PART TWO needs a DIALOG on screen.
ECHO ==============================================================
ECHO.
ECHO  The next two compare what Win32 can tell us about a control
ECHO  against what MSAA charges for the same answer. Against a
ECHO  desktop of application windows that comparison is useless --
ECHO  it needs real CONTROLS: checkboxes, radio buttons, tabs,
ECHO  list boxes.
ECHO.
ECHO  Display Properties is about to open, which has all of them.
ECHO  Leave it open and visible, then come back and press a key.
ECHO.
ECHO  Anything with a lot of controls will do just as well if you
ECHO  would rather pick your own -- a Settings dialog, Find Files,
ECHO  a printer setup box.
ECHO.
PAUSE
START CONTROL.EXE DESK.CPL
ECHO.
ECHO  Waiting. Press a key once the dialog is up and visible.
PAUSE
CLS
ECHO  [5/6] Win32-derived roles vs MSAA, on real controls
PROBE.EXE win32rich
ECHO  [6/6] the walk, and what MSAA adds, on real controls
PROBE.EXE uiwalk
CLS
ECHO ==============================================================
ECHO   Done. Send back, from this folder:
ECHO ==============================================================
ECHO.
ECHO      PROBE.LOG      steps 2 to 6
ECHO      AUTOCHK.TXT    step 1, the one that crashed before
ECHO.
ECHO  If AUTOCHK.TXT is empty or missing, it crashed again --
ECHO  please send the crash dialog's details as before, that is
ECHO  exactly as useful as the file would have been.
ECHO.
PAUSE
