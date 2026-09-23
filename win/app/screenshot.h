/* screenshot.h -- captures full desktop screen into a 24bpp TrueColor BMP.
 *
 * Streams in 16-scanline strips directly to disk to minimize RAM (~38 KB heap).
 */
#ifndef RC_SCREENSHOT_H
#define RC_SCREENSHOT_H

#include <windows.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Captures the entire desktop display into dest_bmp_path as a 24bpp TrueColor BMP.
 * out_size (optional) receives the resulting file size in bytes.
 * Returns 1 on success, 0 on failure.
 */
int rc_screenshot_capture(const char *dest_bmp_path, DWORD *out_size);

#ifdef __cplusplus
}
#endif

#endif
