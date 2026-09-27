/* Reading a file once, handing the bytes to somebody. libfs; libcore only.
 *
 * ONE PASS IS THE WHOLE POINT. A 200MHz machine with a 1998 IDE platter
 * cannot afford to read a file twice, and several callers need to do two
 * things with the same bytes -- send them and checksum them, or checksum them
 * and classify them. So the bytes go to a ByteSink and whoever wants them
 * chains off that, rather than each caller opening the file again.
 *
 * The chunk buffer is on the heap, not in a frame. The RPC worker gets the
 * image's 64KB stack commit and do_scan already holds a ~62KB manifest buffer
 * when it calls down here; Open Watcom emits a plain `sub esp` for a large
 * frame with no stack probe, so the next push lands below the guard page and
 * the process dies with an invalid page fault. That bug was real and it looked
 * intermittent, because it took one extra `walk` frame to move the skip past
 * the guard.
 */
#ifndef RC_FS_READER_H
#define RC_FS_READER_H

#ifndef __cplusplus
#error "fs/reader.h is C++"
#endif

#include <windows.h>

#include "core/bytesink.h"
#include "core/rc_cpp.h"

enum ReadResult
{
    READ_DONE = 0,      /* end of file reached */
    READ_TRUNCATED,     /* stopped at maxBytes; there is more on disk */
    READ_SINK_STOPPED,  /* the sink said stop -- not an error */
    READ_FAILED         /* ReadFile failed; GetLastError says why */
};

class FileReader
{
public:
    FileReader();

    /* Opens for reading, sharing read AND write. A Win98 editor holds a
     * share-deny lock, and a file that cannot be read must be reported
     * unreadable rather than omitted -- omitting it reads as absent, which
     * becomes a tombstone, and the user's later edits are invisible forever. */
    bool open(const char *full);

    bool valid() const { return m_file.valid(); }
    DWORD size() const { return m_size; }

    /* Last-write time as unix seconds, local. Only ever compared against
     * timestamps from this same side: it is a cache key, never an argument
     * about which side is right. Zero if the file has none. */
    DWORD mtime() const { return m_mtime; }

    /* Read to the sink in `chunkBytes` pieces, stopping at `maxBytes` (0 for
     * no limit). `delivered()` is how much actually reached the sink. */
    ReadResult readAll(ByteSink &sink, unsigned chunkBytes, DWORD maxBytes);

    DWORD delivered() const { return m_delivered; }

private:
    FileReader(const FileReader &);
    FileReader &operator=(const FileReader &);

    Handle m_file;
    DWORD  m_size;
    DWORD  m_mtime;
    DWORD  m_delivered;
};

#endif
