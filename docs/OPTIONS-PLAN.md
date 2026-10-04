# Options dialog: per-client settings and execution modes

## Context

This is Phase 4's last user-facing gap.
- **The current menu.** Configure has two items:
  - **Server Host...** edits only host and port.
  - **What can run without asking...** is a read-only view.
- **Hand edits.** Everything else is a hand edit of `gaston.ini` or a bridge flag.
- **The CLI's permission modes misbehave here:**
  - Auto skips `can_use_tool` for some tools and refuses Haiku.
  - "Always" escapes the policy.
  - Manual asks for every `list_dir`.

**Goal:** a **Configure → Options...** sheet that does four things:
- keeps every setting per client;
- replaces the CLI modes with our own **execution modes**;
- makes Auto stop nagging;
- shows openly why each call was allowed, checked or asked about.

Three review rounds shaped this plan; the third used six independent reviewers (red team,
workflow simulation, Win98 UX, Win98 platform, bridge architecture, simplicity).

### Decided with the user
- **Our desk decides.** The SDK runs in `default`, or in `plan` for the Plan preset. Our
  Auto works with Haiku.
- **Each category has one of four values:**
  - **Off**: the tool is not advertised.
  - **Ask me**: the bar appears.
  - **Checker decides**: the second model in `agent/review.py` decides, and the bar
    appears only when it is unsure.
  - **Allow**: it just happens.
- **Auto** is an allowlist of Allow rows, with Checker decides for the rest.
- **Guards classify; they never prompt.** The value of the category is final.
- **Mode selection is per conversation.** Options sets the mode new chats start in, and
  the mode definitions live in the client INI.
- **Three reach tiers:** work folder, other folders, system.
- **The client declares the OS facts.**
  - The protected paths, trusted programs and dangerous programs are all declared by the
    client, with defaults compiled into the client.
  - The bridge adds no OS data. It matches the client's lists as opaque patterns.
  - The existing per-shell grammar in `bridge/profiles/<shell>.toml` (separators, verbs,
    `[shell.always_ask]`) stays as it is, as shell data. It is the fallback, so an old
    client is still guarded against `format`.
  - An old client gets an empty system tier, and the rules view says so.
- **Programs the agent built in the work folder** (`run_built`) are Allowed in Auto.
  - A `.bat` there is judged line by line from the mirror copy, but only to *raise* its
    tier. A script that touches a protected path is still system.
- **Transparency is required.** Every list that decides something can be inspected from the
  dialog, together with where it is declared. A test line explains any command.
- **Command logs** are written on the Win98 machine.
- **Per-mode allowed models** are deferred to the BACKLOG. Our Auto removes the need,
  because Haiku now works in it.

### Invariants outside the matrix
These are expected exclusions in the prompt-count test.
- `off_limits` protects the bridge host.
- `.gaston/` writes are refused, including under its 8.3 alias and through exec.
- The sync-conflict halt and Continue.
- The existing-files dialog at the first commit.
- The guest's refusal to delete a drive root.
- The `input` precheck.
- The `automation` HELLO cap stays the hard ceiling on the screen rows.

## M0: a live bug, fix first

The exit-code wrapper calls a bare `find` (`bridge/profiles/command.com.toml:211`) with the
cwd set to the work folder. COMMAND.COM searches the cwd before PATH, so a `FIND.BAT`
planted by the agent runs on every wrapped exec, under every mode, today.

**Fix:** call `find` by its absolute path from the HELLO inventory, or drop the pipe.

**Test:** plant `find.bat`, run an exec, and assert that `find.bat` did not run.

## Categories and presets

| Row in the dialog | Category | Ask first | Free in work folder | Plan first | Auto |
|---|---|---|---|---|---|
| Change files: work folder | `files_work` | Ask | Allow | Ask | Allow |
| Change files: other folders | `files_else` | Ask | Ask | Ask | Check |
| Change files: Windows & system | `files_sys` | Ask | Ask | Ask | Check |
| Run programs: work folder (trusted tools) | `run_work` | Ask | Allow | Ask | Allow |
| Run programs: ones built here | `run_built` | Ask | Ask | Ask | Allow |
| Run programs: other | `run_else` | Ask | Ask | Ask | Check |
| Run programs: Windows & system | `run_sys` | Ask | Ask | Ask | Check |
| Read files and folders | `read` | Allow | Allow | Allow | Allow |
| Delete without the Recycle Bin | `purge` | Ask | Ask | Ask | Check |
| Look at the screen | `screen_see` | Allow | Allow | Allow | Allow |
| Click and type on the screen | `screen_drive` | Ask | Ask | Ask | Check |
| Run commands on the server | `bridge_shell` | Ask | Ask | Ask | Check |
| Use the web | `web` | Ask | Allow | Ask | Allow |

