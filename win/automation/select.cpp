#include "automation/select.h"
#include "core/str.h"

#include <string.h>

Selector::Selector()
{
    m_stepN = 0;
    m_needsMsaa = 0;
    memset(m_steps, 0, sizeof(m_steps));
}

static void skipSpace(const char *&p)
{
    while (*p == ' ' || *p == '\t')
        p++;
}

static bool isIdentChar(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
        || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.';
}

/* Copy up to `cap`-1 identifier characters. Returns how many were taken, so
 * a caller can tell "nothing there" from "something short". */
static int takeIdent(const char *&p, char *out, unsigned cap)
{
    unsigned n = 0;
    while (isIdentChar(*p) && n + 1 < cap)
        out[n++] = *p++;
    out[n] = '\0';
    /* Refuse silently truncating a longer name into a shorter one that
     * might match something else entirely. */
    while (isIdentChar(*p))
        p++;
    return (int)n;
}

static bool takeQuoted(const char *&p, char *out, unsigned cap)
{
    char quote = *p;
    unsigned n = 0;
    if (quote != '"' && quote != '\'')
        return false;
    p++;
    while (*p && *p != quote && n + 1 < cap)
        out[n++] = *p++;
    out[n] = '\0';
    if (*p != quote)
        return false;
    p++;
    return true;
}

bool Selector::parse(const char *text, char *err, unsigned errCap)
{
    const char *p = text;
    bool pendingDirect = false;

    m_stepN = 0;
    m_needsMsaa = 0;
    memset(m_steps, 0, sizeof(m_steps));
    if (err && errCap)
        err[0] = '\0';
    if (!text || !*text)
    {
        rc_strf(err, errCap, "empty selector");
        return false;
    }

    while (*p)
    {
        skipSpace(p);
        if (!*p)
            break;

        if (*p == '>')
        {
            if (m_stepN == 0)
            {
                rc_strf(err, errCap, "'>' with nothing before it");
                return false;
            }
            pendingDirect = true;
            p++;
            continue;
        }

        if (m_stepN >= RC_SEL_STEPS)
        {
            rc_strf(err, errCap, "more than %d steps", RC_SEL_STEPS);
            return false;
        }

        SelStep &s = m_steps[m_stepN];
        memset(&s, 0, sizeof(s));
        s.ctrlId = -1;
        s.directChild = pendingDirect;
        pendingDirect = false;

        /* A bare class name, if there is one. */
        if (isIdentChar(*p))
            takeIdent(p, s.cls, sizeof(s.cls));

        /* Then any number of #id, [attr], :pseudo, in any order. */
        for (;;)
        {
            if (*p == '#')
            {
                char num[16];
                p++;
                if (!takeIdent(p, num, sizeof(num)))
                {
                    rc_strf(err, errCap, "'#' with no control id");
                    return false;
                }
                s.ctrlId = 0;
                for (const char *q = num; *q; q++)
                {
                    if (*q < '0' || *q > '9')
                    {
                        rc_strf(err, errCap, "control id '%s' is not a number",
                                num);
                        return false;
                    }
                    s.ctrlId = s.ctrlId * 10 + (*q - '0');
                }
                continue;
            }

            if (*p == '[')
            {
                char key[32];
                AttrTest t;
                p++;
                memset(&t, 0, sizeof(t));
                if (!takeIdent(p, key, sizeof(key)))
                {
                    rc_strf(err, errCap, "'[' with no attribute name");
                    return false;
                }
                if (!strcmp(key, "text"))       t.which = ATTR_TEXT;
                else if (!strcmp(key, "class")) t.which = ATTR_CLASS;
                else if (!strcmp(key, "name"))  { t.which = ATTR_NAME;
                                                  m_needsMsaa |= WANT_NAME; }
                else if (!strcmp(key, "role"))  { t.which = ATTR_ROLE;
                                                  m_needsMsaa |= WANT_ROLE; }
                else if (!strcmp(key, "value")) { t.which = ATTR_VALUE;
                                                  m_needsMsaa |= WANT_VALUE; }
                else if (!strcmp(key, "action")){ t.which = ATTR_ACTION;
                                                  m_needsMsaa |= WANT_ACTION; }
                else if (!strcmp(key, "state")) { t.which = ATTR_STATE;
                                                  m_needsMsaa |= WANT_STATE; }
                else
                {
                    rc_strf(err, errCap, "unknown attribute '%s' (text, "
                            "class, name, role, value, action, state)", key);
                    return false;
                }

                if (*p == '^')      { t.op = ATTR_PREFIX;   p++; }
                else if (*p == '$') { t.op = ATTR_SUFFIX;   p++; }
                else if (*p == '*') { t.op = ATTR_CONTAINS; p++; }
                else                  t.op = ATTR_EXACT;
                if (*p != '=')
                {
                    rc_strf(err, errCap, "attribute '%s' has no '='", key);
                    return false;
                }
                p++;
                if (*p == '"' || *p == '\'')
                {
                    if (!takeQuoted(p, t.value, sizeof(t.value)))
                    {
                        rc_strf(err, errCap, "unterminated quote after '%s'",
                                key);
                        return false;
                    }
                }
                else
                {
                    takeIdent(p, t.value, sizeof(t.value));
                }
                if (*p != ']')
                {
                    rc_strf(err, errCap, "missing ']' after '%s'", key);
                    return false;
                }
                p++;
                if (s.attrN >= RC_SEL_ATTRS)
                {
                    rc_strf(err, errCap, "more than %d attributes in one step",
                            RC_SEL_ATTRS);
                    return false;
                }
                s.attrs[s.attrN++] = t;
                continue;
            }

            if (*p == ':')
            {
                char name[24];
                p++;
                if (!takeIdent(p, name, sizeof(name)))
                {
                    rc_strf(err, errCap, "':' with no name");
                    return false;
                }
                if (!strcmp(name, "visible"))      s.needVisible = true;
                else if (!strcmp(name, "enabled")) s.needEnabled = true;
                else if (!strcmp(name, "focused")) s.needFocused = true;
                else if (!strcmp(name, "nth"))
                {
                    if (*p != '(')
                    {
                        rc_strf(err, errCap, ":nth needs (n)");
                        return false;
                    }
                    p++;
                    s.nth = 0;
                    while (*p >= '0' && *p <= '9')
                        s.nth = s.nth * 10 + (*p++ - '0');
                    if (*p != ')' || s.nth <= 0)
                    {
                        rc_strf(err, errCap, ":nth(n) wants a number from 1");
                        return false;
                    }
                    p++;
                }
                else
                {
                    rc_strf(err, errCap,
                            "unknown ':%s' (visible, enabled, focused, nth)",
                            name);
                    return false;
                }
                continue;
            }
            break;
        }

        /* A step that constrains nothing would match every window, which is
         * never what anyone meant to type. */
        if (!s.cls[0] && s.ctrlId < 0 && !s.attrN && !s.needVisible
            && !s.needEnabled && !s.needFocused && !s.nth)
        {
            rc_strf(err, errCap, "step %d constrains nothing", m_stepN + 1);
            return false;
        }
        m_stepN++;
    }

    if (pendingDirect)
    {
        rc_strf(err, errCap, "'>' with nothing after it");
        return false;
    }
    if (m_stepN == 0)
    {
        rc_strf(err, errCap, "nothing to match");
        return false;
    }
    /* :nth counts the nodes the whole selector matched, so it only means
     * anything on the final step. Accepting it elsewhere and ignoring it
     * would return a confidently wrong control, which is the one outcome
     * this grammar exists to rule out. */
    for (int i = 0; i < m_stepN - 1; i++)
        if (m_steps[i].nth)
        {
            rc_strf(err, errCap, ":nth belongs on the last step, not step %d",
                    i + 1);
            return false;
        }
    return true;
}

