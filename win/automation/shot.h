/* shot.h -- the screen, or one window of it, as a BMP on a sink.
 *
 * Streamed in strips, because a full frame is not affordable: a 307 KB
 * 8bpp full-screen DIB against 28 KB measured free after a turn. One strip
 * buffer exists at a time and the bytes leave as they are taken.
 *
 * Depth is min(screen_bpp, maxBpp). The screen can be anything from 4bpp to
 * 32bpp and the caller cannot know which, so one knob covers "a picture to
 * look at" and "just enough to drive the UI" without the wire format
 * depending on a setting somebody can change in Display Properties.
 *
 * The output is a real BMP -- file header, info header, palette when the
 * depth has one, rows -- so the bridge opens it with anything. Compression
 * is NOT done here: measured over four bands of a screen, deflate beat
 * GDI's own RLE at every depth where both exist, and deflate on raw 16bpp
 * beat RLE8 on quantised 8bpp outright. The wire codec does it.
 *
 * It did not, for as long as this comment has been here: UIDAT was not in
 * `rc_frame_compressible` (win/net/frameproto.cpp), so every screenshot the
 * client ever sent went out raw. Noticed 2026-10-03 while turning deflate on
 * for gremote and fixed in both. It is still the CODEC's decision per frame
 * -- `shouldCompress` declines when the link is faster than half what
 * deflate manages -- so on a LAN this changes nothing, which is the right
 * answer there.
 */
#ifndef RC_AUTOMATION_SHOT_H
#define RC_AUTOMATION_SHOT_H

#include <windows.h>

/* ByteSink moved to libcore: libfs streams files through the same
 * interface, and gremote links both libraries. */
#include "core/bytesink.h"

struct ShotOptions
{
    HWND window;      /* NULL: the screen */
    RECT rect;        /* used when window is NULL; all zero: everything */
    int  maxBpp;      /* 4, 8, 16, 24, 32 */
    int  stripRows;

    ShotOptions();
};

class Screenshot
{
public:
    Screenshot();

    bool capture(const ShotOptions &opt, ByteSink &sink);

    /* Which pixels `opt` names, before capturing them. For a caller that
     * has to clear the frame first -- see automation/overlay.h. */
    static bool targetRect(const ShotOptions &opt, RECT &out);

    int  width() const  { return m_w; }
    int  height() const { return m_h; }
    int  bpp() const    { return m_bpp; }
    unsigned bytesWritten() const { return m_written; }
    const char *error() const { return m_err; }

private:
    int      m_w, m_h, m_bpp;
    unsigned m_written;
    char     m_err[64];

    Screenshot(const Screenshot &);
    Screenshot &operator=(const Screenshot &);
};

#endif
