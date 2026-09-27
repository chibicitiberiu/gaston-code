/* AllowList. See allow.h. */
#include <winsock.h>
#include <string.h>
#include "core/str.h"
#include "remote/allow.h"

AllowList::AllowList() : m_net(0), m_mask(0), m_any(true)
{
}

bool AllowList::set(const char *cidr)
{
    char text[64];
    char *slash;
    unsigned long bits = 32;
    unsigned long addr;

    m_any = true;
    m_net = 0;
    m_mask = 0;
    if (!cidr || !*cidr)
        return true;
    if (!rc_strcpy(text, sizeof text, cidr))
        return false;

    slash = strchr(text, '/');
    if (slash)
    {
        const char *p = slash + 1;
        *slash = '\0';
        /* Digits only, and at most two of them: strtoul would take " 24",
         * "0x18" and "24junk", and a prefix nobody meant is a rule nobody
         * meant. */
        if (!*p || strlen(p) > 2)
            return false;
        bits = 0;
        for (; *p; ++p)
        {
            if (*p < '0' || *p > '9')
                return false;
            bits = bits * 10 + (unsigned long)(*p - '0');
        }
        if (bits > 32)
            return false;
    }

    /* inet_addr says INADDR_NONE for both "bad" and "255.255.255.255"; the
     * broadcast address is not something to allow anyway, so refusing it
     * costs nothing and keeps the test to one comparison. */
    addr = inet_addr(text);
    if (addr == INADDR_NONE)
        return false;

    /* A shift of 32 on x86 shifts by 0 -- the count is taken mod 32 -- so
     * /0 would become a mask of 0xFFFFFFFF, the exact opposite of what it
     * means. Both ends are spelled out rather than computed. */
    m_mask = (bits == 0) ? 0UL : (bits == 32 ? 0xFFFFFFFFUL
                                             : ~((1UL << (32 - bits)) - 1UL));
    m_net = ntohl(addr) & m_mask;
    m_any = false;
    return true;
}

bool AllowList::allows(unsigned long addr) const
{
    if (m_any)
        return true;
    return (ntohl(addr) & m_mask) == m_net;
}
