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
 *   probe automation Phase 6's gates 0.12 and 0.13 (screen strips, oleacc)
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "core/str.h"
/* Open Watcom's two OLE headers disagree about VARIANT unless this is set.
 * oaidl.h:130 picks the NAMED layout (n1.n2.n3) on `__STDC__`, but
 * oleauto.h:125's V_VT/V_I4 accessors only check NONAMELESSUNION, so they
 * expand to `(x)->vt` against a struct that has no such member and the
 * error names a field you can see in the header. Defining it makes both
 * agree on the named form. */
#define NONAMELESSUNION
#include <objbase.h>
#include <oleauto.h>
#include <oleacc.h>
#include "miniz.h"

static FILE *g_log;
/* COM1, when the VM has a file-backed serial port. Everything say() emits
 * goes out of the machine as it happens, so a run can be watched from the
 * host with `tail -f` instead of screenshotting an 80x25 DOS box and losing
 * whatever scrolled off. NULL when there is no port, which is not an error:
 * fopen on a device Win98 never enumerated simply fails. */
static HANDLE g_com = INVALID_HANDLE_VALUE;

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
    /* CRLF: the host reads this as a text file, and a bare LF makes every
     * line of a 200-line dump run together in half the tools that open it. */
    if (g_com != INVALID_HANDLE_VALUE) {
        DWORD wrote;
        WriteFile(g_com, buf, (DWORD)strlen(buf), &wrote, NULL);
        WriteFile(g_com, "\r\n", 2, &wrote, NULL);
    }
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
        rc_strcpy(buf, sizeof(buf), "a,b,c");
        tok = strtok(buf, ",");
        while (tok) tok = strtok(NULL, ",");
        rc_strf(buf, sizeof(buf), "%d", i);
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

    rc_strf(cmd, sizeof(cmd), "%s", cmdline);

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

    rc_strf(cmd, sizeof(cmd), "%s /c echo RETROCLAUDE_MARKER", shell);
    try_cmd("echo through the shell", cmd, "RETROCLAUDE_MARKER");

    rc_strf(cmd, sizeof(cmd), "%s /c ver", shell);
    try_cmd("ver", cmd, NULL);

    rc_strf(cmd, sizeof(cmd), "%s /c dir c:\\ /w", shell);
    try_cmd("dir", cmd, NULL);

    /* FIND is the known-bad 16-bit tool. */
    rc_strf(cmd, sizeof(cmd),
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

/* ---------------------------------------------------------- 0.12 / 0.13 */
/* Phase 6's screen-and-input gates. PLAN.md:1197 moved both out of Phase 3
 * with the screen tier, and neither has an answer yet.
 *
 * 0.12 exists because 0.10 answered a smaller question than it looks. It
 * proved GDI will RLE8 a WHOLE screen -- 307,200 bytes to 27,088 in 14ms,
 * 11.3:1 -- but a whole screen is exactly what there is no room for:
 * PROGRESS.md:709 measured 28 KB free after a turn against a 307 KB
 * full-frame 8bpp DIB. Capture has to go out in strips, so what matters is
 * whether GetDIBits emits a valid RLE8 stream for a RANGE of scanlines.
 * PLAN.md:1683 is blunt about why that is not a formality: "RLE8 is
 * row-oriented so it ought to, but 'ought to' is what 0.9 and 0.10 were
 * there to replace." Two arms below, because there are two ways to ask:
 * startScan/numScans against a full-screen bitmap, and a whole small bitmap
 * that a strip was BitBlt'd into (what app/screenshot.cpp already does).
 *
 * 0.13 is oleacc.dll. Absent means TREE falls back to the window tree and
 * HELLO says msaa=0 -- a whole branch of the design settled by one
 * LoadLibrary. Never a static import: a missing DLL in the import table
 * kills the image before main() with no log written.
 *
 * The rest are REMOTE.md's "UI details decided now", every one of them
 * carrying "verify on the P200". The foreground lock matters most: PLAN.md
 * said 9x has none, and REMOTE.md:344 corrected that to "98 introduced it
 * alongside 2000". Only the machine can say which is true here.
 */
/* All three sit behind `#if WINVER >= 0x0500` in Open Watcom's winuser.h,
 * and win/Makefile pins WINVER at 0x0400 on purpose (raising it changes
 * sizeof(NONCLIENTMETRICS)). Whether Win98 HONOURS them is the question;
 * whether the header admits they exist is not. */
#ifndef SPI_GETFOREGROUNDLOCKTIMEOUT
#define SPI_GETFOREGROUNDLOCKTIMEOUT 0x2000
#define SPI_SETFOREGROUNDLOCKTIMEOUT 0x2001
#endif
#ifndef OBJID_WINDOW
#define OBJID_WINDOW 0x00000000L
#endif
#ifndef OBJID_CLIENT
#define OBJID_CLIENT 0xFFFFFFFCL
#endif
/* oleacc.h:112 opens the STATE_SYSTEM_* block with `#ifndef
 * STATE_SYSTEM_UNAVAILABLE` and then defines every member of it EXCEPT
 * that one, so the guard names the only constant it omits. */
#ifndef STATE_SYSTEM_UNAVAILABLE
#define STATE_SYSTEM_UNAVAILABLE 0x00000001L
#endif

/* {618736E0-3C3D-11CF-810C-00AA00389B71} -- spelled out rather than linked
 * from uuid.lib, so a missing IID_IAccessible cannot fail the link on a
 * toolchain where oleacc's uuid is not in the import library. */
static const GUID RC_IID_IAccessible =
    { 0x618736e0, 0x3c3d, 0x11cf,
      { 0x81, 0x0c, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };

typedef HRESULT (WINAPI *PFN_AOFW)(HWND, LONG, REFIID, void **);

static int g_win_top;
static int g_win_nodes;
static int g_win_deepest;

static void count_children(HWND parent, int depth)
{
    HWND c;
    if (depth > g_win_deepest) g_win_deepest = depth;
    for (c = GetWindow(parent, GW_CHILD); c; c = GetWindow(c, GW_HWNDNEXT)) {
        g_win_nodes++;
        if (g_win_nodes > 20000) return;      /* a probe, not a walker */
        count_children(c, depth + 1);
    }
}

static BOOL CALLBACK count_top(HWND h, LPARAM lp)
{
    (void)lp;
    g_win_top++;
    g_win_nodes++;
    count_children(h, 1);
    return TRUE;
}

static void probe_automation(void)
{
    /* Probe-only: the shipping strip buffer is ~38 KB on the heap. This one
     * is static and large so an RLE8 arm that expands instead of shrinking
     * cannot scribble past it. Do not copy this into win/automation/. */
    static unsigned char buf[512 * 1024];
    static unsigned char info[sizeof(BITMAPINFOHEADER) + 256 * 4];
    BITMAPINFOHEADER *bi = (BITMAPINFOHEADER *)info;
    HDC screen, mem;
    HBITMAP full, strip, oldbmp;
    int w, h, bpp, lines;
    const int STRIP_H = 16;

    head("0.12  RLE8 for a scanline range / 0.13  oleacc / UI details");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC(NULL) failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);
    bpp = GetDeviceCaps(screen, BITSPIXEL);
    say("display        : %dx%d at %d bpp", w, h, bpp);

    mem = CreateCompatibleDC(screen);
    full = CreateCompatibleBitmap(screen, w, h);
    oldbmp = (HBITMAP)SelectObject(mem, full);
    BitBlt(mem, 0, 0, w, h, screen, 0, 0, SRCCOPY);

    /* 0.12a -- rows [h/2, h/2+STRIP_H) of the full-screen bitmap, as RLE8.
     * This is the form that needs no second bitmap at all. */
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = h; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RLE8;
    bi->biSizeImage = sizeof(buf);
    SetLastError(0);
    lines = GetDIBits(mem, full, h / 2, STRIP_H, buf, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    if (lines == STRIP_H && bi->biSizeImage > 0
        && bi->biSizeImage < sizeof(buf)) {
        say("0.12a range    : PASS -- %d lines, %lu bytes",
            lines, (unsigned long)bi->biSizeImage);
    } else {
        say("0.12a range    : FAIL -- lines=%d size=%lu err=%lu",
            lines, (unsigned long)bi->biSizeImage, GetLastError());
        say("   -> strips ship BI_RGB 8bpp; the bridge does the RLE.");
    }

    /* 0.12b -- BitBlt a strip into its own bitmap, then RLE8 the whole of
     * it. This is what app/screenshot.cpp:106-140 already does for 24bpp,
     * so if 0.12a fails and this passes, the port keeps its current shape. */
    SelectObject(mem, oldbmp);
    strip = CreateCompatibleBitmap(screen, w, STRIP_H);
    SelectObject(mem, strip);
    BitBlt(mem, 0, 0, w, STRIP_H, screen, 0, h / 2, SRCCOPY);
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = STRIP_H; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RLE8;
    bi->biSizeImage = sizeof(buf);
    SetLastError(0);
    lines = GetDIBits(mem, strip, 0, STRIP_H, buf, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    if (lines == STRIP_H && bi->biSizeImage > 0
        && bi->biSizeImage < sizeof(buf)) {
        say("0.12b blt+whole: PASS -- %d lines, %lu bytes",
            lines, (unsigned long)bi->biSizeImage);
    } else {
        say("0.12b blt+whole: FAIL -- lines=%d size=%lu err=%lu",
            lines, (unsigned long)bi->biSizeImage, GetLastError());
    }

    /* The palette claim. FORKED.md:34 says a fresh memory DC carries only
     * the 20 static system colours, so an 8bpp GetDIBits returns colour-index
     * noise until the live hardware palette is selected in. That is recorded
     * as hardware-verified on retro-agent, never here. Count what the colour
     * table actually comes back with, and what the screen says it has. */
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = STRIP_H; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RGB;
    lines = GetDIBits(mem, strip, 0, STRIP_H, buf, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    {
        RGBQUAD *tbl = (RGBQUAD *)(info + sizeof(BITMAPINFOHEADER));
        int i, nonzero = 0;
        PALETTEENTRY pal[256];
        UINT got;
        for (i = 0; i < 256; i++) {
            if (tbl[i].rgbRed || tbl[i].rgbGreen || tbl[i].rgbBlue) nonzero++;
        }
        got = GetSystemPaletteEntries(screen, 0, 256, pal);
        say("8bpp BI_RGB    : %d lines (the fallback if 0.12 fails)", lines);
        say("palette        : DC table %d non-black of 256, screen reports %u",
            nonzero, got);
        if (bpp <= 8 && nonzero <= 24) {
            say("   -> confirms the 20-static-colour trap; select the live "
                "palette before capture.");
        }
    }

    SelectObject(mem, oldbmp);
    DeleteObject(strip);
    DeleteObject(full);
    DeleteDC(mem);

    /* 0.13 -- oleacc, loaded and actually asked a question. */
    {
        HMODULE acc = LoadLibraryA("oleacc.dll");
        if (!acc) {
            say("0.13 oleacc    : ABSENT (%lu) -> msaa=0, TREE is window-only",
                GetLastError());
        } else {
            PFN_AOFW aofw = (PFN_AOFW)GetProcAddress(
                acc, "AccessibleObjectFromWindow");
            say("0.13 oleacc    : loaded, AccessibleObjectFromWindow %s",
                aofw ? "found" : "MISSING");
            if (aofw) {
                HWND target = FindWindowA("Shell_TrayWnd", NULL);
                if (!target) target = GetDesktopWindow();
                CoInitialize(NULL);
                {
                    IAccessible *ia = NULL;
                    HRESULT hr = aofw(target, OBJID_WINDOW,
                                      &RC_IID_IAccessible, (void **)&ia);
                    if (hr != S_OK || !ia) {
                        say("     walk      : AccessibleObjectFromWindow "
                            "hr=0x%08lx", (unsigned long)hr);
                    } else {
                        long kids = -1;
                        ia->lpVtbl->get_accChildCount(ia, &kids);
                        say("     walk      : OK, childCount=%ld on %s",
                            kids, target == GetDesktopWindow()
                                  ? "desktop" : "Shell_TrayWnd");
                        ia->lpVtbl->Release(ia);
                    }
                }
                CoUninitialize();
            }
            FreeLibrary(acc);
        }
    }

    /* The foreground lock. REMOTE.md:344 says 98 has one and PLAN.md said
     * it does not; whichever is true, a background gremote that cannot take
     * the foreground makes the taskbar button flash instead of typing. */
    {
        DWORD before = 0, after = 0;
        BOOL rg = SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                        &before, 0);
        BOOL rs = SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                        (void *)0, SPIF_SENDCHANGE);
        SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &after, 0);
        say("fg lock        : get=%d(%lu) set=%d now=%lu",
            (int)rg, (unsigned long)before, (int)rs, (unsigned long)after);
    }

    /* Cross-process window text has to go through SendMessageTimeout, since
     * IsHungAppWindow is 2000+ and GetWindowText returns only a caption
     * across a process boundary (PLAN.md:1636). */
    {
        HWND t = FindWindowA("Shell_TrayWnd", NULL);
        DWORD_PTR res = 0;
        LRESULT ok;
        if (!t) t = GetDesktopWindow();
        ok = SendMessageTimeoutA(t, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 1000,
                                 &res);
        say("SendMsgTimeout : WM_NULL ret=%ld (0 = timed out or hung)",
            (long)ok);
    }

    /* What a real desktop actually holds, so max_tree_nodes=2000 in
     * REMOTE.md's [ui] section is a measurement and not a guess. */
    g_win_top = g_win_nodes = g_win_deepest = 0;
    EnumWindows(count_top, 0);
    say("window census  : %d top-level, %d nodes total, depth %d",
        g_win_top, g_win_nodes, g_win_deepest);

    ReleaseDC(NULL, screen);
}

/* ------------------------------------------------------------ 0.13b */
/* What a TREE walk actually costs, broken down, because the only number
 * anyone had was arithmetic: 44 nodes times a 2000ms call_timeout_ms is 88
 * seconds, and that is the cost only if every window on the desktop is
 * wedged. Nobody has timed a real one.
 *
 * Five passes over the SAME node list, each adding one thing, so the
 * difference between two adjacent rows is the cost of the thing added:
 *
 *   A  enumerate only                 EnumWindows + GW_CHILD/GW_HWNDNEXT
 *   B  + GetClassName                 same-process, no message sent
 *   C  + WM_GETTEXT, 2000ms budget    cross-process, blocks on their pump
 *   D  + WM_GETTEXT, 200ms budget     is the budget doing the damage?
 *   E  + MSAA name per node           is oleacc the expensive part?
 *
 * GetTickCount on 9x ticks about every 55ms, so anything quick is timed
 * over repeats and divided. A single pass of a slow one is enough.
 */
#define UIW_MAX 4096
static HWND g_uiw[UIW_MAX];
static int g_uiw_n;

/* Visible-only, and this is not an optimisation.
 *
 * Walking hidden windows MOVED the desktop: `probe msaa` left the Start
 * menu open every run while `probe screen`, same driving, never did. A
 * closed Start menu is a window that still exists and is merely hidden, and
 * asking oleacc for its accessible object appears to realise it. A tool
 * whose job is to report what is on screen must not change what is on
 * screen, so hidden windows are not walked at all.
 *
 * It is also free and it halves the work: 45 to 47 of the 83 nodes on this
 * desktop were invisible.
 */
static int g_uiw_all;                    /* 1 = walk hidden ones too */

static void collect_kids(HWND parent)
{
    HWND c;
    for (c = GetWindow(parent, GW_CHILD); c; c = GetWindow(c, GW_HWNDNEXT)) {
        if (g_uiw_n >= UIW_MAX) return;
        if (!g_uiw_all && !IsWindowVisible(c)) continue;
        g_uiw[g_uiw_n++] = c;
        collect_kids(c);
    }
}

static BOOL CALLBACK collect_top(HWND h, LPARAM lp)
{
    (void)lp;
    if (g_uiw_n >= UIW_MAX) return FALSE;
    if (!g_uiw_all && !IsWindowVisible(h)) return TRUE;
    g_uiw[g_uiw_n++] = h;
    collect_kids(h);
    return TRUE;
}

static void probe_uiwalk(void)
{
    DWORD t0, t1;
    int i, r, timeouts;
    char buf[256];
    const int REPS = 20;

    head("0.13b  what a TREE walk costs, one column at a time");

    /* A -- enumeration alone. */
    t0 = GetTickCount();
    for (r = 0; r < REPS; r++) { g_uiw_n = 0; EnumWindows(collect_top, 0); }
    t1 = GetTickCount();
    say("A enum only    : %d nodes, %lu ms/walk",
        g_uiw_n, (unsigned long)(t1 - t0) / REPS);

    /* B -- plus the class name. GetClassName reads the class atom out of
     * USER's own tables; it sends no message, so this should be nearly
     * free and is here to prove that rather than assume it. */
    t0 = GetTickCount();
    for (r = 0; r < REPS; r++)
        for (i = 0; i < g_uiw_n; i++)
            GetClassNameA(g_uiw[i], buf, sizeof(buf));
    t1 = GetTickCount();
    say("B + class      : %lu ms/walk", (unsigned long)(t1 - t0) / REPS);

    /* C -- plus the text, at the budget REMOTE.md proposes. This is the
     * one that can block: WM_GETTEXT is marshalled by USER32 and does not
     * return until the target's own message loop answers it. */
    timeouts = 0;
    t0 = GetTickCount();
    for (i = 0; i < g_uiw_n; i++) {
        DWORD_PTR res = 0;
        buf[0] = '\0';
        if (!SendMessageTimeoutA(g_uiw[i], WM_GETTEXT, sizeof(buf),
                                 (LPARAM)buf, SMTO_ABORTIFHUNG, 2000, &res))
            timeouts++;
    }
    t1 = GetTickCount();
    say("C + text 2000ms: %lu ms total, %d timed out",
        (unsigned long)(t1 - t0), timeouts);

    /* D -- the same again on a tenth of the budget. If C and D agree, the
     * budget is not what costs; if D is ten times faster, the walk is
     * spending its life waiting on windows that were never going to
     * answer, and the fix is a short first pass. */
    timeouts = 0;
    t0 = GetTickCount();
    for (i = 0; i < g_uiw_n; i++) {
        DWORD_PTR res = 0;
        buf[0] = '\0';
        if (!SendMessageTimeoutA(g_uiw[i], WM_GETTEXT, sizeof(buf),
                                 (LPARAM)buf, SMTO_ABORTIFHUNG, 200, &res))
            timeouts++;
    }
    t1 = GetTickCount();
    say("D + text 200ms : %lu ms total, %d timed out",
        (unsigned long)(t1 - t0), timeouts);

    /* E -- plus MSAA. One IAccessible per node, asked for its name, then
     * released. If this dwarfs C, the answer is to fetch MSAA lazily for
     * the nodes the caller asks about rather than for the whole walk. */
    {
        HMODULE acc = LoadLibraryA("oleacc.dll");
        PFN_AOFW aofw = acc ? (PFN_AOFW)GetProcAddress(
            acc, "AccessibleObjectFromWindow") : 0;
        if (!aofw) {
            say("E + msaa       : skipped, oleacc unavailable");
        } else {
            int made = 0, named = 0;
            CoInitialize(NULL);
            t0 = GetTickCount();
            for (i = 0; i < g_uiw_n; i++) {
                IAccessible *ia = 0;
                if (aofw(g_uiw[i], OBJID_WINDOW, &RC_IID_IAccessible,
                         (void **)&ia) == S_OK && ia) {
                    VARIANT self;
                    BSTR nm = 0;
                    made++;
                    /* V_VT/V_I4 rather than .vt/.lVal: Open Watcom's C mode
                     * has no anonymous unions, so the members are really
                     * n1.n2.vt and n1.n2.n3.lVal (oleauto.h:127-133). */
                    V_VT(&self) = VT_I4;
                    V_I4(&self) = 0;             /* CHILDID_SELF */
                    if (ia->lpVtbl->get_accName(ia, self, &nm) == S_OK && nm) {
                        named++;
                        SysFreeString(nm);
                    }
                    ia->lpVtbl->Release(ia);
                }
            }
            t1 = GetTickCount();
            say("E + msaa       : %lu ms total, %d objects, %d named",
                (unsigned long)(t1 - t0), made, named);
            CoUninitialize();
        }
        if (acc) FreeLibrary(acc);
    }

    say("   (55ms tick: A and B are over %d repeats and divided)", REPS);
}

/* ------------------------------------------------------------ 0.13c */
/* Is MSAA worth 7.6ms a node? 0.13b said it names 17 of 45; it did not say
 * what those names are worth, and a name we already had from WM_GETTEXT is
 * worth nothing.
 *
 * So: for every node, fetch both sides and count where MSAA adds something
 * the plain window tree could not give. The fields that can only come from
 * MSAA are state (is this checkbox CHECKED -- the window tree cannot say
 * without sending BM_GETCHECK, and that is per-control-class guesswork),
 * value (an edit control's contents, a slider's position) and the default
 * action. Role mostly duplicates the class name for standard controls but
 * separates push button from checkbox from radio, which are style bits.
 */
static void bstr_narrow(BSTR b, char *out, int cap)
{
    out[0] = '\0';
    if (!b) return;
    WideCharToMultiByte(CP_ACP, 0, b, -1, out, cap, NULL, NULL);
    out[cap - 1] = '\0';
}

static void msaa_pass(PFN_AOFW aofw, LONG objid, const char *label)
{
    int i;
    int n_text = 0, n_name = 0, n_name_only = 0, n_diff = 0;
    int n_value = 0, n_action = 0, n_role = 0, n_state = 0;
    int n_checked = 0, n_focused = 0, n_unavail = 0, n_invis = 0;
    int shown = 0;
    int n_obj = 0;

    say("-- %s --", label);
    for (i = 0; i < g_uiw_n; i++) {
        char text[128], name[128], value[128], action[64], cls[64];
        DWORD_PTR res = 0;
        IAccessible *ia = 0;
        VARIANT self, vrole, vstate;
        BSTR b = 0;
        long role = 0, state = 0;

        text[0] = name[0] = value[0] = action[0] = '\0';
        GetClassNameA(g_uiw[i], cls, sizeof(cls));
        SendMessageTimeoutA(g_uiw[i], WM_GETTEXT, sizeof(text), (LPARAM)text,
                            SMTO_ABORTIFHUNG, 200, &res);
        if (text[0]) n_text++;

        if (aofw(g_uiw[i], objid, &RC_IID_IAccessible,
                 (void **)&ia) != S_OK || !ia)
            continue;
        n_obj++;

        V_VT(&self) = VT_I4;
        V_I4(&self) = 0;

        if (ia->lpVtbl->get_accName(ia, self, &b) == S_OK && b) {
            bstr_narrow(b, name, sizeof(name));
            SysFreeString(b);
            b = 0;
        }
        if (ia->lpVtbl->get_accValue(ia, self, &b) == S_OK && b) {
            bstr_narrow(b, value, sizeof(value));
            SysFreeString(b);
            b = 0;
        }
        if (ia->lpVtbl->get_accDefaultAction(ia, self, &b) == S_OK && b) {
            bstr_narrow(b, action, sizeof(action));
            SysFreeString(b);
            b = 0;
        }
        V_VT(&vrole) = VT_EMPTY;
        if (ia->lpVtbl->get_accRole(ia, self, &vrole) == S_OK
            && V_VT(&vrole) == VT_I4)
            role = V_I4(&vrole);
        V_VT(&vstate) = VT_EMPTY;
        if (ia->lpVtbl->get_accState(ia, self, &vstate) == S_OK
            && V_VT(&vstate) == VT_I4)
            state = V_I4(&vstate);

        if (name[0]) n_name++;
        if (name[0] && !text[0]) n_name_only++;
        if (name[0] && text[0] && strcmp(name, text)) n_diff++;
        if (value[0]) n_value++;
        if (action[0]) n_action++;
        if (role) n_role++;
        if (state) n_state++;
        if (state & STATE_SYSTEM_CHECKED)     n_checked++;
        if (state & STATE_SYSTEM_FOCUSED)     n_focused++;
        if (state & STATE_SYSTEM_UNAVAILABLE) n_unavail++;
        if (state & STATE_SYSTEM_INVISIBLE)   n_invis++;

        /* Raw: every node, every field, no filtering and no summary. The
         * counts hid exactly what was wanted -- `value 0` looked like MSAA
         * having nothing to say when it was the wrong objid being asked. */
        shown++;
        say("%-13.13s|%-14.14s|%-14.14s|%-10.10s|%-7.7s|r%ld s%lx",
            cls, text[0] ? text : "-", name[0] ? name : "-",
            value[0] ? value : "-", action[0] ? action : "-",
            role, (unsigned long)state);
        ia->lpVtbl->Release(ia);
    }

    say("-- %s: %d nodes, %d objs, %d rows --", label, g_uiw_n, n_obj, shown);
    (void)n_text; (void)n_name; (void)n_name_only; (void)n_diff;
    (void)n_value; (void)n_action; (void)n_role; (void)n_state;
    (void)n_checked; (void)n_focused; (void)n_unavail; (void)n_invis;
}

static void probe_msaa(void)
{
    HMODULE acc;
    PFN_AOFW aofw;

    head("0.13c  what MSAA actually adds over the window tree");

    acc = LoadLibraryA("oleacc.dll");
    aofw = acc ? (PFN_AOFW)GetProcAddress(acc, "AccessibleObjectFromWindow")
               : 0;
    if (!aofw) { say("oleacc unavailable"); if (acc) FreeLibrary(acc); return; }

    g_uiw_n = 0;
    EnumWindows(collect_top, 0);
    CoInitialize(NULL);

    /* Both object ids, because asking the wrong one is the obvious way to
     * conclude MSAA is poor when it is not. OBJID_WINDOW is the frame --
     * caption, menu, scrollbars. OBJID_CLIENT is the control itself, and it
     * is where an edit's contents and a checkbox's state actually live.
     * Most MSAA clients ask for CLIENT; the first pass here asked for
     * WINDOW and reported value=0 across 48 nodes, which is exactly the
     * result a wrong objid produces. */
    msaa_pass(aofw, OBJID_WINDOW, "OBJID_WINDOW (frame)");
    msaa_pass(aofw, OBJID_CLIENT, "OBJID_CLIENT (the control)");

    CoUninitialize();
    FreeLibrary(acc);
}

/* ------------------------------------------------------------ 0.13d */
/* Can we read a web page as a control tree, or only as pixels?
 *
 * 0.13c walked HWNDs and found `chk 0` with a checked checkbox plainly on
 * screen. That is not MSAA being poor, it is the question being wrong: IE
 * renders the whole document into ONE window of class
 * `Internet Explorer_Server`, so no amount of EnumChildWindows reaches a
 * link or an input. The DOM is reachable only by asking that window's
 * accessible object for its CHILDREN and recursing -- which is a different
 * traversal from the HWND tree and has to be written as one.
 *
 * If this returns the headings, links and form controls, a browser is
 * readable exactly and cheaply and the screenshot path is not needed for
 * it. If it returns one opaque node, a browser is pixels and OCR.
 */
typedef HRESULT (WINAPI *PFN_ACHILDREN)(IAccessible *, LONG, LONG,
                                        VARIANT *, LONG *);
typedef UINT (WINAPI *PFN_ROLETEXT)(DWORD, LPSTR, UINT);

static PFN_ACHILDREN g_achildren;
static PFN_ROLETEXT  g_roletext;
static int g_dom_n;
static int g_dom_shown;

static void dom_fields(IAccessible *acc, VARIANT child,
                       char *name, int ncap, char *val, int vcap, long *role)
{
    BSTR b = 0;
    VARIANT vr;
    name[0] = val[0] = '\0';
    *role = 0;
    if (acc->lpVtbl->get_accName(acc, child, &b) == S_OK && b) {
        bstr_narrow(b, name, ncap); SysFreeString(b); b = 0;
    }
    if (acc->lpVtbl->get_accValue(acc, child, &b) == S_OK && b) {
        bstr_narrow(b, val, vcap); SysFreeString(b); b = 0;
    }
    V_VT(&vr) = VT_EMPTY;
    if (acc->lpVtbl->get_accRole(acc, child, &vr) == S_OK && V_VT(&vr) == VT_I4)
        *role = V_I4(&vr);
}

static void dom_emit(const char *name, const char *val, long role, int depth)
{
    char rt[48];
    rt[0] = '\0';
    if (g_roletext) g_roletext((DWORD)role, rt, sizeof(rt));
    g_dom_shown++;
    say("%*s%-12.12s|%-26.26s|%.22s", depth, "",
        rt[0] ? rt : "?", name[0] ? name : "-", val[0] ? val : "");
}

static void dom_walk(IAccessible *acc, int depth)
{
    long kids = 0, got = 0, i;
    VARIANT *v;

    if (depth > 10 || g_dom_n > 300) return;
    if (acc->lpVtbl->get_accChildCount(acc, &kids) != S_OK || kids <= 0) return;
    v = (VARIANT *)HeapAlloc(GetProcessHeap(), 0, sizeof(VARIANT) * kids);
    if (!v) return;
    if (g_achildren(acc, 0, kids, v, &got) < 0) {
        HeapFree(GetProcessHeap(), 0, v);
        return;
    }
    for (i = 0; i < got; i++) {
        char name[64], val[64];
        long role = 0;
        g_dom_n++;
        if (V_VT(&v[i]) == VT_DISPATCH && V_DISPATCH(&v[i])) {
            /* A real object: it can have children of its own. */
            IAccessible *kid = 0;
            IDispatch *d = V_DISPATCH(&v[i]);
            if (d->lpVtbl->QueryInterface(d, &RC_IID_IAccessible,
                                          (void **)&kid) == S_OK && kid) {
                VARIANT self;
                V_VT(&self) = VT_I4; V_I4(&self) = 0;
                dom_fields(kid, self, name, sizeof(name), val, sizeof(val),
                           &role);
                dom_emit(name, val, role, depth);
                dom_walk(kid, depth + 1);
                kid->lpVtbl->Release(kid);
            }
            d->lpVtbl->Release(d);
        } else if (V_VT(&v[i]) == VT_I4) {
            /* A leaf the PARENT describes: ask the parent, using the id. */
            dom_fields(acc, v[i], name, sizeof(name), val, sizeof(val), &role);
            dom_emit(name, val, role, depth);
        }
    }
    HeapFree(GetProcessHeap(), 0, v);
}

static BOOL CALLBACK find_ie(HWND h, LPARAM lp)
{
    char cls[64];
    HWND *out = (HWND *)lp;
    GetClassNameA(h, cls, sizeof(cls));
    if (!strcmp(cls, "Internet Explorer_Server")) { *out = h; return FALSE; }
    { HWND c;
      for (c = GetWindow(h, GW_CHILD); c; c = GetWindow(c, GW_HWNDNEXT)) {
          if (!find_ie(c, lp)) return FALSE;
      } }
    return TRUE;
}

static void probe_dom(void)
{
    HMODULE acc;
    PFN_AOFW aofw;
    HWND ie = 0;
    DWORD t0, t1;

    head("0.13d  is a web page a control tree, or just pixels?");

    acc = LoadLibraryA("oleacc.dll");
    aofw = acc ? (PFN_AOFW)GetProcAddress(acc, "AccessibleObjectFromWindow") : 0;
    g_achildren = acc ? (PFN_ACHILDREN)GetProcAddress(acc, "AccessibleChildren")
                      : 0;
    g_roletext = acc ? (PFN_ROLETEXT)GetProcAddress(acc, "GetRoleTextA") : 0;
    if (!aofw || !g_achildren) {
        say("oleacc: aofw=%p children=%p", (void *)aofw, (void *)g_achildren);
        if (acc) FreeLibrary(acc);
        return;
    }
    say("GetRoleTextA %s", g_roletext ? "found" : "MISSING");

    EnumWindows(find_ie, (LPARAM)&ie);
    if (!ie) {
        say("no 'Internet Explorer_Server' window -- open a page first");
        FreeLibrary(acc);
        return;
    }

    CoInitialize(NULL);
    {
        IAccessible *doc = 0;
        if (aofw(ie, OBJID_CLIENT, &RC_IID_IAccessible, (void **)&doc) == S_OK
            && doc) {
            char name[64], val[64];
            long role = 0;
            VARIANT self;
            V_VT(&self) = VT_I4; V_I4(&self) = 0;
            dom_fields(doc, self, name, sizeof(name), val, sizeof(val), &role);
            say("document       : role=%ld name=%.28s", role, name);
            g_dom_n = g_dom_shown = 0;
            t0 = GetTickCount();
            dom_walk(doc, 1);
            t1 = GetTickCount();
            say("DOM nodes %d in %lu ms (showing first %d)",
                g_dom_n, (unsigned long)(t1 - t0), g_dom_shown);
            doc->lpVtbl->Release(doc);
        } else {
            say("AccessibleObjectFromWindow failed on the IE server window");
        }
    }
    CoUninitialize();
    FreeLibrary(acc);
}

/* ------------------------------------------------------------- 0.12b */
/* The `max_bpp` ladder: what each rung costs and what it buys.
 *
 * The capture depth is `min(screen_bpp, max_bpp)`, so one knob covers both
 * "a picture for a person to look at" and "just enough to drive the UI".
 * It also picks the codec, because BMP only has a compressed form at 4 and
 * 8 bits -- BI_RLE4 and BI_RLE8 -- and GDI produces those itself. At 16 and
 * above there is no RLE in the format and deflate is the only option.
 *
 * Both are measured here for every rung, against the SAME strip, so the
 * lossless-deflate and lossy-RLE answers are comparable. Deflate runs at
 * level 1, which is what the guest would use outbound.
 *
 * A strip rather than a full frame: capture streams in strips anyway, and a
 * 32bpp full screen is 1.9MB of static buffer this program should not have.
 */
static void probe_depths(void)
{
    static unsigned char raw[512 * 1024];
    static unsigned char packed[512 * 1024];
    static unsigned char info[sizeof(BITMAPINFOHEADER) + 256 * 4];
    static const int DEPTHS[5] = { 4, 8, 16, 24, 32 };
    BITMAPINFOHEADER *bi = (BITMAPINFOHEADER *)info;
    const int ROWS = 128;
    HDC screen, mem;
    HBITMAP strip, oldbmp;
    int w, h, native, k;

    head("0.12b  max_bpp ladder: size and cost per rung");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);
    native = GetDeviceCaps(screen, BITSPIXEL);
    say("screen %dx%d at %dbpp; strip of %d rows", w, h, native, ROWS);
    say("bpp  BI_RGB bytes   ms  deflate  ms   RLE bytes  ms");

    mem = CreateCompatibleDC(screen);
    strip = CreateCompatibleBitmap(screen, w, ROWS);
    oldbmp = (HBITMAP)SelectObject(mem, strip);
    BitBlt(mem, 0, 0, w, ROWS, screen, 0, h / 3, SRCCOPY);

    for (k = 0; k < 5; k++) {
        int bpp = DEPTHS[k];
        unsigned long stride = (((unsigned long)w * bpp + 31) / 32) * 4;
        unsigned long want = stride * ROWS;
        DWORD t0, t1;
        int lines;
        unsigned long rgb_ms = 0, def_ms = 0, rle_ms = 0;
        mz_ulong clen = sizeof(packed);
        unsigned long rle_bytes = 0;

        if (want > sizeof(raw)) { say("%2d   (strip too big)", bpp); continue; }

        memset(info, 0, sizeof(info));
        bi->biSize = sizeof(BITMAPINFOHEADER);
        bi->biWidth = w; bi->biHeight = ROWS; bi->biPlanes = 1;
        bi->biBitCount = (WORD)bpp; bi->biCompression = BI_RGB;
        t0 = GetTickCount();
        lines = GetDIBits(mem, strip, 0, ROWS, raw, (BITMAPINFO *)info,
                          DIB_RGB_COLORS);
        t1 = GetTickCount();
        rgb_ms = t1 - t0;
        if (lines != ROWS) { say("%2d   FAILED (%d lines)", bpp, lines); continue; }

        t0 = GetTickCount();
        if (mz_compress2(packed, &clen, raw, want, 1) != MZ_OK) clen = 0;
        t1 = GetTickCount();
        def_ms = t1 - t0;

        if (bpp == 4 || bpp == 8) {
            memset(info, 0, sizeof(info));
            bi->biSize = sizeof(BITMAPINFOHEADER);
            bi->biWidth = w; bi->biHeight = ROWS; bi->biPlanes = 1;
            bi->biBitCount = (WORD)bpp;
            bi->biCompression = (bpp == 8) ? BI_RLE8 : BI_RLE4;
            bi->biSizeImage = sizeof(packed);
            t0 = GetTickCount();
            lines = GetDIBits(mem, strip, 0, ROWS, packed,
                              (BITMAPINFO *)info, DIB_RGB_COLORS);
            t1 = GetTickCount();
            rle_ms = t1 - t0;
            rle_bytes = (lines == ROWS) ? bi->biSizeImage : 0;
        }

        say("%2d %11lu %5lu %8lu %4lu %10lu %4lu",
            bpp, want, rgb_ms, (unsigned long)clen, def_ms,
            rle_bytes, rle_ms);
    }

    SelectObject(mem, oldbmp);
    DeleteObject(strip);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
    say("(0 in an RLE column = the format has none at that depth, or refused)");
}

