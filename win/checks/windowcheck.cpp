/* Does core/window.cpp's Window and SubclassedWindow do what they say?
 *
 * Runs the real code under Wine and prints `verdict<TAB>case<TAB>detail`
 * rows for tests/python/guest/test_guest_window.py. Messages are sent, not
 * posted, so no message loop is needed: SendMessage to a window on this
 * thread calls its procedure directly.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "core/window.h"

/* Its own block, clear of core/ and the other checks (tools/check/msgids.py). */
#define WM_CHECK_WHO (WM_APP + 0x210)

static const char CLASS_NAME[] = "GastonWindowCheck";

static void say(const char *name, bool pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
}

class Probe : public Window
{
public:
    explicit Probe(int id, bool failCreate = false)
        : m_id(id), m_failCreate(failCreate), m_createSawHwnd(false),
          m_sawDestroy(false), m_sawNcDestroy(false), m_destroyed(false)
    {
    }

    ~Probe()
    {
        destroy();
    }

    bool make(HINSTANCE instance)
    {
        return create(0, CLASS_NAME, "probe", WS_POPUP, 0, 0, 10, 10, 0, 0,
                      instance);
    }

    int m_id;
    bool m_failCreate;
    bool m_createSawHwnd;
    bool m_sawDestroy;
    bool m_sawNcDestroy;
    bool m_destroyed;

protected:
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp)
    {
        switch (msg)
        {
        case WM_CREATE:
            m_createSawHwnd = hwnd() != 0;
            return m_failCreate ? -1 : 0;
        case WM_DESTROY:
            m_sawDestroy = true;
            break;
        case WM_NCDESTROY:
            m_sawNcDestroy = true;
            break;
        case WM_CHECK_WHO:
            return m_id;
        }
        return Window::handle(msg, wp, lp);
    }

    void onDestroyed()
    {
        m_destroyed = true;
    }
};

/* Swallows one character, passes everything else to the EDIT. */
class NoLetter : public SubclassedWindow
{
public:
    explicit NoLetter(char banned) : m_banned(banned), m_destroyed(false) {}
    bool m_destroyed;

protected:
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp)
    {
        if (msg == WM_CHAR && (char)wp == m_banned)
            return 0;
        return previous(msg, wp, lp);
    }

    void onDestroyed()
    {
        m_destroyed = true;
    }

private:
    char m_banned;
};

static HWND makeEdit(HINSTANCE instance)
{
    return CreateWindowExA(0, "EDIT", "", WS_POPUP, 0, 0, 100, 20, 0, 0,
                           instance, 0);
}

static void typeInto(HWND edit, const char *text)
{
    for (; *text; text++)
        SendMessageA(edit, WM_CHAR, (WPARAM)(unsigned char)*text, 0);
}

static void textOf(HWND edit, char *out, int cap)
{
    out[0] = '\0';
    GetWindowTextA(edit, out, cap);
}

int main(void)
{
    HINSTANCE instance = GetModuleHandleA(0);
    say("register", Window::registerClass(instance, CLASS_NAME, 0), CLASS_NAME);
    say("register-again-is-fine",
        Window::registerClass(instance, CLASS_NAME, 0), "already exists");

    /* --- Window --------------------------------------------------------- */
    {
        Probe p(1);
        bool made = p.make(instance);
        say("create", made && p.valid() && IsWindow(p.hwnd()), "");
        say("wm-create-sees-hwnd", p.m_createSawHwnd, "before create returns");

        HWND h = p.hwnd();
        p.destroy();
        say("destroy-runs-handle",
            p.m_sawDestroy && p.m_sawNcDestroy && p.m_destroyed,
            "WM_DESTROY, WM_NCDESTROY, onDestroyed");
        say("destroy-forgets-handle", !p.valid() && !IsWindow(h), "");
        p.destroy();
        say("destroy-twice-is-safe", !p.valid(), "");
    }
    {
        /* Destroyed from outside: the object must notice and not destroy a
         * recycled handle later. */
        Probe p(2);
        p.make(instance);
        DestroyWindow(p.hwnd());
        say("outside-destroy-noticed", !p.valid() && p.m_destroyed, "");
    }
    {
        Probe *p = new Probe(3);
        p->make(instance);
        HWND h = p->hwnd();
        delete p;
        say("delete-destroys-window", !IsWindow(h), "");
    }
    {
        Probe p(4, true);
        bool made = p.make(instance);
        say("failed-wm-create", !made && !p.valid(), "WM_CREATE returned -1");
    }
    {
        /* Two objects of one class each get their own messages: no
         * s_instance. */
        Probe a(10), b(20);
        a.make(instance);
        b.make(instance);
        LRESULT ra = SendMessageA(a.hwnd(), WM_CHECK_WHO, 0, 0);
        LRESULT rb = SendMessageA(b.hwnd(), WM_CHECK_WHO, 0, 0);
        char detail[32];
        rc_strf(detail, sizeof detail, "%ld %ld", (long)ra, (long)rb);
        say("two-instances-one-class", ra == 10 && rb == 20, detail);
    }

    /* --- SubclassedWindow ---------------------------------------------- */
    {
        NoLetter a('x'), b('y');
        HWND ea = makeEdit(instance), eb = makeEdit(instance);
        bool attached = a.attach(ea) && b.attach(eb);
        say("attach", attached && a.hwnd() == ea, "");
        say("attach-twice-refused", !a.attach(eb), "eb already has b");

        char ta[16], tb[16];
        typeInto(ea, "axy");
        typeInto(eb, "axy");
        textOf(ea, ta, sizeof ta);
        textOf(eb, tb, sizeof tb);
        say("two-subclasses-independent",
            strcmp(ta, "ay") == 0 && strcmp(tb, "ax") == 0, ta);

        HWND back = a.detach();
        typeInto(ea, "x");
        textOf(ea, ta, sizeof ta);
        say("detach-restores", back == ea && !a.valid() &&
            strcmp(ta, "ayx") == 0, ta);
        DestroyWindow(ea);

        /* Destroyed from outside: the subclass ends itself. */
        DestroyWindow(eb);
        say("subclass-outside-destroy", !b.valid() && b.m_destroyed, "");
    }
    {
        NoLetter *c = new NoLetter('z');
        HWND ec = makeEdit(instance);
        c->attach(ec);
        delete c;
        say("delete-destroys-control", !IsWindow(ec), "");
    }
    return 0;
}
