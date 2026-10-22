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
 *   probe power      has this box SetThreadExecutionState, and does a running
 *                    gremote refuse a suspend when one is put to it
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "core/str.h"
#include "inproc/shm.h"
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

    rc_strf(cmd, sizeof(cmd), "%s /c echo GASTON_MARKER", shell);
    try_cmd("echo through the shell", cmd, "GASTON_MARKER");

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

/* Build a path to `name` in this exe's own directory.
 *
 * A bare fopen("CORPUS.TXT") would resolve against the CURRENT directory,
 * which is the kit folder when TEST.BAT runs it and something else entirely
 * when it is started from a DOS prompt elsewhere. The file not being found
 * then looks exactly like the file not being shipped. Returns 0 if the path
 * does not fit, which on this platform means something is very wrong. */
static int beside_exe(const char *name, char *out, unsigned cap)
{
    char path[MAX_PATH];
    unsigned cut = 0, i;

    path[0] = '\0';
    if (!GetModuleFileNameA(NULL, path, sizeof(path)))
        return 0;
    for (i = 0; path[i]; i++)
        if (path[i] == '\\' || path[i] == '/' || path[i] == ':')
            cut = i + 1;
    if (cut + strlen(name) + 1 > cap)
        return 0;
    memcpy(out, path, cut);
    return rc_strcpy(out + cut, cap - cut, name);
}

/* ------------------------------------------------------------------ 0.5c */
/* Every threshold in the adaptive-codec design rests on these numbers, and so
 * far they are estimates from a modern CPU. */
static void probe_deflate(void)
{
    static char src[256 * 1024];
    static unsigned char comp[300 * 1024];
    static unsigned char dec[256 * 1024];
    /* THE CORPUS IS READ FROM DISK, AND THERE IS NO FALLBACK.
     *
     * This used to synthesise its sample: one two-line block repeated, which
     * compresses ~164:1 because deflate on near-pure repetition barely has to
     * search. On the guest that gave 7707 KB/s at level 1 and the comment
     * here said in as many words that it was NOT a usable threshold -- and
     * the number got quoted anyway, for a year, across four documents.
     *
     * So the sample is now CORPUS.TXT beside the exe: this project's own
     * prose and code, 2.76:1, built by tools/gen/mkcorpus.py and carried on
     * the floppy by tools/gen/mkw98kit.sh. If it is absent this section
     * REFUSES rather than falling back, because a fallback is exactly how the
     * unusable number came to be believed: a run with no corpus would print a
     * plausible figure under the same heading, and nothing downstream could
     * tell the two apart. A refusal cannot be misread.
     *
     * src[] is 256 KB and the corpus is sized to it rather than the reverse:
     * growing this static costs committed-at-load bytes on a 32 MB machine. */
    char corpus[MAX_PATH];
    FILE *cf;
    mz_ulong clen, dlen;
    unsigned total = 0;
    DWORD t0, t1;
    int rc, lvl;

    head("0.5c  deflate throughput on this CPU");

    if (!beside_exe("CORPUS.TXT", corpus, sizeof(corpus))) {
        say("REFUSED: cannot build a path to CORPUS.TXT");
        return;
    }
    cf = fopen(corpus, "rb");
    if (!cf) {
        say("REFUSED: no CORPUS.TXT beside this exe (%s).", corpus);
        say("  0.5c cannot close without it. The synthetic sample this");
        say("  section used to build compressed 164:1 and its throughput was");
        say("  never a usable threshold, so there is deliberately no");
        say("  fallback. Copy CORPUS.TXT from the kit, or rebuild it with");
        say("  python3 tools/gen/mkcorpus.py.");
        return;
    }
    total = (unsigned)fread(src, 1, sizeof(src) - 1, cf);
    fclose(cf);
    if (total < 64 * 1024) {
        say("REFUSED: CORPUS.TXT is only %u bytes; under 64K the timings sit",
            total);
        say("  inside GetTickCount's own resolution and say nothing.");
        return;
    }
    src[total] = '\0';
    /* The CRC makes two runs comparable, and that is not decoration: the
     * corpus is derived from eleven files in the tree, so it moves whenever
     * one of them is edited. A throughput figure is only comparable against
     * another run of the same bytes, and the machines this gate needs are
     * measured days apart. tools/gen/mkcorpus.py prints the same number. */
    say("corpus         : %u bytes from CORPUS.TXT, crc32 %08lx", total,
        (unsigned long)mz_crc32(MZ_CRC32_INIT, (const unsigned char *)src,
                                total));

    /* A DISCARDED WARM PASS, and it is not ceremony.
     *
     * Timed cold, the FIRST level measured pays for miniz's internal
     * allocation and every page fault behind it, and the second gets a warm
     * heap for free. That reported 184ms at level 1 on the XP against 94ms
     * on the SLOWER P200, with level 6 at "1ms" -- an ordering artifact read
     * as a measurement, and the same bug the MSAA probe had. */
    clen = sizeof(comp);
    mz_compress2(comp, &clen, (const unsigned char *)src, total, 6);
    clen = sizeof(comp);
    mz_compress2(comp, &clen, (const unsigned char *)src, total, 1);

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
    mz_uncompress(dec, &dlen, comp, clen);    /* warm, discarded */
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
    /* Save and RESTORE. The first version of this set the timeout to 0 and
     * left it there; it is a registry value, so it survived a reboot and
     * the original 15000 could never be measured again. A probe that cannot
     * be run twice is not a measurement, and changing the machine you are
     * observing is the bug this whole section is about. */
    {
        DWORD before = 0, after = 0, back = 0;
        BOOL rg = SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                        &before, 0);
        BOOL rs = SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                        (void *)0, SPIF_SENDCHANGE);
        SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &after, 0);
        SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                              (void *)(UINT_PTR)before, SPIF_SENDCHANGE);
        SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &back, 0);
        say("fg lock        : was=%lu (get=%d) set0=%d -> %lu, restored=%lu",
            (unsigned long)before, (int)rg, (int)rs,
            (unsigned long)after, (unsigned long)back);
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

/* Find the DOCUMENT by its MSAA role, never by a browser's window class.
 *
 * The old version looked for "Internet Explorer_Server", which is IE by
 * construction -- and Win98 had Netscape 4, Netscape 6/Mozilla and Opera as
 * well, each with its own internals. ROLE_SYSTEM_DOCUMENT is 15 on all of
 * them, in every language, and a browser that answers nothing here is not a
 * failure: it is the boundary between this tier and GDI capture, which is
 * worth knowing before a policy depends on it.
 *
 * Every document found is reported with the class that carried it, because
 * that list is the actual result of this gate.
 */
#define RC_ROLE_DOCUMENT 15L
#define FIND_DOCS_MAX    8
#define FIND_DEPTH_MAX   16

static PFN_AOFW g_find_aofw;

struct FoundDoc {
    HWND  hwnd;
    char  cls[64];
    DWORD pid;
    long  role;
};

static struct FoundDoc g_docs[FIND_DOCS_MAX];
static int   g_docs_n;
static int   g_docs_asked;

/* The role of a window's client object, or -1. */
static long role_of(HWND h)
{
    IAccessible *a = 0;
    long role = -1;
    if (!g_find_aofw)
        return -1;
    g_docs_asked++;
    if (g_find_aofw(h, OBJID_CLIENT, &RC_IID_IAccessible, (void **)&a) == S_OK
        && a) {
        VARIANT self, v;
        V_VT(&self) = VT_I4; V_I4(&self) = 0;
        V_VT(&v) = VT_EMPTY;
        if (a->lpVtbl->get_accRole(a, self, &v) == S_OK && V_VT(&v) == VT_I4)
            role = V_I4(&v);
        VariantClear(&v);
        a->lpVtbl->Release(a);
    }
    return role;
}

/* Depth-capped, unlike the version this replaces: it recursed GW_CHILD with
 * no bound at all, and a cyclic sibling chain would have run until the
 * stack gave out. */
static void find_docs(HWND parent, int depth)
{
    HWND c;
    if (depth > FIND_DEPTH_MAX || g_docs_n >= FIND_DOCS_MAX)
        return;
    for (c = GetWindow(parent, GW_CHILD); c; c = GetWindow(c, GW_HWNDNEXT)) {
        long role;
        if (g_docs_n >= FIND_DOCS_MAX)
            return;
        if (!IsWindowVisible(c))
            continue;
        role = role_of(c);
        if (role == RC_ROLE_DOCUMENT) {
            struct FoundDoc *d = &g_docs[g_docs_n++];
            d->hwnd = c;
            /* Cleared first. GetClassNameA leaves the buffer untouched on
             * failure and the old code strcmp'd whatever was on the stack. */
            d->cls[0] = '\0';
            GetClassNameA(c, d->cls, sizeof(d->cls));
            d->pid = 0;
            GetWindowThreadProcessId(c, &d->pid);
            d->role = role;
            continue;       /* do not descend INTO a document */
        }
        find_docs(c, depth + 1);
    }
}

static BOOL CALLBACK find_docs_top(HWND h, LPARAM lp)
{
    (void)lp;
    if (g_docs_n >= FIND_DOCS_MAX)
        return FALSE;
    if (!IsWindowVisible(h))
        return TRUE;
    find_docs(h, 1);
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

    /* CoInitialize BEFORE the search: role_of makes COM calls too, and the
     * search is now most of this gate's work. */
    CoInitialize(NULL);

    g_find_aofw = aofw;
    g_docs_n = g_docs_asked = 0;
    t0 = GetTickCount();
    EnumWindows(find_docs_top, 0);
    t1 = GetTickCount();
    say("document search: %d found, %d windows asked, %lu ms",
        g_docs_n, g_docs_asked, (unsigned long)(t1 - t0));
    if (!g_docs_n) {
        say("no window reports ROLE_SYSTEM_DOCUMENT -- open a page first.");
        say("  If a browser IS open and showing a page, that is the RESULT:");
        say("  this browser exposes no MSAA document and belongs to the GDI");
        say("  capture tier, not this one. Say which browser in the log.");
        CoUninitialize();
        FreeLibrary(acc);
        return;
    }
    /* THE ANSWER OF THIS GATE. One line per document, with the class that
     * carried it -- that list is what tells us whether the cross-browser
     * claim holds, and it must never become a class we then hard-code. */
    {
        int di;
        for (di = 0; di < g_docs_n; di++)
            say("  document %d   : class=%s hwnd=%p pid=%lu", di,
                g_docs[di].cls, (void *)g_docs[di].hwnd,
                (unsigned long)g_docs[di].pid);
    }
    ie = g_docs[0].hwnd;

    {
        IAccessible *doc = 0;
        if (aofw(ie, OBJID_CLIENT, &RC_IID_IAccessible, (void **)&doc) == S_OK
            && doc) {
            char name[64], val[64];
            long role = 0;
            VARIANT self;
            V_VT(&self) = VT_I4; V_I4(&self) = 0;
            dom_fields(doc, self, name, sizeof(name), val, sizeof(val), &role);
            say("document       : role=%ld name=%.40s", role, name);
            /* Say WHICH document, loudly. The first run of this walked
             * Explorer's folder view -- also MSHTML, so it produced a
             * perfectly good-looking tree -- while the page it was supposed
             * to test had been closed. The result was not wrong, it was
             * about something else, which is worse. */
            if (!strstr(name, "UITEST") && !strstr(name, "uitest")) {
                say("  *** NOT the test page. Open D:\\UITEST.HTM in IE and");
                say("  *** run this again; what follows is some other");
                say("  *** document that happened to enumerate first.");
            }
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
    /* 64, not 128. At 32bpp a 128-row strip of a 1280x1024 screen is
     * 655,360 bytes and overflows `raw`, and the overflow check skips the
     * rung silently -- so the widest displays, which are exactly the ones
     * where the size question matters most, would have reported nothing.
     * 64 rows keeps every rung inside the buffer up to 1600x1200. */
    const int ROWS = 64;
    HDC screen, mem;
    HBITMAP strip, oldbmp;
    int w, h, native, k, yi;

    head("0.12b  max_bpp ladder: size and cost per rung");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);
    native = GetDeviceCaps(screen, BITSPIXEL);
    say("screen %dx%d at %dbpp; strip of %d rows", w, h, native, ROWS);
    /* Level 1 is what the guest would use outbound; level 6 is what the
     * bridge uses the other way. Both, so the size left on the table by
     * choosing the fast one is visible rather than assumed. */
    say("bpp      raw  RGBms   z1 by  z1ms   z6 by  z6ms   RLE by RLEms");

    mem = CreateCompatibleDC(screen);
    strip = CreateCompatibleBitmap(screen, w, ROWS);
    oldbmp = (HBITMAP)SelectObject(mem, strip);

    /* Four bands down the screen, not one. The first version sampled a
     * single strip of mostly-flat desktop and concluded deflate beats RLE
     * by 2.7x, which is a claim about that band and not about screenshots.
     * Flat colour and dense text compress nothing alike, and a capture
     * policy is chosen for the worst band, not the best. */
    for (yi = 0; yi < 4; yi++) {
    int ytop = (h - ROWS) * yi / 3;
    BitBlt(mem, 0, 0, w, ROWS, screen, 0, ytop, SRCCOPY);
    say("-- strip at y=%d --", ytop);

    for (k = 0; k < 5; k++) {
        int bpp = DEPTHS[k];
        unsigned long stride = (((unsigned long)w * bpp + 31) / 32) * 4;
        unsigned long want = stride * ROWS;
        DWORD t0, t1;
        int lines;
        unsigned long rgb_ms = 0, z1_ms = 0, z6_ms = 0, rle_ms = 0;
        mz_ulong clen = sizeof(packed), clen6 = sizeof(packed);
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
        z1_ms = t1 - t0;

        t0 = GetTickCount();
        if (mz_compress2(packed, &clen6, raw, want, 6) != MZ_OK) clen6 = 0;
        t1 = GetTickCount();
        z6_ms = t1 - t0;

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

        say("%2d %8lu %6lu %7lu %5lu %7lu %5lu %8lu %5lu",
            bpp, want, rgb_ms, (unsigned long)clen, z1_ms,
            (unsigned long)clen6, z6_ms, rle_bytes, rle_ms);
    }
    }   /* strip */

    SelectObject(mem, oldbmp);
    DeleteObject(strip);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
    say("(0 in an RLE column = the format has none at that depth, or refused)");
}

/* ------------------------------------------------------------ 0.12c */
/* Does the RLE8 stream actually DECODE, or did the call merely succeed?
 *
 * 0.12 checked that GetDIBits returned the row count it was asked for and a
 * biSizeImage inside the buffer. That is not the same claim at all: a codec
 * that returned success and garbage passes it, and the whole strip-capture
 * design rests on the answer. The check was cheap and wrong and this
 * replaces it.
 *
 * The test is byte-level and happens off the machine. Three BMPs of the
 * SAME strip go to the floppy:
 *
 *   S24.BMP   24bpp BI_RGB   -- ground truth, nothing clever in the path
 *   S8.BMP     8bpp BI_RGB   -- the fallback if RLE is no good
 *   S8R.BMP    8bpp BI_RLE8  -- the thing under test
 *
 * The host opens all three with Pillow and asserts S8 and S8R decode to
 * IDENTICAL pixels. They are compared after decoding rather than as index
 * bytes, because GetDIBits picks the colour table per call and the two are
 * free to differ without either being wrong.
 */
static int write_bmp(const char *path, const BITMAPINFOHEADER *bih,
                     const RGBQUAD *pal, int palcount,
                     const unsigned char *bits, unsigned long nbits)
{
    BITMAPFILEHEADER bfh;
    FILE *f = fopen(path, "wb");
    unsigned long off;
    if (!f) return 0;
    off = sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER)
        + (unsigned long)palcount * sizeof(RGBQUAD);
    memset(&bfh, 0, sizeof(bfh));
    bfh.bfType = 0x4D42;                              /* 'BM' */
    bfh.bfOffBits = off;
    bfh.bfSize = off + nbits;
    fwrite(&bfh, sizeof(bfh), 1, f);
    fwrite(bih, sizeof(BITMAPINFOHEADER), 1, f);
    if (palcount > 0) fwrite(pal, sizeof(RGBQUAD), palcount, f);
    fwrite(bits, 1, nbits, f);
    fclose(f);
    return 1;
}

