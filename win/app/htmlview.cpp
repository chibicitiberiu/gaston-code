/* htmlview.cpp -- the transcript, hosted in MSHTML. See htmlview.h.
 *
 * The COM host below is the one proven by win/htmlspike.c on real Win98 SE:
 * IOleClientSite, IOleInPlaceSite and IOleInPlaceFrame as embedded structs
 * with static vtables, recovered to the host by fixed offsets. That is the
 * standard way to do COM from C and it needs no ATL and no C++.
 *
 * Open Watcom ships mshtml.h, exdisp.h, docobj.h and oleidl.h but NOT
 * mshtmhst.h, so IDocHostUIHandler is unavailable without hand-declaring it.
 * It only controls chrome -- the 3D border, the context menu -- so it is left
 * out; the scrollbars it would suppress are ones the transcript wants anyway.
 */
#define CINTERFACE
#define COBJMACROS
/* oaidl.h declares VARIANT with NAMED unions under __STDC__, but oleauto.h's
 * V_VT/V_UNION macros only follow suit when NONAMELESSUNION is defined. Left
 * alone the two disagree and every VARIANT access fails to compile. */
#define NONAMELESSUNION
#include <windows.h>
#include <ole2.h>
#include <exdisp.h>
/* Watcom's own mshtml.h has `typedef enum _BoolValue {...};` with no name
 * bound to the typedef -- harmless under C, but C++ (which this file now is)
 * diagnoses the dangling `typedef` as an unexpected storage class specifier
 * (W146). Nothing here to fix; it is the shipped header, not our code.
 * `#pragma warning 146 9` does not silence it in this Watcom build, so the
 * Makefile passes -wcd=146 for this one translation unit instead. */
#include <mshtml.h>
#include <docobj.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "app/htmlview.h"
#include "app/comptr.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

typedef struct Host {
    IOleClientSite     site;
    IOleInPlaceSite    place;
    IOleInPlaceFrame   frame;
    /* No IDocHostUIHandler: Open Watcom ships mshtml.h but not mshtmhst.h, so
     * that interface has to be hand-declared. It only controls chrome -- the
     * 3D border, the scrollbars, the context menu -- so the spike does without
     * it and answers the question that matters first, which is whether this
     * activates and paints at all. */
    LONG               refs;
    HWND               hwnd;      /* our window, the control's parent */
} Host;

#define FROM_SITE(p)  ((Host *)(void *)(p))
#define FROM_PLACE(p) ((Host *)(((char *)(p)) - offsetof(Host, place)))
#define FROM_FRAME(p) ((Host *)(((char *)(p)) - offsetof(Host, frame)))

static HRESULT host_qi(Host *h, REFIID riid, void **out)
{
    if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IOleClientSite))
        *out = &h->site;
    else if (IsEqualIID(riid, IID_IOleInPlaceSite))
        *out = &h->place;
    else if (IsEqualIID(riid, IID_IOleInPlaceFrame)
             || IsEqualIID(riid, IID_IOleInPlaceUIWindow)
             || IsEqualIID(riid, IID_IOleWindow))
        *out = &h->frame;
    else { *out = NULL; return E_NOINTERFACE; }
    h->refs++;
    return S_OK;
}

/* The host outlives the control, so reference counting is a formality here;
 * it still has to be present and consistent or the control will misbehave. */
#define STD_IUNKNOWN(NAME, FROM)                                              \
static HRESULT STDMETHODCALLTYPE NAME##_QueryInterface(void *p, REFIID r,     \
                                                       void **o)              \
{ return host_qi(FROM(p), r, o); }                                            \
static ULONG STDMETHODCALLTYPE NAME##_AddRef(void *p)                         \
{ return (ULONG)(++FROM(p)->refs); }                                          \
static ULONG STDMETHODCALLTYPE NAME##_Release(void *p)                        \
{ return (ULONG)(--FROM(p)->refs); }

STD_IUNKNOWN(site, FROM_SITE)
STD_IUNKNOWN(place, FROM_PLACE)
STD_IUNKNOWN(frame, FROM_FRAME)

