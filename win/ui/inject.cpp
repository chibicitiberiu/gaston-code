#include "ui/inject.h"
#include "core/log.h"
#include "core/str.h"

#include <string.h>

/* Never these, and there is NO force escape.
 *
 * `REMOTE.md:344` lets KILL take Explorer with `force=1`; injection has no
 * equivalent, because a bad kill is immediate and visible while a bad
 * injection shows up later and belongs to the host. Killing the first four
 * hangs 9x. Explorer is here because we have actually crashed it. */
static const char *const RC_NO_INJECT[] =
{
    "EXPLORER.EXE", "MSGSRV32.EXE", "KERNEL32.DLL", "MPREXE.EXE",
    "MMTASK.TSK", "SYSTRAY.EXE", "RUNDLL32.EXE", "KERNEL32.EXE",
    "SPOOL32.EXE", "WINOLDAP.MOD"
};

/* PROCESSENTRY32, spelled out.
 *
 * Every field is here because the LAYOUT is the contract -- TOOLHELP writes
 * into this by offset and `dwSize` tells it which version it is looking at.
 * Most of them are never read here and all of them must exist; dropping the
 * ones we do not use would move `szExeFile` and hand us somebody else's
 * bytes as a program name, which is the string the deny list is checked
 * against. tlhelp32.h is not included because this must not become a static
 * import on a machine whose KERNEL32 might not have it. */
struct RcProcessEntry32
{
    DWORD dwSize;
    DWORD cntUsage;
    DWORD th32ProcessID;
    DWORD th32DefaultHeapID;
    DWORD th32ModuleID;
    DWORD cntThreads;
    DWORD th32ParentProcessID;
    LONG  pcPriClassBase;
    DWORD dwFlags;
    char  szExeFile[MAX_PATH];
};

static void upperBase(const char *path, char *out, unsigned cap)
{
    const char *base = path ? path : "";
    unsigned i;
    for (i = 0; base[i]; i++)
        if (base[i] == '\\' || base[i] == '/' || base[i] == ':')
        {
            base = &base[i + 1];
            i = (unsigned)-1;   /* restart the scan after the separator */
        }
    for (i = 0; i + 1 < cap && base[i]; i++)
    {
        char c = base[i];
        out[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    }
    out[i] = '\0';
}

bool rc_inject_allowed(const char *exeOrPath)
{
    char up[64];
    unsigned i;
    if (!exeOrPath || !exeOrPath[0])
        return false;
    upperBase(exeOrPath, up, sizeof(up));
    if (!up[0])
        return false;
    for (i = 0; i < sizeof(RC_NO_INJECT) / sizeof(RC_NO_INJECT[0]); i++)
        if (!strcmp(up, RC_NO_INJECT[i]))
            return false;
    return true;
}

Injector::Injector()
{
    m_map = NULL;
    m_shm = NULL;
    m_req = NULL;
    m_done = NULL;
    m_seq = 0;
    m_err[0] = '\0';
}

Injector::~Injector()
{
    close();
}

bool Injector::open()
{
    if (m_shm)
        return true;

    m_map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                               0, sizeof(RcInShm), RC_SHM_NAME);
    if (!m_map)
    {
        rc_strf(m_err, sizeof(m_err), "no section (%lu)",
                (unsigned long)GetLastError());
        return false;
    }
    m_shm = (RcInShm *)MapViewOfFile(m_map, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!m_shm)
    {
        CloseHandle(m_map);
        m_map = NULL;
        rc_strf(m_err, sizeof(m_err), "no view (%lu)",
                (unsigned long)GetLastError());
        return false;
    }

    /* Stamp and ZERO. An old injected copy can outlive the controller that
     * made it, and nothing here unmaps on their side, so counters that are
     * never reset are cumulative across every run since boot -- which is
     * how gate 0.15d came to report a result it had not measured. */
    memset(m_shm, 0, sizeof(RcInShm));
    m_shm->magic = RC_SHM_MAGIC;
    m_shm->version = RC_SHM_VER;
    m_shm->cb = sizeof(RcInShm);
    m_shm->generation = GetTickCount() | 1;

    m_req = CreateEventA(NULL, TRUE, FALSE, RC_SHM_REQ_EVENT);
    m_done = CreateEventA(NULL, TRUE, FALSE, RC_SHM_DONE_EVENT);
    return true;
}

