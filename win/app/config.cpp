#include "app/config.h"

#include <string.h>

#include "core/log.h"

static const char DEFAULT_HOST[] = "192.168.1.2";
static const long DEFAULT_PORT = 7890;
static const char DEFAULT_SECRET[] = "dev-secret";
/* Where a conversation's own folder goes -- C:\Gaston\LISTFILE and so on.
 * Empty still disables sync, and an INI that already names a root keeps it:
 * the default only applies to a machine that has never had one. */
static const char DEFAULT_SYNC_ROOT[] = "C:\\Gaston";

static const char *const BRIDGE_KEYS[] = { "host", "port", "secret" };
/* font_size is RETIRED, not unknown: older clients wrote it and INIs on real
 * machines still carry it. Accepted and ignored, so an old INI still loads. */
static const char *const UI_KEYS[] = { "autoscroll", "font_size",
                                      "automation" };
static const char *const SYNC_KEYS[] = { "root" };
static const char *const GENERAL_KEYS[] = { "model", "effort", "mode",
                                           "page" };
static const char *const FILES_KEYS[] = { "sweep", "maxkb", "existing" };
static const char *const PERM_KEYS[] = { "deadline" };
static const char *const LOG_KEYS[] = { "dir", "commands" };
static const char *const MODES_KEYS[] = { "order" };
static const char *const LIST_PREFIXES[] = { "add", "rem" };
static const char *const IGNORE_PREFIXES[] = { "p" };
static const char *const PAGE_NAMES[] = { "small", "normal", "large" };
/* RC_EXISTING_ASK is -1, so these are offset by one. */
static const char *const EXISTING_NAMES[] = { "ask", "keep", "replace",
                                             "merge" };

ClientConfig::ClientConfig()
    : port(0), autoscroll(true), automation(true), pageSize(RC_PAGE_NORMAL),
      sweepMin(0), maxFileKb(0), existing(RC_EXISTING_ASK), deadlineMin(0),
      modeCount(0), cmdLog(false)
{
    host[0] = secret[0] = syncRoot[0] = '\0';
    defaultModel[0] = defaultEffort[0] = defaultMode[0] = '\0';
    logDir[0] = '\0';
}

namespace {

int indexOf(const char *const *names, int n, const char *v)
{
    for (int i = 0; i < n; i++)
        if (lstrcmpiA(names[i], v) == 0) return i;
    return -1;
}

/* A numbered list, `<prefix>01`..: read in order into one '\n'-separated
 * string. A gap is allowed; an unknown key was already refused by
 * checkNumbered. */
bool readList(IniFile &ini, const char *section, const char *prefix,
              unsigned max, TextList &out)
{
    char key[16], val[264];
    out.clear();
    for (unsigned i = 1; i <= max; i++) {
        rc_strf(key, sizeof key, "%s%02u", prefix, i);
        if (!ini.getString(section, key, "", val, sizeof val))
            return false;
        if (val[0]) out.add(val);
    }
    return true;
}

bool writeList(IniFile &ini, const char *section, const char *prefix,
               const TextList &items)
{
    char key[16], val[264];
    unsigned pos = 0, n = 0;
    bool ok = true;
    while (items.next(&pos, val, sizeof val)) {
        rc_strf(key, sizeof key, "%s%02u", prefix, ++n);
        ok = ini.setString(section, key, val) && ok;
    }
    return ok;
}

}  // namespace

/* Fills m_path for IniFile's constructor. A truncated path becomes "",
 * which IniFile refuses, rather than a valid-looking path to another file. */
static const char *iniPath(Path &out, const char *exeDir)
{
    out.join(exeDir ? exeDir : "", "gaston.ini");
    return out.ok() ? out.str() : "";
}

/* m_path is declared before m_ini, so it is constructed first. */
ConfigStore::ConfigStore(const char *exeDir)
    : m_ini(iniPath(m_path, exeDir))
{
}

const char *ConfigStore::error() const
{
    return m_ini.error();
}

const char *ConfigStore::path() const
{
    return m_ini.path();
}

