#include "core/window.h"

#include "core/log.h"

/* The property a subclassed control carries its object in. A string, so
 * SetPropA registers the atom itself; removed at WM_NCDESTROY, because a
 * property left on a destroyed window leaks its atom on 9x. */
static const char THIS_PROP[] = "GastonWindowThis";

/* --- Window ---------------------------------------------------------------- */

Window::~Window()
{
    destroy();
}

void Window::destroy()
{
    /* DestroyWindow while still owned, so handle() sees WM_DESTROY and
     * WM_NCDESTROY; windowProc lets go of the handle at WM_NCDESTROY. The
     * guard's reset() would forget it first and hide both. */
    if (valid())
        DestroyWindow(hwnd());
}

bool Window::registerClass(HINSTANCE instance, const char *className,
                           WNDCLASSA *wc)
{
    WNDCLASSA local;
    if (!wc)
    {
        ZeroMemory(&local, sizeof local);
        local.hCursor = LoadCursorA(NULL, IDC_ARROW);
        wc = &local;
    }
    wc->lpfnWndProc = windowProc;
    wc->hInstance = instance;
    wc->lpszClassName = className;
    if (!RegisterClassA(wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS)
    {
        rc_log("window: RegisterClass(%s) failed, error %lu", className,
               GetLastError());
        return false;
    }
    return true;
}

bool Window::create(DWORD exStyle, const char *className, const char *title,
                    DWORD style, int x, int y, int width, int height,
                    HWND parent, HMENU menuOrId, HINSTANCE instance)
{
    if (valid())
        return false;
    HWND made = CreateWindowExA(exStyle, className, title, style, x, y, width,
                                height, parent, menuOrId, instance, this);
    if (!made)
    {
        rc_log("window: CreateWindowEx(%s) failed, error %lu", className,
               GetLastError());
        return false;
    }
    /* windowProc took ownership at WM_NCCREATE. If WM_CREATE failed the
     * window is already gone and the guard already empty. */
    return valid();
}

LRESULT Window::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    return DefWindowProcA(hwnd(), msg, wp, lp);
}

LRESULT CALLBACK Window::windowProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_NCCREATE)
    {
        Window *self = (Window *)((CREATESTRUCTA *)lp)->lpCreateParams;
        SetWindowLongA(hwnd, GWL_USERDATA, (LONG)self);
        /* Owned from here, so hwnd() is valid inside WM_CREATE, before
         * CreateWindowEx returns. */
        if (self)
            self->m_window.reset(hwnd);
    }
    Window *self = (Window *)GetWindowLongA(hwnd, GWL_USERDATA);
    /* A top-level window gets WM_GETMINMAXINFO before WM_NCCREATE, and a
     * window being torn down by ~Window after the guard let go has no one
     * to forward to. */
    if (!self || self->hwnd() != hwnd)
        return DefWindowProcA(hwnd, msg, wp, lp);


    if (msg == WM_NCDESTROY)
    {
        LRESULT r = self->handle(msg, wp, lp);
        SetWindowLongA(hwnd, GWL_USERDATA, 0);
        self->m_window.release();
        self->onDestroyed();
        return r;
    }
    return self->handle(msg, wp, lp);
}

/* --- SubclassedWindow ------------------------------------------------------ */

SubclassedWindow::~SubclassedWindow()
{
    destroy();
}

void SubclassedWindow::destroy()
{
    /* Through the window procedure, as Window::destroy, so WM_NCDESTROY
     * gives the procedure back and removes the property. */
    if (valid())
        DestroyWindow(hwnd());
}

bool SubclassedWindow::attach(HWND control)
{
    if (!control || valid() || GetPropA(control, THIS_PROP))
        return false;
    if (!SetPropA(control, THIS_PROP, (HANDLE)this))
        return false;
    m_previous = (WNDPROC)SetWindowLongA(control, GWL_WNDPROC,
                                         (LONG)windowProc);
    m_window.reset(control);
    return true;
}

HWND SubclassedWindow::detach()
{
    HWND control = m_window.release();
    if (control)
    {
        SetWindowLongA(control, GWL_WNDPROC, (LONG)m_previous);
        RemovePropA(control, THIS_PROP);
    }
    return control;
}

LRESULT SubclassedWindow::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    return previous(msg, wp, lp);
}

LRESULT SubclassedWindow::previous(UINT msg, WPARAM wp, LPARAM lp)
{
    return CallWindowProcA(m_previous, hwnd(), msg, wp, lp);
}

LRESULT CALLBACK SubclassedWindow::windowProc(HWND hwnd, UINT msg, WPARAM wp,
                                              LPARAM lp)
{
    SubclassedWindow *self = (SubclassedWindow *)GetPropA(hwnd, THIS_PROP);
    if (!self)
        return DefWindowProcA(hwnd, msg, wp, lp);


    if (msg == WM_NCDESTROY)
    {
        WNDPROC previous = self->m_previous;
        self->detach();
        self->onDestroyed();
        return CallWindowProcA(previous, hwnd, msg, wp, lp);
    }
    return self->handle(msg, wp, lp);
}
