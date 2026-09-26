#include "ui/uirec.h"
#include "core/str.h"

#include <string.h>

/* The variable-length tail, in a fixed order the parser relies on. Seven
 * fields because that is every string a record can carry; an absent one is
 * length zero and costs two bytes in the head. */
struct Tail
{
    const char *p[7];
    unsigned    n[7];
};

static unsigned emit(char *out, unsigned cap, char src,
                     const char *path, const char *parent,
                     long ctrl, long child, const RECT &r, unsigned flags,
                     const Tail &t)
{
    char head[192];
    unsigned hn, total = 0;
    int i;

    if (!out || !cap || !path)
        return 0;
    if (!parent)
        parent = "-";
    /* An empty path token would make the head ambiguous: the parser splits
     * on spaces and two in a row would shift every field after it. */
    if (!path[0])
        path = "-";

    for (i = 0; i < 7; i++)
        total += t.n[i];

    rc_strf(head, sizeof(head),
            "%c %s %s %ld %ld %ld %ld %ld %ld %u %u %u %u %u %u %u %u ",
            src, path, parent, ctrl, child,
            (long)r.left, (long)r.top, (long)r.right, (long)r.bottom,
            flags,
            t.n[0], t.n[1], t.n[2], t.n[3], t.n[4], t.n[5], t.n[6]);
    hn = (unsigned)strlen(head);

    /* Refuse, never clamp. A short row is not detectable downstream: the
     * lengths stop matching the bytes and the parser reads the NEXT
     * record's fields as this one's. */
    if (hn + total + 1 > cap)
        return 0;

    memcpy(out, head, hn);
    total = hn;
    for (i = 0; i < 7; i++)
    {
        if (t.n[i])
            memcpy(out + total, t.p[i], t.n[i]);
        total += t.n[i];
    }
    out[total] = '\n';
    return total + 1;
}

static void put(Tail &t, int i, const char *s, unsigned cap)
{
    unsigned n;
    if (!s)
        s = "";
    /* strnlen: `Node::text` is a fixed buffer and a WM_GETTEXT that filled
     * it exactly leaves no terminator to find. */
    for (n = 0; n < cap && s[n]; n++)
        ;
    t.p[i] = s;
    t.n[i] = n;
}

unsigned rc_uirec_window(char *out, unsigned cap, const Node &n,
                         const Enrichment *e)
{
    Tail t;
    unsigned flags = 0;

    put(t, 0, n.cls,  RC_CLASS_MAX);
    put(t, 1, n.text, RC_TEXT_MAX);
    put(t, 2, n.role, RC_ROLE_MAX);
    put(t, 3, e ? e->name   : "", RC_ENRICH_VAL);
    put(t, 4, e ? e->value  : "", RC_ENRICH_VAL);
    put(t, 5, e ? e->state  : "", RC_ENRICH_VAL);
    put(t, 6, e ? e->action : "", RC_ENRICH_VAL);

    if (n.visible)      flags |= RC_NF_VISIBLE;
    if (n.enabled)      flags |= RC_NF_ENABLED;
    if (n.focused)      flags |= RC_NF_FOCUSED;
    if (n.textTimedOut) flags |= RC_NF_TIMEDOUT;

    /* The parent is the path with its last component removed -- "0/3/1"
     * has parent "0/3" -- so it is carried rather than made the bridge's
     * problem to re-derive for every record. A root has none. */
    char parent[RC_PATH_MAX];
    {
        unsigned i, cut = 0;
        for (i = 0; i < RC_PATH_MAX && n.path[i]; i++)
            if (n.path[i] == '/')
                cut = i;
        if (cut)
        {
            memcpy(parent, n.path, cut);
            parent[cut] = '\0';
        }
        else
        {
            parent[0] = '-';
            parent[1] = '\0';
        }
    }

    /* Not n.hwnd. See the header: a handle that escapes can be sent back
     * after the window is gone and click whatever inherited it. */
    return emit(out, cap, RC_SRC_WINDOW, n.path, parent, (long)n.ctrlId, 0L,
                n.rect, flags, t);
}

unsigned rc_uirec_acc(char *out, unsigned cap, const AccNode &n,
                      const char *path, const char *parent)
{
    Tail t;
    RECT r = n.rect;
    unsigned flags = 0;

    if (!n.hasRect)
    {
        r.left = r.top = r.right = r.bottom = 0;
    }

    put(t, 0, "", 1);                       /* no class: it is not a window */
    put(t, 1, "", 1);                       /* no WM_GETTEXT either */
    put(t, 2, n.role,  RC_ENRICH_VAL);
    put(t, 3, n.name,  RC_ENRICH_VAL);
    put(t, 4, n.value, RC_ENRICH_VAL);
    put(t, 5, n.state, RC_ENRICH_VAL);
    put(t, 6, "", 1);

    /* An accessible element carries no visible/enabled bits of its own --
     * those live in `state`, which MSAA spells out. Marking it visible
     * would be inventing a fact. */
    if (strstr(n.state, "invisible") == 0 && n.state[0])
        flags |= RC_NF_VISIBLE;

    return emit(out, cap, RC_SRC_ACC, path, parent, 0L, n.childId, r, flags,
                t);
}
