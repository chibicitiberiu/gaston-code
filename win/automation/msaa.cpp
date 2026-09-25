/* NONAMELESSUNION before the OLE headers, and it is not optional.
 *
 * Open Watcom's two headers disagree with each other about VARIANT without
 * it: oaidl.h picks the NAMED layout (n1.n2.n3) on __STDC__, while
 * oleauto.h's V_VT and V_I4 accessors only test NONAMELESSUNION and so
 * expand to (x)->vt against a struct that has no such member. The error
 * names a field you can see in the header, which is why it takes a while.
 */
#define NONAMELESSUNION
#include <objbase.h>
#include <oleauto.h>
#include <oleacc.h>

#include "automation/msaa.h"

#include <string.h>

/* Behind WINVER >= 0x0500, and the build pins 0x0400. OBJID_CLIENT is the
 * control; OBJID_WINDOW is its frame and carries nothing useful. */
#ifndef OBJID_CLIENT
#define OBJID_CLIENT 0xFFFFFFFCL
#endif

/* Spelled out rather than linked from uuid.lib, so a toolchain whose import
 * library lacks oleacc's IID cannot fail the link. */
static const GUID RC_IID_IAccessible =
    { 0x618736e0, 0x3c3d, 0x11cf,
      { 0x81, 0x0c, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };

/* REFIID is `const IID &` in C++ and `const IID *` in C, and the interface
 * methods are called directly rather than through lpVtbl. This file is C++;
 * win/checks/probe.c does the same work in C and therefore looks different. */
typedef HRESULT (WINAPI *PFN_AOFW)(HWND, LONG, REFIID, void **);
typedef UINT (WINAPI *PFN_ROLETEXT)(DWORD, LPSTR, UINT);

MsaaSource::MsaaSource()
{
    m_oleacc = LoadLibraryA("oleacc.dll");
    m_aofw = m_oleacc
        ? (void *)GetProcAddress(m_oleacc, "AccessibleObjectFromWindow") : 0;
    m_roleText = m_oleacc
        ? (void *)GetProcAddress(m_oleacc, "GetRoleTextA") : 0;
    m_comReady = false;
    m_made = 0;
    m_named = 0;
    if (m_aofw)
    {
        /* Apartment-threaded, and the caller's thread is expected to have a
         * message queue -- REMOTE.md puts the ui worker on one for exactly
         * this. S_FALSE means somebody already initialised it, which is
         * fine and must not be treated as failure. */
        HRESULT hr = CoInitialize(NULL);
        m_comReady = (hr == S_OK || hr == S_FALSE);
    }
}

MsaaSource::~MsaaSource()
{
    if (m_comReady)
        CoUninitialize();
    if (m_oleacc)
        FreeLibrary(m_oleacc);
}

static void bstrToNarrow(BSTR b, char *out, unsigned cap)
{
    out[0] = '\0';
    if (!b)
        return;
    WideCharToMultiByte(CP_ACP, 0, b, -1, out, (int)cap, NULL, NULL);
    out[cap - 1] = '\0';
}

/* "push button" -> "pushbutton". GetRoleTextA returns a human phrase with
 * spaces in it; a selector is written as [role=pushbutton] and should not
 * make the caller guess at spacing or case. */
static void normaliseRole(char *s)
{
    char *w = s;
    for (const char *r = s; *r; r++)
    {
        char c = *r;
        if (c == ' ' || c == '-' || c == '\t')
            continue;
        if (c >= 'A' && c <= 'Z')
            c = (char)(c - 'A' + 'a');
        *w++ = c;
    }
    *w = '\0';
}

bool MsaaSource::enrich(HWND h, int want, Enrichment &out)
{
    memset(&out, 0, sizeof(out));
    if (!m_aofw || !h || !IsWindow(h))
        return false;

    IAccessible *acc = 0;
    PFN_AOFW aofw = (PFN_AOFW)m_aofw;
    if (aofw(h, OBJID_CLIENT, RC_IID_IAccessible, (void **)&acc) != S_OK
        || !acc)
        return false;
    m_made++;

    VARIANT self;
    V_VT(&self) = VT_I4;
    V_I4(&self) = 0;                    /* CHILDID_SELF */

    BSTR b = 0;
    if ((want & Selector::WANT_NAME) && acc->get_accName(self, &b) == S_OK
        && b)
    {
        bstrToNarrow(b, out.name, sizeof(out.name));
        SysFreeString(b);
        b = 0;
        if (out.name[0])
            m_named++;
    }
    if ((want & Selector::WANT_VALUE) && acc->get_accValue(self, &b) == S_OK
        && b)
    {
        bstrToNarrow(b, out.value, sizeof(out.value));
        SysFreeString(b);
        b = 0;
    }

    VARIANT vrole;
    V_VT(&vrole) = VT_EMPTY;
    if ((want & Selector::WANT_ROLE)
        && acc->get_accRole(self, &vrole) == S_OK
        && V_VT(&vrole) == VT_I4)
    {
        long role = V_I4(&vrole);
        if (m_roleText)
        {
            PFN_ROLETEXT rt = (PFN_ROLETEXT)m_roleText;
            rt((DWORD)role, out.role, sizeof(out.role));
            normaliseRole(out.role);
        }
        /* No GetRoleTextA is not a reason to lose the role. The number is
         * still an answer, and a selector can be written against it. */
        if (!out.role[0])
        {
            char *w = out.role;
            long v = role;
            char digits[16];
            int n = 0;
            if (v == 0) digits[n++] = '0';
            while (v > 0 && n < 15) { digits[n++] = (char)('0' + v % 10);
                                      v /= 10; }
            while (n > 0) *w++ = digits[--n];
            *w = '\0';
        }
    }

    acc->Release();
    return true;
}
