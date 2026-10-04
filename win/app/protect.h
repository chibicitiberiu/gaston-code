/* What this machine declares about itself, and the PREFS body that carries it.
 *
 * THE CLIENT HOLDS THE OS FACTS (CLAUDE.md). Which files are this system's
 * own, which programs can wreck a disk, which ones are ordinary tools: those
 * are true of Windows 98, so they live here, compiled in, and reach the
 * bridge as opaque patterns. The bridge matches them and knows nothing of
 * what they mean.
 *
 * THREE LISTS, each built-in entries minus the person's removals plus their
 * additions (Options > Permissions > Lists...):
 *
 *   protect  path globs, `*` within a folder and `**` across folders; the
 *            Windows folder and the boot drive are expanded here, from the
 *            machine, never assumed to be C:\WINDOWS
 *   danger   program names, each with the reason shown to the person
 *   trust    program names that may run without a second look -- the
 *            detected tools' own (`trustN=` in tools_detect.ini) plus the
 *            accessories that ship with Windows
 */
#ifndef RC_PROTECT_H
#define RC_PROTECT_H

#ifndef __cplusplus
#error "app/protect.h is C++"
#endif

#include <string>
#include <vector>

#include "app/config.h"

#define RC_SRC_BUILTIN  0       /* compiled in (this file) */
#define RC_SRC_USER     1       /* gaston.ini's [protect]/[danger]/[trust] */
#define RC_SRC_TOOL     2       /* a detected tool's trustN= */
#define RC_SRC_REMOVED  3       /* built in, removed by the person */

/* Plain data, not std::strings: a list is shown and rebuilt rarely, and a
 * std::string member puts exception tables behind every function that
 * copies one (measured: config and this file cost 30KB that way). */
struct ListEntry
{
    char value[264];            /* a pattern, or a program name */
    char reason[64];            /* why it is listed; may be empty */
    int  source;                /* RC_SRC_* */
};

/* The three lists as they apply now -- removed built-ins included, marked
 * RC_SRC_REMOVED, so the inspector can show and restore them. */
void rc_list_protect(const ClientConfig &cfg, const char *exeDir,
                     std::vector<ListEntry> &out);
void rc_list_danger(const ClientConfig &cfg, std::vector<ListEntry> &out);
void rc_list_trust(const ClientConfig &cfg, const char *exeDir,
                   std::vector<ListEntry> &out);

/* The PREFS body for this configuration (bridge/prefs.py's keys). */
std::string rc_prefs_body(const ClientConfig &cfg, const char *exeDir);

#endif