static void probe_verify(void)
{
    static unsigned char bits[512 * 1024];
    static unsigned char info[sizeof(BITMAPINFOHEADER) + 256 * 4];
    BITMAPINFOHEADER *bi = (BITMAPINFOHEADER *)info;
    RGBQUAD *pal = (RGBQUAD *)(info + sizeof(BITMAPINFOHEADER));
    const int ROWS = 64;
    HDC screen, mem;
    HBITMAP strip, oldbmp;
    int w, h, lines;
    unsigned long stride;

    head("0.12c  does the RLE8 stream decode? (writes BMPs beside the exe)");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);
    say("screen %dx%d at %dbpp, strip %d rows at y=%d",
        w, h, GetDeviceCaps(screen, BITSPIXEL), ROWS, h / 3);

    mem = CreateCompatibleDC(screen);
    strip = CreateCompatibleBitmap(screen, w, ROWS);
    oldbmp = (HBITMAP)SelectObject(mem, strip);
    BitBlt(mem, 0, 0, w, ROWS, screen, 0, h / 3, SRCCOPY);

    /* Ground truth. */
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = ROWS; bi->biPlanes = 1;
    bi->biBitCount = 24; bi->biCompression = BI_RGB;
    stride = (((unsigned long)w * 24 + 31) / 32) * 4;
    lines = GetDIBits(mem, strip, 0, ROWS, bits, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    bi->biSizeImage = stride * ROWS;
    say("S24.BMP  %d lines, %lu bytes, written %d",
        lines, stride * ROWS,
        write_bmp("S24.BMP", bi, 0, 0, bits, stride * ROWS));

    /* 8bpp uncompressed, palette and all. */
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = ROWS; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RGB;
    stride = (((unsigned long)w * 8 + 31) / 32) * 4;
    lines = GetDIBits(mem, strip, 0, ROWS, bits, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    bi->biSizeImage = stride * ROWS;
    bi->biClrUsed = 256;
    say("S8.BMP   %d lines, %lu bytes, written %d",
        lines, stride * ROWS,
        write_bmp("S8.BMP", bi, pal, 256, bits, stride * ROWS));

    /* The one under test. biSizeImage comes back as the COMPRESSED size,
     * and that is what the file header must carry. */
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = ROWS; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RLE8;
    bi->biSizeImage = sizeof(bits);
    lines = GetDIBits(mem, strip, 0, ROWS, bits, (BITMAPINFO *)info,
                      DIB_RGB_COLORS);
    bi->biClrUsed = 256;
    say("S8R.BMP  %d lines, %lu bytes, written %d",
        lines, (unsigned long)bi->biSizeImage,
        write_bmp("S8R.BMP", bi, pal, 256, bits, bi->biSizeImage));

    SelectObject(mem, oldbmp);
    DeleteObject(strip);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
    say("now decode all three on the host and compare S8 against S8R");
}

/* ------------------------------------------------------------- 0.15 */
/* Can we be TOLD when the UI changes, instead of asking?
 *
 * Everything so far polls: walk the windows, read them, walk them again.
 * That is the expensive way and it cannot say WHEN something changed, only
 * that it is different now. Three mechanisms might replace it, in
 * decreasing order of how much they would buy and increasing order of how
 * invasive they are:
 *
 *   SetWinEventHook   the right answer if it exists. MSAA's own
 *                     notification channel: the system tells us a control
 *                     got focus, changed value, was created or destroyed.
 *                     Documented for 2000+ in USER32; MSAA 1.3 for 9x is
 *                     supposed to add it. Nobody here has checked.
 *
 *   SetWindowsHookEx  ancient and certainly present, but a GLOBAL hook has
 *                     to live in a DLL that the system maps into every
 *                     process. This section proves the local case works
 *                     and measures it; the injected case needs that DLL
 *                     and is tested separately.
 *
 *   WaitForInputIdle  not a change notification at all, but the readiness
 *                     question: has the app finished chewing on what we
 *                     sent it and gone back to its message loop.
 *
 * Availability first, because two of the three are only rumoured to be on
 * this OS, and everything downstream is shaped by which survive.
 */
/* HWINEVENTHOOK and WINEVENTPROC live behind WINVER >= 0x0500 and the
 * build pins 0x0400, so they are spelled out here. The handle is opaque. */
typedef HANDLE RC_HWINEVENTHOOK;
typedef void (CALLBACK *RC_WINEVENTPROC)(RC_HWINEVENTHOOK, DWORD, HWND,
                                         LONG, LONG, DWORD, DWORD);
typedef RC_HWINEVENTHOOK (WINAPI *PFN_SETWINEVENT)(DWORD, DWORD, HMODULE,
                                                   RC_WINEVENTPROC, DWORD,
                                                   DWORD, DWORD);
typedef BOOL (WINAPI *PFN_UNHOOKWINEVENT)(RC_HWINEVENTHOOK);
typedef DWORD (WINAPI *PFN_WAITIDLE)(HANDLE, DWORD);

static int g_hook_calls;
static int g_cbt_calls;

static LRESULT CALLBACK cwp_hook(int code, WPARAM w, LPARAM l)
{
    if (code >= 0) g_hook_calls++;
    return CallNextHookEx(0, code, w, l);
}

static LRESULT CALLBACK cbt_hook(int code, WPARAM w, LPARAM l)
{
    if (code >= 0) g_cbt_calls++;
    return CallNextHookEx(0, code, w, l);
}

static LRESULT CALLBACK hook_sink_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    return DefWindowProc(h, m, w, l);
}

static void probe_hooks(void)
{
    HMODULE u32, k32;
    PFN_SETWINEVENT setwinevent;
    PFN_UNHOOKWINEVENT unhookwinevent;
    PFN_WAITIDLE waitidle;
    WNDCLASSA wc;
    HWND sink;
    HHOOK h;
    DWORD t0, t1;
    int i;
    const int N = 2000;

    head("0.15  can we be told, instead of asking?");

    u32 = GetModuleHandleA("user32.dll");
    k32 = GetModuleHandleA("kernel32.dll");
    setwinevent = (PFN_SETWINEVENT)GetProcAddress(u32, "SetWinEventHook");
    unhookwinevent = (PFN_UNHOOKWINEVENT)GetProcAddress(u32, "UnhookWinEvent");
    /* USER32 first. WaitForInputIdle is a USER32 export, not a KERNEL32
     * one -- it has to be, since "is this app idle" is a question about a
     * message queue. Looking only in kernel32 reported it absent on a
     * machine that has it, which is the same shape of mistake as asking
     * MSAA for OBJID_WINDOW and concluding it knows nothing. */
    waitidle = (PFN_WAITIDLE)GetProcAddress(u32, "WaitForInputIdle");
    if (!waitidle)
        waitidle = (PFN_WAITIDLE)GetProcAddress(k32, "WaitForInputIdle");

    say("user32!SetWinEventHook  : %s",
        setwinevent ? "PRESENT" : "absent");
    say("user32!UnhookWinEvent   : %s",
        unhookwinevent ? "PRESENT" : "absent");
    say("user32!NotifyWinEvent   : %s",
        GetProcAddress(u32, "NotifyWinEvent") ? "PRESENT" : "absent");
    say("WaitForInputIdle        : %s (user32=%s kernel32=%s)",
        waitidle ? "PRESENT" : "absent",
        GetProcAddress(u32, "WaitForInputIdle") ? "y" : "n",
        GetProcAddress(k32, "WaitForInputIdle") ? "y" : "n");
    say("user32!SetWindowsHookExA: %s",
        GetProcAddress(u32, "SetWindowsHookExA") ? "PRESENT" : "absent");
    if (!setwinevent)
        say("   -> no WinEvents: change detection is polling or a hook DLL.");

    /* A window to send messages to. WS_POPUP, never shown, and messages are
     * SENT not posted, so the proc runs on this thread and no pump is
     * needed -- the same trick tests/guest/windowcheck.cpp uses. */
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)hook_sink_proc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonProbeSink";
    RegisterClassA(&wc);
    sink = CreateWindowExA(0, "GastonProbeSink", "sink", WS_POPUP,
                           0, 0, 10, 10, NULL, NULL, wc.hInstance, NULL);
    if (!sink) { say("could not create the sink window"); return; }

    /* Baseline: N sends with no hook installed. */
    t0 = GetTickCount();
    for (i = 0; i < N; i++) SendMessageA(sink, WM_NULL, 0, 0);
    t1 = GetTickCount();
    say("%d sends, no hook    : %lu ms", N, (unsigned long)(t1 - t0));

    /* The same with a thread-local WH_CALLWNDPROC in the way. The delta is
     * what a hook costs per message, which is the number that decides
     * whether hooking the whole desktop is affordable at 200MHz. */
    g_hook_calls = 0;
    h = SetWindowsHookExA(WH_CALLWNDPROC, cwp_hook, NULL, GetCurrentThreadId());
    if (!h) {
        say("WH_CALLWNDPROC local  : FAILED (%lu)", GetLastError());
    } else {
        t0 = GetTickCount();
        for (i = 0; i < N; i++) SendMessageA(sink, WM_NULL, 0, 0);
        t1 = GetTickCount();
        say("%d sends, hooked     : %lu ms, %d hook calls",
            N, (unsigned long)(t1 - t0), g_hook_calls);
        UnhookWindowsHookEx(h);
    }

    /* WH_CBT is the one that would report window create/destroy/activate,
     * which is the change feed we actually want. Local first. */
    g_cbt_calls = 0;
    h = SetWindowsHookExA(WH_CBT, cbt_hook, NULL, GetCurrentThreadId());
    if (!h) {
        say("WH_CBT local          : FAILED (%lu)", GetLastError());
    } else {
        HWND tmp = CreateWindowExA(0, "GastonProbeSink", "t", WS_POPUP,
                                   0, 0, 10, 10, NULL, NULL, wc.hInstance,
                                   NULL);
        if (tmp) DestroyWindow(tmp);
        say("WH_CBT local          : OK, %d calls for one create+destroy",
            g_cbt_calls);
        UnhookWindowsHookEx(h);
    }

    /* A GLOBAL hook with no DLL. Expected to fail, and the failure is the
     * point: it is what says an injected DLL is not optional. Passing a
     * hook proc that lives in an EXE to a system-wide hook cannot work,
     * because the system has to map the code into every other process. */
    h = SetWindowsHookExA(WH_CBT, cbt_hook, GetModuleHandleA(NULL), 0);
    if (h) {
        /* Win98 ACCEPTS this and the hook never fires -- 0.15b installed
         * the same thing, started another program, pumped for five seconds
         * and counted zero calls, not even for this process. So a returned
         * handle means nothing here, which is a nastier failure than a
         * refusal: the code looks installed and silently watches nothing. */
        say("WH_CBT global, no DLL : handle returned -- but see 0.15b, it");
        say("                        delivers NOTHING. Needs a real DLL.");
        UnhookWindowsHookEx(h);
    } else {
        say("WH_CBT global, no DLL : refused (%lu) -> needs a DLL",
            GetLastError());
    }

    DestroyWindow(sink);
    UnregisterClassA("GastonProbeSink", wc.hInstance);
}

