/* Diagnostic log. There is no console on this platform and no debugger worth
 * the name, so "no log file was created" is the primary signal that WinMain
 * never ran -- an NT-only static import kills the EXE at load, before main(),
 * with no other trace.
 *
 * Writes with raw WriteFile rather than CRT stdio: msvcrt stdio can itself
 * fault on 9x, and a logger that dies during a crash is worse than none.
 */
#ifndef RC_LOG_H
#define RC_LOG_H


#ifdef __cplusplus
extern "C" {
#endif
void rc_log_open(const char *exe_dir);
void rc_log(const char *fmt, ...);
void rc_log_close(void);
const char *rc_log_path(void);

#ifdef __cplusplus
}
#endif

#endif
