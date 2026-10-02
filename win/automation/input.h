/* input.h -- clicking and typing at another program.
 *
 * Everything here can go wrong in a way that is somebody else's problem, so
 * the refusals matter more than the actions.
 *
 * `SendInput` exists on 98 but not on 95 or NT4 before SP3, so it is
 * resolved with GetProcAddress and preferred when found: it delivers a
 * batch atomically, which matters on a machine a person may also be
 * touching. `keybd_event`/`mouse_event` are the fallback and are what
 * REMOTE.md assumed would be the only path.
 */
#ifndef RC_AUTOMATION_INPUT_H
#define RC_AUTOMATION_INPUT_H

#include <windows.h>

enum InputResult
{
    INPUT_OK = 0,
    INPUT_REFUSED_SELF,     /* the target belongs to this process */
    INPUT_NO_WINDOW,        /* not a window, or gone */
    INPUT_NOT_FOREGROUND,   /* focus would not move; we did not type */
    INPUT_OCCLUDED,         /* something else is on top of the point */
    /* A `[Name]` in the text that is not a key this knows. NOTHING WAS
     * TYPED: the whole string is checked before the first keystroke, because
     * a string half delivered into somebody's text box is worse than one
     * refused. Before this existed `[Delete]` was typed as eight characters
     * and reported as success -- measured in a live session, 2026-10-02. */
    INPUT_BAD_KEY,
    INPUT_FAILED,
    /* A character no key on the target's layout produces and that Alt+Numpad
     * cannot reach either (a control character). Nothing was typed. */
    INPUT_BAD_CHAR,
    /* The foreground left the window the caller said it was typing into.
     * Checked before every keystroke; nothing more was sent. */
    INPUT_FOCUS_LOST
};

enum MouseButton { BTN_LEFT = 0, BTN_RIGHT, BTN_MIDDLE };

/* One key by its OS-neutral NAME, resolved to this OS's codes.
 *
 * The names are a contract with the bridge (bridge/agent/keynames.py,
 * KEYS_V1): append-only, never renamed. The VK alone is not enough on 9x,
 * which cannot address the right-hand Shift, Ctrl or Alt by VK at all --
 * the side is the SCAN CODE (0x36 for right Shift) or the E0 extended flag
 * (right Ctrl, right Alt), and AltGr is Ctrl and Alt together. */
struct KeySpec
{
    BYTE vk;
    BYTE scan;          /* 0: MapVirtualKey's */
    bool extended;
    bool altGr;         /* sent as Ctrl+right Alt */
};

class Input
{
public:
    /* How long to keep looking after raising a covered window, and how
     * often. The raise returns before the z-order has settled. */
    enum { RAISE_SETTLE_MS = 600, RAISE_STEP_MS = 50 };

    Input();

    /* Try to make the foreground lock stop flashing a taskbar button
     * instead of raising a window. Measured at 15000ms on Win98 SE, and
     * setting it to 0 is accepted and reads back as 0. Call once at start.
     * Returns the value that was there, so a caller can put it back --
     * leaving it at 0 is a change to somebody's machine that outlives us.
     */
    static DWORD relaxForegroundLock();
    static void  restoreForegroundLock(DWORD previous);

    /* True if this window belongs to us. Compares the PROCESS, not the
     * HWND: a child control is a different HWND on the same thread, and an
     * HWND comparison lets the agent type into our own input box in a
     * self-feeding loop. */
    static bool isOurs(HWND target);

    /* Bring `target` forward and give it focus, attaching to its input
     * queue only if it will not come forward on its own -- and DETACHING
     * afterwards, or this thread permanently inherits the target's
     * input-queue state. Verifies with GetForegroundWindow and reports
     * INPUT_NOT_FOREGROUND rather than typing somewhere unintended. */
    InputResult focus(HWND target);

private:
    void noteBlocker(HWND under);
    char m_blocker[128];

public:

    /* Click at a screen point. After moving, WindowFromPoint must be the
     * window that was asked for; if something is on top, the window is
     * RAISED and the point re-checked, and only then is the click refused.
     * Raising is what a person's click would have done anyway, and without
     * it the refusal named an action nothing implemented (BACKLOG 16). */
    /* `checkOnly`: move, raise and verify, and stop short of the click --
     * for a caller that presses its own button. */
    InputResult clickAt(HWND expect, int x, int y, bool checkOnly = false);

    /* After INPUT_OCCLUDED, the class and title of whatever is on top --
     * empty at any other time. Naming it is the difference between a model
     * retrying blindly and a model knowing it is looking at our own window
     * or a dialog it has not read yet. */
    const char *blocker() const { return m_blocker; }

    /* The centre of `target`'s rect, then clickAt. One execution path, not
     * two: a control reference resolves to a point and then performs the
     * same real click a coordinate would. */
    InputResult clickWindow(HWND target);

