#!/usr/bin/env python3
"""Render docs/GUIDE.md into the user guide zip.

ONE SOURCE. docs/GUIDE.md is the guide; this turns it into a page that can
be read in Internet Explorer 5 on the machine being set up, and packs it
with its images and the licences. Nothing here is written twice.

WHY A ZIP AND NOT ONE SELF-CONTAINED .html: IE 5.0 has no `data:` URI
support (win/resources/view.css:151), so the screenshots have to be real
files beside the page.

WHAT IE 5.0 NEEDS, and none of it is guesswork -- every line is already
paid for in win/resources/view.css, which is the stylesheet this project
renders every transcript through on real 1998 hardware:

  * NO DOCTYPE. win/app/export.cpp:42-48 is this project's one existing
    standalone-HTML-read-in-a-browser artefact and it emits none, with the
    stylesheet inlined in <style>. An external <link rel=stylesheet> is
    UNPROVEN here, which is a better reason for inline CSS than the
    data-URI absence.

  * CHARSET AS http-equiv, never the HTML5 short form. Both
    win/app/htmlview.cpp:520 and export.cpp:43 emit it that way, and
    <meta charset> is the one spelling this project never uses. Belt and
    braces: the body is encoded ascii/xmlcharrefreplace, so the declaration
    cannot be wrong about anything.

  * NO COMPOUND CLASS SELECTORS. IE5 and IE6 read `.a.b` as `.b` alone
    (view.css:179), which is why this codebase has `.permdone` rather than
    `.perm.done`. A rule meant for one box would silently style every one.

  * VERDANA, falling back to MS Sans Serif (view.css:23-33). MS Sans Serif
    is a bitmap .FON with no italic face, so GDI fakes italics by shearing
    the glyph grid. Not Arial.

  * NO <p> INSIDE <li>. win/view/templates.cpp:107 records that an item
    "has to stay an <li> or a list becomes paragraphs inside a <ul>, which
    IE5 renders as an empty bullet followed by the text". Python-Markdown
    emits <li><p>...</p></li> for any LOOSE list -- one with blank lines
    between items -- and a guide's numbered steps are loose by
    construction, because each step has a code block under it. Stripped
    below.

  * NO <pre> IN AN overflow:auto BOX. IE 5.0 sizes such a box to its
    content and then draws the horizontal scrollbar INSIDE that height, so
    a code block becomes nothing but a scrollbar (view.css:61-69, measured
    2026-10-03). view.css pays for this with a `height: expression(...)`
    hack. This file does not need to: every line in every fenced block in
    GUIDE.md is under 72 characters, and the check at the bottom keeps it
    that way. Short lines are a better fix than an expression.

  * WIDTH AND HEIGHT ON <img>, which markdown does not emit. Added here
    from the real file, so the page does not reflow as each screenshot
    arrives.

view.css itself is NOT reused: it is 11 KB of transcript furniture (.you,
.tool, .perm, .thinking) tuned for 640x480 at 8pt, which is unreadable in
the modern browser this page also has to serve. What transfers is the
knowledge, which is the list above.

Run:  python3 tools/gen/mkguide.py   (writes build/release/)
"""

import argparse
import os
import pathlib
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit

REPO = pathlib.Path(toolkit.REPO)

#: Lifted from win/resources/view.css and raised from 8pt, which is right
#: for a 470px pane on a 640x480 screen and too small for a page somebody
#: reads straight through. No compound selectors anywhere below.
STYLE = """
body { font: 10pt Verdana, 'MS Sans Serif', sans-serif; background: #ffffff;
       color: #000000; margin: 0; padding: 12px 18px 32px 18px; }
h1 { font-size: 17pt; margin: 0 0 4px 0; }
h2 { font-size: 13pt; margin: 22px 0 7px 0; padding-bottom: 3px;
     border-bottom: 1px solid #c0c0c0; }
h3 { font-size: 11pt; margin: 16px 0 5px 0; }
p, li { line-height: 1.45; }
p { margin: 0 0 9px 0; }
ul, ol { margin: 0 0 9px 0; padding-left: 26px; }
li { margin: 0 0 4px 0; }
pre { background: #f4f4f4; border: 1px solid #c0c0c0; margin: 9px 0;
      padding: 7px 9px; font: 9pt 'Courier New', monospace; }
code { font: 9pt 'Courier New', monospace; background: #f0f0f0; }
pre code { background: transparent; font-size: 9pt; }
table { border-collapse: collapse; margin: 9px 0; }
th { background: #e8e8f0; text-align: left; vertical-align: top;
     border: 1px solid #c0c0c0; padding: 4px 7px; }
td { border: 1px solid #c0c0c0; padding: 4px 7px; vertical-align: top; }
img { border: 1px solid #808080; margin: 9px 0; }
a { color: #000080; }
"""

