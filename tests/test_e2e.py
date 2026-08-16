"""End-to-end test: load a page from a local server and read the pixels back.

Everything else in the suite checks one layer. This checks the join between
all of them, the way a person looking at the screen would: a page is fetched
over a real socket, parsed, styled, laid out, rasterised and written to a
PNG, and then that PNG is decoded again and the colours in it are counted.

It exists because the suite had no such check and a regression walked
straight through the gap. `<img>` stopped drawing anything at all -- every
image on every page silently replaced by its alt-text placeholder -- and
nothing anywhere went red, because the only end-to-end assertion was that a
screenshot of `about:blank` came to more than 2000 bytes, which a blank white
rectangle satisfies comfortably.

So the fixture page carries a colour of its own for each thing that has to
survive the trip: a background, a border, glyphs, a PNG and a GIF, in shades
picked so that finding one pixel of the right colour is proof that the layer
that draws it ran. An image that quietly disappears takes its colour with
it, and this test says which one and where it should have been.

The photograph is checked the other way round. A JPEG has no flat colours to
count, so it is decoded here as well and the shot is required to contain the
pixels the decode produced -- the same photograph, in the same numbers,
somewhere on the page. That is the assertion that "a real page with real
photographs draws pixels rather than [img]" reduces to, and it fails if the
decoder drifts by a level as readily as if it stops working.
"""
import collections
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import browser as browsermod
from feetbrowser import imagecodec
from feetbrowser.layout import DrawText
from feetbrowser.net import URL
from feetbrowser.window import Event

from fixture_server import FixtureServer


# The fixture's palette. Every value is deliberately odd so it cannot be
# confused with the browser chrome, the default stylesheet or a rounding
# error, and none of them is a shade the antialiaser produces on its own.
PAGE_BG = (0x12, 0x34, 0x56)
TEXT = (0xFF, 0xEE, 0x00)
BORDER = (0xAB, 0xCD, 0xEF)
PNG_BODY, PNG_MARK = (255, 0, 128), (0, 255, 255)
GIF_BODY, GIF_MARK = (20, 200, 100), (255, 150, 0)
PNG_SIZE, GIF_SIZE = (120, 80), (90, 60)
PNG_MARK_SIZE, GIF_MARK_SIZE = 24, 18
JPEG_SIZE = (320, 224)


def _decode(path):
    with open(path, "rb") as fh:
        return imagecodec.decode_png(fh.read())


