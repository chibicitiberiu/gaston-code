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
#include "inproc/shm.h"

/* The layout, the name and the version all live in inproc/shm.h, which the
 * controller and the probe include too. There used to be a second copy of
 * the struct in probe.c, which is a layout that can drift while both sides
 * still compile. */
static HANDLE   g_map;
static RcInShm *g_shm;
static HANDLE   g_req;    /* the controller has asked */
static HANDLE   g_done;   /* the collector has answered */

/* collect.c, linked into this DLL. Declared rather than included so this
 * file stays the one that knows about hooks and that one stays the one
 * that knows about controls. */
void rc_inproc_collect(RcInShm *shm);
static void (*g_collect)(RcInShm *) = rc_inproc_collect;

/* CreateFileMapping against INVALID_HANDLE_VALUE is the pagefile-backed,
 * name-addressed form: the first caller makes it, every later one in any
 * process opens the same object. No file on disk, nothing to clean up. */
static void shm_attach(void)
{
    RcInShm *v;
    if (g_shm) return;
    g_map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                               0, sizeof(RcInShm), RC_SHM_NAME);
    if (!g_map)
        return;
    v = (RcInShm *)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!v)
        return;
    /* REFUSE rather than guess, the rule caps.py already follows for
     * HELLO. A section we do not recognise is one somebody else is
     * writing, and the field we would read out of it decides whose import
     * table gets rewritten. The controller stamps these; a DLL that finds
     * them unset was loaded into a run that has not started yet.
     *
     * The name carries the version too, so this should be unreachable --
     * it is here because "should be unreachable" is exactly the check
     * worth keeping when the cost of being wrong is somebody's shell. */
    if (v->magic != RC_SHM_MAGIC || v->version != RC_SHM_VER
        || v->cb != sizeof(RcInShm)) {
        UnmapViewOfFile(v);
        CloseHandle(g_map);
        g_map = NULL;
        return;
    }
    g_shm = v;

    /* Opened, never created, by the DLL side: the controller owns them, so
     * a DLL that finds them missing has been loaded into a run that is not
     * listening and simply does not signal. */
    if (!g_req)
        g_req = OpenEventA(EVENT_ALL_ACCESS, FALSE, RC_SHM_REQ_EVENT);
    if (!g_done)
        g_done = OpenEventA(EVENT_ALL_ACCESS, FALSE, RC_SHM_DONE_EVENT);
}

/* The controller's side of the same thing: stamp the header and zero the
 * counters, so a run measures ITSELF.
 *
 * Nothing ever zeroed these and no injected copy ever unmaps the section,
 * so `attaches` was cumulative across every run since boot -- and the
 * verdict "INJECTION WORKS" tested `attaches > 1`. It could be satisfied
 * by residue from a run that injected nothing. */
void rc_hook_shm_begin(void)
{
    if (!g_shm) {
        if (g_map) return;
        g_map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                                   0, sizeof(RcInShm), RC_SHM_NAME);
        if (!g_map) return;
        g_shm = (RcInShm *)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS,
                                           0, 0, 0);
        if (!g_shm) return;
    }
    g_shm->magic = RC_SHM_MAGIC;
    g_shm->version = RC_SHM_VER;
    g_shm->cb = sizeof(RcInShm);
    g_shm->generation = GetTickCount() | 1;
    g_shm->calls = 0;
    g_shm->attaches = 0;
    g_shm->pids_n = 0;
    g_shm->patched = 0;
    g_shm->draws = 0;
    g_shm->ring_next = 0;
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
typedef BOOL (WINAPI *PFN_TEXTOUTW)(HDC, int, int, LPCWSTR, int);
typedef BOOL (WINAPI *PFN_EXTTEXTOUTW)(HDC, int, int, UINT, CONST RECT *,
                                       LPCWSTR, UINT, CONST INT *);

static PFN_TEXTOUTA    g_real_textout;
static PFN_EXTTEXTOUTA g_real_exttextout;
/* THE W FORMS ARE NOT OPTIONAL, and assuming they were is what gate 0.15g
 * was written to check. It reports COMCTL32 importing ExtTextOutW and
 * TextOutW and NEITHER of the A forms -- so a patch that covered only A
 * would have captured nothing from the common controls, which is most of
 * what a dialog is made of. 9x implements the W entry points as thin
 * wrappers, but what a module IMPORTS is what its thunk points at, and the
 * thunk is what we rewrite. */
