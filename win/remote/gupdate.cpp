/* gupdate.exe -- the one program on this machine that can replace gremote.
 *
 * WHY IT EXISTS AT ALL. `ship()` ends the program holding the target image
 * and then writes over it, which works for every program on the box except
 * the one doing the shipping: gremote's own pid is on `Processes::kill`'s
 * permanent deny list, so the call comes back `denied` and a new build can
 * be deployed everywhere except where it is most needed. Measured on the
 * real hardware, 2026-10-01. The missing piece is not a smarter kill -- it
 * is a SECOND process, small enough to be beneath suspicion, that outlives
 * the server and puts the new image in place after it has gone.
 *
 *   gupdate -src <staged> -dst <image> [-mutex <name>] [-wait ms]
 *           [-verify ms]
 *
 * It waits, copies, swaps, relaunches, exits. It never listens on anything
 * and it never asks a question.
 *
 * HOW IT KNOWS THE OLD PROCESS HAS GONE, and why it is two tests and not
 * one. Both are polls; they answer different questions and the program
 * needs both answered.
 *
 *   The image lock. On Windows 98 a running .exe is LOCKED -- a write to
 *   its image is refused (measured 2026-09-27, docs/REMOTE.md step 4). So
 *   "can I open the destination for writing" IS the question, asked
 *   directly, rather than a proxy for it. The alternative considered was a
 *   pid handed over at launch and waited on with OpenProcess: that needs
 *   the pid, needs OpenProcess to behave the same on 9x as on NT, survives
 *   pid reuse only by luck, and -- the part that decides it -- answers a
 *   DIFFERENT question. A process handle going signalled does not promise
 *   the loader has released the image; the open does, because the open is
 *   the operation that is about to happen anyway. No pid, no process API,
 *   no new failure mode, and the test is the act.
 *
 *   The single-instance mutex. gremote holds `GastonRemoteSingleInstance`
 *   for as long as it runs, and a second copy started while the first is
 *   alive RAISES NOTHING AND EXITS -- which is ship()'s trap 2, and here it
 *   would read as a successful relaunch that changed nothing. So the
 *   relaunch waits for the named object to disappear, which happens when
 *   the last handle to it closes, which is when the old process is really
 *   gone. `-mutex` is optional and the program is generic without it; the
 *   lock test alone is enough to write the file, but not enough to start
 *   the replacement.
 *
 *   They disagree on Wine, which is the useful part: Wine does NOT lock a
 *   running image, so under Wine only the mutex test says anything, and on
 *   Win98 both do. A wait built on either one alone would pass its tests on
 *   one platform and race on the other.
 *
 * WHAT HAPPENS WHEN THE WAIT RUNS OUT. The worst outcome available to this
 * program is a machine with no server and no explanation, so there is no
 * path out of here that leaves nothing running. If the wait expires nothing
 * is written at all -- the old image is still whole, still locked, probably
 * still running -- and the destination is launched anyway before exiting
 * non-zero. Either the old process is alive, in which case the launch hits
 * the single-instance mutex and quietly does nothing, or it died in some
 * way that left the image held, in which case the launch is the recovery.
 * Every other failure path does the same: put back what was there, start
 * it, say so in GUPDATE.LOG.
 *
 * THE WAY BACK. The previous image is not deleted, it is renamed --
 * GREMOTE.EXE becomes GREMOTE.OLD -- so a person at the keyboard always has
 * one command between them and the build that worked. On top of that, one
 * automatic rollback is implemented because it costs a single wait on a
 * handle this program already holds: if the relaunched image EXITS within
 * `-verify` milliseconds, it did not come up, and the .OLD is moved back
 * and started instead. That catches the failure this platform is famous
 * for -- an NT-only static import kills the image at load, before WinMain,
 * with no log written -- and it catches a build that refuses its own
 * config.
 *
 * WHAT IT DOES NOT CATCH, written down rather than pretended at: an image
 * that loads, stays up, and never answers. A server that starts and then
 * fails to bind, or wedges before its accept loop, looks exactly like a
 * healthy one from here. Settling that needs a connect-back probe -- a
 * socket to 127.0.0.1 on gremote's configured port, inside the verify
 * window, which means this program would have to read gremote.ini and link
 * Winsock. That is a real option and not a large one; it is deliberately
 * not taken here, because the caller on the other end of the wire is
 * ALREADY doing exactly that probe (gremote/ship.py `self_update`, which
 * reconnects and reports the version), and a second copy of the judgement
 * inside a program that cannot report its findings to anybody is worth less
 * than the one that can.
 *
 * NO App, AND WHY. CLAUDE.md puts new C++ under `App`'s lifecycle, and the
 * reason is file statics and a message loop that has to pump. This program
 * has neither: no services, no window, no messages, and a total lifetime
 * measured in seconds. App's hidden window would make it answer
 * WM_ENDSESSION, which for a program whose entire job is to outlive the
 * thing that just stopped is the wrong instinct. What the rule is actually
 * protecting -- nothing at file scope with a constructor -- holds here:
 * every object below lives in a local, in a guard from core/rc_cpp.h.
 *
 * SUBSYSTEM nt_win, not nt. A console image opens a console when it starts,
 * and this one is started by a service that may be running before anybody
 * has logged on.
 */