- **Preset names change, and the ids map once on reopen.** The ids are `manual`,
  `workfolder`, `plan` and `auto`.
  - `default` → Ask first
  - `acceptEdits` → Free in work folder
  - `plan` → Plan first
  - `auto` → Auto, with a one-line notice.
  - Maps are applied on read. The legacy `mode` key is still written so a rollback works.
- **Plan first equals Ask first and is locked.**
  - ExitPlanMode is always Ask. That bar is the plan approval.
  - On approval the desk returns `PermissionResultAllow(updated_permissions=[setMode default])`, the only `updated_permissions` still forwarded, then restores the chat's previous mode.
  - A spike confirms that the CLI offers ExitPlanMode with our tool list, and that it routes MCP writes to the desk during plan.
- **Outside the matrix, always on:** `toolnote` and `sync_status` (bridge data), and the
  bridge's own Read/Glob/Grep (mirror reads, which the CLI approves itself).
- **Any tool without a category is Ask.** A meta test asserts that every advertised tool has a category.

## Classification

`bridge/agent/categories.py` provides `classify(ctx, tool, args) -> Verdict(cat, rule, source)`.
- It is pure. `Ctx` holds the caps, grammars, client lists, work root, bridge work dir and mirror manifest.
- It absorbs `cmdguard.reason` and `drive_root_write` (`permdesk.py:340-375`), so the warning text and the category come from one analysis.
- It is covered by table tests.

### Resolution and tiers
- **One resolver.** A single resolver is shared with the tools: the cwd defaults to the work root and paths go through `_abs` (`tools.py:384-385`, `:755-763`).
  - "Inside" is measured against `work_guest` for both the fs tools and exec.
  - Bridge Write/Edit are measured with POSIX `realpath` against the mirror, as `off_limits` already does.
- **Danger scan.** It applies only where a command starts:
  - the first word of each fragment;
  - the word after `call`, `start`, `lh`/`loadhigh` and `command /c`;
  - `launch`'s program.
  - It checks against the client's `danger=` list plus the union of `always_ask` over every shell in the inventory, so 4DOS is covered.
  - `safe_if`/`ask_only_if` turn `fdisk /STATUS` into `read`.
  - `debug.c` as an argument is not a hit.
- **Program resolution follows the shell's search order.**
  - It uses new profile data: `[shell] cwd_first = true`, `scripts = [".bat"]` and `exts`.
  - If a bare name resolves to a file inside the work folder (checked against the mirror manifest), the call is `run_built`. This also defeats a planted `WCL386.BAT` shadowing a trusted tool.
  - **Trusted** means the name is on the client's `trust=` list *and* does not resolve inside the work folder. That gives `run_work` when there are no outside targets.
  - Any other program is `run_else`. That includes `launch`, `kill`, the profile's `unbounded` verbs, and any program when there is no mirror to check.
- **A `.bat` in the work folder** is read from the mirror and every line is classified.
  - The strictest line wins, with a floor of `run_built`.
  - `call` is followed two levels deep.
  - `%n`, `goto` or `for` cause the line to fall to doubt.
  - The content hash is rechecked just before the exec.
- **Targets.**
  - Write, move and delete targets come from `Guard.targets`, with redirects made quote-aware.
  - For a trusted program, every path-shaped token is scanned, including the text after `=` or `:` (e.g. `-fe=C:\…`).
  - An `@response` file and a makefile go through the same `.bat`-style read when they are in the mirror.
  - An exec made only of write verbs is classified as `files_*` by its targets.
  - A device target (`Root.check` DEVICE) is `files_else`.
  - UNC paths are `files_else`, even for reads.
  - Removing or moving the work root itself is `files_else`. Today `Root.check` treats an empty tail as INSIDE (`cmdguard.py:489-492`).
  - Bulk deletes through exec (`del`, `deltree`, `rd`) or `/y` overwrites of existing synced files are `purge`.
- **Reads.** An exec whose every fragment is a read verb, with no file redirect, is `read`.
  - Profile fixes keep this honest: `date` and `time` with arguments get `write_if`, and `print` and `pause` leave the read list.
  - Where a program lives, and paths it only reads, never raise the tier.
- **Doubt.** An unresolved `%VAR%`, a component of three or more dots, an unbalanced quote, or an unparseable line is doubt.
  - Doubt takes the stricter of the `*_else` and `*_sys` values, with the order Off > Ask > Check > Allow, except that Off becomes Ask.
  - The honesty line says the system tier is best effort, and that the work-folder boundary is the one that fails safe.
