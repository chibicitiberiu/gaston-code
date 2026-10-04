/* Execution modes, as this client shows and stores them.
 *
 * The BRIDGE decides (bridge/agent/policy.py); this is the same table, so the
 * Options dialog can show what a preset does and the person can start a mode
 * of their own from one. A full-matrix agreement test
 * (tests/python/meta/test_modes_agreement.py) parses RC_PRESETS below and
 * holds it to the bridge's -- a preset that read differently here would
 * describe a policy that is not the one in force.
 *
 * A category is one row of the Permissions page; its value is one of four
 * letters, kept as a char so a mode is RC_CATS bytes and a whole table of
 * them costs nothing worth measuring:
 *
 *   'o' Off      not offered to the model at all
 *   'a' Ask      the bar
 *   'c' Check    a second model decides; the bar only when it is unsure
 *   'y' Allow    it happens
 */
#ifndef RC_MODES_H
#define RC_MODES_H

#ifndef __cplusplus
#error "app/modes.h is C++"
#endif

#define RC_CATS          13
#define RC_MODES_MAX      8     /* the client's own; 4 presets + 8 < 16 */
#define RC_MODE_ID       12     /* "u-" + 8 hex + NUL, and room */
#define RC_MODE_LABEL    32
#define RC_PRESETS_N      4

/* In the bridge's order (categories.CATS). The wire names. */
extern const char *const RC_CAT_NAMES[RC_CATS];

struct ModeDef
{
    char id[RC_MODE_ID];
    char label[RC_MODE_LABEL];
    char base[RC_MODE_ID];        /* the preset it was made from */
    char values[RC_CATS];         /* 'o' 'a' 'c' 'y' */
    ModeDef();
};

struct Preset
{
    const char *id;
    const char *label;
    const char *values;           /* RC_CATS letters, RC_CAT_NAMES order */
    bool        locked;           /* Plan first: shown, never edited */
};

extern const Preset RC_PRESETS[RC_PRESETS_N];

/* The preset with this id, or NULL. */
const Preset *rc_preset(const char *id);

/* 'o','a','c','y' <-> "off","ask","check","allow"; 0 / NULL for anything
 * else, so a typo in an INI is refused rather than read as some value. */
const char *rc_mode_value_name(char v);
char rc_mode_value_of(const char *name);

/* A fresh user-mode id, "u-" and eight hex digits from the clock and a
 * counter: random enough that deleting gaston.ini and making a new mode
 * never inherits a chat that remembered an old one. */
void rc_mode_new_id(char *out, unsigned cap);

#endif
