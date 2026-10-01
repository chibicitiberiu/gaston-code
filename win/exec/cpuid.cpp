#include "exec/cpuid.h"

#include <string.h>

/* THE TWO INSTRUCTIONS, as Watcom auxiliary pragmas.
 *
 * `#pragma aux` rather than an `_asm` block because this is the form Open
 * Watcom documents for a whole function body, and because it states the
 * register contract -- what goes in, what comes out, what is clobbered --
 * instead of leaving the optimiser to guess around a block it cannot see
 * into.
 *
 * EBX is pushed and popped by hand. It is callee-save in Watcom's register
 * calling convention and CPUID overwrites it, which on an -os build is a
 * corrupted local in the CALLER rather than anything visible here.
 */

extern "C" int rc_cpuid_present(void);
#pragma aux rc_cpuid_present =      \
    "pushfd"                        \
    "pop  eax"                      \
    "mov  ecx, eax"                 \
    "xor  eax, 0x00200000"          \
    "push eax"                      \
    "popfd"                         \
    "pushfd"                        \
    "pop  eax"                      \
    "xor  eax, ecx"                 \
    "shr  eax, 21"                  \
    "and  eax, 1"                   \
    "push ecx"                      \
    "popfd"                         \
    value [eax] modify [eax ecx];

/* One leaf into four dwords at `regs` -- eax, ebx, ecx, edx in that order. */
extern "C" void rc_cpuid(unsigned leaf, unsigned *regs);
#pragma aux rc_cpuid =              \
    "push ebx"                      \
    "cpuid"                         \
    "mov  [esi+0], eax"             \
    "mov  [esi+4], ebx"             \
    "mov  [esi+8], ecx"             \
    "mov  [esi+12], edx"            \
    "pop  ebx"                      \
    parm [eax] [esi] modify [eax ecx edx];

bool cpuidPresent()
{
    return rc_cpuid_present() != 0;
}

/* Copy one dword as four little-endian bytes. The strings CPUID returns are
 * laid out that way, which is why the obvious memcpy of the struct works and
 * is what this is. */
static void append(char *out, unsigned &at, unsigned size, unsigned reg)
{
    for (int i = 0; i < 4; ++i)
    {
        if (at + 1 >= size)
            return;
        out[at++] = (char)((reg >> (i * 8)) & 0xFF);
    }
}

bool cpuVendor(char *out, unsigned size)
{
    if (!out || size == 0)
        return false;
    out[0] = '\0';
    if (!cpuidPresent())
        return false;

    unsigned r[4];
    rc_cpuid(0, r);
    unsigned at = 0;
    /* EBX, EDX, ECX -- in that order, which is not the order they come back
     * in. "GenuineIntel" is ebx="Genu", edx="ineI", ecx="ntel". */
    append(out, at, size, r[1]);
    append(out, at, size, r[3]);
    append(out, at, size, r[2]);
    out[at] = '\0';
    return at > 0;
}

bool cpuBrand(char *out, unsigned size)
{
    if (!out || size == 0)
        return false;
    out[0] = '\0';
    if (!cpuidPresent())
        return false;

    /* HOW MANY EXTENDED LEAVES THIS PROCESSOR HAS, asked before any are read.
     * On a CPU with none, leaf 0x80000000 returns something that is not a
     * leaf count at all -- early Intel parts return the STANDARD maximum --
     * so the test is that the answer is in the extended range AND reaches
     * the three leaves the string is spread across. */
    unsigned r[4];
    rc_cpuid(0x80000000u, r);
    if (r[0] < 0x80000004u || r[0] > 0x8000FFFFu)
        return false;

    char raw[49];
    unsigned at = 0;
    for (unsigned leaf = 0x80000002u; leaf <= 0x80000004u; ++leaf)
    {
        rc_cpuid(leaf, r);
        append(raw, at, sizeof raw, r[0]);
        append(raw, at, sizeof raw, r[1]);
        append(raw, at, sizeof raw, r[2]);
        append(raw, at, sizeof raw, r[3]);
    }
    raw[at] = '\0';

    /* A NUL may arrive before the 48th byte, and the processor pads what is
     * left with spaces -- usually leading ones, which is what makes the
     * string look wrong when it is printed raw. */
    unsigned first = 0;
    while (raw[first] == ' ')
        ++first;
    unsigned last = (unsigned)strlen(raw + first);
    while (last > 0 && (raw[first + last - 1] == ' '
                        || raw[first + last - 1] == '\t'))
        --last;
    if (last == 0)
        return false;
    if (last >= size)
        last = size - 1;
    memcpy(out, raw + first, last);
    out[last] = '\0';
    return true;
}
