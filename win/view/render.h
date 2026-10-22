/* Records to markup. libview; libcore only.
 *
 * The client's half of stage 3. The bridge sends what a thing IS -- a
 * paragraph, a fence, a table, a permission card -- and this decides what it
 * looks like. Before this, the bridge sent finished HTML written to IE5's
 * constraints, and a second client would have had to parse that HTML back
 * into the structure the bridge had already worked out and thrown away.
 *
 * NOTHING HERE KNOWS THE WIRE. A record arrives as bytes with a length and
 * leaves as bytes in a ByteSink; which frame carried it is the exe's
 * business (docs/REMOTE.md).
 *
 * THE MARKUP IS NOT IN THIS FILE. It is in win/resources/view.tpl, beside
 * the exe, so that changing how a block looks is an edit and a restart
 * rather than a rebuild -- a 4-minute iteration into a 30-second one, which
 * docs/PROGRESS.md:955 records the value of. The same argument the
 * stylesheet already won. A missing or malformed file DEGRADES to a
 * compiled-in last resort rather than refusing to start, because the box is
 * updated by hand with COPY and a half-updated one is a real state.
 *
 * THE RECORD FORMAT, one per block, in the frame body:
 *
 *     <kind> [<attr>=<value>]*   LF   first line, ASCII, bounded
 *     <tag>[ <payload>]          LF   zero or more; tag is one ASCII letter
 *     .                          LF
 *
 * Within a payload line, \x01 plus a letter opens an inline run (c code,
 * s strong, e emphasis) and \x02 closes the innermost. The stack is a real
 * stack: a run nests inside one of its own kind, "_a *b* c_" being
 * <i>a <i>b</i> c</i>. Depth is structurally bounded at three by the
 * grammar on the far side and CHECKED here rather than assumed.
 */
#ifndef RC_VIEW_RENDER_H
#define RC_VIEW_RENDER_H

/* --- the constants, which are language-neutral --------------------------
 *
 * BEFORE the C++ guard, deliberately. win/net/caps.h needs
 * RC_BLOCKS_VERSION for the HELLO key it declares, and caps.h is included
 * by tests/guest/capscheck.c, which is C -- the agreement check that
 * compares every RC_* capability constant against bridge/files/caps.py.
 *
 * Putting the guard at the top of this file broke that build, and only
 * `make wine-smoke` caught it: `make -C win gaston` does not compile the
 * check binaries. The alternative was a second copy of the version number
 * in caps.h with a test to keep the two in step, which is one more thing
 * to keep in step for no gain.
 */

/* The block vocabulary this build understands. Declared at the handshake as
 * `blocks`; the bridge refuses a value it does not know rather than
 * guessing. bridge/view/blocks.py VERSION is the other half. */
#define RC_BLOCKS_VERSION 1

/* The appearance files beside the exe. Bumped when a block's markup or a
 * class name changes in a way a stale file would get wrong. A mismatch is
 * loud and non-fatal: a stale appearance beats no transcript. */
#define RC_VIEW_VERSION 1

/* The id the permission card carries while it can still be answered.
 * rc_html::settle_perm finds it, swaps the class and CLEARS the id, so the
 * next request's card is the only one holding it. */
#define RC_ID_PERM "perm"

#ifndef __cplusplus
/* Everything below is the C++ API: ByteSink is a class and these take a
 * reference to one. A C translation unit gets the constants above and
 * stops here. */
#else

#include "core/bytesink.h"

/* The deepest inline nesting a record can contain. Three, structurally:
 * **X** and *X* exclude * from X, __X__ and _X_ exclude _, so a chain uses
 * at most one *-family level, one _-family level and code. One spare. */
#define RC_SPAN_DEPTH 4

/* How long a payload line may be. The bridge's own record cap is a megabyte;
 * this is the per-line bound that keeps the parser's working set flat. A
 * longer line is refused, not cut -- half a line of markup is worse than a
 * missing block. */
#define RC_LINE_MAX 8192

/* Render one record into `out`.
 *
 * `wrapcol` is the column a literal block's long lines are broken at, and it
 * is a PARAMETER because the three callers want three different numbers: the
 * view passes its window's column, export a fixed one, and rendercheck 56 so
 * that its output can be compared with the bridge's. Zero means do not wrap.
 *
 * The honest limit, recorded because somebody will notice: a resize does not
 * re-wrap blocks already in the document. The client holds their rendered
 * HTML, not their records, and holding records would cost heap on a machine
 * measured at 28KB free after a turn. New blocks and a re-rendered page pick
 * up the new width.
 *
 * False means the sink stopped it or the record was refused; either way
 * nothing half-written is left behind that the caller must undo -- the
 * caller inserts per record, so a refused record is simply absent.
 */
