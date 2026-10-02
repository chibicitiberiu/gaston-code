/* keyprobe -- how does Windows 98 produce a character on a layout that is
 * not US English?
 *
 * Not a check. It measures what the `input` tool's text steps will rely on,
 * none of which Wine can answer -- Wine's keyboard layouts are the host's:
 *
 *   1. VkKeyScanExA against a layout that is LOADED but not active: does it
 *      answer for that layout, and does it report AltGr as Ctrl+Alt (6)?
 *   2. ToAsciiEx as a dead-key detector: negative for `^` on French, `'` on
 *      US-International? And does asking leave the dead key PENDING in this
 *      thread's state, so the next real keystroke combines with it?
 *   3. Delivery, per strategy, into an EDIT control: VK plus modifiers;
 *      a dead key followed by Space; Alt+Numpad 0nnn (ANSI) -- with NumLock
 *      both on and off.
 *
 * Usage:  KEYPROBE [layout ...]      e.g. KEYPROBE 00000409 00000407
 * With no arguments it tries US, German, French and US-International.
 *
 * The edit control is OUR OWN, so this needs no second program: keystrokes
 * go to the foreground thread, and the foreground thread is this one. The
 * layout under test is activated for this thread with ActivateKeyboardLayout,
 * which is exactly the per-thread state the client has to read from the
 * TARGET's thread instead.
 *
 * Output: KEYPROBE.TXT beside the exe, and the console. Every line is one
 * fact, so the file can be committed as the measurement.
 */

#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "core/str.h"
#include "automation/input.h"

static FILE *g_out;

static void say(const char *fmt, ...)
{
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf(line, sizeof(line) - 1, fmt, ap);
    line[sizeof(line) - 1] = '\0';
    va_end(ap);
    fputs(line, stdout);
    fputc('\n', stdout);
    if (g_out)
    {
        fputs(line, g_out);
        fputc('\n', g_out);
        fflush(g_out);
    }
}

/* The characters every layout is asked about. Code page 1252 bytes, so the
 * file is plain ASCII escapes: a, Z, @, EURO (0x80), e-acute (0xE9), ^,
 * e-circumflex (0xEA), {, }, |, backslash, ~, `, ', ", u-umlaut (0xFC),
 * sharp s (0xDF), section (0xA7), 1, & */
static const char TEST_CHARS[] =
    "aZ@\x80\xE9^\xEA{}|\\~`'\"\xFC\xDF\xA7" "1&";

static const char *const DEFAULT_LAYOUTS[] =
    { "00000409", "00000407", "0000040C", "00020409" };

/* ---- the window under test -------------------------------------------- */

static HWND g_frame, g_edit;