bool ConfigStore::load(ClientConfig *cfg)
{
    rc_strcpy(cfg->host, sizeof cfg->host, DEFAULT_HOST);
    cfg->port = DEFAULT_PORT;
    rc_strcpy(cfg->secret, sizeof cfg->secret, DEFAULT_SECRET);
    rc_strcpy(cfg->syncRoot, sizeof cfg->syncRoot, DEFAULT_SYNC_ROOT);
    cfg->autoscroll = true;
    /* ON, which is what this client has always done. See config.h. */
    cfg->automation = true;
    cfg->defaultModel[0] = cfg->defaultEffort[0] = cfg->defaultMode[0] = 0;
    cfg->pageSize = RC_PAGE_NORMAL;
    cfg->sweepMin = 0;
    cfg->maxFileKb = 0;
    cfg->existing = RC_EXISTING_ASK;
    cfg->ignore.clear();
    cfg->deadlineMin = 0;
    cfg->modeCount = 0;
    cfg->protectAdd.clear(); cfg->protectRem.clear();
    cfg->dangerAdd.clear(); cfg->dangerRem.clear();
    cfg->trustAdd.clear(); cfg->trustRem.clear();
    cfg->logDir[0] = 0;
    cfg->cmdLog = false;

    if (!m_ini.ok())
        return false;

    bool existed = GetFileAttributesA(m_ini.path()) != 0xFFFFFFFF;
    bool good =
        m_ini.checkKeys("bridge", BRIDGE_KEYS, 3) &&
        m_ini.checkKeys("ui", UI_KEYS, 3) &&
        m_ini.checkKeys("sync", SYNC_KEYS, 1) &&
        m_ini.getString("bridge", "host", DEFAULT_HOST, cfg->host,
                        sizeof cfg->host) &&
        m_ini.getInt("bridge", "port", DEFAULT_PORT, 1, 65535, &cfg->port) &&
        m_ini.getString("bridge", "secret", DEFAULT_SECRET, cfg->secret,
                        sizeof cfg->secret) &&
        m_ini.getBool("ui", "autoscroll", true, &cfg->autoscroll) &&
        m_ini.getBool("ui", "automation", true, &cfg->automation) &&
        m_ini.getString("sync", "root", DEFAULT_SYNC_ROOT, cfg->syncRoot,
                        sizeof cfg->syncRoot);
    if (good)
        good = loadOptions(cfg);
    if (!good)
    {
        rc_log("config: %s: %s", m_ini.path(), m_ini.error());
        return false;
    }

    rc_log("config: bridge=%s:%ld autoscroll=%d automation=%d "
           "sync_root='%s'", cfg->host, cfg->port, cfg->autoscroll ? 1 : 0,
           cfg->automation ? 1 : 0, cfg->syncRoot);

    if (!existed)
        save(*cfg);
    return true;
}

/* Everything the Options dialog added, read the house way: an absent key
 * takes its default, a present one that does not parse refuses startup
 * with its name. */
bool ConfigStore::loadOptions(ClientConfig *cfg)
{
    char text[64];
    long n;

    if (!m_ini.checkKeys("general", GENERAL_KEYS, 4) ||
        !m_ini.checkKeys("files", FILES_KEYS, 3) ||
        !m_ini.checkKeys("perm", PERM_KEYS, 1) ||
        !m_ini.checkKeys("log", LOG_KEYS, 2) ||
        !m_ini.checkKeys("modes", MODES_KEYS, 1) ||
        !m_ini.checkNumbered("ignore", IGNORE_PREFIXES, 1, RC_IGNORE_MAX) ||
        !m_ini.checkNumbered("protect", LIST_PREFIXES, 2, RC_LIST_MAX) ||
        !m_ini.checkNumbered("danger", LIST_PREFIXES, 2, RC_LIST_MAX) ||
        !m_ini.checkNumbered("trust", LIST_PREFIXES, 2, RC_LIST_MAX))
        return false;

    if (!m_ini.getString("general", "model", "", cfg->defaultModel,
                         sizeof cfg->defaultModel) ||
        !m_ini.getString("general", "effort", "", cfg->defaultEffort,
                         sizeof cfg->defaultEffort) ||
        !m_ini.getString("general", "mode", "", cfg->defaultMode,
                         sizeof cfg->defaultMode) ||
        !m_ini.getString("general", "page", "normal", text, sizeof text))
        return false;
    cfg->pageSize = indexOf(PAGE_NAMES, 3, text);
    if (cfg->pageSize < 0) {
        rc_log("config: [general] page: must be small, normal or large");
        cfg->pageSize = RC_PAGE_NORMAL;
        return false;
    }

    if (!m_ini.getInt("files", "sweep", 0, -1, 1440, &cfg->sweepMin) ||
        !m_ini.getInt("files", "maxkb", 0, 0, 1048576, &cfg->maxFileKb) ||
        !m_ini.getString("files", "existing", "ask", text, sizeof text))
        return false;
    n = indexOf(EXISTING_NAMES, 4, text);
    if (n < 0) {
        rc_log("config: [files] existing: must be ask, keep, replace or "
               "merge");
        return false;
    }
    cfg->existing = (int)n - 1;

    if (!readList(m_ini, "ignore", "p", RC_IGNORE_MAX, cfg->ignore) ||
        !m_ini.getInt("perm", "deadline", 0, 0, 1440, &cfg->deadlineMin) ||
        !readList(m_ini, "protect", "add", RC_LIST_MAX, cfg->protectAdd) ||
        !readList(m_ini, "protect", "rem", RC_LIST_MAX, cfg->protectRem) ||
        !readList(m_ini, "danger", "add", RC_LIST_MAX, cfg->dangerAdd) ||
        !readList(m_ini, "danger", "rem", RC_LIST_MAX, cfg->dangerRem) ||
        !readList(m_ini, "trust", "add", RC_LIST_MAX, cfg->trustAdd) ||
        !readList(m_ini, "trust", "rem", RC_LIST_MAX, cfg->trustRem) ||
        !m_ini.getString("log", "dir", "", cfg->logDir, sizeof cfg->logDir) ||
        !m_ini.getBool("log", "commands", false, &cfg->cmdLog))
        return false;

    return loadModes(cfg);
}

