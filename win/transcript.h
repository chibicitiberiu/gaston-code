/* The transcript view: a RichEdit 2.0 control plus the batching idiom.
 *
 * RichEdit 2.0 is what ships with Win98 (riched20.dll; 3.0 arrives with
 * ME/2000/XP). CHARFORMAT2 covers everything the formatting spec needs --
 * bold, per-run monospace, colour -- so no custom-drawn view is required.
 *
 * Memory is bounded by PAGINATION, not by this control: the bridge sends one
 * page at a time and the control never holds more. See ../docs/PLAN.md.
 */
#ifndef RC_TRANSCRIPT_H
#define RC_TRANSCRIPT_H

#include <windows.h>

typedef enum {
    RC_STYLE_PLAIN = 0,
    RC_STYLE_CODE,
    RC_STYLE_BOLD,
    RC_STYLE_THINKING,
    RC_STYLE_TOOL,
    RC_STYLE_ERROR,
    RC_STYLE_USER,
    RC_STYLE__COUNT
} rc_style;

typedef struct {
    HWND     hwnd;
    HMODULE  riched;
    int      batching;
    int      was_at_bottom;
} rc_transcript;

int  rc_transcript_create(rc_transcript *t, HWND parent, int id, HINSTANCE inst);
void rc_transcript_destroy(rc_transcript *t);
void rc_transcript_begin(rc_transcript *t);
void rc_transcript_end(rc_transcript *t);
void rc_transcript_append(rc_transcript *t, const char *text, rc_style style);
void rc_transcript_clear(rc_transcript *t);
int  rc_transcript_at_bottom(rc_transcript *t);

#endif
