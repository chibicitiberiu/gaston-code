/* framecheck -- the frame header contract, run against the shipping C.
 *
 * WHY THIS EXISTS. frameproto.h has claimed since the file was written that
 * "header values are percent-encoded for %, \n and \r only". That was true of
 * the PARSER and false of the BUILDER: rc_frame_build appended values raw for
 * the whole of Phases 1-3 while the bridge percent-DECODED everything it
 * received. Nothing caught it because nothing ever ran the two halves against
 * each other -- the parser was tested with bridge-shaped input, and the
 * builder's output was only ever read by the bridge, which is the side that
 * would misread it.
 *
 * So this links the real frameproto.o and does the round trip in one process:
 * build a frame, parse it back, and check the value that comes out is the
 * value that went in. tests/python/test_guest_frame.py then does the other
 * half, feeding this binary's bytes to bridge/wire/protocol.py's parser, so a
 * change on either side fails.
 *
 * Output is one tab-separated `pass|FAIL<TAB>name<TAB>detail` line per check,
 * the same shape cppcheck.exe and synccheck.exe use.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "net/frameproto.h"
#include "core/str.h"


/* Append with the bounded formatter and report the new end.
 *
 * The unbounded formatter would be shorter here, and `make -C win check`
 * refuses it anyway: the gate greps the whole of win/ INCLUDING its own check
 * binaries, and the one time an exception was argued for a provably safe
 * literal copy, the answer recorded in audit 01.0 was to convert it so the
 * gate needs no exceptions at all. It caught this file on its first run. */
static unsigned addf(char *buf, unsigned cap, unsigned pos, const char *fmt,
                     int arg, int use_arg)
{
    if (pos >= cap) return pos;
    if (use_arg) rc_strf(buf + pos, cap - pos, fmt, arg);
    else         rc_strf(buf + pos, cap - pos, "%s", fmt);
    return pos + (unsigned)strlen(buf + pos);
}

static int g_fail = 0;

static void say(const char *name, int pass, const char *detail)
{
    if (!pass) g_fail = 1;
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
}

/* Build one frame with a single header and hand back the payload. */
static int build1(char *buf, unsigned cap, const char *key, const char *val)
{
    const char *keys[1];
    const char *vals[1];
    keys[0] = key;
    vals[0] = val;
    return rc_frame_build(buf, cap, "TEST", keys, vals, 1, NULL, 0);
}

/* Build, then parse the result back, and report the value that survived. */
static int roundtrip(const char *key, const char *val, char *out, unsigned outcap)
{
    static char buf[RC_MAX_FRAME + 1024];
    rc_frame f;
    int total = build1(buf, sizeof(buf), key, val);
    const char *got;

    if (total < 0) return -1;
    /* rc_frame_parse takes the payload, not the 4-byte length prefix, and it
     * writes NULs into what it is given -- so it gets the live buffer, which
     * is exactly how net.cpp calls it. */
    if (rc_frame_parse(buf + 4, (unsigned)(total - 4), &f) != 0) return -2;
    got = rc_frame_get(&f, key);
    if (!got) return -3;
    if (!rc_strcpy(out, outcap, got)) return -4;
    return 0;
}

static void encoding_cases(void)
{
    char got[RC_MAX_VAL];
    static char buf[RC_MAX_FRAME + 1024];
    int total;

    /* The three bytes that change the shape of a frame. */
    say("roundtrip-percent",
        roundtrip("k", "C:\\100%WORK", got, sizeof(got)) == 0
        && strcmp(got, "C:\\100%WORK") == 0,
        "a literal % survives");

    say("roundtrip-newline",
        roundtrip("k", "one\ntwo", got, sizeof(got)) == 0
        && strcmp(got, "one\ntwo") == 0,
        "LF survives and does not split the header");

    say("roundtrip-cr",
        roundtrip("k", "one\rtwo", got, sizeof(got)) == 0
        && strcmp(got, "one\rtwo") == 0,
        "CR survives");

    /* %41 must come back as "%41" and NOT as "A". This is the case that says
     * the encoder ran: an unencoded % would be decoded by the parser. */
    say("roundtrip-hexlike",
        roundtrip("k", "50%41", got, sizeof(got)) == 0
        && strcmp(got, "50%41") == 0,
        "a %-plus-hex sequence is not decoded into something else");

    say("roundtrip-plain",
        roundtrip("k", "C:\\WORK\\notes.md", got, sizeof(got)) == 0
        && strcmp(got, "C:\\WORK\\notes.md") == 0,
        "an ordinary path is untouched");

    /* The encoded form must actually be on the wire, not just survive a round
     * trip through a matching pair of bugs. */
    total = build1(buf, sizeof(buf), "k", "a\nb");
    say("wire-has-escape",
        total > 0 && memchr(buf + 4, '\n', (unsigned)(total - 4)) != NULL
        && strstr(buf + 4, "k=a%0Ab\n") != NULL,
        "LF leaves as %0A");

    total = build1(buf, sizeof(buf), "k", "100%");
    say("wire-percent-escaped",
        total > 0 && strstr(buf + 4, "k=100%25\n") != NULL,
        "% leaves as %25");

    /* Non-ASCII characters (e.g. \xe9 'é') must be percent-encoded on the wire
     * so that the header block remains strictly printable ASCII. */
    say("roundtrip-nonascii",
        roundtrip("k", "caf\xe9", got, sizeof(got)) == 0
        && strcmp(got, "caf\xe9") == 0,
        "high bytes survive round trip");

    total = build1(buf, sizeof(buf), "k", "caf\xe9");
    say("wire-nonascii-escaped",
        total > 0 && strstr(buf + 4, "k=caf%E9\n") != NULL,
        "high byte leaves as %E9");
}