/* ------------------------------------------------------------ 0.15b */
/* Presence is not delivery. 0.15 found SetWinEventHook exported and a
 * system-wide WH_CBT accepted where it was expected to be refused; neither
 * says a single event ever arrives, still less one from ANOTHER process,
 * which is the only kind worth having.
 *
 * So: install both, start a second program, pump for a few seconds, and
 * count what turns up and whose window it was about. The cross-process
 * count is the whole answer. If it is zero for the hook, 9x accepted an
 * installation that only ever watches us, and a real DLL is needed after
 * all. If it is non-zero, injection is a solved problem on this OS and the
 * DLL question goes away.
 *
 * Notepad is the second program: it starts fast, opens one plain window,
 * and has nothing to lose when it is closed again.
 */
#define RC_EVENT_MIN                 0x00000001L
#define RC_EVENT_OBJECT_VALUECHANGE  0x0000800EL
#define RC_WINEVENT_OUTOFCONTEXT     0x0000

static int g_we_all, g_we_other;
static int g_cbt_all, g_cbt_other;
static DWORD g_self_pid;

static int hwnd_is_other(HWND h)
{
    DWORD pid = 0;
    if (!h || !IsWindow(h)) return 0;
    GetWindowThreadProcessId(h, &pid);
    return pid && pid != g_self_pid;
}

static void CALLBACK we_proc(RC_HWINEVENTHOOK hook, DWORD ev, HWND h,
                             LONG obj, LONG child, DWORD tid, DWORD ms)
{
    (void)hook; (void)ev; (void)obj; (void)child; (void)tid; (void)ms;
    g_we_all++;
    if (hwnd_is_other(h)) g_we_other++;
}

static LRESULT CALLBACK cbt_global(int code, WPARAM w, LPARAM l)
{
    if (code >= 0) {
        g_cbt_all++;
        /* For HCBT_CREATEWND / HCBT_ACTIVATE / HCBT_DESTROYWND the wParam
         * is the window in question. Good enough to attribute an owner. */
        if (hwnd_is_other((HWND)w)) g_cbt_other++;
    }
    return CallNextHookEx(0, code, w, l);
}

static void pump_for(DWORD ms)
{
    DWORD until = GetTickCount() + ms;
    MSG msg;
    while ((long)(GetTickCount() - until) < 0) {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        Sleep(10);
    }
}

static void probe_events(void)
{
    HMODULE u32 = GetModuleHandleA("user32.dll");
    PFN_SETWINEVENT setwinevent =
        (PFN_SETWINEVENT)GetProcAddress(u32, "SetWinEventHook");
    PFN_UNHOOKWINEVENT unhookwinevent =
        (PFN_UNHOOKWINEVENT)GetProcAddress(u32, "UnhookWinEvent");
    RC_HWINEVENTHOOK weh = 0;
    HHOOK cbt = 0;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    int started;

    head("0.15b  do those hooks deliver, and from whose process?");
    g_self_pid = GetCurrentProcessId();
    g_we_all = g_we_other = g_cbt_all = g_cbt_other = 0;

    if (setwinevent) {
        /* OUTOFCONTEXT means the callback runs HERE, on our thread, out of
         * the target's message dispatch. That is the safe variant and the
         * one that needs no DLL: the system posts to our queue, which is
         * why the pump below is not optional. */
        weh = setwinevent(RC_EVENT_MIN, RC_EVENT_OBJECT_VALUECHANGE,
                          NULL, we_proc, 0, 0, RC_WINEVENT_OUTOFCONTEXT);
        say("SetWinEventHook install : %s", weh ? "OK" : "FAILED");
    } else {
        say("SetWinEventHook install : not available");
    }

    cbt = SetWindowsHookExA(WH_CBT, cbt_global, GetModuleHandleA(NULL), 0);
    say("WH_CBT system-wide      : %s", cbt ? "installed" : "refused");

    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    started = CreateProcessA(NULL, "notepad.exe", NULL, NULL, FALSE,
                             0, NULL, NULL, &si, &pi);
    say("notepad started         : %s", started ? "yes" : "no");

    pump_for(5000);

    if (started) {
        /* Ask first, insist second. Notepad with nothing typed into it has
         * no reason to argue, but a terminate on a process that was about
         * to show a "save changes?" box would leave a modal on screen for
         * whoever is sitting at the machine. */
        pump_for(500);
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }

    pump_for(500);

    if (weh && unhookwinevent) unhookwinevent(weh);
    if (cbt) UnhookWindowsHookEx(cbt);

    say("WinEvents  : %d total, %d about another process's window",
        g_we_all, g_we_other);
    say("WH_CBT     : %d total, %d about another process's window",
        g_cbt_all, g_cbt_other);
    if (g_we_other > 0) {
        say("   -> WinEvents cross the process boundary. Change detection");
        say("      can be a subscription, not a poll, with no DLL at all.");
    }
    if (cbt && g_cbt_other == 0) {
        say("   -> the system-wide hook installed but only ever saw us,");
        say("      so a real injected DLL would be needed for that route.");
    }
    if (cbt && g_cbt_other > 0) {
        say("   -> the system-wide hook sees other processes WITHOUT a DLL.");
        say("      That is a 9x-ism worth building on, and worth distrusting");
        say("      until it is seen twice.");
    }
}

/* ------------------------------------------------------------ 0.15c */
/* WinEvents deliver. Can they be BUILT ON?
 *
 * Three different questions, and a subscription can pass the first and
 * fail the others:
 *
 *   latency       how long between doing a thing and hearing about it.
 *                 This is the floor for WaitForText / WaitForControl: a
 *                 wait cannot be tighter than the notification it rides.
 *
 *   granularity   WHICH events exist. A stream that only says "something
 *                 changed somewhere" is a poll trigger. One that
 *                 distinguishes a name change from a focus change from a
 *                 dialog appearing is a queryable feed, and the difference
 *                 decides whether WaitForText is cheap or a rescan.
 *
 *   completeness  do N actions produce N events. A feed that drops one in
 *                 ten is worse than no feed, because a wait built on it
 *                 hangs instead of failing, and it hangs rarely, which is
 *                 the worst failure schedule there is.
 *
 * Part A does four known actions on our own window and times each to its
 * event. Part B counts what a second program's lifetime produces, by type.
 */
#define RC_EV_SYS_FOREGROUND   0x0003L
#define RC_EV_SYS_MENUSTART    0x0004L
#define RC_EV_SYS_DIALOGSTART  0x0010L
#define RC_EV_OBJ_CREATE       0x8000L
#define RC_EV_OBJ_DESTROY      0x8001L
#define RC_EV_OBJ_SHOW         0x8002L
#define RC_EV_OBJ_HIDE         0x8003L
#define RC_EV_OBJ_FOCUS        0x8005L
#define RC_EV_OBJ_SELECTION    0x8006L
#define RC_EV_OBJ_STATECHANGE  0x800AL
#define RC_EV_OBJ_LOCATION     0x800BL
#define RC_EV_OBJ_NAMECHANGE   0x800CL
#define RC_EV_OBJ_VALUECHANGE  0x800EL

static const struct { long id; const char *name; } EVNAMES[] = {
    { RC_EV_SYS_FOREGROUND,  "SYS_FOREGROUND" },
    { RC_EV_SYS_MENUSTART,   "SYS_MENUSTART" },
    { RC_EV_SYS_DIALOGSTART, "SYS_DIALOGSTART" },
    { RC_EV_OBJ_CREATE,      "OBJ_CREATE" },
    { RC_EV_OBJ_DESTROY,     "OBJ_DESTROY" },
    { RC_EV_OBJ_SHOW,        "OBJ_SHOW" },
    { RC_EV_OBJ_HIDE,        "OBJ_HIDE" },
    { RC_EV_OBJ_FOCUS,       "OBJ_FOCUS" },
    { RC_EV_OBJ_SELECTION,   "OBJ_SELECTION" },
    { RC_EV_OBJ_STATECHANGE, "OBJ_STATECHANGE" },
    { RC_EV_OBJ_LOCATION,    "OBJ_LOCATION" },
    { RC_EV_OBJ_NAMECHANGE,  "OBJ_NAMECHANGE" },
    { RC_EV_OBJ_VALUECHANGE, "OBJ_VALUECHANGE" }
};
#define EVNAMES_N (sizeof(EVNAMES) / sizeof(EVNAMES[0]))

static int  g_hist[EVNAMES_N];
static int  g_hist_other;
static long g_want_ev;
static HWND g_want_hwnd;
static int  g_want_got;
static DWORD g_want_at;
/* The SYSTEM's own timestamp for the same event -- see lat_proc. */
static DWORD g_want_sysat;

static void CALLBACK lat_proc(RC_HWINEVENTHOOK hook, DWORD ev, HWND h,
                              LONG obj, LONG child, DWORD tid, DWORD ms)
{
    unsigned k;
    int known = 0;
    (void)hook; (void)obj; (void)child; (void)tid;
    for (k = 0; k < EVNAMES_N; k++)
        if ((long)ev == EVNAMES[k].id) { g_hist[k]++; known = 1; break; }
    if (!known) g_hist_other++;
    if (!g_want_got && (long)ev == g_want_ev
        && (g_want_hwnd == NULL || h == g_want_hwnd)) {
        g_want_got = 1;
        /* TWO CLOCKS, AND THE GAP BETWEEN THEM IS THE POINT.
         *
         * `ms` is dwmsEventTime: when the SYSTEM raised the event. Reading
         * GetTickCount() here instead measures when OUR message loop got
         * round to delivering it, which is a pump latency and includes
         * however long this process was busy. PROGRESS.md 0.15c marks the
         * old figures unusable for exactly that reason -- "the system's own
         * event timestamp is discarded and the clock is read inside the
         * callback" -- and `ms` was being voided one line above, so the
         * number it wanted was already on the stack.
         *
         * Both are kept rather than one replaced: the pump figure is what
         * every earlier run recorded, so dropping it would make this
         * incomparable with them, and the DIFFERENCE is the thing worth
         * reading. Same tick base, so they subtract.
         *
         * What this does not fix: both are GetTickCount, whose 9x resolution
         * is about 55 ms. A true latency under one tick reads as 0 either
         * way, and no amount of arithmetic here changes that. */
        g_want_at = GetTickCount();
        g_want_sysat = ms;
    }
}

/* Pump until the awaited event lands or the budget runs out. Returns the
 * wait in ms, or -1 for "never arrived", which is the answer that matters:
 * a missing event is what makes a WaitFor hang. */
/* The same wait measured from the system's own timestamp, set by the call
 * below. A global rather than an out-parameter to match how the rest of this
 * section already carries state out of the callback. */
static long g_sys_d;

static long await_event(long ev, HWND h, DWORD t0, DWORD budget)
{
    DWORD until = GetTickCount() + budget;
    MSG msg;
    g_want_ev = ev; g_want_hwnd = h; g_want_got = 0; g_want_at = 0;
    g_want_sysat = 0; g_sys_d = -1L;
    while (!g_want_got && (long)(GetTickCount() - until) < 0) {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        Sleep(1);
    }
    if (g_want_got)
        g_sys_d = (long)(g_want_sysat - t0);
    return g_want_got ? (long)(g_want_at - t0) : -1L;
}

static void probe_evlat(void)
{
    HMODULE u32 = GetModuleHandleA("user32.dll");
    PFN_SETWINEVENT setwinevent =
        (PFN_SETWINEVENT)GetProcAddress(u32, "SetWinEventHook");
    PFN_UNHOOKWINEVENT unhookwinevent =
        (PFN_UNHOOKWINEVENT)GetProcAddress(u32, "UnhookWinEvent");
    RC_HWINEVENTHOOK weh;
    WNDCLASSA wc;
    HWND w;
    DWORD t0;
    long d;
    unsigned k;
    int hits, tries;

    head("0.15c  WinEvent latency, granularity and completeness");
    if (!setwinevent) { say("SetWinEventHook not available"); return; }

    memset(g_hist, 0, sizeof(g_hist));
    g_hist_other = 0;
    weh = setwinevent(RC_EVENT_MIN, RC_EV_OBJ_VALUECHANGE, NULL, lat_proc,
                      0, 0, RC_WINEVENT_OUTOFCONTEXT);
    if (!weh) { say("hook install failed"); return; }

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)hook_sink_proc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonEvSink";
    RegisterClassA(&wc);

    /* A -- one action, one event, timed. */
    t0 = GetTickCount();
    w = CreateWindowExA(0, "GastonEvSink", "one", WS_OVERLAPPEDWINDOW,
                        40, 40, 200, 120, NULL, NULL, wc.hInstance, NULL);
    d = await_event(RC_EV_OBJ_CREATE, w, t0, 2000);
    say("create  -> OBJ_CREATE     : %ld ms pump, %ld ms system", d, g_sys_d);

    t0 = GetTickCount();
    ShowWindow(w, SW_SHOW);
    d = await_event(RC_EV_OBJ_SHOW, w, t0, 2000);
    say("show    -> OBJ_SHOW       : %ld ms pump, %ld ms system", d, g_sys_d);

    t0 = GetTickCount();
    SetWindowTextA(w, "renamed");
    d = await_event(RC_EV_OBJ_NAMECHANGE, w, t0, 2000);
    say("settext -> OBJ_NAMECHANGE : %ld ms pump, %ld ms system", d, g_sys_d);

    t0 = GetTickCount();
    DestroyWindow(w);
    d = await_event(RC_EV_OBJ_DESTROY, NULL, t0, 2000);
    say("destroy -> OBJ_DESTROY    : %ld ms pump, %ld ms system", d, g_sys_d);

    /* Completeness: the same action twenty times. Nineteen of twenty is a
     * feed you cannot build a wait on. */
    hits = 0;
    for (tries = 0; tries < 20; tries++) {
        HWND t = CreateWindowExA(0, "GastonEvSink", "x", WS_OVERLAPPEDWINDOW,
                                 0, 0, 50, 50, NULL, NULL, wc.hInstance, NULL);
        t0 = GetTickCount();
        if (await_event(RC_EV_OBJ_CREATE, t, t0, 500) >= 0) hits++;
        DestroyWindow(t);
        await_event(RC_EV_OBJ_DESTROY, NULL, GetTickCount(), 300);
    }
    say("completeness              : %d of 20 creates produced an event",
        hits);

    /* B -- what does a whole other program look like on this feed. */
    {
        STARTUPINFOA si;
        PROCESS_INFORMATION pi;
        memset(g_hist, 0, sizeof(g_hist));
        g_hist_other = 0;
        memset(&si, 0, sizeof(si)); si.cb = sizeof(si);
        memset(&pi, 0, sizeof(pi));
        if (CreateProcessA(NULL, "notepad.exe", NULL, NULL, FALSE, 0, NULL,
                           NULL, &si, &pi)) {
            pump_for(4000);
            TerminateProcess(pi.hProcess, 0);
            CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
            pump_for(1500);
        }
        say("-- events during one notepad lifetime --");
        for (k = 0; k < EVNAMES_N; k++)
            if (g_hist[k]) say("   %-16s %d", EVNAMES[k].name, g_hist[k]);
        if (g_hist_other) say("   %-16s %d", "(other ids)", g_hist_other);
    }

    if (unhookwinevent) unhookwinevent(weh);
    UnregisterClassA("GastonEvSink", wc.hInstance);
}