- **The client's own files.**
  - `protect.cpp` adds the client's install directory and `gaston.ini` to the protected defaults.
  - On the bridge, the protected list is a floor for the session. A PREFS sent mid-session may add to it but not remove from it. A removal takes effect at the next connect, with a notice.
  - `.gastonignore`/`.gitignore` writes are `files_else`.

### Client-declared lists (PREFS lines)
- `protect=<glob>`.
  - The client expands `GetWindowsDirectoryA` and the boot drive itself.
  - It adds 8.3 aliases via `GetShortPathNameA`, on the literal prefix only and on fixed drives only, cached once per run.
  - The defaults are narrow:
    - boot files at the root;
    - `WINDOWS\*.INI`;
    - `SYSTEM.DAT` and `USER.DAT`;
    - `WINDOWS\SYSTEM\**`;
    - `COMMAND.COM`.
  - `*\TEMP\**` is excluded from the defaults, so cleanups don't turn into a stream of system bars.
- `danger=<program>`.
- `trust=<program>`.
  - The defaults come from each enabled, detected tool's `trust1..8` keys in
    `tools_detect.ini`/`tools.ini` (see the Tools page). These are its compilers, linkers
    and make tools.
  - Also included: notepad, write, wordpad, mspaint and calc for `launch` when their arguments stay inside.
- **Matching on the bridge.** It normalises with `Guard.norm` and matches with a new grammar-agnostic `paths.glob_match(pat, path, sep, fold)`: `*` stays within one component, `**` crosses components.

## Bridge

### Prefs
- `bridge/prefs.py` is a registry of `Pref(key, parse, timing)`. The keys and their timing:
  - `general.model`, `general.effort`, `general.mode`: new conversations only, never forked.
  - `view.page`: the next page seal, clamped by `limits_for`.
  - `sync.sweep_s` and `sync.max_kb`: the next pass. The sweeper reads the guest's value and does not touch `config`.
  - `sync.ignore`: the next pass.
  - `protect`, `danger`, `trust`: live, under the floor rule. A change clears the checker cache.
  - `mode.*`: live at the desk. A change to the Off-set rebuilds the session (see below).
  - `perm.deadline_s`.
  - `general.model` from the client takes precedence over the bridge's `--model` for new chats.
- **Handshake.** `GuestSession.prefs` is replaced wholesale at every handshake. A HELLO without `prefs` resets it to the defaults, so prefs never leak between machines (`connection.py:213-218`).
- **The dispatcher lives in `SessionCoordinator`**, not in OptionsService, which sits at layer 3.
  - All keys share validate → remember → notice → `send_options`.
  - Each key brings its own apply:
    - model: live;
    - effort: the existing fork, with no revert;
    - mode: bookkeeping, plus `set_permission_mode` only when crossing the Plan boundary.
  - `PrefsService` sits at layer 7 or above in `SERVICE_LAYERS`.
  - SETMODEL, SETEFFORT and SETMODE move onto this dispatcher, which closes BACKLOG §10 layer 4.

### Policy
- `bridge/agent/policy.py` provides `Policy.verdict(cat)` and `sdk_mode()`, and replaces the `perm_mode` checks.
- `_PERMISSION_MODES` becomes `{default, plan}`.
- The presets are a Python table. A full-matrix agreement test checks it against the C++ table.
- **Conversation SETTINGS** (`conversations.py:102`) gain three keys: `mode_id`, `machine` and `grants`.
  - Grants are dropped if `machine` differs.
  - On every switch or new chat, the grants are cleared and the mode is set to `general.mode` *before* the restore. Today `restore_settings` returns early when nothing was saved (`services/session.py:263-264`).
  - User mode ids are random (`u-3f9a2c1d`), so a deleted `gaston.ini` cannot reuse one.
  - If a mode no longer exists, the chat falls back to `general.mode`, then to Ask first, with a notice. The stored id is kept.

### The desk
`can_use_tool` runs these steps in order:
1. the invariants;
2. `ensure_work`;
3. `classify`;
4. a matching grant lifts Ask or Check to Allow;
5. then by value:
   - **Off** denies with "turned off in this mode -- do not retry";
   - **Allow** allows;
   - **Check** runs the preview and the checker *concurrently*, and the bar appears only if the checker is unsure;
   - **Ask** shows the preview, then the bar.

`gate()` stays. It consumes the desk's approval and never checks the same call twice.

