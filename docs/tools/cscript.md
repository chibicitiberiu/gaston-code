# Windows Script Host (`cscript`) on Windows 98

`cscript.exe` is the console-mode execution host for the Windows Script Host (WSH) runtime. On Windows 98 SE, it provides a full COM-capable scripting environment (supporting VBScript and JScript) that is significantly more expressive and reliable than MS-DOS batch scripts under `COMMAND.COM`.

---

## 1. Architecture: `cscript.exe` vs `wscript.exe`

Windows provides two script host binaries:

| Host | Binary | Output Target | Suitability for Agents |
|---|---|---|---|
| **CScript** | `CSCRIPT.EXE` | Console (`stdout` / `stderr`) | **Yes** — captures clean text output and return codes |
| **WScript** | `WSCRIPT.EXE` | GUI Dialogs (`MessageBox`) | **No** — `WScript.Echo` and runtime errors pop up modal dialogs that halt headless execution |

> [!IMPORTANT]
> **Always invoke scripts through `cscript //nologo script.vbs`**.
> Never run `.vbs` files bare (e.g. `script.vbs`) and never call `wscript.exe`. The default file association for `.vbs` on Windows 98 points to `wscript.exe`, which displays every `WScript.Echo` as a modal pop-up dialog that freezes non-interactive execution waiting for user input.

---

## 2. Detection & Installation Paths

On Windows 98 SE, `CSCRIPT.EXE` is installed as part of the optional "Windows Scripting Host" component (enabled by default in standard installs).

- **Windows 98 SE default**: `C:\WINDOWS\COMMAND\CSCRIPT.EXE` (already on default `PATH`)
- **WSH 5.6 update / Win ME**: `C:\WINDOWS\CSCRIPT.EXE` or `C:\WINDOWS\SYSTEM\CSCRIPT.EXE`
- **Windows NT / 2000 / XP**: `C:\WINNT\SYSTEM32\CSCRIPT.EXE` or `C:\WINDOWS\SYSTEM32\CSCRIPT.EXE`

---

## 3. Version Differences: WSH 5.0 vs WSH 5.6

Windows 98 SE ships out of the box with **WSH Version 5.0** (build `5.0.0.4615`). Microsoft released **WSH 5.6** (`scr56en.exe`) as a standalone update for Windows 98/ME/NT4.

### Detection Mechanism
The bridge tool detection checks for marker files:
- If `C:\WINDOWS\SYSTEM\WSHCON.DLL` (the WSH Controller library for remote scripting) is present, the bridge reports **WSH 5.6**.
- Otherwise, if `CSCRIPT.EXE` is present, the bridge reports **WSH 5.0**.

### Critical Feature Differences

| Feature | WSH 5.0 (Win98 SE Default) | WSH 5.6 (Redistributable Update) |
|---|---|---|
| `WScript.Echo` | Yes (prints to console stdout) | Yes |
| `WScript.Arguments` | Yes (indexed argument collection) | Yes (plus Named and Unnamed collections) |
| `WScript.Quit(code)` | Yes (returns DOS `errorlevel`) | Yes |
| `Scripting.FileSystemObject` | Yes (`scrrun.dll`) | Yes |
| `WScript.Shell.Run` | Yes (`Run(cmd, intWindowStyle, bWaitOnReturn)`) | Yes |
| `WScript.Shell.Exec` | **Not available** | **Available** (`Exec` returns `StdIn`/`StdOut`/`StdErr`) |
| `WScript.Sleep` | Not in base 5.0 | Yes |

> [!WARNING]
> **In WSH 5.0, `WshShell.Exec` does NOT exist.**
> Calling `WshShell.Exec(...)` in WSH 5.0 fails with runtime error: *Object doesn't support this property or method*.
> To capture output from an external program in WSH 5.0, redirect stdout to a temporary file using `WshShell.Run "%COMSPEC% /c command > out.tmp", 0, True` and read `out.tmp` using `FileSystemObject`.

---

## 4. Command-Line Switches

```bat
cscript [options...] script.vbs [arguments...]
```

Essential switches:
- `//nologo`: Suppresses the Microsoft startup banner. Essential for machine-readable output.
- `//B`: Batch mode. Suppresses all error dialogs and user prompts from displaying.
- `//T:<seconds>`: Execution timeout in seconds (e.g. `//T:30`). Prevents runaway or hung scripts.
- `//E:<engine>`: Overrides engine (e.g. `//E:VBScript` or `//E:JScript` if using non-standard extension).
- `//H:CScript`: Registers CScript as the default script host for the current user.

Example recommended invocation:
```bat
cscript //nologo //B //T:30 myscript.vbs arg1 arg2
```

---

## 5. Practical VBScript Patterns for Agents

### Pattern 1: Run Command & Capture Output (WSH 5.0 Safe)
```vbscript
' run_cmd.vbs
Option Explicit
Dim sh, fso, tmpFile, cmd, text

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

tmpFile = fso.GetSpecialFolder(2) & "\out_" & fso.GetTempName()
cmd = "%COMSPEC% /c dir C:\ > " & tmpFile

sh.Run cmd, 0, True

If fso.FileExists(tmpFile) Then
    Dim ts
    Set ts = fso.OpenTextFile(tmpFile, 1) ' 1 = ForReading
    If Not ts.AtEndOfStream Then
        text = ts.ReadAll
        WScript.Echo text
    End If
    ts.Close
    fso.DeleteFile tmpFile, True
End If
```

### Pattern 2: Reading & Writing Files with `FileSystemObject`
```vbscript
' file_io.vbs
Option Explicit
Dim fso, ts, line

Set fso = CreateObject("Scripting.FileSystemObject")

' Writing
Set ts = fso.CreateTextFile("C:\WORK\SAMPLE.TXT", True) ' True = overwrite
ts.WriteLine "First line"
ts.WriteLine "Second line"
ts.Close

' Reading line-by-line
If fso.FileExists("C:\WORK\SAMPLE.TXT") Then
    Set ts = fso.OpenTextFile("C:\WORK\SAMPLE.TXT", 1)
    Do While Not ts.AtEndOfStream
        line = ts.ReadLine
        WScript.Echo "Line: " & line
    Loop
    ts.Close
End If
```

### Pattern 3: String Search and In-Place Replace
```vbscript
' replace_text.vbs
Option Explicit
Dim fso, ts, content, targetFile

targetFile = "C:\WORK\CONFIG.INI"
Set fso = CreateObject("Scripting.FileSystemObject")

If fso.FileExists(targetFile) Then
    Set ts = fso.OpenTextFile(targetFile, 1)
    content = ts.ReadAll
    ts.Close

    content = Replace(content, "OldString", "NewString")

    Set ts = fso.CreateTextFile(targetFile, True)
    ts.Write content
    ts.Close
    WScript.Echo "Replaced successfully."
Else
    WScript.Echo "File not found: " & targetFile
    WScript.Quit 1
End If
```

### Pattern 4: Registry Reading & Writing
```vbscript
' reg_access.vbs
Option Explicit
Dim sh, val

Set sh = CreateObject("WScript.Shell")
On Error Resume Next
val = sh.RegRead("HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Version")
If Err.Number = 0 Then
    WScript.Echo "Windows Version: " & val
Else
    WScript.Echo "Key not found."
End If
On Error GoTo 0
```