/* ------------------------------------------------------------ 0.15d */
/* The injection question, asked properly this time.
 *
 * 0.15b showed a system-wide hook whose proc lives in an EXE installs and
 * never fires. The fix the documentation implies is a DLL, because the
 * code has to run inside each hooked process. This section builds that
 * case: load HOOKDLL.DLL, install the hook with ITS module handle, start
 * another program, and read the counters out of the shared mapping the DLL
 * writes to.
 *
 * `attaches` is the number that answers the question. It counts processes
 * that mapped the DLL. One means only we have it and nothing was injected;
 * more than one means Win98 put our code inside a program that did not ask
 * for it, which is both the capability and the reason to be careful with
 * it.
 *
 * If this works it also proves the channel for the OTHER two things a DLL
 * was wanted for -- reading text out of controls whose messages 9x does
 * not marshal, and seeing an app go idle -- because both need exactly this:
 * our code in their address space and a way to report back.
 */
/* Must match win/inproc/hookdll.c exactly. Two declarations of one shared
 * layout is a hazard, and the alternative -- a header both include -- means
 * the DLL picks up whatever else that header drags in, on a file that gets
 * mapped into other people's processes. Kept in step by hand, deliberately. */
/* The layout comes from the header the DLL uses. There used to be a copy
 * of it here, which is exactly the drift inproc/shm.h exists to stop. */

/* ------------------------------------------------------------ 0.15f */
/* TARGETED injection: one thread, one process, mapped nowhere else.
 *
 * 0.15d proved a SYSTEM-WIDE WH_CBT hook, which is how Explorer came to be
 * in scope and how Explorer came to die. The plan's D1 switches to
 * SetWindowsHookEx(WH_CALLWNDPROC, proc, dll, targetThreadId) and asserts
 * as a fact that this maps into one process and nowhere else. Nobody has
 * run that. It is a gate, not an assumption.
 *
 * Two things this has to show, and the second is the one that matters:
 *
 *   the DLL's proc runs inside the target -- otherwise there is no tier D
 *   at all;
 *
 *   and it runs NOWHERE ELSE. `pids_n` counting exactly the target (plus
 *   possibly ourselves) is the evidence. Anything more and the "targeted"
 *   claim is false, whatever the API documentation says.
 */

/* Never these, and there is no force escape here -- unlike KILL, where
 * REMOTE.md:344 allows Explorer with force=1. Killing the first four hangs
 * 9x; injecting into them is worse, because the damage shows up later and
 * belongs to them. Explorer is on the list because we have actually killed
 * it. */
static const char *const RC_NO_INJECT[] = {
    "EXPLORER.EXE", "MSGSRV32.EXE", "KERNEL32.DLL", "MPREXE.EXE",
    "MMTASK.TSK", "SYSTRAY.EXE", "RUNDLL32.EXE"
};

static void upper_base(const char *path, char *out, unsigned cap)
{
    const char *base = path;
    unsigned i;
    for (i = 0; path[i]; i++)
        if (path[i] == '\\' || path[i] == '/' || path[i] == ':')
            base = &path[i + 1];
    for (i = 0; i + 1 < cap && base[i]; i++) {
        char c = base[i];
        out[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    }
    out[i] = '\0';
}

static int may_inject(const char *exe)
{
    unsigned i;
    char up[64];
    upper_base(exe, up, sizeof(up));
    for (i = 0; i < sizeof(RC_NO_INJECT) / sizeof(RC_NO_INJECT[0]); i++)
        if (!strcmp(up, RC_NO_INJECT[i]))
            return 0;
    return 1;
}

static DWORD g_want_pid;
static HWND  g_want_win;

static BOOL CALLBACK find_by_pid(HWND h, LPARAM lp)
{
    DWORD pid = 0;
    (void)lp;
    if (!IsWindowVisible(h)) return TRUE;
    GetWindowThreadProcessId(h, &pid);
    if (pid == g_want_pid) { g_want_win = h; return FALSE; }
    return TRUE;
}

/* ------------------------------------------------------------ 0.15g */
/* WHICH text call does the thing we care about actually make?
 *
 * D4 patches TextOutA and ExtTextOutA in every loaded module, and its
 * stated purpose is MSHTML -- a web page is drawn there, not by the
 * browser's executable. If MSHTML draws through ExtTextOutW, or through
 * DrawTextA, or by ordinal, then D4 captures nothing from the one target it
 * exists for, and it would have been built before anybody looked.
 *
 * Reading an import table is free and needs no injection, so this is the
 * cheapest question in the whole section. It is asked here rather than
 * assumed there.
 */
static const char *const RC_GDI_TEXT_FNS[] = {
    "TextOutA", "TextOutW", "ExtTextOutA", "ExtTextOutW",
    "TabbedTextOutA", "PolyTextOutA"
};
static const char *const RC_USER_TEXT_FNS[] = {
    "DrawTextA", "DrawTextW", "DrawTextExA", "DrawTextExW"
};

/* Open Watcom's headers do not carry it at this WINVER. It is the top bit
 * of the thunk word, which is how "imported by ordinal, not by name" is
 * encoded -- hookdll.c defines it for the same reason. */
#ifndef IMAGE_ORDINAL_FLAG
#define IMAGE_ORDINAL_FLAG 0x80000000
#endif

/* Bounded append. lstrcatA is unbounded and the source audit refuses it,
 * correctly: `found` is a fixed buffer and the names come from a PE header
 * that a corrupt or hostile module controls. */
static void append_fn(char *dst, unsigned cap, const char *what)
{
    unsigned n = 0, i;
    while (n + 1 < cap && dst[n]) n++;
    if (n && n + 1 < cap) dst[n++] = ' ';
    for (i = 0; n + 1 < cap && what[i]; i++) dst[n++] = what[i];
    dst[n] = '\0';
}

/* Read-only twin of hookdll's patch_import: says what a module imports
 * without touching a byte of it. */
static void report_imports(const char *modName, HMODULE mod)
{
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)mod;
    IMAGE_NT_HEADERS *nt;
    IMAGE_IMPORT_DESCRIPTOR *imp;
    DWORD rva;
    char found[256];
    unsigned fn;
    int any = 0;

    found[0] = '\0';
    if (!mod || dos->e_magic != IMAGE_DOS_SIGNATURE) {
        say("  %-14s not loaded", modName);
        return;
    }
    nt = (IMAGE_NT_HEADERS *)((char *)mod + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) {
        say("  %-14s not a PE", modName);
        return;
    }
    rva = nt->OptionalHeader
            .DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress;
    if (!rva) { say("  %-14s imports nothing", modName); return; }

    for (imp = (IMAGE_IMPORT_DESCRIPTOR *)((char *)mod + rva);
         imp->Name; imp++) {
        const char *dll = (const char *)mod + imp->Name;
        IMAGE_THUNK_DATA *names;
        int isGdi = lstrcmpiA(dll, "GDI32.DLL") == 0;
        int isUser = lstrcmpiA(dll, "USER32.DLL") == 0;
        if (!isGdi && !isUser)
            continue;
        /* OriginalFirstThunk is the by-name table. Without it there are no
         * names to match and the patch could not find its target either --
         * which is itself the answer, so it is reported. */
        if (!imp->OriginalFirstThunk) {
            say("  %-14s imports %s with no name table (bound or ordinal)",
                modName, dll);
            continue;
        }
        names = (IMAGE_THUNK_DATA *)((char *)mod + imp->OriginalFirstThunk);
        for (; names->u1.AddressOfData; names++) {
            IMAGE_IMPORT_BY_NAME *byname;
            const char *have;
            if (names->u1.Ordinal & IMAGE_ORDINAL_FLAG)
                continue;
            byname = (IMAGE_IMPORT_BY_NAME *)
                     ((char *)mod + names->u1.AddressOfData);
            have = (const char *)byname->Name;
            if (isGdi) {
                for (fn = 0; fn < sizeof(RC_GDI_TEXT_FNS)
                             / sizeof(RC_GDI_TEXT_FNS[0]); fn++)
                    if (!strcmp(have, RC_GDI_TEXT_FNS[fn])) {
                        append_fn(found, sizeof(found), have);
                        any = 1;
                    }
            } else {
                for (fn = 0; fn < sizeof(RC_USER_TEXT_FNS)
                             / sizeof(RC_USER_TEXT_FNS[0]); fn++)
                    if (!strcmp(have, RC_USER_TEXT_FNS[fn])) {
                        append_fn(found, sizeof(found), have);
                        any = 1;
                    }
        }
        }
    }
    if (any) say("  %-14s %s", modName, found);
    else     say("  %-14s no text entry point imported by name", modName);
}

static void probe_gditext(void)
{
    static const char *const MODS[] = {
        "MSHTML.DLL", "COMCTL32.DLL", "USER32.DLL", "COMDLG32.DLL",
        "SHELL32.DLL", "RICHED32.DLL"
    };
    unsigned i;

    head("0.15g  which text call does each drawing module actually make?");
    say("D4 patches TextOutA and ExtTextOutA. If the module that draws a web");
    say("page imports something else, D4 captures nothing from the one target");
    say("it exists for. Reading an import table needs no injection.");
    say("");

    report_imports("(this exe)", GetModuleHandleA(NULL));
    for (i = 0; i < sizeof(MODS) / sizeof(MODS[0]); i++) {
        HMODULE m = GetModuleHandleA(MODS[i]);
        int loadedHere = 0;
        if (!m) { m = LoadLibraryA(MODS[i]); loadedHere = m != NULL; }
        report_imports(MODS[i], m);
        if (loadedHere) FreeLibrary(m);
    }
    say("");
    say("  A module listing ExtTextOutW or DrawTextA and NOT the A forms is");
    say("  a module D4 would miss. Widen the patch list, do not assume.");
}

