@ECHO OFF
CLS
ECHO ==============================================================
ECHO   Gaston Code -- Windows 98 capability tests
ECHO ==============================================================
ECHO.
ECHO  This READS the machine and writes its results into this
ECHO  folder. It installs nothing, starts no service, changes no
ECHO  setting permanently, and writes no file outside this folder.
ECHO.
ECHO  Close anything you care about first. One step walks every
ECHO  VISIBLE window on the desktop and asks it for its name; an
ECHO  application that has stopped responding can make that step
ECHO  wait. Explorer and this DOS box are enough.
ECHO.
ECHO  On a Pentium-class machine expect several minutes. Some
ECHO  steps sit still for a while. That is the test, not a hang.
ECHO.
PAUSE
IF EXIST PROBE.LOG DEL PROBE.LOG
CLS
ECHO  PART ONE -- nothing needs to be on screen for these.
ECHO.
ECHO  [1/13] platform, CPU and memory
PROBE.EXE platform
ECHO  [2/13] threads
PROBE.EXE threads
ECHO  [3/13] 16-bit EXEC capture
PROBE.EXE exec
ECHO  [4/13] deflate throughput  -- THE number we cannot fake
PROBE.EXE deflate
ECHO  [5/13] hooks and change notification
PROBE.EXE hooks
CLS
ECHO ==============================================================
ECHO   PART TWO needs a busy screen.
ECHO ==============================================================
ECHO.
ECHO  The rest measure capturing and reading the screen, and they
ECHO  are meaningless against an empty desktop -- one of them will
ECHO  say so and refuse rather than print a flattering zero.
ECHO.
ECHO  Internet Explorer is about to open UITEST.HTM. Let it finish
ECHO  loading and LEAVE IT OPEN, somewhere it is not completely
ECHO  hidden behind this window. Then come back and press a key.
ECHO.
ECHO  If IE does not open by itself, open UITEST.HTM from this
ECHO  folder by hand.
ECHO.
PAUSE
START UITEST.HTM
ECHO.
ECHO  Waiting for you. Press a key once the page is up.
PAUSE
CLS
ECHO  [6/13] screen capture and BI_RLE8
PROBE.EXE screen
ECHO  [7/13] depth ladder, four bands, raw vs deflate vs RLE
PROBE.EXE depths
ECHO  [8/13] writing BMPs for off-machine checking
PROBE.EXE verify
ECHO  [9/13] ImageClick, cost of finding a picture on screen
PROBE.EXE imgfind
ECHO  [10/13] oleacc, foreground lock, window census
PROBE.EXE automation
ECHO  [11/13] cost of a window walk
PROBE.EXE uiwalk
ECHO  [12/13] what MSAA adds
PROBE.EXE msaa
ECHO  [13/13] reading the browser page as a tree
PROBE.EXE dom
CLS
ECHO ==============================================================
ECHO   Done. Please send back, from this folder:
ECHO ==============================================================
ECHO.
ECHO      PROBE.LOG      all the numbers
ECHO      S24.BMP        the same strip, three ways
ECHO      S8.BMP
ECHO      S8R.BMP
ECHO.
ECHO  The three BMPs are one strip of your screen, saved three
ECHO  ways. They are compared off the machine to prove the
ECHO  compressed one decodes to the same picture as the plain one.
ECHO.
ECHO  If a step printed something alarming, PROBE.LOG has it too.
ECHO.
PAUSE
