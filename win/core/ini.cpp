#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "core/ini.h"
#include "core/log.h"
#include "core/str.h"

static void ini_path(char *out, unsigned n, const char *exe_dir)
{
    rc_strf(out, n, "%s\\retroclaude.ini", exe_dir);
    out[n - 1] = '\0';
}

void rc_config_load(rc_config *cfg, const char *exe_dir)
{
    char path[MAX_PATH];
    ini_path(path, sizeof(path), exe_dir);

    GetPrivateProfileStringA("bridge", "host", "192.168.1.2",
                             cfg->host, sizeof(cfg->host), path);
    cfg->port = (int)GetPrivateProfileIntA("bridge", "port", 7890, path);
    GetPrivateProfileStringA("bridge", "secret", "dev-secret",
                             cfg->secret, sizeof(cfg->secret), path);
    cfg->autoscroll = (int)GetPrivateProfileIntA("ui", "autoscroll", 1, path);
    /* "" disables sync. This line was missing for one real session on the
     * guest: save() wrote the key, load() never read it back, so the sync root
     * was empty on every launch and the whole layer was silently off. Exactly
     * the font_size shape the comment in save() warns about, wearing the other
     * face -- which is why inicheck.exe now asserts the round trip for every
     * field rather than for the ones someone remembered. */
    /* C:\Gaston by default, which is where a conversation's own folder goes
     * -- C:\Gaston\LISTFILE and so on. Empty still disables sync, and an
     * INI that already names a root keeps it: the default only applies to a
     * machine that has never had one. */
    GetPrivateProfileStringA("sync", "root", "C:\\Gaston",
                             cfg->sync_root, sizeof(cfg->sync_root), path);

    rc_log("config: bridge=%s:%d autoscroll=%d sync_root='%s'", cfg->host,
           cfg->port, cfg->autoscroll, cfg->sync_root);

    /* Write the file back on first run so there is something to edit. */
    if (GetFileAttributesA(path) == 0xFFFFFFFF)
        rc_config_save(cfg, exe_dir);
}

/* Returns non-zero if every value reached the file.
 *
 * The returns used to be discarded, all five. On read-only or full media the
 * user changed host or sync root, the dialog closed as if it had been
 * accepted, and the next launch read the old values back -- no log, no
 * message, nothing to connect the two. app/cache.cpp has a careful comment
 * about exactly this shape ("a cache that quietly does nothing is exactly the
 * kind of thing that gets reported ... a month later"); this file did not
 * follow it. (audit 01.37)
 *
 * Every write is attempted even after one fails, so a partially-writable file
 * gets as much as it can hold and the caller still learns it was partial. */
int rc_config_save(const rc_config *cfg, const char *exe_dir)
{
    char path[MAX_PATH], num[32];
    int ok = 1;
    ini_path(path, sizeof(path), exe_dir);

    if (!WritePrivateProfileStringA("bridge", "host", cfg->host, path)) ok = 0;
    rc_strf(num, sizeof(num), "%d", cfg->port);
    if (!WritePrivateProfileStringA("bridge", "port", num, path)) ok = 0;
    if (!WritePrivateProfileStringA("bridge", "secret", cfg->secret, path)) ok = 0;
    rc_strf(num, sizeof(num), "%d", cfg->autoscroll);
    if (!WritePrivateProfileStringA("ui", "autoscroll", num, path)) ok = 0;
    /* Every field that load() reads, save() must write. font_size was missed
     * for months and the same omission would make a sync root vanish. */
    if (!WritePrivateProfileStringA("sync", "root", cfg->sync_root, path)) ok = 0;

    if (!ok)
        rc_log("ini: could not write %s (%lu) -- settings will not survive a "
               "restart", path, (unsigned long)GetLastError());
    return ok;
}
