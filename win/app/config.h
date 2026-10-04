/* The client's settings: gaston.ini beside the exe, read and written
 * through libcore's IniFile.
 *
 * Replaces rc_config and core/ini.cpp (docs/REMOTE.md, C3). The difference
 * that matters: a value that is present but wrong -- a port that is not a
 * number, a key nobody reads -- now REFUSES startup with a message naming it,
 * where GetPrivateProfileIntA used to turn `port=78g0` into 78 and connect
 * to the wrong place without a word.
 *
 * Not the registry: an INI is inspectable with EDIT.COM on the guest itself.
 */
#ifndef RC_CONFIG_H
#define RC_CONFIG_H

#ifndef __cplusplus
#error "app/config.h is C++"
#endif

#include "app/modes.h"
#include "core/inifile.h"
#include "core/textlist.h"
#include "core/rc_cpp.h"

/* Bounds on everything a person can add in Options. Refused past these,
 * never truncated: a list cut short in silence is a place unprotected. */
#define RC_LIST_MAX     32      /* each of protect / danger / trust, +/- */
#define RC_IGNORE_MAX   64

#define RC_PAGE_SMALL   0
#define RC_PAGE_NORMAL  1
#define RC_PAGE_LARGE   2

/* What to do when a new work folder already holds files. ASK is the old
 * behaviour, the dialog in app/workdlg.cpp; the other three answer it in
 * advance with workcopy.h's RC_WORK_* (KEEP 0, REPLACE 1, MERGE 2). */
#define RC_EXISTING_ASK (-1)

struct ClientConfig
{
    char host[64];
    long port;
    char secret[128];
    char syncRoot[128];     /* "" disables sync entirely */
    bool autoscroll;
    /* May the agent drive this screen -- read the window tree, click, type,
     * take a picture, raise a window? Declared at the handshake, and the
     * bridge offers the five screen tools only when it is true (BACKLOG.md
     * 19). DEFAULTS TO TRUE: the ops have shipped since Phase 6 and this
     * client can genuinely drive a screen, so defaulting it off would remove
     * five tools from every install that upgrades, with no error anywhere to
     * say why the agent had stopped being able to see anything. */
    bool automation;

    /* --- General: defaults for NEW chats (an open one keeps its own) --- */
    char defaultModel[64];      /* "" = the bridge's */
    char defaultEffort[16];
    char defaultMode[RC_MODE_ID];
    int  pageSize;              /* RC_PAGE_* */

    /* --- Files ---------------------------------------------------------- */
    long sweepMin;              /* 0 = bridge default, -1 = only on change */
    long maxFileKb;             /* 0 = bridge default */
    int  existing;              /* RC_EXISTING_ASK or an RC_WORK_* */
    /* TextList, not std::vector<std::string> (core/textlist.h): this
     * header is included by half the program, and STL here cost the image
     * about 25KB with 600 bytes of budget left (measured 2026-10-04). */
    TextList ignore;

    /* --- Permissions ---------------------------------------------------- */
    long deadlineMin;           /* 0 = bridge default (ten minutes) */
    ModeDef modes[RC_MODES_MAX];/* the person's own */
    int  modeCount;
    /* Additions to, and removals from, the built-in lists (app/protect.cpp).
     * `danger` additions are "program reason". */
    TextList protectAdd, protectRem;
    TextList dangerAdd, dangerRem;
    TextList trustAdd, trustRem;

    /* --- Misc ----------------------------------------------------------- */
    char logDir[MAX_PATH];      /* "" = beside the exe; applies next start */
    bool cmdLog;                /* save every command and its output */

    ClientConfig();
};

class ConfigStore
{
public:
    /* <exeDir>\gaston.ini */
    explicit ConfigStore(const char *exeDir);

    /* Defaults, then the file. A missing file is written with the defaults
     * so there is something to edit. False if a present value is invalid:
     * the caller refuses to start and shows error(). */
    bool load(ClientConfig *cfg);

    /* Every field is attempted even after one fails, so a partially
     * writable file gets as much as it can hold; false if any failed. */
    bool save(const ClientConfig &cfg);

    const char *error() const;
    const char *path() const;

private:
    bool loadOptions(ClientConfig *cfg);
    bool loadModes(ClientConfig *cfg);
    bool saveOptions(const ClientConfig &cfg);
    bool saveModes(const ClientConfig &cfg);

    ConfigStore(const ConfigStore &);
    ConfigStore &operator=(const ConfigStore &);

    Path m_path;     /* must stay above m_ini: initialised first */
    IniFile m_ini;
};

#endif
