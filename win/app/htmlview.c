/* htmlview.c -- the transcript, hosted in MSHTML. See htmlview.h.
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
#include <mshtml.h>
#include <docobj.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "app/htmlview.h"
#include "core/log.h"
#include "core/str.h"

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
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IOleClientSite))
        *out = &h->site;
    else if (IsEqualIID(riid, &IID_IOleInPlaceSite))
        *out = &h->place;
    else if (IsEqualIID(riid, &IID_IOleInPlaceFrame)
             || IsEqualIID(riid, &IID_IOleInPlaceUIWindow)
             || IsEqualIID(riid, &IID_IOleWindow))
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

static IOleClientSiteVtbl g_site_vtbl = {
    (void *)site_QueryInterface, (void *)site_AddRef, (void *)site_Release,
    (void *)site_SaveObject, (void *)site_GetMoniker, (void *)site_GetContainer,
    (void *)site_ShowObject, (void *)site_OnShowWindow,
    (void *)site_RequestNewObjectLayout
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

static IOleInPlaceFrameVtbl g_frame_vtbl = {
    (void *)frame_QueryInterface, (void *)frame_AddRef, (void *)frame_Release,
    (void *)frame_GetWindow, (void *)frame_ContextSensitiveHelp,
    (void *)frame_GetBorder, (void *)frame_RequestBorderSpace,
    (void *)frame_SetBorderSpace, (void *)frame_SetActiveObject,
    (void *)frame_InsertMenus, (void *)frame_SetMenu, (void *)frame_RemoveMenus,
    (void *)frame_SetStatusText, (void *)frame_EnableModeless,
    (void *)frame_TranslateAccelerator
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

static IOleInPlaceSiteVtbl g_place_vtbl = {
    (void *)place_QueryInterface, (void *)place_AddRef, (void *)place_Release,
    (void *)place_GetWindow, (void *)place_ContextSensitiveHelp,
    (void *)place_CanInPlaceActivate, (void *)place_OnInPlaceActivate,
    (void *)place_OnUIActivate, (void *)place_GetWindowContext,
    (void *)place_Scroll, (void *)place_OnUIDeactivate,
    (void *)place_OnInPlaceDeactivate, (void *)place_DiscardUndoState,
    (void *)place_DeactivateAndUndo, (void *)place_OnPosRectChange
};


/* ----------------------------------------------------------------------- */

struct rc_html {
    Host           host;
    IOleObject    *obj;
    IWebBrowser2  *web;
    IHTMLDocument2 *doc;
    IHTMLElement  *slot_t;      /* finished blocks */
    IHTMLElement  *slot_l;      /* the block being written */
    HWND           parent;
    int            ready;
    char          *queue;       /* appends that arrived before the document */
    unsigned       queue_len, queue_cap;
    char           path[MAX_PATH];
    int            diag;
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
    IHTMLElementCollection *all = NULL;
    IDispatch *disp = NULL;
    IHTMLElement *el = NULL;
    VARIANT name, index;

    if (FAILED(IHTMLDocument2_get_all(doc, &all)) || !all) return NULL;
    VariantInit(&name);
    VariantInit(&index);
    /* V_VT / V_BSTR rather than the struct fields: Open Watcom declares
     * VARIANT with named unions, so v.vt does not compile. The macros are the
     * portable spelling and work either way. */
    V_VT(&name) = VT_BSTR;
    V_BSTR(&name) = to_bstr(id);
    V_VT(&index) = VT_I4;
    V_I4(&index) = 0;
    if (SUCCEEDED(IHTMLElementCollection_item(all, name, index, &disp)) && disp)
        IDispatch_QueryInterface(disp, &IID_IHTMLElement, (void **)&el);
    if (disp) IDispatch_Release(disp);
    SysFreeString(V_BSTR(&name));
    IHTMLElementCollection_Release(all);
    return el;
}

static void queue_add(rc_html *v, const char *frag)
{
    unsigned n = (unsigned)strlen(frag);
    if (v->queue_len + n + 1 > v->queue_cap) {
        unsigned cap = v->queue_cap ? v->queue_cap * 2 : 4096;
        char *p;
        while (cap < v->queue_len + n + 1) cap *= 2;
        p = v->queue ? (char *)HeapReAlloc(GetProcessHeap(), 0, v->queue, cap)
                     : (char *)HeapAlloc(GetProcessHeap(), 0, cap);
        /* HeapReAlloc failing leaves the original block valid; dropping the
         * fragment loses a line of transcript, which beats leaking. */
        if (!p) return;
        v->queue = p;
        v->queue_cap = cap;
    }
    memcpy(v->queue + v->queue_len, frag, n);
    v->queue_len += n;
    v->queue[v->queue_len] = '\0';
}

