/* Settings live in retroclaude.ini beside the exe, via
 * GetPrivateProfileString. Not the registry: an INI is inspectable with
 * EDIT.COM on the guest itself, and it removes any need to fork retro-agent's
 * registry.c (507 lines) for zero loss of function.
 */
#ifndef RC_INI_H
#define RC_INI_H

typedef struct {
    char host[64];
    int  port;
    char secret[128];
    int  font_size;
    int  autoscroll;
    char sync_root[128];      /* "" disables sync entirely */
} rc_config;

void rc_config_load(rc_config *cfg, const char *exe_dir);
void rc_config_save(const rc_config *cfg, const char *exe_dir);

#endif
