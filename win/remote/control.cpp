#include "remote/control.h"

#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"
#include "exec/system.h"

/* A DATA body's worth of process lines. Well under the 64KB frame cap, and
 * small enough that a long listing starts arriving while it is still being
 * made rather than after. */
enum { PS_CHUNK = 8192 };

/* On 98, TerminateProcess returning TRUE is not the same as "gone", so a
 * kill waits a moment and says `pending` if the process is still there.
 * Short, because this runs on the reader. */
enum { KILL_WAIT_MS = 250 };

/* Long enough for one small frame to reach the wire on a LAN. ExitWindowsEx
 * does not come back, so an OK still in the out queue is an OK nobody ever
 * sees -- the caller would read the reboot as a crash. */
enum { REBOOT_FLUSH_MS = 2000 };

/* The updater, found beside this exe. Its absence is a refusal, not a
 * fallback: see update(). */
static const char UPDATER[] = "gupdate.exe";

/* The name gremote's own single-instance mutex goes by (remote/main.cpp).
 * Handed to the updater so it can tell "the old server has really gone"
 * from "the image happens to be writable", which are different facts and on
 * Wine only the second one is ever true. */
static const char INSTANCE_MUTEX[] = "GastonRemoteSingleInstance";

/* Same budget shape as REBOOT's: one small frame onto a LAN, before this
 * process stops being able to send anything. */
enum { UPDATE_FLUSH_MS = 2000 };

ControlOps::ControlOps(Processes &procs, Connection &conn, App &app)
    : m_procs(&procs), m_conn(&conn), m_app(&app)
{
}

void ControlOps::sysinfo(Responder &r)
{
    SystemInfo si;
    char memFree[16], uptime[16], memKb[16];
    const char *keys[3];
    const char *vals[3];

    readSystemInfo(si);
    rc_strf(memKb, sizeof memKb, "%lu", (unsigned long)si.memKb);
    rc_strf(memFree, sizeof memFree, "%lu", (unsigned long)si.memFreeKb);
    /* GetTickCount, so it wraps after 49.7 days. Sent raw: turning it into
     * something a person reads is the caller's job, like everything else. */
    rc_strf(uptime, sizeof uptime, "%lu", (unsigned long)si.uptimeMs);
    keys[0] = "mem_kb";      vals[0] = memKb;
    keys[1] = "mem_free_kb"; vals[1] = memFree;
    keys[2] = "uptime_ms";   vals[2] = uptime;
    /* No USER/GDI free-resource numbers: GetFreeSystemResources is 16-bit
     * only, and reaching for it from here would break this lane's one rule. */
    r.ok(keys, vals, 3);
}

/* Lines of "pid ppid threads exe", flushed to a DATA frame every PS_CHUNK.
 * The same spelling the client's PSLIST uses -- %08lx for the pids because
 * on 9x they are obfuscated pointers with the high bit set, which is not a
 * number anybody should be reading as signed. */
class PsChunks : public ProcessSink
{
public:
    PsChunks(Responder &r) : m_r(&r), m_len(0), m_count(0), m_stopped(false) {}

    bool onProcess(const ProcessInfo &p)
    {
        char line[MAX_PATH + 48];
        unsigned n;

        rc_strf(line, sizeof line, "%08lx %08lx %lu %s\n",
                (unsigned long)p.pid, (unsigned long)p.ppid,
                (unsigned long)p.threads, p.exe);
        n = (unsigned)strlen(line);
        if (m_len + n > sizeof m_buf && !flush())
            return false;
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_r->data(m_buf, m_len))
        {
            /* The caller is gone or too far behind. Stop walking rather
             * than finish a listing nobody will read. */
            m_stopped = true;
            return false;
        }
        m_len = 0;
        return true;
    }

    int count() const { return m_count; }
    bool stopped() const { return m_stopped; }

private:
    Responder *m_r;
    char m_buf[PS_CHUNK];
    unsigned m_len;
    int m_count;
    bool m_stopped;
};

