#include "sync/verify.h"

VerifyResult SyncCrcVerifier::verify(const char *tempPath)
{
    rc_sync_stat got;

    /* A full content read, not an attribute stat: this is the only thing that
     * proves the bytes arrived intact, and a file somebody still holds open
     * has attributes but cannot be read. Those two outcomes are different
     * answers -- see VerifyResult. */
    rc_sync_stat_file(tempPath, &got);
    if (got.type != 'f') return VERIFY_UNREADABLE;

    /* Against the checksum the DECLARED class selects, not against our own
     * classification of the bytes. A hard `got.cls != cls` failure here made a
     * bridge-side `-text` override unusable: the bridge declares byte-exact,
     * we look at the content and call it text, and every transfer comes back
     * crc_mismatch forever. The bridge is the side that reads .gitattributes;
     * our classifier is a default, not a veto. */
    return RC_CRC_FOR(got, m_cls) == m_want ? VERIFY_OK : VERIFY_MISMATCH;
}
