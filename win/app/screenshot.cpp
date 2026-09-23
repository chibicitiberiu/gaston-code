/* screenshot.cpp -- captures full desktop screen into a 24bpp TrueColor BMP.
 *
 * Uses 16-scanline strips with GetDIBits to keep memory consumption bounded (~38 KB)
 * and writes directly to disk.
 */
#include "app/screenshot.h"
#include "core/log.h"
#include <string.h>

int rc_screenshot_capture(const char *dest_bmp_path, DWORD *out_size)
{
    if (!dest_bmp_path || !*dest_bmp_path) return 0;

    HDC hdc_screen = GetDC(NULL);
    if (!hdc_screen) {
        rc_log("screenshot: GetDC failed");
        return 0;
    }

    int screen_w = GetSystemMetrics(SM_CXSCREEN);
    int screen_h = GetSystemMetrics(SM_CYSCREEN);
    if (screen_w <= 0 || screen_h <= 0) {
        rc_log("screenshot: invalid screen dimensions %dx%d", screen_w, screen_h);
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    /* 24bpp rows are 3 bytes per pixel, padded to 4-byte DWORD alignment */
    DWORD row_stride = ((DWORD)(screen_w * 3 + 3) / 4) * 4;
    DWORD image_bytes = row_stride * (DWORD)screen_h;

    BITMAPFILEHEADER bfh;
    BITMAPINFOHEADER bih;
    memset(&bfh, 0, sizeof(bfh));
    memset(&bih, 0, sizeof(bih));

    bfh.bfType = 0x4D42; /* 'BM' */
    bfh.bfOffBits = sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER);
    bfh.bfSize = bfh.bfOffBits + image_bytes;

    bih.biSize = sizeof(BITMAPINFOHEADER);
    bih.biWidth = screen_w;
    bih.biHeight = screen_h; /* Positive: standard bottom-up BMP */
    bih.biPlanes = 1;
    bih.biBitCount = 24;
    bih.biCompression = BI_RGB;
    bih.biSizeImage = image_bytes;

    HANDLE hf = CreateFileA(dest_bmp_path, GENERIC_WRITE, 0, NULL,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hf == INVALID_HANDLE_VALUE) {
        rc_log("screenshot: CreateFileA(%s) failed (GetLastError=%lu)",
               dest_bmp_path, (unsigned long)GetLastError());
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    DWORD written = 0;
    if (!WriteFile(hf, &bfh, sizeof(bfh), &written, NULL) || written != sizeof(bfh) ||
        !WriteFile(hf, &bih, sizeof(bih), &written, NULL) || written != sizeof(bih)) {
        rc_log("screenshot: failed to write BMP headers");
        CloseHandle(hf);
        DeleteFileA(dest_bmp_path);
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    HDC hdc_mem = CreateCompatibleDC(hdc_screen);
    if (!hdc_mem) {
        rc_log("screenshot: CreateCompatibleDC failed");
        CloseHandle(hf);
        DeleteFileA(dest_bmp_path);
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    const int STRIP_H = 16;
    HBITMAP hbm_strip = CreateCompatibleBitmap(hdc_screen, screen_w, STRIP_H);
    if (!hbm_strip) {
        rc_log("screenshot: CreateCompatibleBitmap failed");
        DeleteDC(hdc_mem);
        CloseHandle(hf);
        DeleteFileA(dest_bmp_path);
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    HGDIOBJ old_bmp = SelectObject(hdc_mem, hbm_strip);
    DWORD strip_buf_size = row_stride * STRIP_H;
    BYTE *strip_buf = (BYTE *)HeapAlloc(GetProcessHeap(), 0, strip_buf_size);
    if (!strip_buf) {
        rc_log("screenshot: failed to allocate strip buffer (%lu bytes)", (unsigned long)strip_buf_size);
        SelectObject(hdc_mem, old_bmp);
        DeleteObject(hbm_strip);
        DeleteDC(hdc_mem);
        CloseHandle(hf);
        DeleteFileA(dest_bmp_path);
        ReleaseDC(NULL, hdc_screen);
        return 0;
    }

    /* Bottom-up BMP: write scanlines from bottom of the screen to the top */
    int y_remaining = screen_h;
    int success = 1;

    while (y_remaining > 0) {
        int cur_strip_h = (y_remaining >= STRIP_H) ? STRIP_H : y_remaining;
        int screen_y = y_remaining - cur_strip_h;

        if (!BitBlt(hdc_mem, 0, 0, screen_w, cur_strip_h, hdc_screen, 0, screen_y, SRCCOPY)) {
            rc_log("screenshot: BitBlt failed at y=%d", screen_y);
            success = 0;
            break;
        }

        BITMAPINFO bi;
        memset(&bi, 0, sizeof(bi));
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bi.bmiHeader.biWidth = screen_w;
        bi.bmiHeader.biHeight = cur_strip_h; /* Positive: bottom-up */
        bi.bmiHeader.biPlanes = 1;
        bi.bmiHeader.biBitCount = 24;
        bi.bmiHeader.biCompression = BI_RGB;

        int lines = GetDIBits(hdc_mem, hbm_strip, 0, cur_strip_h, strip_buf, &bi, DIB_RGB_COLORS);
        if (lines != cur_strip_h) {
            rc_log("screenshot: GetDIBits returned %d, expected %d", lines, cur_strip_h);
            success = 0;
            break;
        }

        DWORD bytes_to_write = row_stride * (DWORD)cur_strip_h;
        if (!WriteFile(hf, strip_buf, bytes_to_write, &written, NULL) || written != bytes_to_write) {
            rc_log("screenshot: WriteFile failed for strip");
            success = 0;
            break;
        }

        y_remaining -= cur_strip_h;
    }

    HeapFree(GetProcessHeap(), 0, strip_buf);
    SelectObject(hdc_mem, old_bmp);
    DeleteObject(hbm_strip);
    DeleteDC(hdc_mem);
    ReleaseDC(NULL, hdc_screen);
    CloseHandle(hf);

    if (!success) {
        DeleteFileA(dest_bmp_path);
        return 0;
    }

    if (out_size) *out_size = bfh.bfSize;
    rc_log("screenshot: captured %dx%d (%lu bytes) to %s",
           screen_w, screen_h, (unsigned long)bfh.bfSize, dest_bmp_path);
    return 1;
}
