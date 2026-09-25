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

typedef struct {
    long  calls;
    long  attaches;
    long  pids_n;
    DWORD pids[RC_MAXPIDS];
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

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)inst;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        shm_attach();
        /* Counted even before any hook fires: a process that maps this and
         * never calls the proc still proves the mapping happened. */
        if (g_shm) g_shm->attaches++;
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