/* -------------------------------------------------------------------- */
int main(int argc, char **argv)
{
    const char *only = (argc > 1) ? argv[1] : NULL;

    /* Append, not truncate: the sections are usually run one at a time
     * ("probe threads", "probe exec", ...) and a truncating open means the
     * log only ever holds whichever section ran last. */
    g_log = fopen("PROBE.LOG", "a");
    /* Expected to fail on this guest, and kept because the failure is the
     * useful part: it prints "no COM1" and tells you to redirect instead.
     *
     * Win32 cannot open the port here. fopen("COM1","w") cannot -- "w" is
     * CREATE_ALWAYS underneath and a character device cannot be created --
     * and CreateFile with OPEN_EXISTING cannot either, because Win98 never
     * bound a driver to the port QEMU added. Meanwhile `ECHO x > COM1` from
     * the same DOS box works, because DOS device redirection reaches the
     * hardware without going through the Windows COM driver at all.
     *
     * So the way to get a run off the machine is the SHELL's redirection,
     * not this handle:  D:\PROBE.EXE <section> > COM1  -- which streams the
     * whole run to vm/serial.log on the host as it happens. */
    g_com = CreateFileA("COM1", GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0,
                        NULL);
    say("RetroClaude Phase 0 probe");
    say("serial out: %s",
        g_com != INVALID_HANDLE_VALUE ? "COM1 open" : "no COM1");
    say("built %s %s", __DATE__, __TIME__);

    if (!only || !strcmp(only, "platform")) probe_platform();
    if (!only || !strcmp(only, "threads"))  probe_threads();
    if (!only || !strcmp(only, "exec"))     probe_exec();
    if (!only || !strcmp(only, "screen"))   probe_screen();
    if (!only || !strcmp(only, "deflate"))  probe_deflate();
    if (!only || !strcmp(only, "html"))     probe_html();
    if (!only || !strcmp(only, "watch"))    probe_watch();
    if (!only || !strcmp(only, "automation")) probe_automation();
    if (!only || !strcmp(only, "uiwalk"))   probe_uiwalk();
    if (!only || !strcmp(only, "msaa"))     probe_msaa();
    if (!only || !strcmp(only, "dom"))      probe_dom();
    if (!only || !strcmp(only, "depths"))   probe_depths();

    say("");
    say("=== done -- results also in PROBE.LOG ===");
    if (g_log) fclose(g_log);
    if (g_com != INVALID_HANDLE_VALUE) CloseHandle(g_com);
    return 0;
}
