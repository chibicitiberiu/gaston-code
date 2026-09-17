/* probe.exe -- the Phase 0 guest experiments, in one console program.
 *
 * Everything here is a question that cannot be answered from Linux, Wine, or
 * documentation. Each one either confirms a plan assumption or invalidates it,
 * and two of them can reshape Phase 3.
 *
 * Writes PROBE.LOG beside itself as well as to stdout, because on this
 * platform "no log file was created" is itself the primary diagnostic: an
 * NT-only static import kills the process at load, before main(), leaving
 * nothing else behind.
 *
 *   probe            run everything
 *   probe threads    run one section
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <objbase.h>
#include "miniz.h"

static FILE *g_log;

static void say(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
    va_end(ap);
    buf[sizeof(buf) - 1] = '\0';
    fputs(buf, stdout);
    fputs("\n", stdout);
    fflush(stdout);
    if (g_log) { fputs(buf, g_log); fputs("\n", g_log); fflush(g_log); }
}

static void head(const char *t) { say(""); say("=== %s ===", t); }

/* ------------------------------------------------------------------ 0.7 */
/* The binary running at all proves the Open Watcom CRT's GetCommandLineW and
 * GetModuleFileNameW imports resolved. They are stubs on Win9x, but they must
 * at least EXIST as exports or the loader refuses the image before main().
 * This was the last unverified assumption in the whole toolchain. */
static void probe_platform(void)
{
    DWORD v = GetVersion();
    DWORD major = (DWORD)(LOBYTE(LOWORD(v)));
    DWORD minor = (DWORD)(HIBYTE(LOWORD(v)));
    MEMORYSTATUS ms;
    OSVERSIONINFOA osv;

    head("0.7  platform / W-suffix imports");
    say("PASS: the image loaded, so GetCommandLineW and GetModuleFileNameW");
    say("      resolved against this kernel32.");
    say("GetVersion      : %08lX  (%lu.%lu)", v, major, minor);
    say("family          : %s", (v & 0x80000000) ? "Win9x" : "NT");

    memset(&osv, 0, sizeof(osv));
    osv.dwOSVersionInfoSize = sizeof(osv);
    if (GetVersionExA(&osv))
        say("build           : %lu.%lu.%lu  '%s'",
            osv.dwMajorVersion, osv.dwMinorVersion,
            osv.dwBuildNumber & 0xFFFF, osv.szCSDVersion);

    memset(&ms, 0, sizeof(ms));
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatus(&ms);
    say("physical memory : %lu KB total, %lu KB free",
        ms.dwTotalPhys / 1024, ms.dwAvailPhys / 1024);
    say("page file       : %lu KB free", ms.dwAvailPageFile / 1024);

    /* GetFreeSystemResources is a 16-bit USER.EXE export; a Win32 process
     * reaches it through rsrc32.dll, not user32. Worth knowing because the
     * 16-bit USER/GDI heaps are a fixed 64KB that does not scale with RAM. */
    {
        HMODULE h = LoadLibraryA("rsrc32.dll");
        if (h) {
            typedef UINT (WINAPI *PFN)(UINT);
            PFN f = (PFN)GetProcAddress(h, "_MyGetFreeSystemResources32@4");
            if (f) {
                say("free resources  : system %u%%, GDI %u%%, USER %u%%",
                    f(0), f(1), f(2));
            } else {
                say("free resources  : rsrc32.dll present, entry point not found");
            }
            FreeLibrary(h);
        } else {
            say("free resources  : rsrc32.dll not present (NT?)");
        }
    }
}

/* ------------------------------------------------------------------ 0.11 */
/* Two claims to settle:
 *   - CreateThread(..., lpThreadId=NULL) is rejected on Win95/98 with
 *     ERROR_INVALID_PARAMETER while NT accepts it.
 *   - Open Watcom's multi-threaded CRT allocates its per-thread block in
 *     _beginthreadex, so a raw CreateThread thread never gets one.
 * Both are load-bearing for the reader/RPC threads. */
