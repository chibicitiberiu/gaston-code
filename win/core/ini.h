/* Settings live in retroclaude.ini beside the exe, via
 * GetPrivateProfileString. Not the registry: an INI is inspectable with
 * EDIT.COM on the guest itself, and it removes any need to fork retro-agent's
 * registry.c (507 lines) for zero loss of function.
 */
#ifndef RC_INI_H
#define RC_INI_H


#ifdef __cplusplus
extern "C" {
#endif
typedef struct {
    char host[64];
    int  port;
    char secret[128];
    int  autoscroll;
    char sync_root[128];      /* "" disables sync entirely */
} rc_config;

void rc_config_load(rc_config *cfg, const char *exe_dir);
/* Non-zero if every value reached the file. A caller that ignores this tells
 * the user their setting was accepted when it was not -- see the note in
 * ini.cpp. */
int rc_config_save(const rc_config *cfg, const char *exe_dir);

#ifdef __cplusplus
}
#endif

#endif