#include <windows.h>
#include <stdlib.h>
#include <string.h>

#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

/* Every exit code means something, because the only other thing this
 * program can tell anyone is GUPDATE.LOG and a person has to go and read
 * that. 0 is the only one where the NEW image is running. */
enum ExitCode
{
    EX_OK = 0,
    EX_BAD_ARGS = 1,      /* nothing was attempted */
    EX_WAIT_EXPIRED = 2,  /* nothing written; destination launched anyway */
    EX_COPY_FAILED = 3,   /* nothing written; destination launched anyway */
    EX_SWAP_FAILED = 4,   /* old image back in place and launched */
    EX_ROLLED_BACK = 5,   /* the new image did not stay up; old one running */
    EX_NOTHING_RUNS = 6   /* the loud one: the launch itself failed */
};

enum
{
    POLL_MS = 250,
    /* Long enough for gremote's own shutdown -- its accept loop wakes
     * within 250ms and `joinOrLeak` gives the server 8 seconds -- plus a
     * 1998 platter having a bad moment. Bounded, because an unbounded wait
     * here is a machine with no server and nobody watching. */
    DEFAULT_WAIT_MS = 60000,
    MIN_WAIT_MS = 1000,
    MAX_WAIT_MS = 600000,
    /* How long the replacement has to stay up before it counts as started.
     * An image that dies at load dies in well under a second; this is that
     * with room for a cold start off a slow disk. */
    DEFAULT_VERIFY_MS = 4000,
    MAX_VERIFY_MS = 60000
};

/* --- the command line ---------------------------------------------------
 *
 * Hand-parsed rather than taken from the CRT's argv, for one reason: a path
 * with a space in it. `C:\Program Files\...` is an ordinary answer to where
 * anything lives, and a tokeniser that gets quoting wrong here silently
 * installs a build at the wrong path. Refuses a token too long for its
 * buffer rather than truncating it -- a truncated path is a different file,
 * which is win/core/str.h's whole argument.
 */
enum TokenResult { TOK_NONE = 0, TOK_OK, TOK_TOO_LONG };

static TokenResult nextToken(const char **cursor, char *out, unsigned cap)
{
    const char *p = *cursor;
    unsigned n = 0;
    bool quoted = false;

    while (*p == ' ' || *p == '\t')
        p++;
    if (!*p)
    {
        *cursor = p;
        return TOK_NONE;
    }
    for (; *p; p++)
    {
        if (*p == '"')
        {
            quoted = !quoted;
            continue;
        }
        if (!quoted && (*p == ' ' || *p == '\t'))
            break;
        if (n + 1 >= cap)
        {
            *cursor = p;
            return TOK_TOO_LONG;
        }
        out[n++] = *p;
    }
    out[n] = '\0';
    *cursor = p;
    return TOK_OK;
}