static void probe_targeted(void)
{
    HMODULE dll;
    HOOKPROC proc;
    HANDLE map;
    RcInShm *shm;
    HHOOK h;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    DWORD tid = 0, pid = 0;
    long i;
    int mine = 0, theirs = 0, other = 0;

    head("0.15f  can injection be aimed at ONE process?");

    say("deny list (no force escape): EXPLORER.EXE MSGSRV32.EXE "
        "KERNEL32.DLL MPREXE.EXE MMTASK.TSK SYSTRAY.EXE RUNDLL32.EXE");
    say("  may_inject(notepad.exe)  = %d", may_inject("notepad.exe"));
    say("  may_inject(C:\\WINDOWS\\EXPLORER.EXE) = %d",
        may_inject("C:\\WINDOWS\\EXPLORER.EXE"));
    say("  may_inject(explorer.exe) = %d  (case must not matter)",
        may_inject("explorer.exe"));

    map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                             0, sizeof(RcInShm), RC_SHM_NAME);
    shm = map ? (RcInShm *)MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, 0)
              : NULL;
    if (!shm) { say("shared mapping failed (%lu)", GetLastError()); return; }

    shm->magic = RC_SHM_MAGIC;
    shm->version = RC_SHM_VER;
    shm->cb = sizeof(RcInShm);
    shm->generation = GetTickCount() | 1;
    shm->calls = 0;
    shm->attaches = 0;
    shm->pids_n = 0;
    shm->patched = 0;
    shm->draws = 0;
    shm->ring_next = 0;
    /* No patch target at all for this gate: the question is whether the
     * code ARRIVES, and rewriting an import table is a separate risk that
     * does not need to be taken to answer it. */
    shm->patch_pid = 0;
    shm->patch_exe[0] = '\0';

    dll = LoadLibraryA("HOOKDLL.DLL");
    if (!dll) dll = LoadLibraryA(".\\HOOKDLL.DLL");
    if (!dll) { say("HOOKDLL.DLL not found (%lu)", GetLastError());
                UnmapViewOfFile(shm); CloseHandle(map); return; }
    proc = (HOOKPROC)GetProcAddress(dll, "rc_hook_proc");
    if (!proc) proc = (HOOKPROC)GetProcAddress(dll, "_rc_hook_proc@12");
    if (!proc) { say("rc_hook_proc not exported"); FreeLibrary(dll);
                 UnmapViewOfFile(shm); CloseHandle(map); return; }

    memset(&si, 0, sizeof(si)); si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessA(NULL, "notepad.exe", NULL, NULL, FALSE, 0, NULL,
                        NULL, &si, &pi)) {
        say("could not start notepad (%lu)", GetLastError());
        FreeLibrary(dll); UnmapViewOfFile(shm); CloseHandle(map); return;
    }
    pid = pi.dwProcessId;
    say("notepad pid %lu", (unsigned long)pid);

    /* Its window, and therefore its thread. A hook is aimed at a THREAD;
     * there is no process-wide form. */
    for (i = 0; i < 60 && !tid; i++) {
        Sleep(100);
        g_want_pid = pid; g_want_win = NULL;
        EnumWindows(find_by_pid, 0);
        if (g_want_win) tid = GetWindowThreadProcessId(g_want_win, NULL);
    }
    if (!tid) {
        say("notepad never showed a window; cannot aim a hook");
    } else {
        /* WHICH hook type, measured rather than assumed.
         *
         * The plan named WH_CALLWNDPROC and asserted it maps into one
         * process. On this Win98 it installs and never fires -- so the
         * question is not "does D1 work" but "which of these delivers",
         * and the only way to know is to try each and count. Every one is
         * aimed at ONE THREAD; none of them is the system-wide form that
         * put Explorer in scope. */
        static const struct { int id; const char *name; } HOOKS[] = {
            { WH_CALLWNDPROC,    "WH_CALLWNDPROC"    },
            { WH_GETMESSAGE,     "WH_GETMESSAGE"     },
            { WH_CBT,            "WH_CBT"            },
            { WH_CALLWNDPROCRET, "WH_CALLWNDPROCRET" }
        };
        unsigned hk;
        say("target thread %lu", (unsigned long)tid);
        for (hk = 0; hk < sizeof(HOOKS) / sizeof(HOOKS[0]); hk++) {
            long before = shm->calls;
            long pidsBefore = shm->pids_n;
            h = SetWindowsHookExA(HOOKS[hk].id, proc, dll, tid);
            if (!h) {
                say("  %-18s refused (%lu)", HOOKS[hk].name,
                    (unsigned long)GetLastError());
                continue;
            }
            /* Give the thread something to do: a sent message for the
             * CALLWNDPROC family, and a posted one so GETMESSAGE has
             * something to retrieve. SendMessageTimeout, never
             * SendMessage -- this must not be able to wedge the probe. */
            {
                DWORD_PTR res = 0;
                for (i = 0; i < 20; i++) {
                    SendMessageTimeoutA(g_want_win, WM_NULL, 0, 0,
                                        SMTO_ABORTIFHUNG, 200, &res);
                    PostMessageA(g_want_win, WM_NULL, 0, 0);
                    Sleep(50);
                }
            }
            Sleep(300);
            UnhookWindowsHookEx(h);
            Sleep(200);
            say("  %-18s installed; +%ld calls, +%ld distinct pids",
                HOOKS[hk].name, shm->calls - before,
                shm->pids_n - pidsBefore);
        }
    }

    Sleep(300);
    if (pi.hProcess) {
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }

    for (i = 0; i < shm->pids_n && i < 16; i++) {
        if (shm->pids[i] == GetCurrentProcessId()) mine++;
        else if (shm->pids[i] == pid) theirs++;
        else { other++; say("   UNEXPECTED pid %lu ran our code",
                            (unsigned long)shm->pids[i]); }
    }
    say("attaches %ld, calls %ld, distinct pids %ld "
        "(us %d, target %d, other %d)",
        shm->attaches, shm->calls, shm->pids_n, mine, theirs, other);

    if (theirs && !other)
        say("   -> TARGETED INJECTION WORKS: our code ran in the target and "
            "in no other process.");
    else if (theirs && other)
        say("   -> it reached the target AND %d other process(es). The "
            "'one process' claim is FALSE; D1 needs rethinking.", other);
    else
        say("   -> our code never ran in the target. D depends on this; "
            "without it the whole in-process tier is unavailable.");

    /* Every process that mapped us has now unmapped: the hook is gone. If
     * a later run sees attaches climbing without a hook installed, an old
     * copy is still resident somewhere and the section is being written by
     * something we do not control. */
    FreeLibrary(dll);
    UnmapViewOfFile(shm);
    CloseHandle(map);
}

static void probe_inject(void)
{
    HMODULE dll;
    HOOKPROC proc;
    HANDLE map;
    RcInShm *shm;
    HHOOK h;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    int started;
    long i;

    head("0.15d  can we get our code into another process?");

    /* The mapping MUST exist and name its target before the DLL loads,
     * because the DLL decides whether to patch in its DllMain and that runs
     * inside LoadLibrary below. Naming only ourselves is what keeps every
     * other injected process's import table untouched. */
    map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                             0, sizeof(RcInShm), RC_SHM_NAME);
    shm = map ? (RcInShm *)MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, 0)
              : NULL;
    if (!shm) { say("shared mapping failed (%lu)", GetLastError()); return; }

    /* STAMP AND ZERO, and this is the correction that makes this gate mean
     * anything. Nothing zeroed these before and no injected copy ever
     * unmaps the section, so `attaches` was cumulative across every run
     * since boot -- and the verdict below tested `attaches > 1`. It could
     * be satisfied entirely by residue from a run that injected nothing. */
    shm->magic = RC_SHM_MAGIC;
    shm->version = RC_SHM_VER;
    shm->cb = sizeof(RcInShm);
    shm->generation = GetTickCount() | 1;
    shm->calls = 0;
    shm->attaches = 0;
    shm->pids_n = 0;
    shm->patched = 0;
    shm->draws = 0;
    shm->ring_next = 0;
    say("generation %lu, counters zeroed", (unsigned long)shm->generation);

    /* Pid AND name. A 9x pid is an obfuscated pointer into the process
     * database and it is reused, so a target that exits can hand its pid
     * to something else before we let go of it. */
    shm->patch_pid = GetCurrentProcessId();
    {
        char path[MAX_PATH];
        const char *base = path;
        int bi;
        path[0] = '\0';
        GetModuleFileNameA(NULL, path, sizeof(path));
        for (bi = 0; path[bi]; bi++)
            if (path[bi] == '\\' || path[bi] == '/' || path[bi] == ':')
                base = &path[bi + 1];
        for (bi = 0; bi < 31 && base[bi]; bi++)
            shm->patch_exe[bi] = base[bi];
        shm->patch_exe[bi] = '\0';
    }
    say("patch target set to this process only (pid %lu, %s)",
        (unsigned long)shm->patch_pid, shm->patch_exe);

    /* The CD first, then wherever we are. On the kit both sit together. */
    dll = LoadLibraryA("HOOKDLL.DLL");
    if (!dll) dll = LoadLibraryA("D:\\HOOKDLL.DLL");
    if (!dll) {
        say("HOOKDLL.DLL not found (%lu) -- cannot test injection",
            GetLastError());
        UnmapViewOfFile(shm); CloseHandle(map);
        return;
    }
    /* Decorated, because a hook proc is __stdcall and Open Watcom exports
     * it as _rc_hook_proc@12. The plain name is tried first so a future
     * aliased build keeps working. */
    proc = (HOOKPROC)GetProcAddress(dll, "rc_hook_proc");
    if (!proc) proc = (HOOKPROC)GetProcAddress(dll, "_rc_hook_proc@12");
    say("HOOKDLL.DLL loaded, proc %s", proc ? "found" : "MISSING");
    if (!proc) { FreeLibrary(dll); UnmapViewOfFile(shm);
                 CloseHandle(map); return; }

    /* Draw something ourselves, through our OWN patched import table, so
     * the mechanism is tested separately from whether any other program
     * happens to paint its own text. LoadLibrary above already ran the
     * patch in this process; if this string comes back out of the ring,
     * the interception works and anything missing afterwards is about
     * WHERE the target draws, not about whether we can see a draw. */
    {
        HDC dc = CreateCompatibleDC(NULL);
        HBITMAP bm = CreateCompatibleBitmap(GetDC(NULL), 200, 40);
        HGDIOBJ old = SelectObject(dc, bm);
        TextOutA(dc, 7, 9, "GASTON-SELFTEST", 15);
        SelectObject(dc, old);
        DeleteObject(bm);
        DeleteDC(dc);
    }

    say("attaches so far %ld (this process)", shm->attaches);

    h = SetWindowsHookExA(WH_CBT, proc, dll, 0);
    say("WH_CBT with a DLL       : %s", h ? "installed" : "refused");
    if (!h) { UnmapViewOfFile(shm); CloseHandle(map); FreeLibrary(dll);
              return; }

    memset(&si, 0, sizeof(si)); si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    started = CreateProcessA(NULL, "notepad.exe", NULL, NULL, FALSE, 0,
                             NULL, NULL, &si, &pi);
    say("notepad started         : %s", started ? "yes" : "no");
    pump_for(5000);
    if (started) {
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        pump_for(1000);
    }
    UnhookWindowsHookEx(h);

    say("attaches (processes that mapped the DLL) : %ld", shm->attaches);
    say("hook calls, all processes                : %ld", shm->calls);
    say("distinct processes that ran the proc     : %ld", shm->pids_n);
    for (i = 0; i < shm->pids_n && i < 16; i++)
        say("   pid %lu%s", (unsigned long)shm->pids[i],
            shm->pids[i] == GetCurrentProcessId() ? "  (this one)" : "");
    /* pids_n, not attaches. `attaches` counts DllMain calls and our own is
     * one of them; the question is whether a process that is not this one
     * ran our code, and only the distinct-pid count answers that without
     * being satisfiable by our own activity. */
    if (shm->pids_n > 1)
        say("   -> INJECTION WORKS. Our code ran inside another program.");
    else if (shm->attaches > 1)
        say("   -> the DLL was MAPPED elsewhere but its proc never ran "
            "there. Mapping is not execution; do not read this as yes.");
    else
        say("   -> no injection: only this process ever mapped the DLL.");

    /* The GDI half. This is the text an owner-drawn control paints and
     * answers no message about, so anything here is text no other tier in
     * the ladder could have got. */
    /* SAY WHOSE. The patch target above is this very process, so every
     * draw below was painted by the probe and captured by the probe --
     * which proves the mechanism and proves nothing about a foreign
     * process. Reading this as "GDI capture works" was the mistake. */
    say("IAT patch took in %ld process(es); %ld text draws seen",
        shm->patched, shm->draws);
    say("   NOTE: the patch target is this process, so these are OUR draws.");
    say("   Capturing a foreign process's text is untested -- see D4.");
    if (shm->draws > 0) {
        long shown = 0;
        for (i = 0; i < RC_TEXTN && shown < 12; i++) {
            RcDraw *d = &shm->ring[i];
            if (!d->text[0]) continue;
            shown++;
            say("   pid %lu @%ld,%ld  \"%.40s\"",
                (unsigned long)d->pid, d->x, d->y, d->text);
        }
        say("   -> GDI text capture works; that is text no message gives.");
    } else if (shm->patched > 0) {
        say("   -> patched but nothing drew. The target may paint through");
        say("      a DLL of its own rather than its own import table.");
    }

    UnmapViewOfFile(shm);
    CloseHandle(map);
    FreeLibrary(dll);
}

/* ------------------------------------------------------------ 0.15e */
/* WaitForControl and WaitForText, built and run rather than designed.
 *
 * The design says: subscribe, THEN check, then wait on the feed. Checking
 * first races -- the thing can appear between the check and the
 * subscription, and the wait then blocks for its whole timeout on a
 * condition that is already true. This is that ordering, executed.
 *
 * It is event-driven and the probe proves it: `rechecks` counts how many
 * times the tree was actually looked at. A poll at 50ms over four seconds
 * would be eighty. A handful means the feed drove it.
 *
 * The negative test matters as much as the positive one. A wait for
 * something that will never exist must come back at its deadline and say
 * so. A primitive that hangs instead is worse than none, because it hangs
 * rarely and in production.
 */
static int g_dirty;

static void CALLBACK wf_proc(RC_HWINEVENTHOOK hook, DWORD ev, HWND h,
                             LONG obj, LONG child, DWORD tid, DWORD ms)
{
    (void)hook; (void)h; (void)obj; (void)child; (void)tid; (void)ms;
    switch ((long)ev) {
    case RC_EV_OBJ_CREATE: case RC_EV_OBJ_DESTROY:
    case RC_EV_OBJ_SHOW:   case RC_EV_OBJ_HIDE:
    case RC_EV_OBJ_NAMECHANGE: case RC_EV_OBJ_VALUECHANGE:
        g_dirty = 1;
        break;
    default:
        break;
    }
}

static char g_find_cls[64];
static char g_find_sub[64];
static HWND g_found;

/* The whole selector, for a probe: class equals, text contains. Enough to
 * exercise the wait; the real grammar is in docs/UIAUTOMATION.md. */
static BOOL CALLBACK find_cb(HWND h, LPARAM lp)
{
    char cls[64], txt[128];
    DWORD_PTR res = 0;
    (void)lp;
    if (!IsWindowVisible(h)) return TRUE;
    cls[0] = '\0';
    GetClassNameA(h, cls, sizeof(cls));
    if (g_find_cls[0] && strcmp(cls, g_find_cls)) return TRUE;
    if (g_find_sub[0]) {
        txt[0] = '\0';
        SendMessageTimeoutA(h, WM_GETTEXT, sizeof(txt), (LPARAM)txt,
                            SMTO_ABORTIFHUNG, 200, &res);
        if (!strstr(txt, g_find_sub)) return TRUE;
    }
    g_found = h;
    return FALSE;
}

static HWND match_once(const char *cls, const char *sub)
{
    g_find_cls[0] = g_find_sub[0] = '\0';
    if (cls) rc_strncpy(g_find_cls, sizeof(g_find_cls), cls,
                        (unsigned)strlen(cls));
    if (sub) rc_strncpy(g_find_sub, sizeof(g_find_sub), sub,
                        (unsigned)strlen(sub));
    g_found = NULL;
    EnumWindows(find_cb, 0);
    return g_found;
}