### The checker (`review.py`)
- It is one long-lived reviewer client per bridge, not a fresh `query()` for every call.
- **Its input is a fixed header built from the classifier:** tool, category, rule and resolved targets.
  - Content-like fields are fenced as untrusted and are the only fields truncated. Today `json.dumps(args)[:1500]` can hide `path` behind the content (`review.py:108`).
  - The system prompt says that instructions found inside the action are evidence for ASK.
- **The SAFE cache:**
  - It is per chat and bounded to 64 entries.
  - It is keyed on tool, args, rule and preview, plus a content hash for a script.
  - It is never used for `run_*`, `purge`, `files_sys`, `input` or `launch`.
  - It is cleared on any write in the chat, and on a mode, list or conversation change.
- **When unreachable**, it says so once (`_announce_gap`).
- **The bar shows "Checking…"** while the checker runs.

### "Allow for this chat" (replaces the CLI's Always)
- Forwarding of `updated_permissions` stops, apart from Plan's setMode.
- **Grants are narrow:**
  - `files_work`;
  - `run_work`;
  - `run_built`, keyed on the exact resolved path;
  - `screen_drive`, keyed on the target window's process, so typing into another window asks again.
- **There is no grant** for `files_else`, any `*_sys`, `purge`, `run_else`, or any doubt or DEVICE verdict.
- A grant resolves queued bars of the same category whose rule is not doubt. It is cleared when the mode changes, and it is stored in SETTINGS.
- This closes BACKLOG §11b.

### Off and session rebuilds
- The Session records a tool-surface fingerprint: the filtered `tools=`, the machine tool set, and `caps.automation`/`ops`.
- When a mode change or an attach changes that fingerprint, the session rebuilds under `turn_lock` with `make_session(resume=old)` and no fork, the same path the effort change uses (`services/session.py:548-585`).
- Filtering happens inside `machine_tools.build` and in `TOOLS`.
- Mid-turn, Off is also a desk denial.
- This also makes the automation checkbox truly take effect after a reconnect. Today `_post_attach` only builds a session when there is none (`server.py:390`).

### Prose
Every new sentence goes in `bridge/prompts/*.toml` (`test_prose_lives_in_toml.py`):
- the Off denial;
- notices;
- category tags;
- checker rule text;
- preset summaries.

Bridge prose says "protected path". The client owns "Windows & system".

## Wire

- **HELLO cap `prefs=1`.**
  - `files/caps.py:419,461` gains `prefs=_num(h,"prefs",0,0,1)`, and it is added to `summary()` and `win/net/caps.h`. 36 of the 48 header slots are used.
  - Each added key or category bumps the number, and unknown values are refused.
- **PREFS frame.**
  - The client queues it right after HELLO through `rc_net_set_prefs` (`net.cpp:544-555`).
  - It is never compressible (`frameproto.cpp:64`).
  - The body is built once, cached, and capped at **16 KB** so HELLO + inventory + PREFS fit the 64 KB out-queue (`link.h:34`).
  - The body is `key=value` lines in ACP, split on the first `=`.
- **On the bridge.** `attach` is split as `negotiate` → `read_prefs` (only when `caps.prefs≥1`, after the secret check and `fold.use(acp)`) → the rest.
  - PREFS must be the next frame, otherwise `expected_prefs`.
  - A bad PREFS is `Refused("bad_prefs", key)`.
  - Legacy clients are never delayed.
- **Re-send on OK.** The client sends PREFS again through `send_ui`.
  - The bridge answers `PREFSACK ok=0|1 key=` and never ERROR, so a mid-turn turn is not aborted (`mainwindow.cpp:688-701`).
  - If the send gets FULL, it retries with a visible notice.
- **The client handles `unknown_type`** for PREFS or GETLISTS without aborting the turn. It degrades to "settings saved here only".
- **Inspector frames.**
  - `GETLISTS list=<id>` returns `LISTENT` rows and then `LISTEND hidden=N`, at most 256 rows per list.
  - `GETLISTS kind=exec|path` carries the test line in the body, together with the pending (unsaved) lists from the sheet, so the test does not lie before OK.
  - Guest handlers land in the same commit (`test_frame_contract.py`).
  - Old clients still send `RULES`, and `rules_view` is rewritten from the Policy.

## Client

### Platform foundations
- **`dlgtmpl.{h,cpp}`** is extracted from `hostdlg.cpp:38` and `workdlg.cpp:53-66`.
  - Each page gets its own heap `std::vector<BYTE>`, capped at 8 KB and owned by the sheet until `PropertySheetA` returns, because pages are created lazily.
  - It gets a style parameter in `head()`. Pages use `WS_CHILD|DS_CONTROL|DS_3DLOOK|DS_SETFONT|WS_CAPTION`.
  - It gets an item form that takes a class-name string.
  - Combo `cy` includes the dropdown height, about 60 DLU.
