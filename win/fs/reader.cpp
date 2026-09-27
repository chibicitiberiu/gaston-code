#include "fs/reader.h"

#include "fs/finddata.h"


FileReader::FileReader()
    : m_file(INVALID_HANDLE_VALUE), m_size(0), m_mtime(0), m_delivered(0)
{
}

bool FileReader::open(const char *full)
{
    FILETIME ft;

    if (!full || !*full) return false;

    m_file.reset(CreateFileA(full, GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL));
    if (!m_file.valid()) return false;

    m_size = GetFileSize(m_file.get(), NULL);
    if (GetFileTime(m_file.get(), NULL, NULL, &ft)) m_mtime = fsToUnix(ft);
    m_delivered = 0;
    return true;
}

ReadResult FileReader::readAll(ByteSink &sink, unsigned chunkBytes,
                               DWORD maxBytes)
{
    HeapPtr<char> buf;
    DWORD got = 0;

    if (!m_file.valid()) return READ_FAILED;
    if (!chunkBytes) return READ_FAILED;

    /* HeapAlloc rather than new[]: operator new throws bad_alloc
     * (core/heap.cpp), and this runs on the RPC worker, where the contract
     * every caller already implements is "unreadable", not an exception
     * unwinding through a frame handler. */
    buf.reset((char *)HeapAlloc(GetProcessHeap(), 0, chunkBytes));
    if (!buf.valid()) return READ_FAILED;

    for (;;)
    {
        unsigned want = chunkBytes;

        if (maxBytes)
        {
            if (m_delivered >= maxBytes)
            {
                /* Only truncated if there is actually more behind it. A cap
                 * that lands exactly on the end of the file is a complete
                 * read, and saying otherwise sends the caller looking for a
                 * rest that does not exist. */
                return m_size > m_delivered ? READ_TRUNCATED : READ_DONE;
            }
            if (maxBytes - m_delivered < want)
                want = (unsigned)(maxBytes - m_delivered);
        }

        if (!ReadFile(m_file.get(), buf.get(), want, &got, NULL))
            return READ_FAILED;
        if (!got) return READ_DONE;

        m_delivered += got;
        if (!sink.write(buf.get(), got)) return READ_SINK_STOPPED;
    }
}
