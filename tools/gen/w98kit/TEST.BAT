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
ECHO  [1/10] platform, CPU and memory
PROBE.EXE platform
ECHO  [2/10] threads
PROBE.EXE threads
ECHO  [3/10] 16-bit EXEC capture
PROBE.EXE exec
ECHO  [4/10] deflate throughput  -- THE number we cannot fake
PROBE.EXE deflate
ECHO  [5/10] screen capture and BI_RLE8
PROBE.EXE screen
ECHO  [6/10] depth ladder, four bands, raw vs deflate vs RLE
PROBE.EXE depths
ECHO  [7/10] writing BMPs for off-machine checking
PROBE.EXE verify
ECHO  [8/10] oleacc, foreground lock, window census
PROBE.EXE automation
ECHO  [9/10] cost of a window walk
PROBE.EXE uiwalk
ECHO  [10/10] what MSAA adds
PROBE.EXE msaa
CLS
ECHO ==============================================================
ECHO   One step left, and it needs a browser window.
ECHO ==============================================================
ECHO.
ECHO  Internet Explorer is about to open UITEST.HTM. Let it finish
ECHO  loading, LEAVE IT OPEN, come back here and press a key.
ECHO.
ECHO  If IE does not open by itself, open UITEST.HTM from this
ECHO  folder by hand.
ECHO.
PAUSE
START UITEST.HTM
ECHO.
ECHO  Waiting for you. Press a key once the page is up.
PAUSE
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
