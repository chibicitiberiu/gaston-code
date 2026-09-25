/* hookdll.dll -- the smallest thing that answers "can we inject at all".
 *
 * 0.15b established that a system-wide WH_CBT whose hook proc lives in an
 * EXE installs and then delivers nothing -- Win98 hands back a handle and
 * never calls it. The documented reason is that a system-wide hook has to
 * run INSIDE each hooked process, so the code must live somewhere the
 * system can map there: a DLL. This is that DLL, and the question it
 * answers is not "does the hook fire" but "does this file get mapped into
 * other programs' address spaces, and can it talk back".
 *
 * Talking back is the interesting half. Once the system maps this into
 * Notepad, the copy running there shares no globals with the copy in
 * probe.exe -- on Win9x a DLL's data is per-process like anywhere else. So
 * the counters live in a NAMED file mapping, which every copy opens by
 * name and therefore sees the same page of. That is also the mechanism a
 * real implementation would use to get text back out of a process whose
 * messages are not marshalled, so proving it here proves more than the
 * hook.
 *
 * Three numbers come out of it:
 *
 *   attaches  how many processes loaded this DLL. If it is 1, only
 *             probe.exe has it and nothing was injected.
 *   calls     how many times the hook proc ran, anywhere.
 *   pids      how many DISTINCT processes ran it, which is the one that
 *             cannot be faked by our own process being busy.
 *
 * Nothing here may be NT-only. This file is mapped into programs that
 * did not ask for it, so an unresolvable import is not our crash, it is
 * theirs -- and on a machine somebody is using.
 */
#include <windows.h>

#define RC_SHM_NAME "GastonHookShm"
#define RC_MAXPIDS  16
#define RC_TEXTN    48
#define RC_TEXTLEN  56

/* One captured draw. Bounded and tiny on purpose: this structure is mapped
 * into every process the hook reaches, so its size is paid per process on
 * a machine with 28 KB measured free after a turn. A ring, so a chatty
 * application overwrites its own old entries instead of growing. */
typedef struct {
    DWORD pid;
    long  x, y;
    char  text[RC_TEXTLEN];
} RcDraw;

typedef struct {
    long   calls;
    long   attaches;
    long   pids_n;
    DWORD  pids[RC_MAXPIDS];
    /* Which process may have its import table rewritten. Zero means none,
     * and none is the default. The first version patched every process the
     * hook reached, which included Explorer, and Explorer died -- see
     * unpatch_text_calls for why it died AFTER we let go. Opt-in, one named
     * process at a time, set by the controller BEFORE the DLL is loaded. */
    DWORD  patch_pid;
    long   patched;        /* processes where the IAT patch took */
    long   draws;          /* total text draws seen, ever */
    long   ring_next;      /* next slot to write, wraps */
    RcDraw ring[RC_TEXTN];
} RcHookShm;

static HANDLE     g_map;
static RcHookShm *g_shm;

/* CreateFileMapping against INVALID_HANDLE_VALUE is the pagefile-backed,
 * name-addressed form: the first caller makes it, every later one in any
 * process opens the same object. No file on disk, nothing to clean up. */
static void shm_attach(void)
{
    if (g_shm) return;
    g_map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                               0, sizeof(RcHookShm), RC_SHM_NAME);
    if (g_map)
        g_shm = (RcHookShm *)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS,
                                           0, 0, 0);
}

/* ---- GDI text capture by import-table patch -------------------------
 *
 * The last thing messages cannot give us. An owner-drawn control answers
 * no WM_GETTEXT and has no MSAA value; what it does do is call
 * TextOutA or ExtTextOutA to paint itself. Inside the process, those calls
 * are ours to see.
 *
 * The patch is to the host module's IMPORT TABLE, not to GDI32 itself.
 * Patching the function body would affect every process sharing the DLL --
 * on 9x the system DLLs live in shared memory above 2GB, so that is the
 * whole machine, from inside an application that did not consent. Patching
 * this module's thunks changes only who this one module calls.
 *
 * Everything here runs on the host's painting path, in a process we do not
 * own, so it must be impossible for it to fail: no allocation, no CRT, no
 * blocking, bounds checks everywhere, and the original is always called.
 */