rc_html *rc_html_create(HWND parent)
{
    rc_html *v = (rc_html *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                                      sizeof(rc_html));
    if (!v) return NULL;
    v->parent = parent;
    v->host.site.lpVtbl  = &g_site_vtbl;
    v->host.place.lpVtbl = &g_place_vtbl;
    v->host.frame.lpVtbl = &g_frame_vtbl;
    v->host.refs = 1;
    v->host.hwnd = parent;
    return v;
}

void rc_html_set_document(rc_html *v, const char *doc)
{
    RECT rc;
    HRESULT hr;
    FILE *f;
    VARIANT empty;
    BSTR url;
    char dir[MAX_PATH];

    if (!v || v->obj) return;

    /* A file rather than document.write: writing needs the document to exist
     * first, which means waiting for a navigation anyway, and this way there
     * is exactly one asynchronous step to wait on instead of two. */
    GetModuleFileNameA(NULL, dir, sizeof(dir) - 1);
    { char *slash = strrchr(dir, '\\'); if (slash) *slash = '\0'; }
    rc_strf(v->path, sizeof(v->path), "%s\\view.htm", dir);
    f = fopen(v->path, "wb");
    if (!f) { rc_log("html: cannot write %s", v->path); return; }
    fputs(doc, f);
    fclose(f);

    hr = CoCreateInstance(&CLSID_WebBrowser, NULL, CLSCTX_INPROC_SERVER,
                          &IID_IOleObject, (void **)&v->obj);
    if (FAILED(hr)) { rc_log("html: CoCreateInstance %08lX", (unsigned long)hr); return; }

    IOleObject_SetClientSite(v->obj, &v->host.site);
    OleSetContainedObject((IUnknown *)v->obj, TRUE);

    GetClientRect(v->parent, &rc);
    hr = IOleObject_DoVerb(v->obj, OLEIVERB_INPLACEACTIVATE, NULL,
                           &v->host.site, 0, v->parent, &rc);
    if (FAILED(hr)) { rc_log("html: DoVerb %08lX", (unsigned long)hr); return; }

    hr = IOleObject_QueryInterface(v->obj, &IID_IWebBrowser2, (void **)&v->web);
    if (FAILED(hr)) { rc_log("html: QI IWebBrowser2 %08lX", (unsigned long)hr); return; }

    {
        char url8[MAX_PATH + 16];
        char *p;
        rc_strf(url8, sizeof(url8), "file:///%s", v->path);
        for (p = url8; *p; p++) if (*p == '\\') *p = '/';
        url = to_bstr(url8);
    }
    VariantInit(&empty);
    IWebBrowser2_Navigate(v->web, url, &empty, &empty, &empty, &empty);
    SysFreeString(url);
    rc_log("html: navigating to %s", v->path);

    {   /* Where did the control actually put its window? A control that is
         * hosting correctly but sized to nothing looks exactly like one that
         * is not hosting at all. */
        IOleWindow *ow = NULL;
        HWND h = NULL;
        if (SUCCEEDED(IOleObject_QueryInterface(v->obj, &IID_IOleWindow,
                                                (void **)&ow)) && ow) {
            IOleWindow_GetWindow(ow, &h);
            IOleWindow_Release(ow);
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

int rc_html_ready(rc_html *v) { return v && v->ready; }

int rc_html_poll(rc_html *v)
{
    READYSTATE rs = READYSTATE_UNINITIALIZED;
    IDispatch *disp = NULL;

    if (!v || v->ready) return v ? v->ready : 0;
    if (!v->web) return 0;
    if (FAILED(IWebBrowser2_get_ReadyState(v->web, &rs))) return 0;
    if (rs != READYSTATE_COMPLETE) return 0;

    if (FAILED(IWebBrowser2_get_Document(v->web, &disp)) || !disp) return 0;
    IDispatch_QueryInterface(disp, &IID_IHTMLDocument2, (void **)&v->doc);
    IDispatch_Release(disp);
    if (!v->doc) return 0;

    v->slot_t = find_id(v->doc, "t");
    v->slot_l = find_id(v->doc, "l");
    if (!v->slot_t || !v->slot_l) {
        rc_log("html: document has no #t/#l containers");
        return 0;
    }
    v->ready = 1;
    rc_log("html: document ready");

    if (v->queue_len) {
        rc_html_append(v, v->queue);
        v->queue_len = 0;
        v->queue[0] = '\0';
    }
    return 1;
}

void rc_html_append(rc_html *v, const char *frag)
{
    BSTR where, html;
    HRESULT hr;

    if (!v || !frag || !*frag) return;
    if (!v->ready) { queue_add(v, frag); return; }
    where = to_bstr("beforeEnd");
    html = to_bstr(frag);
    hr = IHTMLElement_insertAdjacentHTML(v->slot_t, where, html);
    SysFreeString(html);
    SysFreeString(where);

    /* Quiet unless something goes wrong. A failed insert is invisible by
     * definition: the call returns, nothing appears, and the window looks
     * like a layout problem rather than a DOM one. */
    if (FAILED(hr))
        rc_log("html: append FAILED hr=%08lX len=%u [%.48s]",
               (unsigned long)hr, (unsigned)strlen(frag), frag);
    else if (!v->diag) {
        v->diag = 1;
        rc_log("html: first append ok, %u bytes", (unsigned)strlen(frag));
    }
}

void rc_html_live(rc_html *v, const char *frag)
{
    BSTR html;

    if (!v || !v->ready) return;
    html = to_bstr(frag ? frag : "");
    {
        HRESULT hr = IHTMLElement_put_innerHTML(v->slot_l, html);
        if (FAILED(hr))
            rc_log("html: live FAILED hr=%08lX", (unsigned long)hr);
    }
    SysFreeString(html);
}

void rc_html_clear(rc_html *v)
{
    BSTR empty;

    if (!v) return;
    v->queue_len = 0;
    if (v->queue) v->queue[0] = '\0';
    if (!v->ready) return;
    empty = to_bstr("");
    IHTMLElement_put_innerHTML(v->slot_t, empty);
    IHTMLElement_put_innerHTML(v->slot_l, empty);
    SysFreeString(empty);
}

void rc_html_scroll_bottom(rc_html *v)
{
    IHTMLWindow2 *win = NULL;

    if (!v || !v->ready) return;
    /* scrollBy with an absurd delta rather than computing the document height:
     * the window clamps, which is exactly the behaviour wanted and cannot
     * overshoot -- the mistake the RichEdit version made with SB_BOTTOM. */
    if (SUCCEEDED(IHTMLDocument2_get_parentWindow(v->doc, &win)) && win) {
        IHTMLWindow2_scrollBy(win, 0, 1000000);
        IHTMLWindow2_Release(win);
    }
}

int rc_html_at_bottom(rc_html *v)
{
    IHTMLElement2 *b2 = NULL;
    IHTMLElement *body = NULL;
    long top = 0, height = 0, client = 0;
    int at = 1;

    if (!v || !v->ready) return 1;
    if (FAILED(IHTMLDocument2_get_body(v->doc, &body)) || !body) return 1;
    if (SUCCEEDED(IHTMLElement_QueryInterface(body, &IID_IHTMLElement2,
                                              (void **)&b2)) && b2) {
        IHTMLElement2_get_scrollTop(b2, &top);
        IHTMLElement2_get_scrollHeight(b2, &height);
        IHTMLElement2_get_clientHeight(b2, &client);
        /* Within a couple of lines of the end counts as "at the bottom", so
         * autoscroll does not stop following because of a rounding pixel. */
        at = (top + client >= height - 24);
        IHTMLElement2_Release(b2);
    }
    IHTMLElement_Release(body);
    return at;
}

void rc_html_move(rc_html *v, int x, int y, int w, int h)
{
    IOleInPlaceObject *ip = NULL;
    RECT rc;

    if (!v || !v->obj) return;
    rc.left = x; rc.top = y; rc.right = x + w; rc.bottom = y + h;
    if (SUCCEEDED(IOleObject_QueryInterface(v->obj, &IID_IOleInPlaceObject,
                                            (void **)&ip)) && ip) {
        IOleInPlaceObject_SetObjectRects(ip, &rc, &rc);
        IOleInPlaceObject_Release(ip);
    }
}

void rc_html_destroy(rc_html *v)
{
    if (!v) return;
    if (v->slot_t) IHTMLElement_Release(v->slot_t);
    if (v->slot_l) IHTMLElement_Release(v->slot_l);
    if (v->doc) IHTMLDocument2_Release(v->doc);
    if (v->web) IWebBrowser2_Release(v->web);
    if (v->obj) {
        IOleObject_Close(v->obj, OLECLOSE_NOSAVE);
        IOleObject_SetClientSite(v->obj, NULL);
        IOleObject_Release(v->obj);
    }
    if (v->queue) HeapFree(GetProcessHeap(), 0, v->queue);
    HeapFree(GetProcessHeap(), 0, v);
}
