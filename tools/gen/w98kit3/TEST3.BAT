@ECHO OFF
CLS
ECHO ==============================================================
ECHO   Gaston Code -- the same tests, a second machine
ECHO ==============================================================
ECHO.
ECHO  This is the identical program that ran on the 200MHz box.
ECHO  The point is the COMPARISON, so nothing here is new: same
ECHO  binary, same steps, same order.
ECHO.
ECHO  Steps 1 and 2 measure this machine's speed and memory, so
ECHO  the rest can be read as ratios rather than raw numbers.
ECHO.
ECHO  Reads the machine, writes into this folder only, installs
ECHO  nothing. One step starts Notepad and closes it.
ECHO.
PAUSE
IF EXIST PROBE.LOG DEL PROBE.LOG
IF EXIST AUTOCHK.TXT DEL AUTOCHK.TXT
CLS
ECHO  PART ONE -- no dialog needed.
ECHO.
ECHO  [1/8] platform, CPU and memory
PROBE.EXE platform
ECHO  [2/8] deflate throughput -- how fast this machine is
PROBE.EXE deflate
ECHO  [3/8] the library's own checks
AUTOCHK.EXE > AUTOCHK.TXT
ECHO  [4/8] where MSAA's time actually goes
PROBE.EXE msaacost
ECHO  [5/8] three traversals of the same desktop
PROBE.EXE msaafast
ECHO  [6/8] WaitForControl / WaitForGone   (starts Notepad)
PROBE.EXE waitfor
CLS
ECHO ==============================================================
ECHO   PART TWO needs a DIALOG on screen.
ECHO ==============================================================
ECHO.
ECHO  The last two compare what a control's class and style bits
ECHO  say about it against what MSAA charges for the same answer.
ECHO  Against a desktop of application windows that comparison is
ECHO  useless -- it needs real CONTROLS: checkboxes, radio
ECHO  buttons, tabs, list boxes.
ECHO.
ECHO  Display Properties is about to open, which has all of them.
ECHO  Leave it open and visible, then press a key.
ECHO.
ECHO  The 200MHz machine had it open for these two, so please do
ECHO  the same here or the two runs will not be comparable.
ECHO.
PAUSE
START CONTROL.EXE DESK.CPL
ECHO.
ECHO  Waiting. Press a key once the dialog is up and visible.
PAUSE
CLS
ECHO  [7/8] Win32-derived roles vs MSAA, on real controls
PROBE.EXE win32rich
ECHO  [8/8] the walk, and what MSAA adds, on real controls
PROBE.EXE uiwalk
CLS
ECHO ==============================================================
ECHO   Done. Send back, from this folder:
ECHO ==============================================================
ECHO.
ECHO      PROBE.LOG      steps 1, 2 and 4 to 8
ECHO      AUTOCHK.TXT    step 3
ECHO.
PAUSE
