/* A short list of short strings, '\n'-separated in one heap buffer.
 *
 * For the settings a person adds by hand -- protected places, trusted
 * programs, ignore patterns -- where a std::vector<std::string> was the
 * obvious type and measured as the expensive one: with exceptions on, it and
 * the std::string it brings put about 25KB of code and tables into the
 * client (2026-10-04), most of it because the header that held it is
 * included by half the program. This is one buffer, no templates, no
 * exceptions: growth that fails leaves the list as it was and says so.
 *
 * Entries are compared without case, as the machine compares names.
 */
#ifndef RC_TEXTLIST_H
#define RC_TEXTLIST_H

#ifndef __cplusplus
#error "core/textlist.h is C++"
#endif

class TextList
{
public:
    TextList();
    TextList(const TextList &other);
    TextList &operator=(const TextList &other);
    ~TextList();

    /* Next entry into `out`: `*pos` starts at 0; false past the last. An
     * entry too long for `out` is skipped, not cut -- a cut pattern names a
     * different place. */
    bool next(unsigned *pos, char *out, unsigned cap) const;
    unsigned count() const;
    bool has(const char *entry) const;
    /* False when it was empty, already there, contained a newline, or
     * memory ran out -- the list is then unchanged. */
    bool add(const char *entry);
    /* The same without the duplicate check, for two lists kept in step
     * (ids and their labels) where a repeated label is still a row. */
    bool push(const char *entry);
    void remove(const char *entry);
    void clear();
    bool empty() const { return m_len == 0; }
    /* The whole list, '\n'-separated, NUL-terminated; "" when empty. */
    const char *text() const { return m_buf ? m_buf : ""; }

private:
    bool reserve(unsigned need);

    char    *m_buf;
    unsigned m_len;
    unsigned m_cap;
};

#endif