static PFN_TEXTOUTW    g_real_textoutw;
static PFN_EXTTEXTOUTW g_real_exttextoutw;

/* AND DrawText, which is USER32 and not GDI32.
 *
 * Gate 0.15g, run on a real Win98 rather than under Wine: MSHTML.DLL
 * imports DrawTextW and DrawTextA, COMCTL32 imports DrawTextExA, and
 * COMDLG32 imports DrawTextA. MSHTML is the module D4 exists for and a
 * dialog is mostly common controls, so leaving these out would have meant
 * capturing part of a web page and part of a dialog and calling it the
 * text on screen -- which is worse than capturing nothing, because it
 * looks like an answer.
 *
 * `n` may be -1 here, meaning the string is NUL-terminated. The position
 * is the rectangle's top-left, which is where the first line starts for
 * every alignment that is not centred or right -- approximate, and the
 * alternative is not recording it at all. */
typedef int (WINAPI *PFN_DRAWTEXTA)(HDC, LPCSTR, int, LPRECT, UINT);
typedef int (WINAPI *PFN_DRAWTEXTW)(HDC, LPCWSTR, int, LPRECT, UINT);
typedef int (WINAPI *PFN_DRAWTEXTEXA)(HDC, LPSTR, int, LPRECT, UINT, void *);
typedef int (WINAPI *PFN_DRAWTEXTEXW)(HDC, LPWSTR, int, LPRECT, UINT, void *);

static PFN_DRAWTEXTA   g_real_drawtext;
static PFN_DRAWTEXTW   g_real_drawtextw;
static PFN_DRAWTEXTEXA g_real_drawtextexa;
static PFN_DRAWTEXTEXW g_real_drawtextexw;

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
        /* Control bytes would wreck a line-based log on the other side,
         * and a NUL mid-string would truncate the record silently. */
        /* UNSIGNED. With a signed char every byte from 0x80 up is
         * negative and was mapped to '.', which destroys exactly the
         * accented and box-drawing characters a Win98 application draws --
         * and REMOTE.md is careful elsewhere that guest bytes cross
         * untouched for the bridge to decode with HELLO's codepage. */
        unsigned char c = (unsigned char)s[i];
        d->text[i] = (c >= 32) ? (char)c : '.';
    }
    d->text[n] = '\0';
    g_shm->ring_next = (slot + 1) % RC_TEXTN;
}

/* A wide run, narrowed into a bounded stack buffer before it is recorded.
 *
 * WideCharToMultiByte and not a hand-rolled fold: this runs inside somebody
 * else's paint and the codepage is theirs, not ours. Bounded because the
 * caller's `n` is a character count and nothing stops it being enormous. */
static void record_w(int x, int y, LPCWSTR w, int n)
{
    char narrow[RC_TEXTLEN * 2];
    int got;
    if (!w || n <= 0)
        return;
    if (n > (int)sizeof(narrow) - 1)
        n = (int)sizeof(narrow) - 1;
    got = WideCharToMultiByte(CP_ACP, 0, w, n, narrow,
                              (int)sizeof(narrow) - 1, NULL, NULL);
    if (got <= 0)
        return;
    narrow[got] = '\0';
    record(x, y, narrow, got);
}

static BOOL WINAPI my_textoutw(HDC dc, int x, int y, LPCWSTR s, int n)
{
    record_w(x, y, s, n);
    return g_real_textoutw ? g_real_textoutw(dc, x, y, s, n) : FALSE;
}

static BOOL WINAPI my_exttextoutw(HDC dc, int x, int y, UINT opt,
                                  CONST RECT *r, LPCWSTR s, UINT n,
                                  CONST INT *dx)
{
    record_w(x, y, s, (int)n);
    return g_real_exttextoutw
        ? g_real_exttextoutw(dc, x, y, opt, r, s, n, dx) : FALSE;
}

/* A count of -1 means "until the NUL", and the caller's buffer is theirs.
 * Bounded either way: we copy at most what the ring slot holds. */
static int draw_len(const char *s, int n)
{
    int i;
    if (n >= 0)
        return n;
    for (i = 0; i < RC_TEXTLEN * 2 && s[i]; i++)
        ;
    return i;
}