static volatile LONG g_ticks;
static HWND g_sink;

static unsigned __stdcall worker_beginthreadex(void *arg)
{
    int i;
    (void)arg;
    for (i = 0; i < 20; i++) {
        /* Touch CRT per-thread state deliberately: errno and strtok are the
         * two that bite when the single-threaded CRT is linked. */
        char buf[64];
        char *tok;
        errno = 0;
        strcpy(buf, "a,b,c");
        tok = strtok(buf, ",");
        while (tok) tok = strtok(NULL, ",");
        _snprintf(buf, sizeof(buf), "%d", i);
        InterlockedIncrement(&g_ticks);
        Sleep(10);
    }
    return 0;
}

static DWORD WINAPI worker_createthread(LPVOID arg)
{
    (void)arg;
    InterlockedIncrement(&g_ticks);
    return 0;
}

static LRESULT CALLBACK sink_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_APP) { InterlockedIncrement(&g_ticks); return 0; }
    return DefWindowProcA(h, m, w, l);
}

static void probe_threads(void)
{
    WNDCLASSA wc;
    HANDLE th[3];
    unsigned tid;
    DWORD dwtid;
    MSG msg;
    int i, n = 0;
    HANDLE bad;

    head("0.11  threads, with a message loop");

    /* (a) CreateThread with a NULL thread-id pointer. */
    SetLastError(0);
    bad = CreateThread(NULL, 0, worker_createthread, NULL, 0, NULL);
    if (bad) {
        say("CreateThread(lpThreadId=NULL) : ACCEPTED  (NT behaviour)");
        WaitForSingleObject(bad, 2000);
        CloseHandle(bad);
    } else {
        say("CreateThread(lpThreadId=NULL) : REJECTED, err=%lu  (Win9x)",
            GetLastError());
        say("   -> always pass a real thread-id pointer. This silently");
        say("      disabled every helper thread in retro-agent for years.");
    }

    /* (b) CreateThread WITH a thread-id pointer, for contrast. */
    SetLastError(0);
    bad = CreateThread(NULL, 0, worker_createthread, NULL, 0, &dwtid);
    if (bad) {
        say("CreateThread(&tid)            : accepted");
        WaitForSingleObject(bad, 2000);
        CloseHandle(bad);
    } else {
        say("CreateThread(&tid)            : REJECTED, err=%lu", GetLastError());
    }

    /* (c) Three _beginthreadex threads hammering CRT per-thread state while a
     *     message loop runs, which is the shape the real client uses. */
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = sink_proc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "ProbeSink";
    RegisterClassA(&wc);
    g_sink = CreateWindowExA(0, "ProbeSink", "", 0, 0, 0, 0, 0,
                             NULL, NULL, wc.hInstance, NULL);
    say("message sink window           : %s", g_sink ? "created" : "FAILED");

    g_ticks = 0;
    for (i = 0; i < 3; i++) {
        th[i] = (HANDLE)_beginthreadex(NULL, 0, worker_beginthreadex,
                                       NULL, 0, &tid);
        if (th[i]) n++;
    }
    say("_beginthreadex threads started: %d of 3", n);

    {
        DWORD deadline = GetTickCount() + 5000;
        while (GetTickCount() < deadline) {
            while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessageA(&msg);
            }
            if (g_sink) PostMessageA(g_sink, WM_APP, 0, 0);
            Sleep(20);
        }
    }
    for (i = 0; i < n; i++) {
        WaitForSingleObject(th[i], 3000);
        CloseHandle(th[i]);
    }
    say("ticks (threads + PostMessage)  : %ld", g_ticks);
    say(g_ticks > 50
        ? "PASS: threads and a message loop coexist"
        : "FAIL: too few ticks -- threads or the message loop stalled");
}

