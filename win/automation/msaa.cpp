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
#include "automation/calldeadline.h"

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
typedef HRESULT (WINAPI *PFN_ACCCHILDREN)(IAccessible *, LONG, LONG,
                                          VARIANT *, LONG *);

/* {00020404-0000-0000-C000-000000000046}, spelled out for the same reason
 * as IAccessible's: a toolchain whose uuid.lib is missing one must not fail
 * the link over it. */
static const GUID RC_IID_IEnumVARIANT =
    { 0x00020404, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };

bool rc_msaa_present(void)
{
    HMODULE lib = LoadLibraryA("oleacc.dll");
    bool got = false;
    if (lib)
    {
        got = GetProcAddress(lib, "AccessibleObjectFromWindow") != 0;
        FreeLibrary(lib);
    }
    return got;
}

void MsaaSource::disable()
{
    /* The constructor has already loaded it, so this frees rather than
     * prevents -- the observable contract (`available()` false, the walk
     * on the window tree, HELLO saying 0) is the same either way, and the
     * library does not stay mapped. */
    if (m_oleacc)
    {
        FreeLibrary(m_oleacc);
        m_oleacc = 0;
    }
    m_aofw = 0;
    m_roleText = 0;
    m_children = 0;
}

MsaaSource::MsaaSource()
{
    m_oleacc = LoadLibraryA("oleacc.dll");
    m_aofw = m_oleacc
        ? (void *)GetProcAddress(m_oleacc, "AccessibleObjectFromWindow") : 0;
    m_roleText = m_oleacc
        ? (void *)GetProcAddress(m_oleacc, "GetRoleTextA") : 0;
    m_children = m_oleacc
        ? (void *)GetProcAddress(m_oleacc, "AccessibleChildren") : 0;
    m_comReady = false;
    m_made = 0;
    m_named = 0;
    m_byEnum = 0;
    m_byHelper = 0;
    m_until = 0;
    m_hasDeadline = false;
    m_accSeen = 0;
    m_deadline = 0;
    m_abandoned = 0;
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

/* Our own spellings, not GetStateTextA's.
 *
 * GetStateTextA returns a human phrase in the SYSTEM's language, so on a
 * German Win98 `[state*="checked"]` would silently never match. A selector
 * is a grammar and a grammar does not move with the locale. The bit numbers
 * are fixed by MSAA and are the same everywhere.
 *
 * Ordered by what a caller actually asks about, because a list longer than
 * the field is truncated at a name boundary and the ones at the front are
 * the ones worth keeping. */
struct StateName { long bit; const char *name; };

static const StateName kStates[] =
{
    { 0x00000010L, "checked"     },
    { 0x00000020L, "mixed"       },
    { 0x00000008L, "pressed"     },
    { 0x00000002L, "selected"    },
    { 0x00000001L, "unavailable" },   /* MSAA's word for disabled */
    { 0x00000200L, "expanded"    },
    { 0x00000400L, "collapsed"   },
    { 0x00000040L, "readonly"    },
    { 0x00000004L, "focused"     },
    { 0x00100000L, "focusable"   },
    { 0x00008000L, "invisible"   },
    { 0x00010000L, "offscreen"   }
};

static void stateToText(long bits, char *out, unsigned cap)
{
    unsigned n = 0;
    out[0] = '\0';
    for (unsigned i = 0; i < sizeof(kStates) / sizeof(kStates[0]); i++)
    {
        if (!(bits & kStates[i].bit))
            continue;
        unsigned len = (unsigned)strlen(kStates[i].name);
        unsigned need = len + (n ? 1 : 0);
        /* Whole names only. A list cut mid-word would make
         * [state*="checked"] match "checke" -- or worse, not match a control
         * that genuinely is checked. */
        if (n + need + 1 > cap)
            break;
        if (n)
            out[n++] = ',';
        memcpy(out + n, kStates[i].name, len);
        n += len;
    }
    out[n] = '\0';
}

bool MsaaSource::enrich(HWND h, int want, Enrichment &out)
{
    memset(&out, 0, sizeof(out));
    if (!m_aofw || !h || !IsWindow(h))
        return false;
    /* Past the deadline, do not start. A call begun here would be cancelled
     * by the filter anyway, after a round trip we already know we cannot
     * afford. */
    if (m_deadline && m_deadline->expired())
    {
        m_abandoned++;
        return false;
    }

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
    /* Cleared unconditionally at the end of the block: a custom control may
     * answer with a VT_BSTR role string rather than a number, and the
     * VT_I4 guard would then skip past an allocation nobody frees -- once
     * per candidate, in the tier that runs on every resolve. */
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

    VariantClear(&vrole);

    if (want & Selector::WANT_ACTION)
    {
        b = 0;
        if (acc->get_accDefaultAction(self, &b) == S_OK && b)
        {
            bstrToNarrow(b, out.action, sizeof(out.action));
            SysFreeString(b);
            b = 0;
            /* The same fold as the role, for the same reason: it arrives as
             * "Switch To" and a selector is written [action=switchto]. */
            normaliseRole(out.action);
        }
    }

    VARIANT vstate;
    V_VT(&vstate) = VT_EMPTY;
    if ((want & Selector::WANT_STATE)
        && acc->get_accState(self, &vstate) == S_OK
        && V_VT(&vstate) == VT_I4)
    {
        stateToText(V_I4(&vstate), out.state, sizeof(out.state));
    }

    acc->Release();
    return true;
}

/* ---- the accessible tree ------------------------------------------- */

AccWalkOptions::AccWalkOptions()
{
    /* Name and role only. Each extra field is another marshalled call per
     * node, and a document has hundreds of nodes -- the difference between
     * two fields and four is the difference between a page that reads in a
     * second and one that does not read at all. */
    want = Selector::WANT_NAME | Selector::WANT_ROLE;
    /* A page, not a desktop. Past this the caller is told it was truncated
     * rather than handed a partial tree that looks complete. */
    maxNodes = 512;
    maxDepth = 16;
    deadlineMs = 8000;
}

/* How many children to ask for in one go.
 *
 * The batch IS the optimisation. Per-child AccessibleObjectFromWindow was
 * measured at 8500 us a node because each one is a WM_GETOBJECT round trip;
 * sixteen children in one Next is one crossing. Sixteen rather than more
 * because the VARIANT array is a stack local and the frame has 4096 bytes
 * before it steps over Win98's guard page. */
enum { ACC_BATCH = 16 };

void MsaaSource::fillAcc(IAccessible *acc, long childId, int depth,
                         const AccWalkOptions &opt, AccNode &out)
{
    memset(&out, 0, sizeof(out));
    out.depth = depth;
    out.childId = childId;
    out.roleId = -1;

    VARIANT self;
    V_VT(&self) = VT_I4;
    V_I4(&self) = childId;

    VARIANT vrole;
    V_VT(&vrole) = VT_EMPTY;
    if (acc->get_accRole(self, &vrole) == S_OK && V_VT(&vrole) == VT_I4)
    {
        out.roleId = V_I4(&vrole);
        if ((opt.want & Selector::WANT_ROLE) && m_roleText)
        {
            PFN_ROLETEXT rt = (PFN_ROLETEXT)m_roleText;
            rt((DWORD)out.roleId, out.role, sizeof(out.role));
            normaliseRole(out.role);
        }
    }
    VariantClear(&vrole);

    BSTR b = 0;
    if ((opt.want & Selector::WANT_NAME)
        && acc->get_accName(self, &b) == S_OK && b)
    {
        bstrToNarrow(b, out.name, sizeof(out.name));
        SysFreeString(b);
        b = 0;
        if (out.name[0])
            m_named++;
    }
    if ((opt.want & Selector::WANT_VALUE)
        && acc->get_accValue(self, &b) == S_OK && b)
    {
        bstrToNarrow(b, out.value, sizeof(out.value));
        SysFreeString(b);
        b = 0;
    }
    if (opt.want & Selector::WANT_STATE)
    {
        VARIANT vstate;
        V_VT(&vstate) = VT_EMPTY;
        if (acc->get_accState(self, &vstate) == S_OK
            && V_VT(&vstate) == VT_I4)
            stateToText(V_I4(&vstate), out.state, sizeof(out.state));
        VariantClear(&vstate);
    }

    /* Always. A node with no coordinates cannot be clicked, and clicking is
     * what this tier is for -- a link's text tells the agent it is there,
     * the rectangle is what lets it press it. */
    long x = 0, y = 0, w = 0, h = 0;
    if (acc->accLocation(&x, &y, &w, &h, self) == S_OK && (w || h))
    {
        out.rect.left = x;
        out.rect.top = y;
        out.rect.right = x + w;
        out.rect.bottom = y + h;
        out.hasRect = true;
    }
}

AccEnd MsaaSource::walkAcc(IAccessible *acc, long childId, int depth,
                           const AccWalkOptions &opt, AccSink &sink)
{
    if (m_hasDeadline && (long)(GetTickCount() - m_until) >= 0)
        return ACC_DEADLINE;
    if (m_accSeen >= opt.maxNodes)
        return ACC_MAXNODES;

    {
        AccNode n;
        fillAcc(acc, childId, depth, opt, n);
        m_accSeen++;
        if (!sink.onAcc(n))
            return ACC_STOPPED;
    }

    /* A simple element has no children of its own -- it is not an object,
     * it is an id its parent answers for. Asking it for children asks the
     * PARENT again and walks forever. */
    if (childId != 0)
        return ACC_COMPLETE;

    if (depth >= opt.maxDepth)
        return ACC_MAXDEPTH;

    long total = 0;
    if (acc->get_accChildCount(&total) != S_OK || total <= 0)
        return ACC_COMPLETE;

    /* The enumerator first, the helper behind it. Not a preference: the two
     * machines disagreed about which is faster and agreed that both work,
     * so what matters is that a missing or broken one is caught rather than
     * hard-coded around. */
    IEnumVARIANT *en = 0;
    if (acc->QueryInterface(RC_IID_IEnumVARIANT, (void **)&en) != S_OK)
        en = 0;

    PFN_ACCCHILDREN kids = (PFN_ACCCHILDREN)m_children;
    long taken = 0;
    AccEnd end = ACC_COMPLETE;

    while (taken < total && end == ACC_COMPLETE)
    {
        VARIANT batch[ACC_BATCH];
        unsigned long got = 0;
        bool viaEnum = false;

        for (int i = 0; i < ACC_BATCH; i++)
            V_VT(&batch[i]) = VT_EMPTY;

        if (en && en->Next(ACC_BATCH, batch, &got) >= S_OK && got > 0)
        {
            viaEnum = true;
            m_byEnum++;
        }
        else if (kids)
        {
            LONG n = 0;
            long want = total - taken;
            if (want > ACC_BATCH)
                want = ACC_BATCH;
            if (kids(acc, taken, want, batch, &n) < S_OK || n <= 0)
                break;
            got = (unsigned long)n;
            m_byHelper++;
        }
        else
        {
            break;      /* neither route exists; nothing more to say */
        }

        for (unsigned long i = 0; i < got && end == ACC_COMPLETE; i++)
        {
            if (V_VT(&batch[i]) == VT_DISPATCH && V_DISPATCH(&batch[i]))
            {
                IAccessible *kid = 0;
                if (V_DISPATCH(&batch[i])->QueryInterface(
                        RC_IID_IAccessible, (void **)&kid) == S_OK && kid)
                {
                    m_made++;
                    end = walkAcc(kid, 0, depth + 1, opt, sink);
                    kid->Release();
                }
            }
            else if (V_VT(&batch[i]) == VT_I4)
            {
                end = walkAcc(acc, V_I4(&batch[i]), depth + 1, opt, sink);
            }
        }

        for (unsigned long i = 0; i < got; i++)
            VariantClear(&batch[i]);

        taken += (long)got;
        /* A batcher that keeps returning fewer than it promised would spin
         * here forever on a count that does not shrink. */
        if (!viaEnum && got == 0)
            break;
    }

    if (en)
        en->Release();
    return end;
}

AccEnd MsaaSource::enumerateFrom(HWND root, const AccWalkOptions &opt,
                                 AccSink &sink)
{
    m_byEnum = 0;
    m_byHelper = 0;
    m_accSeen = 0;
    m_hasDeadline = opt.deadlineMs != 0;
    m_until = GetTickCount() + opt.deadlineMs;

    if (!m_aofw || !root || !IsWindow(root))
        return ACC_NO_OBJECT;

    /* One arm for the whole enumeration rather than one per node: the
     * filter's job is to stop a single call blocking forever, and the
     * walk's own deadline check between nodes does the rest. */
    if (m_deadline && opt.deadlineMs)
        m_deadline->arm(opt.deadlineMs);

    IAccessible *acc = 0;
    PFN_AOFW aofw = (PFN_AOFW)m_aofw;
    if (aofw(root, OBJID_CLIENT, RC_IID_IAccessible, (void **)&acc) != S_OK
        || !acc)
        return ACC_NO_OBJECT;
    m_made++;

    AccEnd end = walkAcc(acc, 0, 0, opt, sink);
    acc->Release();
    if (m_deadline && opt.deadlineMs)
        m_deadline->disarm();
    return end;
}