HEAD = ("<html><head><meta http-equiv=\"Content-Type\" "
        "content=\"text/html; charset=windows-1252\">"
        "<title>%s</title><style>%s</style></head><body>")
TAIL = "</body></html>"

TITLE = "Setting up Gaston Code"

#: Fenced-block lines wider than this get a horizontal scrollbar in a
#: 640x480 IE5 window, and the <pre> note above says what that costs.
MAX_CODE_COLS = 72

#: MEASURED IN IE 5.0 ON THE P200, 2026-10-27: the screenshots are
#: 800x600 full-desktop captures, and a maximised IE5 window on an 800x600
#: screen has about 780px of content width once the vertical scrollbar is
#: taken out. So a natural-size image overflowed and put a horizontal
#: scrollbar under the WHOLE page -- the text still wrapped and stayed
#: readable, but every page carried a scrollbar it did not need.
#:
#: Scaled with width/height attributes rather than cropped. Cropping to
#: just the client window would be sharper, but the desktop around it is
#: part of what the shot is saying -- this runs on Windows 98 -- and these
#: are illustrations, not reference material.
MAX_IMG_W = 700


def read_guide():
    return (REPO / "docs" / "GUIDE.md").read_text(encoding="utf-8")


def check_code_width(text):
    """Refuse a source whose code blocks will not fit. See MAX_CODE_COLS."""
    bad, fenced = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
        elif fenced and len(line) > MAX_CODE_COLS:
            bad.append("  GUIDE.md:%d is %d columns" % (n, len(line)))
    if bad:
        raise SystemExit("mkguide: code lines too wide for IE5:\n"
                         + "\n".join(bad))


def unwrap_list_paragraphs(html):
    """<li><p>x</p></li> -> <li>x</li>, which is the only form IE5 renders.

    See the module docstring. A loose list is what every numbered step with
    a command under it produces, so this is the common case and not a
    corner.
    """
    html = re.sub(r"<li>\s*<p>", "<li>", html)
    html = re.sub(r"</p>\s*</li>", "</li>", html)
    # A multi-paragraph item keeps its inner breaks; <br> is universal
    # where a second <p> inside <li> is not.
    return re.sub(r"</p>\s*<p>(?=[^<]*</li>)", "<br><br>", html)


def size_images(html, images_dir):
    """Add width and height from the real file, which markdown never does."""
    from PIL import Image

    def one(m):
        src = m.group("src")
        path = images_dir / pathlib.Path(src).name
        if not path.is_file():
            raise SystemExit("mkguide: %s is referenced and missing" % src)
        with Image.open(path) as im:
            w, h = im.size
        if w > MAX_IMG_W:
            h = round(h * MAX_IMG_W / float(w))
            w = MAX_IMG_W
        return '<img%s width="%d" height="%d"' % (m.group("rest"), w, h)

    return re.sub(r'<img(?P<rest>[^>]*?src="(?P<src>[^"]+)"[^>]*?)',
                  one, html)


def render(text, images_dir):
    import markdown

    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    body = unwrap_list_paragraphs(body)
    body = size_images(body, images_dir)
    page = (HEAD % (TITLE, STYLE)) + body + TAIL
    # ascii, so the charset declaration cannot be wrong about anything and
    # a codepage IE5 guesses at cannot change what the page says.
    return page.encode("ascii", "xmlcharrefreplace")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mkguide.py",
                                 description="the user guide zip")
    ap.add_argument("--out", default=str(REPO / "build" / "release"))
    args = ap.parse_args(argv)

    version = re.search(r'^version = "([^"]+)"',
                        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
                        re.M)
    if not version:
        raise SystemExit("mkguide: no version in pyproject.toml")
    version = version.group(1)

    text = read_guide()
    check_code_width(text)
    images_dir = REPO / "docs" / "images"
    html = render(text, images_dir)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / ("gaston-code-%s-guide.zip" % version)

    referenced = sorted(set(re.findall(r"!\[[^\]]*\]\(images/([^)]+)\)", text)))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("guide.html", html)
        z.write(REPO / "docs" / "GUIDE.md", "GUIDE.md")
        for name in referenced:
            z.write(images_dir / name, "images/" + name)
        # Every other artefact carries these and this one would otherwise
        # be the one conveyed work in the release with no licence in it.
        # NOTICE is also where AGPL section 13 is explained in plain words.
        z.write(REPO / "LICENSE", "LICENSE.TXT")
        z.write(REPO / "NOTICE", "NOTICE.TXT")

    print("=== gaston-code %s, user guide ===" % version)
    print("    %d images, %d bytes of html" % (len(referenced), len(html)))
    print()
    print("%s  --  %d bytes" % (dest.name, dest.stat().st_size))
    print()
    print("guide %s in %s" % (version, out))


if __name__ == "__main__":
    main()
