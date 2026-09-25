/* The OLE apartment MSHTML needs, as a service.
 *
 * OleInitialize, not CoInitialize: in-place activation of an embedded control
 * needs the full OLE apartment. Registered first by the composition root, so
 * it is torn down last, after the window has released MSHTML.
 */
#ifndef RC_OLESERVICE_H
#define RC_OLESERVICE_H

#ifndef __cplusplus
#error "app/oleservice.h is C++"
#endif

#include "core/app.h"

class OleService : public Service
{
public:
    /* `title` captions the message box shown if OLE cannot start. */
    explicit OleService(const char *title);

    const char *name() const;
    bool init();
    bool join(DWORD timeoutMs);

private:
    const char *m_title;
    bool m_up;
};

#endif
