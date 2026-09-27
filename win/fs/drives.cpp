#include "fs/drives.h"

#include <string.h>

#include "core/rc_cpp.h"

DriveInfo::DriveInfo()
    : letter(0), type(DRIVE_UNKNOWN), serial(0), hasMedia(false)
{
    label[0] = '\0';
    totalBytes.QuadPart = 0;
    freeBytes.QuadPart = 0;
}

namespace {

/* SetErrorMode restored on the way out, whatever happens in between. There is
 * no guard for this in rc_cpp.h because this is the only place that needs it:
 * everything else on this machine either wants the critical-error handler or
 * never touches removable media. */
class QuietErrors
{
public:
    QuietErrors()
    {
        m_prev = SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX);
    }
    ~QuietErrors() { SetErrorMode(m_prev); }

private:
    QuietErrors(const QuietErrors &);
    QuietErrors &operator=(const QuietErrors &);
    UINT m_prev;
};

} /* namespace */

void enumerateDrives(DriveSink &sink)
{
    DWORD mask = GetLogicalDrives();
    QuietErrors quiet;
    int i;

    for (i = 0; i < 26; i++)
    {
        DriveInfo info;
        char root[8];

        if (!(mask & (1UL << i))) continue;

        root[0] = (char)('A' + i);
        root[1] = ':';
        root[2] = '\\';
        root[3] = '\0';

        info.letter = root[0];
        info.type = GetDriveTypeA(root);

        /* A removable with no media answers nothing, slowly, and used to
         * answer with a dialog. Its letter and its type are still true and
         * are still worth reporting -- "there is a floppy drive and it is
         * empty" is an answer. */
        {
            DWORD serial = 0, maxComp = 0, flags = 0;
            char label[64];
            label[0] = '\0';
            if (GetVolumeInformationA(root, label, (DWORD)sizeof(label),
                                      &serial, &maxComp, &flags, NULL, 0))
            {
                rc_strcpy(info.label, sizeof(info.label), label);
                info.serial = serial;
                info.hasMedia = true;
            }
        }

        {
            ULARGE_INTEGER freeb, total;
            if (freeBytes64(root, freeb)) info.freeBytes = freeb;
            if (totalBytes64(root, total)) info.totalBytes = total;
        }

        if (!sink.onDrive(info)) return;
    }
}
