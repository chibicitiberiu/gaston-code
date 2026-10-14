#include "automation/shot.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <string.h>

ShotOptions::ShotOptions()
{
    window = NULL;
    rect.left = rect.top = rect.right = rect.bottom = 0;
    maxBpp = 16;
    /* 16 rows at 800 wide and 32bpp is 51,200 bytes. The knob exists so a
     * caller on a wider screen can come down, not so anyone can come up. */
    stripRows = 16;
}

Screenshot::Screenshot()
{
    m_w = m_h = m_bpp = 0;
    m_written = 0;
    m_err[0] = '\0';
}

static int clampBpp(int want, int screen)
{
    int v = want < screen ? want : screen;
    /* Only the depths a BMP can actually carry. Anything else silently
     * becomes a file nothing opens. */
    if (v >= 32) return 32;
    if (v >= 24) return 24;
    if (v >= 16) return 16;
    if (v >= 8)  return 8;
    return 4;
}

/* Which pixels this would read: a window's rect, an explicit rect, or the
 * whole screen. Factored out of capture so a caller can ask BEFORE it
 * captures -- the driving indicator has to be out of the frame, and it has
 * to be told which frame (automation/overlay.h). Unclipped, which is right
 * for "does the strip overlap this" and is why capture clips its own copy.
 */
bool Screenshot::targetRect(const ShotOptions &opt, RECT &r)
{
    if (opt.window)
        return IsWindow(opt.window) && GetWindowRect(opt.window, &r) != 0;
    if (opt.rect.right > opt.rect.left && opt.rect.bottom > opt.rect.top)
    {
        r = opt.rect;
        return true;
    }
    WindowDC screen(NULL);
    if (!screen.valid())
        return false;
    r.left = 0;
    r.top = 0;
    r.right = GetDeviceCaps(screen.get(), HORZRES);
    r.bottom = GetDeviceCaps(screen.get(), VERTRES);
    return true;
}

bool Screenshot::capture(const ShotOptions &opt, ByteSink &sink)
{
    m_written = 0;
    m_err[0] = '\0';

    WindowDC screen(NULL);             /* the whole desktop, released on exit */
    if (!screen.valid())
    {
        rc_strf(m_err, sizeof(m_err), "no screen DC");
        return false;
    }

    RECT r;
    if (!targetRect(opt, r))
    {
        rc_strf(m_err, sizeof(m_err), "window is gone");
        return false;
    }

    /* Clipped to the screen, because a window can be partly off it and
     * BitBlt from outside returns whatever happens to be there. */
    int sw = GetDeviceCaps(screen.get(), HORZRES);
    int sh = GetDeviceCaps(screen.get(), VERTRES);
    if (r.left < 0) r.left = 0;
    if (r.top < 0) r.top = 0;
    if (r.right > sw) r.right = sw;
    if (r.bottom > sh) r.bottom = sh;

    m_w = r.right - r.left;
    m_h = r.bottom - r.top;
    if (m_w <= 0 || m_h <= 0)
    {
        rc_strf(m_err, sizeof(m_err), "empty region");
        return false;
    }
    m_bpp = clampBpp(opt.maxBpp, GetDeviceCaps(screen.get(), BITSPIXEL));

    int rows = opt.stripRows > 0 ? opt.stripRows : 16;
    if (rows > m_h) rows = m_h;

    unsigned long stride = (((unsigned long)m_w * m_bpp + 31) / 32) * 4;
    unsigned long imageBytes = stride * (unsigned long)m_h;
    int palEntries = (m_bpp <= 8) ? (1 << m_bpp) : 0;
    unsigned long headerBytes = sizeof(BITMAPFILEHEADER)
                              + sizeof(BITMAPINFOHEADER)
                              + (unsigned long)palEntries * sizeof(RGBQUAD);

    MemoryDC mem(CreateCompatibleDC(screen.get()));
    GdiObj strip(CreateCompatibleBitmap(screen.get(), m_w, rows));
    if (!mem.valid() || !strip.valid())
    {
        rc_strf(m_err, sizeof(m_err), "no memory DC or bitmap");
        return false;
    }
    Selected chosen(mem.get(), strip.get());

    /* info carries the header AND the colour table GetDIBits fills in, so
     * it has to be one allocation of both. */
    HeapPtr<unsigned char> info((unsigned char *)HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY,
        sizeof(BITMAPINFOHEADER) + 256 * sizeof(RGBQUAD)));
    HeapPtr<unsigned char> band((unsigned char *)HeapAlloc(
        GetProcessHeap(), 0, stride * (unsigned long)rows));
    if (!info.valid() || !band.valid())
    {
        rc_strf(m_err, sizeof(m_err), "out of memory for a strip");
        return false;
    }
    BITMAPINFOHEADER *bi = (BITMAPINFOHEADER *)info.get();

    /* Capture the BOTTOM strip first and work up. A BMP with a positive
     * biHeight is stored bottom-up, so emitting strips in capture order
     * only produces a correct file if capture runs the same way. Taking
     * them top-down and writing them in order yields a picture in bands,
     * each one right and the whole thing upside down in pieces. */
    int y = m_h;
    bool first = true;
    while (y > 0)
    {
        int take = rows;
        if (take > y) take = y;
        int srcTop = r.top + (y - take);

        if (!BitBlt(mem.get(), 0, 0, m_w, take, screen.get(),
                    r.left, srcTop, SRCCOPY))
        {
            rc_strf(m_err, sizeof(m_err), "BitBlt failed");
            return false;
        }

        memset(info.get(), 0, sizeof(BITMAPINFOHEADER)
                              + 256 * sizeof(RGBQUAD));
        bi->biSize = sizeof(BITMAPINFOHEADER);
        bi->biWidth = m_w;
        bi->biHeight = take;
        bi->biPlanes = 1;
        bi->biBitCount = (WORD)m_bpp;
        bi->biCompression = BI_RGB;
        int got = GetDIBits(mem.get(), (HBITMAP)strip.get(), 0, take,
                            band.get(), (BITMAPINFO *)info.get(),
                            DIB_RGB_COLORS);
        if (got != take)
        {
            rc_strf(m_err, sizeof(m_err), "GetDIBits gave %d of %d rows",
                    got, take);
            return false;
        }

        if (first)
        {
            /* The headers go out now, with the palette GetDIBits has just
             * filled in -- which is why they cannot be written before the
             * first strip is taken. */
            BITMAPFILEHEADER bfh;
            memset(&bfh, 0, sizeof(bfh));
            bfh.bfType = 0x4D42;               /* 'BM' */
            bfh.bfOffBits = headerBytes;
            bfh.bfSize = headerBytes + imageBytes;

            BITMAPINFOHEADER out = *bi;
            out.biHeight = m_h;                /* the whole picture */
            out.biSizeImage = imageBytes;
            out.biClrUsed = (DWORD)palEntries;

            if (!sink.write(&bfh, sizeof(bfh))
                || !sink.write(&out, sizeof(out)))
                return false;
            m_written += sizeof(bfh) + sizeof(out);
            if (palEntries)
            {
                unsigned long n = (unsigned long)palEntries * sizeof(RGBQUAD);
                if (!sink.write(info.get() + sizeof(BITMAPINFOHEADER),
                                (unsigned)n))
                    return false;
                m_written += (unsigned)n;
            }
            first = false;
        }

        if (!sink.write(band.get(), (unsigned)(stride * (unsigned long)take)))
            return false;
        m_written += (unsigned)(stride * (unsigned long)take);
        y -= take;
    }
    return true;
}
