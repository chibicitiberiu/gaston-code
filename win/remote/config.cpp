#include "remote/config.h"

#include <windows.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"

/* Every interface. The box this runs on is a test machine on an isolated
 * LAN and the point is to reach it from another machine; `allow` is what
 * narrows that, not the bind address. */
static const char DEFAULT_BIND[] = "0.0.0.0";
/* 7890 and 7897 are the client's, 7900+ the test harness's (REMOTE.md). */
static const long DEFAULT_PORT = 7401;
/* Refused by load(). A default that WORKED would mean a machine nobody
 * configured comes up listening with a secret published in a document. */
static const char DEFAULT_SECRET[] = "change-me";
static const char DEFAULT_ALLOW[] = "";
static const long DEFAULT_HEARTBEAT_S = 30;

static const long DEFAULT_MAX_TIMEOUT_MS = 120000;
static const long DEFAULT_MAX_OUTPUT_KB = 1024;

/* OFF, and the only defensible default. Turning it on changes how the hardware
 * behaves while gremote is up, so an install that predates the option has to
 * come up behaving exactly as it did before. */
static const bool DEFAULT_PREVENT_SLEEP = false;

static const char *const SERVER_KEYS[] = {
    "bind", "port", "secret", "allow", "heartbeat_timeout_s"
};
static const char *const EXEC_KEYS[] = { "max_timeout_ms", "max_output_kb" };
static const char *const MACHINE_KEYS[] = { "prevent_sleep" };

/* Fills m_path for IniFile's constructor. A truncated path becomes "",
 * which IniFile refuses, rather than a valid-looking path to another file. */
static const char *iniPath(Path &out, const char *exeDir)
{
    out.join(exeDir ? exeDir : "", "gremote.ini");
    return out.ok() ? out.str() : "";
}

ServerConfigStore::ServerConfigStore(const char *exeDir)
    : m_ini(iniPath(m_path, exeDir))
{
    m_error[0] = '\0';
}

const char *ServerConfigStore::path() const
{
    return m_ini.path();
}

const char *ServerConfigStore::error() const
{
    /* Ours first: IniFile's is empty when the file parsed but the values do
     * not make a server. */
    return m_error[0] ? m_error : m_ini.error();
}

bool ServerConfigStore::fail(const char *why)
{
    rc_strcpy(m_error, sizeof m_error, why);
    return false;
}

bool ServerConfigStore::load(ServerConfig *cfg, ExecConfig *exec,
                             MachineConfig *machine)
{
    rc_strcpy(cfg->bind, sizeof cfg->bind, DEFAULT_BIND);
    cfg->port = DEFAULT_PORT;
    rc_strcpy(cfg->secret, sizeof cfg->secret, DEFAULT_SECRET);
    rc_strcpy(cfg->allowText, sizeof cfg->allowText, DEFAULT_ALLOW);
    cfg->heartbeatS = DEFAULT_HEARTBEAT_S;
    exec->maxTimeoutMs = DEFAULT_MAX_TIMEOUT_MS;
    exec->maxOutputKb = DEFAULT_MAX_OUTPUT_KB;
    machine->preventSleep = DEFAULT_PREVENT_SLEEP;
    m_error[0] = '\0';

    if (!m_ini.ok())
        return fail("[server] the ini path is unusable; it must be an "
                    "absolute path, not a bare filename");

    bool existed = GetFileAttributesA(m_ini.path()) != 0xFFFFFFFF;
    bool good =
        m_ini.checkKeys("server", SERVER_KEYS, 5) &&
        m_ini.getString("server", "bind", DEFAULT_BIND, cfg->bind,
                        sizeof cfg->bind) &&
        m_ini.getInt("server", "port", DEFAULT_PORT, 1, 65535, &cfg->port) &&
        m_ini.getString("server", "secret", DEFAULT_SECRET, cfg->secret,
                        sizeof cfg->secret) &&
        m_ini.getString("server", "allow", DEFAULT_ALLOW, cfg->allowText,
                        sizeof cfg->allowText) &&
        /* 0 disables the drop, which is for a debugger session where the
         * far side is stopped at a breakpoint. 5 seconds is the floor: less
         * than that and a slow LAN drops a live client. */
        m_ini.getInt("server", "heartbeat_timeout_s", DEFAULT_HEARTBEAT_S,
                     0, 3600, &cfg->heartbeatS) &&
        m_ini.checkKeys("exec", EXEC_KEYS, 2) &&
        m_ini.getInt("exec", "max_timeout_ms", DEFAULT_MAX_TIMEOUT_MS,
                     1000, 3600000, &exec->maxTimeoutMs) &&
        /* Capped at 16MB: a frame is 64KB and the output streams, so this
         * bounds the total a single command may hand back, not any one
         * allocation. */
        m_ini.getInt("exec", "max_output_kb", DEFAULT_MAX_OUTPUT_KB,
                     1, 16384, &exec->maxOutputKb) &&
        m_ini.checkKeys("machine", MACHINE_KEYS, 1) &&
        m_ini.getBool("machine", "prevent_sleep", DEFAULT_PREVENT_SLEEP,
                      &machine->preventSleep);

    if (!existed)
    {
        /* Something to edit. It refuses to start until the secret changes,
         * which is the message the caller prints. */
        m_ini.setString("server", "bind", cfg->bind);
        m_ini.setInt("server", "port", cfg->port);
        m_ini.setString("server", "secret", cfg->secret);
        m_ini.setString("server", "allow", cfg->allowText);
        m_ini.setInt("server", "heartbeat_timeout_s", cfg->heartbeatS);
        m_ini.setInt("exec", "max_timeout_ms", exec->maxTimeoutMs);
        m_ini.setInt("exec", "max_output_kb", exec->maxOutputKb);
        m_ini.setBool("machine", "prevent_sleep", machine->preventSleep);
        m_ini.flush();
        rc_log("config: wrote a starting %s", m_ini.path());
    }

    if (!good)
    {
        rc_log("config: %s: %s", m_ini.path(), m_ini.error());
        return false;
    }

    /* The checks no single getter can make. */
    if (!cfg->secret[0])
        return fail("[server] secret: must be set");
    if (strcmp(cfg->secret, DEFAULT_SECRET) == 0)
        return fail("[server] secret: still the shipped default; set one");
    if (!cfg->allow.set(cfg->allowText))
        return fail("[server] allow: not an address or address/prefix");
    if (cfg->heartbeatS && cfg->heartbeatS < 5)
        return fail("[server] heartbeat_timeout_s: 0 to disable, or 5 or "
                    "more");
    return true;
}
