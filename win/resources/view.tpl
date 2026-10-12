; How the transcript looks. Edit this, restart the client, see the change.
;
; This is half of what view.css is: the stylesheet decides colour, spacing
; and type, and this decides the elements and the class names they carry.
; Keeping the structure compiled in while the style was a file left half the
; appearance needing a rebuild, which is the asymmetry this closes.
;
; THE FORMAT IS DELIBERATELY BELOW A PARSER. Control flow stays in the code:
; the code loops table rows, the code decides whether a fence has a language
; label. A template holds at most ONE {} -- the children hole, where the
; block's content is streamed -- and any number of {name} value holes, which
; may repeat ({level} appears twice in a heading). There are no conditionals
; and no loops. A VARIANT IS A SECOND KEY, not a branch: toolresult and
; toolresult_err, permhead_ask and permhead_view, permlead and permlead_desc.
;
; A wrapper whose content continues AFTER the hole cannot be one template, so
; those are composed: `toolresult` wraps a `pre` and maybe a `clip`, `code`
; wraps maybe a `lang` and a `pre`, `exporthead` wraps a title and maybe a
; `when`. That is why the file has fewer wrappers than it looks like it needs.
;
; WHAT IS NOT HERE, and saying so is what keeps the claim honest:
;
;   * The <br> that joins prose lines, and the three inline marks (<code>,
;     <b>, <i>). A separator is neither a children hole nor a value hole,
;     and an inline mark is the one thing a designer restyles in view.css
;     rather than in markup.
;   * Two class names that are DOM operations rather than markup:
;     rc_html::settle_perm writes "permdone" and settle_pending writes "you"
;     through put_className. They stay as RC_CSS_* in win/app/htmlview.h,
;     and tests/python/meta/test_guest_css.py asserts that the "you" there
;     appears in this file's `user` class list.
;
; READ THROUGH GetPrivateProfileStringA, which strips whitespace from BOTH
; ENDS of a value and keeps a semicolon inside one. A value therefore cannot
; begin or end with a meaningful space -- which is why the em dash before a
; permission description lives in permlead_desc with the space inside it,
; rather than as a suffix on the key before. Put comments on their own line.
;
; `version` must match RC_VIEW_VERSION in win/view/render.h. A mismatch is a
; status line and a log line, loud and non-fatal: a stale appearance beats
; no transcript.

[markup]
version         = 1

; --- prose -------------------------------------------------------------
para            = <p>{}</p>
heading         = <h{level}>{}</h{level}>
quote           = <blockquote>{}</blockquote>
rule            = <hr>

; --- lists -------------------------------------------------------------
bullet          = <ul>{}</ul>
numbered        = <ol>{}</ol>
item            = <li>{}</li>

; --- code --------------------------------------------------------------
code            = <div class="codewrap">{}</div>
lang            = <div class="lang">{}</div>
pre             = <pre>{}</pre>

; --- tables ------------------------------------------------------------
table           = <table>{}</table>
row             = <tr>{}</tr>
cell            = <td>{}</td>
headcell        = <th>{}</th>

; --- what the app and the machine say -----------------------------------
user            = <div class="you">{}</div>
; The echo of what you typed, before the bridge has acknowledged it. The
; class and the id are BOTH load-bearing and neither is only markup:
; rc_html::settle_pending finds the element by id and writes the `user`
; class over this one, so "you" must be in both and the id must match
; RC_ID_PENDING in win/app/htmlview.h. test_block_vocab.py asserts it.
pending         = <div class="you pending" id="pend">{}</div>
notice          = <div class="notice">{}</div>
error           = <div class="err">{}</div>
thinking        = <div class="thinking">{}</div>
status          = <div class="status"><img src="spin.gif" width="16" height="16" alt="">{}</div>

; One key with two holes rather than a separate `toolname`: the space after
; </span> would be trailing whitespace in a toolname value, and the profile
; API strips it.
tool            = <div class="tool"><span class="toolname">{name}</span> {}</div>
toolresult      = <div class="toolresult">{}</div>
toolresult_err  = <div class="toolresult err">{}</div>
clip            = <div class="clip">-- shown to you in full; {dropped} more characters were sent to the model --</div>

; --- the permission card -------------------------------------------------
; `h` on the wire names WHICH head; the words are here, because they are the
; card's furniture and not the bridge's data. permhead_view duplicates
; bridge/prompts/policy.toml's policy.view.head deliberately, and
; tests/python/agent/test_permissions_gaston.py keeps the two in step.
perm            = <div class="perm" id="perm">{}</div>
perm_plain      = <div class="perm">{}</div>
permhead_ask    = <div class="permhead">Permission required</div><div class="permdonehead">Answered</div>
permhead_view   = <div class="permhead">What can run without asking</div>
permlead        = <p><b>{lead}</b></p>
permlead_desc   = <p><b>{lead}</b> &mdash; {}</p>
permwarn        = <p class="permwarn"><b>Check this one:</b> {}</p>
permprev        = <p><b>What it touches:</b> {}</p>
permpara        = <p>{}</p>
permsteps       = <ol>{}</ol>
permstep        = <li>{}</li>
permtable       = <table>{}</table>
permrow         = <tr><th>{key}</th><td>{}</td></tr>

; --- export ---------------------------------------------------------------
exporthead      = <div class="exporthead">{}</div>
exporthead_title = <h1>{}</h1>
exporthead_when = <div class="notice">{}</div>