/* Returns ms waited, or -1 on timeout. `rechecks` comes back so the caller
 * can see whether the feed or the clock did the work. */
static long wait_for(const char *cls, const char *sub, int want,
                     DWORD budget, int *rechecks)
{
    DWORD t0 = GetTickCount();
    DWORD until = t0 + budget;
    MSG msg;
    int n = 1;
    if ((match_once(cls, sub) != NULL) == want) { *rechecks = 1; return 0; }
    g_dirty = 0;
    while ((long)(GetTickCount() - until) < 0) {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (g_dirty) {
            g_dirty = 0;
            n++;
            if ((match_once(cls, sub) != NULL) == want) {
                *rechecks = n;
                return (long)(GetTickCount() - t0);
            }
        }
        Sleep(1);
    }
    *rechecks = n;
    return -1L;
}

static void probe_waitfor(void)
{
    HMODULE u32 = GetModuleHandleA("user32.dll");
    PFN_SETWINEVENT setwinevent =
        (PFN_SETWINEVENT)GetProcAddress(u32, "SetWinEventHook");
    PFN_UNHOOKWINEVENT unhookwinevent =
        (PFN_UNHOOKWINEVENT)GetProcAddress(u32, "UnhookWinEvent");
    RC_HWINEVENTHOOK weh;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    long d;
    int n;

    head("0.15e  WaitForControl / WaitForText, actually run");
    if (!setwinevent) { say("SetWinEventHook not available"); return; }

    /* Subscribe FIRST. Everything below depends on this line being above
     * the CreateProcess, not below it. */
    weh = setwinevent(RC_EVENT_MIN, RC_EV_OBJ_VALUECHANGE, NULL, wf_proc,
                      0, 0, RC_WINEVENT_OUTOFCONTEXT);
    if (!weh) { say("hook install failed"); return; }

    memset(&si, 0, sizeof(si)); si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessA(NULL, "notepad.exe", NULL, NULL, FALSE, 0, NULL,
                        NULL, &si, &pi)) {
        say("could not start notepad");
        if (unhookwinevent) unhookwinevent(weh);
        return;
    }

    d = wait_for("Notepad", NULL, 1, 8000, &n);
    say("WaitForControl(.Notepad)      : %ld ms, %d rechecks", d, n);

    d = wait_for("Notepad", "Notepad", 1, 4000, &n);
    say("WaitForText(.Notepad,'Notepad'): %ld ms, %d rechecks", d, n);

    /* The one that must fail, and fail on time. */
    d = wait_for("NoSuchClassAtAll", NULL, 1, 1500, &n);
    say("WaitForControl(absent), 1500ms: %ld ms, %d rechecks  (-1 = timed out)",
        d, n);

    TerminateProcess(pi.hProcess, 0);
    d = wait_for("Notepad", NULL, 0, 6000, &n);
    say("WaitForGone(.Notepad)         : %ld ms, %d rechecks", d, n);
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);

    if (unhookwinevent) unhookwinevent(weh);
    say("(rechecks well under a poll count means the feed drove the wait)");
}

/* ------------------------------------------------------------ 0.13e */
/* WHERE does MSAA's second per control go?
 *
 * Reported from the P200: roughly a second per control, which is unusable
 * and far worse than the emulator's ~12ms. Two candidate causes need
 * separating before anything is changed, because the fixes are opposite:
 *
 *   object creation   AccessibleObjectFromWindow does a WM_GETOBJECT round
 *                     trip into the target and, for a window that does not
 *                     answer, oleacc falls back to a proxy -- possibly
 *                     after waiting out a timeout. If this is the cost,
 *                     fetching fewer properties changes nothing and the
 *                     answer is to create ONE object per parent and
 *                     enumerate with AccessibleChildren.
 *
 *   property fetch    three COM calls per node (name, value, role), each
 *                     marshalled if the proxy lives in another apartment.
 *                     If this is the cost, the answer is to fetch only what
 *                     the caller asked for, which is a small change.
 *
 * So each phase is timed separately against the SAME nodes, and each is
 * repeated so a 55ms tick cannot hide a difference. Ten nodes, not the
 * whole desktop: at a second each the whole desktop would be minutes.
 */
static void probe_msaacost(void)
{
    HMODULE acc;
    PFN_AOFW aofw;
    PFN_ACHILDREN achildren;
    int i, n;
    DWORD t0, t1;
    const int LIMIT = 10;

    head("0.13e  where MSAA's time actually goes");

    acc = LoadLibraryA("oleacc.dll");
    aofw = acc ? (PFN_AOFW)GetProcAddress(acc, "AccessibleObjectFromWindow")
               : 0;
    achildren = acc ? (PFN_ACHILDREN)GetProcAddress(acc, "AccessibleChildren")
                    : 0;
    if (!aofw) { say("oleacc unavailable"); if (acc) FreeLibrary(acc); return; }

    g_uiw_n = 0;
    EnumWindows(collect_top, 0);
    n = g_uiw_n < LIMIT ? g_uiw_n : LIMIT;
    say("timing the first %d visible windows of %d", n, g_uiw_n);

    CoInitialize(NULL);

    /* WARM UP FIRST, and discard it. The first version timed A first and
     * got 19ms/node for create-only against 5ms/node for create-plus-role,
     * which cannot be true -- the second does strictly more work. A was
     * paying oleacc's one-time setup for every phase that followed. The
     * same warm/cold trap the window walk hit, and the same fix. */
    for (i = 0; i < n; i++) {
        IAccessible *ia = 0;
        if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&ia) == S_OK && ia)
            ia->lpVtbl->Release(ia);
    }

    /* A -- create and release only. No properties asked for at all. */
    t0 = GetTickCount();
    for (i = 0; i < n; i++) {
        IAccessible *ia = 0;
        if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&ia) == S_OK && ia)
            ia->lpVtbl->Release(ia);
    }
    t1 = GetTickCount();
    say("A create+release only : %lu ms total, %lu ms/node",
        (unsigned long)(t1 - t0), (unsigned long)(t1 - t0) / (n ? n : 1));

    /* B -- the same, but OBJID_WINDOW. If the frame object is much cheaper
     * than the client one, the cost is in what oleacc builds, not in the
     * message. */
    t0 = GetTickCount();
    for (i = 0; i < n; i++) {
        IAccessible *ia = 0;
        if (aofw(g_uiw[i], OBJID_WINDOW, &RC_IID_IAccessible,
                 (void **)&ia) == S_OK && ia)
            ia->lpVtbl->Release(ia);
    }
    t1 = GetTickCount();
    say("B create, OBJID_WINDOW: %lu ms total, %lu ms/node",
        (unsigned long)(t1 - t0), (unsigned long)(t1 - t0) / (n ? n : 1));

    /* C, D, E -- one property each, on top of a create. The difference
     * between each and A is what that one property costs. */
    {
        struct { const char *name; int which; } props[3];
        int k;
        props[0].name = "+ role  "; props[0].which = 0;
        props[1].name = "+ name  "; props[1].which = 1;
        props[2].name = "+ value "; props[2].which = 2;
        for (k = 0; k < 3; k++) {
            t0 = GetTickCount();
            for (i = 0; i < n; i++) {
                IAccessible *ia = 0;
                VARIANT self;
                if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                         (void **)&ia) != S_OK || !ia)
                    continue;
                V_VT(&self) = VT_I4; V_I4(&self) = 0;
                if (props[k].which == 0) {
                    VARIANT vr;
                    V_VT(&vr) = VT_EMPTY;
                    ia->lpVtbl->get_accRole(ia, self, &vr);
                } else if (props[k].which == 1) {
                    BSTR b = 0;
                    if (ia->lpVtbl->get_accName(ia, self, &b) == S_OK && b)
                        SysFreeString(b);
                } else {
                    BSTR b = 0;
                    if (ia->lpVtbl->get_accValue(ia, self, &b) == S_OK && b)
                        SysFreeString(b);
                }
                ia->lpVtbl->Release(ia);
            }
            t1 = GetTickCount();
            say("%s          : %lu ms total, %lu ms/node", props[k].name,
                (unsigned long)(t1 - t0),
                (unsigned long)(t1 - t0) / (n ? n : 1));
        }
    }

    /* F -- one object for the PARENT, then AccessibleChildren. If A is the
     * cost, this is the fix: one WM_GETOBJECT instead of one per node. */
    if (achildren) {
        IAccessible *parent = 0;
        t0 = GetTickCount();
        if (aofw(GetDesktopWindow(), OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&parent) == S_OK && parent) {
            long kids = 0, got = 0;
            parent->lpVtbl->get_accChildCount(parent, &kids);
            if (kids > 0) {
                VARIANT *v = (VARIANT *)HeapAlloc(GetProcessHeap(), 0,
                                                  sizeof(VARIANT) * kids);
                if (v) {
                    achildren(parent, 0, kids, v, &got);
                    HeapFree(GetProcessHeap(), 0, v);
                }
            }
            t1 = GetTickCount();
            say("F one parent + children: %lu ms for %ld children (%lu ms each)",
                (unsigned long)(t1 - t0), got,
                (unsigned long)(t1 - t0) / (got ? got : 1));
            parent->lpVtbl->Release(parent);
        } else {
            t1 = GetTickCount();
            say("F one parent + children: desktop has no accessible object");
        }
    }

    CoUninitialize();
    FreeLibrary(acc);
    say("(warm: a discarded pass runs before any of these)");
    say("A high, properties near zero -> create ONE object per parent.");
    say("A low, a property high       -> fetch only what was asked for.");
}

/* ------------------------------------------------------------ 0.13f */
/* Three ways to read the same desktop, because the first one is wrong.
 *
 * Measured on the P200: 1886ms to enrich 103 nodes, 35 times the cost of
 * the entire rest of the walk. The reason is in Mozilla's MSAA server notes
 * and in every AT vendor's experience -- "Fewer calls are much better
 * because COM Marshaling is slow" -- and a walk that calls
 * AccessibleObjectFromWindow once per HWND is making the maximum possible
 * number of cross-process calls.
 *
 *   A  per-HWND        AccessibleObjectFromWindow for every window, then
 *                      one property. What the code does today.
 *
 *   B  AccessibleChildren from one root. One object for the desktop, then
 *                      oleacc's batching helper down the tree. It uses
 *                      IEnumVARIANT underneath when the server offers it
 *                      and falls back to get_accChild in a loop when not,
 *                      so this measures what we get for free.
 *
 *   C  IEnumVARIANT::Next explicitly, asking for every child of a node in
 *                      ONE marshalled call. The technique the documentation
 *                      names -- and, measured on the P200, NOT the fastest:
 *                      2555 us/node against AccessibleChildren's 2078. The
 *                      emulator said the opposite (2196 against 3630),
 *                      which is how a plausible optimisation gets adopted
 *                      on the strength of the wrong machine.
 *
 * The win is not the enumerator. It is walking from ONE root instead of
 * calling AccessibleObjectFromWindow per window: 8500 us/node to 2078, a
 * little over 4x, and oleacc's own helper already does the batching.
 *
 * Same desktop, same property fetched, so the difference is the traversal
 * and nothing else.
 */
static const GUID RC_IID_IEnumVARIANT =
    { 0x00020404, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };

static int g_fast_nodes;

/* Shared by B and C: fetch the one property we are timing, so the three
 * strategies are compared on identical work per node. */
static void touch_one(IAccessible *acc, VARIANT child)
{
    VARIANT vr;
    V_VT(&vr) = VT_EMPTY;
    acc->lpVtbl->get_accRole(acc, child, &vr);
}

static void walk_children(IAccessible *acc, int depth, int useEnum)
{
    long kids = 0, got = 0, i;
    VARIANT *v;

    /* The SAME cap for both traversals, and low enough that both reach
     * it. The first run capped B at 400 and let C finish at 192, so the
     * two totals were not comparable and neither were the times. */
    if (depth > 8 || g_fast_nodes >= 150) return;
    if (acc->lpVtbl->get_accChildCount(acc, &kids) != S_OK || kids <= 0)
        return;
    v = (VARIANT *)HeapAlloc(GetProcessHeap(), 0, sizeof(VARIANT) * kids);
    if (!v) return;

    if (useEnum) {
        /* ONE call for every child of this node. */
        IEnumVARIANT *ev = 0;
        unsigned long fetched = 0;
        if (acc->lpVtbl->QueryInterface(acc, &RC_IID_IEnumVARIANT,
                                        (void **)&ev) == S_OK && ev) {
            ev->lpVtbl->Reset(ev);
            ev->lpVtbl->Next(ev, (unsigned long)kids, v, &fetched);
            ev->lpVtbl->Release(ev);
            got = (long)fetched;
        } else {
            /* No enumerator on this server: fall back rather than report a
             * speed that came from doing less work. */
            if (g_achildren) g_achildren(acc, 0, kids, v, &got);
        }
    } else {
        if (g_achildren) g_achildren(acc, 0, kids, v, &got);
    }

    for (i = 0; i < got; i++) {
        g_fast_nodes++;
        if (V_VT(&v[i]) == VT_DISPATCH && V_DISPATCH(&v[i])) {
            IAccessible *kid = 0;
            IDispatch *d = V_DISPATCH(&v[i]);
            if (d->lpVtbl->QueryInterface(d, &RC_IID_IAccessible,
                                          (void **)&kid) == S_OK && kid) {
                VARIANT self;
                V_VT(&self) = VT_I4; V_I4(&self) = 0;
                touch_one(kid, self);
                walk_children(kid, depth + 1, useEnum);
                kid->lpVtbl->Release(kid);
            }
            d->lpVtbl->Release(d);
        } else if (V_VT(&v[i]) == VT_I4) {
            /* A simple element: the PARENT answers for it, so there is no
             * object to create at all. This is where the saving lives. */
            touch_one(acc, v[i]);
        }
    }
    HeapFree(GetProcessHeap(), 0, v);
}

