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

/* One version, four places: the VERSIONINFO block wants it twice (binary and
 * string), Windows 98's file-properties tab reads the string, and the title
 * bar may want it later. Keeping the numeric and text forms adjacent is the
 * only defence against them drifting, which is the classic VERSIONINFO bug.
 */
#define RC_VER_MAJOR 0
#define RC_VER_MINOR 4
#define RC_VER_PATCH 0
#define RC_VER_STR   "0.4.0"

#endif