/* ------------------------------------------------------------------- 0.9 */
/* The one that can reshape Phase 3.
 *
 * retro-agent's exec.c was assumed to "handle" 16-bit console tools. It does
 * not: the PeekNamedPipe loop stops a hung child blocking the agent, but it
 * does not make capture work. And on Win9x EVERY command goes through
 * COMMAND.COM, which is itself 16-bit.
 *
 * The assertion that matters is NOT "it did not hang". It is "the output is
 * non-empty and correct". A broken capture passes a no-hang test with empty
 * output after a 60 second wait. */
static int run_capture(const char *cmdline, char *out, unsigned outsz,
                       DWORD *ms, int *timed_out)
{
    SECURITY_ATTRIBUTES sa;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    HANDLE rd = NULL, wr = NULL;
    DWORD start = GetTickCount();
    unsigned got = 0;
    char cmd[512];

    *timed_out = 0;
    out[0] = '\0';

    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    if (!CreatePipe(&rd, &wr, &sa, 16384)) return -1;

    /* The read end must not be inherited by the child, or the pipe never
     * reports EOF. SetHandleInformation is the obvious call and it is
     * Win2000+ -- a single NT-only STATIC import kills the whole image at
     * load on Win9x, before main(), with no log written and nothing to
     * debug. DuplicateHandle is the pre-Win2000 way and works everywhere. */
    {
        HANDLE dup = NULL;
        if (DuplicateHandle(GetCurrentProcess(), rd,
                            GetCurrentProcess(), &dup,
                            0, FALSE, DUPLICATE_SAME_ACCESS)) {
            CloseHandle(rd);
            rd = dup;
        }
    }

    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = wr;
    si.hStdError = wr;          /* both into one pipe, as exec.c does */
    si.hStdInput = NULL;

    _snprintf(cmd, sizeof(cmd) - 1, "%s", cmdline);
    cmd[sizeof(cmd) - 1] = '\0';

    /* CREATE_NO_WINDOW is invalid for a 16-bit child, hence SW_HIDE above. */
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0,
                        NULL, NULL, &si, &pi)) {
        CloseHandle(rd); CloseHandle(wr);
        return -2;
    }
    CloseHandle(wr);            /* so ReadFile ends when the child exits */

    for (;;) {
        DWORD avail = 0, rdn = 0;
        if (GetTickCount() - start > 20000) { *timed_out = 1; break; }
        if (PeekNamedPipe(rd, NULL, 0, NULL, &avail, NULL) && avail > 0) {
            if (got + avail >= outsz - 1) avail = outsz - 1 - got;
            if (avail && ReadFile(rd, out + got, avail, &rdn, NULL) && rdn) {
                got += rdn;
                out[got] = '\0';
                continue;
            }
        }
        if (WaitForSingleObject(pi.hProcess, 200) == WAIT_OBJECT_0) {
            /* Drain whatever is left after exit. */
            while (PeekNamedPipe(rd, NULL, 0, NULL, &avail, NULL) && avail > 0) {
                if (got + avail >= outsz - 1) avail = outsz - 1 - got;
                if (!avail || !ReadFile(rd, out + got, avail, &rdn, NULL) || !rdn)
                    break;
                got += rdn;
                out[got] = '\0';
            }
            break;
        }
    }

    if (*timed_out) TerminateProcess(pi.hProcess, 1);
    *ms = GetTickCount() - start;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    CloseHandle(rd);
    return (int)got;
}

