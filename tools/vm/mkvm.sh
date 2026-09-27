#!/usr/bin/env bash
# Build an unattended Windows 98 SE guest for Gaston Code.
#
# Win98 Setup supports an answer file (MSBATCH.INF) -- this is how OEMs
# deployed it. Combined with partitioning and formatting the disk from Linux,
# that removes every interactive step except hardware detection.
#
# Boot order is `once=a,order=c`: the floppy runs once to `sys c:` and launch
# Setup, and every reboot after that comes off the hard disk, which is what
# Setup expects.
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
HERE="$REPO"
VM="$HERE/vm"
LIB="${WIN98_LIB:-/mnt/zizo/Library/Software/Operating Systems/Windows/Windows 9x}"
ISO="${WIN98_ISO:-$LIB/Win98SE_bootable.iso}"

DISK="$VM/win98.qcow2"
RAW="$VM/win98.raw"
FLOPPY="$VM/boot98.img"
DISK_MB="${DISK_MB:-2000}"

# Supplied by the user; not stored in git.
KEY="${WIN98_KEY:?set WIN98_KEY to the product key}"

mkdir -p "$VM"

say() { printf '\n=== %s ===\n' "$*"; }

# --- 1. boot floppy ---------------------------------------------------------
say "extracting the El Torito boot floppy"
if [ ! -f "$FLOPPY" ]; then
    # 7z exits non-zero on "Incorrect big-endian headers" for this ISO while
    # still extracting correctly, so the exit status is deliberately ignored
    # and the result is checked instead.
    7z e -y -o"$VM" "$ISO" '[BOOT]/*' >/dev/null 2>&1 || true
    if [ -f "$VM/Boot-1.44M.img" ]; then
        mv -f "$VM/Boot-1.44M.img" "$FLOPPY"
    else
        echo "could not extract the boot floppy from $ISO" >&2
        exit 1
    fi
fi
# It is a FAT12 image, so mtools can rewrite files in place without touching
# the boot sector.
file "$FLOPPY" | sed 's/^/    /'

# --- 2. answer file ---------------------------------------------------------
say "writing MSBATCH.INF"
cat > "$VM/msbatch.inf" <<INF
[BatchSetup]
Version=3.0 (32-bit)

[Setup]
Express=1
InstallDir=C:\WINDOWS
InstallType=3
ProductKey="$KEY"
EBD=0
ShowEula=0
ChangeDir=0
OptionalComponents=1
Network=1
System=0
CCP=0
CleanBoot=0
Display=0
PenWinWarning=0
InstallLocationsMRU=0
DevicePath=0
Uninstall=0
NoPrompt2Boot=1
TimeZone="GMT"
VRC=0

[NameAndOrg]
Name="Gaston Code"
Org="Gaston Code"
Display=0

[Network]
ComputerName="RETRO98"
Workgroup="WORKGROUP"
Description="Gaston Code guest"
Display=0
PrimaryLogon=VREDIR
Clients=VREDIR
Protocols=MSTCP
Security=SHARE

[MSTCP]
LMHOSTS=0
DHCP=1
DNS=0

[VREDIR]
ValidatedLogon=0

[OptionalComponents]
"Accessibility Options"=0
"Desktop Wallpaper"=0
"Document Templates"=0
"Games"=0
"Imaging"=0
"Mouse Pointers"=0
"Paint"=1
"Screen Savers"=0
"Windows Scripting Host"=1
"Dial-Up Networking"=0
"Direct Cable Connection"=0
"HyperTerminal"=0
"Phone Dialer"=0
"Audio Compression"=0
"Video Compression"=0
"Volume Control"=0
"Sound Recorder"=0
"Media Player"=0
INF
unix2dos -q "$VM/msbatch.inf" 2>/dev/null || sed -i 's/$/\r/' "$VM/msbatch.inf"
wc -l < "$VM/msbatch.inf" | sed 's/^/    lines: /'

# --- 3. floppy AUTOEXEC -----------------------------------------------------
say "patching the floppy AUTOEXEC.BAT"
cat > "$VM/autoexec.bat" <<'BAT'
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
BAT
sed -i 's/$/\r/' "$VM/autoexec.bat"

mcopy -i "$FLOPPY" -o "$VM/autoexec.bat" ::AUTOEXEC.BAT
mcopy -i "$FLOPPY" -o "$VM/msbatch.inf"  ::MSBATCH.INF
# Shorten the boot menu wait from 30s.
mtype -i "$FLOPPY" ::CONFIG.SYS > "$VM/config.sys"
sed -i 's/menudefault=CD,30/menudefault=CD,3/' "$VM/config.sys"
mcopy -i "$FLOPPY" -o "$VM/config.sys" ::CONFIG.SYS
mdir -i "$FLOPPY" ::MSBATCH.INF ::AUTOEXEC.BAT | tail -4 | sed 's/^/    /'

# --- 4. hard disk -----------------------------------------------------------
say "creating a ${DISK_MB}MB FAT32 disk"
if [ -f "$RAW" ]; then
    cp -f "$RAW" "$RAW.bak.$(date +%Y%m%d-%H%M%S)"
    echo "    backed up the existing raw image before overwriting"
fi
rm -f "$RAW"
truncate -s "${DISK_MB}M" "$RAW"

# Type 0x0C = FAT32 LBA, which is what Win98 SE expects. Start at 2048 for
# alignment; Win98 does not care but modern tooling is happier.
sfdisk -q "$RAW" >/dev/null <<PART
label: dos
start=2048, type=0c, bootable
PART

OFFSET=$((2048 * 512))
mkfs.vfat -F 32 -n WIN98 --offset 2048 "$RAW" >/dev/null
echo "    partition at byte $OFFSET"
mdir -i "$RAW"@@$OFFSET :: 2>&1 | tail -2 | sed 's/^/    /'

qemu-img convert -f raw -O qcow2 "$RAW" "$DISK"
qemu-img info "$DISK" | grep -E 'virtual size|disk size' | sed 's/^/    /'

say "ready"
echo "  disk:   $DISK"
echo "  floppy: $FLOPPY"
echo "  iso:    $ISO"
echo
echo "Run: tools/vm/runvm.sh install"
