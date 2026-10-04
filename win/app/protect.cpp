/* protect.cpp -- see app/protect.h. */
#include <windows.h>
#include <string.h>

#include "app/protect.h"
#include "app/tooldet.h"
#include "core/log.h"
#include "core/str.h"

namespace {

/* {WINDIR} and {BOOT} are expanded from the machine; {GASTON} is the folder
 * this program runs from. Narrow on purpose (the plan's workflow review):
 * the TEMP folders are left out, so cleaning one is not a run of system
 * prompts, and the Windows folder is protected by its settings and system
 * files rather than wholesale. */
struct Builtin { const char *value; const char *reason; };

const Builtin PROTECT[] = {
    { "{WINDIR}\\*.INI",        "Windows settings" },
    { "{WINDIR}\\SYSTEM\\**",   "Windows system files" },
    { "{WINDIR}\\SYSTEM.DAT",   "the registry" },
    { "{WINDIR}\\USER.DAT",     "the registry" },
    { "{WINDIR}\\WIN.COM",      "starts Windows" },
    { "{WINDIR}\\COMMAND.COM",  "the command interpreter" },
    { "{WINDIR}\\COMMAND\\**",  "the MS-DOS commands" },
    { "{BOOT}\\AUTOEXEC.BAT",   "runs at start-up" },
    { "{BOOT}\\CONFIG.SYS",     "loads drivers at start-up" },
    { "{BOOT}\\MSDOS.SYS",      "boot settings" },
    { "{BOOT}\\IO.SYS",         "the boot loader" },
    { "{BOOT}\\COMMAND.COM",    "the boot command interpreter" },
    { "{BOOT}\\*.SYS",          "start-up system files" },
    /* This program and its settings: gaston.ini holds these very lists,
     * and a mode that let the agent edit it could edit its own leash. */
    { "{GASTON}\\**",           "Gaston itself and its settings" },
};

const Builtin DANGER[] = {
    { "format",   "erases a disk" },
    { "fdisk",    "rewrites the partition table" },
    { "sys",      "rewrites the boot files" },
    { "debug",    "can write raw disk sectors" },
    { "scanreg",  "replaces the registry from a backup" },
    { "regedit",  "changes the registry" },
    { "defrag",   "rewrites the whole disk's layout" },
    { "scandisk", "rewrites disk structures" },
    { "chkdsk",   "can rewrite disk structures" },
    { "recover",  "destroys a folder's structure" },
    { "smartdrv", "changes how the disk is cached" },
    { "emm386",   "changes how memory is mapped" },
};

/* Accessories that ship with Windows. Trusted to RUN in the work folder;
 * what they are given to open is still judged. */
const Builtin TRUST[] = {
    { "notepad", "Windows accessory" },
    { "write",   "Windows accessory" },
    { "wordpad", "Windows accessory" },
    { "mspaint", "Windows accessory" },
    { "calc",    "Windows accessory" },
};

#define COUNT(a) (sizeof(a) / sizeof((a)[0]))

/* `pattern` with its tokens replaced, into `out`; false when a token has no
 * value on this machine (and the entry is then left out, never guessed). */
bool expand(const char *pattern, const char *exeDir, char *out, unsigned cap)
{
    char windir[MAX_PATH], boot[4];
    UINT n = GetWindowsDirectoryA(windir, sizeof windir);
    if (n == 0 || n >= sizeof windir) windir[0] = 0;
    boot[0] = 0;
    if (windir[0] && windir[1] == ':') {
        boot[0] = windir[0]; boot[1] = ':'; boot[2] = 0;
    }
    const char *toks[3] = { "{WINDIR}", "{BOOT}", "{GASTON}" };
    const char *vals[3] = { windir, boot, exeDir ? exeDir : "" };
    unsigned o = 0;
    for (const char *p = pattern; *p; ) {
        int hit = -1;
        for (int i = 0; i < 3 && hit < 0; i++)
            if (strncmp(p, toks[i], strlen(toks[i])) == 0) hit = i;
        if (hit >= 0) {
            if (!vals[hit][0]) return false;
            unsigned l = (unsigned)strlen(vals[hit]);
            if (o + l >= cap) return false;
            memcpy(out + o, vals[hit], l);
            o += l;
            p += strlen(toks[hit]);
        } else {
            if (o + 1 >= cap) return false;
            out[o++] = *p++;
        }
    }
    out[o] = 0;
    return true;
}

/* The 8.3 spelling of the literal part of `pattern`, up to the first
 * component with a wildcard, into `out`; false when there is none or it is
 * the same. A long-named folder can be reached as PROGRA~1 too, and a
 * protected place reachable under another name is not protected. Fixed
 * drives only: asking a floppy spins its motor and can put up a dialog
 * (BACKLOG 20). */
bool shortAlias(const char *pattern, char *out, unsigned cap)
{
    char dir[MAX_PATH], shortp[MAX_PATH];
    const char *wild = strpbrk(pattern, "*?");
    unsigned litlen = wild ? (unsigned)(wild - pattern)
                           : (unsigned)strlen(pattern);
    if (litlen >= sizeof dir) return false;
    memcpy(dir, pattern, litlen);
    dir[litlen] = 0;
    char *cut = strrchr(dir, '\\');
    if (!cut || cut - dir < 2 || dir[1] != ':') return false;
    *cut = 0;
    char root[4] = { dir[0], ':', '\\', 0 };
    if (GetDriveTypeA(root) != DRIVE_FIXED) return false;
    DWORD n = GetShortPathNameA(dir, shortp, sizeof shortp);
    if (n == 0 || n >= sizeof shortp) return false;
    if (lstrcmpiA(shortp, dir) == 0) return false;
    return rc_strf(out, cap, "%s%s", shortp, pattern + (cut - dir)) != 0;
}

void push(std::vector<ListEntry> &out, const char *value, const char *reason,
          int source)
{
    ListEntry e;
    rc_strcpy(e.value, sizeof e.value, value);
    rc_strcpy(e.reason, sizeof e.reason, reason ? reason : "");
    e.source = source;
    out.push_back(e);
}

void merge(const Builtin *table, unsigned n, const TextList &rem,
           const TextList &add, bool programs, const char *exeDir,
           std::vector<ListEntry> &out)
{
    char v[264];
    for (unsigned i = 0; i < n; i++) {
        if (programs) rc_strcpy(v, sizeof v, table[i].value);
        else if (!expand(table[i].value, exeDir, v, sizeof v)) continue;
        bool gone = rem.has(table[i].value) || rem.has(v);
        push(out, v, table[i].reason, gone ? RC_SRC_REMOVED : RC_SRC_BUILTIN);
    }
    unsigned pos = 0;
    while (add.next(&pos, v, sizeof v)) {
        char *sp = programs ? strchr(v, ' ') : NULL;
        if (sp) *sp = 0;
        push(out, v, sp ? sp + 1 : "", RC_SRC_USER);
    }
}

struct TrustCtx { std::vector<ListEntry> *out; const TextList *rem; };

void trustSink(void *ctx, const char *section, const char *program)
{
    TrustCtx *t = (TrustCtx *)ctx;
    char why[64];
    rc_strf(why, sizeof why, "detected tool: %s", section);
    push(*t->out, program, why,
         t->rem->has(program) ? RC_SRC_REMOVED : RC_SRC_TOOL);
}

void line(std::string &body, const char *key, const char *value)
{
    body += key;
    body += '=';
    body += value;
    body += '\n';
}

void lineN(std::string &body, const char *key, long value)
{
    char b[24];
    rc_strf(b, sizeof b, "%ld", value);
    line(body, key, b);
}

}  // namespace