void ControlOps::ps(Responder &r)
{
    char count[16];
    const char *keys[1];
    const char *vals[1];

    if (!m_procs->available())
    {
        r.err("no_toolhelp");
        return;
    }
    /* The listing is on the heap, not the stack: PsChunks carries an 8KB
     * buffer and the frame gate refuses any frame reaching one page. */
    UniqueObj<PsChunks> lines(new PsChunks(r));
    if (!lines.valid())
    {
        r.err("no_memory");
        return;
    }
    /* readKinds=false: whether a process is 16-bit costs a header read per
     * entry, and nothing in this listing reports it. */
    m_procs->list(*lines.get(), false);
    lines->flush();
    if (lines->stopped())
        return;     /* the link is gone; END would not reach it either */
    rc_strf(count, sizeof count, "%d", lines->count());
    keys[0] = "count";
    vals[0] = count;
    r.end(keys, vals, 1);
}

void ControlOps::kill(Responder &r, rc_frame *f)
{
    const char *pidText = rc_frame_get(f, "pid");
    const char *force = rc_frame_get(f, "force");
    const char *code = "failed";

    if (!pidText || !*pidText)
    {
        r.err("no_such_pid");
        return;
    }
    /* Base 16, matching what PS prints. */
    DWORD pid = (DWORD)strtoul(pidText, NULL, 16);
    switch (m_procs->kill(pid, force && strcmp(force, "1") == 0,
                          KILL_WAIT_MS))
    {
    case KILL_OK:
        r.ok();
        return;
    case KILL_PENDING:     code = "pending";     break;
    case KILL_DENIED:      code = "denied";      break;
    case KILL_DOS_SESSION: code = "dos_session"; break;
    case KILL_NO_SUCH_PID: code = "no_such_pid"; break;
    case KILL_NO_TOOLHELP: code = "no_toolhelp"; break;
    default:               code = "failed";      break;
    }
    rc_log("control: KILL %s -> %s", pidText, code);
    r.err(code);
}

void ControlOps::reboot(Responder &r, rc_frame *f)
{
    const char *confirm = rc_frame_get(f, "confirm");
    const char *mode = rc_frame_get(f, "mode");
    RebootMode which = REBOOT_RESTART;

    /* Not a default anyone can fall into. This ends every unsaved thing on
     * the machine, and it exists for a client that has wedged. */
    if (!confirm || strcmp(confirm, "yes") != 0)
    {
        r.err("unconfirmed");
        return;
    }
    if (mode && strcmp(mode, "logoff") == 0)
        which = REBOOT_LOGOFF;
    else if (mode && strcmp(mode, "poweroff") == 0)
        which = REBOOT_POWEROFF;
    else if (mode && strcmp(mode, "reboot") != 0 && *mode)
    {
        r.err("bad_mode");
        return;
    }

    rc_log("control: REBOOT mode=%s", mode ? mode : "reboot");
    r.ok();
    /* Before the machine goes, not after: ExitWindowsEx does not return. */
    m_conn->drain(REBOOT_FLUSH_MS);
    if (!requestReboot(which, false))
        rc_log("control: Windows refused the shutdown");
}

/* Is `path` plausibly a 32-bit Windows image for this machine?
 *
 * MZ, a PE signature where e_lfanew says, machine i386, PE32. Not a
 * validation of the program -- nothing short of running it is -- but it
 * separates "a build" from "a text file, a 16-bit exe, or an upload that
 * only got halfway", and those are the mistakes that are actually made.
 * Reading 90 bytes of a file that was written seconds ago is the whole
 * disk cost; see the lane note in update(). */
