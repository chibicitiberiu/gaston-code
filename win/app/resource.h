/* Resource identifiers, shared by gaston.rc and the C++ that loads them.
 *
 * This header is included by BOTH wrc and wpp386, so it must stay
 * preprocessor-only -- no types, no declarations. wrc runs a full C
 * preprocessor over a .rc file, but it knows nothing about C++.
 *
 * Ids start at 101 rather than 1: the first hundred are conventionally left
 * to the system, and more practically, LoadIcon(hInst, MAKEINTRESOURCE(1))
 * is what the shell itself uses to find an EXE's default icon, so a low id
 * collides with a lookup we do not control.
 */
#ifndef RC_RESOURCE_H
#define RC_RESOURCE_H

/* The application icon. The shell picks the LOWEST-numbered icon resource in
 * an EXE as the one it shows for the file, so if a second icon is ever added
 * it must be numbered above this one or the taskbar changes without anybody
 * touching this line. */
#define IDI_GASTON   101

/* The status bar's connection light. Three icons rather than one drawn three
 * ways: SB_SETICON takes an HICON, and swapping which handle a part holds is
 * cheaper than owner-drawing the part -- which would also have needed a
 * WM_DRAWITEM that this window's handler currently claims unconditionally
 * for the chat list, so an owner-drawn status part would render blank with
 * no error at all. */
#define IDI_NET_OK   102
#define IDI_NET_WARN 103
#define IDI_NET_BAD  104

/* One version, four places: the VERSIONINFO block wants it twice (binary and
 * string), Windows 98's file-properties tab reads the string, and the title
 * bar may want it later. Keeping the numeric and text forms adjacent is the
 * only defence against them drifting, which is the classic VERSIONINFO bug.
 */
#define RC_VER_MAJOR 0
#define RC_VER_MINOR 4
#define RC_VER_PATCH 0
#define RC_VER_STR   "0.4.0"

/* Menu commands. Here rather than in main.cpp because gaston.rc's
 * ACCELERATORS block has to name IDM_NEW, and wrc cannot see a #define in a
 * .cpp. IDM_MODEL0/IDM_EFFORT0 stay in app/optmenu.h: they are a runtime
 * RANGE owned by that module, and no resource refers to them. */
#define IDM_NEW      1099
#define IDM_EXPORT   1100
#define IDM_CLOSE    1101
#define IDM_HOST     1110
#define IDM_ABOUT    1120

/* Accelerator commands.
 *
 * These are DELIBERATELY not the control ids they end up driving. The page
 * buttons are ID_PREV/ID_NEXT in app/pagebar.h, and that header opens with
 *
 *     #ifndef __cplusplus
 *     #error "pagebar.h is C++; the page bar is a class now"
 *     #endif
 *
 * so gaston.rc cannot include it -- wrc preprocesses a .rc as C and does not
 * define __cplusplus. Duplicating the literals in two files would be worse:
 * the numbers would drift the first time someone renumbered one side. So the
 * accelerator table gets its own ids in its own range and wnd_proc routes
 * them, which also keeps "what a key does" in one readable place.
 */
#define IDA_PAGEPREV 130
#define IDA_PAGENEXT 131
#define IDA_REFRESH  132

/* The accelerator table itself. */
#define IDA_MAIN     140

/* Virtual key codes for the ACCELERATORS block.
 *
 * wrc does not include windows.h -- it preprocesses a .rc as C with only what
 * that file includes -- so VK_F5 and friends are simply undefined when the
 * resource compiler runs, and the table fails to parse. Guarded, so the C++
 * side keeps winuser.h's real definitions and these are only ever used by
 * wrc. Writing the numbers inline in gaston.rc would work too and would turn
 * a keyboard table into magic constants. */
#ifndef VK_PRIOR
#define VK_PRIOR 0x21
#endif
#ifndef VK_NEXT
#define VK_NEXT  0x22
#endif
#ifndef VK_F5
#define VK_F5    0x74
#endif

#endif
