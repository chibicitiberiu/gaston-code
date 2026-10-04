/* modes.cpp -- see app/modes.h. */
#include <windows.h>
#include <string.h>

#include "app/modes.h"
#include "core/str.h"

const char *const RC_CAT_NAMES[RC_CATS] = {
    "files_work", "files_else", "files_sys",
    "run_work", "run_built", "run_else", "run_sys",
    "read", "purge", "screen_see", "screen_drive",
    "bridge_shell", "web",
};

/* THE PLAN'S TABLE (docs/OPTIONS-PLAN.md), letter for letter in the order
 * above. The agreement test parses these lines; keep each preset on one. */
const Preset RC_PRESETS[RC_PRESETS_N] = {
    { "manual",     "Ask first",           "aaaaaaayayaaa", false },
    { "workfolder", "Free in work folder", "yaayaaayayaay", false },
    { "plan",       "Plan first",          "aaaaaaayayaaa", true  },
    { "auto",       "Auto",                "yccyyccycyccy", false },
};

ModeDef::ModeDef()
{
    id[0] = '\0';
    label[0] = '\0';
    base[0] = '\0';
    memset(values, 'a', sizeof(values));
}

const Preset *rc_preset(const char *id)
{
    int i;
    if (!id) return NULL;
    for (i = 0; i < RC_PRESETS_N; i++)
        if (strcmp(RC_PRESETS[i].id, id) == 0)
            return &RC_PRESETS[i];
    return NULL;
}

const char *rc_mode_value_name(char v)
{
    switch (v) {
    case 'o': return "off";
    case 'a': return "ask";
    case 'c': return "check";
    case 'y': return "allow";
    }
    return NULL;
}

char rc_mode_value_of(const char *name)
{
    if (!name) return 0;
    if (strcmp(name, "off") == 0) return 'o';
    if (strcmp(name, "ask") == 0) return 'a';
    if (strcmp(name, "check") == 0) return 'c';
    if (strcmp(name, "allow") == 0) return 'y';
    return 0;
}

void rc_mode_new_id(char *out, unsigned cap)
{
    static DWORD counter;
    DWORD seed = GetTickCount() ^ (GetCurrentProcessId() << 16) ^
                 (++counter * 2654435761UL);
    SYSTEMTIME st;
    GetLocalTime(&st);
    seed ^= ((DWORD)st.wMilliseconds << 20) ^ ((DWORD)st.wSecond << 8)
            ^ st.wDay;
    rc_strf(out, cap, "u-%08lx", (unsigned long)seed);
}
