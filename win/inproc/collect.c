/* collect.c -- reading a program's controls from inside it.
 *
 * Everything here runs in somebody else's process, on a thread borrowed
 * from their message dispatch. So the rules are harder than anywhere else
 * in this project:
 *
 *   no allocation, ever -- the response block is preallocated and bounded;
 *   no CRT, no COM, no oleacc on this path;
 *   no blocking call without a timeout;
 *   a cap and a deadline on every loop;
 *   and it must be brief, because while it runs their program is not
 *   repainting.
 *
 * What it can do that nothing outside can:
 *
 *   LB_GETTEXT, LVM_GETITEMTEXT and TVM_GETITEM take a POINTER, and across
 *   a process boundary on 9x nothing marshals it -- the receiving process
 *   writes into an address that means something else in our address space,
 *   or nothing at all. NT's answer is VirtualAllocEx in the target, which
 *   does not exist on 9x. Inside the target there is nothing to place: the
 *   pointer is already theirs. `docs/PLAN.md` recorded list contents as an
 *   MSAA-or-OCR problem, and that is true from outside and false from here.
 *
 * This is the argument for the whole in-process tier and it rests on no
 * timing measurement, which matters because the timing argument that was
 * once made for it has been retracted (docs/UIAUTOMATION.md).
 */
#include "inproc/shm.h"

/* Behind WINVER >= 0x0400 in places, and spelled out rather than trusted. */
#ifndef BM_GETCHECK
#define BM_GETCHECK 0x00F0
#endif
#ifndef CB_GETCURSEL
#define CB_GETCURSEL 0x0147
#endif
#ifndef CB_GETCOUNT
#define CB_GETCOUNT 0x0146
#endif
#ifndef CB_GETLBTEXT
#define CB_GETLBTEXT 0x0148
#endif
#ifndef CB_GETLBTEXTLEN
#define CB_GETLBTEXTLEN 0x0149
#endif
#ifndef LB_GETCURSEL
#define LB_GETCURSEL 0x0188
#endif
#ifndef LB_GETCOUNT
#define LB_GETCOUNT 0x018B
#endif
#ifndef LB_GETTEXT
#define LB_GETTEXT 0x0189
#endif
#ifndef LB_GETTEXTLEN
#define LB_GETTEXTLEN 0x018A
#endif

/* ListView and TreeView, which have no headers we can rely on here. */
#define RC_LVM_FIRST        0x1000
#define RC_LVM_GETITEMCOUNT (RC_LVM_FIRST + 4)
#define RC_LVM_GETITEMTEXTA (RC_LVM_FIRST + 45)
#define RC_TVM_FIRST        0x1100
#define RC_TVM_GETCOUNT     (RC_TVM_FIRST + 5)
#define RC_TVM_GETNEXTITEM  (RC_TVM_FIRST + 10)
#define RC_TVM_GETITEMA     (RC_TVM_FIRST + 12)
#define RC_TVGN_ROOT        0x0000
#define RC_TVGN_NEXT        0x0001
#define RC_TVGN_CHILD       0x0004
#define RC_TVIF_TEXT        0x0001

typedef struct {
    UINT   mask;
    int    iItem, iSubItem;
    UINT   state, stateMask;
    LPSTR  pszText;
    int    cchTextMax;
    int    iImage;
    LPARAM lParam;
} RC_LVITEMA;

typedef struct {
    UINT   mask;
    HANDLE hItem;
    UINT   state, stateMask;
    LPSTR  pszText;
    int    cchTextMax;
    int    iImage, iSelectedImage, cChildren;
    LPARAM lParam;
} RC_TVITEMA;

/* Every ask goes through here. SMTO_ABORTIFHUNG because we are inside the
 * process and a control that will not answer would otherwise stop its own
 * application while we wait for it. */
static LRESULT ask(HWND h, UINT msg, WPARAM wp, LPARAM lp, DWORD ms)
{
    DWORD_PTR res = 0;
    if (!SendMessageTimeoutA(h, msg, wp, lp, SMTO_ABORTIFHUNG, ms, &res))
        return 0;
    return (LRESULT)res;
}

static int same(const char *a, const char *b)
{
    return lstrcmpiA(a, b) == 0;
}

static void copy_bounded(char *dst, unsigned cap, const char *src)
{
    unsigned i;
    for (i = 0; i + 1 < cap && src[i]; i++)
        dst[i] = src[i];
    dst[i] = '\0';
}