static void try_cmd(const char *label, const char *cmd, const char *expect)
{
    static char buf[32768];
    DWORD ms = 0;
    int timed_out = 0;
    int n = run_capture(cmd, buf, sizeof(buf), &ms, &timed_out);

    say("");
    say("  %s", label);
    say("    cmd      : %s", cmd);
    if (n < 0) { say("    RESULT   : FAIL, CreateProcess error %d", n); return; }
    say("    bytes    : %d in %lums%s", n, ms, timed_out ? " (TIMED OUT)" : "");
    if (n > 0) {
        char first[120];
        int i = 0;
        while (i < (int)sizeof(first) - 1 && buf[i] && buf[i] != '\r'
               && buf[i] != '\n') { first[i] = buf[i]; i++; }
        first[i] = '\0';
        say("    first    : %s", first);
    }
    if (n == 0)
        say("    RESULT   : FAIL -- EMPTY. A no-hang test would have passed.");
    else if (expect && !strstr(buf, expect))
        say("    RESULT   : FAIL -- output did not contain '%s'", expect);
    else
        say("    RESULT   : PASS -- non-empty and correct");
}

static void probe_exec(void)
{
    DWORD v = GetVersion();
    int is9x = (v & 0x80000000) != 0;
    const char *shell = is9x ? "command.com" : "cmd.exe";
    char cmd[256];

    head("0.9  16-bit EXEC capture (can reshape Phase 3)");
    say("shell for this OS: %s%s", shell, is9x ? "  (16-bit!)" : "");
    say("The assertion is 'output non-empty AND correct', not 'did not hang'.");

    _snprintf(cmd, sizeof(cmd), "%s /c echo RETROCLAUDE_MARKER", shell);
    try_cmd("echo through the shell", cmd, "RETROCLAUDE_MARKER");

    _snprintf(cmd, sizeof(cmd), "%s /c ver", shell);
    try_cmd("ver", cmd, NULL);

    _snprintf(cmd, sizeof(cmd), "%s /c dir c:\\ /w", shell);
    try_cmd("dir", cmd, NULL);

    /* FIND is the known-bad 16-bit tool. */
    _snprintf(cmd, sizeof(cmd),
              "%s /c echo hello | find \"hello\"", shell);
    try_cmd("pipe through FIND (known-bad 16-bit tool)", cmd, "hello");

    say("");
    say("  If these are empty, the fallback is claudewin9x's: redirect to a");
    say("  temp file and read it back. Budget that rewrite for Phase 3.");
}

/* ------------------------------------------------------------------ 0.10 */
/* Does GetDIBits emit BI_RLE8 directly? If so GDI does the screenshot
 * compression for us and the guest CPU cost is near zero. Unverified in the
 * plan, deliberately not assumed. */