static void probe_msaafast(void)
{
    HMODULE acc;
    PFN_AOFW aofw;
    DWORD t0, t1;
    int i, perhwnd;

    head("0.13f  three traversals, same desktop");

    acc = LoadLibraryA("oleacc.dll");
    aofw = acc ? (PFN_AOFW)GetProcAddress(acc, "AccessibleObjectFromWindow")
               : 0;
    g_achildren = acc ? (PFN_ACHILDREN)GetProcAddress(acc,
                                                      "AccessibleChildren") : 0;
    if (!aofw || !g_achildren) {
        say("oleacc unavailable"); if (acc) FreeLibrary(acc); return;
    }
    CoInitialize(NULL);

    g_uiw_n = 0;
    EnumWindows(collect_top, 0);
    say("desktop has %d visible windows by EnumWindows", g_uiw_n);

    /* Warm, so oleacc's one-time setup is not charged to whichever runs
     * first -- the mistake 0.13e made and had to be redone for. */
    for (i = 0; i < g_uiw_n && i < 20; i++) {
        IAccessible *ia = 0;
        if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&ia) == S_OK && ia)
            ia->lpVtbl->Release(ia);
    }

    /* A -- one AccessibleObjectFromWindow per window, one property each. */
    perhwnd = 0;
    t0 = GetTickCount();
    for (i = 0; i < g_uiw_n; i++) {
        IAccessible *ia = 0;
        if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&ia) == S_OK && ia) {
            VARIANT self;
            V_VT(&self) = VT_I4; V_I4(&self) = 0;
            touch_one(ia, self);
            ia->lpVtbl->Release(ia);
            perhwnd++;
        }
    }
    t1 = GetTickCount();
    say("A per-HWND        : %d nodes in %lu ms (%lu us/node)", perhwnd,
        (unsigned long)(t1 - t0),
        (unsigned long)(t1 - t0) * 1000 / (perhwnd ? perhwnd : 1));

    /* B -- one root, AccessibleChildren down. */
    {
        IAccessible *root = 0;
        g_fast_nodes = 0;
        t0 = GetTickCount();
        if (aofw(GetDesktopWindow(), OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&root) == S_OK && root) {
            walk_children(root, 0, 0);
            root->lpVtbl->Release(root);
        }
        t1 = GetTickCount();
        say("B AccessibleChildren: %d nodes in %lu ms (%lu us/node)", g_fast_nodes,
            (unsigned long)(t1 - t0),
            (unsigned long)(t1 - t0) * 1000 / (g_fast_nodes ? g_fast_nodes : 1));
    }

    /* C -- one root, IEnumVARIANT::Next down. */
    {
        IAccessible *root = 0;
        g_fast_nodes = 0;
        t0 = GetTickCount();
        if (aofw(GetDesktopWindow(), OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&root) == S_OK && root) {
            walk_children(root, 0, 1);
            root->lpVtbl->Release(root);
        }
        t1 = GetTickCount();
        say("C IEnumVARIANT::Next: %d nodes in %lu ms (%lu us/node)", g_fast_nodes,
            (unsigned long)(t1 - t0),
            (unsigned long)(t1 - t0) * 1000 / (g_fast_nodes ? g_fast_nodes : 1));
    }

    /* Does the enumerator even exist here? If servers on this machine do
     * not offer one, B and C are the same code and the batching win has to
     * come from somewhere else. */
    {
        IAccessible *root = 0;
        if (aofw(GetDesktopWindow(), OBJID_CLIENT, &RC_IID_IAccessible,
                 (void **)&root) == S_OK && root) {
            IEnumVARIANT *ev = 0;
            HRESULT hr = root->lpVtbl->QueryInterface(root,
                             &RC_IID_IEnumVARIANT, (void **)&ev);
            say("desktop offers IEnumVARIANT : %s",
                (hr == S_OK && ev) ? "yes" : "no");
            if (ev) ev->lpVtbl->Release(ev);
            root->lpVtbl->Release(root);
        }
    }

    CoUninitialize();
    FreeLibrary(acc);
    say("node counts differ on purpose: A sees windows, B and C see the");
    say("accessible tree, which includes simple elements with no HWND.");
    say("On the P200: B beat C. Prefer AccessibleChildren; the saving is");
    say("the single root, not the enumerator.");
}

/* ------------------------------------------------------------ 0.13g */
/* How much of MSAA can plain Win32 answer, and for what?
 *
 * MSAA costs 11.5ms a node because IAccessible is COM and a window in
 * another process hands back a proxy that marshals every call. That is not
 * avoidable -- the interface IS COM. What IS avoidable is asking it things
 * Win32 already knows.
 *
 * A Button's style bits ARE its role: BS_PUSHBUTTON, BS_CHECKBOX,
 * BS_RADIOBUTTON, BS_GROUPBOX. GetWindowLong is a read of memory USER
 * already has, and the walk does it anyway for `visible` and `enabled`, so
 * the role costs nothing at all. Checked state is BM_GETCHECK, one window
 * message. A combo's selection is CB_GETCURSEL and CB_GETLBTEXT.
 *
 * So this times a Win32-only enrichment against the same nodes MSAA was
 * timed on, and reports how many of them it could actually classify --
 * because a fast answer for a third of the desktop is not a win.
 */
static const char *role_from_style(const char *cls, DWORD style)
{
    if (!lstrcmpiA(cls, "Button")) {
        switch (style & 0x0FL) {              /* BS_TYPEMASK, low nibble */
        case BS_PUSHBUTTON: case BS_DEFPUSHBUTTON: return "pushbutton";
        case BS_CHECKBOX:   case BS_AUTOCHECKBOX:  return "checkbox";
        case BS_RADIOBUTTON:
        case BS_AUTORADIOBUTTON:                   return "radiobutton";
        case BS_GROUPBOX:                          return "grouping";
        case BS_3STATE: case BS_AUTO3STATE:        return "checkbox";
        default:                                   return "pushbutton";
        }
    }
    if (!lstrcmpiA(cls, "Edit"))           return "text";
    if (!lstrcmpiA(cls, "Static"))         return "statictext";
    if (!lstrcmpiA(cls, "ListBox"))        return "list";
    if (!lstrcmpiA(cls, "ComboBox"))       return "combobox";
    if (!lstrcmpiA(cls, "ScrollBar"))      return "scrollbar";
    if (!lstrcmpiA(cls, "SysListView32"))  return "list";
    if (!lstrcmpiA(cls, "SysTreeView32"))  return "outline";
    if (!lstrcmpiA(cls, "SysTabControl32"))return "pagetablist";
    if (!lstrcmpiA(cls, "ToolbarWindow32"))return "toolbar";
    if (!lstrcmpiA(cls, "msctls_statusbar32")) return "statusbar";
    if (!lstrcmpiA(cls, "msctls_progress32"))  return "progressbar";
    if (!lstrcmpiA(cls, "msctls_trackbar32"))  return "slider";
    return 0;                                  /* unknown to us */
}

static void probe_win32rich(void)
{
    DWORD t0, t1;
    int i, classified, checkable, checked;
    char cls[64];

    head("0.13g  what Win32 can answer without COM at all");

    g_uiw_n = 0;
    EnumWindows(collect_top, 0);
    say("%d visible windows", g_uiw_n);

    classified = checkable = checked = 0;
    t0 = GetTickCount();
    for (i = 0; i < g_uiw_n; i++) {
        DWORD style;
        const char *role;
        cls[0] = '\0';
        GetClassNameA(g_uiw[i], cls, sizeof(cls));
        style = (DWORD)GetWindowLongA(g_uiw[i], GWL_STYLE);
        role = role_from_style(cls, style);
        if (role) classified++;
        /* The one state MSAA is usually wanted for, by message. */
        if (role && (!lstrcmpA(role, "checkbox")
                     || !lstrcmpA(role, "radiobutton"))) {
            DWORD_PTR res = 0;
            checkable++;
            if (SendMessageTimeoutA(g_uiw[i], BM_GETCHECK, 0, 0,
                                    SMTO_ABORTIFHUNG, 200, &res) && res)
                checked++;
        }
    }
    t1 = GetTickCount();
    say("style-derived role : %d of %d classified in %lu ms (%lu us/node)",
        classified, g_uiw_n, (unsigned long)(t1 - t0),
        (unsigned long)(t1 - t0) * 1000 / (g_uiw_n ? g_uiw_n : 1));
    say("BM_GETCHECK        : %d checkable, %d checked", checkable, checked);

    /* The same nodes through MSAA, for the role only, so the comparison is
     * like for like: one property, same windows, same moment. */
    {
        HMODULE acc = LoadLibraryA("oleacc.dll");
        PFN_AOFW aofw = acc
            ? (PFN_AOFW)GetProcAddress(acc, "AccessibleObjectFromWindow") : 0;
        if (aofw) {
            int got = 0;
            CoInitialize(NULL);
            /* warm */
            for (i = 0; i < g_uiw_n && i < 10; i++) {
                IAccessible *ia = 0;
                if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                         (void **)&ia) == S_OK && ia)
                    ia->lpVtbl->Release(ia);
            }
            t0 = GetTickCount();
            for (i = 0; i < g_uiw_n; i++) {
                IAccessible *ia = 0;
                if (aofw(g_uiw[i], OBJID_CLIENT, &RC_IID_IAccessible,
                         (void **)&ia) == S_OK && ia) {
                    VARIANT self, vr;
                    V_VT(&self) = VT_I4; V_I4(&self) = 0;
                    V_VT(&vr) = VT_EMPTY;
                    if (ia->lpVtbl->get_accRole(ia, self, &vr) == S_OK)
                        got++;
                    ia->lpVtbl->Release(ia);
                }
            }
            t1 = GetTickCount();
            say("MSAA role, same set: %d of %d in %lu ms (%lu us/node)",
                got, g_uiw_n, (unsigned long)(t1 - t0),
                (unsigned long)(t1 - t0) * 1000 / (g_uiw_n ? g_uiw_n : 1));
            CoUninitialize();
        }
        if (acc) FreeLibrary(acc);
    }
    say("unclassified are custom classes, where MSAA is the only source.");
}

/* ------------------------------------------------------------- 0.16 */
/* ImageClick: find a small bitmap on screen and click it. The question is
 * not whether it works -- it is a scan, it always works -- but WHERE the
 * scan runs.
 *
 * On the bridge it is free and correct, at the cost of shipping a capture
 * across the wire first. On the guest it costs no wire at all and spends
 * P200 cycles on the thing a P200 is worst at. "The guest transforms
 * nothing" says bridge, and this section is here to find out how badly the
 * rule would hurt if it were broken, because a number beats an argument.
 *
 * Two strategies, because the naive one is not what anyone would ship:
 *
 *   naive     compare the template at every position
 *   filtered  scan for the template's first byte, compare only on a hit
 *
 * The filtered one is what a real implementation does and can be
 * hundreds of times faster on a screen with large flat areas -- or barely
 * faster, if the first byte happens to be the desktop's own colour. Both
 * are measured against the same screen, which is whatever is in front of
 * the machine, so the ratio is a real-world one and not a contrived best
 * case.
 */
