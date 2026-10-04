/* textlist.cpp -- see core/textlist.h. */
#include <windows.h>
#include <string.h>

#include "core/textlist.h"

TextList::TextList() : m_buf(0), m_len(0), m_cap(0) {}

TextList::TextList(const TextList &other) : m_buf(0), m_len(0), m_cap(0)
{
    *this = other;
}

TextList &TextList::operator=(const TextList &other)
{
    if (this == &other) return *this;
    clear();
    if (other.m_len && reserve(other.m_len + 1)) {
        memcpy(m_buf, other.m_buf, other.m_len + 1);
        m_len = other.m_len;
    }
    return *this;
}

TextList::~TextList()
{
    if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf);
}

bool TextList::reserve(unsigned need)
{
    if (need <= m_cap) return true;
    unsigned cap = m_cap ? m_cap * 2 : 128;
    while (cap < need) cap *= 2;
    char *p = (char *)(m_buf ? HeapReAlloc(GetProcessHeap(), 0, m_buf, cap)
                             : HeapAlloc(GetProcessHeap(), 0, cap));
    if (!p) return false;
    m_buf = p;
    m_cap = cap;
    return true;
}

bool TextList::next(unsigned *pos, char *out, unsigned cap) const
{
    while (*pos < m_len) {
        unsigned start = *pos, end = start;
        while (end < m_len && m_buf[end] != '\n') end++;
        *pos = end < m_len ? end + 1 : end;
        unsigned len = end - start;
        if (len == 0 || len >= cap) continue;
        memcpy(out, m_buf + start, len);
        out[len] = 0;
        return true;
    }
    return false;
}

unsigned TextList::count() const
{
    unsigned n = 0;
    for (unsigned i = 0; i < m_len; i++)
        if (m_buf[i] == '\n') n++;
    return m_len ? n + 1 : 0;
}

bool TextList::has(const char *entry) const
{
    if (!entry || !*entry) return false;
    unsigned want = (unsigned)strlen(entry), pos = 0;
    while (pos < m_len) {
        unsigned end = pos;
        while (end < m_len && m_buf[end] != '\n') end++;
        if (end - pos == want && strnicmp(m_buf + pos, entry, want) == 0)
            return true;
        pos = end + 1;
    }
    return false;
}

bool TextList::add(const char *entry)
{
    if (!entry || !*entry || strchr(entry, '\n') || has(entry)) return false;
    unsigned n = (unsigned)strlen(entry);
    if (!reserve(m_len + n + 2)) return false;
    if (m_len) m_buf[m_len++] = '\n';
    memcpy(m_buf + m_len, entry, n);
    m_len += n;
    m_buf[m_len] = 0;
    return true;
}

void TextList::remove(const char *entry)
{
    if (!entry || !m_len) return;
    unsigned want = (unsigned)strlen(entry), pos = 0;
    while (pos < m_len) {
        unsigned end = pos;
        while (end < m_len && m_buf[end] != '\n') end++;
        if (end - pos == want && strnicmp(m_buf + pos, entry, want) == 0) {
            /* Take the entry and ONE separator with it. */
            unsigned from = pos, to = end < m_len ? end + 1 : end;
            if (to == m_len && from > 0) from--;      /* last: eat the one before */
            memmove(m_buf + from, m_buf + to, m_len - to + 1);
            m_len -= to - from;
            continue;
        }
        pos = end + 1;
    }
}

void TextList::clear()
{
    m_len = 0;
    if (m_buf) m_buf[0] = 0;
}