static bool isWin32Image(const char *path)
{
    unsigned char dos[64];
    unsigned char pe[26];
    DWORD got = 0;
    long off;

    Handle f(CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                         OPEN_EXISTING, 0, NULL));
    if (!f.valid())
        return false;
    if (!ReadFile(f.get(), dos, sizeof dos, &got, NULL) || got != sizeof dos)
        return false;
    if (dos[0] != 'M' || dos[1] != 'Z')
        return false;
    off = (long)dos[60] | ((long)dos[61] << 8) | ((long)dos[62] << 16) |
          ((long)dos[63] << 24);
    if (off < 64 || off > 0x100000L)
        return false;
    if (SetFilePointer(f.get(), off, NULL, FILE_BEGIN) == 0xFFFFFFFF)
        return false;
    if (!ReadFile(f.get(), pe, sizeof pe, &got, NULL) || got != sizeof pe)
        return false;
    if (pe[0] != 'P' || pe[1] != 'E' || pe[2] || pe[3])
        return false;
    if (pe[4] != 0x4c || pe[5] != 0x01)     /* IMAGE_FILE_MACHINE_I386 */
        return false;
    /* The optional header starts 20 bytes after the file header. */
    if (pe[24] != 0x0b || pe[25] != 0x01)   /* PE32, not PE32+ or ROM */
        return false;
    return true;
}

static bool samePath(const char *a, const char *b)
{
    Path fa, fb;
    if (!GetFullPathNameA(a, fa.cap(), fa.raw(), NULL) ||
        !GetFullPathNameA(b, fb.cap(), fb.raw(), NULL))
        return _stricmp(a, b) == 0;
    return _stricmp(fa.str(), fb.str()) == 0;
}

/* UPDATE -- replace this server with a build already staged on the disk,
 * and stop so that the replacement can happen.
 *
 * THE RULE THIS HAS TO ANSWER FOR. docs/REMOTE.md says, deliberately:
 * "There is no remote QUIT. Stopping gremote remotely is EXEC of something
 * that kills it, which is deliberate friction." A self-update stops the
 * process. So either that rule forbids self-update, or the rule is about
 * something narrower than "the process must never stop".
 *
 * It is the narrower thing. What the friction protects is the machine's
 * reachability: a caller must not be able to take the box away from itself
 * with one frame and nothing to show for it. REBOOT already stops this
 * process -- harder, and taking the whole machine with it -- and it is
 * allowed because at the end of it the box comes back. That is the actual
 * test, and it is the one applied here.
 *
 * SO THIS CANNOT BE USED AS A BARE QUIT, and not by promising. There is no
 * spelling of this op that means "stop". It stops only after it has, in
 * order: found the updater beside this exe; resolved its own image path;
 * been given a `src` that exists, is a different file, and reads as a
 * 32-bit PE; and successfully STARTED the updater process. Any of those
 * failing is an ERR and the server is still here. The destination is never
 * a header -- it is always this exe, read from the loader -- so UPDATE
 * cannot be pointed at anything else, and `ship()` remains the way to
 * replace any other program. The nearest thing to abuse left is uploading
 * the current build and updating to it, which is a restart with the same
 * binary; the box comes back, so it fails to be a quit.
 *
 * What a caller still cannot do is stop the server WITHOUT handing over a
 * replacement that the machine will then be running. EXEC-of-something-that-
 * kills-it is still the general way to stop it, and it still costs what it
 * cost before.
 *
 * WHY THE CONTROL LANE. It opens a file, which the lane's charter is wary
 * of -- but the read is 90 bytes of a file the caller wrote seconds ago,
 * and the alternative is worse: on the fs lane a stuck GET would be exactly
 * the state in which you cannot ship the fix for it. Same argument that
 * puts PS and KILL here.
 */