    /* Characters, and named keys in square brackets. Ctrl+Alt+Del is
     * refused: on 9x the VMM owns it at ring 0 so this probably cannot
     * synthesise it anyway, and the cost of being wrong is a reboot.
     *
     *     Hello[Tab]world[Enter]
     *     [Ctrl+A][Delete]              -- clear a text box
     *     [Alt+F4]  [Shift+Home]  [Ctrl+Shift+End]
     *     [[                            -- a literal '['
     *
     * Names are matched without regard to case and listed in KEY_NAMES
     * (input.cpp), which `tests/python/guest/test_guest_agreement.py` scrapes
     * so the bridge cannot advertise a key this does not have.
     *
     * AN UNKNOWN NAME IS A REFUSAL AND NOTHING IS TYPED -- `badKey` is then
     * filled with the offending token. A plain `[` that opens nothing this
     * recognises is also refused rather than typed, because the alternative
     * is what used to happen: the brackets went into the user's text box and
     * the call reported success. Pass NULL for `badKey` if the name is not
     * wanted. */
    /* `sent`, when given, receives the number of KEYSTROKES delivered -- not
     * the length of `text`. With named keys the two stopped being the same
     * thing: `[Enter]` is seven characters and one keypress, and the caller's
     * reply said "Typed 7 characters" (measured on real hardware,
     * 2026-10-02, where `[Down][Down][Down][Down][Down][Down][Enter]` was
     * reported as 43). */
    InputResult typeText(HWND target, const char *text,
                         char *badKey = 0, unsigned badKeySize = 0,
                         unsigned *sent = 0);
    InputResult pressKey(HWND target, int vk);

    bool usingSendInput() const { return m_sendInput != 0; }

    /* ---- the `input` step list's primitives ---------------------------
     *
     * These do NOT focus anything: the step runner decides where input is
     * meant to go and checks the foreground itself before each one. What
     * they do keep is the record of what is HELD, so that a step list that
     * fails, is cancelled or simply forgets an `up` never leaves a key down
     * on somebody's machine (releaseAll). */

    /* KEYS_V1, case-insensitive. False for a name this does not know. */
    static bool keyByName(const char *name, KeySpec &out);

    InputResult keyDown(const KeySpec &k);
    InputResult keyUp(const KeySpec &k);
    /* Down and up, with modifier bits (1 Shift, 2 Ctrl, 4 Alt) around it. */
    InputResult tapKey(const KeySpec &k, int mods);
    /* A CHORD on a character: `[Ctrl+S]`. A letter or digit is its VK
     * whatever the layout's shift state for it -- an accelerator for Ctrl+1
     * is registered against VK_1, and on AZERTY '1' needs Shift
     * (docs/measurements/keyboard-layouts-vm-20261002.txt). Anything else
     * goes through VkKeyScanEx on `hkl`, its own shift folded in. */
    InputResult tapChar(unsigned char c, int mods, HKL hkl);

    /* Characters, on the layout `hkl` -- the TARGET's, which is per thread
     * on 98. Per character: the key VkKeyScanEx names IF ToAsciiEx agrees it
     * produces exactly that character; a dead key followed by Space; else
     * Alt+Numpad 0nnn. `\n` is Enter, `\t` is Tab, `\r\n` is one Enter.
     * Checked whole before the first keystroke: INPUT_BAD_CHAR names the
     * offset in `badAt`. `expectTop`, when given, must stay the root of the
     * foreground window (or own it) before every character. */
    InputResult typeChars(const char *text, unsigned len, HKL hkl,
                          HWND expectTop, unsigned &sent, unsigned &badAt);

    /* The mouse. Screen pixels. A move with a button held is walked in
     * short steps, because a drag that jumps can skip DragDetect's
     * threshold test entirely. */
    void        moveTo(int x, int y);
    InputResult buttonDown(MouseButton b);
    InputResult buttonUp(MouseButton b);
    InputResult clickButtonAt(HWND expect, int x, int y, MouseButton b,
                              int count);

    /* WM_CLOSE, posted -- the polite way, which lets the program ask about
     * unsaved work. The caller waits and looks. */
    InputResult closeWindow(HWND top);

    /* Everything still down, released: keys in reverse of a sane order,
     * then the buttons. Safe to call when nothing is held. */
    void releaseAll();
    bool anyHeld() const;

    static HWND rootOf(HWND h);

private:
    void *m_sendInput;      /* PFN, NULL when this OS lacks it */
    void  tapVk(int vk, bool extended);
    void  sendKey(const KeySpec &k, bool up);
    void  altNumpad(unsigned char c);
    bool  ctrlAltHeld() const;

    /* What this object has pressed and not released. One bit per VK, one
     * more per VK for the extended flag it went down with, and the three
     * buttons. 70 bytes: a full table would be cheaper to scan, but this is
     * what release needs and nothing else. */
    BYTE  m_held[32];
    BYTE  m_heldExt[32];
    int   m_heldButtons;

    Input(const Input &);
    Input &operator=(const Input &);
};

#endif