- **A new `DialogPage` base in `core/window.h`.**
  - Its static DLGPROC finds `this` from `psp->lParam` at WM_INITDIALOG and stores it in `DWL_USER`.
  - It holds a non-owning HWND, cleared at WM_NCDESTROY.
  - It has virtual `onInit`/`onApply`/`onKillActive`.
  - Pages never call EndDialog.
  - CLAUDE.md gains this as the third window shape.
- **`OptionsSheet`.**
  - It uses `sizeof(PROPSHEETPAGEA_V1)` and `sizeof(PROPSHEETHEADERA_V1)`, which Watcom has (`prsht.h:345,555`).
  - It uses `PSH_USECALLBACK` and strips `DS_CONTEXTHELP` at `PSCB_PRECREATE`.
  - It shows **Apply** (no `PSH_NOAPPLYNOW`), disabled until something changes.
  - It saves on an `applied` flag set in PSN_APPLY and on a diff, never on the return value.
  - It validates in PSN_KILLACTIVE.
  - Child dialogs are owned by the sheet.
  - `ICC_TAB_CLASSES` is added at `main.cpp:147`.
  - `IDM_OPTIONS` replaces `IDM_HOST`, and `hostdlg` is deleted.
  - "What can run without asking..." opens the Permissions page.
  - The plan states that a permission bar cannot be answered while the sheet is open, and that its timer keeps running.
- **`applyConnectionConfig()`** covers automation (`main.cpp:219`) and the sync root (`:172-196`).
  - It is called between `rc_net_stop` and `rc_net_start` through a hook in `Reconnector::tryConnect` (`reconnect.cpp:62-72`). `g_home` is not locked, which is mem-audit F7.
  - Fields that reconnect carry "(reconnects)".
  - If a turn is streaming, OK asks "Stop the current reply and reconnect?".
- **INI.**
  - `IniFile` gains `deleteKey` (`WritePrivateProfileStringA(s,k,NULL)`) and `dropSection`.
  - Lists use **numbered keys** (`add01=`…`add32=`, `rem01=`…), because the profile API cannot hold repeated keys. `checkKeys` gets a prefix+number form.
  - An orphaned `[mode.*]` section, one not named in `[modes] order=`, is ignored and dropped on the next save.
  - All of this stays under 12 KB, against the 64 KB limit.

### Tabs (about 230×170 DLU pages; mnemonics on every label)
- **Connection:**
  - host;
  - port;
  - secret (masked).
- **General:**
  - "New chats start in mode:", with the hint "The Mode menu changes the current chat only.";
  - default model;
  - default effort;
  - autoscroll, which stays in sync with the pagebar `ID_FOLLOW`;
  - page size: Small, Normal or Large.
- **Files:**
  - work folder root, with Browse;
  - "Share files with the assistant";
  - sync every N minutes, or manual;
  - largest file to sync;
  - "Ignore (never copy)": a listbox with Add/Remove, at most 64 entries, and no multiline EDIT because of the Esc→WM_CLOSE quirk;
  - "When a new work folder already has files": Ask me (the default), Keep theirs, Replace or Merge.
- **Permissions:**
  ```
  Mode: [Auto (modified)  v] [Save As...] [Delete]
    Works freely in the work folder; a checker screens the rest.
  +- Files and programs -----------------------------------+
  |                       Change files     Run programs    |
  |  In the work folder   [Allow      v]   [Allow      v]  |
  |  Built here                            [Allow      v]  |
  |  Other folders        [Checker... v]   [Checker... v]  |
  |  Windows and system   [Checker... v]   [Checker... v]  |
  +- Other actions ----------------------------------------+
  |  Read files and folders          [Allow           v]   |
  |  Delete without Recycle Bin      [Checker decides v]   |
  |  Look at the screen              [Allow           v]   |
  |  Click and type on the screen    [Checker decides v]   |
  |  Run commands on the server      [Checker decides v]   |
  |  Use the web                     [Allow           v]   |
  +--------------------------------------------------------+
  [x] Let the assistant see and use this screen (reconnects)
  Ask times out after [10] minutes.
  Checker = a second AI that screens risky actions.
                        [Lists...]  [Restore Defaults]
  ```
  - **Save As pattern.** The page uses Win98's Save As pattern, as in Display schemes.
    - Editing a built-in mode shows "X (modified)".
    - **Save As...** creates a user mode.
    - On OK with unsaved changes, the page asks "Save as a new mode?".
    - **Delete** is for user modes only.
    - There is no Rename.
    - **Plan first** is the only locked mode.
  - **Summary.** Built-ins have fixed summary text. A user mode gets a two-sentence summary generated from its matrix.
  - **Combos and rows.**
    - Off is listed last in each combo, as "Off (assistant can't try)".
    - Read has no Off.
    - The screen rows are greyed when automation is off.
    - The checker has a one-line legend and a tooltip-free hint.
  - **Timeout.** The timeout text at `permdesk.py:421-423` becomes parameterised.
  - **User modes** are stored as snapshots under random ids, with at most 8. `base=` only fills in categories added in a later version.