void Injector::close()
{
    /* Disarm before letting go. A section left naming a target is a
     * standing invitation to any copy of the DLL still resident. */
    if (m_shm)
    {
        m_shm->patch_pid = 0;
        m_shm->patch_exe[0] = '\0';
        m_shm->op = RC_OP_NONE;
        UnmapViewOfFile(m_shm);
        m_shm = NULL;
    }
    if (m_map)   { CloseHandle(m_map);  m_map = NULL; }
    if (m_req)   { CloseHandle(m_req);  m_req = NULL; }
    if (m_done)  { CloseHandle(m_done); m_done = NULL; }
}

bool Injector::stamp(DWORD pid, const char *exeBase)
{
    unsigned i;
    if (!m_shm)
        return false;
    m_shm->patch_pid = pid;
    for (i = 0; i + 1 < sizeof(m_shm->patch_exe) && exeBase[i]; i++)
        m_shm->patch_exe[i] = exeBase[i];
    m_shm->patch_exe[i] = '\0';
    return true;
}

InjectResult Injector::collect(HWND anyWindowOfTarget, HWND root,
                               bool wantItems, DWORD deadlineMs)
{
    DWORD pid = 0, tid = 0;
    char path[MAX_PATH];
    char base[64];
    HHOOK hook;
    HMODULE dll;
    HOOKPROC proc;
    InjectResult out = INJECT_FAILED;

    m_err[0] = '\0';
    if (!m_shm)
    {
        rc_strf(m_err, sizeof(m_err), "the section is not open");
        return INJECT_FAILED;
    }
    if (!anyWindowOfTarget || !IsWindow(anyWindowOfTarget))
        return INJECT_NO_WINDOW;

    tid = GetWindowThreadProcessId(anyWindowOfTarget, &pid);
    if (!tid || !pid)
        return INJECT_NO_WINDOW;
    if (pid == GetCurrentProcessId())
    {
        rc_strf(m_err, sizeof(m_err), "that window is ours");
        return INJECT_DENIED;
    }

    /* The name, from the process itself. Not from anything the caller
     * passed: the deny list is only worth having if it is checked against
     * what is actually running. */
    path[0] = '\0';
    {
        /* GetModuleFileNameEx is NT-only. On 9x the way to a process's
         * image name is TOOLHELP32, resolved rather than imported. */
        typedef HANDLE (WINAPI *PFN_SNAP)(DWORD, DWORD);
        typedef BOOL (WINAPI *PFN_P32)(HANDLE, void *);
        RcProcessEntry32 pe;
        HMODULE k32 = GetModuleHandleA("kernel32.dll");
        PFN_SNAP snap = k32
            ? (PFN_SNAP)GetProcAddress(k32, "CreateToolhelp32Snapshot") : 0;
        PFN_P32 first = k32
            ? (PFN_P32)GetProcAddress(k32, "Process32First") : 0;
        PFN_P32 next = k32
            ? (PFN_P32)GetProcAddress(k32, "Process32Next") : 0;
        if (snap && first && next)
        {
            HANDLE snapshot = snap(0x00000002 /* TH32CS_SNAPPROCESS */, 0);
            if (snapshot != INVALID_HANDLE_VALUE)
            {
                BOOL ok;
                pe.dwSize = sizeof(pe);
                for (ok = first(snapshot, &pe); ok; ok = next(snapshot, &pe))
                    if (pe.th32ProcessID == pid)
                    {
                        rc_strncpy(path, sizeof(path), pe.szExeFile,
                                   (unsigned)strlen(pe.szExeFile));
                        break;
                    }
                CloseHandle(snapshot);
            }
        }
    }
    if (!path[0])
    {
        /* Unnamed is a REFUSAL, not a licence. If we cannot say what this
         * process is, we cannot say it is not Explorer. */
        rc_strf(m_err, sizeof(m_err), "cannot name pid %lu",
                (unsigned long)pid);
        return INJECT_DENIED;
    }
    if (!rc_inject_allowed(path))
    {
        rc_strf(m_err, sizeof(m_err), "%s is on the deny list", path);
        return INJECT_DENIED;
    }
    upperBase(path, base, sizeof(base));

    dll = LoadLibraryA("HOOKDLL.DLL");
    if (!dll)
        dll = LoadLibraryA(".\\HOOKDLL.DLL");
    if (!dll)
    {
        rc_strf(m_err, sizeof(m_err), "HOOKDLL.DLL not found (%lu)",
                (unsigned long)GetLastError());
        return INJECT_NO_DLL;
    }
    proc = (HOOKPROC)GetProcAddress(dll, "rc_hook_proc");
    if (!proc)
        proc = (HOOKPROC)GetProcAddress(dll, "_rc_hook_proc@12");
    if (!proc)
    {
        FreeLibrary(dll);
        rc_strf(m_err, sizeof(m_err), "rc_hook_proc is not exported");
        return INJECT_NO_DLL;
    }

    /* Everything below must reach the teardown at the bottom. */
    stamp(pid, base);
    m_shm->root = (DWORD)(DWORD_PTR)root;
    m_shm->maxNodes = RC_IN_NODES;
    m_shm->wantItems = wantItems ? 1 : 0;
    m_shm->deadlineMs = deadlineMs;
    m_shm->result = RC_RES_IDLE;
    m_shm->nodeN = 0;
    m_shm->itemN = 0;
    m_shm->seq++;
    m_seq = m_shm->seq;
    m_shm->op = RC_OP_COLLECT;
    ResetEvent(m_done);

    /* WH_GETMESSAGE, and that is a measured choice rather than the obvious
     * one.
     *
     * The plan specified WH_CALLWNDPROC. Gate 0.15f, run on a real Win98,
     * says it INSTALLS AND NEVER FIRES -- zero calls, zero processes --
     * and so do WH_CBT and WH_CALLWNDPROCRET when aimed at a thread.
     * WH_GETMESSAGE delivered 23 calls into exactly one process, the
     * target, and no other. Had this been built on the documented choice
     * the whole in-process tier would have been silently dead.
     *
     * One THREAD either way. Not the system-wide form 0.15d used: that
     * reaches every process that sees the event, which is how Explorer
     * came to be in scope and how Explorer came to die. */
    hook = SetWindowsHookExA(WH_GETMESSAGE, proc, dll, tid);
    if (!hook)
    {
        rc_strf(m_err, sizeof(m_err), "the system refused the hook (%lu)",
                (unsigned long)GetLastError());
        out = INJECT_REFUSED;
    }
    else
    {
        /* Give the thread something to RETRIEVE, not merely something to
         * dispatch: WH_GETMESSAGE fires inside GetMessage, so a posted
         * message is what wakes it and a sent one is not enough. Both, so
         * a target that is idle in GetMessage and one that is busy in a
         * SendMessage both get poked. SendMessageTimeout and never
         * SendMessage: a wedged target must not take us with it. */
        DWORD until = GetTickCount() + (deadlineMs ? deadlineMs : 2000) + 1000;
        DWORD_PTR res = 0;
        out = INJECT_NO_REPLY;
        while ((long)(GetTickCount() - until) < 0)
        {
            PostMessageA(anyWindowOfTarget, WM_NULL, 0, 0);
            SendMessageTimeoutA(anyWindowOfTarget, WM_NULL, 0, 0,
                                SMTO_ABORTIFHUNG, 100, &res);
            if (WaitForSingleObject(m_done, 50) == WAIT_OBJECT_0)
                break;
            if (m_shm->ackSeq == m_seq)
                break;
        }
        if (m_shm->ackSeq == m_seq)
        {
            if (m_shm->result == RC_RES_OK)
                out = INJECT_OK;
            else if (m_shm->result == RC_RES_TRUNCATED)
                out = INJECT_TRUNCATED;
            else if (m_shm->result == RC_RES_REFUSED)
                out = INJECT_DENIED;
            else
                out = INJECT_FAILED;
        }
        UnhookWindowsHookEx(hook);
    }

    /* Disarm FIRST, then let the DLL go. Between the unhook and the
     * FreeLibrary the target may still be running our code; a section that
     * no longer names it means the worst it can do is nothing. */
    m_shm->op = RC_OP_NONE;
    m_shm->patch_pid = 0;
    m_shm->patch_exe[0] = '\0';
    FreeLibrary(dll);

    if (out != INJECT_OK && out != INJECT_TRUNCATED && !m_err[0])
        rc_strf(m_err, sizeof(m_err), "no answer from pid %lu",
                (unsigned long)pid);
    return out;
}