/* --- IOleClientSite ---------------------------------------------------- */
static HRESULT STDMETHODCALLTYPE site_SaveObject(void *p)
{ (void)p; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE site_GetMoniker(void *p, DWORD a, DWORD b,
                                                 IMoniker **m)
{ (void)p; (void)a; (void)b; *m = NULL; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE site_GetContainer(void *p, IOleContainer **c)
{ (void)p; *c = NULL; return E_NOINTERFACE; }
static HRESULT STDMETHODCALLTYPE site_ShowObject(void *p)
{ (void)p; return S_OK; }
static HRESULT STDMETHODCALLTYPE site_OnShowWindow(void *p, BOOL s)
{ (void)p; (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE site_RequestNewObjectLayout(void *p)
{ (void)p; return E_NOTIMPL; }

/* Open Watcom's C++ mode requires an exact function-pointer type match to
 * initialize a Vtbl struct field -- the (void *) cast that satisfies C (which
 * allows an implicit void* <-> function-pointer round trip) does not satisfy
 * C++. These typedefs name the real per-interface signature so each slot
 * below can be cast to it explicitly; the reinterpret is the same one C did
 * silently, same functions, same order, same values. */
typedef HRESULT (STDMETHODCALLTYPE *Site_QI)(IOleClientSite *, REFIID, void **);
typedef ULONG   (STDMETHODCALLTYPE *Site_Ref)(IOleClientSite *);
typedef HRESULT (STDMETHODCALLTYPE *Site_Void)(IOleClientSite *);
typedef HRESULT (STDMETHODCALLTYPE *Site_GetMoniker)(IOleClientSite *, DWORD, DWORD, IMoniker **);
typedef HRESULT (STDMETHODCALLTYPE *Site_GetContainer)(IOleClientSite *, IOleContainer **);
typedef HRESULT (STDMETHODCALLTYPE *Site_OnShowWindow)(IOleClientSite *, BOOL);

static IOleClientSiteVtbl g_site_vtbl = {
    (Site_QI)site_QueryInterface, (Site_Ref)site_AddRef, (Site_Ref)site_Release,
    (Site_Void)site_SaveObject, (Site_GetMoniker)site_GetMoniker,
    (Site_GetContainer)site_GetContainer,
    (Site_Void)site_ShowObject, (Site_OnShowWindow)site_OnShowWindow,
    (Site_Void)site_RequestNewObjectLayout
};

/* --- IOleWindow / IOleInPlaceFrame ------------------------------------- */
static HRESULT STDMETHODCALLTYPE frame_GetWindow(void *p, HWND *h)
{ *h = FROM_FRAME(p)->hwnd; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_ContextSensitiveHelp(void *p, BOOL b)
{ (void)p; (void)b; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_GetBorder(void *p, LPRECT r)
{ (void)p; (void)r; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE frame_RequestBorderSpace(void *p, LPCBORDERWIDTHS w)
{ (void)p; (void)w; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE frame_SetBorderSpace(void *p, LPCBORDERWIDTHS w)
{ (void)p; (void)w; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE frame_SetActiveObject(void *p, void *o, LPCOLESTR n)
{ (void)p; (void)o; (void)n; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_InsertMenus(void *p, HMENU m, void *w)
{ (void)p; (void)m; (void)w; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE frame_SetMenu(void *p, HMENU m, HOLEMENU h, HWND w)
{ (void)p; (void)m; (void)h; (void)w; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_RemoveMenus(void *p, HMENU m)
{ (void)p; (void)m; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE frame_SetStatusText(void *p, LPCOLESTR t)
{ (void)p; (void)t; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_EnableModeless(void *p, BOOL e)
{ (void)p; (void)e; return S_OK; }
static HRESULT STDMETHODCALLTYPE frame_TranslateAccelerator(void *p, LPMSG m, WORD w)
{ (void)p; (void)m; (void)w; return S_FALSE; }

/* Same reasoning as Site_* above, for IOleInPlaceFrame's slots. */
typedef HRESULT (STDMETHODCALLTYPE *Frame_QI)(IOleInPlaceFrame *, REFIID, void **);
typedef ULONG   (STDMETHODCALLTYPE *Frame_Ref)(IOleInPlaceFrame *);
typedef HRESULT (STDMETHODCALLTYPE *Frame_GetWindow)(IOleInPlaceFrame *, HWND *);
typedef HRESULT (STDMETHODCALLTYPE *Frame_BOOL)(IOleInPlaceFrame *, BOOL);
typedef HRESULT (STDMETHODCALLTYPE *Frame_GetBorder)(IOleInPlaceFrame *, LPRECT);
typedef HRESULT (STDMETHODCALLTYPE *Frame_BorderSpace)(IOleInPlaceFrame *, LPCBORDERWIDTHS);
typedef HRESULT (STDMETHODCALLTYPE *Frame_SetActiveObject)(IOleInPlaceFrame *, IOleInPlaceActiveObject *, LPCOLESTR);
typedef HRESULT (STDMETHODCALLTYPE *Frame_InsertMenus)(IOleInPlaceFrame *, HMENU, LPOLEMENUGROUPWIDTHS);
typedef HRESULT (STDMETHODCALLTYPE *Frame_SetMenu)(IOleInPlaceFrame *, HMENU, HOLEMENU, HWND);
typedef HRESULT (STDMETHODCALLTYPE *Frame_RemoveMenus)(IOleInPlaceFrame *, HMENU);
typedef HRESULT (STDMETHODCALLTYPE *Frame_SetStatusText)(IOleInPlaceFrame *, LPCOLESTR);
typedef HRESULT (STDMETHODCALLTYPE *Frame_TranslateAccelerator)(IOleInPlaceFrame *, LPMSG, WORD);

static IOleInPlaceFrameVtbl g_frame_vtbl = {
    (Frame_QI)frame_QueryInterface, (Frame_Ref)frame_AddRef, (Frame_Ref)frame_Release,
    (Frame_GetWindow)frame_GetWindow, (Frame_BOOL)frame_ContextSensitiveHelp,
    (Frame_GetBorder)frame_GetBorder, (Frame_BorderSpace)frame_RequestBorderSpace,
    (Frame_BorderSpace)frame_SetBorderSpace, (Frame_SetActiveObject)frame_SetActiveObject,
    (Frame_InsertMenus)frame_InsertMenus, (Frame_SetMenu)frame_SetMenu,
    (Frame_RemoveMenus)frame_RemoveMenus,
    (Frame_SetStatusText)frame_SetStatusText, (Frame_BOOL)frame_EnableModeless,
    (Frame_TranslateAccelerator)frame_TranslateAccelerator
};

/* --- IOleInPlaceSite --------------------------------------------------- */
static HRESULT STDMETHODCALLTYPE place_GetWindow(void *p, HWND *h)
{ *h = FROM_PLACE(p)->hwnd; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_ContextSensitiveHelp(void *p, BOOL b)
{ (void)p; (void)b; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_CanInPlaceActivate(void *p)
{ (void)p; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_OnInPlaceActivate(void *p)
{ (void)p; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_OnUIActivate(void *p)
{ (void)p; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_GetWindowContext(void *p,
        IOleInPlaceFrame **fr, IOleInPlaceUIWindow **doc,
        LPRECT pos, LPRECT clip, LPOLEINPLACEFRAMEINFO info)
{
    Host *h = FROM_PLACE(p);
    *fr = &h->frame;
    *doc = NULL;
    GetClientRect(h->hwnd, pos);
    *clip = *pos;
    info->cb = sizeof(OLEINPLACEFRAMEINFO);
    info->fMDIApp = FALSE;
    info->hwndFrame = h->hwnd;
    info->haccel = NULL;
    info->cAccelEntries = 0;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE place_Scroll(void *p, SIZE s)
{ (void)p; (void)s; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE place_OnUIDeactivate(void *p, BOOL f)
{ (void)p; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_OnInPlaceDeactivate(void *p)
{ (void)p; return S_OK; }
static HRESULT STDMETHODCALLTYPE place_DiscardUndoState(void *p)
{ (void)p; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE place_DeactivateAndUndo(void *p)
{ (void)p; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE place_OnPosRectChange(void *p, LPCRECT r)
{ (void)p; (void)r; return S_OK; }

/* Same reasoning as Site_* above, for IOleInPlaceSite's slots. */
typedef HRESULT (STDMETHODCALLTYPE *Place_QI)(IOleInPlaceSite *, REFIID, void **);
typedef ULONG   (STDMETHODCALLTYPE *Place_Ref)(IOleInPlaceSite *);
typedef HRESULT (STDMETHODCALLTYPE *Place_GetWindow)(IOleInPlaceSite *, HWND *);
typedef HRESULT (STDMETHODCALLTYPE *Place_BOOL)(IOleInPlaceSite *, BOOL);
typedef HRESULT (STDMETHODCALLTYPE *Place_Void)(IOleInPlaceSite *);
typedef HRESULT (STDMETHODCALLTYPE *Place_GetWindowContext)(IOleInPlaceSite *,
        IOleInPlaceFrame **, IOleInPlaceUIWindow **, LPRECT, LPRECT, LPOLEINPLACEFRAMEINFO);
typedef HRESULT (STDMETHODCALLTYPE *Place_OnPosRectChange)(IOleInPlaceSite *, LPCRECT);
typedef HRESULT (STDMETHODCALLTYPE *Place_Scroll)(IOleInPlaceSite *, SIZE);

/* The Scroll slot takes SIZE BY VALUE, and Watcom's C++ front end emits W602
 * ("class value used ... in converted function type") for it -- its own COM
 * bookkeeping, not anything unsafe here: place_Scroll is declared with
 * exactly the signature oleidl.h gives the slot. The build runs -wx, so a
 * warning is an error, and the fix is one scoped flag in win/Makefile
 * (-wcd=602 on htmlview.o only) rather than anything clever in this file.
 *
 * It was briefly something clever. An earlier version left this slot 0 and
 * memcpy'd the pointer's bytes in at runtime from the constructor, on the
 * belief that -wcd=602 did not work. It does. That version left a real
 * vtable with a null entry in it between static init and the first
 * rc_html_impl, which is a worse thing to own than a suppressed warning. */
static IOleInPlaceSiteVtbl g_place_vtbl = {
    (Place_QI)place_QueryInterface, (Place_Ref)place_AddRef, (Place_Ref)place_Release,
    (Place_GetWindow)place_GetWindow, (Place_BOOL)place_ContextSensitiveHelp,
    (Place_Void)place_CanInPlaceActivate, (Place_Void)place_OnInPlaceActivate,
    (Place_Void)place_OnUIActivate, (Place_GetWindowContext)place_GetWindowContext,
    (Place_Scroll)place_Scroll, (Place_BOOL)place_OnUIDeactivate,
    (Place_Void)place_OnInPlaceDeactivate, (Place_Void)place_DiscardUndoState,
    (Place_Void)place_DeactivateAndUndo, (Place_OnPosRectChange)place_OnPosRectChange
};

/* ----------------------------------------------------------------------- */

/* The state behind the pimpl. See htmlview.h for why it is hidden -- the
 * short version is that ole2.h/mshtml.h/docobj.h together are a few thousand
 * lines and main.cpp, the only caller, needs none of them. */
struct rc_html_impl {
    Host                    host;
    ComPtr<IOleObject>      obj;
    ComPtr<IWebBrowser2>    web;
    ComPtr<IHTMLDocument2>  doc;
    ComPtr<IHTMLElement>    slot_t;     /* finished blocks */
    ComPtr<IHTMLElement>    slot_l;     /* the block being written */
    HWND           parent;
    int            ready;
    char          *queue;       /* appends that arrived before the document */
    unsigned       queue_len, queue_cap;
    char           path[MAX_PATH];
    int            diag;
    int            autoscroll;  /* the user's setting, finally consulted */

    explicit rc_html_impl(HWND p)
        : parent(p), ready(0), queue(0), queue_len(0), queue_cap(0), diag(0),
          /* On unless the user says otherwise -- the same default
           * rc_config_load uses. The struct is zero-filled, so without this
           * the setting would be off by accident on the very first run. */
          autoscroll(1)
    {
        path[0] = '\0';
        memset(&host, 0, sizeof(Host));
        host.site.lpVtbl  = &g_site_vtbl;
        host.place.lpVtbl = &g_place_vtbl;
        host.frame.lpVtbl = &g_frame_vtbl;
        host.refs = 1;
        host.hwnd = p;
    }

    ~rc_html_impl()
    {
        if (obj.valid()) {
            IOleObject_Close(obj.get(), OLECLOSE_NOSAVE);
            IOleObject_SetClientSite(obj.get(), NULL);
        }
        if (queue) HeapFree(GetProcessHeap(), 0, queue);
        /* obj/web/doc/slot_t/slot_l release themselves here, in reverse
         * declaration order, as the ComPtr members go out of scope. */
    }
};

/* CP1252 in, UTF-16 out. The bridge folds everything to CP1252 and the
 * document says so in its own meta tag, so naming the codepage explicitly
 * keeps the two agreeing even if the guest's ANSI codepage is not 1252. */
static BSTR to_bstr(const char *s)
{
    int n;
    WCHAR *w;
    BSTR b;

    if (!s) s = "";
    n = MultiByteToWideChar(1252, 0, s, -1, NULL, 0);
    if (n <= 0) return SysAllocString(L"");
    w = (WCHAR *)HeapAlloc(GetProcessHeap(), 0, (unsigned)n * sizeof(WCHAR));
    if (!w) return SysAllocString(L"");
    MultiByteToWideChar(1252, 0, s, -1, w, n);
    b = SysAllocString(w);
    HeapFree(GetProcessHeap(), 0, w);
    return b;
}

static IHTMLElement *find_id(IHTMLDocument2 *doc, const char *id)
{
    ComPtr<IHTMLElementCollection> all;
    ComPtr<IDispatch> disp;
    ComPtr<IHTMLElement> el;
    VARIANT name, index;
    Bstr idb;

    {
        IHTMLElementCollection *all_raw = NULL;
        if (FAILED(IHTMLDocument2_get_all(doc, &all_raw)) || !all_raw)
            return NULL;
        all.reset(all_raw);
    }
    VariantInit(&name);
    VariantInit(&index);
    /* V_VT / V_BSTR rather than the struct fields: Open Watcom declares
     * VARIANT with named unions, so v.vt does not compile. The macros are the
     * portable spelling and work either way. */
    V_VT(&name) = VT_BSTR;
    idb.reset(to_bstr(id));
    V_BSTR(&name) = idb.get();
    V_VT(&index) = VT_I4;
    V_I4(&index) = 0;
    {
        IDispatch *disp_raw = NULL;
        if (SUCCEEDED(IHTMLElementCollection_item(all.get(), name, index, &disp_raw))
            && disp_raw) {
            disp.reset(disp_raw);
            IHTMLElement *el_raw = NULL;
            IDispatch_QueryInterface(disp.get(), IID_IHTMLElement, (void **)&el_raw);
            el.reset(el_raw);
        }
    }
    return el.release();
}

/* Fragments that arrive before the document is ready buffer here. This used
 * to double with no ceiling -- the one allocation in the guest with no cap,
 * on a machine that measures free memory in KB. The document normally becomes
 * ready in under a second, so today this rarely holds more than a notice or
 * two, but Phase 6's inline-image path appends fragments large enough to make
 * "rarely" not a real bound. Capped here, and an overflow drops the fragment
 * with a loud log line rather than growing forever -- the same drop-and-log
 * shape manifest_add (win/sync/syncrpc.cpp) uses for its own bounded buffer.
 * See audit finding 04.29. */
#define RC_HTML_QUEUE_CAP (64u * 1024u)

static void queue_add(rc_html_impl *d, const char *frag)
{
    unsigned n = (unsigned)strlen(frag);
    unsigned need = d->queue_len + n + 1;

    if (need > RC_HTML_QUEUE_CAP) {
        rc_log("html: pre-ready queue would exceed %u bytes (have %u, this "
               "fragment %u); dropping it",
               (unsigned)RC_HTML_QUEUE_CAP, d->queue_len, n);
        return;
    }
    if (need > d->queue_cap) {
        unsigned cap = d->queue_cap ? d->queue_cap * 2 : 4096;
        char *p;
        while (cap < need) cap *= 2;
        if (cap > RC_HTML_QUEUE_CAP) cap = RC_HTML_QUEUE_CAP;
        p = d->queue ? (char *)HeapReAlloc(GetProcessHeap(), 0, d->queue, cap)
                     : (char *)HeapAlloc(GetProcessHeap(), 0, cap);
        /* HeapReAlloc failing leaves the original block valid; dropping the
         * fragment loses a line of transcript, which beats leaking. Now also
         * logged -- it was silent before, on a platform where the log is the
         * only diagnostic. */
        if (!p) {
            rc_log("html: pre-ready queue realloc to %u bytes failed; "
                   "dropping this fragment", cap);
            return;
        }
        d->queue = p;
        d->queue_cap = cap;
    }
    memcpy(d->queue + d->queue_len, frag, n);
    d->queue_len += n;
    d->queue[d->queue_len] = '\0';
}

rc_html *rc_html::create(HWND parent)
{
    rc_html_impl *v = new rc_html_impl(parent);
    if (!v) return NULL;
    rc_html *h = new rc_html(v);
    if (!h) { delete v; return NULL; }
    return h;
}

rc_html::~rc_html()
{
    delete d;
}

void rc_html::set_document(const char *doc)
{
    RECT rc;
    HRESULT hr;
    FILE *f;
    VARIANT empty;
    char dir[MAX_PATH];

    if (d->obj.valid()) return;

    /* A file rather than document.write: writing needs the document to exist
     * first, which means waiting for a navigation anyway, and this way there
     * is exactly one asynchronous step to wait on instead of two. */
    GetModuleFileNameA(NULL, dir, sizeof(dir) - 1);
    { char *slash = strrchr(dir, '\\'); if (slash) *slash = '\0'; }
    rc_strf(d->path, sizeof(d->path), "%s\\view.htm", dir);
    f = fopen(d->path, "wb");
    if (!f) { rc_log("html: cannot write %s", d->path); return; }
    fputs(doc, f);
    fclose(f);

    hr = CoCreateInstance(CLSID_WebBrowser, NULL, CLSCTX_INPROC_SERVER,
                          IID_IOleObject, (void **)d->obj.addr());
    if (FAILED(hr)) { rc_log("html: CoCreateInstance %08lX", (unsigned long)hr); return; }

    IOleObject_SetClientSite(d->obj.get(), &d->host.site);
    OleSetContainedObject((IUnknown *)d->obj.get(), TRUE);

    GetClientRect(d->parent, &rc);
    hr = IOleObject_DoVerb(d->obj.get(), OLEIVERB_INPLACEACTIVATE, NULL,
                           &d->host.site, 0, d->parent, &rc);
    if (FAILED(hr)) { rc_log("html: DoVerb %08lX", (unsigned long)hr); return; }

    hr = IOleObject_QueryInterface(d->obj.get(), IID_IWebBrowser2,
                                   (void **)d->web.addr());
    if (FAILED(hr)) { rc_log("html: QI IWebBrowser2 %08lX", (unsigned long)hr); return; }

    {
        char url8[MAX_PATH + 16];
        char *p;
        Bstr url;
        rc_strf(url8, sizeof(url8), "file:///%s", d->path);
        for (p = url8; *p; p++) if (*p == '\\') *p = '/';
        url.reset(to_bstr(url8));
        VariantInit(&empty);
        IWebBrowser2_Navigate(d->web.get(), url.get(), &empty, &empty, &empty, &empty);
    }
    rc_log("html: navigating to %s", d->path);

    {   /* Where did the control actually put its window? A control that is
         * hosting correctly but sized to nothing looks exactly like one that
         * is not hosting at all. */
        ComPtr<IOleWindow> ow;
        HWND h = NULL;
        {
            IOleWindow *ow_raw = NULL;
            if (SUCCEEDED(IOleObject_QueryInterface(d->obj.get(), IID_IOleWindow,
                                                    (void **)&ow_raw)) && ow_raw) {
                ow.reset(ow_raw);
                IOleWindow_GetWindow(ow.get(), &h);
            }
        }
        if (h) {
            RECT r;
            GetWindowRect(h, &r);
            rc_log("html: control hwnd=%p vis=%d rect=%ld,%ld %ldx%ld",
                   (void *)h, IsWindowVisible(h) ? 1 : 0,
                   (long)r.left, (long)r.top,
                   (long)(r.right - r.left), (long)(r.bottom - r.top));
        } else {
            rc_log("html: control has no window");
        }
    }
}

int rc_html::poll()
{
    READYSTATE rs = READYSTATE_UNINITIALIZED;

    if (d->ready) return d->ready;
    if (!d->web.valid()) return 0;
    if (FAILED(IWebBrowser2_get_ReadyState(d->web.get(), &rs))) return 0;
    if (rs != READYSTATE_COMPLETE) return 0;

    {
        ComPtr<IDispatch> disp;
        IDispatch *disp_raw = NULL;
        if (FAILED(IWebBrowser2_get_Document(d->web.get(), &disp_raw)) || !disp_raw)
            return 0;
        disp.reset(disp_raw);
        IHTMLDocument2 *doc_raw = NULL;
        IDispatch_QueryInterface(disp.get(), IID_IHTMLDocument2, (void **)&doc_raw);
        d->doc.reset(doc_raw);
    }
    if (!d->doc.valid()) return 0;

    d->slot_t.reset(find_id(d->doc.get(), "t"));
    d->slot_l.reset(find_id(d->doc.get(), "l"));
    if (!d->slot_t.valid() || !d->slot_l.valid()) {
        rc_log("html: document has no #t/#l containers");
        return 0;
    }
    d->ready = 1;
    rc_log("html: document ready");

    if (d->queue_len) {
        append(d->queue);
        d->queue_len = 0;
        d->queue[0] = '\0';
    }
    return 1;
}

/* --- the transcript's own vocabulary -------------------------------------
 *
 * Escaping lives here because it has to happen on EVERY path into the
 * document, and leaving it to the caller meant two paths did not do it.
 * `<` in a bridge error message or a hostname ended the document early. */
static void esc_into(const char *in, char *out, unsigned cap)
{
    unsigned o = 0;

    /* The 8-byte margin is the longest replacement plus its terminator, so a
     * character is never half-written. Truncation is silent by necessity --
     * this is the transcript, and refusing to show a long line would be worse
     * than showing most of it -- but the CALLERS size their buffers from the
     * input rather than guessing. */
    for (; *in && o + 8 < cap; in++) {
        const char *rep = NULL;
        if (*in == '&')       rep = "&amp;";
        else if (*in == '<')  rep = "&lt;";
        else if (*in == '>')  rep = "&gt;";
        else if (*in == '"')  rep = "&quot;";
        else if (*in == '\n') rep = "<br>";
        else if (*in == '\r') continue;
        if (rep) {
            unsigned n = (unsigned)strlen(rep);
            memcpy(out + o, rep, n);
            o += n;
        } else {
            out[o++] = *in;
        }
    }
    out[o] = '\0';
}

/* One policy, one place. Was: two call sites scrolled unconditionally, one
 * scrolled if already at the bottom, six never scrolled, and the user's
 * autoscroll setting was read by nothing. */
void rc_html::settle(int was_at_bottom)
{
    if (d->autoscroll && was_at_bottom) scroll_bottom();
}

/* Escape `text` into a heap block wrapped in `<div class="cls">`, append it,
 * and apply the scroll policy. Heap rather than stack because escaping can
 * expand five-fold and the largest frame in this program is already in
 * main.c. */
void rc_html::say(const char *cls, const char *text)
{
    unsigned n, cap;
    int bottom;

    if (!text) return;
    n = (unsigned)strlen(text);
    cap = n * 6 + 64;                       /* '&' -> "&amp;" is the worst */
    HeapPtr<char> esc((char *)HeapAlloc(GetProcessHeap(), 0, cap));
    if (!esc.valid()) return;
    esc_into(text, esc.get(), cap);

    HeapPtr<char> div((char *)HeapAlloc(GetProcessHeap(), 0, cap + 64));
    if (!div.valid()) return;
    rc_strf(div.get(), cap + 64, "<div class=\"%s\">%s</div>", cls, esc.get());

    bottom = at_bottom();
    append(div.get());
    settle(bottom);
}

void rc_html::notice(const char *text)
{ say(RC_CSS_NOTICE, text); }
void rc_html::error(const char *text)
{ say(RC_CSS_ERROR, text); }
void rc_html::user(const char *text)
{ say(RC_CSS_USER, text); }

void rc_html::block(const char *html)
{
    int bottom;
    if (!html || !*html) return;
    bottom = at_bottom();
    append(html);
    settle(bottom);
}

void rc_html::set_autoscroll(int on) { d->autoscroll = on; }

void rc_html::append(const char *frag)
{
    HRESULT hr;

    if (!frag || !*frag) return;
    if (!d->ready) { queue_add(d, frag); return; }
    Bstr where(to_bstr("beforeEnd"));
    Bstr html(to_bstr(frag));
    hr = IHTMLElement_insertAdjacentHTML(d->slot_t.get(), where.get(), html.get());

    /* Quiet unless something goes wrong. A failed insert is invisible by
     * definition: the call returns, nothing appears, and the window looks
     * like a layout problem rather than a DOM one. */
    if (FAILED(hr))
        rc_log("html: append FAILED hr=%08lX len=%u [%.48s]",
               (unsigned long)hr, (unsigned)strlen(frag), frag);
    else if (!d->diag) {
        d->diag = 1;
        rc_log("html: first append ok, %u bytes", (unsigned)strlen(frag));
    }
}

void rc_html::live(const char *frag)
{
    if (!d->ready) return;
    Bstr html(to_bstr(frag ? frag : ""));
    {
        HRESULT hr = IHTMLElement_put_innerHTML(d->slot_l.get(), html.get());
        if (FAILED(hr))
            rc_log("html: live FAILED hr=%08lX", (unsigned long)hr);
    }
}

void rc_html::clear()
{
    d->queue_len = 0;
    if (d->queue) d->queue[0] = '\0';
    if (!d->ready) return;
    Bstr empty(to_bstr(""));
    IHTMLElement_put_innerHTML(d->slot_t.get(), empty.get());
    IHTMLElement_put_innerHTML(d->slot_l.get(), empty.get());
}

void rc_html::scroll_bottom()
{
    if (!d->ready) return;
    /* scrollBy with an absurd delta rather than computing the document height:
     * the window clamps, which is exactly the behaviour wanted and cannot
     * overshoot -- the mistake the RichEdit version made with SB_BOTTOM. */
    ComPtr<IHTMLWindow2> win;
    IHTMLWindow2 *win_raw = NULL;
    if (SUCCEEDED(IHTMLDocument2_get_parentWindow(d->doc.get(), &win_raw)) && win_raw) {
        win.reset(win_raw);
        IHTMLWindow2_scrollBy(win.get(), 0, 1000000);
    }
}

int rc_html::at_bottom() const
{
    long top = 0, height = 0, client = 0;
    int at = 1;

    if (!d->ready) return 1;
    ComPtr<IHTMLElement> body;
    {
        IHTMLElement *body_raw = NULL;
        if (FAILED(IHTMLDocument2_get_body(d->doc.get(), &body_raw)) || !body_raw)
            return 1;
        body.reset(body_raw);
    }
    ComPtr<IHTMLElement2> b2;
    {
        IHTMLElement2 *b2_raw = NULL;
        if (SUCCEEDED(IHTMLElement_QueryInterface(body.get(), IID_IHTMLElement2,
                                                  (void **)&b2_raw)) && b2_raw) {
            b2.reset(b2_raw);
            IHTMLElement2_get_scrollTop(b2.get(), &top);
            IHTMLElement2_get_scrollHeight(b2.get(), &height);
            IHTMLElement2_get_clientHeight(b2.get(), &client);
            /* Within a couple of lines of the end counts as "at the bottom", so
             * autoscroll does not stop following because of a rounding pixel. */
            at = (top + client >= height - 24);
        }
    }
    return at;
}

void rc_html::move(int x, int y, int w, int h)
{
    RECT rc;

    if (!d->obj.valid()) return;
    rc.left = x; rc.top = y; rc.right = x + w; rc.bottom = y + h;
    ComPtr<IOleInPlaceObject> ip;
    IOleInPlaceObject *ip_raw = NULL;
    if (SUCCEEDED(IOleObject_QueryInterface(d->obj.get(), IID_IOleInPlaceObject,
                                            (void **)&ip_raw)) && ip_raw) {
        ip.reset(ip_raw);
        IOleInPlaceObject_SetObjectRects(ip.get(), &rc, &rc);
    }
}