bool rc_render(const char *rec, unsigned len, unsigned wrapcol, ByteSink &out);

/* Render exactly ONE record and say how many bytes it used, or -1.
 *
 * For the caller that inserts per record rather than per frame -- the
 * transcript, where a page replay is up to 90 blocks and rendering them
 * into one buffer would hold the whole page's markup AND the UTF-16 copy
 * MSHTML makes of it, on a machine measured at 28KB free after a turn.
 *
 * Returning the LENGTH rather than a cursor keeps the caller's loop
 * obvious and means the parse is not run twice to find the boundary. */
int rc_render_one(const char *rec, unsigned len, unsigned wrapcol,
                  ByteSink &out);

/* --- what the GUEST composes ------------------------------------------
 *
 * A notice, an error, the echo of what you typed, the status line. These
 * are not bridge content and never cross the wire: they say what the APP
 * is doing, which is why "the guest transforms nothing" does not cover
 * them and never did.
 *
 * They take ALREADY-ESCAPED markup and only wrap it, because the guest's
 * own escaping is not the record path's and the difference is deliberate:
 * esc_into escapes the double quote as well, and folds a newline to <br>,
 * because there is no upstream line splitter for a status message. The
 * record path escapes three characters and keeps newlines, matching the
 * bridge byte for byte. Routing these through the record path instead
 * would turn every multi-line local notice into one wrapped line.
 *
 * What they DO get from the record path is the template: the class names
 * and the element live in view.tpl with everything else, so a designer
 * restyling the transcript does not have to find two places.
 */
enum RcLocal
{
    RC_LOCAL_NOTICE = 0,
    RC_LOCAL_ERROR,
    RC_LOCAL_USER,
    RC_LOCAL_PENDING,   /* the echo, before the bridge acknowledges it */
    RC_LOCAL_STATUS
};

bool rc_wrap_local(RcLocal kind, const char *escaped, ByteSink &out);

/* How many whole records `rec` holds, or -1 if it is malformed. A frame body
 * is zero or more WHOLE records and zero is real -- an empty LIVE body is
 * what clears the open slot. */
int rc_render_count(const char *rec, unsigned len);

/* Load win/resources/view.tpl from beside the exe.
 *
 * Returns false and fills `err` when the file is missing, a known key is
 * absent or empty, or an unknown key is present. The caller does NOT refuse
 * to start on a false: it logs, says so in the status bar, and renders
 * through the compiled-in fallback. See rc_tpl_degraded().
 *
 * IniFile cannot report two of those three on its own and the plan assumed
 * it could. A missing file yields silent successes with the caller's
 * defaults; `para=` reads back present-and-empty; a mistyped [marku] passes
 * checkKeys because the section it enumerates has no keys at all. So the
 * loader does its own existence check and its own all-present-and-non-empty
 * pass. view/templates.cpp says so again where it does it.
 */
bool rc_tpl_load(const char *exeDir, char *err, unsigned errCap);

/* True when rendering is using the compiled-in fallback -- every block as
 * escaped text in a paragraph -- because view.tpl could not be used. The
 * warning does NOT go only to the transcript: in this state a transcript
 * notice renders as an unstyled <p> indistinguishable from model prose, in
 * a document whose CSS is probably also missing, since all three files ship
 * and COPY together. It goes to the status bar, which is pane text rather
 * than HTML, and to the log. */
bool rc_tpl_degraded();

/* The version the loaded view.tpl declared, or 0 when none was loaded.
 * Compared against RC_VIEW_VERSION by the caller. */
int rc_tpl_version();

/* Write every loaded template as `key<TAB>value` lines. For
 * `rendercheck --dump-templates`, which exists for one reason:
 * GetPrivateProfileStringA on Windows 98 will not round-trip a control
 * character while Wine hands it back intact (win/core/inifile.cpp:50-107,
 * "every test passed on three machines and the guest fell over on the first
 * one"). A template file is exactly that round trip, carrying quotes,
 * semicolons, equals signs, angle brackets and significant internal spaces,
 * and inifilecheck's longest string case is seven bytes. So the values are
 * compared byte for byte against the repo file ON THE GUEST before the
 * equality proof is trusted. */
bool rc_tpl_dump(ByteSink &out);

#endif  /* __cplusplus */

#endif
