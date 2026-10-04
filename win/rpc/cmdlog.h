/* Every command the assistant runs, saved on this machine (Options > Misc).
 *
 * <log folder>\CMDLOG\YYYYMMDD.CMD -- 8.3 names, one file a day: when a
 * command ran, where, the command line, everything it printed, and how it
 * ended. Written by the RPC worker while the command runs, chunk by chunk,
 * straight to the file: nothing is held in memory (the guest has 28KB free
 * after a turn) and nothing is flushed per line (that is what makes
 * core/log.cpp expensive on a 1998 disk; this is a record, not a crash log).
 *
 * BOUNDED: 1MB a day -- past it a day's file says so once and takes no more
 * -- and thirty days kept, older days deleted once a day on the worker.
 *
 * Settings are set from the UI thread and read by the worker, under a lock.
 */
#ifndef RC_CMDLOG_H
#define RC_CMDLOG_H

#ifndef __cplusplus
#error "rpc/cmdlog.h is C++"
#endif

#include <windows.h>

#include "core/rc_cpp.h"

#define RC_CMDLOG_DAY_MAX  (1024ul * 1024ul)
#define RC_CMDLOG_DAYS     30

/* On or off, and where (the log folder; CMDLOG\ is made under it). Any
 * thread; takes effect from the next command. */
void rc_cmdlog_configure(const char *dir, bool on);

class CmdLog
{
public:
    CmdLog();
    /* Opens today's file and writes the header, if logging is on. */
    void begin(const char *id, const char *command, const char *cwd);
    void chunk(const char *data, unsigned len);
    void end(const char *status, unsigned long code, bool truncated);

private:
    CmdLog(const CmdLog &);
    CmdLog &operator=(const CmdLog &);
    void put(const char *data, unsigned len);

    Handle m_file;
    DWORD  m_size;
    bool   m_capped;
};

#endif
