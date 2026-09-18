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

int  rc_exec_run(const char *command, const char *cwd, int use_shell,
                 DWORD timeout_ms, rc_exec_sink sink, void *ctx,
                 rc_exec_result *out);

#ifdef __cplusplus
}
#endif

#endif
