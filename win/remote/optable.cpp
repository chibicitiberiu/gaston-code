#include "remote/optable.h"

#include <string.h>

/* One row per op, spelled out. No prefix rule: the client's SYNC* wildcard
 * earns its place because that family is closed and named, and a prefix rule
 * here would silently claim every future op that happens to start the same
 * way. A linear strcmp over twenty rows costs nothing next to the syscall
 * each row leads to. */
const GOpRow G_OP_TABLE[] = {
    { "AUTH",    G_CONTROL, false },
    { "PING",    G_CONTROL, false },
    { "CANCEL",  G_CONTROL, false },
    { "SYSINFO", G_CONTROL, false },
    { "PS",      G_CONTROL, false },
    { "KILL",    G_CONTROL, false },
    { "REBOOT",  G_CONTROL, false },
    { "UPDATE",  G_CONTROL, false },

    { "EXEC",    G_JOB,     false },
    { "LAUNCH",  G_JOB,     false },

    { "LS",      G_FS,      false },
    { "STAT",    G_FS,      false },
    { "GET",     G_FS,      false },
    { "PUT",     G_FS,      false },
    { "DATA",    G_FS,      true  },
    { "END",     G_FS,      true  },
    { "RM",      G_FS,      false },
    { "MKDIR",   G_FS,      false },
    { "RMDIR",   G_FS,      false },
    { "MV",      G_FS,      false },
    { "DRIVES",  G_FS,      false }
};

const int G_OP_TABLE_N = sizeof(G_OP_TABLE) / sizeof(G_OP_TABLE[0]);

bool gremoteOp(const char *type, const GOpRow **out)
{
    int i;
    for (i = 0; i < G_OP_TABLE_N; i++)
    {
        if (strcmp(type, G_OP_TABLE[i].op) == 0)
        {
            *out = &G_OP_TABLE[i];
            return true;
        }
    }
    return false;
}