static void probe_screen(void)
{
    HDC screen, mem;
    HBITMAP bmp;
    BITMAPINFOHEADER bi;
    int w, h, bpp;
    DWORD t0, t1;
    static unsigned char buf[2048 * 1024];
    int lines;

    head("0.10  screen capture / BI_RLE8");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC(NULL) failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);
    bpp = GetDeviceCaps(screen, BITSPIXEL);
    say("display        : %dx%d at %d bpp", w, h, bpp);

    mem = CreateCompatibleDC(screen);
    bmp = CreateCompatibleBitmap(screen, w, h);
    SelectObject(mem, bmp);

    t0 = GetTickCount();
    BitBlt(mem, 0, 0, w, h, screen, 0, 0, SRCCOPY);
    t1 = GetTickCount();
    say("BitBlt full    : %lums", t1 - t0);

    /* 24bpp reference. */
    memset(&bi, 0, sizeof(bi));
    bi.biSize = sizeof(bi);
    bi.biWidth = w; bi.biHeight = h; bi.biPlanes = 1;
    bi.biBitCount = 24; bi.biCompression = BI_RGB;
    t0 = GetTickCount();
    lines = GetDIBits(mem, bmp, 0, h, buf, (BITMAPINFO *)&bi, DIB_RGB_COLORS);
    t1 = GetTickCount();
    say("GetDIBits 24bpp: %d lines in %lums, %lu bytes",
        lines, t1 - t0, (unsigned long)(((w * 3 + 3) & ~3) * h));

    /* 8bpp, uncompressed. */
    {
        static unsigned char info[sizeof(BITMAPINFOHEADER) + 256 * 4];
        BITMAPINFOHEADER *p = (BITMAPINFOHEADER *)info;
        memset(info, 0, sizeof(info));
        p->biSize = sizeof(BITMAPINFOHEADER);
        p->biWidth = w; p->biHeight = h; p->biPlanes = 1;
        p->biBitCount = 8; p->biCompression = BI_RGB;
        t0 = GetTickCount();
        lines = GetDIBits(mem, bmp, 0, h, buf, (BITMAPINFO *)info,
                          DIB_RGB_COLORS);
        t1 = GetTickCount();
        say("GetDIBits 8bpp : %d lines in %lums, %lu bytes",
            lines, t1 - t0, (unsigned long)(((w + 3) & ~3) * h));

        /* 8bpp, RLE8 -- the question. */
        memset(info, 0, sizeof(info));
        p->biSize = sizeof(BITMAPINFOHEADER);
        p->biWidth = w; p->biHeight = h; p->biPlanes = 1;
        p->biBitCount = 8; p->biCompression = BI_RLE8;
        p->biSizeImage = sizeof(buf);
        SetLastError(0);
        t0 = GetTickCount();
        lines = GetDIBits(mem, bmp, 0, h, buf, (BITMAPINFO *)info,
                          DIB_RGB_COLORS);
        t1 = GetTickCount();
        if (lines > 0 && p->biSizeImage > 0 && p->biSizeImage < sizeof(buf)) {
            say("GetDIBits RLE8 : PASS -- %d lines, %lu bytes in %lums",
                lines, (unsigned long)p->biSizeImage, t1 - t0);
            say("   -> GDI does the screenshot compression for us.");
        } else {
            say("GetDIBits RLE8 : not supported (lines=%d err=%lu)",
                lines, GetLastError());
            say("   -> hand-roll RLE8, roughly 40 lines.");
        }
    }

    DeleteObject(bmp);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
}

/* ------------------------------------------------------------------ 0.5c */
/* Every threshold in the adaptive-codec design rests on these numbers, and so
 * far they are estimates from a modern CPU. */
static void probe_deflate(void)
{
    static char src[256 * 1024];
    static unsigned char comp[300 * 1024];
    static unsigned char dec[256 * 1024];
    /* NOTE: this sample is one two-line block repeated, so it compresses
     * ~164:1 and the throughput below is flattered badly -- deflate on near
     * pure repetition barely has to search. Measured on the guest it gave
     * 7707 KB/s at level 1, which is NOT a usable threshold. Before 0.5c can
     * be closed this needs a realistic corpus (the project's own prose and
     * code, which measured 2.16:1) read from a file next to the exe. */
    static const char *SAMPLE =
        "The bridge owns markdown, so the wire carries pre-styled runs.\r\n"
        "if (len > RC_MAX_FRAME) return -1;  /* 64KB, not 32MB */\r\n";
    mz_ulong clen, dlen;
    unsigned i, slen, total = 0;
    DWORD t0, t1;
    int rc, lvl;

    head("0.5c  deflate throughput on this CPU");

    slen = (unsigned)strlen(SAMPLE);
    while (total + slen < sizeof(src) - 1) {
        memcpy(src + total, SAMPLE, slen);
        total += slen;
        /* Vary it a little so the ratio is not absurdly flattered. */
        if ((total / slen) % 7 == 0 && total + 8 < sizeof(src))
            { memcpy(src + total, "vary-XY\n", 8); total += 8; }
    }
    src[total] = '\0';
    say("sample         : %u bytes of mixed prose and code", total);

    for (lvl = 1; lvl <= 6; lvl += 5) {
        clen = sizeof(comp);
        t0 = GetTickCount();
        rc = mz_compress2(comp, &clen, (const unsigned char *)src, total, lvl);
        t1 = GetTickCount();
        if (rc != MZ_OK) { say("level %d: compress failed rc=%d", lvl, rc); continue; }
        say("level %d deflate: %lu -> %lu (%.2f:1) in %lums  = %lu KB/s",
            lvl, (unsigned long)total, (unsigned long)clen,
            (double)total / (double)clen, t1 - t0,
            (t1 - t0) ? (unsigned long)(total / (t1 - t0)) : 0UL);
    }

    clen = sizeof(comp);
    mz_compress2(comp, &clen, (const unsigned char *)src, total, 1);
    dlen = sizeof(dec);
    t0 = GetTickCount();
    rc = mz_uncompress(dec, &dlen, comp, clen);
    t1 = GetTickCount();
    say("inflate        : %lu -> %lu in %lums = %lu KB/s  (rc=%d)",
        (unsigned long)clen, (unsigned long)dlen, t1 - t0,
        (t1 - t0) ? (unsigned long)(dlen / (t1 - t0)) : 0UL, rc);
    say(memcmp(dec, src, total) == 0 ? "round trip     : PASS"
                                     : "round trip     : FAIL");
    say("");
    say("  At ~750 KB/s LAN, compression is worth it only if the deflate");
    say("  rate above comfortably exceeds it. At ~5 KB/s dialup it always is.");
}

