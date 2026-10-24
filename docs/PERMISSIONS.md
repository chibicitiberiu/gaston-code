# Permissions: execution modes, classification and the desk

What decides whether a tool call runs, gets checked, or raises a bar. The
front end for all of it is **Configure → Options...** in the client.

Four things it does:
- keeps every setting per client;
- replaces the CLI modes with our own **execution modes**;
- makes Auto stop nagging;
- shows openly why each call was allowed, checked or asked about.

## Design rules
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

## Invariants outside the matrix
These are expected exclusions in the prompt-count test.
- `off_limits` protects the bridge host.
- `.gaston/` writes are refused, including under its 8.3 alias and through exec.
- The sync-conflict halt and Continue.
- The existing-files dialog at the first commit.
- The guest's refusal to delete a drive root.
- The `input` precheck.
- The `automation` HELLO cap stays the hard ceiling on the screen rows.

## Categories and presets

The four preset ids on the wire and in the INI are `manual`, `workfolder`,
`plan` and `auto`. The display names below are not.

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

- **Plan first equals Ask first and is locked.**
  - ExitPlanMode is always Ask. That bar is the plan approval.
  - On approval the desk returns `PermissionResultAllow(updated_permissions=[setMode default])`, the only `updated_permissions` still forwarded, then restores the chat's previous mode.
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

### Policy
- `bridge/agent/policy.py` provides `Policy.verdict(cat)` and `sdk_mode()`, and replaces the `perm_mode` checks.
- `_PERMISSION_MODES` becomes `{default, plan}`.
- The presets are a Python table. A full-matrix agreement test checks it against the C++ table.
- **Conversation SETTINGS** (`conversations.py:102`) gain three keys: `mode_id`, `machine` and `grants`.
  - Grants are dropped if `machine` differs.
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
  - The system prompt says that instructions found inside the action are evidence for ASK.
- **The SAFE cache:**
  - It is per chat and bounded to 64 entries.
  - It is keyed on tool, args, rule and preview, plus a content hash for a script.
  - It is never used for `run_*`, `purge`, `files_sys`, `input` or `launch`.
  - It is cleared on any write in the chat, and on a mode, list or conversation change.
- **When unreachable**, it says so once (`_announce_gap`).
- **The bar shows "Checking…"** while the checker runs.

### "Allow for this chat"
- Forwarding of `updated_permissions` stops, apart from Plan's setMode.
- **Grants are narrow:**
  - `files_work`;
  - `run_work`;
  - `run_built`, keyed on the exact resolved path;
  - `screen_drive`, keyed on the target window's process, so typing into another window asks again.
- **There is no grant** for `files_else`, any `*_sys`, `purge`, `run_else`, or any doubt or DEVICE verdict.
- A grant resolves queued bars of the same category whose rule is not doubt. It is cleared when the mode changes, and it is stored in SETTINGS.

### Off and session rebuilds
- The Session records a tool-surface fingerprint: the filtered `tools=`, the machine tool set, and `caps.automation`/`ops`.
- When a mode change or an attach changes that fingerprint, the session rebuilds under `turn_lock` with `make_session(resume=old)` and no fork, the same path the effort change uses (`services/session.py:548-585`).
- Filtering happens inside `machine_tools.build` and in `TOOLS`.
- Mid-turn, Off is also a desk denial.

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

- **Tools:** compilers, script hosts, shells and the like.
  - It is a front end over the existing detection (`win/app/tooldet.cpp`):
    - `tools_detect.ini` is shipped and replaced on every install.
    - `tools.ini` holds the user's own entries. It is never overwritten and is read
      second, so it wins.
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

### Lists... (the transparency inspector)
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
  - `rc_log_open` appends to the existing `.log` with a banner.
  - After the mutex passes, `rc_log_begin_run(dir)` renames the old log to `.prv` and starts a new file.
  - The folder applies at the next start.
- **The command log is `<logdir>\CMDLOG\YYYYMMDD.CMD`.** The 8.3 rule applies.
  - Each chunk is written directly by a wrapper around `ExecOut` (`rpc.cpp:431`), on the worker only (`run_one` at `:575` and the abandon path at `:523`, never the reader's `cancel_job`). There is no tee and no flush.
  - Each entry is a header line, then the chunks, then a trailer.
  - The 1 MB/day cap is checked with `GetFileSize`.
  - A 30-day sweep runs once a day on the worker.
  - A log folder inside the sync root is refused.

## Worked examples
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

## Win98 property-sheet constraints

Things the Options sheet had to learn, worth knowing before touching it:

- Each page's dialog template is its own heap buffer, capped at 8 KB and
  owned by the sheet until `PropertySheetA` returns, because pages are
  created lazily.
- A combo's `cy` must include the dropdown height, about 60 DLU.
- Watcom has `PROPSHEETPAGEA_V1` and `PROPSHEETHEADERA_V1` (`prsht.h`), and
  those are the sizes to pass.
- `PSH_USECALLBACK`, and strip `DS_CONTEXTHELP` at `PSCB_PRECREATE`.
- Pages never call `EndDialog`. Validate in `PSN_KILLACTIVE`; save on an
  `applied` flag from `PSN_APPLY`, never on the return value.
- No multiline EDIT on a list page: Esc turns into `WM_CLOSE`.
- INI lists use **numbered keys** (`add01=`...), because the profile API
  cannot hold repeated keys.
- `applyConnectionConfig()` runs between `rc_net_stop` and `rc_net_start`;
  fields that need it are labelled "(reconnects)". If a turn is streaming,
  OK asks "Stop the current reply and reconnect?".

## Two standing constraints

**COMMAND.COM searches the cwd before PATH.** Anything the bridge wraps
around a guest command must name its helper by absolute path from the HELLO
inventory -- otherwise a `FIND.BAT` the agent plants in the work folder runs
on every wrapped exec, under every mode. This bit once, through the
exit-code wrapper's bare `find`.

**The app log cannot be turned off**, and the startup banner always stays.

## The design target

Auto produces zero bars for an ordinary build-and-run loop in the work
folder. Only the listed invariants, and a checker that is unsure, produce a
bar in Auto. That is the whole point of the feature.
