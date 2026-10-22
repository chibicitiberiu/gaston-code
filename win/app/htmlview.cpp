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
#include <errno.h>
#include <string.h>
#include "app/htmlview.h"
#include "view/render.h"
#include "app/comptr.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

#include <string>

/* THE GUIDS ARE SPELLED OUT, not linked from uuid.lib, and that is worth
 * 31,520 bytes. Watcom ships uuid.lib as a SINGLE object module, so a
 * reference to any one GUID pulls all 1,970 of them into the image -- the
 * linker's own map marks 1,957 of them unreferenced. Measured 2026-10-11:
 * uuid.lib was the second largest module in gaston.exe and the third in
 * gremote.exe, where a single IID_IUnknown was paying for the lot.
 *
 * win/automation/msaa.cpp:25 already did this for oleacc's IID, for a
 * different reason (a toolchain whose import library lacks it cannot fail
 * the link), and research/guest/probe.c:566 does it in C. The RC_ prefix is not
 * decoration: oleidl.h, exdisp.h and mshtml.h already declare these names
 * as `EXTERN_C const IID`, so a file-scope `static const GUID` of the same
 * name is a redeclaration with different linkage and will not compile.
 *
 * The bytes were read out of the linked image rather than typed, and
 * tests/guest/cppcheck.cpp compares every one of them against uuid.lib's own
 * copy on each build -- because a wrong byte here does not fail a gate, it
 * fails CoCreateInstance on the guest and leaves no transcript at all. */