/* ------------------------------------------------------------------ html */
/* What does hosting the HTML engine actually cost on this machine?
 *
 * The plan rejected MSHTML partly on a "multi-MB DLL tax", which was an
 * estimate. It is measurable, and the estimate is probably pessimistic: on
 * Win9x the code pages of a DLL are shared between processes, and Explorer and
 * the shell have already mapped most of this. What our process pays for is its
 * own data, not mshtml.dll's size on disk.
 *
 * Free physical memory before and after is the honest measure, because it
 * counts everything the engine drags in rather than only what we asked for. */
static unsigned long free_kb(void)
{
    MEMORYSTATUS ms;
    memset(&ms, 0, sizeof(ms));
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatus(&ms);
    return (unsigned long)(ms.dwAvailPhys / 1024);
}

/* Defined locally rather than pulled from uuid.lib, so this costs nothing at
 * link time if the idea is dropped. */
static const GUID CLSID_HTMLDocument_ =
    {0x25336920,0x03F9,0x11CF,{0x8F,0xD0,0x00,0xAA,0x00,0x68,0x6F,0x13}};
static const GUID CLSID_WebBrowser_ =
    {0x8856F961,0x340A,0x11D0,{0xA9,0x6B,0x00,0xC0,0x4F,0xD7,0x05,0xA2}};
static const GUID IID_IUnknown_ =
    {0x00000000,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};

static void one_step(const char *what, unsigned long *prev)
{
    unsigned long now = free_kb();
    long delta = (long)*prev - (long)now;
    say("  %-34s %7lu KB free   (%+ld KB)", what, now, -delta ? delta : 0);
    *prev = now;
}

static void probe_html(void)
{
    unsigned long start, prev;
    HMODULE m;
    IUnknown *doc = NULL, *web = NULL;
    HRESULT hr;

    head("HTML  cost of hosting MSHTML (is an HTML transcript affordable?)");

    start = prev = free_kb();
    say("  %-34s %7lu KB free", "baseline", start);

    if (FAILED(CoInitialize(NULL))) { say("  CoInitialize FAILED"); return; }
    one_step("CoInitialize", &prev);

    m = LoadLibraryA("mshtml.dll");
    if (!m) {
        say("  mshtml.dll NOT PRESENT -- no HTML engine on this install");
        CoUninitialize();
        return;
    }
    one_step("LoadLibrary(mshtml.dll)", &prev);

    hr = CoCreateInstance(&CLSID_HTMLDocument_, NULL, CLSCTX_INPROC_SERVER,
                          &IID_IUnknown_, (void **)&doc);
    if (FAILED(hr)) say("  CoCreateInstance(HTMLDocument) failed hr=%08lX",
                        (unsigned long)hr);
    else one_step("CoCreateInstance(HTMLDocument)", &prev);

    hr = CoCreateInstance(&CLSID_WebBrowser_, NULL, CLSCTX_INPROC_SERVER,
                          &IID_IUnknown_, (void **)&web);
    if (FAILED(hr)) say("  CoCreateInstance(WebBrowser) failed hr=%08lX",
                        (unsigned long)hr);
    else one_step("CoCreateInstance(WebBrowser)", &prev);

    say("");
    say("  TOTAL COST: %ld KB", (long)start - (long)free_kb());
    say("  Budget: the GUI itself measured ~1.4 MB, and a 32MB Win98 has");
    say("  roughly 9 MB free at the desktop.");

    if (web) web->lpVtbl->Release(web);
    if (doc) doc->lpVtbl->Release(doc);
    CoUninitialize();
    say("  after releasing everything:       %7lu KB free", free_kb());
}