def _boxes(width, height, rgba):
    """Every colour in the shot, mapped to its pixel count and its bounding
    box. One pass, because a 1000x720 shot is 720,000 pixels and doing it
    once per colour of interest is the difference between a test and a wait.
    """
    count = collections.Counter()
    box = {}
    for i in range(width * height):
        colour = (rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
        count[colour] += 1
        x, y = i % width, i // width
        seen = box.get(colour)
        if seen is None:
            box[colour] = [x, y, x, y]
        else:
            if x < seen[0]:
                seen[0] = x
            if x > seen[2]:
                seen[2] = x
            if y > seen[3]:
                seen[3] = y
    return count, box


def _report(count, drawn):
    """What to print when an assertion fails: the twenty commonest colours
    and the text that was drawn, which together say what the renderer did
    instead of what it was asked to."""
    lines = ["  colours in the shot (top 20):"]
    for colour, n in count.most_common(20):
        lines.append("    #%02x%02x%02x  %d" % (colour + (n,)))
    lines.append("  text drawn: %r" % (drawn,))
    return "\n".join(lines)


def _within(inner, outer, slack=0):
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def test_page_renders_every_layer():
    with FixtureServer() as fixtures, tempfile.TemporaryDirectory() as folder:
        shot = os.path.join(folder, "e2e-shot.png")
        browser = browsermod.screenshot(fixtures.url("pixels.html"), shot)
        width, height, rgba = _decode(shot)

    drawn = [c.text for c in browser.tabs[0].display_list
             if isinstance(c, DrawText) and c.text]
    count, box = _boxes(width, height, rgba)
    detail = _report(count, drawn)

    # The alt-text placeholder is what `<img>` falls back to, so seeing it is
    # the regression itself rather than a symptom of one. Checked first: it
    # names the image that failed, which no amount of pixel counting can.
    placeholders = [t for t in drawn if "[img" in t]
    assert not placeholders, (
        "images fell back to their alt-text placeholder: %s\n%s"
        % (placeholders, detail))

    # The page background. The chrome is above it, so it must not start at
    # the top of the shot -- a page painted over the toolbar is a bug too.
    assert count[PAGE_BG] > width * 50, (
        "the page background barely got painted\n%s" % detail)
    page = box[PAGE_BG]
    assert page[1] > 0, "the page was painted over the browser chrome"

    # Glyphs. Coverage is antialiased, so only the middles of the strokes
    # land on the exact colour; a few hundred of those is a word, and zero
    # is a font engine that produced nothing.
    assert count[TEXT] > 300, (
        "almost no text was drawn (%d pixels of #ffee00)\n%s"
        % (count[TEXT], detail))
    text = box[TEXT]
    assert text[2] - text[0] > 60, (
        "the heading is too narrow to be four glyphs\n%s" % detail)

    # The border, which must be a frame and not a filled box: the middle of
    # its bounding box belongs to whatever it surrounds.
    assert BORDER in box, "the border was not drawn\n%s" % detail
    frame = box[BORDER]
    assert frame[2] - frame[0] > 400, (
        "the border does not span the page\n%s" % detail)
    mid = ((frame[1] + frame[3]) // 2) * width + (frame[0] + frame[2]) // 2
    assert tuple(rgba[mid * 4:mid * 4 + 3]) != BORDER, (
        "the border filled its box instead of outlining it\n%s" % detail)

    # The two images. Each is a solid field with a differently coloured
    # square in its top-left corner, so the body proves the decode and the
    # blit, and the corner proves the rows went down the way they came in.
    for name, body, mark, (iw, ih), marked in (
            ("swatch.png", PNG_BODY, PNG_MARK, PNG_SIZE, PNG_MARK_SIZE),
            ("dot.gif", GIF_BODY, GIF_MARK, GIF_SIZE, GIF_MARK_SIZE)):
        assert body in box, (
            "%s never reached the screen: no #%02x%02x%02x anywhere\n%s"
            % ((name,) + body + (detail,)))
        assert mark in box, (
            "%s lost its corner marker\n%s" % (name, detail))
        whole = [min(box[body][0], box[mark][0]),
                 min(box[body][1], box[mark][1]),
                 max(box[body][2], box[mark][2]),
                 max(box[body][3], box[mark][3])]
        assert (whole[2] - whole[0] + 1, whole[3] - whole[1] + 1) == (iw, ih), (
            "%s was drawn %dx%d, not %dx%d\n%s"
            % (name, whole[2] - whole[0] + 1, whole[3] - whole[1] + 1,
               iw, ih, detail))
        assert count[body] + count[mark] >= iw * ih * 0.9, (
            "%s is full of holes: %d of %d pixels\n%s"
            % (name, count[body] + count[mark], iw * ih, detail))
        assert count[mark] >= marked * marked * 0.9, (
            "%s: the corner marker is the wrong size\n%s" % (name, detail))
        assert box[mark][0] == whole[0] and box[mark][1] == whole[1], (
            "%s came out mirrored or upside down\n%s" % (name, detail))
        assert _within(whole, frame), (
            "%s landed outside the bordered box\n%s" % (name, detail))

    png, gif = box[PNG_BODY], box[GIF_BODY]
    assert png[2] < gif[0], (
        "the two images are not side by side in source order\n%s" % detail)
    assert text[3] < frame[1], (
        "the heading did not end up above the bordered box\n%s" % detail)

    # The photograph. Counting one colour proves nothing about a JPEG, so
    # this counts all of them: decode the fixture here, and require the shot
    # to hold at least as many of each colour as the photograph has. A
    # decoder that stopped working takes every one of them away, and one
    # that drifted by a level takes most of them.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "photo.jpg"), "rb") as fh:
        jw, jh, photo = imagecodec.decode(fh.read())
    assert (jw, jh) == JPEG_SIZE, "the fixture photograph changed size"
    wanted = collections.Counter(
        (photo[i * 4], photo[i * 4 + 1], photo[i * 4 + 2])
        for i in range(jw * jh))
    landed = sum(min(n, count[colour]) for colour, n in wanted.items())
    assert landed > jw * jh * 0.9, (
        "photo.jpg: %d of %d decoded pixels reached the screen\n%s"
        % (landed, jw * jh, detail))

    # A whole page with a photograph on it has now been fetched, decoded,
    # laid out and drawn. On the CI job that installs Pillow and cairosvg
    # this is the moment that says the photograph came from our decoder: an
    # import added back by reflex would have succeeded there and left its
    # module behind. Everywhere else neither is installed and this is free.
    leaked = [m for m in ("PIL", "cairosvg") if m in sys.modules]
    assert not leaked, (
        "the browser imported %s to draw the page" % ", ".join(leaked))

    print("  page %dx%d, background %d px, glyphs %d px, border %d px, "
          "swatch.png %d px, dot.gif %d px, photo.jpg %d of %d px"
          % (width, height, count[PAGE_BG], count[TEXT], count[BORDER],
             count[PNG_BODY] + count[PNG_MARK],
             count[GIF_BODY] + count[GIF_MARK], landed, jw * jh))


# -- the address bar against pasted line breaks -----------------------------
#
# Every character that ends a line somewhere: LF, CR and CRLF from the three
# families of text file, the vertical tab and form feed, the file/group/record
# separators, NEL, and the Unicode line and paragraph separators that a copy
# out of rendered text really can carry. The tab rides along because it is the
# same kind of character -- no URL holds one, and the renderer has no glyph
# for it. `str.splitlines` is the list Python agrees with.
LINE_BREAKS = ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e",
               "\x85", "\u2028", "\u2029")
BULLETS = "• first bullet%s• second bullet"
FLAT_BULLETS = "• first bullet • second bullet"


def _browser():
    """A headless browser with one about: tab and the address bar focused."""
    browser = browsermod.Browser()
    browser.canvas.resize(browsermod.WIDTH, browsermod.HEIGHT)
    browser._apply_resize()
    browser.new_tab("about:blank")
    browser.focus = "address"
    browser.address_text = ""
    browser.address_caret = 0
    browser.address_sel = None
    browser.address_view = 0
    return browser


def _paste(browser, text):
    browser.address_text = ""
    browser.address_caret = 0
    browser.address_sel = None
    browser.address_view = 0
    browser.window.clipboard_clear()
    browser.window.clipboard_append(text)
    browser._address_paste()
    return browser.address_text


def test_address_bar_flattens_every_kind_of_line_break():
    """Paste multi-line text into the address bar and it stays one line.

    The bar is one box with one line of text in it and the renderer honours
    a break wherever it finds one, so anything that survives to the draw is
    painted outside the box. This drives the real paste handler on a real
    browser, once per break character, and then the two other ways text
    reaches the bar: a keystroke, and the programmatic reset that fills the
    bar from the tab's URL.
    """
    browser = _browser()
    for brk in LINE_BREAKS:
        got = _paste(browser, BULLETS % brk)
        assert got == FLAT_BULLETS, (
            "pasting a %r between two bullets left %r" % (brk, got))
        assert got.splitlines() == [got], (
            "%r still breaks a line after %r was pasted" % (got, brk))
        assert browser.address_caret == len(got), (
            "the caret is at %d in %d characters after pasting %r"
            % (browser.address_caret, len(got), brk))

    # A break becomes a space rather than nothing: two bullets pasted
    # together must not weld into one word.
    assert "bullet•" not in _paste(browser, BULLETS % "\n")

    # Runs of breaks, and the whitespace hugging them, are one space -- a
    # blank line between paragraphs is not two words apart.
    assert _paste(browser, "one\n\n\ntwo") == "one two"
    assert _paste(browser, "one  \n  two") == "one two"
    assert _paste(browser, "one\ttwo") == "one two"

    # Whitespace around the whole paste goes: a URL copied off a page
    # arrives with the line ending still attached to it.
    assert _paste(browser, "\n  https://example.com/  \n") == \
        "https://example.com/"
    assert _paste(browser, "\r\n\r\n") == ""

    # The typed path. A control character cannot be typed, but a space very
    # much can, and flattening must not eat it.
    browser.address_text = ""
    browser.address_caret = 0
    for ch in "a b":
        browser._address_key(Event(char=ch, keysym=ch))
    assert browser.address_text == "a b", (
        "typing does not survive the flattening: %r" % browser.address_text)

    # The programmatic path: the bar is refilled from the tab's URL when it
    # takes focus, and a URL is quite capable of carrying a break.
    browser.active_tab.url = URL("https://example.com/a\nb")
    browser._address_reset_from_tab()
    assert browser.address_text == "https://example.com/a b", (
        "a URL with a break in it reached the bar: %r" % browser.address_text)
    assert browser.address_caret == len(browser.address_text)
    print("  %d break characters, all flattened to a space"
          % len(LINE_BREAKS))


def test_pasted_line_breaks_do_not_paint_outside_the_address_bar():
    """The bug as the reporter saw it: pixels below the address bar.

    Semantics are one thing and the screen is another, so this renders the
    chrome twice -- once with an empty bar, once with a multi-line paste in
    it -- and compares the band underneath the address box. Anything the
    paste added down there is text that got away.
    """
    browser = _browser()
    top = browsermod.toes.band_height(browser.chrome_bands())
    box_bottom = top + 72
    x0 = browser._address_bar_x() - 10
    # From just under the box down past where a second line of chrome text
    # would land, which is over the top of the page by then.
    rows = range(box_bottom + 2, box_bottom + 40)

    def band():
        browser.draw()
        surface = browser.canvas.render()
        pixels, stride = bytes(surface.pixels), surface.stride
        return [pixels[y * stride + x0 * 3:y * stride + surface.width * 3]
                for y in rows]

    empty = band()
    pasted = _paste(browser, BULLETS % "\n")
    # Only that the paste landed at all -- what it looks like afterwards is
    # the other test's business, and this one has to reach its pixels even
    # when the flattening is not there.
    assert "second bullet" in pasted, "the paste did not reach the bar"
    after = band()

    dirty = [rows[i] for i in range(len(rows)) if after[i] != empty[i]]
    assert not dirty, (
        "a pasted line break painted %d rows of pixels below the address "
        "bar (y=%d..%d, box ends at y=%d): the text escaped the box"
        % (len(dirty), dirty[0], dirty[-1], box_bottom))
    print("  %d rows below the address box, none of them repainted"
          % len(rows))


def _menu_item_y(menu, index):
    """Canvas y of the centre of the menu item at `index`, or a separator
    raises. Walks the same row arithmetic ContextMenu.draw uses so the test
    clicks where the menu really paints."""
    y0 = menu.y + menu.PAD
    for i, item in enumerate(menu.items):
        if item is None:
            y0 += menu.SEP
            continue
        if i == index:
            return y0 + menu.ITEM_H / 2
        y0 += menu.ITEM_H
    raise AssertionError("menu has no item at index %d" % index)


def _hamburger_click(browser):
    """Click the centre of the hamburger settings button in the toolbar."""
    band = browsermod.toes.band_height(browser.chrome_bands())
    menu_x = browser.canvas.winfo_width() - browsermod.MENU_BTN_W - 8
    browser._chrome_click(menu_x + browsermod.MENU_BTN_W / 2, band + 60)
    return menu_x


def test_settings_menu_opens_from_hamburger_button():
    """The hamburger button at the right of the address bar drops the
    settings menu, right-aligned under the button, with the about pages
    and the toe hub."""
    browser = _browser()
    menu_x = _hamburger_click(browser)
    menu = browser.context_menu
    assert menu.open_, "clicking the hamburger did not open the menu"
    labels = [item[0] for item in menu.items if item is not None]
    assert labels == ["Bookmarks", "History", "Manage Shoes", "Manage Toes"], (
        "unexpected settings menu items: %r" % labels)
    # The menu hangs from the button's right edge, below the toolbar.
    band = browsermod.toes.band_height(browser.chrome_bands())
    assert menu.x + menu.width == menu_x + browsermod.MENU_BTN_W, (
        "the menu is not right-aligned under the hamburger")
    assert menu.y == band + 74, "the menu is not under the toolbar"
    # The open menu is drawn again by a chrome repaint: it hangs into the
    # chrome band, which the repaint wipes first.
    browser._draw_chrome()
    assert menu.open_, "a chrome repaint closed the settings menu"
    print("  menu items: %s, right-aligned under the button"
          % ", ".join(labels))


def test_settings_menu_bookmarks_opens_new_tab():
    """'Bookmarks' opens the bookmarks about page in a fresh tab."""
    browser = _browser()
    _hamburger_click(browser)
    menu = browser.context_menu
    browser._context_menu_click(menu.x + 20, _menu_item_y(menu, 0))
    assert not menu.open_, "choosing an item left the menu open"
    assert len(browser.tabs) == 2, "Bookmarks did not open a new tab"
    assert isinstance(browser.active_tab.url, browsermod._BookmarksURL), (
        "Bookmarks opened %r instead of about:bookmarks"
        % browser.active_tab.url)
    print("  Bookmarks -> about:bookmarks in a new tab")


def test_settings_menu_manage_toes_opens_the_hub():
    """'Manage Toes' opens the toe hub in a fresh tab."""
    browser = _browser()
    _hamburger_click(browser)
    menu = browser.context_menu
    browser._context_menu_click(menu.x + 20, _menu_item_y(menu, 5))
    assert not menu.open_, "choosing an item left the menu open"
    assert len(browser.tabs) == 2, "Manage Toes did not open a new tab"
    assert str(browser.active_tab.url) == "toe://hub", (
        "Manage Toes opened %r instead of toe://hub" % browser.active_tab.url)
    print("  Manage Toes -> toe://hub in a new tab")


def test_settings_menu_stays_attached_across_a_resize():
    """Resizing the window re-anchors the open settings menu under the
    hamburger button, which is pinned to the window's right edge, while a
    right-click menu stays where its click point put it."""
    browser = _browser()
    _hamburger_click(browser)
    menu = browser.context_menu
    assert menu.anchor == "burger", \
        "the settings menu is not marked as burger-anchored"
    browser.canvas.resize(700, 720)
    browser._apply_resize()
    w = browser.canvas.winfo_width()
    assert menu.x + menu.width == w - 8, (
        "after a resize the menu sits %dpx from the hamburger's right edge"
        % (w - 8 - (menu.x + menu.width)))
    # A right-click menu is anchored to its click point, not the chrome:
    # a resize must leave it where the user opened it.
    browser._on_context_menu(Event(x=300, y=300))
    page_menu = browser.context_menu
    assert page_menu.open_ and page_menu.anchor is None, \
        "a right-click menu is not burger-anchored"
    x_before = page_menu.x
    y_before = page_menu.y
    browser.canvas.resize(900, 720)
    browser._apply_resize()
    assert (page_menu.x, page_menu.y) == (x_before, y_before), (
        "a resize moved the right-click menu from its click point")
    print("  menu re-anchored under the button; right-click menus stay put")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} END-TO-END TESTS PASSED")


if __name__ == "__main__":
    main()
