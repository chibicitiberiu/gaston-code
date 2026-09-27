@ECHO OFF
set EXPAND=YES
SET DIRCMD=/O:N
set LglDrv=27 * 26 Z 25 Y 24 X 23 W 22 V 21 U 20 T 19 S 18 R 17 Q 16 P 15
set LglDrv=%LglDrv% O 14 N 13 M 12 L 11 K 10 J 9 I 8 H 7 G 6 F 5 E 4 D 3 C
cls
call setramd.bat %LglDrv%
set temp=c:\
set tmp=c:\
path=%RAMD%:\;a:\;%CDROM%:\
copy command.com %RAMD%:\ > NUL
set comspec=%RAMD%:\command.com
copy extract.exe %RAMD%:\ > NUL
%RAMD%:\extract /y /e /l %RAMD%: ebd.cab > NUL
LH %ramd%:\MSCDEX.EXE /D:mscd001 /L:%CDROM% > NUL

ECHO.
ECHO Gaston Code unattended Windows 98 SE install
ECHO.

REM Make C: bootable so Setup's reboots land on the hard disk.
IF EXIST C:\SYSDONE.TXT GOTO SETUP
ECHO Making C: bootable...
SYS C: > NUL
ECHO done > C:\SYSDONE.TXT

:SETUP
REM Copy the answer file to C: FIRST.
REM
REM Setup reads the batch file lazily, not at launch, so referencing
REM A:\MSBATCH.INF means the floppy has to stay inserted for an unknown
REM length of time. It also must NOT be inserted when Setup reboots, or the
REM reboot lands back here and the install restarts from zero. Putting the
REM answer file on C: removes that race completely: the floppy becomes
REM disposable the moment this line finishes.
COPY A:\MSBATCH.INF C:\MSBATCH.INF > NUL
IF NOT EXIST C:\MSBATCH.INF GOTO NOBATCH

REM Find the CD. The RAM drive takes a letter, so it is not always D:.
FOR %%d IN (D E F G H I) DO IF EXIST %%d:\WIN98\SETUP.EXE SET SRC=%%d
IF "%SRC%"=="" GOTO NOCD
ECHO Source: %SRC%:\WIN98
ECHO Answer file: C:\MSBATCH.INF
ECHO Starting Setup (unattended)...
ECHO.
%SRC%:\WIN98\SETUP.EXE C:\MSBATCH.INF /IS /IM
GOTO END

:NOBATCH
ECHO Could not copy MSBATCH.INF to C:
GOTO END

:NOCD
ECHO Could not find WIN98\SETUP.EXE on any drive.
:END