/* ------------------------------------------------------------------ 0.14 */
/* Does Windows 98 have a directory watcher, and does it actually work?
 *
 * Open Watcom's headers declare FindFirstChangeNotificationA and friends, but a
 * header declaring a function proves it is part of Win32 -- NOT that this
 * guest's KERNEL32 exports it. That distinction is what 0.7 existed for, and a
 * static import the guest lacks kills the image before WinMain with no log.
 *
 * So this resolves it the way the product will: dynamically. A NULL here is a
 * supported answer, not a failure -- the sync layer polls regardless and the
 * notification is only an accelerator. What we are really measuring is whether
 * the accelerator is worth wiring up, and whether bWatchSubtree means on FAT
 * what the NT-era documentation says it means. */

typedef HANDLE (WINAPI *FFCN)(LPCSTR, BOOL, DWORD);
typedef BOOL   (WINAPI *FNCN)(HANDLE);
typedef BOOL   (WINAPI *FCCN)(HANDLE);

static FFCN p_first;
static FNCN p_next;
static FCCN p_close;

/* Wait for the handle, report how long it took. Returns 1 if it signalled. */
static int watch_wait(HANDLE h, const char *what, DWORD ms)
{
    DWORD t0 = GetTickCount();
    DWORD r  = WaitForSingleObject(h, ms);
    DWORD dt = GetTickCount() - t0;

    if (r == WAIT_OBJECT_0) {
        say("    %-34s SIGNALLED after %lums", what, (unsigned long)dt);
        return 1;
    }
    if (r == WAIT_TIMEOUT) {
        say("    %-34s no signal (waited %lums)", what, (unsigned long)dt);
        return 0;
    }
    say("    %-34s wait failed r=%lu err=%lu", what,
        (unsigned long)r, (unsigned long)GetLastError());
    return 0;
}

static void touch_file(const char *path, const char *text)
{
    FILE *f = fopen(path, "wb");
    if (f) { fputs(text, f); fclose(f); }
    else say("    (could not write %s)", path);
}

