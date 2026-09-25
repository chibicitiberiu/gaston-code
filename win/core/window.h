/* A window as an object: the window procedure finds `this` and forwards to a
 * virtual handle(), so no module needs a static proc or a global instance to
 * reach its state.
 *
 * Two shapes, because the client has both:
 *
 *   Window            a window of our own class. `this` is passed through
 *                     CreateWindowEx's lpCreateParams and stored in
 *                     GWL_USERDATA at WM_NCCREATE, the pattern App's hidden
 *                     window used first.
 *   SubclassedWindow  an existing control -- a LISTBOX, an EDIT -- whose
 *                     window procedure is replaced. `this` goes in a window
 *                     property rather than GWL_USERDATA, which belongs to
 *                     whoever created the control. The chat list and the
 *                     compose box each did this through a static s_instance,
 *                     so only one of each could ever exist.
 *
 * Both hold the HWND in a WindowGuard, so an object destroyed while its
 * window lives takes the window with it, and both forget the handle at
 * WM_NCDESTROY, so a window destroyed from outside (a parent going, or
 * DestroyWindow on it) is not destroyed twice.
 *
 * DESTROY FROM THE DERIVED DESTRUCTOR. A window destroyed by ~Window runs its
 * WM_DESTROY while the derived part of the object is already gone, so only
 * the base handle() -- DefWindowProc -- sees it. A derived class whose
 * WM_DESTROY does real work calls destroy() in its own destructor, where its
 * handle() is still the one that runs.
 */
#ifndef RC_WINDOW_H
#define RC_WINDOW_H

#ifndef __cplusplus
#error "core/window.h is C++"
#endif

#include <windows.h>
#include "core/rc_cpp.h"

class Window
{
public:
    virtual ~Window();

    HWND hwnd() const { return m_window.get(); }
    bool valid() const { return m_window.valid() != 0; }

    /* DestroyWindow, if there is one. Safe to repeat. */
    void destroy();

    /* Registers a class whose procedure is Window's. `wc` supplies
     * everything else (icon, cursor, brush, style) and may be null; its
     * lpfnWndProc, hInstance and lpszClassName are overwritten. A class that
     * already exists is success, so two objects of one class are fine. */
    static bool registerClass(HINSTANCE instance, const char *className,
                              WNDCLASSA *wc);

protected:
    Window() {}

    /* CreateWindowEx with `this` attached. handle() already sees WM_CREATE,
     * with hwnd() valid, before this returns. */
    bool create(DWORD exStyle, const char *className, const char *title,
                DWORD style, int x, int y, int width, int height,
                HWND parent, HMENU menuOrId, HINSTANCE instance);

    /* Every message after WM_NCCREATE. Default: DefWindowProc. */
    virtual LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

    /* The window is gone and hwnd() is already null. */
    virtual void onDestroyed() {}

private:
    Window(const Window &);
    Window &operator=(const Window &);

    static LRESULT CALLBACK windowProc(HWND hwnd, UINT msg, WPARAM wp,
                                       LPARAM lp);

    WindowGuard m_window;
};

class SubclassedWindow
{
public:
    virtual ~SubclassedWindow();

    HWND hwnd() const { return m_window.get(); }
    bool valid() const { return m_window.valid() != 0; }

    /* Take over `control`'s window procedure, and own the control. False if
     * it is null or already attached to something. */
    bool attach(HWND control);

    /* Give the procedure back and stop owning the control, which lives on.
     * Returns it. */
    HWND detach();

    /* DestroyWindow on the control, if attached. Safe to repeat. */
    void destroy();

protected:
    SubclassedWindow() : m_previous(0) {}

    /* Every message. Default: the control's own procedure. */
    virtual LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

    /* The control's own procedure, for a handle() that adds to it. */
    LRESULT previous(UINT msg, WPARAM wp, LPARAM lp);

    virtual void onDestroyed() {}

private:
    SubclassedWindow(const SubclassedWindow &);
    SubclassedWindow &operator=(const SubclassedWindow &);

    static LRESULT CALLBACK windowProc(HWND hwnd, UINT msg, WPARAM wp,
                                       LPARAM lp);

    WindowGuard m_window;
    WNDPROC m_previous;
};

#endif