struct Args
{
    Path src;
    Path dst;
    Buf<64> mutexName;
    long waitMs;
    long verifyMs;

    /* C++98 does not default-initialise a non-POD, and this one is a local
     * rather than a static, so every member is set here or it is garbage. */
    Args() : waitMs(DEFAULT_WAIT_MS), verifyMs(DEFAULT_VERIFY_MS) {}
};

static long clampLong(long v, long lo, long hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

/* True if the line parsed into something usable. `why` is for the log. */
static bool parseArgs(const char *cmdLine, Args &a, const char **why)
{
    const char *p = cmdLine ? cmdLine : "";
    Path flag;
    Path value;

    for (;;)
    {
        TokenResult t = nextToken(&p, flag.raw(), flag.cap());
        if (t == TOK_NONE)
            break;
        if (t == TOK_TOO_LONG)
        {
            *why = "a switch is longer than MAX_PATH";
            return false;
        }
        if (nextToken(&p, value.raw(), value.cap()) != TOK_OK)
        {
            *why = "a switch has no value, or one too long to hold";
            return false;
        }
        if (strcmp(flag.str(), "-src") == 0)
            a.src.set(value.str());
        else if (strcmp(flag.str(), "-dst") == 0)
            a.dst.set(value.str());
        else if (strcmp(flag.str(), "-mutex") == 0)
            a.mutexName.set(value.str());
        else if (strcmp(flag.str(), "-wait") == 0)
            a.waitMs = clampLong(strtol(value.str(), NULL, 10),
                                 MIN_WAIT_MS, MAX_WAIT_MS);
        else if (strcmp(flag.str(), "-verify") == 0)
            a.verifyMs = clampLong(strtol(value.str(), NULL, 10),
                                   0, MAX_VERIFY_MS);
        else
        {
            /* Refused, not ignored. gremote builds this line itself, so an
             * unknown switch means the two halves have drifted -- and
             * carrying on would install something with the wrong settings
             * rather than not at all. */
            *why = "unknown switch";
            return false;
        }
    }
    if (a.src.empty() || a.dst.empty())
    {
        *why = "need -src and -dst";
        return false;
    }
    if (!a.src.ok() || !a.dst.ok() || !a.mutexName.ok())
    {
        *why = "a path did not fit";
        return false;
    }
    return true;
}

/* --- names beside the destination --------------------------------------
 *
 * GREMOTE.EXE -> GREMOTE.OLD and GREMOTE.NEW. The extension is REPLACED
 * rather than appended, so the result is still an 8.3 name on a FAT volume
 * -- `GREMOTE.EXE.OLD` has no 8.3 spelling and the one the filesystem
 * invents is not a name anybody can type back.
 */
static bool sibling(Path &out, const char *path, const char *ext)
{
    const char *base = path;
    const char *q;
    const char *dot = 0;

    for (q = path; *q; q++)
        if (*q == '\\' || *q == '/' || *q == ':')
            base = q + 1;
    for (q = base; *q; q++)
        if (*q == '.')
            dot = q;
    if (dot)
        out.setn(path, (unsigned)(dot - path));
    else
        out.set(path);
    out.cat(ext);
    return out.ok() && !out.empty();
}

static void directoryOf(Path &out, const char *path)
{
    const char *q;
    const char *end = path;

    for (q = path; *q; q++)
        if (*q == '\\' || *q == '/')
            end = q;
    out.setn(path, (unsigned)(end - path));
}

static bool fileExists(const char *path)
{
    DWORD a = GetFileAttributesA(path);
    return a != 0xFFFFFFFF && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

/* -1 when it cannot be read. GetFileSize rather than GetFileAttributesEx:
 * the latter is on the NT-only deny list (tools/check/imports.py). */
static long fileSize(const char *path)
{
    Handle f(CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                         OPEN_EXISTING, 0, NULL));
    DWORD n;
    if (!f.valid())
        return -1;
    n = GetFileSize(f.get(), NULL);
    return n == 0xFFFFFFFF ? -1 : (long)n;
}

/* Best effort. A file off a CD arrives read-only and every rename and
 * delete below would then fail for a reason nobody would guess from the
 * error code. */
static void clearReadOnly(const char *path)
{
    DWORD a = GetFileAttributesA(path);
    if (a != 0xFFFFFFFF && (a & FILE_ATTRIBUTE_READONLY))
        SetFileAttributesA(path, a & ~FILE_ATTRIBUTE_READONLY);
}

/* --- the two waits ------------------------------------------------------ */

static bool imageIsFree(const char *path)
{
    Handle h(CreateFileA(path, GENERIC_WRITE, 0, NULL, OPEN_EXISTING,
                         FILE_ATTRIBUTE_NORMAL, NULL));
    DWORD e;
    if (h.valid())
        return true;
    e = GetLastError();
    /* Nothing there is not "locked": a first install has nothing to wait
     * for, and so does a destination somebody moved out of the way. */
    return e == ERROR_FILE_NOT_FOUND || e == ERROR_PATH_NOT_FOUND;
}

static bool serverIsGone(const char *mutexName)
{
    if (!mutexName || !*mutexName)
        return true;
    /* The object lives as long as ANY handle to it does, so a failed open
     * is the honest signal that nobody holds it. Closed immediately --
     * holding it across the sleep would keep alive the very thing being
     * waited for. */
    Handle m(OpenMutexA(SYNCHRONIZE, FALSE, mutexName));
    return !m.valid();
}

/* True when both tests pass inside the budget. `waited` is filled either
 * way, for the log. DWORD subtraction is wrap-correct, which matters on a
 * box that has been up for 49 days. */
static bool waitForTheOldOneToGo(const Args &a, DWORD *waited)
{
    DWORD start = GetTickCount();
    for (;;)
    {
        DWORD spent;
        if (imageIsFree(a.dst.str()) && serverIsGone(a.mutexName.str()))
        {
            *waited = GetTickCount() - start;
            return true;
        }
        spent = GetTickCount() - start;
        if (spent >= (DWORD)a.waitMs)
        {
            *waited = spent;
            return false;
        }
        Sleep(POLL_MS);
    }
}

/* --- starting it again -------------------------------------------------- */

/* Start `exe` in its own directory. `stayedUpFor` is how long it survived,
 * and `*alive` whether it was still running at the end of the verify
 * window. A zero `verifyMs` skips the wait and reports alive. */
static bool relaunch(const char *exe, long verifyMs, bool *alive,
                     DWORD *stayedUpFor, DWORD *pid)
{
    Path dir;
    Path cmd;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    DWORD start;

    *alive = false;
    *stayedUpFor = 0;
    *pid = 0;

    directoryOf(dir, exe);
    /* Quoted: CreateProcess with a NULL application name splits an unquoted
     * line at the first space, and `C:\Program Files\...` is a real path. */
    if (!cmd.set("\"") || !cmd.cat(exe) || !cmd.cat("\""))
    {
        rc_log("gupdate: the relaunch command line does not fit");
        return false;
    }

    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_SHOWNORMAL;
    /* bInherit FALSE: nothing of ours follows a program meant to outlive
     * this one. */
    if (!CreateProcessA(NULL, cmd.raw(), NULL, NULL, FALSE, 0, NULL,
                        dir.empty() ? NULL : dir.str(), &si, &pi))
    {
        rc_log("gupdate: could not start %s: %lu", exe,
               (unsigned long)GetLastError());
        return false;
    }
    {
        Handle thread(pi.hThread);
        Handle proc(pi.hProcess);
        *pid = pi.dwProcessId;
        if (verifyMs <= 0)
        {
            *alive = true;
            return true;
        }
        /* The one piece of automatic rollback this program can honestly
         * make: an image that dies at load is gone before this returns,
         * and the handle says so without any API that might not be there. */
        start = GetTickCount();
        *alive = WaitForSingleObject(proc.get(), (DWORD)verifyMs)
                 != WAIT_OBJECT_0;
        *stayedUpFor = GetTickCount() - start;
        if (!*alive)
        {
            DWORD code = 0;
            GetExitCodeProcess(proc.get(), &code);
            rc_log("gupdate: %s started as %08lx and exited %lu after %lums",
                   exe, (unsigned long)*pid, (unsigned long)code,
                   (unsigned long)*stayedUpFor);
        }
    }
    return true;
}

/* Start the destination and say so. Used by every failure path: this
 * program does not return to a machine with nothing running. */
static int launchAndReport(const char *dst, int codeIfItWorks)
{
    bool alive = false;
    DWORD up = 0, pid = 0;
    if (!relaunch(dst, 0, &alive, &up, &pid))
    {
        rc_log("gupdate: NOTHING IS RUNNING -- %s would not start. The "
               "previous build, if there is one, is beside it with a .OLD "
               "extension.", dst);
        return EX_NOTHING_RUNS;
    }
    rc_log("gupdate: started %s as %08lx", dst, (unsigned long)pid);
    return codeIfItWorks;
}

/* --- the swap ----------------------------------------------------------- */

/* The whole swap is src -> dstNew -> (dst -> dstOld) -> dst, and this does
 * only the first arrow; WinMain does the two renames.
 *
 * Splitting it there is the point. The copy lands under its OWN name first,
 * so a short or refused copy never touches the image that is currently
 * working -- only once there is a verified file at `dstNew` does anything
 * move, and the instant with no file at `dst` is then the gap between two
 * renames rather than the length of a 240KB copy onto a 1998 platter.
 *
 * win/fs/path.h argues the opposite for the sync layer -- do not move the
 * old file aside unless something sweeps up afterwards -- and that argument
 * does not reach here. The aside IS the deliverable: `GREMOTE.OLD` is the
 * way back, it is meant to still be there tomorrow, and this program is
 * itself the recovery pass for the only window in which it could be
 * mistaken for debris.
 */
static bool installImage(const Args &a, const char *dstNew,
                         const char **why)
{
    long want = fileSize(a.src.str());
    long got;

    if (want <= 0)
    {
        *why = "the staged image is empty or unreadable";
        return false;
    }
    clearReadOnly(dstNew);
    DeleteFileA(dstNew);
    if (!CopyFileA(a.src.str(), dstNew, FALSE))
    {
        rc_log("gupdate: copy %s -> %s failed: %lu", a.src.str(), dstNew,
               (unsigned long)GetLastError());
        *why = "the copy failed";
        return false;
    }
    got = fileSize(dstNew);
    if (got != want)
    {
        rc_log("gupdate: %s is %ld bytes, the source is %ld", dstNew, got,
               want);
        DeleteFileA(dstNew);
        *why = "the copy came out a different size";
        return false;
    }
    return true;
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    Args a;
    Path logDir;
    Path self;
    Path dstOld;
    Path dstNew;
    const char *why = "";
    DWORD waited = 0;
    bool hadOld = false;

    (void)inst;
    (void)prev;
    (void)show;

    /* Beside this exe, which in the normal case is beside gremote.log --
     * the one place anybody will think to look. Opened before anything
     * else, for the same reason gremote opens its own first: on this
     * platform "no log file" is the primary diagnostic. */
    GetModuleFileNameA(NULL, self.raw(), self.cap());
    directoryOf(logDir, self.str());
    rc_log_open(logDir.str(), "gupdate");

    if (!parseArgs(cmd, a, &why))
    {
        rc_log("gupdate: %s. Usage: gupdate -src <file> -dst <file> "
               "[-mutex <name>] [-wait <ms>] [-verify <ms>]", why);
        rc_log_close();
        return EX_BAD_ARGS;
    }
    rc_log("gupdate: %s -> %s (wait %ldms, verify %ldms, mutex %s)",
           a.src.str(), a.dst.str(), a.waitMs, a.verifyMs,
           a.mutexName.empty() ? "(none)" : a.mutexName.str());

    if (!sibling(dstOld, a.dst.str(), ".OLD") ||
        !sibling(dstNew, a.dst.str(), ".NEW"))
    {
        rc_log("gupdate: cannot form a name beside %s", a.dst.str());
        rc_log_close();
        return EX_BAD_ARGS;
    }

    if (!waitForTheOldOneToGo(a, &waited))
    {
        int code;
        rc_log("gupdate: after %lums the destination is still held or the "
               "server is still registered. NOTHING HAS BEEN WRITTEN -- %s "
               "is the build that was already there.",
               (unsigned long)waited, a.dst.str());
        code = launchAndReport(a.dst.str(), EX_WAIT_EXPIRED);
        rc_log_close();
        return code;
    }
    rc_log("gupdate: the old process let go after %lums",
           (unsigned long)waited);

    if (!installImage(a, dstNew.str(), &why))
    {
        int code;
        rc_log("gupdate: %s. Nothing has been written; %s is untouched.",
               why, a.dst.str());
        code = launchAndReport(a.dst.str(), EX_COPY_FAILED);
        rc_log_close();
        return code;
    }

    /* Aside, then in. Two renames, microseconds apart. */
    hadOld = fileExists(a.dst.str());
    if (hadOld)
    {
        clearReadOnly(dstOld.str());
        DeleteFileA(dstOld.str());
        clearReadOnly(a.dst.str());
        if (!MoveFileA(a.dst.str(), dstOld.str()))
        {
            int code;
            rc_log("gupdate: could not move %s aside to %s: %lu",
                   a.dst.str(), dstOld.str(),
                   (unsigned long)GetLastError());
            DeleteFileA(dstNew.str());
            code = launchAndReport(a.dst.str(), EX_SWAP_FAILED);
            rc_log_close();
            return code;
        }
    }
    if (!MoveFileA(dstNew.str(), a.dst.str()))
    {
        int code;
        rc_log("gupdate: could not rename %s into place: %lu", dstNew.str(),
               (unsigned long)GetLastError());
        if (hadOld)
            MoveFileA(dstOld.str(), a.dst.str());
        code = launchAndReport(a.dst.str(), EX_SWAP_FAILED);
        rc_log_close();
        return code;
    }
    rc_log("gupdate: installed; the previous build is %s",
           hadOld ? dstOld.str() : "(there was none)");

    {
        bool alive = false;
        DWORD up = 0, pid = 0;
        bool started = relaunch(a.dst.str(), a.verifyMs, &alive, &up, &pid);
        if (started && alive)
        {
            rc_log("gupdate: %s is running as %08lx; done", a.dst.str(),
                   (unsigned long)pid);
            rc_log_close();
            return EX_OK;
        }
        /* It did not come up. This is the half of rollback that can be
         * decided from here -- see the header: an image that loads, stays
         * up and never answers still looks healthy to this program, and
         * the caller's reconnect is what settles that one. */
        if (!hadOld)
        {
            int code;
            rc_log("gupdate: the new image did not come up and there is no "
                   "previous build to go back to.");
            code = launchAndReport(a.dst.str(), EX_ROLLED_BACK);
            rc_log_close();
            return code;
        }
        rc_log("gupdate: ROLLING BACK -- the new image did not stay up. "
               "The one that failed is still at %s.", a.src.str());
        clearReadOnly(a.dst.str());
        DeleteFileA(a.dst.str());
        if (!MoveFileA(dstOld.str(), a.dst.str()))
        {
            rc_log("gupdate: the rollback rename failed: %lu. The previous "
                   "build is at %s and has to be put back by hand.",
                   (unsigned long)GetLastError(), dstOld.str());
            rc_log_close();
            return EX_NOTHING_RUNS;
        }
        {
            int code = launchAndReport(a.dst.str(), EX_ROLLED_BACK);
            rc_log_close();
            return code;
        }
    }
}