void rc_list_protect(const ClientConfig &cfg, const char *exeDir,
                     std::vector<ListEntry> &out)
{
    out.clear();
    merge(PROTECT, COUNT(PROTECT), cfg.protectRem, cfg.protectAdd, false,
          exeDir, out);
}

void rc_list_danger(const ClientConfig &cfg, std::vector<ListEntry> &out)
{
    out.clear();
    merge(DANGER, COUNT(DANGER), cfg.dangerRem, cfg.dangerAdd, true, NULL,
          out);
}

void rc_list_trust(const ClientConfig &cfg, const char *exeDir,
                   std::vector<ListEntry> &out)
{
    out.clear();
    merge(TRUST, COUNT(TRUST), cfg.trustRem, cfg.trustAdd, true, NULL, out);
    TrustCtx t;
    t.out = &out;
    t.rem = &cfg.trustRem;
    rc_tooldet_trust(exeDir, trustSink, &t);
}

std::string rc_prefs_body(const ClientConfig &cfg, const char *exeDir)
{
    static const char *const PAGES[] = { "small", "normal", "large" };
    std::string body;
    std::vector<ListEntry> list;
    char key[RC_MODE_ID + 32], v[264];
    unsigned i, pos;

    if (cfg.defaultModel[0]) line(body, "general.model", cfg.defaultModel);
    if (cfg.defaultEffort[0]) line(body, "general.effort", cfg.defaultEffort);
    if (cfg.defaultMode[0]) line(body, "general.mode", cfg.defaultMode);
    if (cfg.pageSize >= 0 && cfg.pageSize < 3 &&
        cfg.pageSize != RC_PAGE_NORMAL)
        line(body, "view.page", PAGES[cfg.pageSize]);
    if (cfg.sweepMin)
        lineN(body, "sync.sweep_s", cfg.sweepMin < 0 ? -1 : cfg.sweepMin * 60);
    if (cfg.maxFileKb) lineN(body, "sync.max_kb", cfg.maxFileKb);
    pos = 0;
    while (cfg.ignore.next(&pos, v, sizeof v))
        line(body, "sync.ignore", v);
    if (cfg.deadlineMin) lineN(body, "perm.deadline_s", cfg.deadlineMin * 60);

    rc_list_protect(cfg, exeDir, list);
    for (i = 0; i < list.size(); i++) {
        if (list[i].source == RC_SRC_REMOVED) continue;
        line(body, "protect", list[i].value);
        if (shortAlias(list[i].value, v, sizeof v)) line(body, "protect", v);
    }
    rc_list_danger(cfg, list);
    for (i = 0; i < list.size(); i++) {
        if (list[i].source == RC_SRC_REMOVED) continue;
        rc_strf(key, sizeof key, "danger.%s", list[i].value);
        line(body, key, list[i].reason[0] ? list[i].reason : list[i].value);
    }
    rc_list_trust(cfg, exeDir, list);
    TextList seen;
    for (i = 0; i < list.size(); i++) {
        if (list[i].source == RC_SRC_REMOVED) continue;
        if (!seen.add(list[i].value)) continue;     /* already sent */
        line(body, "trust", list[i].value);
    }

    for (int m = 0; m < cfg.modeCount && m < RC_MODES_MAX; m++) {
        const ModeDef &d = cfg.modes[m];
        rc_strf(key, sizeof key, "mode.%s.label", d.id);
        line(body, key, d.label);
        for (int c = 0; c < RC_CATS; c++) {
            const char *val = rc_mode_value_name(d.values[c]);
            rc_strf(key, sizeof key, "mode.%s.c.%s", d.id, RC_CAT_NAMES[c]);
            line(body, key, val ? val : "ask");
        }
    }
    return body;
}