static void probe_watch(void)
{
    HMODULE k32;
    HANDLE  h;
    int     signalled = 0, total = 0;

    head("0.14  directory change notification (sync layer accelerator)");

    k32 = GetModuleHandleA("kernel32.dll");
    p_first = (FFCN)GetProcAddress(k32, "FindFirstChangeNotificationA");
    p_next  = (FNCN)GetProcAddress(k32, "FindNextChangeNotification");
    p_close = (FCCN)GetProcAddress(k32, "FindCloseChangeNotification");

    say("FindFirstChangeNotificationA : %s",
        p_first ? "present" : "MISSING");
    say("FindNextChangeNotification   : %s",
        p_next ? "present" : "MISSING");
    say("FindCloseChangeNotification  : %s",
        p_close ? "present" : "MISSING");

    if (!p_first || !p_next || !p_close) {
        say("");
        say("  -> not all present. The sync layer polls; nothing is lost but");
        say("     latency. Do not add a static import for these.");
        return;
    }

    /* A tree with a subdirectory, so bWatchSubtree can be tested honestly. */
    CreateDirectoryA("C:\\WATCHTST", NULL);
    CreateDirectoryA("C:\\WATCHTST\\SUB", NULL);
    touch_file("C:\\WATCHTST\\SEED.TXT", "seed\r\n");

    h = p_first("C:\\WATCHTST", TRUE,
                FILE_NOTIFY_CHANGE_FILE_NAME |
                FILE_NOTIFY_CHANGE_LAST_WRITE |
                FILE_NOTIFY_CHANGE_SIZE);
    if (h == INVALID_HANDLE_VALUE || h == NULL) {
        say("  FindFirstChangeNotificationA failed: err=%lu",
            (unsigned long)GetLastError());
        say("  -> polling only.");
        return;
    }
    say("");
    say("  watching C:\\WATCHTST with bWatchSubtree=TRUE");

    /* Nothing has happened yet: it must NOT already be signalled, or every
     * scan would be triggered by its own previous scan. */
    total++;
    if (watch_wait(h, "before any change (want: none)", 700))
        say("      ^^ signalled with no change -- would cause a rescan loop");
    else
        signalled++;

    touch_file("C:\\WATCHTST\\NEW1.TXT", "one\r\n");
    total++;
    signalled += watch_wait(h, "create in watched root", 5000);

    if (!p_next(h)) say("    FindNextChangeNotification failed: err=%lu",
                        (unsigned long)GetLastError());
    touch_file("C:\\WATCHTST\\SEED.TXT", "changed content\r\n");
    total++;
    signalled += watch_wait(h, "modify existing file", 5000);

    if (!p_next(h)) say("    FindNextChangeNotification failed (2)");
    touch_file("C:\\WATCHTST\\SUB\\DEEP.TXT", "deep\r\n");
    total++;
    signalled += watch_wait(h, "create in SUBDIRECTORY", 5000);

    if (!p_next(h)) say("    FindNextChangeNotification failed (3)");
    DeleteFileA("C:\\WATCHTST\\NEW1.TXT");
    total++;
    signalled += watch_wait(h, "delete in watched root", 5000);

    p_close(h);

    say("");
    say("  %d of %d checks behaved as the sync layer needs", signalled, total);
    if (signalled == total)
        say("  -> usable as an accelerator; poll interval can be generous.");
    else
        say("  -> unreliable here. Keep polling brisk; treat any signal as a"
            " bonus.");

    /* Tidy up so a re-run starts clean. */
    DeleteFileA("C:\\WATCHTST\\SEED.TXT");
    DeleteFileA("C:\\WATCHTST\\SUB\\DEEP.TXT");
    RemoveDirectoryA("C:\\WATCHTST\\SUB");
    RemoveDirectoryA("C:\\WATCHTST");
}

/* -------------------------------------------------------------------- */
int main(int argc, char **argv)
{
    const char *only = (argc > 1) ? argv[1] : NULL;

    /* Append, not truncate: the sections are usually run one at a time
     * ("probe threads", "probe exec", ...) and a truncating open means the
     * log only ever holds whichever section ran last. */
    g_log = fopen("PROBE.LOG", "a");
    say("RetroClaude Phase 0 probe");
    say("built %s %s", __DATE__, __TIME__);

    if (!only || !strcmp(only, "platform")) probe_platform();
    if (!only || !strcmp(only, "threads"))  probe_threads();
    if (!only || !strcmp(only, "exec"))     probe_exec();
    if (!only || !strcmp(only, "screen"))   probe_screen();
    if (!only || !strcmp(only, "deflate"))  probe_deflate();
    if (!only || !strcmp(only, "html"))     probe_html();
    if (!only || !strcmp(only, "watch"))    probe_watch();

    say("");
    say("=== done -- results also in PROBE.LOG ===");
    if (g_log) fclose(g_log);
    return 0;
}