static const GUID RC_IID_IUnknown =
    { 0x00000000, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleObject =
    { 0x00000112, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleInPlaceObject =
    { 0x00000113, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleWindow =
    { 0x00000114, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleInPlaceUIWindow =
    { 0x00000115, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleInPlaceFrame =
    { 0x00000116, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleClientSite =
    { 0x00000118, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_IID_IOleInPlaceSite =
    { 0x00000119, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const GUID RC_CLSID_WebBrowser =
    { 0x8856f961, 0x340a, 0x11d0,
      { 0xa9, 0x6b, 0x00, 0xc0, 0x4f, 0xd7, 0x05, 0xa2 } };
static const GUID RC_IID_IWebBrowser2 =
    { 0xd30c1661, 0xcdaf, 0x11d0,
      { 0x8a, 0x3e, 0x00, 0xc0, 0x4f, 0xc9, 0xe2, 0x6e } };
static const GUID RC_IID_IHTMLElement =
    { 0x3050f1ff, 0x98b5, 0x11cf,
      { 0xbb, 0x82, 0x00, 0xaa, 0x00, 0xbd, 0xce, 0x0b } };
static const GUID RC_IID_IHTMLElement2 =
    { 0x3050f434, 0x98b5, 0x11cf,
      { 0xbb, 0x82, 0x00, 0xaa, 0x00, 0xbd, 0xce, 0x0b } };
static const GUID RC_IID_IHTMLDocument2 =
    { 0x332c4425, 0x26cb, 0x11d0,
      { 0xb4, 0x83, 0x00, 0xc0, 0x4f, 0xd9, 0x01, 0x19 } };

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
    if (IsEqualIID(riid, RC_IID_IUnknown) || IsEqualIID(riid, RC_IID_IOleClientSite))
        *out = &h->site;
    else if (IsEqualIID(riid, RC_IID_IOleInPlaceSite))
        *out = &h->place;
    else if (IsEqualIID(riid, RC_IID_IOleInPlaceFrame)
             || IsEqualIID(riid, RC_IID_IOleInPlaceUIWindow)
             || IsEqualIID(riid, RC_IID_IOleWindow))
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
    ComPtr<IHTMLElement>    slot_s;     /* the status line (optional) */
    HWND           parent;
    int            ready;
    std::string    queue;       /* appends that arrived before the document */
    char           path[MAX_PATH];
    int            diag;
    int            autoscroll;  /* the user's setting, finally consulted */
    /* A fingerprint of the document currently installed, so set_document can
     * tell "the bridge sent the same skeleton again" (every reconnect) from
     * "the stylesheet changed" (worth re-navigating for, and the only way a
     * CSS edit on the bridge ever reaches the guest). Length plus a rolling
     * sum: a real hash would be better and this is not a security boundary,
     * it is a did-anything-change test on a machine with 28KB free. */
    unsigned       doc_len, doc_sum;

    /* --- the live path's cost, per turn -----------------------------------
     *
     * Stage 3 moves rendering onto this machine, so "can a 1998 CPU re-render
     * the open block ten times a second" stops being rhetorical. This is the
     * instrument that answers it, and it is permanent rather than scaffolding
     * because stage 5 wants the same number off the P200.
     *
     * QueryPerformanceCounter, for the reason win/wire/connection.cpp:664-667
     * already records: GetTickCount's tick is 55ms on 9x, and the threshold
     * being tested is 5ms per update. So the fallback here REFUSES rather
     * than degrades -- a measurement that cannot resolve the thing it is
     * measuring is worse than none, because it reads like data.
     * docs/PROGRESS.md:87 is the standing rule ("Do not budget against
     * them").
     *
     * `liveRenderUs` is zero until stage 3's C2 gives this path something to
     * render; until then the DOM half is the whole cost, which is the point
     * of taking the number now. */
    LARGE_INTEGER  perfHz;
    int            perfOk;      /* -1 unknown, 0 refused, 1 usable */

    /* Why set_document gave up, for boot_local's caller to show. A member
     * rather than a return value because the failures are five returns
     * deep in COM setup and each knows something different; 192 bytes
     * holds a MAX_PATH path and a sentence about it. */
    char           bootErr[192];
    unsigned       liveCount;
    unsigned long  liveBytes;
    unsigned long  liveDomUs, liveDomMaxUs;
    unsigned long  liveRenderUs, liveRenderMaxUs;

    explicit rc_html_impl(HWND p)
        : parent(p), ready(0), diag(0),
          doc_len(0), doc_sum(0),
          perfOk(-1),
          liveCount(0), liveBytes(0),
          liveDomUs(0), liveDomMaxUs(0),
          liveRenderUs(0), liveRenderMaxUs(0),
          /* On unless the user says otherwise -- the same default
           * ConfigStore::load uses. The struct is zero-filled, so without this
           * the setting would be off by accident on the very first run. */
          autoscroll(1)
    {
        path[0] = '\0';
        bootErr[0] = '\0';
        perfHz.QuadPart = 0;
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
    BSTR b;

    /* NULL ON FAILURE, never an empty string.
     *
     * Substituting L"" made every failure here SUCCEED downstream and lose
     * data in silence: insertAdjacentHTML("") returns S_OK, so append()'s
     * `if (FAILED(hr))` never fired, diag still reported "first append ok",
     * and the bridge's block simply was not in the transcript. live() was
     * worse -- put_innerHTML("") WIPED the partial answer instead of leaving
     * it alone. A NULL BSTR makes those calls fail loudly, which is what the
     * callers' existing error handling is already written for. */
    if (!s) s = "";
    /* Straight into the BSTR: SysAllocStringLen(NULL, len) gives a buffer
     * of that many characters plus the terminator, so there is no
     * temporary to widen into and copy out of. */
    n = MultiByteToWideChar(1252, 0, s, -1, NULL, 0);
    if (n <= 0) return NULL;
    b = SysAllocStringLen(NULL, (UINT)(n - 1));
    if (!b) { rc_log("html: no memory to widen %d bytes", n); return NULL; }
    MultiByteToWideChar(1252, 0, s, -1, b, n);
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
            IDispatch_QueryInterface(disp.get(), RC_IID_IHTMLElement, (void **)&el_raw);
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

    if (d->queue.size() + n + 1 > RC_HTML_QUEUE_CAP) {
        rc_log("html: pre-ready queue would exceed %u bytes (have %u, this "
               "fragment %u); dropping it",
               (unsigned)RC_HTML_QUEUE_CAP, (unsigned)d->queue.size(), n);
        return;
    }
    d->queue += frag;
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

/* The document's skeleton, COMPILED IN while the stylesheet is a file.
 *
 * Not a template, and the asymmetry is deliberate rather than an oversight:
 * the three slot ids are found at runtime by find_id, so they have to be
 * known to C++ anyway, and a template whose keys the code must ALSO hold as
 * constants is two sources for one fact. What a designer wants to change
 * here is the style and the block markup, and both of those are files.
 *
 * #s is written by the GUEST and that is not a breach of "the guest
 * transforms nothing": it renders no bridge content, it says what the app
 * itself is doing. It is the one slot the old bridge-written skeleton made
 * optional, BECAUSE "the bridge's DOC is authoritative and may be an older
 * build than this guest" -- which is the whole argument this stage answers,
 * and after C4 the sentence stops being true in the direction that made the
 * slot optional. */
static const char SKELETON_HEAD[] =
    "<html><head><meta http-equiv=\"Content-Type\" "
    "content=\"text/html; charset=";
static const char SKELETON_MID[] = "\"><style>";
static const char SKELETON_TAIL[] =
    "</style></head><body><div id=\"" RC_SLOT_DONE "\"></div>"
    "<div id=\"" RC_SLOT_LIVE "\"></div>"
    "<div id=\"" RC_SLOT_STATUS "\"></div></body></html>";

/* What to put in the <meta>, for THIS machine's ANSI codepage.
 *
 * Derived from GetACP() rather than sent: the charset must name the table
 * the bytes were written with, and that is a fact about this machine. The
 * bridge computes the same thing from the codec it folds with
 * (bridge/files/caps.py html_charset) and the two must agree, which is what
 * tests/python/meta/test_guest_css.py's charset case asserts.
 *
 * The CJK pages have conventional IANA names that browsers of the era
 * recognise; their numeric spellings are not the web's. */
static const char *acp_charset()
{
    switch (GetACP()) {
    case 932: return "shift_jis";
    case 936: return "gb2312";
    case 949: return "euc-kr";
    case 950: return "big5";
    default:  break;
    }
    /* 1250..1258 are the Windows ANSI pages and take windows-NNNN. Anything
     * else is accepted under its own number by every browser that can
     * render it at all. */
    {
        static char buf[24];
        rc_strf(buf, sizeof buf, "windows-%u", (unsigned)GetACP());
        return buf;
    }
}

int rc_html::boot_local(char *err, unsigned errCap)
{
    char dir[MAX_PATH], path[MAX_PATH];
    long n;
    int ok = 0;

    /* Cleared first, so a caller that forgets to check the return value
     * still reads an empty string rather than a stack frame. */
    if (err && errCap) err[0] = '\0';

    GetModuleFileNameA(NULL, dir, sizeof(dir) - 1);
    { char *slash = strrchr(dir, '\\'); if (slash) *slash = '\0'; }
    rc_strf(path, sizeof(path), "%s\\view.css", dir);

    /* THE STYLESHEET, not a finished document. Until stage 3 this read
     * view.htm, which mktools-iso.sh GENERATED from bridge/view/html.py at
     * package time -- so the appearance was the bridge's even in the file
     * shipped beside the exe, and a guest could not change how the guest
     * looks by rebuilding the guest. Now the client composes the document
     * and the file holds only what a person would want to edit. */
    File f(fopen(path, "rb"));
    if (!f.valid()) {
        rc_log("html: no stylesheet at %s", path);
        rc_strf(err, errCap, "there is no view.css beside GASTON.EXE (%s)",
                path);
        return 0;
    }
    fseek(f.get(), 0, SEEK_END);
    n = ftell(f.get());
    fseek(f.get(), 0, SEEK_SET);
    /* A stylesheet is about 11KB. Anything far larger is not the file we
     * ship, and reading it into a heap block on a 28KB-free machine is not
     * worth finding out. */
    if (n > 0 && n < 64 * 1024) {
        const char *charset = acp_charset();
        /* memcpy with lengths we already have, not strcpy: check-sources
         * bans the str* family outright (win/core/str.h), and here the
         * lengths are the thing being computed anyway. */
        unsigned headN = (unsigned)strlen(SKELETON_HEAD);
        unsigned csN   = (unsigned)strlen(charset);
        unsigned midN  = (unsigned)strlen(SKELETON_MID);
        unsigned tailN = (unsigned)strlen(SKELETON_TAIL);
        unsigned total = (unsigned)n + headN + csN + midN + tailN + 1;
        HeapPtr<char> buf((char *)HeapAlloc(GetProcessHeap(), 0, total));
        if (buf.valid()) {
            char *w = buf.get();
            size_t got;
            memcpy(w, SKELETON_HEAD, headN); w += headN;
            memcpy(w, charset, csN);         w += csN;
            memcpy(w, SKELETON_MID, midN);   w += midN;
            got = fread(w, 1, (size_t)n, f.get());
            if (got == (size_t)n) {
                w += n;
                memcpy(w, SKELETON_TAIL, tailN + 1);
                /* CLOSE BEFORE INSTALLING. set_document writes view.htm
                 * beside this file; holding a FILE* open across that is an
                 * unnecessary hazard on a platform whose sharing rules are
                 * not the ones anyone remembers. (It is no longer the SAME
                 * file, as it was before stage 3 -- but closing early is
                 * still right and is cheaper than reasoning about it.) */
                f.reset();
                if (set_document(buf.get())) {
                    ok = 1;
                    rc_log("html: document composed from %ld bytes of CSS, "
                           "charset %s", n, charset);
                } else {
                    rc_strf(err, errCap, "%s", d->bootErr);
                }
            } else {
                rc_log("html: short read of %s (%u of %ld)", path,
                       (unsigned)got, n);
                rc_strf(err, errCap, "%s could not be read in full "
                        "(%u of %ld bytes)", path, (unsigned)got, n);
            }
        } else {
            rc_log("html: no room for a %ld-byte document", n);
            rc_strf(err, errCap, "there is not enough memory free to "
                    "compose the transcript document");
        }
    } else {
        rc_log("html: stylesheet is %ld bytes; ignored", n);
        rc_strf(err, errCap, "%s is %ld bytes, which is not the file this "
                "build ships", path, n);
    }
    /* Whatever path we leave by, the guard closes it. The explicit close
     * above is the one that must happen EARLY, not the one that must
     * happen at all. */
    return ok;
}

/* Point the control at d->path. Split out because set_document has two
 * callers' worth of work now: the first install creates the control and
 * navigates, a later one with different content only navigates. */
void rc_html::renavigate()
{
    char url8[MAX_PATH + 16];
    char *p;
    Bstr url;
    VARIANT empty;

    if (!d->web.valid()) return;
    rc_strf(url8, sizeof(url8), "file:///%s", d->path);
    for (p = url8; *p; p++) if (*p == '\\') *p = '/';
    url.reset(to_bstr(url8));
    VariantInit(&empty);
    IWebBrowser2_Navigate(d->web.get(), url.get(), &empty, &empty, &empty,
                          &empty);
    rc_log("html: navigating to %s", d->path);
}

/* Cheap content fingerprint; see rc_html_impl::doc_sum. */
static void doc_fingerprint(const char *doc, unsigned *len, unsigned *sum)
{
    unsigned n = 0, acc = 2166136261u;
    const unsigned char *p = (const unsigned char *)doc;
    for (; *p; p++, n++) { acc ^= *p; acc *= 16777619u; }
    *len = n;
    *sum = acc;
}

/* Undo everything set_document recorded, so the NEXT attach retries.
 *
 * The fingerprint is stored before the control is built, which is what makes
 * "identical skeleton -> no-op" cheap on a reconnect. The cost is that any
 * failure after that point has to clear it by hand, or the identical skeleton
 * the bridge re-sends is matched against a fingerprint for a document that was
 * never successfully installed, and the view stays blank forever. */
/* Give up on the control, leaving nothing half-attached.
 *
 * This used to drop the two references and nothing else, while
 * ~rc_html_impl went to the trouble of Close + SetClientSite(NULL) first.
 * The difference mattered on RE-navigation: three of this function's five
 * callers are the file-write failures below, which sit BEFORE the
 * `if (d->obj.valid())` branch, so on a mid-session document update they
 * released a control that was already created and in-place active --
 * without closing it, and without dropping a client site that points at
 * `&d->host.site`, a sub-object of this very struct.
 *
 * Then, because obj is nulled, the next successful set_document() fails the
 * valid() test and calls CoCreateInstance again -- the outcome that
 * function's own comment names as the thing to avoid: "Creating a second
 * control would leak the first and lose the transcript."
 *
 * Same sequence as the destructor, for the same reason. (mem-audit L1) */
static void boot_failed(rc_html_impl *d)
{
    d->doc_len = 0;
    d->doc_sum = 0;
    d->ready   = 0;
    if (d->obj.valid()) {
        IOleObject_Close(d->obj.get(), OLECLOSE_NOSAVE);
        IOleObject_SetClientSite(d->obj.get(), NULL);
    }
    d->web.reset(NULL);
    d->obj.reset(NULL);
}

bool rc_html::set_document(const char *doc)
{
    RECT rc;
    HRESULT hr;
    VARIANT empty;
    char dir[MAX_PATH];
    unsigned len, sum;

    if (!doc || !*doc) {
        rc_strf(d->bootErr, sizeof(d->bootErr), "the document came out empty");
        return false;
    }

    /* Re-installable, where this used to be once per process.
     *
     * The old guard was `if (d->obj.valid()) return;`, and main.cpp had a
     * second one (g_doc_sent) that was also per-process rather than per
     * connection. Between them, a stylesheet change on the bridge could not
     * reach a running guest at all -- it needed the app restarted -- and the
     * document could never be installed before a bridge answered.
     *
     * Now: identical content is a no-op, because the bridge re-sends the
     * same skeleton on every attach and re-navigating would throw the
     * transcript away for nothing. Different content re-navigates, which is
     * what makes a CSS edit land on a reconnect. */
    doc_fingerprint(doc, &len, &sum);
    if (d->obj.valid() && len == d->doc_len && sum == d->doc_sum)
        return true;            /* already installed, which is a success */
    d->doc_len = len;
    d->doc_sum = sum;

    /* A file rather than document.write: writing needs the document to exist
     * first, which means waiting for a navigation anyway, and this way there
     * is exactly one asynchronous step to wait on instead of two. */
    GetModuleFileNameA(NULL, dir, sizeof(dir) - 1);
    { char *slash = strrchr(dir, '\\'); if (slash) *slash = '\0'; }
    rc_strf(d->path, sizeof(d->path), "%s\\view.htm", dir);
    /* CHECKED, all three. A full disk truncates the skeleton mid-document and
     * a read-only exe directory (a CD-ROM or a network install) fails the
     * open -- both left a blank transcript with one log line and no way back,
     * because the fingerprint above was already recorded and the bridge
     * re-sends identical bytes forever. fputs returns EOF on error and the
     * bytes a buffered fclose flushes can fail there and nowhere else, which
     * is why the close is checked rather than assumed. */
    File f(fopen(d->path, "wb"));
    if (!f.valid()) {
        rc_log("html: cannot write %s (%d)", d->path, errno);
        rc_strf(d->bootErr, sizeof(d->bootErr), "cannot write %s", d->path);
        boot_failed(d);
        return false;
    }
    if (fputs(doc, f.get()) == EOF) {
        rc_log("html: short write to %s -- the skeleton would be truncated",
               d->path);
        rc_strf(d->bootErr, sizeof(d->bootErr),
                "only part of %s could be written -- is the disk full?",
                d->path);
        boot_failed(d);
        return false;           /* the guard closes it on the way out */
    }
    /* THE CLOSE IS CHECKED, so it cannot be left to the guard: a destructor
     * has nowhere to put the answer, and stdio reports a full disk HERE and
     * nowhere else -- which is what the comment above this block is about.
     * release() hands the handle to the check, which then owns it. */
    if (fclose(f.release()) != 0) {
        rc_log("html: cannot flush %s -- out of disk?", d->path);
        rc_strf(d->bootErr, sizeof(d->bootErr),
                "%s could not be flushed -- is the disk full?", d->path);
        boot_failed(d);
        return false;
    }

    /* The file is rewritten above either way; if the control is already
     * hosting, all that is left is to point it at the new bytes. Creating a
     * second control would leak the first and lose the transcript. */
    if (d->obj.valid()) {
        d->ready = 0;
        d->slot_t.reset(NULL);
        d->slot_l.reset(NULL);
        d->slot_s.reset(NULL);
        renavigate();
        rc_log("html: document changed, re-navigating");
        return true;
    }

    hr = CoCreateInstance(RC_CLSID_WebBrowser, NULL, CLSCTX_INPROC_SERVER,
                          RC_IID_IOleObject, (void **)d->obj.addr());
    if (FAILED(hr)) {
        rc_log("html: CoCreateInstance %08lX", (unsigned long)hr);
        rc_strf(d->bootErr, sizeof(d->bootErr),
                "the Internet Explorer web browser control could not be "
                "created (%08lX)", (unsigned long)hr);
        return false;           /* obj is null, so the next attach retries */
    }

    IOleObject_SetClientSite(d->obj.get(), &d->host.site);
    OleSetContainedObject((IUnknown *)d->obj.get(), TRUE);

    GetClientRect(d->parent, &rc);
    /* BOTH failure paths below must UNDO the fingerprint and drop `obj`, or
     * the next attach cannot retry.
     *
     * doc_len/doc_sum are recorded near the top of this function, before the
     * control exists. If DoVerb or the QI failed, this returned with obj
     * VALID and web null -- so on the next reconnect the bridge re-sent the
     * identical skeleton, the guard up there saw
     * `obj.valid() && len == doc_len && sum == doc_sum` and returned at once.
     * Nothing ever retried: poll() returned 0 forever, every frame piled into
     * the pre-ready queue until it hit its cap, and the window stayed a blank
     * rectangle for the rest of the session with reconnecting unable to fix
     * it. CoCreateInstance failing does NOT have this problem, because it
     * leaves obj null; only these two latch. */
    hr = IOleObject_DoVerb(d->obj.get(), OLEIVERB_INPLACEACTIVATE, NULL,
                           &d->host.site, 0, d->parent, &rc);
    if (FAILED(hr)) {
        rc_log("html: DoVerb %08lX", (unsigned long)hr);
        rc_strf(d->bootErr, sizeof(d->bootErr),
                "the web browser control refused to activate (%08lX)",
                (unsigned long)hr);
        boot_failed(d);
        return false;
    }

    hr = IOleObject_QueryInterface(d->obj.get(), RC_IID_IWebBrowser2,
                                   (void **)d->web.addr());
    if (FAILED(hr)) {
        rc_log("html: QI IWebBrowser2 %08lX", (unsigned long)hr);
        rc_strf(d->bootErr, sizeof(d->bootErr),
                "the web browser control has no IWebBrowser2 (%08lX)",
                (unsigned long)hr);
        boot_failed(d);
        return false;
    }

    renavigate();
    (void)empty;

    {   /* Where did the control actually put its window? A control that is
         * hosting correctly but sized to nothing looks exactly like one that
         * is not hosting at all. */
        ComPtr<IOleWindow> ow;
        HWND h = NULL;
        {
            IOleWindow *ow_raw = NULL;
            if (SUCCEEDED(IOleObject_QueryInterface(d->obj.get(), RC_IID_IOleWindow,
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

    return true;
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
        IDispatch_QueryInterface(disp.get(), RC_IID_IHTMLDocument2, (void **)&doc_raw);
        d->doc.reset(doc_raw);
    }
    if (!d->doc.valid()) return 0;

    d->slot_t.reset(find_id(d->doc.get(), RC_SLOT_DONE));
    d->slot_l.reset(find_id(d->doc.get(), RC_SLOT_LIVE));
    if (!d->slot_t.valid() || !d->slot_l.valid()) {
        rc_log("html: document has no #t/#l containers");
        return 0;
    }
    /* Optional, and deliberately not part of the test above. The bridge's
     * DOC wins over the copy shipped beside the exe, and the two can be
     * different builds -- so a guest that insists on #s would refuse to
     * render a transcript at all when talking to an older bridge. Losing the
     * status line is cosmetic; losing #t is the whole window. */
    d->slot_s.reset(find_id(d->doc.get(), RC_SLOT_STATUS));
    if (!d->slot_s.valid())
        rc_log("html: no #s container; status line disabled");
    d->ready = 1;
    rc_log("html: document ready");

    if (!d->queue.empty()) {
        /* Moved out first: append() must not read a buffer this could
         * touch, and swapping with an empty string gives the memory back --
         * the queue is only ever needed before the first document. */
        std::string pending;
        pending.swap(d->queue);
        append(pending.c_str());
    }
    return 1;
}

/* --- the records the bridge sends, and the buffer they render into -------
 *
 * A heap buffer per RECORD and not one per frame, which is the memory
 * mitigation the plan's §7 asks for: a page replay is up to 90 blocks, and
 * rendering them into one buffer would hold the whole page's markup AND
 * the UTF-16 BSTR to_bstr makes of it -- twice the rendered length -- on a
 * machine measured at 28 KB free after a turn. One record at a time holds
 * one record's worth.
 *
 * It does NOT apply to live(): that is put_innerHTML on #l, a wholesale
 * replacement, because the bridge re-sends the whole open block every
 * flush. Inserting per record there would append a fresh copy every 100ms,
 * and the live path is one record by construction.
 */
class HtmlBuf : public ByteSink
{
public:
    HtmlBuf() : m_buf(0), m_len(0), m_cap(0) {}
    ~HtmlBuf() { if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf); }

    bool write(const void *data, unsigned len)
    {
        if (m_len + len + 1 > m_cap && !grow(m_len + len + 1))
            return false;
        memcpy(m_buf + m_len, data, len);
        m_len += len;
        m_buf[m_len] = '\0';
        return true;
    }

    const char *str() const { return m_buf ? m_buf : ""; }
    unsigned len() const { return m_len; }

private:
    HtmlBuf(const HtmlBuf &);
    HtmlBuf &operator=(const HtmlBuf &);

    bool grow(unsigned need)
    {
        /* Doubling from 4KB, a live update's size. CAPPED, because the one
         * allocation in this program with no ceiling was the pre-ready
         * queue and audit 04.29 is what that cost. A record needing more
         * is refused and logged, not grown into. */
        unsigned want = m_cap ? m_cap : 4096;
        char *bigger;
        while (want < need && want < (1u << 20))
            want *= 2;
        if (want < need) {
            rc_log("html: a record needs %u bytes of markup, over the cap",
                   need);
            return false;
        }
        bigger = (char *)(m_buf
                          ? HeapReAlloc(GetProcessHeap(), 0, m_buf, want)
                          : HeapAlloc(GetProcessHeap(), 0, want));
        if (!bigger) {
            rc_log("html: no memory to render %u bytes", want);
            return false;
        }
        m_buf = bigger;
        m_cap = want;
        return true;
    }

    char    *m_buf;
    unsigned m_len;
    unsigned m_cap;
};

/* --- the transcript's own vocabulary -------------------------------------
 *
 * Escaping lives here because it has to happen on EVERY path into the
 * document, and leaving it to the caller meant two paths did not do it.
 * `<` in a bridge error message or a hostname ended the document early.
 *
 * This is not the same escaping as the bridge's html.esc (html.py, plain
 * html.escape(quote=False)), and that is by design rather than drift: the
 * two never see the same string. html.esc feeds the markdown pipeline, whose
 * output arrives here pre-rendered and goes straight through say()'s callers
 * block()/live() -- past this function entirely, newlines and all, because
 * the bridge's line-oriented parser already turned structure into real HTML
 * before it was ever escaped. esc_into only runs on plain text that has no
 * such structure: the user's own locally-echoed input, and the handful of
 * one-line protocol fields (ERROR's code/msg, NOTICE's body, TURN's error
 * msg) that the bridge sends unescaped on purpose and expects THIS side to
 * make safe -- see the "Escaped, which it was not" fix at each call site in
 * main.c. For that content, folding \n to <br> and dropping \r is the guest's
 * own call to make, not a mismatch to close: there is no upstream line
 * splitter for a status message. Recorded as audit finding 03.23. */
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
 * autoscroll setting was read by nothing.
 *
 * THE SETTING IS AN OVERRIDE NOW, not a second condition. It used to be
 * `autoscroll && was_at_bottom`, so following the conversation depended on a
 * measurement of where the view happened to be -- and "happened to be" is
 * doing a lot of work there. `at_bottom()` compares scrollTop + clientHeight
 * against scrollHeight with 24px of slack, and anything that leaves the view
 * a few pixels short (a restored page, a block that grew after it was
 * measured, a manual nudge that was never scrolled back) turns autoscroll off
 * silently and permanently until the user scrolls to the end by hand.
 * Reported twice from the real machine, most recently for an error notice
 * that never came into view.
 *
 * So: ON means the view follows, always. OFF means it never moves on its own.
 * Both are states the user chose and can see (Configure > Autoscroll), which
 * is worth more than a heuristic that is right most of the time and gives no
 * account of itself when it is not. `was_at_bottom` stays in the signature
 * because every caller already measures it and the OFF case is where a
 * smarter policy would go if one is ever wanted. */
void rc_html::settle(int was_at_bottom)
{
    (void)was_at_bottom;
    if (d->autoscroll) scroll_bottom();
}

/* Escape `text` into a heap block wrapped in `<div class="cls">`, append it,
 * and apply the scroll policy. Heap rather than stack because escaping can
 * expand five-fold and the largest frame in this program is already in
 * main.c. */
void rc_html::say_local(RcLocal kind, const char *text)
{
    unsigned n, cap;
    int bottom;
    HtmlBuf out;

    if (!text) return;
    n = (unsigned)strlen(text);
    cap = n * 6 + 64;                       /* '&' -> "&amp;" is the worst */
    /* BOTH failures are logged. queue_add was fixed for exactly this and
     * these two were not: notice(), error() and user() all land here, and
     * error() is what says "the connection is down" -- so on the machine's
     * binding constraint the message explaining why things are failing was
     * the message that vanished without trace. The arithmetic is sound (6
     * bytes is the true worst case, "&quot;"); only the silence was wrong. */
    HeapPtr<char> esc((char *)HeapAlloc(GetProcessHeap(), 0, cap));
    if (!esc.valid()) {
        rc_log("html: no memory to escape a %u byte local block", n);
        return;
    }
    esc_into(text, esc.get(), cap);

    /* THE WRAPPER COMES FROM view.tpl from stage 3 C2, so the class names
     * and the element are in one file with every other block's. What does
     * NOT come from there is the escaping above: see rc_wrap_local's
     * comment in view/render.h for why these keep their own. */
    if (!rc_wrap_local(kind, esc.get(), out)) {
        rc_log("html: could not wrap a %u byte local block", n);
        return;
    }
    bottom = at_bottom();
    append(out.str());
    settle(bottom);
}

void rc_html::settle_pending()
{
    if (!d->ready) return;
    {
        ComPtr<IHTMLElement> el;
        IHTMLElement *raw = find_id(d->doc.get(), RC_ID_PENDING);
        if (!raw) return;                   /* nothing outstanding */
        el.reset(raw);
        {
            /* Class first, then the id. Dropping the id is what keeps the
             * NEXT echo's id unique -- MSHTML's getElementById returns the
             * first match, so a stale one would make settle_pending settle
             * the wrong message from then on. */
            Bstr cls(to_bstr(RC_CSS_USER));
            Bstr none(to_bstr(""));
            IHTMLElement_put_className(el.get(), cls.get());
            IHTMLElement_put_id(el.get(), none.get());
        }
    }
}

void rc_html::settle_perm()
{
    if (!d->ready) return;
    {
        ComPtr<IHTMLElement> el;
        IHTMLElement *raw = find_id(d->doc.get(), RC_ID_PERM);
        if (!raw) return;                   /* nothing on screen */
        el.reset(raw);
        {
            /* Class first, then the id -- same order and same reason as
             * settle_pending: getElementById returns the first match, so a
             * stale id would make the NEXT answer collapse this block again
             * instead of the new one. */
            Bstr cls(to_bstr(RC_CSS_PERMDONE));
            Bstr none(to_bstr(""));
            IHTMLElement_put_className(el.get(), cls.get());
            IHTMLElement_put_id(el.get(), none.get());
        }
    }
}

void rc_html::notice(const char *text)
{ say_local(RC_LOCAL_NOTICE, text); }
void rc_html::error(const char *text)
{ say_local(RC_LOCAL_ERROR, text); }
void rc_html::user(const char *text)
{
    /* STRIP BEFORE ESCAPING, because the bridge does (01.56).
     *
     * This is the local echo of what the person just typed, and the bridge
     * renders the SAME message into the sealed page with
     * bridge/view/html.py's user_turn(), which does text.strip() first. The
     * guest did not, so a message with a trailing blank line -- which is one
     * stray Enter before Send -- rendered one line taller here than in the
     * copy the user scrolls back to. Two renderings of one message that do
     * not match, in a transcript whose whole job is to be the record.
     *
     * The bridge owns the rule, not this side: it renders the transcript and
     * the guest transforms nothing, so where the two disagree the guest is
     * what moves. Leading and trailing whitespace only -- Python's strip()
     * with no argument -- and nothing about the interior, which user_turn
     * splits into lines and this side keeps as-is. */
    const char *b = text;
    unsigned n;

    if (!b) return;
    while (*b == ' ' || *b == '\t' || *b == '\r' || *b == '\n') b++;
    n = (unsigned)strlen(b);
    while (n && (b[n - 1] == ' ' || b[n - 1] == '\t'
                 || b[n - 1] == '\r' || b[n - 1] == '\n')) n--;

    {
        /* say_local takes a C string, so the trimmed tail needs its own copy.
         * Heap rather than stack: the input is the user's whole message and
         * the largest frame in this program already lives in main. */
        HeapPtr<char> t((char *)HeapAlloc(GetProcessHeap(), 0, n + 1));
        if (!t.valid()) { say_local(RC_LOCAL_PENDING, b); return; }
        memcpy(t.get(), b, n);
        t.get()[n] = '\0';
        say_local(RC_LOCAL_PENDING, t.get());
    }
}

void rc_html::block(const char *html)
{
    int bottom;
    if (!html || !*html) return;
    bottom = at_bottom();
    append(html);
    settle(bottom);
}

void rc_html::records(const char *rec, unsigned len, unsigned wrapcol)
{
    const char *p = rec;
    const char *end = rec + len;
    int bottom;

    if (!rec || !len) return;
    bottom = at_bottom();
    while (p < end) {
        /* One record at a time, so the buffer is reused rather than
         * accumulated across a page. rc_render_count would be a second
         * pass; instead the renderer is asked for one record and says
         * where it stopped. */
        HtmlBuf out;
        int n = rc_render_one(p, (unsigned)(end - p), wrapcol, out);
        if (n <= 0) {
            rc_log("html: a record was refused %u bytes into a %u byte "
                   "body; the rest of it is dropped",
                   (unsigned)(p - rec), len);
            break;
        }
        if (out.len())
            append(out.str());
        p += n;
    }
    settle(bottom);
}

/* Non-zero if QPC can resolve a 5ms step on this machine.
 *
 * Asked once and remembered, because QueryPerformanceFrequency is a syscall
 * and this sits on a path that runs ten times a second. Refusing is a real
 * outcome: on a box where QPC is absent or has a tick coarser than the
 * threshold, every number below would be a rounded-up lie, so there are no
 * numbers at all and the log says which machine it was. */
static int perf_usable(rc_html_impl *d)
{
    if (d->perfOk >= 0) return d->perfOk;

    d->perfOk = 0;
    if (!QueryPerformanceFrequency(&d->perfHz) || d->perfHz.QuadPart <= 0) {
        rc_log("html: live timing off, no performance counter");
        return 0;
    }
    /* One tick must be well under the 5ms-per-update floor stage 3 budgets
     * against. 1MHz on the PIT, 3.5MHz-plus on anything later; a counter
     * slower than 20kHz (50us a tick) cannot see a 5ms difference with any
     * confidence, so it is declined by the same argument as GetTickCount. */
    if (d->perfHz.QuadPart < 20000) {
        rc_log("html: live timing off, counter is only %lu Hz",
               (unsigned long)d->perfHz.QuadPart);
        return 0;
    }
    d->perfOk = 1;
    return 1;
}

static unsigned long perf_us(rc_html_impl *d, const LARGE_INTEGER &t0)
{
    LARGE_INTEGER t1;

    if (!QueryPerformanceCounter(&t1)) return 0;
    return (unsigned long)(((t1.QuadPart - t0.QuadPart) * 1000000ULL) /
                           d->perfHz.QuadPart);
}

void rc_html::live_records(const char *rec, unsigned len, unsigned wrapcol)
{
    HtmlBuf out;
    int timed;
    LARGE_INTEGER t0;

    /* An EMPTY body is not nothing to do: it is what clears #l at the end
     * of a turn, and live("") is how that has always been said. */
    if (!rec || !len) { live(""); return; }

    /* TIMED, because this is the number stage 3 exists to be able to
     * answer: the bridge re-sends the whole open block every flush, so
     * this runs ten times a second for the length of an answer and the
     * work it does moved onto a 1998 CPU. A3 measured the DOM half with
     * nothing to compare it against; this is the other half. */
    timed = perf_usable(d);
    if (timed) timed = QueryPerformanceCounter(&t0) ? 1 : 0;

    if (!rc_render(rec, len, wrapcol, out)) {
        rc_log("html: live record refused; leaving what is on screen");
        return;
    }
    if (timed) live_cost(perf_us(d, t0));
    live(out.str());
}

void rc_html::set_autoscroll(int on) { d->autoscroll = on; }

void rc_html::append(const char *frag)
{
    HRESULT hr;

    if (!frag || !*frag) return;
    if (!d->ready) { queue_add(d, frag); return; }
    Bstr where(to_bstr("beforeEnd"));
    Bstr html(to_bstr(frag));
    /* to_bstr returns NULL when it could not widen the fragment. Refuse
     * rather than insert, so the block is missing WITH a log line instead of
     * missing silently. */
    if (!where.get() || !html.get()) {
        rc_log("html: append refused, could not widen %u bytes",
               (unsigned)strlen(frag));
        return;
    }
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

void rc_html::live_cost(unsigned long renderUs)
{
    /* Stage 3's C2 calls this with what the record render cost, just before
     * handing the markup to live(). Separate from live() itself so the two
     * halves can be told apart in the log -- the whole question is the ratio
     * between them, and connection.cpp already learned that a single total
     * answers nothing. */
    d->liveRenderUs += renderUs;
    if (renderUs > d->liveRenderMaxUs) d->liveRenderMaxUs = renderUs;
}

void rc_html::live_report()
{
    unsigned n = d->liveCount;

    if (!n) return;             /* a turn with no streamed text */

    /* Totals and a count, not means. The arithmetic belongs wherever the
     * log is read -- it costs nothing there and two divides plus two more
     * arguments is 60-odd bytes of image here, on a build with 3KB of slack.
     * Max is kept because it is not derivable from a total. */
    if (d->perfOk == 1)
        rc_log("live: n=%u bytes=%lu dom_us=%lu dom_max=%lu "
               "render_us=%lu render_max=%lu",
               n, d->liveBytes,
               d->liveDomUs, d->liveDomMaxUs,
               d->liveRenderUs, d->liveRenderMaxUs);
    else
        rc_log("live: n=%u bytes=%lu untimed", n, d->liveBytes);

    d->liveCount = 0;
    d->liveBytes = 0;
    d->liveDomUs = d->liveDomMaxUs = 0;
    d->liveRenderUs = d->liveRenderMaxUs = 0;
}

void rc_html::live(const char *frag)
{
    int bottom;

    if (!d->ready) return;

    /* Sample BEFORE the replace, and settle after -- the same order say()
     * and block() have always used, which this function did not.
     *
     * It did not scroll at all. Streaming text therefore never followed, and
     * worse, it broke the append path too: the live block grows the document
     * without moving the view, so by the time the finished block arrives
     * at_bottom() is already false and settle() declines. That is why
     * autoscroll looked like it "stopped working" partway through a turn and
     * then stayed stopped -- the finished answer was below the fold and the
     * user had to press Ctrl+End to find it.
     *
     * Two COM round trips per streamed delta is the cost. Measured against
     * the alternative of a timer, it is the cheaper one: at_bottom() already
     * reads three properties off the same IHTMLElement2 the poll timer
     * touches, and a 100ms timer that scrolls unconditionally would fight
     * the user every time they scrolled up to read. */
    bottom = at_bottom();
    {
        /* Timed from here, deliberately: the widen to UTF-16 is part of what
         * the DOM step costs on this machine. to_bstr allocates twice the
         * RENDERED length, which is the figure stage 3's memory note cares
         * about, so measuring around it and not just around put_innerHTML is
         * what makes the two comparable. at_bottom() is outside, because it
         * is scroll policy rather than the write, and it has been there since
         * before this measurement existed. */
        int timed = perf_usable(d);
        LARGE_INTEGER t0;
        unsigned long us;

        if (timed) timed = QueryPerformanceCounter(&t0) ? 1 : 0;

        Bstr html(to_bstr(frag ? frag : ""));
        HRESULT hr;
        /* CHECKED EXPLICITLY, because put_innerHTML(NULL) does not fail --
         * MSHTML treats it as "set it to nothing", which would WIPE the
         * partial answer on screen. That is the same outcome the old
         * to_bstr's L"" substitution produced, so returning NULL is only
         * half the fix; this is the other half. */
        if (!html.get()) {
            rc_log("html: live refused, could not widen the delta");
            return;             /* leave what is on screen alone */
        }
        hr = IHTMLElement_put_innerHTML(d->slot_l.get(), html.get());
        if (FAILED(hr)) {
            rc_log("html: live FAILED hr=%08lX", (unsigned long)hr);
            return;             /* nothing moved, so nothing to follow */
        }

        /* Only a completed update counts. A refused or failed one did not do
         * the work, and averaging it in would drag the mean toward whatever
         * the failure path costs. */
        d->liveCount++;
        d->liveBytes += (unsigned long)(frag ? strlen(frag) : 0);
        if (timed) {
            us = perf_us(d, t0);
            d->liveDomUs += us;
            if (us > d->liveDomMaxUs) d->liveDomMaxUs = us;
        }
    }
    settle(bottom);
}

void rc_html::status(const char *frag)
{
    if (!d->ready || !d->slot_s.valid()) return;
    {
        Bstr html(to_bstr(frag ? frag : ""));
        HRESULT hr;
        if (!html.get()) return;    /* see live(): NULL would blank the line */
        hr = IHTMLElement_put_innerHTML(d->slot_s.get(), html.get());
        if (FAILED(hr))
            rc_log("html: status FAILED hr=%08lX", (unsigned long)hr);
    }
    /* Deliberately no settle() here, unlike live().
     *
     * This line re-renders once a second for the whole turn -- from
     * TIMER_STATUS, and ONLY from there. It used to also be driven once per
     * inbound frame by render_frame's unconditional status_refresh(), so on a
     * streamed answer it did a full MultiByteToWideChar + SysAllocString +
     * put_innerHTML reflow per frame, with content identical between frames
     * because the only variable is a whole-second counter. On a 200MHz
     * Pentium that is a real cost on top of the live() write the frame
     * actually carries, and this comment asserted a frequency that was not
     * true (audit 01.33). Following it
     * would mean scrolling on a timer, which fights the user the moment they
     * scroll up to read something -- and at_bottom()'s 24px tolerance is
     * already about the height of this line, so the append path stays
     * correct without any help. */
}

void rc_html::clear()
{
    std::string().swap(d->queue);
    if (!d->ready) return;
    Bstr empty(to_bstr(""));
    /* Clearing is the one place a NULL would do the right thing anyway, but
     * to_bstr("") allocates a real empty BSTR and only fails on OOM -- in
     * which case there is nothing to clear with and nothing worth doing. */
    if (!empty.get()) { rc_log("html: clear refused, out of memory"); return; }
    IHTMLElement_put_innerHTML(d->slot_t.get(), empty.get());
    IHTMLElement_put_innerHTML(d->slot_l.get(), empty.get());
    if (d->slot_s.valid())
        IHTMLElement_put_innerHTML(d->slot_s.get(), empty.get());
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

void rc_html::scroll_page(int dir)
{
    long client = 0;

    if (!d->ready) return;
    {
        ComPtr<IHTMLElement> body;
        IHTMLElement *body_raw = NULL;
        if (FAILED(IHTMLDocument2_get_body(d->doc.get(), &body_raw)) || !body_raw)
            return;
        body.reset(body_raw);
        ComPtr<IHTMLElement2> b2;
        IHTMLElement2 *b2_raw = NULL;
        if (FAILED(IHTMLElement_QueryInterface(body.get(), RC_IID_IHTMLElement2,
                                               (void **)&b2_raw)) || !b2_raw)
            return;
        b2.reset(b2_raw);
        IHTMLElement2_get_clientHeight(b2.get(), &client);
    }
    if (client <= 0) return;

    /* Two lines of overlap, so the line you were reading is still on screen
     * after the jump. 24 is the same figure at_bottom() uses for "close
     * enough to the end", and for the same reason: it is about two lines of
     * 8pt text. */
    {
        long delta = client - 24;
        if (delta < 1) delta = 1;
        ComPtr<IHTMLWindow2> win;
        IHTMLWindow2 *win_raw = NULL;
        if (SUCCEEDED(IHTMLDocument2_get_parentWindow(d->doc.get(), &win_raw))
            && win_raw) {
            win.reset(win_raw);
            IHTMLWindow2_scrollBy(win.get(), 0, dir < 0 ? -delta : delta);
        }
    }
}

void rc_html::scroll_top()
{
    if (!d->ready) return;
    {
        ComPtr<IHTMLWindow2> win;
        IHTMLWindow2 *win_raw = NULL;
        if (SUCCEEDED(IHTMLDocument2_get_parentWindow(d->doc.get(), &win_raw))
            && win_raw) {
            win.reset(win_raw);
            /* Symmetric with scroll_bottom's absurd positive delta: the
             * window clamps, so this cannot overshoot. */
            IHTMLWindow2_scrollBy(win.get(), 0, -1000000);
        }
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
        if (SUCCEEDED(IHTMLElement_QueryInterface(body.get(), RC_IID_IHTMLElement2,
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
    if (SUCCEEDED(IOleObject_QueryInterface(d->obj.get(), RC_IID_IOleInPlaceObject,
                                            (void **)&ip_raw)) && ip_raw) {
        ip.reset(ip_raw);
        IOleInPlaceObject_SetObjectRects(ip.get(), &rc, &rc);
    }
}
