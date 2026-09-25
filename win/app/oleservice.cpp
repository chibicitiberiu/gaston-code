#include "app/oleservice.h"

#include <ole2.h>
#include "core/log.h"

OleService::OleService(const char *title)
    : m_title(title), m_up(false)
{
}

const char *OleService::name() const
{
    return "ole";
}

bool OleService::init()
{
    if (FAILED(OleInitialize(NULL)))
    {
        rc_log("OleInitialize failed");
        MessageBoxA(NULL, "OLE could not start.", m_title,
                    MB_OK | MB_ICONERROR);
        return false;
    }
    m_up = true;
    return true;
}

bool OleService::join(DWORD timeoutMs)
{
    (void)timeoutMs;
    if (m_up)
        OleUninitialize();
    m_up = false;
    return true;
}