static void probe_imgfind(void)
{
    static unsigned char shot[512 * 1024];
    static unsigned char info[sizeof(BITMAPINFOHEADER) + 256 * 4];
    static unsigned char tmpl[32 * 32];
    BITMAPINFOHEADER *bi = (BITMAPINFOHEADER *)info;
    const int TW = 32, TH = 32;
    HDC screen, mem;
    HBITMAP area, oldbmp;
    int w, h, rows, sx, sy, x, y, i, j;
    unsigned long stride;
    DWORD t0, t1;
    int found_x, found_y, hits;

    head("0.16  ImageClick: what a template scan costs on this machine");

    screen = GetDC(NULL);
    if (!screen) { say("GetDC failed"); return; }
    w = GetDeviceCaps(screen, HORZRES);
    h = GetDeviceCaps(screen, VERTRES);

    /* 8bpp, because a template match wants indices to compare, not colour
     * triples, and because it is the depth a capture would arrive in. */
    stride = (((unsigned long)w * 8 + 31) / 32) * 4;
    rows = (int)(sizeof(shot) / stride);
    if (rows > h) rows = h;
    say("screen %dx%d; searching %dx%d (%lu bytes), template %dx%d",
        w, h, w, rows, stride * rows, TW, TH);

    mem = CreateCompatibleDC(screen);
    area = CreateCompatibleBitmap(screen, w, rows);
    oldbmp = (HBITMAP)SelectObject(mem, area);
    BitBlt(mem, 0, 0, w, rows, screen, 0, 0, SRCCOPY);
    memset(info, 0, sizeof(info));
    bi->biSize = sizeof(BITMAPINFOHEADER);
    bi->biWidth = w; bi->biHeight = rows; bi->biPlanes = 1;
    bi->biBitCount = 8; bi->biCompression = BI_RGB;
    if (GetDIBits(mem, area, 0, rows, shot, (BITMAPINFO *)info,
                  DIB_RGB_COLORS) != rows) {
        say("GetDIBits failed; cannot measure");
        goto done;
    }

    /* Lift the template from the most VARIED block on screen, not from the
     * middle. Taking the middle produced a uniform patch of desktop, which
     * matches at (0,0) on the first comparison, and both strategies then
     * "found" it in 0ms having measured nothing at all. A template worth
     * timing is one that appears once, which means one with detail in it.
     *
     * Scored by how many distinct byte values a block holds: flat colour
     * scores 1, a button with text on it scores dozens. */
    {
        int bestscore = -1, cx, cy;
        sx = sy = 0;
        for (cy = 0; cy + TH <= rows; cy += TH) {
            for (cx = 0; cx + TW <= w; cx += TW) {
                unsigned char seen[256];
                int score = 0;
                memset(seen, 0, sizeof(seen));
                for (j = 0; j < TH; j++)
                    for (i = 0; i < TW; i++) {
                        unsigned char v = shot[(cy + j) * stride + cx + i];
                        if (!seen[v]) { seen[v] = 1; score++; }
                    }
                if (score > bestscore) { bestscore = score; sx = cx; sy = cy; }
            }
        }
        say("template from (%d,%d), %d distinct values", sx, sy, bestscore);
        if (bestscore < 8) {
            say("   *** screen too uniform to time a search honestly.");
            say("   *** open a window with text in it and run this again.");
        }
    }
    for (j = 0; j < TH; j++)
        for (i = 0; i < TW; i++)
            tmpl[j * TW + i] = shot[(sy + j) * stride + sx + i];

    /* Naive. */
    found_x = found_y = -1;
    t0 = GetTickCount();
    for (y = 0; y <= rows - TH && found_y < 0; y++) {
        for (x = 0; x <= w - TW; x++) {
            int ok = 1;
            for (j = 0; j < TH && ok; j++)
                for (i = 0; i < TW; i++)
                    if (shot[(y + j) * stride + x + i] != tmpl[j * TW + i])
                        { ok = 0; break; }
            if (ok) { found_x = x; found_y = y; break; }
        }
    }
    t1 = GetTickCount();
    say("naive     : %lu ms, found at (%d,%d)",
        (unsigned long)(t1 - t0), found_x, found_y);

    /* Filtered: look for the first byte, only then compare the block. */
    found_x = found_y = -1;
    hits = 0;
    t0 = GetTickCount();
    for (y = 0; y <= rows - TH && found_y < 0; y++) {
        const unsigned char *row = shot + (unsigned long)y * stride;
        for (x = 0; x <= w - TW; x++) {
            int ok = 1;
            if (row[x] != tmpl[0]) continue;
            hits++;
            for (j = 0; j < TH && ok; j++)
                for (i = 0; i < TW; i++)
                    if (shot[(y + j) * stride + x + i] != tmpl[j * TW + i])
                        { ok = 0; break; }
            if (ok) { found_x = x; found_y = y; break; }
        }
    }
    t1 = GetTickCount();
    say("filtered  : %lu ms, found at (%d,%d), %d first-byte hits",
        (unsigned long)(t1 - t0), found_x, found_y, hits);
    say("   (a correct run finds it at or before (%d,%d))", sx, sy);

    /* THE ABSENT TEMPLATE, WHICH IS THE CASE NOBODY HAD EVER TIMED.
     *
     * Both scans above stop at the first hit, so what they measure is "how
     * long to find something that is there" -- and the template is lifted
     * from this very screen, so it is always there, usually early. The
     * expensive case is the opposite one: a template that is NOT on screen,
     * where there is no early exit and every position is visited. That is
     * also the case a real ImageClick hits whenever it is asked to wait for
     * something that has not appeared yet, which is most of the time.
     *
     * A guaranteed miss is constructed rather than hoped for: histogram the
     * capture, find a byte value that does not occur in it at all, and fill
     * the template with it. A block requiring a colour the screen does not
     * contain cannot match anywhere. If all 256 values are in use the
     * rarest is used instead, which is not a proof, so the result is
     * checked and labelled rather than assumed. */
    {
        unsigned long freq[256];
        unsigned long best;
        int v, pick, guaranteed;
        unsigned long npos;

        for (v = 0; v < 256; v++) freq[v] = 0;
        for (y = 0; y < rows; y++)
            for (x = 0; x < w; x++)
                freq[shot[(unsigned long)y * stride + x]]++;
        pick = 0; best = freq[0];
        for (v = 1; v < 256; v++)
            if (freq[v] < best) { best = freq[v]; pick = v; }
        guaranteed = (best == 0);
        for (j = 0; j < TH; j++)
            for (i = 0; i < TW; i++)
                tmpl[j * TW + i] = (unsigned char)pick;
        say("absent template: solid index %d, which occurs %lu times%s",
            pick, best, guaranteed ? " (so a miss is certain)"
                                   : " (rarest, not absent -- see below)");

        npos = (unsigned long)(rows - TH + 1) * (unsigned long)(w - TW + 1);

        found_x = found_y = -1;
        t0 = GetTickCount();
        for (y = 0; y <= rows - TH && found_y < 0; y++) {
            for (x = 0; x <= w - TW; x++) {
                int ok = 1;
                for (j = 0; j < TH && ok; j++)
                    for (i = 0; i < TW; i++)
                        if (shot[(y + j) * stride + x + i] != tmpl[j * TW + i])
                            { ok = 0; break; }
                if (ok) { found_x = x; found_y = y; break; }
            }
        }
        t1 = GetTickCount();
        say("naive miss     : %lu ms over %lu positions%s",
            (unsigned long)(t1 - t0), npos,
            found_y < 0 ? "" : " *** MATCHED, so this is not a miss case");

        found_x = found_y = -1;
        hits = 0;
        t0 = GetTickCount();
        for (y = 0; y <= rows - TH && found_y < 0; y++) {
            const unsigned char *row = shot + (unsigned long)y * stride;
            for (x = 0; x <= w - TW; x++) {
                int ok = 1;
                if (row[x] != tmpl[0]) continue;
                hits++;
                for (j = 0; j < TH && ok; j++)
                    for (i = 0; i < TW; i++)
                        if (shot[(y + j) * stride + x + i] != tmpl[j * TW + i])
                            { ok = 0; break; }
                if (ok) { found_x = x; found_y = y; break; }
            }
        }
        t1 = GetTickCount();
        say("filtered miss  : %lu ms, %d first-byte hits%s",
            (unsigned long)(t1 - t0), hits,
            found_y < 0 ? "" : " *** MATCHED, so this is not a miss case");
        say("   The miss is the cost a wait pays on every poll where the");
        say("   thing has not appeared. The hit figures above are the floor.");
    }

    /* How much of the screen was actually searched, stated as a number
     * rather than left as a caveat somebody has to recompute. */
    if (rows < h)
        say("NOTE: only %d of %d rows fit in the %lu-byte buffer, so the "
            "bottom %d rows were not searched",
            rows, h, (unsigned long)sizeof(shot), h - rows);
    else
        say("whole screen searched: %d of %d rows fit the buffer", rows, h);

done:
    SelectObject(mem, oldbmp);
    DeleteObject(area);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
}


/* --------------------------------------------------------------------
 * power -- can this machine be stopped from putting itself to sleep?
 *
 * Two questions the documentation could not answer and Wine cannot answer
 * either, which is what this program is for.
 *
 * 1. DOES THIS KERNEL32 EXPORT SetThreadExecutionState? Microsoft documents a
 *    minimum of Windows XP and says nothing about 9x, but the Windows 2000
 *    Application Specification makes the OnNow requirement "indicate busy
 *    application status properly" mandatory for WINDOWS 98 and names that call
 *    as the way to do it. No export list settled it, so ask the machine. If it
 *    is here, gremote's Prevent sleep uses it and never fakes input; if it is
 *    not, the zero-delta mouse_event nudge is the layer that has to carry the
 *    idle case (win/remote/tray.h, mechanisms 3 and 4).
 *
 * 2. DOES A RUNNING gremote ACTUALLY REFUSE A SUSPEND? Both veto messages are
 *    sent at its hidden tray window and the replies are printed. This is the
 *    one part of Prevent sleep that can be exercised on a machine with no APM,
 *    because the message is synthesised here rather than waiting for a power
 *    driver the QEMU guest does not have at all -- so it is also what
 *    tests/python/remote/test_sleep.py drives under Wine.
 *
 * What this CANNOT tell you, and nothing but an idle real machine can: whether
 * 98 sends either message of its own accord when the standby timer expires.
 * For that, leave the box alone past its Power Management timeout with
 * prevent_sleep=0 and read GREMOTE.LOG -- gremote logs the query even when it
 * is going to say yes, precisely so that measurement does not change the
 * machine's behaviour.
 *
 * SendMessageTimeout and never SendMessage: the target is another process and
 * a probe must not be able to wedge on it.
 */
static void probe_power(void)
{
    typedef DWORD (WINAPI *ExecStateFn)(DWORD);
    HMODULE k32;
    ExecStateFn set_exec;
    HWND tray;
    DWORD_PTR reply;

    say("");
    say("=== power  prevent sleep: the mechanisms ===");

    /* KERNEL32 is already mapped in every process; this is a refcount. */
    k32 = LoadLibraryA("KERNEL32.DLL");
    set_exec = k32 ? (ExecStateFn)GetProcAddress(k32, "SetThreadExecutionState")
                   : NULL;
    if (set_exec)
    {
        /* Take the hold and put it straight back, so this probe does not leave
         * the machine awake behind it. ES_CONTINUOUS alone clears it. */
        DWORD was = set_exec(ES_CONTINUOUS | ES_SYSTEM_REQUIRED);
        set_exec(ES_CONTINUOUS);
        say("SetThreadExecutionState : PRESENT, returned %08lx -- gremote will "
            "use it and will NOT fake input", (unsigned long)was);
    }
    else
    {
        say("SetThreadExecutionState : absent -- expected on 98; the nudge is "
            "what has to carry the idle case");
    }
    if (k32) FreeLibrary(k32);

    tray = FindWindowA("GastonRemoteTray", NULL);
    if (!tray)
    {
        say("gremote window          : NOT FOUND -- start gremote.exe first, "
            "then run this again");
        return;
    }
    say("gremote window          : found");

    /* BROADCAST_QUERY_DENY is 0x424d5144; TRUE means it agreed to suspend. */
    reply = 0;
    if (!SendMessageTimeoutA(tray, WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0,
                             SMTO_ABORTIFHUNG, 5000, &reply))
        say("WM_POWERBROADCAST reply : NO ANSWER (timed out or hung)");
    else
        say("WM_POWERBROADCAST reply : %08lx -- %s", (unsigned long)reply,
            reply == (DWORD_PTR)BROADCAST_QUERY_DENY
                ? "BROADCAST_QUERY_DENY, so Prevent sleep is ON and refusing"
                : "not a denial, so Prevent sleep is off");

    /* And the obsolete one, which is the message whose delivery set Microsoft
     * actually documents (each window EnumWindows returns). PWR_FAIL is -1. */
    reply = 0;
    if (!SendMessageTimeoutA(tray, WM_POWER, PWR_SUSPENDREQUEST, 0,
                             SMTO_ABORTIFHUNG, 5000, &reply))
        say("WM_POWER reply          : NO ANSWER (timed out or hung)");
    else
        say("WM_POWER reply          : %08lx -- %s", (unsigned long)reply,
            (LONG)reply == PWR_FAIL
                ? "PWR_FAIL, so Prevent sleep is ON and refusing"
                : "not a refusal, so Prevent sleep is off");
}

/* -------------------------------------------------------------------- */
int main(int argc, char **argv)
{
    const char *only = (argc > 1) ? argv[1] : NULL;

    /* Append, not truncate: the sections are usually run one at a time
     * ("probe threads", "probe exec", ...) and a truncating open means the
     * log only ever holds whichever section ran last. */
    g_log = fopen("PROBE.LOG", "a");
    /* No COM1 here, deliberately.
     *
     * This used to open the port itself and it never once worked: Win98
     * binds no driver to the port QEMU adds, so CreateFile failed and the
     * writes went nowhere. What carried results off the VM was the SHELL's
     * redirection -- `PROBE.EXE section > COM1` -- which reaches the
     * hardware without the Windows COM driver and does not involve this
     * program at all.
     *
     * On a real machine it would be worse than useless. A physical UART
     * means CreateFile SUCCEEDS, and then WriteFile can block forever
     * waiting on CTS from a cable that is not there, hanging a run that has
     * no serial capture set up anyway. PROBE.LOG is the record.
     */
    say("Gaston Code Phase 0 probe");
    say("results are appended to PROBE.LOG beside this exe");
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
    if (!only || !strcmp(only, "msaacost")) probe_msaacost();
    if (!only || !strcmp(only, "msaafast")) probe_msaafast();
    if (!only || !strcmp(only, "win32rich")) probe_win32rich();
    if (!only || !strcmp(only, "dom"))      probe_dom();
    if (!only || !strcmp(only, "depths"))   probe_depths();
    if (!only || !strcmp(only, "verify"))   probe_verify();
    if (!only || !strcmp(only, "hooks"))    probe_hooks();
    if (!only || !strcmp(only, "imgfind"))  probe_imgfind();
    if (!only || !strcmp(only, "events"))   probe_events();
    if (!only || !strcmp(only, "evlat"))    probe_evlat();
    /* THE THREE INJECTION GATES ARE NOT IN THE NO-ARGUMENT SWEEP, and that
     * is the one difference in this list. They load a DLL into programs
     * that did not ask for it; they have killed Explorer once; and one
     * session ended with the guest spinning at a full vCPU hours after they
     * had all passed, with causation never established (BACKLOG 15). The
     * no-argument path is what an operator types to "just run the probe",
     * and it should not be able to reach them by accident. TEST.BAT runs
     * them last, on their own, behind a prompt, with a health check after.
     * Naming one still runs it. */
    if (only && !strcmp(only, "inject"))   probe_inject();
    if (only && !strcmp(only, "targeted")) probe_targeted();
    if (only && !strcmp(only, "gditext"))  probe_gditext();
    if (!only || !strcmp(only, "waitfor"))  probe_waitfor();
    if (!only || !strcmp(only, "power"))    probe_power();

    say("");
    say("=== done -- results also in PROBE.LOG ===");
    if (g_log) fclose(g_log);
    return 0;
}
