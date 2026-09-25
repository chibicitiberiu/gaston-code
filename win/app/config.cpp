#include "app/config.h"

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
static const char *const UI_KEYS[] = { "autoscroll", "font_size" };
static const char *const SYNC_KEYS[] = { "root" };

/* Fills m_path for IniFile's constructor. A truncated path becomes "",
 * which IniFile refuses, rather than a valid-looking path to another file. */
static const char *iniPath(Path &out, const char *exeDir)
{
    out.join(exeDir ? exeDir : "", "retroclaude.ini");
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

    if (!m_ini.ok())
        return false;

    bool existed = GetFileAttributesA(m_ini.path()) != 0xFFFFFFFF;
    bool good =
        m_ini.checkKeys("bridge", BRIDGE_KEYS, 3) &&
        m_ini.checkKeys("ui", UI_KEYS, 2) &&
        m_ini.checkKeys("sync", SYNC_KEYS, 1) &&
        m_ini.getString("bridge", "host", DEFAULT_HOST, cfg->host,
                        sizeof cfg->host) &&
        m_ini.getInt("bridge", "port", DEFAULT_PORT, 1, 65535, &cfg->port) &&
        m_ini.getString("bridge", "secret", DEFAULT_SECRET, cfg->secret,
                        sizeof cfg->secret) &&
        m_ini.getBool("ui", "autoscroll", true, &cfg->autoscroll) &&
        m_ini.getString("sync", "root", DEFAULT_SYNC_ROOT, cfg->syncRoot,
                        sizeof cfg->syncRoot);
    if (!good)
    {
        rc_log("config: %s: %s", m_ini.path(), m_ini.error());
        return false;
    }

    rc_log("config: bridge=%s:%ld autoscroll=%d sync_root='%s'", cfg->host,
           cfg->port, cfg->autoscroll ? 1 : 0, cfg->syncRoot);

    if (!existed)
        save(*cfg);
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
    ok = m_ini.setString("sync", "root", cfg.syncRoot) && ok;
    m_ini.flush();
    if (!ok)
        rc_log("config: could not write %s (%s) -- settings will not "
               "survive a restart", m_ini.path(), m_ini.error());
    return ok;
}
