/* Volumes: which letters exist, what they are, and how much room is left.
 * libfs; libcore only.
 *
 * Split across two translation units on purpose. The client wants free space
 * for one volume and never enumerates drives, so `enumerateDrives` lives in
 * its own object and stays out of gaston.exe's image entirely.
 */
#ifndef RC_FS_DRIVES_H
#define RC_FS_DRIVES_H

#ifndef __cplusplus
#error "fs/drives.h is C++"
#endif

#include <windows.h>

/* Whether the volume answered, and whether anybody asked.
 *
 * A third state and not a bool: UNKNOWN is distinct from ABSENT. A removable
 * or a CD-ROM that `enumerateDrives` was not told to probe is UNKNOWN --
 * nobody looked -- which is a different fact from ABSENT, which means the
 * probe ran and there is nothing in the drive. Collapsing them to one false
 * is exactly the guess BACKLOG 20 asks not to make: it would report a floppy
 * with a disk in it as empty, having never looked. */
enum DriveMedia
{
    MEDIA_UNKNOWN = 0,
    MEDIA_ABSENT  = 1,
    MEDIA_PRESENT = 2
};

struct DriveInfo
{
    char  letter;         /* 'A'..'Z' */
    UINT  type;           /* GetDriveType: DRIVE_FIXED, DRIVE_CDROM, ... */
    char  label[64];      /* empty when there is no media OR no label */
    DWORD serial;
    DriveMedia media;
    ULARGE_INTEGER totalBytes;
    ULARGE_INTEGER freeBytes;

    DriveInfo();
};

class DriveSink
{
public:
    virtual ~DriveSink() {}
    virtual bool onDrive(const DriveInfo &info) = 0;   /* false stops */
};

/* Every letter GetLogicalDrives reports. GetLogicalDrives (a bitmask) and
 * GetDriveTypeA (a lookup in a table the OS already built at boot) touch
 * nothing and are always called -- the letter and the type are free.
 *
 * GetVolumeInformationA and the free/total space calls are a different
 * matter: against a floppy or a CD-ROM bay they move the mechanism. Against
 * `A:` with no disk in it, that is an audible seek and, on some drives, a
 * retry loop that takes seconds -- a side effect nobody asked for from a
 * read-only listing (BACKLOG 20). So they run unconditionally only for a
 * drive that cannot physically do this (fixed, network, RAM disk); for a
 * removable or a CD-ROM they run only when `probe` is true, and otherwise
 * the entry comes back with `media == MEDIA_UNKNOWN` and zeroed label/
 * serial/space -- true answers (the letter exists, this is its type), not
 * guesses.
 *
 * Sets SEM_FAILCRITICALERRORS for the length of the enumeration and restores
 * it afterwards. Without that, asking an empty floppy for its volume label
 * trips the 9x critical-error handler, which is a MODAL DIALOG on the person's
 * screen -- raised by a call they approved as read-only, and blocking whatever
 * thread is doing this until somebody walks over and dismisses it. That
 * guards the dialog, not the seek: SEM_FAILCRITICALERRORS does nothing about
 * the motor, which is why `probe` exists at all.
 */
void enumerateDrives(DriveSink &sink, bool probe = false);

/* Free bytes available to the caller on the volume `pathOrRoot` is on.
 *
 * 64 bits, and that is the point of it existing. GetDiskFreeSpaceA is clamped
 * by the OS to roughly 2GB of clusters on anything larger, so the older call
 * cannot answer for a modern disk -- GetDiskFreeSpaceExA is resolved through
 * GetProcAddress (it arrived in Win95 OSR2, so it is on this machine, but an
 * import that cannot resolve kills the image at load with no log, and a
 * free-space check is not worth the program refusing to start) and the older
 * call is the fallback rather than the answer.
 *
 * False if neither could answer.
 */
bool freeBytes64(const char *pathOrRoot, ULARGE_INTEGER &out);

/* The volume's size, same rules. Reported beside the free figure because
 * "1 MB free" means something different on a floppy and on a 20GB disk. */
bool totalBytes64(const char *pathOrRoot, ULARGE_INTEGER &out);

/* The same, flattened into a DWORD and SATURATED rather than wrapped, for the
 * one caller whose budget is genuinely 32 bits. A 4GB+ volume must not come
 * back as nearly full because the high word was dropped. */
unsigned long freeBytesSaturating(const char *pathOrRoot);

#endif