/* Open Watcom's winnt.h does not define it for a 32-bit target; it is the
 * top bit of the thunk word, which is what "imported by ordinal, not by
 * name" is encoded as. */
#ifndef IMAGE_ORDINAL_FLAG
#define IMAGE_ORDINAL_FLAG 0x80000000
#endif

typedef BOOL (WINAPI *PFN_TEXTOUTA)(HDC, int, int, LPCSTR, int);
typedef BOOL (WINAPI *PFN_EXTTEXTOUTA)(HDC, int, int, UINT, CONST RECT *,
                                       LPCSTR, UINT, CONST INT *);

static PFN_TEXTOUTA    g_real_textout;
static PFN_EXTTEXTOUTA g_real_exttextout;

static void record(int x, int y, LPCSTR s, int n)
{
    RcDraw *d;
    long slot;
    int i;
    if (!g_shm || !s || n <= 0) return;
    g_shm->draws++;
    slot = g_shm->ring_next;
    if (slot < 0 || slot >= RC_TEXTN) slot = 0;
    d = &g_shm->ring[slot];
    d->pid = GetCurrentProcessId();
    d->x = x; d->y = y;
    if (n > RC_TEXTLEN - 1) n = RC_TEXTLEN - 1;
    for (i = 0; i < n; i++) {
        char c = s[i];
        /* Control bytes would wreck a line-based log on the other side,
         * and a NUL mid-string would truncate the record silently. */
        d->text[i] = (c >= 32 || c == 0) ? c : '.';
    }
    d->text[n] = '\0';
    g_shm->ring_next = (slot + 1) % RC_TEXTN;
}

static BOOL WINAPI my_textout(HDC dc, int x, int y, LPCSTR s, int n)
{
    record(x, y, s, n);
    return g_real_textout ? g_real_textout(dc, x, y, s, n)
                          : FALSE;
}

static BOOL WINAPI my_exttextout(HDC dc, int x, int y, UINT opt,
                                 CONST RECT *r, LPCSTR s, UINT n,
                                 CONST INT *dx)
{
    record(x, y, s, (int)n);
    return g_real_exttextout ? g_real_exttextout(dc, x, y, opt, r, s, n, dx)
                             : FALSE;
}

/* Swap one imported function pointer in `mod`'s thunk table. Returns the
 * original, or NULL if this module does not import that name. */
static DWORD *patch_import(HMODULE mod, const char *dll, const char *fn,
                           void *replacement, void **old_out)
{
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)mod;
    IMAGE_NT_HEADERS *nt;
    IMAGE_IMPORT_DESCRIPTOR *imp;
    DWORD rva;
    char *base = (char *)mod;

    if (!mod || IsBadReadPtr(dos, sizeof(*dos))) return NULL;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return NULL;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (IsBadReadPtr(nt, sizeof(*nt))) return NULL;
    if (nt->Signature != IMAGE_NT_SIGNATURE) return NULL;
    rva = nt->OptionalHeader
            .DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress;
    if (!rva) return NULL;
    imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + rva);

    for (; !IsBadReadPtr(imp, sizeof(*imp)) && imp->Name; imp++) {
        const char *name = base + imp->Name;
        IMAGE_THUNK_DATA *orig, *now;
        int i;
        if (IsBadStringPtrA(name, 64)) continue;
        if (lstrcmpiA(name, dll)) continue;
        /* OriginalFirstThunk carries the NAMES, FirstThunk the live
         * pointers. A module built without the former is imported by
         * ordinal only and there is nothing here to match by name. */
        if (!imp->OriginalFirstThunk) continue;
        orig = (IMAGE_THUNK_DATA *)(base + imp->OriginalFirstThunk);
        now  = (IMAGE_THUNK_DATA *)(base + imp->FirstThunk);
        for (i = 0; !IsBadReadPtr(orig + i, sizeof(*orig))
                    && orig[i].u1.AddressOfData; i++) {
            IMAGE_IMPORT_BY_NAME *nm;
            if (orig[i].u1.Ordinal & IMAGE_ORDINAL_FLAG) continue;
            nm = (IMAGE_IMPORT_BY_NAME *)(base + orig[i].u1.AddressOfData);
            if (IsBadReadPtr(nm, sizeof(*nm))) continue;
            if (lstrcmpA((const char *)nm->Name, fn)) continue;
            {
                DWORD prot = 0;
                if (old_out) *old_out = (void *)now[i].u1.Function;
                if (!VirtualProtect(&now[i], sizeof(now[i]),
                                    PAGE_READWRITE, &prot))
                    return NULL;
                now[i].u1.Function = (DWORD)replacement;
                VirtualProtect(&now[i], sizeof(now[i]), prot, &prot);
                return (DWORD *)&now[i].u1.Function;
            }
        }
    }
    return NULL;
}