static bool testOne(const char *have, const AttrTest &t)
{
    unsigned hn, vn;
    if (!have)
        have = "";
    hn = (unsigned)strlen(have);
    vn = (unsigned)strlen(t.value);
    switch (t.op)
    {
    case ATTR_EXACT:    return strcmp(have, t.value) == 0;
    case ATTR_PREFIX:   return vn <= hn && memcmp(have, t.value, vn) == 0;
    case ATTR_SUFFIX:   return vn <= hn
                            && memcmp(have + hn - vn, t.value, vn) == 0;
    case ATTR_CONTAINS: return strstr(have, t.value) != NULL;
    }
    return false;
}

bool Selector::matchesLeaf(const Node &n, const Enrichment *e) const
{
    if (m_stepN == 0)
        return false;
    return matchesStep(m_stepN - 1, n, e);
}

bool Selector::stepNeedsEnrichment(int i, const Node &n) const
{
    if (i < 0 || i >= m_stepN)
        return false;
    const SelStep &s = m_steps[i];
    for (int k = 0; k < s.attrN; k++)
    {
        switch (s.attrs[k].which)
        {
        case ATTR_NAME:
        case ATTR_VALUE:
        case ATTR_ACTION:
        case ATTR_STATE:
            return true;
        case ATTR_ROLE:
            if (!n.role[0])
                return true;
            break;
        case ATTR_TEXT:
        case ATTR_CLASS:
            break;
        }
    }
    return false;
}

bool Selector::matchesStep(int i, const Node &n, const Enrichment *e) const
{
    if (i < 0 || i >= m_stepN)
        return false;
    const SelStep &s = m_steps[i];

    if (s.cls[0] && strcmp(n.cls, s.cls))
        return false;
    if (s.ctrlId >= 0 && n.ctrlId != s.ctrlId)
        return false;
    if (s.needVisible && !n.visible)
        return false;
    if (s.needEnabled && !n.enabled)
        return false;
    if (s.needFocused && !n.focused)
        return false;

    for (int i = 0; i < s.attrN; i++)
    {
        const AttrTest &t = s.attrs[i];
        const char *have = NULL;
        switch (t.which)
        {
        case ATTR_TEXT:  have = n.text; break;
        /* Free, like the bare class name it exists beside: the walk read it
         * with GetClassName before any of this. */
        case ATTR_CLASS: have = n.cls;  break;
        /* No enrichment supplied means the caller has not paid for MSAA on
         * this node yet. That is "unknown", not "no": saying false here
         * would quietly drop every candidate before it could be enriched. */
        case ATTR_NAME:  if (!e) return true; have = e->name;  break;
        /* The walk already derived a role from class and style for every
         * standard control, at no cost. MSAA is consulted only for the
         * classes that produced nothing -- which is the whole point of
         * carrying `Node::role`. */
        case ATTR_ROLE:
            if (n.role[0]) { have = n.role; break; }
            if (!e) return true;
            have = e->role;
            break;
        case ATTR_VALUE: if (!e) return true; have = e->value; break;
        case ATTR_ACTION:if (!e) return true; have = e->action; break;
        case ATTR_STATE: if (!e) return true; have = e->state;  break;
        }
        if (!testOne(have, t))
            return false;
    }
    return true;
}
