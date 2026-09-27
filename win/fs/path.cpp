#include "fs/path.h"

#include <string.h>

WorkNames::WorkNames()
    : tmpSuffix(""), oldSuffix(""), reserve(0), aside(false)
{
}

WorkNames::WorkNames(const char *tmp, const char *old, unsigned res,
                     bool moveAside)
    : tmpSuffix(tmp ? tmp : ""), oldSuffix(old ? old : ""), reserve(res),
      aside(moveAside)
{
}

bool isContainedRelative(const char *rel)
{
    const char *p = rel;

    if (!rel || !*rel) return false;
    if (rel[0] == '/' || rel[0] == '\\') return false;
    if (rel[1] == ':') return false;                  /* a drive spec */

    while (*p)
    {
        const char *seg = p;
        unsigned len = 0;
        while (*p && *p != '/' && *p != '\\') { p++; len++; }
        if (len == 2 && seg[0] == '.' && seg[1] == '.') return false;
        if (len == 1 && seg[0] == '.') return false;
        if (len == 0) return false;                   /* empty component */
        if (*p == '/' || *p == '\\') p++;
    }
    return true;
}

static bool endsWith(const char *name, unsigned len, const char *suffix)
{
    unsigned sn;
    if (!suffix || !*suffix) return false;
    sn = (unsigned)strlen(suffix);
    if (len <= sn) return false;
    return strcmp(name + len - sn, suffix) == 0;
}

bool isWorkingName(const char *name, const WorkNames &names)
{
    unsigned len;
    if (!name) return false;
    len = (unsigned)strlen(name);
    if (endsWith(name, len, names.tmpSuffix)) return true;
    if (endsWith(name, len, names.oldSuffix)) return true;
    return false;
}

bool fitsWithReserve(const char *root, const char *rel, unsigned cap,
                     const WorkNames &names)
{
    unsigned need;
    if (!root || !rel || cap == 0) return false;
    need = (unsigned)strlen(root) + 1 + (unsigned)strlen(rel);
    return need + names.reserve <= cap - 1;
}
