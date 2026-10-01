/* What the processor calls itself. libexec; no libraries, no imports.
 *
 * WHY THIS EXISTS. `GetSystemInfo` gives a family and a model number, and the
 * HELLO header built from it read `x86 family 6 model 10, 1 cpu`. That is
 * accurate and it is also what an agent reads as "a weak machine": on
 * 2026-10-01 one chose an interpreted language over the Visual C++ 6.0
 * installed beside it, reasoning from "tight memory and single CPU", on an
 * Athlon XP 2000+ with 256MB -- which for this operating system is close to
 * the best hardware it ever ran on.
 *
 * The brand string is what System Properties and dxdiag show, and it is the
 * same string: both read it out of CPUID. So this asks the processor directly
 * rather than shelling out to dxdiag and parsing a text file for a value two
 * instructions return -- and it works identically on the VM, where dxdiag may
 * not be installed at all.
 *
 * WHAT IS NOT HERE: the clock speed. Nothing on 9x reports it, so it would
 * have to be an RDTSC delta over a timed interval, and the agreed bound for
 * this is "what dxdiag or System Properties sees" -- which on Windows 98 is
 * the brand string and not a megahertz figure.
 *
 * NOTHING HERE ASSUMES CPUID EXISTS. A 386 and an early 486 do not have it,
 * and `make check` forbids CMOV, so this tree targets machines old enough to
 * matter: `cpuidPresent()` does the documented EFLAGS.ID test first. The brand
 * string itself is newer still -- Intel added it with the Pentium 4, AMD with
 * the Athlon -- so the Pentium 200 this project also drives will report a
 * vendor and nothing more, and that is the expected answer rather than a
 * failure.
 */
#ifndef RC_EXEC_CPUID_H
#define RC_EXEC_CPUID_H

#ifndef __cplusplus
#error "exec/cpuid.h is C++"
#endif

/* True if the CPUID instruction can be executed at all. */
bool cpuidPresent();

/* "GenuineIntel", "AuthenticAMD", ... into `out`. False if CPUID is absent;
 * `out` is then an empty string. Needs 13 bytes. */
bool cpuVendor(char *out, unsigned size);

/* "AMD Athlon(tm) XP 2000+", leading and trailing spaces removed -- the
 * processor pads it, and on some parts with spaces in the MIDDLE too, which
 * are left alone because they are part of the name. False when this processor
 * has no brand string, which is every CPU older than about 1999. Needs 49
 * bytes. */
bool cpuBrand(char *out, unsigned size);

#endif