static int draw_lenw(LPCWSTR w, int n)
{
    int i;
    if (n >= 0)
        return n;
    for (i = 0; i < RC_TEXTLEN * 2 && w[i]; i++)
        ;
    return i;
}

static int WINAPI my_drawtext(HDC dc, LPCSTR s, int n, LPRECT r, UINT f)
{
    if (r && s)
        record(r->left, r->top, s, draw_len(s, n));
    return g_real_drawtext ? g_real_drawtext(dc, s, n, r, f) : 0;
}

static int WINAPI my_drawtextw(HDC dc, LPCWSTR s, int n, LPRECT r, UINT f)
{
    if (r && s)
        record_w(r->left, r->top, s, draw_lenw(s, n));
    return g_real_drawtextw ? g_real_drawtextw(dc, s, n, r, f) : 0;
}

static int WINAPI my_drawtextexa(HDC dc, LPSTR s, int n, LPRECT r, UINT f,
                                 void *p)
{
    if (r && s)
        record(r->left, r->top, s, draw_len(s, n));
    return g_real_drawtextexa ? g_real_drawtextexa(dc, s, n, r, f, p) : 0;
}

static int WINAPI my_drawtextexw(HDC dc, LPWSTR s, int n, LPRECT r, UINT f,
                                 void *p)
{
    if (r && s)
        record_w(r->left, r->top, s, draw_lenw(s, n));
    return g_real_drawtextexw ? g_real_drawtextexw(dc, s, n, r, f, p) : 0;
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
                /* ALREADY OURS. Without this, D4's per-module loop
                 * patches module 2 and reads back `my_textout` as the
                 * "original", so the next character drawn recurses into
                 * itself forever -- inside somebody else's WM_PAINT. That
                 * is a hang of their application, not a crash we could
                 * even find. */
                if (now[i].u1.Function == (DWORD)replacement)
                    return NULL;
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

/* Every slot we rewrote, per module, so every one can be put back.
 *
 * A single pair of pointers was enough while only the EXE was patched. It
 * is not enough for what this is for: Notepad's text is drawn by the EDIT
 * control inside USER32 and a web page by MSHTML.DLL, so patching the
 * executable's imports misses precisely the content wanted. Patching N
 * modules with one slot restores one of them and leaves the other N-1
 * pointing into a DLL that has been unmapped -- which is the Explorer
 * crash again, multiplied.
 */
#define RC_PATCH_MAX 32

typedef struct {
    DWORD *slot;
    void  *original;
} RcPatch;

static RcPatch g_patches[RC_PATCH_MAX];
static int     g_patch_n;

static void patch_one(HMODULE mod, const char *from, const char *fn,
                      void *replacement, void **realOut)
{
    void *orig = NULL;
    DWORD *slot;
    if (g_patch_n >= RC_PATCH_MAX)
        return;
    slot = patch_import(mod, from, fn, replacement, &orig);
    if (!slot || !orig || orig == replacement)
        return;
    g_patches[g_patch_n].slot = slot;
    g_patches[g_patch_n].original = orig;
    g_patch_n++;
    /* Every module imports the same GDI32 entry point, so the first real
     * one we see is the real one for all of them. Never overwritten, or a
     * later module's thunk could become the "original" we call through. */
    if (!*realOut)
        *realOut = orig;
}

/* Module enumeration, resolved rather than imported. TOOLHELP32 is in
 * KERNEL32 on 9x; a static import of something absent kills the image at
 * load, and this image is mapped into programs that did not ask for it. */
#ifndef TH32CS_SNAPMODULE
#define TH32CS_SNAPMODULE 0x00000008
#endif

typedef struct {
    DWORD dwSize;
    DWORD th32ModuleID;
    DWORD th32ProcessID;
    DWORD GlblcntUsage;
    DWORD ProccntUsage;
    BYTE *modBaseAddr;
    DWORD modBaseSize;
    HMODULE hModule;
    char szModule[256];
    char szExePath[MAX_PATH];
} RC_MODULEENTRY32;

typedef HANDLE (WINAPI *PFN_SNAP)(DWORD, DWORD);
typedef BOOL (WINAPI *PFN_MOD32)(HANDLE, RC_MODULEENTRY32 *);

static void patch_text_calls(void)
{
    HMODULE k32 = GetModuleHandleA("kernel32.dll");
    PFN_SNAP snap = k32
        ? (PFN_SNAP)GetProcAddress(k32, "CreateToolhelp32Snapshot") : 0;
    PFN_MOD32 first = k32
        ? (PFN_MOD32)GetProcAddress(k32, "Module32First") : 0;
    PFN_MOD32 next = k32
        ? (PFN_MOD32)GetProcAddress(k32, "Module32Next") : 0;
    void *a = NULL, *b = NULL, *c = NULL, *d = NULL;
    void *e = NULL, *g = NULL, *i2 = NULL, *j = NULL;
    HMODULE self = NULL;

    /* Never ourselves: patching our own imports would route our own
     * pass-through call straight back into the hook. */
    self = GetModuleHandleA("hookdll.dll");

    if (snap && first && next) {
        HANDLE h = snap(TH32CS_SNAPMODULE, GetCurrentProcessId());
        if (h != INVALID_HANDLE_VALUE) {
            RC_MODULEENTRY32 me;
            BOOL ok;
            me.dwSize = sizeof(me);
            for (ok = first(h, &me); ok; ok = next(h, &me)) {
                if (me.hModule == self)
                    continue;
                patch_one(me.hModule, "GDI32.DLL", "TextOutA",
                          (void *)my_textout, &a);
                patch_one(me.hModule, "GDI32.DLL", "ExtTextOutA",
                          (void *)my_exttextout, &b);
                patch_one(me.hModule, "GDI32.DLL", "TextOutW",
                          (void *)my_textoutw, &c);
                patch_one(me.hModule, "GDI32.DLL", "ExtTextOutW",
                          (void *)my_exttextoutw, &d);
                patch_one(me.hModule, "USER32.DLL", "DrawTextA",
                          (void *)my_drawtext, &e);
                patch_one(me.hModule, "USER32.DLL", "DrawTextW",
                          (void *)my_drawtextw, &g);
                patch_one(me.hModule, "USER32.DLL", "DrawTextExA",
                          (void *)my_drawtextexa, &i2);
                patch_one(me.hModule, "USER32.DLL", "DrawTextExW",
                          (void *)my_drawtextexw, &j);
            }
            CloseHandle(h);
        }
    }

    /* No toolhelp, or it gave nothing: the executable alone, which is what
     * this did before and is better than nothing. */
    if (!g_patch_n) {
        HMODULE host = GetModuleHandleA(NULL);
        patch_one(host, "GDI32.DLL", "TextOutA", (void *)my_textout, &a);
        patch_one(host, "GDI32.DLL", "ExtTextOutA", (void *)my_exttextout, &b);
        patch_one(host, "GDI32.DLL", "TextOutW", (void *)my_textoutw, &c);
        patch_one(host, "GDI32.DLL", "ExtTextOutW", (void *)my_exttextoutw, &d);
        patch_one(host, "USER32.DLL", "DrawTextA", (void *)my_drawtext, &e);
        patch_one(host, "USER32.DLL", "DrawTextW", (void *)my_drawtextw, &g);
        patch_one(host, "USER32.DLL", "DrawTextExA",
                  (void *)my_drawtextexa, &i2);
        patch_one(host, "USER32.DLL", "DrawTextExW",
                  (void *)my_drawtextexw, &j);
    }

    if (a) g_real_textout = (PFN_TEXTOUTA)a;
    if (b) g_real_exttextout = (PFN_EXTTEXTOUTA)b;
    if (c) g_real_textoutw = (PFN_TEXTOUTW)c;
    if (d) g_real_exttextoutw = (PFN_EXTTEXTOUTW)d;
    if (e) g_real_drawtext = (PFN_DRAWTEXTA)e;
    if (g) g_real_drawtextw = (PFN_DRAWTEXTW)g;
    if (i2) g_real_drawtextexa = (PFN_DRAWTEXTEXA)i2;
    if (j) g_real_drawtextexw = (PFN_DRAWTEXTEXW)j;
    /* Only once both pass-throughs are known. Between the first patched
     * slot and this assignment, a draw would find g_real_* NULL and the
     * character would simply not appear on somebody's screen. */
    if (g_patch_n && g_shm) g_shm->patched++;
}

/* PUT THEM ALL BACK, and this is not tidiness.
 *
 * A patched import table points at code inside THIS DLL. When the hook is
 * removed the system unmaps us from every process it had injected us into,
 * and any table still pointing here now points at nothing. The host does
 * not crash then; it crashes the next time it draws a character, long
 * after we are gone, and the crash belongs to it. That is exactly what
 * happened to Explorer on 2026-09-26.
 *
 * In reverse, so a module patched twice -- which the guard above should
 * prevent and which this does not rely on -- unwinds to where it started.
 */
static void unpatch_text_calls(void)
{
    DWORD prot = 0;
    while (g_patch_n > 0) {
        RcPatch *p = &g_patches[--g_patch_n];
        if (!p->slot || !p->original)
            continue;
        if (VirtualProtect(p->slot, sizeof(DWORD), PAGE_READWRITE, &prot)) {
            *p->slot = (DWORD)p->original;
            VirtualProtect(p->slot, sizeof(DWORD), prot, &prot);
        }
        p->slot = NULL;
        p->original = NULL;
    }
}

/* Is this process the one the controller named?
 *
 * The pid alone is not enough. On 9x a pid is an obfuscated pointer into
 * the process database and it IS reused: the controller sets patch_pid for
 * Notepad, Notepad exits, the next process to start can be handed the same
 * slot, and the deny list never sees it. So the basename has to agree too.
 */
static int is_the_named_target(void)
{
    char path[MAX_PATH];
    const char *base;
    int i;

    if (!g_shm || !g_shm->patch_pid)
        return 0;
    if (g_shm->patch_pid != GetCurrentProcessId())
        return 0;
    if (!g_shm->patch_exe[0])
        return 0;       /* unnamed is not a licence; it is a refusal */

    path[0] = '\0';
    if (!GetModuleFileNameA(NULL, path, sizeof(path)))
        return 0;
    base = path;
    for (i = 0; path[i]; i++)
        if (path[i] == '\\' || path[i] == '/' || path[i] == ':')
            base = &path[i + 1];

    for (i = 0; i < 31; i++) {
        char a = base[i], b = g_shm->patch_exe[i];
        if (a >= 'a' && a <= 'z') a = (char)(a - 'a' + 'A');
        if (b >= 'a' && b <= 'z') b = (char)(b - 'a' + 'A');
        if (a != b) return 0;
        if (!a) return 1;
    }
    return 1;
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
        if (is_the_named_target())
            patch_text_calls();
    } else if (reason == DLL_PROCESS_DETACH) {
        unpatch_text_calls();
    }
    return TRUE;
}