/* The slots we rewrote, so they can be put back. */
static DWORD *g_slot_textout;
static DWORD *g_slot_exttextout;

static void patch_text_calls(void)
{
    /* NULL means "the module that started this process" -- the EXE. Its
     * own painting is what we want; a standard control paints from inside
     * USER32 and never touches the application's import table at all,
     * which is why this sees owner-drawn text and nothing else. */
    HMODULE host = GetModuleHandleA(NULL);
    void *a = NULL, *b = NULL;
    g_slot_textout = patch_import(host, "GDI32.DLL", "TextOutA",
                                  (void *)my_textout, &a);
    g_slot_exttextout = patch_import(host, "GDI32.DLL", "ExtTextOutA",
                                     (void *)my_exttextout, &b);
    if (g_slot_textout)    g_real_textout = (PFN_TEXTOUTA)a;
    if (g_slot_exttextout) g_real_exttextout = (PFN_EXTTEXTOUTA)b;
    if ((g_slot_textout || g_slot_exttextout) && g_shm) g_shm->patched++;
}

/* PUT IT BACK, and this is not tidiness.
 *
 * A patched import table points at code inside THIS DLL. When the hook is
 * removed the system unmaps us from every process it had injected us into,
 * and any table still pointing here now points at nothing. The host does
 * not crash then; it crashes the next time it draws a character, long
 * after we are gone, and the crash belongs to it. That is exactly what
 * happened to Explorer on 2026-09-26.
 */
static void unpatch_text_calls(void)
{
    DWORD prot = 0;
    if (g_slot_textout && g_real_textout) {
        if (VirtualProtect(g_slot_textout, sizeof(DWORD), PAGE_READWRITE,
                           &prot)) {
            *g_slot_textout = (DWORD)g_real_textout;
            VirtualProtect(g_slot_textout, sizeof(DWORD), prot, &prot);
        }
        g_slot_textout = NULL;
    }
    if (g_slot_exttextout && g_real_exttextout) {
        if (VirtualProtect(g_slot_exttextout, sizeof(DWORD), PAGE_READWRITE,
                           &prot)) {
            *g_slot_exttextout = (DWORD)g_real_exttextout;
            VirtualProtect(g_slot_exttextout, sizeof(DWORD), prot, &prot);
        }
        g_slot_exttextout = NULL;
    }
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)inst;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        shm_attach();
        /* Counted even before any hook fires: a process that maps this and
         * never calls the proc still proves the mapping happened. */
        if (g_shm) g_shm->attaches++;
        /* Only the one process the controller named, and only if it named
         * one. Everything else gets the hook and keeps its import table. */
        if (g_shm && g_shm->patch_pid
            && g_shm->patch_pid == GetCurrentProcessId())
            patch_text_calls();
    } else if (reason == DLL_PROCESS_DETACH) {
        unpatch_text_calls();
    }
    return TRUE;
}

__declspec(dllexport) LRESULT CALLBACK rc_hook_proc(int code, WPARAM wp,
                                                    LPARAM lp)
{
    if (code >= 0) {
        /* DllMain may not have run in this process yet in every ordering,
         * so attach lazily too rather than assuming. */
        shm_attach();
        if (g_shm) {
            DWORD pid = GetCurrentProcessId();
            long i;
            g_shm->calls++;
            for (i = 0; i < g_shm->pids_n && i < RC_MAXPIDS; i++)
                if (g_shm->pids[i] == pid)
                    break;
            if (i == g_shm->pids_n && g_shm->pids_n < RC_MAXPIDS)
                g_shm->pids[g_shm->pids_n++] = pid;
        }
    }
    /* Pass it on. A hook that swallows CBT notifications in every process
     * on the machine would be a memorable way to break a desktop. */
    return CallNextHookEx(0, code, wp, lp);
}