static void limit_cases(void)
{
    static char payload[RC_MAX_FRAME];
    rc_frame f;
    unsigned pos = 0;
    int i;

    /* A frame carrying more headers than rc_frame can hold must be REFUSED.
     * It used to keep the first sixteen and drop the rest in silence, which
     * loses a well-formed header -- and if the lost one is `id`, an ack is
     * answered to nobody. */
    pos = addf(payload, sizeof(payload), pos, "TEST\n", 0, 0);
    for (i = 0; i < RC_MAX_HEADERS + 1; i++)
        pos = addf(payload, sizeof(payload), pos, "k%d=v\n", i, 1);
    pos = addf(payload, sizeof(payload), pos, "\n", 0, 0);
    say("too-many-headers-refused",
        rc_frame_parse(payload, pos, &f) != 0,
        "header 17 drops the frame rather than vanishing");

    /* Exactly at the limit still parses -- the refusal must be off-by-one
     * correct, or every full frame dies. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\n", 0, 0);
    for (i = 0; i < RC_MAX_HEADERS; i++)
        pos = addf(payload, sizeof(payload), pos, "k%d=v\n", i, 1);
    pos = addf(payload, sizeof(payload), pos, "\n", 0, 0);
    say("exactly-max-headers-ok",
        rc_frame_parse(payload, pos, &f) == 0 && f.nheaders == RC_MAX_HEADERS,
        "sixteen headers is fine");

    /* An over-long VALUE is a dropped frame, not a clipped string. cwd rides
     * as a header straight to CreateProcessA, so a clipped one names a
     * different existing directory and the command runs somewhere nobody
     * chose. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\nk=", 0, 0);
    for (i = 0; i < RC_MAX_VAL + 8; i++) payload[pos++] = 'x';
    pos = addf(payload, sizeof(payload), pos, "\n\n", 0, 0);
    say("overlong-value-refused",
        rc_frame_parse(payload, pos, &f) != 0,
        "a value past RC_MAX_VAL drops the frame");

    /* A repeated key drops the frame. The guest used to append it and then
     * never read it, because rc_frame_get returns the first match. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\nid=1\nid=2\n\n", 0, 0);
    say("duplicate-key-refused",
        rc_frame_parse(payload, pos, &f) != 0,
        "a repeated header key drops the frame");

    /* Two DIFFERENT keys are obviously fine -- pinned so the duplicate check
     * cannot be satisfied by refusing everything. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\nid=1\nok=2\n\n", 0, 0);
    say("distinct-keys-ok",
        rc_frame_parse(payload, pos, &f) == 0 && f.nheaders == 2,
        "two different keys still parse");

    /* And an over-long KEY, which has always been refused -- pinned here so
     * the three limits are asserted together rather than two of them. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\n", 0, 0);
    for (i = 0; i < RC_MAX_KEY + 8; i++) payload[pos++] = 'k';
    pos = addf(payload, sizeof(payload), pos, "=v\n\n", 0, 0);
    say("overlong-key-refused",
        rc_frame_parse(payload, pos, &f) != 0,
        "a key past RC_MAX_KEY drops the frame");

    /* Header lookup via rc_frame_get is case-sensitive, matching the duplicate check. */
    pos = 0;
    pos = addf(payload, sizeof(payload), pos, "TEST\nkey=val\n\n", 0, 0);
    say("header-lookup-case-sensitive",
        rc_frame_parse(payload, pos, &f) == 0
        && rc_frame_get(&f, "key") != NULL
        && rc_frame_get(&f, "KEY") == NULL,
        "rc_frame_get is case sensitive");

    /* An over-long TYPE is refused. */
    pos = 0;
    for (i = 0; i < RC_MAX_TYPE + 4; i++) payload[pos++] = 'T';
    payload[pos++] = '\n';
    payload[pos++] = '\n';
    say("overlong-type-refused",
        rc_frame_parse(payload, pos, &f) != 0,
        "a type past RC_MAX_TYPE drops the frame");
}

/* Print the limits so the Python side can assert against the real numbers
 * rather than a copy of them. Same trick as synccheck --limits. */
static void limits(void)
{
    printf("limit\tRC_MAX_FRAME\t%d\n", RC_MAX_FRAME);
    printf("limit\tRC_MAX_HEADERS\t%d\n", RC_MAX_HEADERS);
    printf("limit\tRC_MAX_KEY\t%d\n", RC_MAX_KEY);
    printf("limit\tRC_MAX_VAL\t%d\n", RC_MAX_VAL);
    printf("limit\tRC_MAX_TYPE\t%d\n", RC_MAX_TYPE);
}

/* Emit a frame built by the REAL builder, escaped one byte per line, so the
 * Python side can reconstruct the exact bytes and run them through
 * bridge/wire/protocol.py. Printing raw would not survive stdout. */
static void emit(const char *val)
{
    static char buf[RC_MAX_FRAME + 1024];
    int total = build1(buf, sizeof(buf), "v", val);
    int i;
    if (total < 0) { printf("emit\tFAIL\t\n"); return; }
    printf("emit\t%d\t", total);
    for (i = 4; i < total; i++)
        printf("%02X", (unsigned char)buf[i]);
    printf("\n");
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--limits") == 0) { limits(); return 0; }
    if (argc > 2 && strcmp(argv[1], "--emit") == 0) { emit(argv[2]); return 0; }

    encoding_cases();
    limit_cases();
    return g_fail;
}