- **Tools:** compilers, script hosts, shells and the like.
  - It is a front end over the existing detection (`win/app/tooldet.cpp`):
    - `tools_detect.ini` is shipped and replaced on every install.
    - `tools.ini` holds the user's own entries. It is never overwritten and is read
      second, so it wins.
  ```
  Tools found on this computer                       [Rescan]
   [x] Open Watcom C/C++     C:\WATCOM            Built in
   [x] Visual C++ 6.0        C:\Program Files\...  Built in
   [ ] 4DOS                  not found             Built in
   [x] MS-DOS shell          C:\WINDOWS\COMMAND.COM Built in
   [x] My assembler          C:\NASM               Your entry
  Selected: Open Watcom C/C++ -- the server knows how to use it.
  Found by: WATCOM=C:\WATCOM; C:\WATCOM\BINNT\WCL386.EXE
  Trusted programs: wcl386, wpp386, wmake, wlink        [Edit...]
                         [Add...] [Edit...] [Remove]
  Changes apply when Gaston reconnects.
  ```
  - **The list.** A checkbox listbox with one row per INI section: the name, where it was
    found (or "not found"), and the source ("Built in" for `tools_detect.ini`, "Your entry"
    for `tools.ini`).
    - The selected row's footer shows two things. First, whether the server has a profile
      for it ("the server knows how to use it" or "reported only"). That comes from
      `GETLISTS list=profiles`, which returns the profile names plus their `about` line.
      Second, the evidence that matched.
  - **Enable/disable.**
    - A disabled tool is left out of the HELLO inventory, so the agent is not told about
      it and does not get its note.
    - The state is stored as `[name] enabled=0` in `tools.ini`, since the user's section
      wins. A built-in section is never edited.
    - The last enabled shell cannot be disabled, because the bridge needs one to run
      `exec`.
  - **Add / Edit.** A small dialog with these fields:
    - the name: a combo of known server profiles (from `GETLISTS`), or free text;
    - environment variables (up to 8);
    - marker files (up to 8, with Browse; long names only, per the `tools_detect.ini`
      rule);
    - trusted programs.
    - It writes `env1..8`, `file1..8` and `trust1..8` to `tools.ini`.
    - **Remove** works on user entries only. A built-in can only be disabled.
  - **Trusted programs live with the tool.** A tool's `trust1..8` keys are the default
    `trust=` list for the classifier.
    - It is sent only for enabled tools that were found.
    - The shipped `tools_detect.ini` gets these keys for every toolchain it lists. Examples:
      Watcom → wcl386, wpp386, wmake, wlink; MSVC → cl, link, nmake; cscript → cscript.
    - So the client declares trust per tool, and the bridge stays OS-agnostic.
    - Disabling a tool also removes its programs from the trust list.
  - **Rescan.** Clears `rc_tooldet_cached` and reconnects (marked "reconnects"). The
    session-fingerprint rebuild then picks up the new tool set.
  - **Bounds.** `RC_TOOLDET_MAX_SECTIONS` 32 and `RC_TOOLDET_MAX_N` 8 stay as they are,
    and `tooldet` keeps refusing past them.
- **Misc:**
  - "Save every command and its output", on or off;
  - the app log folder, which applies at the next start.
  - The app log itself cannot be turned off; the startup banner always stays.