/* Rows out of a list, a combo, a list view or a tree view.
 *
 * The bounded scratch buffer is what makes this safe: LB_GETTEXT writes as
 * many bytes as the item has and takes no length, so the only defence is
 * asking LB_GETTEXTLEN first and refusing anything that would not fit.
 * Getting that wrong is a stack overwrite inside somebody else's program.
 */
static long read_items(RcInShm *shm, HWND h, const char *cls, DWORD ms)
{
    char scratch[RC_IN_ITEMLEN * 4];
    long count = 0, got = 0, i;
    int isLb = same(cls, "ListBox"), isCb = same(cls, "ComboBox");
    int isLv = same(cls, "SysListView32"), isTv = same(cls, "SysTreeView32");

    if (!isLb && !isCb && !isLv && !isTv)
        return -1;

    if (isLb)      count = (long)ask(h, LB_GETCOUNT, 0, 0, ms);
    else if (isCb) count = (long)ask(h, CB_GETCOUNT, 0, 0, ms);
    else if (isLv) count = (long)ask(h, RC_LVM_GETITEMCOUNT, 0, 0, ms);
    else           count = (long)ask(h, RC_TVM_GETCOUNT, 0, 0, ms);
    if (count <= 0)
        return 0;

    for (i = 0; i < count; i++) {
        long slot = shm->itemN;
        if (slot >= RC_IN_ITEMS)
            break;
        scratch[0] = '\0';

        if (isLb || isCb) {
            /* LENGTH FIRST. These write the item and take no capacity, so
             * a long row would run off the end of `scratch` -- inside a
             * program that did not ask us to be here. */
            long need = (long)ask(h, isLb ? LB_GETTEXTLEN : CB_GETLBTEXTLEN,
                                  (WPARAM)i, 0, ms);
            if (need < 0 || need >= (long)sizeof(scratch))
                continue;
            ask(h, isLb ? LB_GETTEXT : CB_GETLBTEXT, (WPARAM)i,
                (LPARAM)scratch, ms);
        } else if (isLv) {
            RC_LVITEMA it;
            it.mask = 0; it.iItem = (int)i; it.iSubItem = 0;
            it.state = 0; it.stateMask = 0;
            it.pszText = scratch;
            it.cchTextMax = (int)sizeof(scratch);
            it.iImage = 0; it.lParam = 0;
            ask(h, RC_LVM_GETITEMTEXTA, (WPARAM)i, (LPARAM)&it, ms);
        } else {
            /* A tree is not indexed, so it is walked: root, then each
             * next sibling. Children are not descended into -- one level
             * is what fits, and a caller that wants more asks about the
             * item it cares about. */
            static HANDLE node;
            RC_TVITEMA it;
            if (i == 0)
                node = (HANDLE)ask(h, RC_TVM_GETNEXTITEM, RC_TVGN_ROOT, 0, ms);
            else
                node = (HANDLE)ask(h, RC_TVM_GETNEXTITEM, RC_TVGN_NEXT,
                                   (LPARAM)node, ms);
            if (!node)
                break;
            it.mask = RC_TVIF_TEXT;
            it.hItem = node;
            it.state = 0; it.stateMask = 0;
            it.pszText = scratch;
            it.cchTextMax = (int)sizeof(scratch);
            it.iImage = 0; it.iSelectedImage = 0; it.cChildren = 0;
            it.lParam = 0;
            ask(h, RC_TVM_GETITEMA, 0, (LPARAM)&it, ms);
        }

        scratch[sizeof(scratch) - 1] = '\0';
        copy_bounded(shm->items[slot].text, RC_IN_ITEMLEN, scratch);
        shm->itemN++;
        got++;
    }
    return got;
}

