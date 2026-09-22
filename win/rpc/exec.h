/* Run a command on this machine, streaming output to a sink as it arrives.
 *
 * Streaming rather than buffering is the whole design: the caller never holds
 * the output, so a 32KB result costs one 8KB static buffer rather than 32KB of
 * heap on a machine with 28KB free.
 */
#ifndef RC_EXEC_H
#define RC_EXEC_H


#ifdef __cplusplus
extern "C" {
#endif
#include <windows.h>

typedef enum {
    RC_EXEC_OK = 0,
    RC_EXEC_TIMEOUT,
    RC_EXEC_SPAWN_FAILED,
    RC_EXEC_CANCELLED,
    RC_EXEC_TOO_LONG
} rc_exec_status;

typedef struct {
    rc_exec_status status;
    DWORD          code;        /* child exit code */
    DWORD          bytes;       /* total produced, including any discarded */
    DWORD          ms;
    DWORD          error;       /* GetLastError when spawning failed */
    int            truncated;   /* output exceeded the cap */
} rc_exec_result;

/* Called on the RPC worker thread, zero or more times, as output arrives. */
typedef void (*rc_exec_sink)(void *ctx, const char *data, unsigned len);

void rc_exec_init(void);
/* Clear a stale cancel before a new command. See the note in exec.c. */
void rc_exec_arm(void);
/* Kill any running child. Safe from another thread; that is the point. */
void rc_exec_cancel(void);

/* `shell` is the invocation prefix to splice in front of `command` -- the
 * bridge sends it on the EXEC frame, e.g. "C:\\WINDOWS\\COMMAND.COM /c".
 * NULL or empty spawns `command` directly.
 *
 * THE GUEST NO LONGER CHOOSES. This used to be `int use_shell`, and the
 * shell itself came from a `GetVersion()` test right here -- so a machine
 * with a second shell installed (4DOS beside COMMAND.COM) could not be asked
 * to use it, and the bridge's command guard was judging a command against
 * whichever grammar it had guessed rather than the one that would run it.
 * The machine reports which shells it HAS; the model picks one per call; this
 * splices what it is told and knows nothing else about shells. */
int  rc_exec_run(const char *command, const char *cwd, const char *shell,
                 DWORD timeout_ms, rc_exec_sink sink, void *ctx,
                 rc_exec_result *out);

#ifdef __cplusplus
}
#endif

#endif