/* [modes] order= names the person's modes, in menu order; each is its own
 * [mode.<id>] section. A section NOT named there is an orphan -- left by a
 * crash between writes -- and is ignored, then dropped at the next save. */
bool ConfigStore::loadModes(ClientConfig *cfg)
{
    char order[RC_MODES_MAX * RC_MODE_ID + 16];
    char section[RC_MODE_ID + 8];
    char val[16];

    if (!m_ini.getString("modes", "order", "", order, sizeof order))
        return false;
    for (char *p = order; *p; ) {
        char *comma = strchr(p, ',');
        if (comma) *comma = 0;
        if (*p) {
            if (cfg->modeCount >= RC_MODES_MAX) {
                rc_log("config: [modes] order: more than %d modes",
                       RC_MODES_MAX);
                return false;
            }
            ModeDef &m = cfg->modes[cfg->modeCount];
            m = ModeDef();
            if (!rc_strcpy(m.id, sizeof m.id, p)) {
                rc_log("config: [modes] order: id %s is too long", p);
                return false;
            }
            rc_strf(section, sizeof section, "mode.%s", m.id);
            static const char *const fixed[] = { "label", "base" };
            const char *known[2 + RC_CATS];
            known[0] = fixed[0]; known[1] = fixed[1];
            for (int i = 0; i < RC_CATS; i++) known[2 + i] = RC_CAT_NAMES[i];
            if (!m_ini.checkKeys(section, known, 2 + RC_CATS) ||
                !m_ini.getString(section, "label", "", m.label,
                                 sizeof m.label) ||
                !m_ini.getString(section, "base", "manual", m.base,
                                 sizeof m.base))
                return false;
            if (!m.label[0]) {
                rc_log("config: [%s] label: missing", section);
                return false;
            }
            /* A category this mode predates takes its base's value -- a
             * snapshot, so a later version's new row is still decided. */
            const Preset *base = rc_preset(m.base);
            for (int i = 0; i < RC_CATS; i++) {
                if (!m_ini.getString(section, RC_CAT_NAMES[i], "", val,
                                     sizeof val))
                    return false;
                char v = val[0] ? rc_mode_value_of(val)
                                : (base ? base->values[i] : 'a');
                if (!v) {
                    rc_log("config: [%s] %s: must be off, ask, check or "
                           "allow", section, RC_CAT_NAMES[i]);
                    return false;
                }
                m.values[i] = v;
            }
            cfg->modeCount++;
        }
        if (!comma) break;
        p = comma + 1;
    }
    return true;
}