static void fill_node(RcInShm *shm, RcInNode *n, HWND h, long depth, DWORD ms)
{
    RECT r;
    long items;

    n->hwnd = (DWORD)(DWORD_PTR)h;
    n->parent = (DWORD)(DWORD_PTR)GetParent(h);
    n->depth = depth;
    n->ctrlId = GetDlgCtrlID(h);
    n->style = (DWORD)GetWindowLongA(h, GWL_STYLE);
    n->checked = -1;
    n->sel = -1;
    n->itemCount = 0;
    n->itemFirst = -1;
    n->itemN = 0;

    r.left = r.top = r.right = r.bottom = 0;
    GetWindowRect(h, &r);
    n->left = r.left; n->top = r.top;
    n->right = r.right; n->bottom = r.bottom;

    n->cls[0] = '\0';
    GetClassNameA(h, n->cls, RC_IN_CLASS);
    n->text[0] = '\0';
    /* Direct, because we are inside: no marshalling, no boundary. */
    ask(h, WM_GETTEXT, (WPARAM)RC_IN_TEXT, (LPARAM)n->text, ms);
    n->text[RC_IN_TEXT - 1] = '\0';

    if (same(n->cls, "Button"))
        n->checked = (long)ask(h, BM_GETCHECK, 0, 0, ms);
    if (same(n->cls, "ComboBox"))
        n->sel = (long)ask(h, CB_GETCURSEL, 0, 0, ms);
    else if (same(n->cls, "ListBox"))
        n->sel = (long)ask(h, LB_GETCURSEL, 0, 0, ms);

    if (shm->wantItems) {
        long first = shm->itemN;
        items = read_items(shm, h, n->cls, ms);
        if (items > 0) {
            n->itemFirst = first;
            n->itemN = items;
            n->itemCount = items;
        }
    }
}

/* Depth-first, capped and deadline-bounded. Iterative would be safer still
 * but the depth cap is what actually bounds it, and a Win32 control tree
 * that is 16 deep does not exist. */
static void walk(RcInShm *shm, HWND parent, long depth, DWORD until, DWORD ms)
{
    HWND c;
    if (depth > 16)
        return;
    for (c = GetWindow(parent, GW_CHILD); c; c = GetWindow(c, GW_HWNDNEXT)) {
        if (shm->nodeN >= RC_IN_NODES) {
            shm->result = RC_RES_TRUNCATED;
            return;
        }
        if ((long)(GetTickCount() - until) >= 0) {
            shm->result = RC_RES_TRUNCATED;
            return;
        }
        fill_node(shm, &shm->nodes[shm->nodeN], c, depth, ms);
        shm->nodeN++;
        walk(shm, c, depth + 1, until, ms);
        if (shm->result == RC_RES_TRUNCATED)
            return;
    }
}

/* The whole request, start to finish. Called on the target's own thread. */
void rc_inproc_collect(RcInShm *shm)
{
    DWORD t0 = GetTickCount();
    DWORD ms = 100;
    DWORD until;

    if (!shm)
        return;
    shm->result = RC_RES_WORKING;
    shm->nodeN = 0;
    shm->itemN = 0;

    until = t0 + (shm->deadlineMs ? shm->deadlineMs : 2000);
    /* A per-message budget well under the whole one: we are standing in
     * the way of this program's repaint, and one stuck control must not
     * spend the entire deadline. */
    if (shm->deadlineMs && shm->deadlineMs < 400)
        ms = 40;

    if (shm->root) {
        HWND root = (HWND)(DWORD_PTR)shm->root;
        if (IsWindow(root)) {
            fill_node(shm, &shm->nodes[0], root, 0, ms);
            shm->nodeN = 1;
            walk(shm, root, 1, until, ms);
        }
    } else {
        /* Every top-level window OF THIS PROCESS. GW_HWNDNEXT from the
         * desktop's first child, filtered by pid -- EnumWindows would do
         * as well and this avoids a callback on a borrowed thread. */
        HWND top = GetWindow(GetDesktopWindow(), GW_CHILD);
        DWORD self = GetCurrentProcessId();
        for (; top; top = GetWindow(top, GW_HWNDNEXT)) {
            DWORD pid = 0;
            if (shm->nodeN >= RC_IN_NODES) {
                shm->result = RC_RES_TRUNCATED;
                break;
            }
            if ((long)(GetTickCount() - until) >= 0) {
                shm->result = RC_RES_TRUNCATED;
                break;
            }
            GetWindowThreadProcessId(top, &pid);
            if (pid != self)
                continue;
            fill_node(shm, &shm->nodes[shm->nodeN], top, 0, ms);
            shm->nodeN++;
            walk(shm, top, 1, until, ms);
            if (shm->result == RC_RES_TRUNCATED)
                break;
        }
    }

    shm->elapsedMs = (long)(GetTickCount() - t0);
    if (shm->result != RC_RES_TRUNCATED)
        shm->result = RC_RES_OK;
}