static LRESULT CALLBACK frameProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_SETFOCUS && g_edit)
    {
        SetFocus(g_edit);
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

static bool makeWindow(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = frameProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    wc.lpszClassName = "KeyProbeFrame";
    if (!RegisterClassA(&wc))
        return false;
    g_frame = CreateWindowA("KeyProbeFrame", "keyprobe",
                            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                            40, 40, 560, 160, NULL, NULL, wc.hInstance, NULL);
    if (!g_frame)
        return false;
    g_edit = CreateWindowA("EDIT", "", WS_CHILD | WS_VISIBLE | WS_BORDER
                           | ES_AUTOHSCROLL, 8, 8, 530, 24, g_frame, NULL,
                           wc.hInstance, NULL);
    return g_edit != NULL;
}

/* Keystrokes are posted to the system queue and reach the edit only through
 * this thread's message loop, so the loop has to run between sends. */
static void pump(DWORD ms)
{
    DWORD until = GetTickCount() + ms;
    MSG msg;
    do
    {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
        {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        Sleep(5);
    } while ((long)(until - GetTickCount()) > 0);
}

static void tap(BYTE vk, DWORD extra)
{
    BYTE scan = (BYTE)MapVirtualKeyA(vk, 0);
    keybd_event(vk, scan, extra, 0);
    keybd_event(vk, scan, extra | KEYEVENTF_KEYUP, 0);
}

static void down(BYTE vk) { keybd_event(vk, (BYTE)MapVirtualKeyA(vk, 0), 0, 0); }
static void up(BYTE vk)
{
    keybd_event(vk, (BYTE)MapVirtualKeyA(vk, 0), KEYEVENTF_KEYUP, 0);
}

static bool foreground(void)
{
    SetForegroundWindow(g_frame);
    SetFocus(g_edit);
    pump(50);
    return GetForegroundWindow() == g_frame;
}

static void clearEdit(void)
{
    SetWindowTextA(g_edit, "");
    pump(20);
}

/* ---- 1 and 2: asking, without typing ---------------------------------- */

/* What ToAsciiEx says for `vk` with `mods` (VkKeyScan's 1/2/4 bits) held. */
static int asciiFor(HKL hkl, UINT vk, int mods, char *out)
{
    BYTE ks[256];
    WORD buf[2] = { 0, 0 };
    UINT scan = MapVirtualKeyExA(vk, 0, hkl);
    int r;
    memset(ks, 0, sizeof(ks));
    if (mods & 1) ks[VK_SHIFT] = 0x80;
    if (mods & 2) ks[VK_CONTROL] = 0x80;
    if (mods & 4) ks[VK_MENU] = 0x80;
    r = ToAsciiEx(vk, scan, ks, buf, 0, hkl);
    out[0] = (char)(buf[0] & 0xFF);
    out[1] = (char)(buf[1] & 0xFF);
    return r;
}

static void ask(HKL hkl, const char *layout)
{
    for (const char *p = TEST_CHARS; *p; p++)
    {
        unsigned char c = (unsigned char)*p;
        SHORT vs = VkKeyScanExA((CHAR)c, hkl);
        char got[2];
        if (vs == -1)
        {
            say("ask %s 0x%02X vk=none", layout, c);
            continue;
        }
        int vk = vs & 0xFF, mods = (vs >> 8) & 0xFF;
        int r = asciiFor(hkl, (UINT)vk, mods, got);
        /* A dead key leaves itself pending; asking again with Space is the
         * flush. Recorded, because whether this is needed is the question. */
        char flushed[2] = { 0, 0 };
        int rf = 0;
        if (r < 0)
            rf = asciiFor(hkl, VK_SPACE, 0, flushed);
        say("ask %s 0x%02X vk=0x%02X mods=%d toascii=%d out=0x%02X%s",
            layout, c, vk, mods, r, (unsigned char)got[0],
            r < 0 ? "" : "");
        if (r < 0)
            say("ask %s 0x%02X dead; space-flush toascii=%d out=0x%02X 0x%02X",
                layout, c, rf, (unsigned char)flushed[0],
                (unsigned char)flushed[1]);
    }
}

/* ---- 3: delivery ------------------------------------------------------- */

static void sendMods(int mods, bool press)
{
    if (press)
    {
        if (mods & 2) down(VK_CONTROL);
        if (mods & 4) down(VK_MENU);
        if (mods & 1) down(VK_SHIFT);
    }
    else
    {
        if (mods & 1) up(VK_SHIFT);
        if (mods & 4) up(VK_MENU);
        if (mods & 2) up(VK_CONTROL);
    }
}

/* Alt held, then the digits on the NUMERIC KEYPAD, then Alt released: the
 * leading 0 asks for the ANSI code page rather than the OEM one. */
static void altNumpad(unsigned char c)
{
    char digits[8];
    rc_strf(digits, sizeof(digits), "0%u", (unsigned)c);
    down(VK_MENU);
    for (const char *d = digits; *d; d++)
        tap((BYTE)(VK_NUMPAD0 + (*d - '0')), 0);
    up(VK_MENU);
}

enum Strategy { VIA_VK, VIA_ALTNUM };

static void deliverOne(HKL hkl, unsigned char c, Strategy how)
{
    if (how == VIA_ALTNUM)
    {
        altNumpad(c);
        return;
    }
    SHORT vs = VkKeyScanExA((CHAR)c, hkl);
    if (vs == -1)
    {
        altNumpad(c);
        return;
    }
    int vk = vs & 0xFF, mods = (vs >> 8) & 0xFF;
    char got[2];
    bool dead = asciiFor(hkl, (UINT)vk, mods, got) < 0;
    if (dead)
        asciiFor(hkl, VK_SPACE, 0, got);       /* flush what asking left */
    sendMods(mods, true);
    tap((BYTE)vk, 0);
    sendMods(mods, false);
    if (dead)
        tap(VK_SPACE, 0);
}

static void setNumLock(bool on)
{
    bool now = (GetKeyState(VK_NUMLOCK) & 1) != 0;
    if (now != on)
    {
        tap(VK_NUMLOCK, 0);
        pump(50);
    }
}

static void deliver(HKL hkl, const char *layout, Strategy how,
                    const char *label)
{
    char back[128];
    int ok = 0, n = 0;
    for (const char *p = TEST_CHARS; *p; p++)
    {
        unsigned char c = (unsigned char)*p;
        clearEdit();
        if (!foreground())
        {
            say("type %s %s 0x%02X NOT-FOREGROUND", layout, label, c);
            continue;
        }
        deliverOne(hkl, c, how);
        pump(120);
        back[0] = '\0';
        GetWindowTextA(g_edit, back, sizeof(back));
        bool same = back[0] == (char)c && back[1] == '\0';
        n++;
        if (same)
            ok++;
        say("type %s %s 0x%02X -> \"%s\" (%u bytes, first 0x%02X) %s",
            layout, label, c, back, (unsigned)strlen(back),
            (unsigned char)back[0], same ? "OK" : "WRONG");
    }
    say("type %s %s summary %d/%d", layout, label, ok, n);

    /* The whole string at once: a dead key that was asked about and not
     * flushed would show up HERE, combining with the next character. */
    clearEdit();
    if (foreground())
    {
        for (const char *p = TEST_CHARS; *p; p++)
        {
            deliverOne(hkl, (unsigned char)*p, how);
            pump(40);
        }
        pump(200);
        GetWindowTextA(g_edit, back, sizeof(back));
        say("type %s %s whole \"%s\" %s", layout, label, back,
            strcmp(back, TEST_CHARS) == 0 ? "OK" : "WRONG");
    }
}

/* Ctrl+digit on this layout: does VK_1 with Ctrl reach the window as
 * Ctrl+1 (WM_KEYDOWN VK_1 with Ctrl down) whatever the layout's shift state
 * for '1'? Recorded from VkKeyScanEx alone -- what a chord would send. */
static void chordFacts(HKL hkl, const char *layout)
{
    SHORT one = VkKeyScanExA('1', hkl);
    SHORT z = VkKeyScanExA('z', hkl);
    say("chord %s '1' vk=0x%02X mods=%d; 'z' vk=0x%02X mods=%d", layout,
        one & 0xFF, (one >> 8) & 0xFF, z & 0xFF, (z >> 8) & 0xFF);
}

int main(int argc, char **argv)
{
    char path[MAX_PATH];
    const char *const *layouts = DEFAULT_LAYOUTS;
    int layoutN = 4;
    DWORD lock;

    GetModuleFileNameA(NULL, path, sizeof(path));
    char *slash = strrchr(path, '\\');
    if (slash)
        rc_strf(slash + 1, (unsigned)(sizeof(path) - (slash + 1 - path)),
                "KEYPROBE.TXT");
    g_out = fopen(path, "w");

    if (argc > 1)
    {
        layouts = (const char *const *)(argv + 1);
        layoutN = argc - 1;
    }

    HKL original = GetKeyboardLayout(0);
    say("keyprobe: active layout at start %08lX, numlock %s",
        (unsigned long)(DWORD)(UINT_PTR)original,
        (GetKeyState(VK_NUMLOCK) & 1) ? "on" : "off");
    {
        HKL list[16];
        int n = GetKeyboardLayoutList(16, list);
        for (int i = 0; i < n; i++)
            say("installed %08lX", (unsigned long)(DWORD)(UINT_PTR)list[i]);
    }

    lock = Input::relaxForegroundLock();
    if (!makeWindow())
    {
        say("cannot create the window");
        return 1;
    }
    pump(300);

    for (int i = 0; i < layoutN; i++)
    {
        HKL hkl = LoadKeyboardLayoutA(layouts[i], 0);
        say("layout %s -> hkl %08lX", layouts[i],
            (unsigned long)(DWORD)(UINT_PTR)hkl);
        if (!hkl)
            continue;
        ask(hkl, layouts[i]);
        chordFacts(hkl, layouts[i]);
        ActivateKeyboardLayout(hkl, 0);
        pump(50);
        say("activated: thread layout now %08lX",
            (unsigned long)(DWORD)(UINT_PTR)GetKeyboardLayout(0));
        setNumLock(true);
        deliver(hkl, layouts[i], VIA_VK, "vk/numlock-on");
        deliver(hkl, layouts[i], VIA_ALTNUM, "altnum/numlock-on");
        setNumLock(false);
        deliver(hkl, layouts[i], VIA_ALTNUM, "altnum/numlock-off");
        setNumLock(true);
    }

    ActivateKeyboardLayout(original, 0);
    Input::restoreForegroundLock(lock);
    {
        SHORT s = GetAsyncKeyState(VK_SHIFT), c = GetAsyncKeyState(VK_CONTROL),
              a = GetAsyncKeyState(VK_MENU);
        say("stuck after: shift=%d ctrl=%d alt=%d", (s & 0x8000) != 0,
            (c & 0x8000) != 0, (a & 0x8000) != 0);
    }
    say("keyprobe: done");
    if (g_out)
        fclose(g_out);
    DestroyWindow(g_frame);
    return 0;
}