bool ConfigStore::save(const ClientConfig &cfg)
{
    /* Every field load() reads, save() must write. font_size was missed for
     * months, and the sync root went missing the other way round; inicheck
     * round-trips every field so a third one cannot. */
    bool ok = true;
    ok = m_ini.setString("bridge", "host", cfg.host) && ok;
    ok = m_ini.setInt("bridge", "port", cfg.port) && ok;
    ok = m_ini.setString("bridge", "secret", cfg.secret) && ok;
    ok = m_ini.setBool("ui", "autoscroll", cfg.autoscroll) && ok;
    ok = m_ini.setBool("ui", "automation", cfg.automation) && ok;
    ok = m_ini.setString("sync", "root", cfg.syncRoot) && ok;
    ok = saveOptions(cfg) && ok;
    m_ini.flush();
    if (!ok)
        rc_log("config: could not write %s (%s) -- settings will not "
               "survive a restart", m_ini.path(), m_ini.error());
    return ok;
}

bool ConfigStore::saveOptions(const ClientConfig &cfg)
{
    bool ok = true;
    int page = cfg.pageSize >= 0 && cfg.pageSize < 3 ? cfg.pageSize
                                                     : RC_PAGE_NORMAL;
    int existing = cfg.existing + 1;
    if (existing < 0 || existing > 3) existing = 0;

    ok = m_ini.setString("general", "model", cfg.defaultModel) && ok;
    ok = m_ini.setString("general", "effort", cfg.defaultEffort) && ok;
    ok = m_ini.setString("general", "mode", cfg.defaultMode) && ok;
    ok = m_ini.setString("general", "page", PAGE_NAMES[page]) && ok;
    ok = m_ini.setInt("files", "sweep", cfg.sweepMin) && ok;
    ok = m_ini.setInt("files", "maxkb", cfg.maxFileKb) && ok;
    ok = m_ini.setString("files", "existing", EXISTING_NAMES[existing])
         && ok;
    ok = m_ini.setInt("perm", "deadline", cfg.deadlineMin) && ok;
    ok = m_ini.setString("log", "dir", cfg.logDir) && ok;
    ok = m_ini.setBool("log", "commands", cfg.cmdLog) && ok;

    /* A list is written WHOLE: drop the section, then every entry, so a
     * shortened list leaves no stale tail of numbered keys behind. */
    ok = m_ini.dropSection("ignore") && writeList(m_ini, "ignore", "p",
                                                  cfg.ignore) && ok;
    ok = m_ini.dropSection("protect") &&
         writeList(m_ini, "protect", "add", cfg.protectAdd) &&
         writeList(m_ini, "protect", "rem", cfg.protectRem) && ok;
    ok = m_ini.dropSection("danger") &&
         writeList(m_ini, "danger", "add", cfg.dangerAdd) &&
         writeList(m_ini, "danger", "rem", cfg.dangerRem) && ok;
    ok = m_ini.dropSection("trust") &&
         writeList(m_ini, "trust", "add", cfg.trustAdd) &&
         writeList(m_ini, "trust", "rem", cfg.trustRem) && ok;
    return saveModes(cfg) && ok;
}

bool ConfigStore::saveModes(const ClientConfig &cfg)
{
    char old[RC_MODES_MAX * RC_MODE_ID + 16];
    char section[RC_MODE_ID + 8];
    char order[RC_MODES_MAX * RC_MODE_ID + 16];
    unsigned olen = 0;
    bool ok = true;
    order[0] = 0;

    /* Drop every section the PREVIOUS order named, then write the new ones:
     * a deleted mode's section must not survive to be read as an orphan. */
    if (m_ini.getString("modes", "order", "", old, sizeof old)) {
        for (char *p = old; *p; ) {
            char *comma = strchr(p, ',');
            if (comma) *comma = 0;
            if (*p) {
                rc_strf(section, sizeof section, "mode.%s", p);
                m_ini.dropSection(section);
            }
            if (!comma) break;
            p = comma + 1;
        }
    }
    for (int i = 0; i < cfg.modeCount && i < RC_MODES_MAX; i++) {
        const ModeDef &m = cfg.modes[i];
        rc_strf(section, sizeof section, "mode.%s", m.id);
        ok = m_ini.setString(section, "label", m.label) && ok;
        ok = m_ini.setString(section, "base", m.base) && ok;
        for (int c = 0; c < RC_CATS; c++) {
            const char *name = rc_mode_value_name(m.values[c]);
            ok = m_ini.setString(section, RC_CAT_NAMES[c],
                                 name ? name : "ask") && ok;
        }
        olen += rc_strf(order + olen, sizeof order - olen, "%s%s",
                        olen ? "," : "", m.id) ? (unsigned)strlen(order + olen)
                                               : 0;
    }
    return m_ini.setString("modes", "order", order) && ok;
}

