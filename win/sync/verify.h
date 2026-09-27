/* Proving that what landed on disk is what the bridge sent.
 *
 * Here and not in libfs because the checksum is miniz and the CHOICE of
 * checksum is a negotiation libfs is not party to: the bridge declares the
 * class from .gitattributes, and our own classification is a default, not a
 * veto. libfs just asks "are these the bytes?" and this answers.
 */
#ifndef RC_SYNC_VERIFY_H
#define RC_SYNC_VERIFY_H

#ifndef __cplusplus
#error "sync/verify.h is C++"
#endif

#include "fs/writer.h"
#include "sync/sync.h"

class SyncCrcVerifier : public CommitVerifier
{
public:
    /* `cls` is the class the BRIDGE declared, which selects which of the two
     * checksums to compare. Comparing our own classification instead made a
     * bridge-side `-text` override unusable: the bridge declares byte-exact,
     * we look at the content and call it text, and every transfer comes back
     * crc_mismatch forever. */
    SyncCrcVerifier(unsigned long wantCrc, char cls)
        : m_want(wantCrc), m_cls(cls) {}

    virtual VerifyResult verify(const char *tempPath);

private:
    unsigned long m_want;
    char          m_cls;
};

#endif
