/* dlgtemplate.cpp -- see core/dlgtemplate.h. */
#include <string.h>

#include "core/dlgtemplate.h"

DlgTemplate::DlgTemplate() : m_buf(0), m_len(0), m_bad(false), m_count(0)
{
    m_buf = (BYTE *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                              RC_DLGT_MAX);
    if (!m_buf) m_bad = true;
}

DlgTemplate::~DlgTemplate()
{
    if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf);
}

void DlgTemplate::bytes(const void *p, unsigned n)
{
    if (m_bad) return;
    if (m_len + n > RC_DLGT_MAX) { m_bad = true; return; }
    memcpy(m_buf + m_len, p, n);
    m_len += n;
}

void DlgTemplate::wide(const char *s)
{
    for (; s && *s; s++) w((WORD)(unsigned char)*s);
    w(0);
}

void DlgTemplate::align()
{
    while (!m_bad && (m_len & 3)) {
        if (m_len >= RC_DLGT_MAX) { m_bad = true; return; }
        m_buf[m_len++] = 0;
    }
}

void DlgTemplate::begin(DWORD style, short cx, short cy, const char *title)
{
    m_len = 0;
    m_count = 0;
    dw(style);
    dw(0);                      /* extended style */
    w(0);                       /* control count, patched by item() */
    w(0); w(0);                 /* x, y */
    w((WORD)cx); w((WORD)cy);
    w(0);                       /* no menu */
    w(0);                       /* default class */
    wide(title);
    w(8);                       /* point size: DS_SETFONT */
    wide("MS Sans Serif");
}

void DlgTemplate::head(DWORD style, short x, short y, short cx, short cy,
                       WORD id)
{
    align();
    dw(style | WS_CHILD | WS_VISIBLE);
    dw(0);
    w((WORD)x); w((WORD)y); w((WORD)cx); w((WORD)cy);
    w(id);
    if (!m_bad) {
        m_count++;
        memcpy(m_buf + 8, &m_count, sizeof m_count);
    }
}

void DlgTemplate::item(DWORD style, short x, short y, short cx, short cy,
                       WORD id, WORD cls, const char *text)
{
    head(style, x, y, cx, cy, id);
    w(0xFFFF); w(cls);          /* the predefined-class form */
    wide(text);
    w(0);                       /* no creation data */
}

void DlgTemplate::item(DWORD style, short x, short y, short cx, short cy,
                       WORD id, const char *cls, const char *text)
{
    head(style, x, y, cx, cy, id);
    wide(cls);
    wide(text);
    w(0);
}

const DLGTEMPLATE *DlgTemplate::get() const
{
    return m_bad || !m_buf ? 0 : (const DLGTEMPLATE *)m_buf;
}
