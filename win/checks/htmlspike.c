/* htmlspike.c -- can we host the HTML engine on Win98, from Open Watcom C?
 *
 * The memory objection to an HTML transcript is already dead: measured on the
 * 32MB guest, mshtml.dll plus an HTMLDocument plus a WebBrowser cost 512 KB
 * total, because the shell already hosts MSHTML for folder Web View and a
 * second host pays only for its own instance data.
 *
 * What is NOT settled is whether a minimal host written in plain C89, with no
 * ATL and no C++, actually activates and paints on this platform. That is what
 * this spike is for. It does the smallest useful thing: host the control, point
 * it at a local file, and show it. If this paints, the transcript can move to
 * HTML and get real typography -- padding, borders, background colours,
 * tables, and eventually inline screenshots.
 *
 * The host implements the four interfaces the control actually asks for. Each
 * is a separate embedded struct with its own vtable pointer, recovered back to
 * the Host with a fixed offset, which is the standard way to do COM from C.
 */
#define CINTERFACE
#define COBJMACROS
#include <windows.h>
#include <ole2.h>
#include <exdisp.h>
#include <mshtml.h>
#include <docobj.h>
#include <stdio.h>
#include <stddef.h>
#include <string.h>

static FILE *g_log;
static void say(const char *fmt, ...)
{
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
    va_end(ap);
    buf[sizeof(buf) - 1] = '\0';
    if (g_log) { fputs(buf, g_log); fputs("\n", g_log); fflush(g_log); }
}

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
static Host        g_host;
static IOleObject *g_obj;
static IWebBrowser2 *g_web;

static int html_create(HWND parent)
{
    RECT rc;
    HRESULT hr;

    g_host.site.lpVtbl  = &g_site_vtbl;
    g_host.place.lpVtbl = &g_place_vtbl;
    g_host.frame.lpVtbl = &g_frame_vtbl;
    g_host.refs = 1;
    g_host.hwnd = parent;

    hr = CoCreateInstance(&CLSID_WebBrowser, NULL, CLSCTX_INPROC_SERVER,
                          &IID_IOleObject, (void **)&g_obj);
    if (FAILED(hr)) { say("CoCreateInstance failed %08lX", (unsigned long)hr); return -1; }

    hr = IOleObject_SetClientSite(g_obj, &g_host.site);
    say("SetClientSite %08lX", (unsigned long)hr);
    OleSetContainedObject((IUnknown *)g_obj, TRUE);

    GetClientRect(parent, &rc);
    hr = IOleObject_DoVerb(g_obj, OLEIVERB_INPLACEACTIVATE, NULL,
                           &g_host.site, 0, parent, &rc);
    if (FAILED(hr)) { say("DoVerb failed %08lX", (unsigned long)hr); return -2; }
    say("DoVerb ok");

    hr = IOleObject_QueryInterface(g_obj, &IID_IWebBrowser2, (void **)&g_web);
    if (FAILED(hr)) { say("QI IWebBrowser2 failed %08lX", (unsigned long)hr); return -3; }
    say("IWebBrowser2 ok");
    return 0;
}

static void html_go(const char *url)
{
    VARIANT v;
    BSTR b;
    WCHAR wide[512];
    int n = MultiByteToWideChar(CP_ACP, 0, url, -1, wide, 511);
    wide[n ? n - 1 : 0] = 0;
    b = SysAllocString(wide);
    VariantInit(&v);
    IWebBrowser2_Navigate(g_web, b, &v, &v, &v, &v);
    SysFreeString(b);
    say("navigated to %s", url);
}

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_SIZE && g_obj) {
        RECT rc;
        IOleInPlaceObject *ip = NULL;
        GetClientRect(h, &rc);
        if (SUCCEEDED(IOleObject_QueryInterface(g_obj, &IID_IOleInPlaceObject,
                                                (void **)&ip)) && ip) {
            IOleInPlaceObject_SetObjectRects(ip, &rc, &rc);
            IOleInPlaceObject_Release(ip);
        }
        return 0;
    }
    if (m == WM_DESTROY) { PostQuitMessage(0); return 0; }
    return DefWindowProcA(h, m, w, l);
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASSA wc;
    HWND hwnd;
    MSG msg;
    FILE *f;

    (void)prev; (void)cmd;
    g_log = fopen("HTMLSPIKE.LOG", "w");
    say("=== html spike ===");

    /* A file rather than document.write, so this proves rendering without
     * also depending on navigation-complete timing. */
    f = fopen("C:\\GASTON\\SPIKE.HTM", "wb");
    if (f) {
        fputs("<html><head><style>\r\n"
              "body{font:8pt 'MS Sans Serif';margin:8px;background:#fff;color:#000}\r\n"
              "h2{font-size:10pt;margin:0 0 6px 0;border-bottom:1px solid #808080}\r\n"
              ".you{background:#e8eef7;border-left:3px solid #000080;"
              "padding:4px 6px;margin:8px 0;font-weight:bold;color:#000080}\r\n"
              "pre{background:#f4f4f4;border:1px solid #c0c0c0;padding:5px;"
              "font:9pt 'Courier New';color:#000080;margin:6px 0}\r\n"
              "li{margin:2px 0}\r\n"
              "</style></head><body>\r\n"
              "<h2>Gaston Code</h2>\r\n"
              "<div class=you>&gt; does MSHTML host on Win98 from C?</div>\r\n"
              "<p>If you can read this with a <b>bold</b> word, a tinted quote"
              " box above and a bordered code block below, then yes.</p>\r\n"
              "<ul><li>padding and margins</li><li>borders</li>"
              "<li>background colours</li></ul>\r\n"
              "<pre>int main(void) {\r\n    return 0;\r\n}</pre>\r\n"
              "</body></html>\r\n", f);
        fclose(f);
    }

    if (FAILED(OleInitialize(NULL))) { say("OleInitialize failed"); return 1; }

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = wndproc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    wc.lpszClassName = "HtmlSpike";
    RegisterClassA(&wc);

    hwnd = CreateWindowExA(0, "HtmlSpike", "HTML spike",
                           WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
                           40, 40, 520, 380, NULL, NULL, inst, NULL);
    if (!hwnd) { say("CreateWindow failed"); return 1; }
    ShowWindow(hwnd, show);

    if (html_create(hwnd) == 0)
        html_go("file:///C:/RETRO/SPIKE.HTM");

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    say("=== done ===");
    if (g_log) fclose(g_log);
    return 0;
}