void ControlOps::update(Responder &r, rc_frame *f)
{
    const char *confirm = rc_frame_get(f, "confirm");
    const char *src = rc_frame_get(f, "src");
    const char *waitText = rc_frame_get(f, "wait_ms");
    Path self;
    Path dir;
    Path updater;
    /* Two full paths and five switches: MAX_PATH is not enough for the line
     * even when it is enough for each path in it. Still bounded, and still
     * refused rather than truncated -- a truncated command line is a
     * different command. */
    Buf<640> cmd;
    char pidText[16];
    const char *keys[4];
    const char *vals[4];
    DWORD pid = 0, err = 0;
    long waitMs;
    char *slash;

    /* A different word from REBOOT's `yes` on purpose: a REBOOT frame that
     * got its type edited must not become a valid UPDATE. */
    if (!confirm || strcmp(confirm, "replace") != 0)
    {
        r.err("unconfirmed");
        return;
    }
    if (!src || !*src)
    {
        r.err("no_source");
        return;
    }
    if (!GetModuleFileNameA(NULL, self.raw(), self.cap()) || self.empty())
    {
        r.err("no_path");
        return;
    }
    dir.set(self.str());
    slash = strrchr(dir.raw(), '\\');
    if (slash)
        *slash = '\0';
    if (!updater.join(dir.str(), UPDATER) ||
        GetFileAttributesA(updater.str()) == 0xFFFFFFFF)
    {
        rc_log("control: UPDATE refused, no %s beside %s", UPDATER,
               self.str());
        r.err("no_updater");
        return;
    }
    if (samePath(src, self.str()))
    {
        r.err("same_file");
        return;
    }
    if (!isWin32Image(src))
    {
        rc_log("control: UPDATE refused, %s is not a 32-bit image", src);
        r.err("not_an_image");
        return;
    }

    waitMs = waitText ? strtol(waitText, NULL, 10) : 0;
    if (waitMs < 1000 || waitMs > 600000)
        waitMs = 60000;

    if (!cmd.set("\"") || !cmd.cat(updater.str()) || !cmd.cat("\" -src \"") ||
        !cmd.cat(src) || !cmd.cat("\" -dst \"") || !cmd.cat(self.str()) ||
        !cmd.cat("\" -mutex ") || !cmd.cat(INSTANCE_MUTEX))
    {
        r.err("no_path");
        return;
    }
    {
        char waitArg[32];
        rc_strf(waitArg, sizeof waitArg, " -wait %ld", waitMs);
        if (!cmd.cat(waitArg))
        {
            r.err("no_path");
            return;
        }
    }

    /* The last thing that can fail while this server is still answerable
     * for the outcome. After it, something on this machine is committed to
     * putting a server back. */
    if (!Processes::launch(cmd.str(), dir.str(), SW_SHOWNORMAL, pid, err))
    {
        rc_log("control: UPDATE could not start %s: %lu", updater.str(),
               (unsigned long)err);
        r.err("launch_failed");
        return;
    }

    rc_strf(pidText, sizeof pidText, "%08lx", (unsigned long)pid);
    keys[0] = "pid";     vals[0] = pidText;
    keys[1] = "updater"; vals[1] = updater.str();
    keys[2] = "target";  vals[2] = self.str();
    keys[3] = "src";     vals[3] = src;
    rc_log("control: UPDATE -> %s will replace %s from %s", updater.str(),
           self.str(), src);
    r.ok(keys, vals, 4);
    /* The reply has to be on the wire before the process stops, for the
     * same reason REBOOT drains: an OK still in the out queue is an OK
     * nobody sees, and the caller would read a clean update as a crash. */
    m_conn->drain(UPDATE_FLUSH_MS);
    /* App's own shutdown, not ExitProcess: the lanes stop, a job's child
     * tree is ended, the socket is closed with a FIN. The updater is
     * already waiting for exactly that to finish. */
    m_app->quit(0);
}

bool ControlOps::handle(Responder &r, rc_frame *f)
{
    if (strcmp(f->type, "SYSINFO") == 0)
        sysinfo(r);
    else if (strcmp(f->type, "PS") == 0)
        ps(r);
    else if (strcmp(f->type, "KILL") == 0)
        kill(r, f);
    else if (strcmp(f->type, "REBOOT") == 0)
        reboot(r, f);
    else if (strcmp(f->type, "UPDATE") == 0)
        update(r, f);
    else
        return false;
    return true;
}
