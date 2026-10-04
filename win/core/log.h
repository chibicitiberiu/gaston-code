/* Diagnostic log. There is no console on this platform and no debugger worth
 * the name, so "no log file was created" is the primary signal that WinMain
 * never ran -- an NT-only static import kills the EXE at load, before main(),
 * with no other trace.
 *
 * Writes with raw WriteFile rather than CRT stdio: msvcrt stdio can itself
 * fault on 9x, and a logger that dies during a crash is worse than none.
 *
 * TWO FILES PER PROGRAM, and the second one is the point. <program>.log is
 * this run; <program>.prv is the run before it, renamed there by rc_log_open.
 * Without that, recovering a hung machine -- a forced restart -- destroyed the
 * evidence of the hang, because the restarted program's first act was to
 * truncate the file holding it. Measured on the real Pentium: two reboots
 * after two shutdown hangs, nothing learned from either.
 */
#ifndef RC_LOG_H
#define RC_LOG_H


#ifdef __cplusplus
extern "C" {
#endif
/* `program` names both the files (<exe_dir>\\<program>.log and .prv) and the
 * banner. It is a parameter because more than one of our programs can sit in
 * one directory -- gremote.exe beside gaston.exe on the test box -- and a
 * shared log file is two programs interleaving lines into each other's
 * diagnostics with no way to tell whose is whose. The rotation is per
 * program for the same reason: GASTON.PRV is never GREMOTE's.
 *
 * Both names must have an 8.3 spelling, which `.prv` does and `.log.old`
 * would not: on FAT a name with no 8.3 form is a different file seen from
 * DOS or from the installer.
 *
 * Rotation can never stop logging. A failed rename is reported in the new log
 * and nothing else changes; the return value is still only about whether the
 * log could be opened at all (0, or -1). */
int  rc_log_open(const char *exe_dir, const char *program);

/* rc_log_open in two halves, for a program with a single-instance check
 * (BACKLOG.md 24). EARLY opens <dir>\<program>.log APPENDING -- nothing is
 * renamed, so a copy that bounces off the check adds its few lines to the
 * run that is still going instead of spending that run's rotation -- and
 * still writes from the first statement, which is the diagnostic for an
 * image that dies at load. BEGIN_RUN is called once this copy is the one
 * that will run: it rotates (.log -> .prv) and starts this run's file. */
int  rc_log_open_early(const char *dir, const char *program);
int  rc_log_begin_run(void);
void rc_log(const char *fmt, ...);
void rc_log_close(void);
/* One more line when the log may ALREADY BE CLOSED: writes to the open handle
 * if there is one, and otherwise reopens the file and appends.
 *
 * For App's WM_ENDSESSION handler, which must say that it returned to Windows
 * -- and the last thing it does before that is cleanup(), which is where
 * rc_log_close lives. Without this, "the teardown finished" and "the handler
 * returned" are the same final line, and a fault between them looks exactly
 * like a clean exit. */
void rc_log_tail(const char *fmt, ...);
/* One line of GlobalMemoryStatus, tagged with `when`. */
void rc_log_mem(const char *when);

#ifdef __cplusplus
}
#endif

#endif
