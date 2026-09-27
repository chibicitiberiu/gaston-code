/* Which peers may even reach the handshake.
 *
 * `allow=192.168.1.0/24` in gremote.ini, checked on the accepted socket
 * BEFORE AUTH (docs/REMOTE.md): a machine outside the range never gets to
 * guess at the secret, and never gets to hold the one client slot while it
 * tries. An empty setting means anywhere, which is the right default for a
 * box on an isolated LAN and the wrong one everywhere else -- so it is a
 * setting rather than a constant.
 *
 * One range, not a list. REMOTE.md specifies one, the test LAN is one
 * subnet, and a list invites the question of ordering and negation that
 * nobody has asked.
 */
#ifndef RC_REMOTE_ALLOW_H
#define RC_REMOTE_ALLOW_H

#ifndef __cplusplus
#error "remote/allow.h is C++"
#endif

#include <windows.h>

class AllowList
{
public:
    AllowList();

    /* "a.b.c.d/bits", or "a.b.c.d" for one host, or "" for anywhere. False
     * on anything else, INCLUDING a prefix above 32 or an address the
     * platform will not parse -- a rule nobody can read is refused at
     * startup rather than quietly admitting everyone. */
    bool set(const char *cidr);

    /* `addr` as Winsock hands it over: network byte order, from
     * sockaddr_in::sin_addr.s_addr. */
    bool allows(unsigned long addr) const;

    bool any() const { return m_any; }

private:
    unsigned long m_net;    /* host order, already masked */
    unsigned long m_mask;   /* host order */
    bool m_any;
};

#endif
