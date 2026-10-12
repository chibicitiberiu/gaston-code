/* The template table, private to libview.
 *
 * Separate from render.h because render.h is included by two files in app/
 * and this is nobody's business but the renderer's. Keeping TplKey out of
 * app/ also means adding a template is one library's recompile.
 */
#ifndef RC_VIEW_TEMPLATES_H
#define RC_VIEW_TEMPLATES_H

#ifndef __cplusplus
#error "view/templates.h is C++"
#endif

#include "core/bytesink.h"

/* In the same order as KEYS in templates.cpp. The agreement test asserts
 * that this enum, that array and win/resources/view.tpl all hold the same
 * names, so a key added to one and not the others fails loudly rather than
 * rendering as nothing. */
enum TplKey
{
    TPL_VERSION = 0,
    TPL_PARA, TPL_HEADING, TPL_QUOTE, TPL_RULE,
    TPL_BULLET, TPL_NUMBERED, TPL_ITEM,
    TPL_CODE, TPL_LANG, TPL_PRE,
    TPL_TABLE, TPL_ROW, TPL_CELL, TPL_HEADCELL,
    TPL_USER, TPL_PENDING, TPL_NOTICE, TPL_ERROR, TPL_THINKING, TPL_STATUS,
    TPL_TOOL, TPL_TOOLRESULT, TPL_TOOLRESULT_ERR, TPL_CLIP,
    TPL_PERM, TPL_PERM_PLAIN, TPL_PERMHEAD_ASK, TPL_PERMHEAD_VIEW,
    TPL_PERMLEAD, TPL_PERMLEAD_DESC, TPL_PERMWARN, TPL_PERMPREV,
    TPL_PERMPARA, TPL_PERMSTEPS, TPL_PERMSTEP, TPL_PERMTABLE, TPL_PERMROW,
    TPL_EXPORTHEAD, TPL_EXPORTHEAD_TITLE, TPL_EXPORTHEAD_WHEN,
    TPL_COUNT
};

/* Values for the {name} holes. Four is enough for every template in the
 * file and the bound is checked by the caller, not assumed: a fifth would
 * be silently dropped otherwise. */
struct TplValues
{
    const char *name[4];
    const char *value[4];
    unsigned    n;
};

/* The raw template text, or the compiled-in fallback. */
const char *rc_tpl(TplKey k);

/* A template holds at most one {} -- the children hole. `open` writes
 * everything before it, the caller writes the children, `close` writes what
 * follows. A template with no hole is written whole by `open` and `close`
 * does nothing, which is what lets a caller treat every key the same way.
 *
 * `whole` is for a template whose content is entirely value holes. */
bool rc_tpl_open(TplKey k, const TplValues *v, ByteSink &out);
bool rc_tpl_close(TplKey k, const TplValues *v, ByteSink &out);
bool rc_tpl_whole(TplKey k, const TplValues *v, ByteSink &out);

#endif
