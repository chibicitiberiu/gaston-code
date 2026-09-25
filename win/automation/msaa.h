/* msaa.h -- the enrichment tier, kept off the walk.
 *
 * MSAA is the only source for a checkbox's checked state, an edit's
 * contents where no message is marshalled, and a role that distinguishes a
 * push button from a radio button when the class name says "Button" for
 * both. It is also the expensive tier: measured at roughly the cost of
 * everything else in a walk put together, for a name on well under half the
 * nodes. So it is applied to the candidates a selector is deciding between,
 * never to a tree.
 *
 * Two things this gets right that cost a day to find:
 *
 *   OBJID_CLIENT, not OBJID_WINDOW. Asked for the window object, Win98
 *   reports role 9 -- ROLE_SYSTEM_WINDOW -- for every node on the desktop,
 *   with no value and no action anywhere, which reads exactly like "MSAA
 *   knows nothing on this machine". The client object is the control.
 *
 *   oleacc is LOADED, never imported. A static import of a DLL that is not
 *   there kills the image at load, before WinMain, with nothing written and
 *   nothing to debug. It is present on this Win98 SE; it is not promised on
 *   every one.
 */
#ifndef RC_AUTOMATION_MSAA_H
#define RC_AUTOMATION_MSAA_H

#include "automation/select.h"

class MsaaSource
{
public:
    MsaaSource();
    ~MsaaSource();

    /* False when oleacc is absent or refused. A caller that gets false
     * falls back to the window tree and reports msaa=0 at the handshake,
     * rather than pretending the fields are empty. */
    bool available() const { return m_aofw != 0; }

    /* Fill `out` for one window, fetching ONLY the fields in `want`
     * (Selector::WANT_*). Each field is a separate COM call into another
     * process, so asking for three when the selector needs one is three
     * times the work -- measured at a few ms each on the emulator and
     * reported at about a second per control on a 200MHz machine, which is
     * where that multiplier stops being academic.
     *
     * Returns false if this window has no accessible object, which is
     * ordinary and not an error. The caller owns the deadline. */
    bool enrich(HWND h, int want, Enrichment &out);

    int  objectsMade() const { return m_made; }
    int  namesFound() const  { return m_named; }

private:
    HMODULE m_oleacc;
    void   *m_aofw;        /* AccessibleObjectFromWindow */
    void   *m_roleText;    /* GetRoleTextA, optional */
    bool    m_comReady;
    int     m_made;
    int     m_named;

    MsaaSource(const MsaaSource &);
    MsaaSource &operator=(const MsaaSource &);
};

#endif