/* Serve one request, if one is waiting for us.
 *
 * Called from the hook proc, which means it runs on the TARGET's thread
 * inside their message dispatch -- so it must be brief, must never block
 * without a timeout, and must do nothing at all in the common case where
 * there is no request. The common case is the first four lines.
 *
 * Only the named process serves. Every other process the hook reaches
 * reads the request, sees that it is not for them, and does nothing --
 * which is also why `seq` is checked before `op`: two processes must not
 * both answer. */
static void serve_request(void)
{
    if (!g_shm || !g_collect)
        return;
    if (g_shm->op == RC_OP_NONE || g_shm->ackSeq == g_shm->seq)
        return;
    if (!is_the_named_target())
        return;

    if (g_shm->op == RC_OP_PING) {
        g_shm->nodeN = 0;
        g_shm->itemN = 0;
        g_shm->elapsedMs = 0;
        g_shm->result = RC_RES_OK;
    } else if (g_shm->op == RC_OP_COLLECT) {
        g_collect(g_shm);
    } else {
        g_shm->result = RC_RES_REFUSED;
    }

    /* ackSeq LAST, after the result and the counts are in place. The
     * controller waits on the event but also polls this, and a reader that
     * saw the ack before the data would read the previous answer. */
    g_shm->ackSeq = g_shm->seq;
    if (g_done)
        SetEvent(g_done);
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
        /* The hook is how a request reaches this process at all: it is the
         * only code of ours that runs on the target's own thread. Cheap in
         * the common case -- four comparisons and out. */
        serve_request();
    }
    /* Pass it on. A hook that swallows CBT notifications in every process
     * on the machine would be a memorable way to break a desktop. */
    return CallNextHookEx(0, code, wp, lp);
}
