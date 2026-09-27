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
/* `program` names both the file (<exe_dir>\\<program>.log) and the banner.
 * It is a parameter because more than one of our programs can sit in one
 * directory -- gremote.exe beside gaston.exe on the test box -- and a
 * shared log file is two programs interleaving lines into each other's
 * diagnostics with no way to tell whose is whose. */
int  rc_log_open(const char *exe_dir, const char *program);
void rc_log(const char *fmt, ...);
void rc_log_close(void);
/* One line of GlobalMemoryStatus, tagged with `when`. */
void rc_log_mem(const char *when);

#ifdef __cplusplus
}
#endif

#endif