### Lists... (the transparency inspector)
```
+- Try it --------------------------------------------------+
| Command or path: [scanreg /fix                 ] [Test]   |
| In "Auto": runs a program at system level, so a checker   |
| decides. Why: scanreg is on Dangerous programs (built in).|
+-----------------------------------------------------------+
Show: [Dangerous programs v]  (Protected places | Programs I trust |
       Dangerous programs | Shell rules, built in (read-only) |
       Files never copied (read-only))
  format      Erases a disk                 Built in
  scanreg     Changes the registry          Built in
  mytool                                    Your list
Selected: scanreg -- Changes the registry. Declared in: Built in (Gaston)
   [Add...] [Remove] [Restore Defaults]
```
- **Layout.**
  - The list is a LISTBOX with `LBS_USETABSTOPS`. A footer shows the full reason and source. There is no ListView and no tooltip, keeping the chatlist decision and comctl32 4.00 compatibility.
  - The dialog is about 260 DLU wide.
- **Try it.**
  - "Try it" sits at the top.
  - It uses the mode selected on the Permissions page and the pending lists.
  - It highlights the matching row.
- **Sources** are "Built in" (`win/app/protect.cpp`), "Your list" (`gaston.ini [protect]` and the other list sections), and "Server: <shell> rules" (`bridge/profiles/<shell>.toml`).
- **Editing.**
  - Removing a built-in entry greys it out and asks for confirmation ("This lets the assistant touch it without extra checks.").
  - Restore Defaults brings back removed entries.
  - There is no "Open in Notepad": the next OK would overwrite hand edits.

### The bar
- The command stays first, within the 140/216 limit (`permdesk.py:56-79`), followed by a plain tag: "work folder", "other folder", "Windows system", "screen" or "checker unsure: <reason>".
- The full explanation goes in the transcript block.
- The grant button names its scope: "Allow edits here, this chat", "Allow programs here, this chat", "Allow this program, this chat", or "Allow typing in <window>, this chat".

### Logging
- **The app log, which closes BACKLOG §24.**
  - `rc_log_open` appends to the existing `.log` with a banner.
  - After the mutex passes, `rc_log_begin_run(dir)` renames the old log to `.prv` and starts a new file.
  - The folder applies at the next start.
