/* shm.h -- the backchannel between the client and code inside a target.
 *
 * ONE definition, included by the DLL, by the controller and by the probe.
 * There used to be two: `hookdll.c` held the struct and `probe.c` held a
 * copy of it, which is a layout that can drift silently while both sides
 * still compile. The field it would drift over decides whose import table
 * gets rewritten.
 *
 * Shape: a request block the controller writes, a response block the
 * collector fills, two events to hand the turn over, and a ring of text
 * the GDI patch appends to as it is drawn. Everything is fixed size.
 * Nothing in here grows: the section is mapped into a process that did not
 * ask for it, on a machine with 28 KB measured free after a turn.
 *
 * The header is first and is checked before anything else is read. A
 * section we do not recognise is one somebody else is writing, and the
 * rule is the one `bridge/caps.py` already applies to HELLO -- refuse
 * rather than guess.
 */
#ifndef RC_INPROC_SHM_H
#define RC_INPROC_SHM_H

#include <windows.h>

/* The version is IN THE NAME. CreateFileMapping against an existing name
 * returns the existing object and ignores the size you pass, so two
 * layouts under one name read each other's fields at the wrong offsets --
 * and an old injected copy can outlive the controller that made it,
 * because Explorer does not restart. Bump both together. */
#define RC_SHM_NAME  "GastonHookShm3"
#define RC_SHM_MAGIC 0x47484B33UL   /* 'GHK3' */
#define RC_SHM_VER   3

#define RC_SHM_REQ_EVENT  "GastonHookReq3"
#define RC_SHM_DONE_EVENT "GastonHookDone3"

/* What the collector was asked to do. */
#define RC_OP_NONE     0
#define RC_OP_COLLECT  1    /* walk the target's windows, read their content */
#define RC_OP_PING     2    /* are you there, and what can you do */

#define RC_RES_IDLE        0
#define RC_RES_WORKING     1
#define RC_RES_OK          2
#define RC_RES_TRUNCATED   3    /* hit a cap; what is here is real */
#define RC_RES_REFUSED     4    /* not our process, or not ready */
#define RC_RES_FAILED      5

/* Sized against the one thing that actually binds: this whole structure is
 * mapped into the target, so every number here is memory taken from a
 * program that did not consent to lending it. 48 nodes covers a dialog and
 * most of a list box page; a caller that wants more asks again with a
 * different root. */
#define RC_IN_NODES    48
#define RC_IN_TEXT     96
#define RC_IN_CLASS    32
#define RC_IN_ITEMS    64      /* list/tree rows per response */
#define RC_IN_ITEMLEN  64

#define RC_TEXTN       48      /* GDI draw ring */
#define RC_TEXTLEN     56
#define RC_MAXPIDS     16

/* One window, as seen from INSIDE its own process.
 *
 * The fields that are not in the out-of-process record are the whole point
 * of being here: `checked` and `sel` come from BM_GETCHECK and CB_GETCURSEL,
 * which are a marshalled message each from outside, and the item list comes
 * from LB_GETTEXT / LVM_GETITEMTEXT / TVM_GETITEM, which are NOT MARSHALLED
 * AT ALL on 9x at any clock speed. That is the argument for this tier, and
 * unlike the timing one it depends on no measurement. */
typedef struct {
    DWORD hwnd;                 /* target-local; never leaves the guest */
    DWORD parent;
    long  depth;
    long  ctrlId;
    DWORD style;
    long  left, top, right, bottom;
    long  checked;              /* BM_GETCHECK, -1 when not a button */
    long  sel;                  /* CB/LB_GETCURSEL, -1 when not a list */
    long  itemCount;            /* rows the control says it has */
    long  itemFirst;            /* index into RcInShm::items, -1 for none */
    long  itemN;                /* how many of them fitted */
    char  cls[RC_IN_CLASS];
    char  text[RC_IN_TEXT];
} RcInNode;

typedef struct {
    char text[RC_IN_ITEMLEN];
} RcInItem;

/* One captured draw. A ring, so a chatty application overwrites its own
 * old entries instead of growing. */
typedef struct {
    DWORD pid;
    long  x, y;
    char  text[RC_TEXTLEN];
} RcDraw;

typedef struct {
    /* --- header: read and checked before anything below it -------- */
    DWORD magic;
    DWORD version;
    DWORD cb;                   /* sizeof(RcInShm) as the writer knew it */
    DWORD generation;           /* stamped per run; counters zeroed with it */

    /* --- who may be touched --------------------------------------- */
    DWORD patch_pid;            /* 0 means nobody, and nobody is the default */
    char  patch_exe[32];        /* and the basename must agree: 9x reuses pids */

    /* --- request (controller writes, collector reads) -------------- */
    long  op;
    DWORD seq;                  /* bumped per request; the collector echoes it */
    DWORD root;                 /* HWND to walk, 0 for every top-level */
    long  maxNodes;
    long  wantItems;            /* read list/tree contents, which is the point */
    DWORD deadlineMs;

    /* --- response (collector writes, controller reads) ------------- */
    long  result;
    DWORD ackSeq;
    long  nodeN;
    long  itemN;
    long  elapsedMs;

    /* --- evidence, zeroed per run ---------------------------------- */
    long  calls;
    long  attaches;
    long  pids_n;
    DWORD pids[RC_MAXPIDS];
    long  patched;              /* modules whose IAT we rewrote */
    long  draws;
    long  ring_next;

    RcInNode nodes[RC_IN_NODES];
    RcInItem items[RC_IN_ITEMS];
    RcDraw   ring[RC_TEXTN];
} RcInShm;

#endif
