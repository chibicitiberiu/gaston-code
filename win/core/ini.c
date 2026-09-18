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
    cfg->font_size  = (int)GetPrivateProfileIntA("ui", "font_size", 0, path);
    cfg->autoscroll = (int)GetPrivateProfileIntA("ui", "autoscroll", 1, path);
    /* "" disables sync. This line was missing for one real session on the
     * guest: save() wrote the key, load() never read it back, so the sync root
     * was empty on every launch and the whole layer was silently off. Exactly
     * the font_size shape the comment in save() warns about, wearing the other
     * face -- which is why inicheck.exe now asserts the round trip for every
     * field rather than for the ones someone remembered. */
    GetPrivateProfileStringA("sync", "root", "",
                             cfg->sync_root, sizeof(cfg->sync_root), path);

    rc_log("config: bridge=%s:%d autoscroll=%d sync_root='%s'", cfg->host,
           cfg->port, cfg->autoscroll, cfg->sync_root);

    /* Write the file back on first run so there is something to edit. */
    if (GetFileAttributesA(path) == 0xFFFFFFFF)
        rc_config_save(cfg, exe_dir);
}

void rc_config_save(const rc_config *cfg, const char *exe_dir)
{
    char path[MAX_PATH], num[32];
    ini_path(path, sizeof(path), exe_dir);

    WritePrivateProfileStringA("bridge", "host", cfg->host, path);
    rc_strf(num, sizeof(num), "%d", cfg->port);
    WritePrivateProfileStringA("bridge", "port", num, path);
    WritePrivateProfileStringA("bridge", "secret", cfg->secret, path);
    rc_strf(num, sizeof(num), "%d", cfg->autoscroll);
    WritePrivateProfileStringA("ui", "autoscroll", num, path);
    /* Every field that load() reads, save() must write. font_size was missed
     * for months and the same omission would make a sync root vanish. */
    rc_strf(num, sizeof(num), "%d", cfg->font_size);
    WritePrivateProfileStringA("ui", "font_size", num, path);
    WritePrivateProfileStringA("sync", "root", cfg->sync_root, path);
}