- **The command log is `<logdir>\CMDLOG\YYYYMMDD.CMD`.** The 8.3 rule applies.
  - Each chunk is written directly by a wrapper around `ExecOut` (`rpc.cpp:431`), on the worker only (`run_one` at `:575` and the abandon path at `:523`, never the reader's `cancel_job`). There is no tee and no flush.
  - Each entry is a header line, then the chunks, then a trailer.
  - The 1 MB/day cap is checked with `GetFileSize`.
  - A 30-day sweep runs once a day on the worker.
  - A log folder inside the sync root is refused.

## Milestones and commits

Each commit is small and leaves master green with old and new clients.

**M0: the live fix.**
1. Call `find` by its absolute path in the exit-code wrapper, with a test.

**M1: Auto stops nagging (bridge only, plus a client stub for PREFSACK).** A Haiku spike comes first: run 30 scripted Auto calls and measure the checker's unsure rate.

2. Profile data:
   - `cwd_first`, `scripts` and `exts`;
   - the read/write fixes (`date`/`time`, `print`/`pause`);
   - the `always_ask` union.
3. `paths.glob_match`, the shared resolver, and `classify(Ctx)` with every table row listed under Verification. No behaviour change yet.
4. `Policy`, the presets and Check:
   - the checker rework (long-lived client, fixed header, cache rules, concurrency);
   - SDK `default`/`plan`;
   - the ExitPlanMode spike and the setMode path;
   - the SETTINGS keys, the legacy mapping and the notice;
   - rewriting `rules_view`;
   - the meta test that every tool has a category;
   - the prose in TOML.
   - The four presets reach the existing Mode menu.
5. Grants and the stop on forwarding. Clear grants and mode on switch.
6. The surface fingerprint, the resume-rebuild, and Off filtering.

**M2: the Options sheet.** A spike comes first: a two-page `PropertySheetA` on the VM, checked at 640×480 and with Large Fonts, with a free-resource reading, while a turn streams.

7. Bridge:
   - the `prefs` cap;
   - `read_prefs`;
   - the registry with its timings;
   - PREFSACK;
   - the dispatcher in the Coordinator, with SET* moved onto it;
   - the wholesale reset at handshake;
   - the protect floor.
8. Client:
   - `protect.cpp` defaults (protect, danger, trust, the 8.3 aliases, the install dir and `gaston.ini`);
   - `rc_net_set_prefs`;
   - `IniFile` `deleteKey`/`dropSection`/numbered keys;
   - `unknown_type` and PREFSACK handling.
9. `dlgtmpl`, `DialogPage`, `OptionsSheet` and the Connection page, plus `applyConnectionConfig`. `hostdlg` is retired.
10. The Permissions page: Save As, user modes, Restore Defaults, the automation checkbox and the timeout.
11. GETLISTS/LISTENT/LISTEND, the Lists... inspector with Try it, and the bar tags and grant labels.
12. The General page, with the bridge's `view.page`.
13. The Files page, with the bridge's `sync.*`.
13b. The Tools page:
     - `enabled=` and `trust1..8` in `tooldet`, with disabled tools left out of the
       inventory;
     - `trust1..8` added to `tools_detect.ini`, with a test asserting that every toolchain
       section has them;
     - `GETLISTS list=profiles`;
     - Add/Edit writing `tools.ini`;
     - Rescan clearing the cache and reconnecting.
     - VM check: disabling Watcom drops its note from the system prompt, and an added tool
       is reported after Rescan.

**M3: Misc and docs.**

14. The Misc page and `rc_log_begin_run`, which closes §24.
15. The command log.
16. Docs:
    - PLAN Phase 4;
    - BACKLOG: §10, §11b and §24 closed, §19 updated, and a new entry for per-mode models;
    - PROGRESS;
    - CLAUDE.md (`DialogPage`).

## Verification

### Every commit
- The targeted pytest directories (`tests/python/{wire,services,agent,files,view,meta,server}`).
- `make check-gaston`.
- `make lint`.
- `make wine-smoke` for client code.
- A full `pytest` before each milestone closes.

### Tests that break and get rewritten
- `test_mode_descriptions.py`.
- `services/test_options.py`.
- `agent/test_auto_review.py`.
- `server/test_conversation_settings.py`.
- `services/test_session.py`.
- Goldens 01, 02 and 07.
- `agent/test_permissions_gaston.py`.

### Prompt-count test
It replays five scripted sessions through `can_use_tool` and `gate()` under each preset:
- hello world;
- the CONFIG.SYS fix;
- a Notepad letter;
- a C:\TEMP cleanup;
- a D:\PROJECTS port.

Targets:
- Free in work folder: at most 1 bar for hello world and for the port.
- Auto: zero bars for hello world, and no rubber-stamp checks on its build/run loop.
- Only the listed invariants and an unsure checker produce bars in Auto.

### Classifier table
| Case | Expected |
|---|---|
| exec with no cwd | `run_work` / `run_built` / `run_else` correctly |
| `put_file src\a.c` | `files_work` |
| planted `WCL386.BAT` + `exec wcl386` | `run_built` |
| `x.bat` containing `deltree C:\WINDOWS` | `run_sys` |
| `wcl386 x.c -fe=C:\WINDOWS\COMMAND.COM` | system |
| `deltree /y <work root>` | `files_else` |
| `del *.*` of synced files | `purge` |
| 4DOS `format c:` | `run_sys` |
| `fdisk /STATUS` | `read` |
| `wcl386 debug.c` | not system |
| `type C:\AUTOEXEC.BAT` | `read` |
| `date 01-01-98` | not `read` |
| `del %WINDIR%\WIN.INI` | doubt rule |
| `...\x` | doubt |
| quoted redirect into `C:\AUTOEXEC.BAT` | system |
| copy to `LPT1` | `files_else` |
| `\\srv\x` read | `files_else` |
| `.gaston` via exec and via its 8.3 alias | refused |
| write to `gaston.ini` | `files_sys` |
| legacy client with no lists | `format` → `run_sys` |

### Bridge tests
- Prefs reset after a legacy HELLO.
- `read_prefs` with the wrong order, and with a timeout.
- A bad key gets PREFSACK.
- A protect removal is refused mid-session.
- Grants: scoped, dropped on a machine change, cleared on switch, refused for doubt.
- The checker cache is not used for scripts and is invalidated by writes.
- A changed fingerprint triggers a rebuild.
- The ExitPlanMode transition.
- The GETLISTS caps.
- Full-matrix preset agreement.

### On the VM (`retroclaude-win98`, bridge `--model haiku`)
- Haiku in Auto builds and runs hello.exe and BUILD.BAT with no bar.
- `exec scanreg /fix` and `put_file C:\AUTOEXEC.BAT` go to the checker, and the bar shows "Checking…".
- "Allow this program, this chat" stops repeat bars.
- Lists... Try it explains `scanreg`, and a removed entry takes effect after OK.
- The sheet fits at 640×480 with small and Large Fonts, and the free resources are recorded.
- The sheet stays usable over a streaming turn with a pending bar.
- Apply works.
- A host change and the automation checkbox take effect on reconnect.
- Ignore patterns take effect.
- `CMDLOG\*.CMD` holds a long exec.
- A reopened `acceptEdits` chat shows "Free in work folder".
- `FIND.BAT` is not executed.
