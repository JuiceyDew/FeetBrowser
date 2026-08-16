"""Fast, offline unit tests for URL parsing, HTML, CSS, and internal pages."""
import http.server
import socket
import tempfile
import threading
import time
import urllib.parse
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.canvas import CanvasError, PhotoImage
from feetbrowser.window import Tk

from feetbrowser import net as net_mod
from feetbrowser.net import URL
from feetbrowser.htmlparser import HTMLParser, Element, Text
from feetbrowser.cssparser import CSSParser, parse_inline, style
from feetbrowser.layout import DrawText, get_font, _measure, field_checked, \
    selected_options, option_value, option_label, listbox_rows, \
    listbox_scroll, listbox_active, LISTBOX_ROW_H, LISTBOX_PAD
from feetbrowser.browser import (
    Tab, Browser, _AboutURL, _BookmarksURL, _HistoryURL,
    bookmarks_html, history_html,
    tree_to_list, find_base_href, FormAction, SelectAction, SelectPopup,
    _tab_slot, TAB_LEFT, TAB_WIDTH, TAB_GAP, TAB_CLOSE_W, TAB_DRAG_SLOP,
    NEW_TAB_W
)

# A real Browser() reads ~/.feetbrowser_settings.json for its scroll and
# momentum settings. Point the module at a throwaway file so the machine's
# own settings cannot change what these tests expect.
from feetbrowser import settings as _settings
_settings.SETTINGS_FILE = os.path.join(
    tempfile.mkdtemp(prefix="feetbrowser-units-"), "settings.json")


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def _swallow(fn, *args, **kwargs):
    """Run fn on a helper thread without letting its exception escape into a
    thread nobody is watching. Returns the result, or the exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return exc


def test_url_parsing():
    u = URL("https://example.com/a/b/page.html")
    eq(u.scheme, "https"); eq(u.host, "example.com"); eq(u.port, 443)
    eq(u.path, "/a/b/page.html")

    eq(str(u.resolve("c.html")), "https://example.com/a/b/c.html", "relative")
    eq(str(u.resolve("/x")), "https://example.com/x", "root-relative")
    eq(str(u.resolve("../z")), "https://example.com/a/z", "dotdot")
    eq(str(u.resolve("//cdn.net/s.css")), "https://cdn.net/s.css", "scheme-rel")
    eq(str(u.resolve("https://o.org/y")), "https://o.org/y", "absolute")

    eq(URL("http://h.com:8080/p").port, 8080, "explicit port")
    eq(URL("example.com").scheme, "https", "bare host -> https")

    f = URL("file:///etc/hosts")
    eq(f.scheme, "file"); eq(f.path, "/etc/hosts")

    v = URL("view-source:https://example.com")
    assert v.view_source and v.scheme == "https"

    frag = URL("https://x.com/p#sec")
    eq(frag.fragment, "sec")


def test_data_url():
    _h, body, ctype = URL("data:text/html,<b>hi</b>").request()
    eq(body, "<b>hi</b>"); eq(ctype, "text/html")
    _h, body, _c = URL("data:text/plain;base64,aGVsbG8=").request()
    eq(body, "hello", "base64 data url")


def test_html_parser():
    dom = HTMLParser(
        "<!doctype html><html><head><title>T</title>"
        "<style>a{color:red}</style></head><body>"
        "<p>one<br>two &amp; three<img src=x alt=pic></p>"
        "<!-- comment --><ul><li>a<li>b</ul></body></html>"
    ).parse()
    tags = [n.tag for n in tree_to_list(dom, []) if isinstance(n, Element)]
    for expected in ["html", "head", "title", "style", "body", "p", "br",
                     "img", "ul", "li"]:
        assert expected in tags, f"missing <{expected}>"
    eq(tags.count("li"), 2, "two implicit-closed <li>")
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "two & three" in texts, "entity decode"
    assert "comment" not in texts, "comment stripped"
    # Raw text: <style> content must not be parsed into elements.
    assert "color:red" in texts or any(
        isinstance(c, Text) and "color:red" in c.text
        for n in tree_to_list(dom, []) for c in n.children)


def test_css_cascade():
    rules = CSSParser(
        "p { color: black; } p.warn { color: orange; } #x { color: red; }"
    ).parse()
    dom = HTMLParser(
        '<div><p class="warn" id="x">hi</p><p>bye</p></div>').parse()
    style(dom, rules)
    # id beats class beats tag
    warn = [n for n in tree_to_list(dom, [])
            if isinstance(n, Element) and n.attributes.get("id") == "x"][0]
    eq(warn.style["color"], "red", "id selector wins")
    plain = [n for n in tree_to_list(dom, [])
             if isinstance(n, Element) and n.tag == "p"
             and "id" not in n.attributes][0]
    eq(plain.style["color"], "black", "tag selector")


def _expanded(prop, value):
    """The longhands `_expand` yields for a declaration, as a dict."""
    from feetbrowser.cssparser import _expand
    return dict(list(_expand(prop, value))[1:])


def test_inset_expands_to_the_four_offsets():
    """`inset` is one declaration saying what top/right/bottom/left say in
    four, and layout only reads the four. 1222 of them across the sites
    surveyed, all on absolutely positioned boxes."""
    eq(_expanded("inset", "0"),
       {"top": "0", "right": "0", "bottom": "0", "left": "0"},
       "one value is every side")
    eq(_expanded("inset", "10px 20px"),
       {"top": "10px", "right": "20px", "bottom": "10px", "left": "20px"},
       "two values are block then inline")
    eq(_expanded("inset", "1px 2px 3px 4px"),
       {"top": "1px", "right": "2px", "bottom": "3px", "left": "4px"},
       "four values are clock order")
    eq(_expanded("inset", "calc(1rem + 2px) 0"),
       {"top": "calc(1rem + 2px)", "right": "0",
        "bottom": "calc(1rem + 2px)", "left": "0"},
       "a calc() is one component, spaces and all")
    eq(_expanded("inset", "1px 2px 3px 4px 5px"), {},
       "five values are not a box, and set nothing")


def test_logical_box_properties_become_physical_ones():
    """Assuming horizontal-tb and ltr, which is what the rest of layout
    already believes."""
    eq(_expanded("padding-inline-start", "8px"), {"padding-left": "8px"})
    eq(_expanded("padding-block-end", "4px"), {"padding-bottom": "4px"})
    eq(_expanded("margin-inline", "auto"),
       {"margin-left": "auto", "margin-right": "auto"},
       "one value is both inline sides")
    eq(_expanded("margin-block", "1rem 2rem"),
       {"margin-top": "1rem", "margin-bottom": "2rem"},
       "two values are start then end")
    eq(_expanded("inset-inline-start", "3px"), {"left": "3px"})
    eq(_expanded("inset-block", "5px"), {"top": "5px", "bottom": "5px"})


def test_grid_gap_aliases_and_the_two_value_gap():
    """`grid-gap` is the pre-standard spelling every grid framework of a
    certain age still emits -- 1315 declarations across the surveyed sites,
    and the gap between grid tracks is not a detail. The two-value `gap` is
    here because layout reads the shorthand with parse_px, which takes the
    first number and gives it to both axes."""
    eq(_expanded("grid-column-gap", "12px"), {"column-gap": "12px"})
    eq(_expanded("grid-row-gap", "4px"), {"row-gap": "4px"})
    eq(_expanded("grid-gap", "10px 20px"),
       {"row-gap": "10px", "column-gap": "20px"})
    eq(_expanded("gap", "10px 20px"),
       {"row-gap": "10px", "column-gap": "20px"},
       "row gap first, column gap second")
    eq(_expanded("gap", "8px"), {"row-gap": "8px", "column-gap": "8px"})


def test_logical_padding_indents_the_box_it_is_on():
    """End to end: docs.python.org indents every <dd> with
    `padding-inline-start`, and without the expansion they sat flush left."""
    from feetbrowser.layout import DrawText
    flush = _paint_all("<dl><dd>text</dd></dl>")
    indented = _paint_all("<dl><dd>text</dd></dl>",
                          css="dd { padding-inline-start: 40px }")

    def left(cmds):
        return [c for c in cmds if isinstance(c, DrawText)][0].left
    eq(left(indented), left(flush) + 40, "the logical padding moved the text")


def test_two_value_gap_separates_the_columns():
    from feetbrowser.layout import DrawText
    cmds = _paint_all(
        '<div class="g"><span>A</span><span>B</span></div>',
        css=".g { display: flex; gap: 0 60px } span { width: 20px }")
    xs = sorted(c.left for c in cmds if isinstance(c, DrawText))
    assert xs[1] - xs[0] >= 60, \
        "the column gap is the second value, not the first (%r)" % (xs,)


def test_inheritance_and_inline():
    rules = CSSParser("body { color: green; }").parse()
    dom = HTMLParser(
        '<body><p>x</p><p style="color: purple">y</p></body>').parse()
    style(dom, rules)
    ps = [n for n in tree_to_list(dom, [])
          if isinstance(n, Element) and n.tag == "p"]
    eq(ps[0].style["color"], "green", "inherited from body")
    eq(ps[1].style["color"], "purple", "inline style wins")


def test_welcome_and_reload():
    tab = Tab(700)
    tab.load(_AboutURL())
    eq(tab.title, "New Tab", "welcome title")
    assert isinstance(tab.url, _AboutURL)
    # Reloading an internal page must not crash (regression test).
    tab.load(tab.url, push=False)
    eq(tab.title, "New Tab", "welcome reloads cleanly")


def test_bookmarks_internal_page():
    bookmarks = ["https://example.org", "https://info.cern.ch/hypertext/WWW/TheProject.html"]
    tab = Tab(700)
    tab.load(_BookmarksURL(lambda: bookmarks))
    eq(tab.title, "Bookmarks", "bookmarks title")
    links = [n for n in tree_to_list(tab.nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    hrefs = [n.attributes.get("href", "") for n in links]
    assert bookmarks[0] in hrefs


def test_bookmarks_html_escapes():
    page = bookmarks_html(['https://x.test/?q=<script>alert(1)</script>'])
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_about_page_can_resolve_bookmarks():
    about = _AboutURL(lambda: ["https://example.org"])
    dest = about.resolve("about:bookmarks")
    assert isinstance(dest, _BookmarksURL)
    _h, body, _c = dest.request()
    nodes = HTMLParser(body).parse()
    links = [n for n in tree_to_list(nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    assert links and links[0].attributes.get("href") == "https://example.org"


def test_about_page_can_resolve_history():
    about = _AboutURL(lambda: [])
    dest = about.resolve("about:history")
    assert isinstance(dest, _HistoryURL)
    _h, body, _c = dest.request()
    assert "<title>History</title>" in body


def test_history_html_escapes():
    page = history_html({
        "back": ['https://x.test/?q=<script>alert(1)</script>'],
        "current": "https://safe.test/",
        "forward": [],
    })
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_history_internal_page_loads():
    tab = Tab(700)
    tab.load(_AboutURL())
    tab.load(_BookmarksURL(lambda: ["https://example.org"]))
    tab.load(_HistoryURL(lambda: {
        "back": ["https://example.org"],
        "current": "about:history",
        "forward": ["https://news.ycombinator.com"],
    }))
    eq(tab.title, "History", "history title")
    links = [n for n in tree_to_list(tab.nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    hrefs = {n.attributes.get("href", "") for n in links}
    expected = {"https://example.org", "https://news.ycombinator.com"}
    assert expected.issubset(hrefs)


def test_browser_tab_cycle_wraps():
    tabs = [object(), object(), object()]
    stub = type("Stub", (), {})()
    stub.tabs = tabs
    stub.active_tab = tabs[0]
    stub.draw_calls = 0
    # Switching tabs takes any open <select> list down with it.
    stub.select_popup = SelectPopup()
    stub._dismiss_select_popup = lambda: Browser._dismiss_select_popup(stub)

    def draw():
        stub.draw_calls += 1
    stub.draw = draw

    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[1]
    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[2]
    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[0]
    Browser._cycle_tab(stub, -1)
    assert stub.active_tab is tabs[2]
    assert stub.draw_calls == 4


def test_page_scroll_shortcuts_call_scroll():
    stub = type("Stub", (), {})()
    stub.focus = None
    stub.calls = []
    stub._scroll = lambda delta: stub.calls.append(delta)
    stub.tab_height = lambda: 700
    eq(Browser._on_page_down(stub, None), "break", "pagedown returns break")
    eq(Browser._on_page_up(stub, None), "break", "pageup returns break")
    eq(stub.calls, [580, -580], "page shortcuts use viewport-sized steps")


def _velocity_stub(*ago):
    """A scroll-tracking stub whose history already holds ticks of 30
    pixels, each the given number of seconds old."""
    stub = type("Stub", (), {})()
    stub.SCROLL_VELOCITY_WINDOW = Browser.SCROLL_VELOCITY_WINDOW
    now = time.monotonic()
    stub._scroll_ticks = [(30, now - t) for t in sorted(ago, reverse=True)]
    stub._scroll_velocity = 0.0
    return stub


def test_scroll_velocity_is_a_speed_not_a_mean_of_the_ticks():
    # Three 30-pixel ticks over a tenth of a second is 900 px/s. The mean
    # of the same three ticks is 30 whatever the wheel was doing, which is
    # why the mean cannot drive a momentum curve.
    stub = _velocity_stub(0.10, 0.05)
    Browser._track_scroll_velocity(stub, 30)
    assert 850 < stub._scroll_velocity < 950, stub._scroll_velocity
    fast = _velocity_stub(0.02, 0.01)
    Browser._track_scroll_velocity(fast, 30)
    assert fast._scroll_velocity > 3 * stub._scroll_velocity, \
        "a flick three times as quick read the same: %.0f / %.0f" \
        % (fast._scroll_velocity, stub._scroll_velocity)


def test_a_single_scroll_tick_has_no_speed_yet():
    stub = _velocity_stub()
    Browser._track_scroll_velocity(stub, 30)
    eq(stub._scroll_velocity, 0.0, "one tick spans no time")
    eq(len(stub._scroll_ticks), 1, "the tick was still recorded")


def test_the_scroll_history_does_not_grow_for_the_whole_session():
    # Five hundred ticks from five seconds ago: a browser left scrolling
    # all afternoon must not still be carrying them, nor summing them.
    stub = _velocity_stub(*[5.0] * 500)
    Browser._track_scroll_velocity(stub, 30)
    eq(len(stub._scroll_ticks), 1, "ticks outside the window were kept")
    # One inside the window is a different matter -- that one is the flick.
    stub = _velocity_stub(Browser.SCROLL_VELOCITY_WINDOW / 2)
    Browser._track_scroll_velocity(stub, 30)
    eq(len(stub._scroll_ticks), 2, "a tick inside the window was dropped")


def test_tracking_a_scroll_does_not_start_a_thread_per_tick():
    # Fifty ticks is a second of a wheel being spun. The tolerance is for
    # threads other tests left starting, not for one of ours.
    before = threading.active_count()
    stub = _velocity_stub()
    for _ in range(50):
        Browser._track_scroll_velocity(stub, 30)
    assert threading.active_count() <= before + 2, \
        "%d threads before, %d after" % (before, threading.active_count())


def test_error_page_fallback():
    tab = Tab(700)
    # A bad scheme raises in URL(); load() must render an error page, not crash.
    tab.load("https://nonexistent.invalid.example/")
    assert tab.document is not None, "error page laid out"


def _address_stub():
    class Stub(Browser):
        def __init__(self):
            self.address_text = "https://example.com/"
            self.address_caret = 0
            self.address_sel = None
            self.address_view = 0

        def _address_ensure_visible(self):
            pass
    return Stub()


def test_address_backspace_and_forward_delete():
    stub = _address_stub()
    stub.address_caret = len(stub.address_text)
    Browser._address_backspace(stub)
    assert stub.address_text == "https://example.com", "backspace removes last char"
    Browser._address_forward_delete(stub)
    assert stub.address_text == "https://example.com", "forward delete at end is a no-op"
    stub.address_caret = 0
    Browser._address_forward_delete(stub)
    assert stub.address_text == "ttps://example.com", "forward delete removes first char"


def test_address_select_all_and_insert():
    stub = _address_stub()
    Browser._address_select_all(stub)
    assert stub.address_sel == (0, len("https://example.com/")), "ctrl-a selects all"
    Browser._address_insert(stub, "zz")
    assert stub.address_text == "zz", "typing replaces the selection"


def test_address_caret_movement_and_selection():
    stub = _address_stub()
    stub.address_caret = 4
    Browser._address_move_caret(stub, 2)
    assert stub.address_caret == 6 and stub.address_sel is None, "arrow moves caret"
    Browser._address_move_caret(stub, 1, extend=True)
    assert stub.address_sel == (6, 7), "shift-arrow extends selection"
    Browser._address_move_caret(stub, 1, extend=True)
    assert stub.address_sel == (6, 8), "selection grows with anchor fixed"


def test_address_paste_requires_clipboard():
    stub = _address_stub()
    stub.window = type("W", (), {})()
    stub.window.clipboard_get = lambda: "new.example"
    stub._address_paste()
    assert stub.address_text == "new.examplehttps://example.com/", \
        f"pasted at caret: {stub.address_text}"


def test_url_bare_host_with_port():
    u = URL("example.com:8080")
    eq(u.scheme, "https"); eq(u.host, "example.com"); eq(u.port, 8080)
    u2 = URL("localhost:8000/path")
    eq(u2.host, "localhost"); eq(u2.port, 8000)


def test_url_ipv6():
    u = URL("https://[::1]:8080/x")
    eq(u.host, "::1"); eq(u.port, 8080); eq(u.path, "/x")
    eq(str(u), "https://[::1]:8080/x", "ipv6 round-trip")
    eq(str(URL("http://[::1]/y")), "http://[::1]/y", "ipv6 default port")


def test_url_host_lowered_and_validated():
    eq(URL("HTTP://EXAMPLE.COM").host, "example.com", "host lowercased")
    for bad in ("https:///nohost", "https://host:port", "https://host:99999"):
        try:
            URL(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_dechunk_safety():
    from feetbrowser.net import URL as _URL
    raw = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    eq(_URL._dechunk(raw), b"Wikipedia", "chunked decode")
    eq(_URL._dechunk(b"ffffff\r\nshort"), b"", "truncated chunk handled")


def test_implicit_p_close():
    dom = HTMLParser("<p>alpha<div>beta</div></p>").parse()
    # The first <p> is the one the markup opened. The trailing </p> has no
    # open p to close by then -- the <div> already closed it -- and the spec
    # says an unmatched </p> opens and immediately closes an empty one, so
    # there is a second, empty <p> after the div. Taking the last <p> here
    # would measure that one.
    p = None
    for n in tree_to_list(dom, []):
        if isinstance(n, Element) and n.tag == "p":
            p = n
            break
    assert p is not None
    ptext = "".join(c.text for c in p.children if isinstance(c, Text))
    eq(ptext, "alpha", "block opened inside <p> closes it")
    for c in p.children:
        assert not (isinstance(c, Element) and c.tag == "div"), \
            "div must not be a child of p"
    texts = "".join(c.text for n in tree_to_list(dom, [])
                    for c in n.children if isinstance(c, Text))
    assert "beta" in texts, "content after implicit close kept"


def test_pseudo_selector_stripped():
    rules = CSSParser("a:hover { color: red }").parse()
    dom = HTMLParser('<p><a href="/x">hi</a></p>').parse()
    style(dom, rules)
    a = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "a"][0]
    eq(a.style["color"], "red", "a:hover matches an <a>")


def test_pseudo_element_rule_dropped():
    """A rule targeting ::before/::after creates a box the engine can't draw;
    its declarations must NOT leak onto the parent element (e.g. a 1px
    decorative ::after border must not shrink the element to 1px tall)."""
    rules = CSSParser(
        ".t { height: 100px } .t::after { content: ''; height: 1px }"
    ).parse()
    dom = HTMLParser('<div class="t">x</div>').parse()
    style(dom, rules)
    t = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "div"][0]
    eq(t.style["height"], "100px", "::after height must not leak onto .t")
    rules = CSSParser(".t::after { position: absolute; height: 1px }").parse()
    eq(rules, [], "::after-only rule is dropped entirely")


def test_nested_rule_does_not_stall_the_parser():
    """CSS nesting -- a rule inside a rule -- is what docs.python.org's theme
    and most Tailwind output are written in now:

        .box { color: red; & + div pre { border: none } font-size: 2px }

    The declaration scanner stopped dead on that inner '{': there is no ':'
    in front of it, so it read no property, and the ';' that would have moved
    it on was not there either. The loop then saw the same brace for ever,
    inside a load callback, where settle()'s deadline could never reach it --
    one site in five hung the whole browser on exactly this.

    Nesting is not implemented: flattening it needs the parent selector
    threaded through, which is a feature, not a parse fix. What is required
    here is that the block is *consumed* and the declarations around it
    survive.
    """
    rules = CSSParser(
        ".box { color: red; & + div pre { border: none } font-size: 2px }"
    ).parse()
    eq(len(rules), 1, "the outer rule survives")
    eq(rules[0][1], {"color": "red", "font-size": "2px"},
       "declarations either side of the nested rule are kept")
    # The bare form, with no colon anywhere in the prelude, is the one that
    # spun; so is a nested at-rule, and so is a nested rule inside @layer.
    for css in (".a { .b { color: red } }",
                ".a { color: red; @media screen { color: blue } }",
                "@layer base { .c { .d { x: 1 } color: green } }",
                ".a { &:hover { color: red } margin: 1px }"):
        rules = CSSParser(css).parse()
        assert isinstance(rules, list), css


def test_a_stylesheet_always_finishes_parsing():
    """A stylesheet is bytes off the network. Every loop that walks one has
    to end on every input, including inputs no one would write."""
    for css in ("{", "}", "a{", "a{{", "a{;{", "a{x", "@", "@x", "@x{",
                "a{b:c{", "/*", "a{/*}", 'a{background:url(x{y);color:red}',
                "a{content:'}';color:red}"):
        rules = CSSParser(css).parse()
        assert isinstance(rules, list), css
    eq(CSSParser('a{background:url(x{y);color:red}').parse()[0][1]["color"],
       "red", "a brace inside url() is not a nested rule")
    eq(parse_inline("color: red; margin: 0"),
       {"color": "red", "margin": "0"}, "ordinary inline styles are unharmed")


def test_combinators_do_not_crash_and_match():
    rules = CSSParser(
        "p + span { color: red } p ~ em { color: blue } "
        "ul > li { color: green }"
    ).parse()
    eq(len(rules), 3, "+ and ~ parse (approximated) without crashing")
    dom = HTMLParser('<div><ul><li>d</li></ul></div>').parse()
    style(dom, rules)
    li = [n for n in tree_to_list(dom, []) if isinstance(n, Element) and n.tag == "li"][0]
    eq(li.style["color"], "green", "ul > li matches (child treated as descendant)")


def test_attribute_selector_matches():
    rules = CSSParser("div a[href] { color: blue }").parse()
    eq(len(rules), 1, "attribute selector parses")
    dom = HTMLParser('<div><a href="/x">hi</a><a>no</a></div>').parse()
    style(dom, rules)
    links = [n for n in tree_to_list(dom, [])
             if isinstance(n, Element) and n.tag == "a"]
    eq(links[0].style["color"], "blue", "a[href] matches an anchor with href")
    eq(links[1].style["color"], "black", "anchor without href is not styled")
    rules = CSSParser("a[href] { color: blue } p { color: red }").parse()
    eq(len(rules), 2, "rule after attribute selector still parses")


def test_pseudo_class_structural_and_not():
    rules = CSSParser(
        "li:first-child { color: red } li:nth-child(2n) { color: green } "
        "li:not(.skip) { font-weight: bold }"
    ).parse()
    eq(len(rules), 3, "structural pseudo-classes parse")
    dom = HTMLParser(
        '<ul><li>a</li><li class="skip">b</li><li>c</li><li>d</li></ul>'
    ).parse()
    style(dom, rules)
    lis = [n for n in tree_to_list(dom, [])
           if isinstance(n, Element) and n.tag == "li"]
    eq(lis[0].style["color"], "red", ":first-child matches the first item")
    eq(lis[1].style["color"], "green", ":nth-child(2n) matches even items")
    eq(lis[3].style["color"], "green", ":nth-child(2n) matches the fourth item")
    eq(lis[1].style["font-weight"], "normal", ":not(.skip) excludes .skip")
    eq(lis[2].style["font-weight"], "bold", ":not(.skip) matches others")


def _only_selector(css):
    """The one selector `css` parses to, or None when the rule was dropped."""
    rules = CSSParser(css).parse()
    return rules[0][0] if rules else None


def test_escaped_class_selector_keeps_the_whole_name():
    """A utility-class sheet names classes with characters that are syntax to
    the selector grammar -- `:` `/` `[` `]` -- and escapes them. Matching the
    name with a plain character class does not fail on those, it matches the
    prefix: `.hover\\:bg-red` became `.hover`, a selector that is wrong rather
    than absent. Two thirds of Vimeo's stylesheet went that way."""
    cases = {
        r".md\:flex": "md:flex",
        r".hover\:bg-red": "hover:bg-red",
        r".w-1\/2": "w-1/2",
        r".p-\[10px\]": "p-[10px]",
        r".top-\[-1px\]": "top-[-1px]",
        r".md\:hover\:bg-blue-500": "md:hover:bg-blue-500",
    }
    for text, name in cases.items():
        sel = _only_selector(text + " { color: red }")
        assert sel is not None, "%s must not drop the rule" % text
        eq(sel.kind, "class", "%s is a class selector" % text)
        eq(sel.cls, name, "%s keeps its whole name" % text)


def test_escaped_class_does_not_match_its_own_truncation():
    """The dangerous half of the old bug: the truncated selector still
    matched, so `.md\\:flex` styled every `.md` on the page."""
    rules = CSSParser(r".md\:flex { color: red }").parse()
    eq(len(rules), 1, "the escaped rule survives parsing")
    dom = HTMLParser('<div class="md">a</div>'
                     '<div class="md:flex">b</div>').parse()
    style(dom, rules)
    divs = [n for n in tree_to_list(dom, [])
            if isinstance(n, Element) and n.tag == "div"]
    eq(divs[0].style["color"], "black", ".md is not what .md\\:flex means")
    eq(divs[1].style["color"], "red", "the escaped class matches class=md:flex")


def test_escape_sequences_follow_the_syntax_spec():
    """CSS Syntax 4.3.7: a backslash takes the next character literally, or
    1-6 hex digits name a code point (with one trailing space swallowed as the
    delimiter). Zero, a surrogate, past the Unicode maximum, or nothing at all
    is U+FFFD."""
    cases = {
        r".\41": "A",                    # hex escape, no delimiter needed
        ".\\41 b": "Ab",                 # trailing space is the delimiter
        ".\\31 23": "123",               # a name may not start with a digit
        r".a\ b": "a b",                 # escaped space is a name character
        r".\.dot": ".dot",               # non-hex: taken literally
        r".\0": "\ufffd",                # NUL
        r".\d800": "\ufffd",             # lone surrogate
        r".\110000": "\ufffd",           # past U+10FFFF
    }
    for text, name in cases.items():
        sel = _only_selector(text + " { color: red }")
        assert sel is not None, "%r must parse" % text
        eq(sel.cls, name, "%r unescapes" % text)
    # A backslash with nothing after it: the parser must not run off the end.
    eq(CSSParser("").selector(".\\").cls, "\ufffd",
       "a trailing backslash is U+FFFD")


def test_escapes_in_ids_attributes_and_element_names():
    """The same assumption was repeated for every kind of identifier, so the
    fix has to be too."""
    sel = _only_selector(r"#a\:b { color: red }")
    eq(sel.kind, "id", "escaped id parses")
    eq(sel.id, "a:b", "escaped id keeps its whole name")
    sel = _only_selector(r"[data-x=a\/b] { color: red }")
    eq(sel.kind, "attr", "escaped attribute value parses")
    eq((sel.attr, sel.op, sel.value), ("data-x", "=", "a/b"),
       "attribute value unescapes")
    sel = _only_selector('[data-x="a\\3a b"] { color: red }')
    eq(sel.value, "a:b", "hex escape inside a quoted attribute value")
    sel = _only_selector(r"li\:first { color: red }")
    eq(sel.kind, "tag", "escaped element name parses")
    eq(sel.tag, "li:first", "escaped element name keeps its whole name")
    # And the plain forms still work exactly as they did.
    eq(_only_selector("[disabled] { color: red }").op, None,
       "presence-only attribute selector")
    eq(_only_selector("a[href^='https'] { color: red }").parts[1].value,
       "https", "quoted prefix-match value")


def test_escaped_punctuation_is_not_selector_syntax():
    """An escaped `>` is three glyphs of a class name, not a child
    combinator; an escaped `[` is not a bracket to balance; an escaped comma
    (written `\\2c `) does not end the selector."""
    sel = _only_selector(r".\[\&\>li\]\:mt-2 { color: red }")
    eq(sel.kind, "class", "arbitrary-variant class is one compound")
    eq(sel.cls, "[&>li]:mt-2", "escaped > and [ ] stay in the name")
    sel = _only_selector(r".rgb-\(0\2c 0\) { color: red }")
    eq(sel.cls, "rgb-(0,0)", "escaped comma does not split the selector list")
    rules = CSSParser(r".a\,b { color: red } .c { color: blue }").parse()
    eq(len(rules), 2, "an escaped comma leaves both rules intact")


def test_data_uri_background_parsed():
    css = ('p { background: url(data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"></svg>) }')
    rules = CSSParser(css).parse()
    eq(len(rules), 1, "url() with quotes and < > parses as one rule")


def test_table_cell_content_flows_at_column_width():
    from feetbrowser.layout import DocumentLayout, DrawText
    html = '<table><tr><td>Alpha Bravo Charlie</td><td>Delta Echo</td></tr></table>'
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    words = [c for c in cmds if isinstance(c, DrawText) and c.text in
             ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
    tops = {c.text: c.top for c in words}
    assert tops["Alpha"] == tops["Bravo"] == tops["Charlie"], \
        "cell words share one line instead of wrapping per word"
    assert tops["Delta"] == tops["Echo"], "second cell words share a line"


def test_empty_inline_block_still_paints_its_box():
    """A colour swatch is an empty span with a size and a background. It has
    no text to lay out, which used to mean it was skipped entirely and the
    about:shoes picker showed no colours at all."""
    from feetbrowser.layout import DocumentLayout, DrawRect
    html = ('<p>before <span style="background:#ff0000;display:inline-block;'
            'width:26px;height:12px;"></span> after</p>')
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        cmds.extend(b.paint())
        stack.extend(b.children)
    swatches = [c for c in cmds
                if isinstance(c, DrawRect) and c.color == "#ff0000"]
    eq(len(swatches), 1, "the swatch painted exactly once")
    box = swatches[0]
    eq(box.right - box.left, 26, "swatch keeps its declared width")
    eq(box.bottom - box.top, 12, "swatch keeps its declared height")


def _box_heights(css, html, width=620):
    """Lay out `html` under `css` and return {class name: box height}."""
    from feetbrowser.layout import DocumentLayout
    dom = HTMLParser(html).parse()
    style(dom, CSSParser(css).parse())
    doc = DocumentLayout(dom, width)
    doc.layout()
    out, stack = {}, [doc]
    while stack:
        box = stack.pop()
        node = box.node
        if isinstance(node, Element):
            for cls in node.attributes.get("class", "").split():
                out[cls] = box.height
        stack.extend(box.children)
    return out


def test_line_height_number_sets_the_line_box():
    """Three lines at line-height 1 are three font-sizes tall, and at
    line-height 3 they are nine. The engine used to hardcode 1.25 x (ascent +
    descent) and render both blocks byte-identically."""
    heights = _box_heights(
        ".tight { line-height: 1 } .loose { line-height: 3 }",
        "<div class=tight>one<br>two<br>three</div>"
        "<div class=loose>one<br>two<br>three</div>")
    eq(heights["tight"], 48.0, "3 lines x 16px")     # Chrome: 48
    eq(heights["loose"], 144.0, "3 lines x 48px")    # Chrome: 144


def test_line_height_number_inherits_as_a_factor():
    """A unitless line-height inherits as the *number*, not as the length it
    worked out to on the ancestor: a 32px child of a `line-height: 1.5` body
    gets 48px lines, not 24px ones."""
    heights = _box_heights(
        ".outer { line-height: 1.5 } .inner { font-size: 32px }",
        "<div class=outer>x<div class=inner>y</div></div>")
    eq(heights["inner"], 48.0, "1.5 recomputed against the child's 32px")


def test_line_height_length_and_percentage_inherit_their_result():
    """A length or a percentage computes to a length on the element that
    declares it, and *that* is what inherits -- so the same 32px child gets
    24px lines, the opposite of the unitless case above."""
    for declaration in ("150%", "1.5em", "24px"):
        heights = _box_heights(
            ".outer { font-size: 16px; line-height: %s } "
            ".inner { font-size: 32px }" % declaration,
            "<div class=outer>x<div class=inner>y</div></div>")
        eq(heights["outer"], 48.0,
           "%s: the declaring element's own line is 24px, twice" % declaration)
        eq(heights["inner"], 24.0,
           "%s: the computed 24px is what inherited" % declaration)


def test_line_height_normal_comes_from_the_font():
    """`normal` is the face's own line spacing -- ascent + descent + the line
    gap in the font -- not a constant. The old 1.25 multiplier worked out to
    about 1.33em against Chrome's ~1.15em, so even unstyled text was 15%
    over-leaded."""
    from feetbrowser.layout import _linespace, _metrics
    font = get_font(16, "normal", "roman", "")
    heights = _box_heights("", "<div class=plain>one</div>")
    eq(heights["plain"], float(_linespace(font)),
       "an unstyled line is exactly the font's line spacing")
    ratio = heights["plain"] / 16.0
    assert 1.0 <= ratio <= 1.3, \
        "normal line-height %.3fem is not a plausible font metric" % ratio
    old = 1.25 * (_metrics(font, "ascent") + _metrics(font, "descent"))
    assert heights["plain"] < old, \
        "still using the hardcoded 1.25 leading (%r)" % old


def test_line_height_smaller_than_the_font_is_allowed_to_overlap():
    """CSS 2.1 10.8.1 splits the leading evenly above and below the text box,
    and half of it is negative when line-height is under the font's own
    height. Lines then overlap, which is what the author asked for; clamping
    it would put every tight-leaded heading back where it started."""
    from feetbrowser.layout import DocumentLayout
    heights = _box_heights(".squash { line-height: 4px }",
                           "<div class=squash>one<br>two</div>")
    eq(heights["squash"], 8.0, "two 4px lines, overlapping, not clamped")
    dom = HTMLParser("<div style='line-height:4px'>one<br>two</div>").parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds, stack = [], [doc]
    while stack:
        box = stack.pop()
        cmds.extend(box.paint())
        stack.extend(box.children)
    tops = sorted(c.top for c in cmds if isinstance(c, DrawText))
    eq(len(tops), 2, "both lines drawn")
    eq(tops[1] - tops[0], 4.0, "baselines advance by the line-height")


def test_pre_whitespace_does_not_wrap():
    from feetbrowser.layout import DocumentLayout, DrawText
    css = "pre { white-space: pre; }"
    rules = CSSParser(css).parse()
    html = '<pre>one very long line that must not wrap at all</pre>'
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 200)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    texts = [c for c in cmds if isinstance(c, DrawText)]
    eq(len(texts), 1, "pre line kept on one line")


def test_nowrap_cloud_wraps_as_unit():
    """white-space:nowrap (Wikipedia's language cloud) must not spill past the
    viewport: each link is one unbreakable token, but tokens still wrap to a
    fresh line once the current line runs out of room."""
    from feetbrowser.layout import DocumentLayout, DrawText
    css = '.cloud { width: 200px; } .cloud a { white-space: nowrap; }'
    rules = CSSParser(css).parse()
    links = ' '.join(f'<a href="#{i}">languagename{i:02d}</a>' for i in range(30))
    html = f'<div class="cloud">{links}</div>'
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 200)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    texts = [c for c in cmds if isinstance(c, DrawText)]
    assert texts, "cloud text drawn"
    max_right = max(c.right for c in texts)
    assert max_right <= 200, \
        f"nowrap cloud overflowed viewport: right edge {max_right} > 200"
    tops = {c.text: c.top for c in texts}
    # The whole token moves to the next line, so line tops repeat.
    first_top = tops["languagename00"]
    later_lines = {t for t in tops.values() if t > first_top}
    assert later_lines, "cloud wrapped to multiple lines"


def test_css_data_uri_semicolon():
    css = ('p { background: url(data:image/png;base64,AAAA==);'
           ' color: red; }')
    rules = CSSParser(css).parse()
    eq(len(rules), 1, "data: URI with ; parsed as one rule")
    eq(rules[0][1]["color"], "red", "pair after data: URI intact")


def test_deep_dom_no_recursion():
    depth = 1500
    body = "<div>" * depth + "x" + "</div>" * depth
    dom = HTMLParser(body).parse()
    rules = CSSParser("div { color: blue; }").parse()
    style(dom, rules)  # must not raise RecursionError
    ns = tree_to_list(dom, [])
    assert len(ns) > depth, "tree_to_list built"


def test_double_br_advances_line():
    tab = Tab(700)
    tab._build(URL("https://example.com"), "<p>a<br><br>b</p>", "text/html")
    tops = {c.text: c.top for c in tab.display_list if isinstance(c, DrawText)}
    assert "a" in tops and "b" in tops, tops
    line_h = [c.bottom - c.top for c in tab.display_list
              if isinstance(c, DrawText) and c.text == "a"]
    assert tops["b"] >= tops["a"] + (line_h[0] * 2 - 1), \
        "<br><br> must add two line breaks"

    tab2 = Tab(700)
    tab2._build(URL("https://example.com"), "<p><br></p>", "text/html")
    assert tab2.content_height() > 0, "bare <br> yields nonzero line height"


def test_image_does_not_overlap_following_text():
    tab = Tab(700)
    # A wide image pushes the following word onto the next line, which must
    # start below the image's line box rather than overlapping it. The alt
    # text sizes the placeholder, so its length is computed from the font's
    # actual advance to guarantee an overflow whatever face is in use.
    label_font = get_font(12, "normal", "roman")
    fill = "x" * (int(1600 / _measure(label_font, "x")) + 1)
    tab._build(
        URL("https://example.com"),
        '<p>one<img src=x alt="' + fill + '">two</p>',
        "text/html")
    tops = {c.text: (c.top, c.bottom) for c in tab.display_list
            if isinstance(c, DrawText)}
    assert "one" in tops and "two" in tops
    assert tops["two"][0] > tops["one"][0], \
        "image width forced the wrap onto a new line"
    assert tops["two"][0] >= tops["one"][0] + 8, \
        "line after an image must start below it, not overlap"


def test_table_layout_rows_and_cells():
    from feetbrowser.layout import DocumentLayout, DrawText
    dom = HTMLParser(
        "<table><tr><th>Name</th><th>Age</th></tr>"
        "<tr><td>Ada</td><td>37</td></tr></table>").parse()
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    text_cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                text_cmds.append(cmd.text)
        stack.extend(b.children)
    assert "Ada" in text_cmds and "37" in text_cmds, "cell text painted"
    assert "Name" in text_cmds and "Age" in text_cmds, "header cells painted"
    # Cells must exist as boxes, and the row count must match the DOM.
    rows = [b for b in tree_to_list(doc, []) if b.node.tag == "tr"]
    assert len(rows) == 2, "two table rows laid out"
    for r in rows:
        assert r.height > 0, "rows have nonzero height"
        for c in r.children:
            assert c.width > 0 and c.height > 0, "cells have size"


def test_table_in_flex_does_not_overlap():
    """A table repositioned by flex layout must move its cell content with
    it; otherwise the second table's cells draw on top of the first."""
    from feetbrowser.layout import DocumentLayout, DrawText
    css = "div { display: flex; }"
    rules = CSSParser(css).parse()
    html = ("<div><table><tr><td>alpha</td><td>beta</td></tr></table>"
            "<table><tr><td>gamma</td><td>delta</td></tr></table></div>")
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 620)
    doc.layout()
    texts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            if isinstance(c, DrawText):
                texts.setdefault(c.text, []).append((c.left, c.top))
        stack.extend(b.children)
    ax = texts["alpha"][0][0]
    gx = texts["gamma"][0][0]
    assert gx > ax, f"second table must sit right of first, got {gx} <= {ax}"
    bx = texts["beta"][0][0]
    dx = texts["delta"][0][0]
    assert dx > bx, "second table's cells must not overlap the first's"


def test_image_in_table_cell_sizes_column():
    """A decoded image must size its table column so it doesn't overlap the
    text in the neighbouring cell."""
    from feetbrowser.layout import DocumentLayout, DrawImage, DrawText
    html = ("<table><tr><td><img src='https://example.com/img.png'></td>"
            "<td>zzz</td></tr></table>")
    dom = HTMLParser(html).parse()
    style(dom, [])
    photo = PhotoImage(width=200, height=100)
    cache = {"https://example.com/img.png": photo}
    doc = DocumentLayout(dom, 620)
    doc.image_cache = cache
    doc.layout()
    img, zx = None, None
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            if isinstance(c, DrawImage):
                img = c
            elif isinstance(c, DrawText) and c.text == "zzz":
                zx = c.left
        stack.extend(b.children)
    assert img is not None, "image painted"
    assert zx is not None, "neighbour cell text painted"
    assert zx > img.right, \
        f"neighbour text ({zx}) overlaps the image (ends {img.right})"


def test_url_redirect_adopt():
    """Following an HTTP redirect in place must leave the URL pointing at the
    final host, so relative image/style/script URLs resolve correctly."""
    u = URL("https://google.com/")
    u._adopt(URL("https://www.google.com/path?q=1"))
    eq(str(u), "https://www.google.com/path?q=1", "adopted final URL")
    eq(u.host, "www.google.com", "host updated")
    # A non-redirected URL is untouched.
    v = URL("https://example.com/a")
    eq(str(v), "https://example.com/a")


def test_a_bad_image_is_an_image_error():
    """Image bytes come off the network, so every way a decoder can come
    apart has to arrive as the one exception callers watch for."""
    import struct
    from feetbrowser import imagecodec
    cases = {
        "truncated png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 9,
        "truncated gif": b"GIF89a" + b"\x00" * 8,
        "truncated pnm": b"P5 4 4 255 \x00",
        "not an image": b"<html>",
        # A header is a claim, not a fact: 1.6 billion pixels is 6GB of RGBA.
        "absurd png": b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                      + struct.pack(">IIBBBBB", 40000, 40000, 8, 2, 0, 0, 0)
                      + b"\x00\x00\x00\x00",
    }
    for name, data in cases.items():
        try:
            imagecodec.decode(data)
        except imagecodec.ImageError:
            continue
        except Exception as exc:  # noqa: BLE001 - that is the point
            raise AssertionError("%s raised %r, not ImageError" % (name, exc))
        # A truncated file that still decodes to something is fine.


def test_wide_netpbm_samples_scale_to_maxval():
    """A 16-bit sample is two bytes and still scales against maxval; reading
    only the high byte is right for maxval 65535 and nothing else."""
    import struct
    from feetbrowser import imagecodec
    w, h, rgba = imagecodec.decode(b"P5 1 1 1023 " + struct.pack(">H", 512))
    eq((w, h), (1, 1))
    eq(rgba[0], 127, "roughly half brightness")
    _w, _h, full = imagecodec.decode(b"P5 1 1 1023 " + struct.pack(">H", 1023))
    eq(full[0], 255, "maxval is white")


def test_what_an_img_tag_decodes_and_what_it_does_not():
    """WebP decoded here when Pillow happened to be installed, and does not
    decode at all now. That is a real loss on Google's pages, written down in
    docs/limitations.md rather than hidden, and what matters is the shape of
    the failure: an image we cannot read comes back as None so the layout
    draws its alt text, and never as an exception out of a decoder.

    The content type is not consulted on the way in, because servers get it
    wrong often enough that believing it costs more pictures than ignoring it
    does."""
    photo = Tab._decode_image(_fixture("photo.jpg"), "image/jpeg")
    assert photo is not None, "a JPEG has to decode"
    eq((photo.width(), photo.height()), (320, 224), "JPEG dimensions")
    mislabelled = Tab._decode_image(_fixture("photo.jpg"), "image/png")
    assert mislabelled is not None, "a mislabelled JPEG still has to decode"
    eq((mislabelled.width(), mislabelled.height()), (320, 224))
    eq(Tab._decode_image(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24,
                         "image/webp"), None, "WebP is alt text now")
    eq(Tab._decode_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                         "image/svg+xml"), None, "SVG is alt text now")
    eq(Tab._decode_image(b"", "image/png"), None, "no bytes at all")


def test_float_text_wraps_and_clears():
    from feetbrowser.layout import DrawText, DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (
        ".f { float: left; width: 150px; height: 90px; }"
        ".c { clear: both; }")
    html = (
        "<style>css</style>"
        "<div class=f>FLOATBOX</div>"
        "<p class=wrap>left right</p>"
        "<p class=c>below</p>")
    dom = HTMLParser(html).parse()
    rules = CSSParser(css).parse()
    apply_style(dom, rules)
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()

    tops = {}
    lefts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                tops[cmd.text] = cmd.top
                lefts[cmd.text] = cmd.left
        stack.extend(b.children)

    # The floated box text sits on the left, the <p> text is pushed right of
    # the float's right edge, and the cleared paragraph starts below the float.
    assert tops["FLOATBOX"] <= tops["left"] + 1, "float top at or above wrapping line"
    assert lefts["left"] > 145, "wrapping text indented past 150px-wide float"
    assert tops["below"] >= tops["FLOATBOX"] + 20, "clear pushed paragraph below float"


def _boxes(html, css, width=620, tag="div"):
    """(x, y, width, height) for every box of `tag`, in document order."""
    from feetbrowser.layout import DocumentLayout
    dom = HTMLParser(html).parse()
    style(dom, CSSParser(css).parse())
    doc = DocumentLayout(dom, width)
    doc.layout()
    found, stack = [], [doc]
    while stack:
        box = stack.pop()
        node = getattr(box, "node", None)
        if getattr(node, "tag", None) == tag:
            found.append((box.x, box.y, box.width, box.height))
        stack.extend(box.children)
    return sorted(found, key=lambda b: (b[1], b[0]))


def test_padding_is_counted_once():
    """A block's children start below its padding-top, so the height measured
    from its own top already includes it. Adding it again gave every padded
    card an extra band of empty space and pushed the next one down."""
    boxes = _boxes("<section><div>x</div></section>",
                   "section { padding: 20px } div { height: 50px }",
                   tag="section")
    eq(boxes[0][3], 90.0, boxes)


def test_css_does_arithmetic():
    from feetbrowser.layout import _resolve_len
    cases = [
        ("calc(100% - 240px)", 1000, 760.0),
        ("calc(10px + 18px + 0.25rem)", 0, 32.0),
        ("calc(100%/3)", 900, 300.0),
        ("calc(-1 * 32px)", 0, -32.0),
        ("min(100%, 60rem)", 1600, 960.0),
        ("max(50%, 200px)", 300, 200.0),
        ("clamp(200px, 50%, 400px)", 1000, 400.0),
    ]
    for value, base, want in cases:
        eq(_resolve_len(value, base, -1), want, value)


def test_calc_tokenising_always_advances():
    """`calc(100% -1px)` -- a sign with a space in front of it but none
    behind -- is neither the subtraction operator (CSS wants the space on
    both sides) nor anything the operand scan would accept, so the scan broke
    on the very character it started at, emitted a zero-width token and left
    the cursor where it was. Every length in the sheet went through here, so
    one such value in one stylesheet was a browser that never came back.
    """
    from feetbrowser.layout import _calc_tokens, _resolve_len
    eq(_calc_tokens("100% -1px"), ["100%", "-1px"],
       "a sign glued to its number is part of the operand")
    eq(_calc_tokens("100% - 1px"), ["100%", "-", "1px"],
       "spaced either side, it is still the operator")
    eq(_resolve_len("calc(100px -10px)", 0, -1), 100.0,
       "an unspaced sign reads as one negative operand, not a subtraction")
    for expr in ("-", " - ", "+", "100% -", "- 1px", "()", "(", ")",
                 "1px --2px", "a-b", "* 2", "100%-", "-1px -2px"):
        _calc_tokens(expr)
        _resolve_len("calc(%s)" % expr, 800.0, 0.0)


def test_a_calculated_width_is_used():
    """The value has to survive the parser, not just the resolver -- and a
    page explains its arithmetic in the middle of it."""
    boxes = _boxes("<div><p>x</p></div>",
                   "p { width: calc(100% - /* the gutter */ 120px) }",
                   width=620, tag="p")
    # 620 less the body's own 8px margins is the containing block.
    eq(boxes[0][2], 484.0, boxes)


def test_a_float_starts_below_the_content_it_follows():
    """A "Page 2" link floated at the foot of a listing belongs at the foot.
    Laying every float out before the flow put it over the first story."""
    boxes = _boxes(
        '<article><div class=tall>one</div><div class=tall>two</div>'
        '<div class=more>Page 2</div></article>',
        ".tall { height: 60px } .more { float: left; width: 80px }")
    more = [b for b in boxes if b[2] == 80][0]
    assert more[1] >= 120, ("float sits after the two blocks", more, boxes)


def test_floats_run_along_a_line_before_dropping():
    """Floats are only interesting because they sit beside each other. Ours
    stacked vertically, which turned every 2012 nav bar into a column."""
    boxes = _boxes("<section><div>A</div><div>B</div><div>C</div></section>",
                   "div { float: left; width: 100px; }")
    eq([round(b[0]) for b in boxes], [8, 108, 208], "side by side")
    eq(len({round(b[1]) for b in boxes}), 1, "all on one line")


def test_floats_wrap_when_the_line_runs_out():
    boxes = _boxes(
        "<section><div>A</div><div>B</div><div>C</div></section>",
        "div { float: left; width: 250px; }", width=616)
    tops = [round(b[1]) for b in boxes]
    eq([round(b[0]) for b in boxes], [8, 258, 8], "two fit, the third drops")
    assert tops[0] == tops[1] < tops[2], tops


def test_a_right_float_hugs_the_right_edge():
    boxes = _boxes(
        "<section><div id=l>A</div><div id=r>B</div></section>",
        "#l { float: left; width: 100px } #r { float: right; width: 100px }")
    eq([round(b[0]) for b in boxes], [8, 512], "one each side")
    eq(len({round(b[1]) for b in boxes}), 1, "and both on the top line")


def test_percentage_widths_on_floats_come_from_the_container():
    """`width: 16.6667%` six times over is how a six-across nav bar was
    built before flexbox, and it has to stay on one line."""
    items = "".join("<li>%d</li>" % i for i in range(6))
    boxes = _boxes("<ul>" + items + "</ul>",
                   "li { float: left; width: 16.6667%; list-style: none }"
                   "ul { margin: 0; padding: 0 }", width=1016, tag="li")
    eq(len({round(b[1]) for b in boxes}), 1, "all six on one line")
    eq([round(b[2]) for b in boxes], [167] * 6, "each a sixth of 1000px")


def test_clear_left_only_clears_left_floats():
    from feetbrowser.layout import DrawText, DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (
        ".l { float: left; width: 100px; }"
        ".r { float: right; width: 100px; }"
        ".cl { clear: left; }")
    html = (
        "<style>css</style>"
        "<div class=l>A</div><div class=r>C</div><div class=cl>D</div>")
    dom = HTMLParser(html).parse()
    rules = CSSParser(css).parse()
    apply_style(dom, rules)
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    tops = {}
    lefts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                tops[cmd.text] = cmd.top
                lefts[cmd.text] = cmd.left
        stack.extend(b.children)
    # The cleared div goes below the left float but keeps sharing the line
    # with the right float (right floats are not cleared by clear:left).
    assert tops["D"] >= tops["A"] - 1, "D below left float"
    assert tops["D"] < tops["A"] + 90, "D only cleared the left side"


def test_data_image_placeholder():
    tab = Tab(700)
    tab.load("data:image/png;base64,iVBORw0KGgo=")
    assert tab.document is not None, "image page rendered"
    assert any("img" in c.text.lower() for c in tab.display_list
               if isinstance(c, DrawText)), "image labelled as placeholder"


def test_grid_columns_auto_placement_and_span():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = ".g { display: grid; grid-template-columns: 100px 1fr 2fr; gap: 10px; }"
    html = ("<style>css</style><div class=g>"
            "<div class=a>A</div><div class=b>B</div><div class=c>C</div>"
            "<div class=d>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 700)
    doc.layout()
    items = {b.node.attributes.get("class"): b
             for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "g")}
    # First three items fill the first row across the three tracks.
    assert items["a"].x == 8 and items["a"].width == 100, "first track is 100px"
    assert items["b"].x == 118, "second track starts after gap"
    assert abs(items["b"].width - 188) < 1, f"1fr track {items['b'].width}"
    assert abs(items["c"].width - 376) < 1, f"2fr track {items['c'].width}"
    assert items["a"].y == items["b"].y == items["c"].y, "first row baseline"
    # Fourth item auto-wraps to the next row.
    assert items["d"].y > items["a"].y, "fourth item wrapped to a new row"
    assert items["d"].x == 8, "wrapped item starts at the first track"

    # A spanning item absorbs its columns.
    html2 = ("<style>css</style><div class=g>"
             "<div style='grid-column: span 2'>AB</div><div>C</div></div>")
    dom2 = HTMLParser(html2).parse()
    apply_style(dom2, CSSParser(css).parse())
    body2 = next(n for n in tree_to_list(dom2, [])
                 if getattr(n, "tag", "") == "body")
    doc2 = DocumentLayout(body2, 700)
    doc2.layout()
    items2 = [b for b in tree_to_list(doc2, []) if b.node.tag == "div"
              and b.node.attributes.get("class") != "g"]
    span = items2[0]
    assert abs(span.width - (100 + 10 + 188)) < 1, f"span width {span.width}"


def test_flex_row_grow_and_justify():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    # justify-content: center: no growth, so leftover space is freed.
    css = ".f { display: flex; justify-content: center; gap: 10px; }"
    html = ("<style>css</style><div class=f>"
            "<div class=a>AA</div><div class=b>BB</div>"
            "<div class=c>CC</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    assert all(b.x > doc.children[0].x for b in items), \
        "centered row shifted right of container"
    assert len({int(b.y) for b in items}) == 1, "items share the row baseline"

    # flex-grow: 1: the last item absorbs every leftover pixel.
    css = ".f { display: flex; gap: 10px; } .c { flex-grow: 1; }"
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    a = next(b for b in items if b.node.attributes.get("class") == "a")
    b = next(b for b in items if b.node.attributes.get("class") == "b")
    c = next(b for b in items if b.node.attributes.get("class") == "c")
    assert b.x + b.width < c.x, "flex items do not overlap"
    assert a.x < b.x < c.x, "items laid out left to right"
    assert c.x + c.width <= 8 + 604, "growing item stays inside container"


def test_flex_column_stacks_vertically():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = ".f { display: flex; flex-direction: column; gap: 5px; }"
    html = ("<style>css</style><div class=f>"
            "<div>A</div><div>B</div><div>C</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    ys = sorted(b.y for b in items)
    # One line of text plus the 5px gap. Derived from the font rather than
    # hard-coded, because line height depends on whichever face the GUI
    # backend resolved for the default family.
    step = get_font(16, "normal", "roman").metrics("linespace") + 5
    assert ys[1] - ys[0] >= step, "second item starts below first (gap)"
    assert ys[2] - ys[1] >= step, "third item starts below second (gap)"
    # All column items span the full container width (stretch).
    for b in items:
        assert b.width == 604, f"column item width {b.width}"


def test_flex_wrap_rows_onto_new_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; gap: 4px; }"
           ".a { width: 100px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    line1, line2 = items[0:2], items[2:4]
    assert all(b.y == line1[0].y for b in line1), "first two items share line one"
    assert all(b.y == line2[0].y for b in line2), "last two items share line two"
    assert line2[0].y > line1[0].y, "second line sits below the first"
    assert line2[0].x == line1[0].x, "each line starts at the container edge"


def test_flex_wrap_with_gap():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; row-gap: 10px; column-gap: 20px; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    assert b.x >= a.x + a.width + 20 - 1, "column-gap separates line-one items"
    assert d.x >= c.x + 90 + 20 - 1, "line two items also honor column-gap"
    assert c.y >= a.y + a.height + 10 - 1, "row-gap separates the two lines"
    assert d.y == c.y, "line-two items share a row"


def test_flex_wrap_justify_per_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; justify-content: space-between; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    container = next(b for b in tree_to_list(doc, [])
                     if b.node.attributes.get("class") == "f")
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    # space-between runs independently per line: line one pins its first item
    # at the container's left edge and pushes its second to the right edge...
    assert a.x == container.x, "line-one first item at container start"
    assert b.x > a.x + a.width, "space-between pushed the second item right"
    # ...while line two starts over from the container's left edge again.
    assert c.x == container.x, "line-two first item at container start"
    assert d.x > c.x + c.width, "line-two items also spaced apart"
    assert c.y > a.y, "line two is below line one"


def test_flex_wrap_align_content_center():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; align-content: center; "
           "height: 300px; row-gap: 10px; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div><div class=a>E</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    container = next(b for b in tree_to_list(doc, [])
                     if b.node.attributes.get("class") == "f")
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    top_item = min(items, key=lambda b: b.y)
    bottom_item = max(items, key=lambda b: b.y + b.height)
    top_gap = top_item.y - container.y
    bottom_gap = (container.y + container.height
                  - (bottom_item.y + bottom_item.height))
    assert top_gap > 0, "wrapped lines pushed down from the container top"
    assert abs(top_gap - bottom_gap) < 1, "line block centered vertically"


def test_flex_wrap_align_items_per_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; align-items: flex-end; }"
           ".a { width: 90px; }"
           ".b { width: 90px; height: 60px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=b>B</div>"
            "<div class=a>C</div><div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    # flex-end hangs each item from its line's bottom, so all bottoms on a
    # line line up even though the 60px item rules the line's height.
    assert abs((a.y + a.height) - (b.y + b.height)) < 1, \
        "flex-end aligns items to the bottom of their line"
    assert abs((c.y + c.height) - (d.y + d.height)) < 1, \
        "line two also bottom-aligned"
    assert c.y > a.y, "second line sits below the first"


def test_flex_column_wrap_columns_side_by_side():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-direction: column; flex-wrap: wrap; "
           "height: 200px; column-gap: 10px; }"
           ".a { width: 80px; height: 60px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div><div class=a>E</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d, e = items
    assert b.y > a.y and c.y > b.y, "first column stacks its items vertically"
    assert c.x == a.x, "third item stays in the first column"
    assert d.x > a.x, "fourth item flowed into a second column to the right"
    assert d.x == e.x, "second-column items share an x"
    assert abs(d.x - (a.x + a.width + 10)) < 1, "column-gap separates columns"
    assert e.y > d.y, "second column stacks its items"
    assert d.y == a.y, "both columns top out at the container top"


def test_flex_wrap_reverse_orders_lines_bottom_up():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap-reverse; row-gap: 8px; }"
           ".a { width: 100px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    assert a.y == b.y, "first line items share a row"
    assert c.y == d.y, "second line items share a row"
    assert a.y > c.y, "wrap-reverse puts the first line below the second"
    assert a.x == c.x, "lines still start at the container edge"


def test_data_image_pipeline_renders_drawimage():
    import base64
    import struct
    import zlib as _z
    from feetbrowser.layout import DrawImage

    # Build a tiny valid 2x2 PNG in memory (no Pillow dependency).
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    def png(w, h):
        rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
        raw = b"\x89PNG\r\n\x1a\n"
        raw += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", _z.compress(rows))
        raw += chunk(b"IEND", b"")
        return raw

    b64 = base64.b64encode(png(2, 2)).decode()
    tab = _make_tab(f'<p>before</p><img src="data:image/png;base64,{b64}">'
                    '<p>after</p>')
    # No image fetched yet -> placeholder text rendered.
    assert any("img" in c.text.lower() for c in tab.display_list
               if isinstance(c, DrawText)), "placeholder before image ready"
    # load_images synchronously (root is None path).
    tab.load_images(None)
    assert tab.image_cache, "image_cache populated"
    assert any(isinstance(c, DrawImage) for c in tab.display_list), \
        "DrawImage emitted after image loaded"
    # Re-rendering no longer emits the placeholder text.
    assert not any("img" in c.text.lower() for c in tab.display_list
                   if isinstance(c, DrawText)), "placeholder replaced"


def test_clicking_an_image_follows_its_enclosing_link():
    """A click on a photo wrapped in an `<a>` navigates, like any browser.

    DrawImage used to carry the `<img>` node for hit-testing but no `hit()`
    of its own, so `_node_at()` skipped it and a click on the picture fell
    through to whatever was underneath. On a thumbnail grid such as
    safebooru's browse page -- where every thumbnail is wrapped in a link to
    the post page -- that meant nothing happened when you clicked a photo.
    """
    import base64
    import struct
    import zlib as _z
    from feetbrowser.layout import DrawImage

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    def png(w, h):
        rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
        raw = b"\x89PNG\r\n\x1a\n"
        raw += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", _z.compress(rows))
        raw += chunk(b"IEND", b"")
        return raw

    b64 = base64.b64encode(png(8, 8)).decode()
    tab = _make_tab(
        f'<a href="/post/1"><img src="data:image/png;base64,{b64}"></a>')
    tab.load_images(None)
    drawn = [c for c in tab.display_list if isinstance(c, DrawImage)]
    assert drawn, "the image should have decoded"
    img = drawn[0]
    x, y = (img.left + img.right) / 2, (img.top + img.bottom) / 2
    eq(str(tab.click(x, y)), "https://example.com/post/1",
       "a click on the image follows the link around it")
    eq(tab.link_at(x, y), "/post/1", "hovering the image reports the link")


def test_base_href_detected():
    dom = HTMLParser("<head><base href='/sub/'></head><body>x</body>").parse()
    eq(find_base_href(dom), "/sub/")


def _make_tab(body, url="https://example.com/page"):
    tab = Tab(700)
    u = URL(url)
    tab.url = u
    tab._build(u, body, "text/html")
    return tab


def _control_box(tab, **attrs):
    """The centre point and node of the first form control whose attributes
    match, as (x, y, node) -- i.e. where a user would click it."""
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and all(
                n.attributes.get(k) == v for k, v in attrs.items()):
            return ((lx + rx) / 2, (ty + by) / 2, n)
    return None


def test_page_text_selection():
    tab = _make_tab("<p>Hello world foo bar</p>")
    words = {c.text: c for c in tab.display_list
             if isinstance(c, DrawText) and c.text in
             ("Hello", "world", "foo", "bar")}
    eq(len(words), 4, "words laid out individually")
    hello, foo = words["Hello"], words["foo"]
    # Drag from the start of "Hello" to just past the end of "foo".
    tab.start_selection(hello.left, hello.top + 2)
    assert tab.selection is not None, "selection anchored on press"
    tab.extend_selection(foo.right + 1, foo.top + 2)
    eq(tab.selected_text(), "Hello world foo",
       f"selected text: {tab.selected_text()!r}")
    # Selecting backwards (end above anchor) still yields the right text.
    tab.start_selection(foo.right + 1, foo.top + 2)
    tab.extend_selection(hello.left, hello.top + 2)
    eq(tab.selected_text(), "Hello world foo", "backwards drag selects same text")
    # A zero-width (plain click) selection selects nothing.
    tab.start_selection(hello.left, hello.top + 2)
    tab.extend_selection(hello.left, hello.top + 2)
    eq(tab.selected_text(), "", "zero-width selection is empty")
    tab.selection = None
    eq(tab.selected_text(), "", "cleared selection is empty")


def test_tab_title_truncated_in_draw_tabs():
    """Long page titles must be truncated so they never spill past the tab
    edge (issue #32). The truncation runs in _draw_tabs; exercise the width
    math directly on a stub."""
    stub = type("Stub", (), {})()
    stub.chrome_font = get_font(14, "normal", "roman", "Helvetica")
    title = "frog - DuckDuckGo - search the whole web and never stop"
    title_w = 128
    if _measure(stub.chrome_font, title) > title_w:
        t = title
        while t and _measure(stub.chrome_font, t + "…") > title_w:
            t = t[:-1]
        title = t + "…"
    assert title.endswith("…"), "truncated title shows an ellipsis"
    assert _measure(stub.chrome_font, title) <= title_w + 6, \
        "truncated title fits the tab before the close box"


def test_load_errors_are_collected():
    import tempfile
    html_body = (
        '<html><head>'
        '<link rel="stylesheet" href="http://127.0.0.1:1/x.css">'
        '<script src="http://127.0.0.1:1/y.js"></script>'
        '</head><body><p>hi</p></body></html>')
    fd, path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(html_body)
        tab = Tab(700)
        tab.load("file://" + path)
    finally:
        os.unlink(path)
    kinds = [e.split()[0] for e in tab.net_errors]
    assert "CSS" in kinds, f"CSS failure logged, got {kinds}"
    assert "JS" in kinds, f"JS failure logged, got {kinds}"


def test_doc_error_is_collected():
    tab = Tab(700)
    tab.load("http://127.0.0.1:1/")
    assert tab.net_errors and tab.net_errors[0].startswith("DOC"), \
        tab.net_errors


def test_import_lead_char_preserved():
    """An @import statement matched at a statement boundary must not eat the
    character before it (e.g. the `}` closing the previous rule)."""
    from feetbrowser.browser import _expand_imports
    from feetbrowser.net import URL
    css = ("a{color:red}@import 'http://127.0.0.1:1/nope.css';"
           "b{color:blue}")
    out = _expand_imports(css, URL("https://example.com/"))
    assert "a{color:red}" in out, out
    assert "b{color:blue}" in out, out


def test_media_query_em_units():
    """em/rem media-feature values are resolved at the 16px root size, not
    read as raw pixel numbers."""
    from feetbrowser.cssparser import media_matches
    assert media_matches("(min-width: 40em)", 800, 600), "640px breakpoint matched"
    assert not media_matches("(min-width: 40em)", 600, 600), "600px < 640px"
    assert media_matches("(max-width: 40rem)", 600, 600), "rem max-width matched"
    assert not media_matches("(max-width: 40rem)", 700, 600), "700px > 640px"


def test_viewport_accessor_tracks_set_viewport():
    from feetbrowser.cssparser import get_viewport, set_viewport
    set_viewport(1234, 567)
    assert get_viewport() == (1234.0, 567.0)
    set_viewport(1000, 720)


def test_js_errors_captured_once():
    """_capture_js_errors must not re-count errors already scanned."""
    tab = _make_tab("<script>throw new Error('boom')</script>")
    js = lambda: sum(1 for e in tab.net_errors if e.startswith("JS"))
    before = js()
    assert before > 0, "page-load JS error captured"
    tab._capture_js_errors(tab._js_interp.logs)  # a re-dispatch re-scan
    assert js() == before, "errors must not be double-counted"


def test_form_submit_get():
    tab = _make_tab(
        '<form action="/submit"><input name="q" value="hello world">'
        '<input type="submit" value="Go"></form>')
    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    assert pos is not None
    act = tab.click(*pos)
    assert isinstance(act, FormAction), type(act)
    assert act.payload is None
    assert str(act.url).startswith("https://example.com/submit")
    assert "q=hello+world" in str(act.url), act.url


def test_form_submit_post_and_typing():
    tab = _make_tab(
        '<form method="post" action="/save"><input name="name">'
        '<textarea name="notes"></textarea>'
        '<input type="submit"></form>')
    # Focus the text field and type into it.
    hit = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and not n.attributes.get("type"):
            hit = n
            cx, cy = (lx + rx) / 2, (ty + by) / 2
    assert hit is not None
    tab.click(cx, cy)
    assert tab.focused_input is hit, "click focused the input"
    tab.type_char("a")
    tab.type_char("b")
    eq(hit.attributes["value"], "ab", "typed chars stored")

    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    act = tab.click(*pos)
    assert isinstance(act, FormAction)
    assert act.payload is not None and "name=ab" in act.payload, act.payload


def test_form_submit_merges_existing_query():
    tab = _make_tab(
        '<form action="/search?lang=en"><input name="q" value="hello world">'
        '<input type="submit"></form>')
    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    act = tab.click(*pos)
    assert isinstance(act, FormAction)
    assert str(act.url) == \
        "https://example.com/search?lang=en&q=hello+world", str(act.url)


def test_checkbox_toggle():
    tab = _make_tab(
        '<form action="/r"><input type="checkbox" name="c" value="blue">'
        '<input type="submit"></form>')
    box = _control_box(tab, type="checkbox")
    assert box is not None, "no checkbox box found"
    cx, cy, node = box
    assert not field_checked(node), "unticked until clicked"
    tab.click(cx, cy)
    assert field_checked(node), "click ticks the box"
    eq(node.attributes.get("value"), "blue", "the submitted value survives")
    tab.click(cx, cy)
    assert not field_checked(node), "a second click unticks it"


def test_form_controls_do_not_stack_on_one_another():
    """Controls paint straight into the display list, so the line they sit on
    has to grow to fit them -- otherwise every control on the page lands at
    the same y and a click reaches whichever hit box happens to be first."""
    tab = _make_tab(
        '<form action="/a"><input name="q"><input type="submit" value="Go">'
        '</form>'
        '<form method="post" action="/b"><input name="w">'
        '<input type="submit" value="Send"></form>')
    boxes = tab.document.input_boxes
    eq(len(boxes), 4, "one hit box per control")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert (a[2] <= b[0] or b[2] <= a[0]
                    or a[3] <= b[1] or b[3] <= a[1]), \
                f"controls overlap: {a[:4]} and {b[:4]}"
    # The point that draws "Send" must submit the form "Send" belongs to.
    cx, cy, _ = _control_box(tab, value="Send")
    act = tab.click(cx, cy)
    assert isinstance(act, FormAction), type(act)
    eq(str(act.url), "https://example.com/b", "second form's action")


def test_form_submit_collects_every_kind_of_field():
    tab = _make_tab(
        '<form method="post" action="/save">'
        '<input name="user" value="ada">'
        '<input type="hidden" name="csrf" value="t0ken">'
        '<input type="checkbox" name="cc" value="yes">'
        '<input type="checkbox" name="news" value="daily" checked>'
        '<input type="text" name="ghost" value="x" disabled>'
        '<textarea name="body">from markup</textarea>'
        '<select name="colour"><option>red<option selected>blue</select>'
        '<input type="submit" name="do" value="Save">'
        '<input type="submit" name="do" value="Delete"></form>')
    cx, cy, _ = _control_box(tab, value="Save")
    act = tab.click(cx, cy)
    assert isinstance(act, FormAction), type(act)
    fields = urllib.parse.parse_qsl(act.payload, keep_blank_values=True)
    eq(fields, [("user", "ada"), ("csrf", "t0ken"), ("news", "daily"),
                ("body", "from markup"), ("colour", "blue"), ("do", "Save")],
       "submitted fields")


def _key_stub(tab, clipboard=""):
    """A Browser stripped down to what _on_key touches, wired to `tab` and to
    a clipboard that either holds `clipboard` or, when that is None, refuses
    to be read the way the real one does when nothing text-shaped is on it."""
    def read():
        if clipboard is None:
            raise CanvasError("CLIPBOARD selection doesn't exist")
        return clipboard

    class Stub(Browser):
        def __init__(self):
            self.focus = None
            self.active_tab = tab
            self.toe_contexts = []
            self.context_menu = type("Menu", (), {"open_": False})()
            self.select_popup = SelectPopup()
            self.window = type("Win", (), {"clipboard_get": staticmethod(read)})()
            self.painted = 0

        def _draw_page(self):
            self.painted += 1

    return Stub()


def _key_event(keysym, char="", ctrl=False):
    return type("Event", (), {"keysym": keysym, "char": char,
                              "state": 0x4 if ctrl else 0})()


def test_paste_into_page_field():
    tab = _make_tab('<form action="/s"><input name="q"></form>')
    cx, cy, node = _control_box(tab, name="q")
    tab.click(cx, cy)
    assert tab.focused_input is node, "click focused the field"

    browser = _key_stub(tab, "hello from the clipboard")
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"], "hello from the clipboard", "pasted value")
    eq(browser.painted, 1, "the page was repainted once")

    # Typing still appends after a paste, and pastes accumulate.
    Browser._on_key(browser, _key_event("exclam", "!"))
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"],
       "hello from the clipboard!hello from the clipboard", "paste appends")


def test_paste_folds_newlines_only_in_single_line_fields():
    tab = _make_tab('<form action="/s"><input name="q">'
                    '<textarea name="body"></textarea></form>')
    _cx, _cy, field = _control_box(tab, name="q")
    tab.focused_input = field
    assert tab.insert_text("one\ntwo")
    eq(field.attributes["value"], "one two", "single-line field folds breaks")

    _cx, _cy, area = _control_box(tab, name="body")
    tab.focused_input = area
    assert tab.insert_text("one\ntwo")
    eq(area.attributes["value"], "one\ntwo", "a textarea keeps them")


def test_paste_without_a_clipboard_is_a_no_op():
    tab = _make_tab('<form action="/s"><input name="q" value="kept"></form>')
    _cx, _cy, node = _control_box(tab, name="q")
    tab.focused_input = node
    browser = _key_stub(tab, clipboard=None)
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"], "kept", "unreadable clipboard changes nothing")
    eq(browser.painted, 0, "and costs no repaint")


def test_about_blank_typed():
    assert Browser._looks_like_url("about:blank")
    assert Browser._looks_like_url("localhost:8000")
    assert Browser._looks_like_url("192.168.1.1:80")
    assert not Browser._looks_like_url("hello world")


def test_gt_inside_quoted_attribute_does_not_close_tag():
    dom = HTMLParser('<a href="x?a=1&b=2">link</a>').parse()
    a = [n for n in tree_to_list(dom, []) if isinstance(n, Element) and n.tag == "a"]
    eq(len(a), 1, "only one <a> expected")
    eq(a[0].attributes.get("href"), "x?a=1&b=2", "href preserved intact")


def test_eof_inside_tag_flushes_character_data():
    dom = HTMLParser("<p>hello <b>world").parse()
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "world" in texts, "unterminated <b> text must not be lost"


def test_eof_inside_script_flushes_raw_text():
    dom = HTMLParser("<script>var x = 1;").parse()
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "var x = 1;" in texts, "unterminated <script> body must not be lost"


def test_charset_unknown_falls_back_to_utf8():
    from feetbrowser.net import URL as _URL
    eq(_URL._charset({"content-type": "text/html; charset=charset=X-IMAGINARY"}), "utf8")
    eq(_URL._charset({"content-type": 'text/html; charset="iso-8859-1"'}), "iso-8859-1")
    eq(_URL._charset({"content-type": "text/html"}), "utf8")


def test_resolve_color_handles_css_color_functions():
    from feetbrowser.layout import resolve_color
    eq(resolve_color("rgba(0,0,0,0)"), None, "fully transparent -> no paint")
    eq(resolve_color("rgba(0, 0, 0, 0)"), None, "spaced transparent rgba")
    eq(resolve_color("rgb(255,0,0)"), "#ff0000", "rgb")
    eq(resolve_color("rgba(0,128,255,0.5)"), "#0080ff", "rgba with alpha")
    eq(resolve_color("rgb(255 0 0 / 0.25)"), "#ff0000", "modern space/slash rgb")
    eq(resolve_color("rgb(100%, 50%, 0%)"), "#ff8000", "percentage rgb")
    eq(resolve_color("hsl(120, 100%, 50%)"), "#00ff00", "hsl")
    eq(resolve_color("hsla(0, 100%, 50%, 0)"), None, "transparent hsla")
    eq(resolve_color("#fff"), "#ffffff", "3-digit hex expanded")
    eq(resolve_color("#ff000000"), None, "8-digit hex with alpha 0")
    eq(resolve_color("transparent"), None, "transparent keyword")
    eq(resolve_color("red"), "red", "named color passes through")


def _paint_all(html, css="", width=620, ua=False):
    """Lay a fragment out and collect every paint command in the tree."""
    from feetbrowser.layout import DocumentLayout
    rules = []
    if ua:
        from feetbrowser.browser import DEFAULT_STYLE_SHEET
        rules += DEFAULT_STYLE_SHEET
    if css:
        rules += CSSParser(css).parse()
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, width)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        box = stack.pop()
        cmds.extend(box.paint())
        stack.extend(box.children)
    return cmds


def test_border_shorthand_paints_all_four_edges():
    """`border: 2px solid` used to paint nothing at all -- borders were a
    hardcoded grey outline on table cells and nowhere else."""
    from feetbrowser.layout import DrawRect
    cmds = _paint_all(
        '<div style="border:2px solid #3b6ea5;width:100px;height:40px"></div>')
    edges = [c for c in cmds
             if isinstance(c, DrawRect) and c.color == "#3b6ea5"]
    eq(len(edges), 4, "one filled rect per side")
    for edge in edges:
        assert min(edge.right - edge.left, edge.bottom - edge.top) == 2, \
            f"edge is 2px thick: {edge.right - edge.left}x{edge.bottom - edge.top}"


def test_border_is_painted_inside_the_box():
    """Borders sit inside the box edge so adding one never shifts layout."""
    from feetbrowser.layout import DrawRect
    cmds = _paint_all(
        '<div style="background:#eeeeee;border:4px solid #000000;'
        'width:120px;height:50px"></div>')
    bg = [c for c in cmds if isinstance(c, DrawRect) and c.color == "#eeeeee"][0]
    for edge in [c for c in cmds
                 if isinstance(c, DrawRect) and c.color == "#000000"]:
        assert bg.left <= edge.left and edge.right <= bg.right, "inside x"
        assert bg.top <= edge.top and edge.bottom <= bg.bottom, "inside y"


def test_border_width_without_a_style_paints_nothing():
    """`border-style` initially is `none`, so a width on its own is invisible.
    Getting this wrong puts a black box around half the web."""
    from feetbrowser.layout import DrawRect
    cmds = _paint_all(
        '<div style="border-width:6px;border-color:red;'
        'width:80px;height:30px"></div>')
    eq([c for c in cmds if isinstance(c, DrawRect) and c.color == "red"], [],
       "no style means no border")


def test_border_side_beats_the_shorthand():
    """Declaration order does not decide this -- specificity within the
    border family does: `border-left` always wins over `border`."""
    from feetbrowser.layout import DrawRect
    cmds = _paint_all(
        '<div style="border-left:5px solid #ff0000;border:1px solid #000000;'
        'width:90px;height:40px"></div>')
    reds = [c for c in cmds if isinstance(c, DrawRect) and c.color == "#ff0000"]
    eq(len(reds), 1, "just the left edge is red")
    eq(reds[0].right - reds[0].left, 5, "and it keeps its own width")
    eq(len([c for c in cmds
            if isinstance(c, DrawRect) and c.color == "#000000"]), 3,
       "the other three sides come from the shorthand")


def test_border_clock_order_and_omitted_colour():
    """`border-width: 1px 2px 3px 4px` runs top/right/bottom/left, and a
    shorthand with no colour picks up `color`."""
    from feetbrowser.layout import _border_box
    sides = _border_box({"border-style": "solid",
                         "border-width": "1px 2px 3px 4px",
                         "color": "#123456"})
    eq(sides["top"][0], 1.0)
    eq(sides["right"][0], 2.0)
    eq(sides["bottom"][0], 3.0)
    eq(sides["left"][0], 4.0)
    eq(sides["top"][1], "#123456", "currentColor fills in for the colour")
    eq(_border_box({"border": "solid red"})["top"][0], 3.0,
       "an omitted width is medium")


def test_block_padding_insets_its_children():
    """A padded card used to lay its text out flush against its own border,
    because padding only ever applied to inline content."""
    from feetbrowser.layout import DrawText
    cmds = _paint_all('<div style="padding:20px"><p>Inside</p></div>')
    word = [c for c in cmds if isinstance(c, DrawText) and c.text == "Inside"][0]
    assert word.left >= 20, f"text starts past the left padding: {word.left}"
    assert word.top >= 20, f"text starts below the top padding: {word.top}"


def test_block_padding_narrows_the_content_box():
    """Padding takes width away from children instead of letting them spill
    over the right edge of the box."""
    from feetbrowser.layout import DrawRect
    cmds = _paint_all(
        '<div style="padding:25px;width:300px">'
        '<div style="background:#00ff00;height:10px"></div></div>')
    fill = [c for c in cmds if isinstance(c, DrawRect) and c.color == "#00ff00"][0]
    eq(fill.right - fill.left, 250, "300 wide minus 25 of padding each side")


def test_block_inside_inline_lays_out_as_a_block():
    """A card wrapped in a link -- <a><div>..</div><div>..</div></a> -- put
    every div on one line, because the <a> looked inline to layout_mode."""
    from feetbrowser.layout import DrawText
    cmds = _paint_all(
        '<div><a href="#"><div>First</div><div>Second</div></a></div>')
    tops = {c.text: c.top for c in cmds if isinstance(c, DrawText)}
    assert tops["Second"] > tops["First"], \
        f"the two blocks stack: {tops}"


def test_inline_wrapper_without_blocks_stays_inline():
    """The flip side: a plain link keeps flowing with the text around it."""
    from feetbrowser.layout import DrawText
    cmds = _paint_all('<div>before <a href="#">link</a> after</div>')
    tops = {c.text: c.top for c in cmds if isinstance(c, DrawText)}
    eq(tops["before"], tops["link"], "same line")
    eq(tops["link"], tops["after"], "same line")


def test_ordered_list_numbers_honour_start_and_value():
    from feetbrowser.layout import DrawText
    cmds = _paint_all(
        '<ol start="3"><li>a</li><li value="9">b</li><li>c</li></ol>',
        ua=True)
    markers = sorted(
        (c.top, c.text) for c in cmds
        if isinstance(c, DrawText) and c.text.endswith("."))
    eq([text for _top, text in markers], ["3.", "9.", "10."],
       "start seeds the count and value resets it")


def test_list_style_type_covers_the_counting_styles():
    from feetbrowser.layout import _marker_text
    eq(_marker_text("decimal", 4), "4.")
    eq(_marker_text("decimal-leading-zero", 4), "04.")
    eq(_marker_text("lower-alpha", 27), "aa.")
    eq(_marker_text("upper-alpha", 2), "B.")
    eq(_marker_text("lower-roman", 14), "xiv.")
    eq(_marker_text("upper-roman", 1990), "MCMXC.")
    eq(_marker_text("disc", 1), None, "shapes are drawn, not written")


def test_nested_bullets_change_shape():
    """disc, then circle, then square -- what every default sheet does."""
    from feetbrowser.layout import DrawOval, DrawRect
    cmds = _paint_all(
        '<ul><li>one<ul><li>two<ul><li>three</li></ul></li></ul></li></ul>',
        ua=True)
    ovals = [c for c in cmds if isinstance(c, DrawOval)]
    eq(len([o for o in ovals if o.fill]), 1, "one filled disc")
    eq(len([o for o in ovals if o.outline and not o.fill]), 1, "one ring")
    marks = [c for c in cmds if isinstance(c, DrawRect)
             and c.right - c.left == 6 and c.bottom - c.top == 6]
    eq(len(marks), 1, "one square")


def test_list_style_none_shorthand_reaches_the_items():
    """`list-style` does not inherit, but the type it sets does -- so the
    shorthand has to be expanded at cascade time or `list-style: none` on a
    <ul> leaves every bullet in place."""
    from feetbrowser.layout import DrawOval
    cmds = _paint_all(
        '<ul style="list-style:none"><li>one</li><li>two</li></ul>', ua=True)
    eq([c for c in cmds if isinstance(c, DrawOval)], [], "no bullets left")


def test_list_style_shorthand_keeps_its_position_component():
    from feetbrowser.cssparser import _expand
    eq(dict(_expand("list-style", "square outside")),
       {"list-style": "square outside", "list-style-type": "square",
        "list-style-position": "outside"})
    eq(dict(_expand("color", "red")), {"color": "red"},
       "other properties pass through untouched")


def test_list_item_with_block_content_still_gets_a_marker():
    """Markers were drawn on the inline path only, so an <li> holding a <div>
    silently lost its bullet once block-in-inline started working."""
    from feetbrowser.layout import DrawOval
    cmds = _paint_all('<ul><li><div>boxed</div></li></ul>', ua=True)
    eq(len([c for c in cmds if isinstance(c, DrawOval)]), 1, "bullet survives")


def test_text_decoration_none_wins_over_the_ua_underline():
    """text-decoration does not inherit; the nearest box that declares one
    decides. That is the whole reason `a { text-decoration: none }` works."""
    from feetbrowser.layout import DrawLine
    underlined = _paint_all('<p><a href="#">plain</a></p>', ua=True)
    assert [c for c in underlined if isinstance(c, DrawLine)], \
        "links underline by default"
    bare = _paint_all('<p><a href="#">plain</a></p>',
                      css="a { text-decoration: none; }", ua=True)
    eq([c for c in bare if isinstance(c, DrawLine)], [], "and the page can say no")


def test_light_dark_resolves_to_the_light_side():
    """Sites that theme themselves entirely through light-dark() used to come
    out as a black slab, because the unparsed function fell through to the
    canvas and the canvas falls back to black."""
    from feetbrowser.layout import resolve_color
    eq(resolve_color("light-dark(#ffffff, #18191b)"), "#ffffff")
    eq(resolve_color("light-dark(rgb(255, 0, 0), #000)"), "#ff0000",
       "nested commas do not split the arguments")
    eq(resolve_color("light-dark(transparent, black)"), None)


def test_unreadable_colours_paint_nothing_rather_than_black():
    from feetbrowser.layout import resolve_color
    eq(resolve_color("color-mix(in srgb, red, blue)"), None, "unknown function")
    eq(resolve_color("initial #2d3034"), None, "two tokens is not a colour")
    eq(resolve_color("#12345"), None, "malformed hex")
    eq(resolve_color("rebeccapurple"), "rebeccapurple", "real names survive")
    eq(resolve_color("gray50"), "gray50", "and so do the ones with digits")


def test_media_query_answers_the_preference_features():
    from feetbrowser.cssparser import media_matches
    assert media_matches("(prefers-color-scheme: light)", 800, 600)
    assert not media_matches("(prefers-color-scheme: dark)", 800, 600), \
        "this browser has a light chrome and should say so"
    assert media_matches("(min-width: 400px) and (prefers-color-scheme: light)",
                         800, 600)
    assert not media_matches("(prefers-reduced-motion: reduce)", 800, 600)
    assert media_matches("(orientation: landscape)", 800, 600)
    assert not media_matches("(orientation: portrait)", 800, 600)
    assert media_matches("(min-resolution: 2dppx)", 800, 600), \
        "features we cannot answer still match, so no rule is lost"


def test_at_layer_rules_are_not_thrown_away():
    """A modern stylesheet puts everything inside @layer. Skipping the block
    left such a page with nothing but the UA sheet."""
    rules = CSSParser(
        "@layer base, components;"
        "@layer base { p { color: red; } }"
        "@layer components { .card { color: blue; } }"
        "@supports (display: grid) { div { color: green; } }"
        "@keyframes spin { from { color: pink; } }").parse()
    colors = sorted(body.get("color") for _sel, body in rules)
    eq(colors, ["blue", "green", "red"],
       "layers and @supports come through, keyframes do not")


def test_skipped_at_rule_blocks_end_where_they_should():
    """Finding the `}` that closes a block is a brace count, not a search for
    the next one, and the count has to survive braces that are not structure.
    The scan hops between braces with `find` rather than reading every
    character, so these are the cases where hopping could land wrong."""
    def colors(css):
        return sorted(b["color"] for _s, b in CSSParser(css).parse()
                      if "color" in b)

    eq(colors("@keyframes spin { from { color: pink } } p { color: red }"),
       ["red"], "a nested block does not end the outer one early")
    eq(colors("@font-face { src: url(a) } p { color: red }"),
       ["red"], "a flat skipped block ends at its own brace")
    eq(colors("@keyframes k { a { content: '}' } } p { color: red }"),
       ["red"], "a brace inside a string still counts, as it always did")
    eq(colors("@keyframes k { from { color: pink }"),
       [], "an unterminated block swallows the rest rather than raising")
    eq(colors("@keyframes k {} p { color: red }"),
       ["red"], "an empty block is not an unterminated one")
    eq(colors("@layer a { @layer b { p { color: red } } } q { color: blue }"),
       ["blue", "red"], "layers nest and both halves survive")
    eq(colors("@keyframes k { a { b: c } } } } p { color: red }"),
       ["red"], "stray closers do not lose the rules after them")


def test_viewport_and_font_relative_units():
    from feetbrowser.layout import parse_px
    from feetbrowser.cssparser import set_viewport, get_viewport
    before = get_viewport()
    try:
        set_viewport(1000, 800)
        eq(parse_px("60vw"), 600.0)
        eq(parse_px("15vh"), 120.0)
        eq(parse_px("10vmin"), 80.0)
        eq(parse_px("10vmax"), 100.0)
    finally:
        set_viewport(*before)
    eq(parse_px("2em"), 32.0, "em falls back to the root size")
    eq(parse_px("12pt"), 16.0, "points are 4/3 of a pixel")
    eq(parse_px("nonsense", 7.0), 7.0, "and junk still takes the default")


def test_overflow_hidden_clips_what_leaves_the_box():
    from feetbrowser.layout import DocumentLayout, DrawRect, paint_tree
    html = ('<div style="overflow:hidden;height:20px;width:200px">'
            '<div style="background:#ff0000;height:400px"></div></div>')
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    paint_tree(doc, cmds)
    fill = [c for c in cmds if isinstance(c, DrawRect) and c.color == "#ff0000"]
    eq(len(fill), 1, "the overflowing block still paints")
    eq(fill[0].bottom - fill[0].top, 20, "but only as far as the box goes")


def test_screen_reader_only_text_does_not_show():
    """The visually-hidden recipe -- a 1px box with the content clipped away
    -- is on nearly every accessible site, and its skip links were piling up
    at the top of every page."""
    from feetbrowser.layout import DocumentLayout, DrawText, paint_tree
    html = ('<p>visible</p>'
            '<span style="clip-path:inset(50%);width:1px;height:1px;'
            'overflow:hidden;display:inline-block">skip to content</span>')
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    paint_tree(doc, cmds)
    words = {c.text for c in cmds if isinstance(c, DrawText)}
    assert "visible" in words, words
    assert "skip" not in words and "content" not in words, words


def test_the_older_clip_property_hides_it_too():
    """`clip: rect(1px,1px,1px,1px)` is the spelling `clip-path` replaced,
    and half the sites using one still ship the other alongside it."""
    from feetbrowser.layout import DocumentLayout, DrawText, paint_tree
    html = ('<p>visible</p>'
            '<span style="clip:rect(1px,1px,1px,1px);width:1px;height:1px;'
            'overflow:hidden;display:inline-block">skip to content</span>')
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    paint_tree(doc, cmds)
    words = {c.text for c in cmds if isinstance(c, DrawText)}
    assert "visible" in words, words
    assert "skip" not in words, words


def _painted(html, css="", ua=False):
    """Every word the page actually draws."""
    from feetbrowser.layout import DrawText
    return {c.text for c in _paint_all(html, css, ua=ua)
            if isinstance(c, DrawText)}


def _reds(html, css):
    """Which elements a `color: red` rule reached, by id."""
    dom = HTMLParser(html).parse()
    style(dom, CSSParser(css).parse())
    found, stack = [], [dom]
    while stack:
        node = stack.pop()
        if hasattr(node, "tag"):
            # By id, because colour inherits and the children of a matched
            # element are red too without the rule ever naming them.
            if node.style.get("color") == "red" and node.attributes.get("id"):
                found.append(node.attributes["id"])
            stack.extend(reversed(node.children))
    return sorted(found)


def test_child_combinator_stops_at_the_first_generation():
    """`>` is not a fancier space. A menu that styles `.menu > li` means the
    top level only; reading it as a descendant reaches every submenu too."""
    html = '<div><p id=own>a</p><section><p id=deep>b</p></section></div>'
    eq(_reds(html, "div > p { color: red }"), ["own"], "spaced")
    eq(_reds(html, "div>p { color: red }"), ["own"], "minified, no spaces")
    eq(_reds(html, "div p { color: red }"), ["deep", "own"], "descendant")


def test_sibling_combinators_look_backwards():
    html = ('<div><h1 id=h>t</h1><p id=a>a</p><span id=s>s</span>'
            '<p id=b>b</p></div>')
    eq(_reds(html, "h1 + p { color: red }"), ["a"], "adjacent only")
    eq(_reds(html, "h1 ~ p { color: red }"), ["a", "b"], "any later sibling")
    eq(_reds(html, "ul > li + li { color: red }"),
       [], "no list here to match")
    eq(_reds('<ul><li id=x>1</li><li id=y>2</li><li id=z>3</li></ul>',
             "ul>li+li{color:red}"), ["y", "z"], "all but the first")


def test_has_still_takes_a_relative_selector():
    """`:has(> img)` leads with a combinator, which is legal only in there."""
    eq(_reds('<p id=with><img></p><p id=without>x</p>',
             "p:has(> img) { color: red }"), ["with"])


def test_a_container_query_stays_off():
    """A container query is written to be off most of the time -- it is the
    wide-column variant, not the rule. Flattening it made the variant
    unconditional, and being later in the sheet it won."""
    html = '<div><p id=p>x</p></div>'
    css = ("p { color: green }"
           "@container (min-width: 900px) { p { color: red } }")
    eq(_reds(html, css), [], "the wide variant did not apply")
    dom = HTMLParser(html).parse()
    style(dom, CSSParser(css).parse())
    p = next(n for n in tree_to_list(dom, [])
             if isinstance(n, Element) and n.tag == "p")
    eq(p.style["color"], "green",
       "and the unconditional rule still holds")
    # @supports and @layer are still flattened: a page that wraps its whole
    # stylesheet in a layer has to keep working.
    eq(_reds(html, "@layer base { p { color: red } }"), ["p"], "@layer")
    eq(_reds(html, "@supports (display: grid) { p { color: red } }"),
       ["p"], "@supports")


def test_a_rectangle_has_a_border_unless_told_otherwise():
    """Tk draws create_rectangle with a black 1px outline by default, and
    plugin code written against Tk relies on it. Declining is explicit."""
    from feetbrowser import canvas as canvasmod
    def corner(**opts):
        c = canvasmod.Canvas(None, width=8, height=8, bg="white")
        c.create_rectangle(1, 1, 7, 7, fill="white", **opts)
        surface = c.render()
        i = 1 * surface.stride + 1 * 3
        return tuple(surface.pixels[i:i + 3])
    eq(corner(), (0, 0, 0), "the default border is black")
    eq(corner(outline=""), (255, 255, 255), 'outline="" declines it')
    eq(corner(width=0), (255, 255, 255), "width=0 declines it")
    eq(corner(outline="#ff0000"), (255, 0, 0), "and a colour is honoured")


def test_a_closed_details_shows_only_its_summary():
    """Dropdowns and "read more" panels are <details>; without the rule that
    hides a closed one, every page spills them into the text."""
    html = ('<details><summary>Caches</summary><a>Archive.org</a></details>'
            '<details open><summary>Open</summary><a>Ghostarchive</a></details>')
    words = _painted(html, ua=True)
    assert "Archive.org" not in words, words
    assert {"Caches", "Open", "Ghostarchive"} <= words, words


def test_an_inline_block_keeps_its_blocks_to_itself():
    """A byline is one line with a dropdown in the middle of it. The dropdown
    is an inline-block full of blocks, and letting those blocks reach out
    turns the whole byline into a word per line."""
    from feetbrowser.layout import DrawText
    html = ('<div>via <a href="#">seb</a> | '
            '<span class=drop><div>one</div><div>two</div></span>'
            ' | <a href="#">19 comments</a></div>')
    cmds = [c for c in _paint_all(html, ".drop { display: inline-block }",
                                 ua=True) if isinstance(c, DrawText)]
    tops = {c.text: c.top for c in cmds}
    eq(tops["via"], tops["seb"], "the byline is one line")
    eq(tops["via"], tops["comments"], "still one line past the dropdown")
    assert tops["one"] < tops["two"], "and the blocks inside it stack"
    assert tops["one"] < tops["via"], "sitting on the line, not below it"


def test_hidden_attribute_hides_the_element():
    from feetbrowser.layout import DrawText
    cmds = _paint_all('<p>shown</p><p hidden>gone</p>', ua=True)
    words = {c.text for c in cmds if isinstance(c, DrawText)}
    assert "shown" in words and "gone" not in words, words


def test_link_underline_runs_under_its_spaces():
    """One line under the whole link, not one per word with the spaces
    showing through."""
    from feetbrowser.layout import DrawLine, DrawText
    cmds = _paint_all('<p><a href="#">Jump to content</a></p>', ua=True)
    lines = [c for c in cmds if isinstance(c, DrawLine)]
    eq(len(lines), 1, "one unbroken rule")
    words = [c for c in cmds if isinstance(c, DrawText)]
    eq(lines[0].left, min(w.left for w in words), "starts at the first word")
    eq(lines[0].right, max(w.right for w in words), "ends at the last")


def test_adjacent_links_do_not_share_an_underline():
    """The gap between two separate links stays clear."""
    from feetbrowser.layout import DrawLine
    cmds = _paint_all('<p><a href="/a">one</a> <a href="/b">two</a></p>', ua=True)
    lines = sorted((c.left, c.right)
                   for c in cmds if isinstance(c, DrawLine))
    eq(len(lines), 2, "one rule per link")
    assert lines[0][1] < lines[1][0], "and a gap between them"


def test_underline_restarts_on_the_next_line():
    from feetbrowser.layout import DrawLine
    cmds = _paint_all(
        '<p><a href="#">wrapping link text here</a></p>', width=90, ua=True)
    tops = {c.top for c in cmds if isinstance(c, DrawLine)}
    assert len(tops) > 1, f"a wrapped link is ruled once per line: {tops}"


def test_table_cell_padding_comes_from_css():
    """Cells used to be pinned to a hardcoded 4px inset. Now the padding is
    theirs, so a sheet can widen it and the column widens to match."""
    from feetbrowser.layout import DrawText
    tight = _paint_all('<table><tr><td>Cell</td><td>Two</td></tr></table>',
                       ua=True)
    roomy = _paint_all('<table><tr><td>Cell</td><td>Two</td></tr></table>',
                       css="td { padding: 20px; }", ua=True)
    def second(cmds):
        return [c for c in cmds if isinstance(c, DrawText) and c.text == "Two"][0]
    assert second(roomy).left > second(tight).left + 20, \
        "the wider padding pushes the second column right"


def _start_server(handler, **kw):
    """Serve `handler` on an ephemeral port in a background thread."""
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_reload_bypasses_cache():
    # The response cache serves cached bodies for max-age'd pages; a reload
    # must bypass it and actually re-fetch.
    from feetbrowser.net import URL, _CACHE
    hits = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            body = f"<h1>hit {hits['n']}</h1>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "max-age=9999")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    try:
        u = URL(f"http://127.0.0.1:{srv.server_address[1]}/page")
        _h, first, _c = u.request()
        eq(hits["n"], 1, "first fetch")
        _h, second, _c = u.request()
        eq(hits["n"], 1, "second fetch served from cache")
        eq(first, second)
        _h, third, _c = u.request(refresh=True)
        eq(hits["n"], 2, "refresh re-fetches")
        assert third != first or "hit 2" in third
        _CACHE.clear()
    finally:
        srv.shutdown()


def _tiny_png(w=2, h=2):
    """A minimal valid PNG (no Pillow dependency), for image tests."""
    import struct
    import zlib as _z

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", _z.compress(rows))
    raw += chunk(b"IEND", b"")
    return raw


def test_bytes_fetch_is_not_served_from_the_text_cache():
    # The document that *is* an image is fetched as text (for its content
    # type) and then re-fetched as bytes for the <img> that shows it. The
    # cache must keep the two apart: serving the text-decoded entry to the
    # bytes caller hands the decoder mangled bytes.
    from feetbrowser.net import URL, _CACHE
    png = _tiny_png()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "max-age=9999")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    try:
        u = URL(f"http://127.0.0.1:{srv.server_address[1]}/pic.png")
        _h, text, _c = u.request()          # caches the text form
        _h2, data, ctype = u.request_bytes()  # must not get the cached text
        eq(ctype, "image/png")
        assert data[:8] == b"\x89PNG\r\n\x1a\n", \
            f"bytes fetch returned the cached text, got {data[:8]!r}"
        _CACHE.clear()
    finally:
        srv.shutdown()


def test_a_document_that_is_an_image_renders_it():
    # Visiting an image URL directly (e.g. safebooru.org/includes/header.png)
    # should show the image, not a raw-bytes placeholder.
    from feetbrowser.layout import DrawImage
    png = _tiny_png(4, 4)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "max-age=9999")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    try:
        tab = Tab(700)
        tab.load(f"http://127.0.0.1:{srv.server_address[1]}/pic.png")
        tab.load_images()
        assert tab.image_cache, "direct-image document decoded"
        assert any(isinstance(c, DrawImage) for c in tab.display_list), \
            "direct-image document renders a DrawImage"
        assert not any(getattr(c, "text", "").startswith("[img")
                       for c in tab.display_list), \
            "no placeholder for a directly-visited image"
    finally:
        srv.shutdown()


def test_async_load_in_gui_mode():
    # With a window present, http(s) loads happen off the UI thread so the
    # spinner can spin; loading stays True until the body arrives.
    import time
    from feetbrowser.net import URL

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.2)
            body = b"<h1>async</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    root = Tk(); root.withdraw()
    try:
        class FakeBrowser:
            window = root
            toe_contexts = []

            def draw(self):
                pass

        tab = Tab(700, FakeBrowser())
        url = URL(f"http://127.0.0.1:{srv.server_address[1]}/slow")
        tab.load(url)
        assert tab.loading is True, "GUI-mode http load should be async"
        deadline = time.time() + 5
        while tab.loading and time.time() < deadline:
            tab._drain_async_load()
            time.sleep(0.02)
        assert not tab.loading, "async load should complete"
        assert str(tab.url) == str(url)
        texts = "".join(getattr(c, "text", "") for c in tab.display_list)
        assert "async" in texts, f"page body rendered, got: {texts!r}"
    finally:
        srv.shutdown()
        root.destroy()


# -- the Win32 backend, in the parts that are not Win32 --------------------
#
# tests/test_win32.py opens real windows and only runs on Windows. Everything
# below is the arithmetic and the translation tables behind that window, and
# those are plain functions on purpose: they run, and are checked, on every
# platform the suite runs on.

def test_dib_stride_rounds_rows_up_to_four_bytes():
    from feetbrowser.win32 import dib_stride
    eq(dib_stride(4, 24), 12, "a multiple of four needs no padding")
    eq(dib_stride(5, 24), 16, "15 bytes of pixels round up to 16")
    eq(dib_stride(999, 24), 3000, "2997 bytes of pixels round up to 3000")
    # The reason this backend presents 32bpp: no row ever needs padding, so
    # the frame is one buffer and the whole class of off-by-one row smears
    # cannot happen.
    for width in range(1, 40):
        eq(dib_stride(width, 32), width * 4,
           f"32bpp width {width} should need no padding")


def test_rgb_becomes_bgr_without_moving_a_pixel():
    from feetbrowser.win32 import bgra_from_rgb
    # Two pixels: pure red, then pure green.
    out = bgra_from_rgb(bytearray([255, 0, 0, 0, 255, 0]), 2, 1)
    eq(bytes(out), b"\x00\x00\xff\x00\x00\xff\x00\x00", "channel order")
    eq(len(out), 2 * 1 * 4, "four bytes per pixel")


def test_the_dib_is_top_down_and_row_order_survives():
    """A DIB is bottom-up by default and we declare a negative height
    instead, so the rows must come out in the order they went in. Getting
    this wrong flips the whole page upside down."""
    from feetbrowser.win32 import bgra_from_rgb
    pixels = bytearray([1, 2, 3, 4, 5, 6,        # row 0
                        7, 8, 9, 10, 11, 12])    # row 1
    out = bgra_from_rgb(pixels, 2, 2)
    eq(bytes(out[0:4]), b"\x03\x02\x01\x00", "first pixel of the first row")
    eq(bytes(out[8:12]), b"\x09\x08\x07\x00", "first pixel of the second row")


def test_a_padded_source_stride_is_compacted():
    from feetbrowser.win32 import bgra_from_rgb
    # Three bytes of pixels per row plus two bytes of slack.
    pixels = bytearray([10, 20, 30, 99, 99,
                        40, 50, 60, 99, 99])
    out = bgra_from_rgb(pixels, 1, 2, stride=5)
    eq(bytes(out), b"\x1e\x14\x0a\x00\x3c\x32\x28\x00", "slack was skipped")


def test_the_bitmap_header_is_the_size_windows_expects():
    """GDI reads biSize to tell a BITMAPINFOHEADER from its successors, so a
    header that is not 40 bytes is rejected outright."""
    import ctypes
    from feetbrowser.win32 import BITMAPINFOHEADER
    eq(ctypes.sizeof(BITMAPINFOHEADER), 40)


def test_packed_coordinates_can_be_negative():
    """A drag that leaves the window on the left or the top reports a
    negative coordinate, packed as an unsigned 16-bit field."""
    from feetbrowser.win32 import lparam_point, signed_word
    eq(signed_word(0xFFFF), -1)
    eq(signed_word(0x8000), -32768)
    eq(signed_word(0x7FFF), 32767)
    eq(lparam_point((300 << 16) | 120), (120, 300))
    eq(lparam_point((0xFFFB << 16) | 0xFFF6), (-10, -5), "off the top-left")


def test_a_wheel_notch_stays_in_the_pixel_range():
    """browser.py treats |delta| < 30 as a pixel count and anything larger as
    line units, so a notch has to stay under 30 or one flick moves the page
    by a screenful."""
    from feetbrowser.win32 import wheel_delta
    eq(wheel_delta(120), 20, "one notch forward")
    eq(wheel_delta(-120), -20, "one notch back")
    eq(wheel_delta(0), 0)
    for raw in (120, -120, 360, -360, 3600, -3600, 7, -7):
        delta = wheel_delta(raw)
        assert abs(delta) < 30, f"{raw} became {delta}, out of the pixel range"
        assert (delta > 0) == (raw > 0), f"{raw} lost its direction"


def test_modifier_bits_are_the_ones_the_browser_reads():
    from feetbrowser.win32 import modifier_state
    from feetbrowser.window import STATE_ALT, STATE_CONTROL, STATE_SHIFT
    eq(modifier_state(False, False, False), 0)
    eq(modifier_state(True, False, False), STATE_SHIFT)
    eq(modifier_state(False, True, False), STATE_CONTROL)
    eq(modifier_state(False, False, True), STATE_ALT)
    # browser.py tests `event.state & 0x4` directly for its shortcuts.
    assert modifier_state(False, True, False) & 0x4


def test_named_virtual_keys_map_to_tk_keysyms():
    from feetbrowser.win32 import keysym_for_vk
    from feetbrowser.window import STATE_CONTROL, STATE_SHIFT
    eq(keysym_for_vk(0x0D, 0), "Return")
    eq(keysym_for_vk(0x26, 0), "Up")
    eq(keysym_for_vk(0x21, 0), "Prior", "PageUp is Tk's Prior")
    eq(keysym_for_vk(0x7B, 0), "F12")
    eq(keysym_for_vk(0x09, 0), "Tab")
    # browser.py binds <Control-ISO_Left_Tab> for previous-tab, which is the
    # keysym X11 and Tk use for a shifted Tab.
    eq(keysym_for_vk(0x09, STATE_SHIFT), "ISO_Left_Tab")
    eq(keysym_for_vk(0x09, STATE_SHIFT | STATE_CONTROL), "ISO_Left_Tab")


def test_a_plain_letter_waits_for_the_character_message():
    """WM_CHAR is the only thing that has been through the user's keyboard
    layout, so an unmodified printable key is left to it."""
    from feetbrowser.win32 import keysym_for_vk
    from feetbrowser.window import STATE_ALT, STATE_CONTROL, STATE_SHIFT
    eq(keysym_for_vk(0x4C, 0), None, "plain L")
    eq(keysym_for_vk(0x4C, STATE_SHIFT), None, "shifted L")
    # Under Control the character message carries a control code (Ctrl-L is
    # 0x0C, not "l"), so the letter has to come from the virtual key.
    eq(keysym_for_vk(0x4C, STATE_CONTROL), "l", "Ctrl-L reaches <Control-l>")
    eq(keysym_for_vk(0x54, STATE_CONTROL), "t")
    eq(keysym_for_vk(0x53, STATE_CONTROL | STATE_SHIFT), "S",
       "Tk names a shifted letter by its shifted character")
    eq(keysym_for_vk(0x31, STATE_CONTROL), "1", "digits are not cased")
    eq(keysym_for_vk(0x25, STATE_ALT), "Left", "Alt-Left is still a named key")
    eq(keysym_for_vk(0xBA, STATE_CONTROL), None, "no guess at an OEM key")


def test_character_messages_become_keysyms():
    from feetbrowser.win32 import keysym_for_char
    from feetbrowser.window import STATE_CONTROL
    eq(keysym_for_char("z", 0), ("z", "z"))
    eq(keysym_for_char("é", 0), ("é", "é"), "the layout's own character")
    eq(keysym_for_char(" ", 0), ("space", " "), "Tk calls a space 'space'")
    eq(keysym_for_char("", 0), None, "half a surrogate pair carries nothing")
    # Return, Tab, Escape and Backspace each arrive twice: once as a named
    # virtual key and once as a control code. Only the first is the event.
    eq(keysym_for_char("\r", 0), None)
    eq(keysym_for_char("\x08", 0), None)
    eq(keysym_for_char("\x0c", STATE_CONTROL), None,
       "Ctrl-L was already delivered from the virtual key")


def test_key_sequences_are_offered_most_specific_first():
    """Tk fires exactly one binding, the most specific that matches, and a
    binding matches when its modifiers are a subset of those held."""
    from feetbrowser.window import STATE_CONTROL, STATE_SHIFT, key_sequences
    names = key_sequences("l", STATE_CONTROL)
    eq(names[0], "<Control-l>")
    eq(names[-1], "<Key>", "the generic binding is always the last resort")
    assert "<l>" in names
    names = key_sequences("Up", 0)
    eq(names, ["<Up>", "<Key>"], "an unmodified named key")
    # browser.py binds <Control-Shift-s> for view-source and <Control-s> for
    # nothing, so the shifted spelling has to be offered and has to win.
    names = key_sequences("S", STATE_CONTROL | STATE_SHIFT)
    assert "<Control-Shift-s>" in names, names
    assert names.index("<Control-Shift-s>") < names.index("<Control-s>"), names
    # A subset match is what lets <Control-ISO_Left_Tab> catch Ctrl-Shift-Tab.
    names = key_sequences("ISO_Left_Tab", STATE_CONTROL | STATE_SHIFT)
    assert "<Control-ISO_Left_Tab>" in names, names


def test_win32_module_is_importable_off_windows():
    """gui.platform_root(), pyflakes and this file all import it, so it has
    to load on a machine with no windll at all."""
    from feetbrowser import win32
    if sys.platform != "win32":
        eq(win32.available(), False, "no Win32 window off Windows")
        try:
            win32.Win32Tk()
        except win32.Win32Unavailable:
            pass
        else:
            assert False, "a window opened on a platform with no Win32"


def test_the_display_variable_picks_a_backend_by_name():
    from feetbrowser import gui
    saved = gui.DISPLAY
    try:
        gui.DISPLAY = "none"
        eq(gui.platform_root(), None, "'none' stays headless everywhere")

        gui.DISPLAY = ""
        root = gui.platform_root()
        if sys.platform == "darwin":
            eq(root.__name__, "CocoaTk")
        elif sys.platform == "win32":
            eq(root.__name__, "Win32Tk")
        elif root is not None:
            # x11.py answers here too, and whether it can is a property of
            # the machine rather than of the platform: a desktop with a
            # server running gets X11Tk and headless CI gets None. Both are
            # right, so the only wrong answer is some *other* backend.
            eq(root.__name__, "X11Tk")

        for name in ("win32", "windows"):
            gui.DISPLAY = name
            if sys.platform == "win32":
                eq(gui.platform_root().__name__, "Win32Tk")
            else:
                # Asking by name and silently getting a headless root is the
                # kind of thing you discover from an empty screenshot.
                try:
                    gui.platform_root()
                except RuntimeError as e:
                    assert "Win32" in str(e), f"unhelpful message: {e}"
                else:
                    assert False, f"FEETBROWSER_DISPLAY={name} should raise"

        gui.DISPLAY = "cocoa"
        if sys.platform == "darwin":
            eq(gui.platform_root().__name__, "CocoaTk")
        else:
            try:
                gui.platform_root()
            except RuntimeError as e:
                assert "Cocoa" in str(e), f"unhelpful message: {e}"
            else:
                assert False, "FEETBROWSER_DISPLAY=cocoa should raise here"
    finally:
        gui.DISPLAY = saved


def test_file_urls_understand_a_drive_letter():
    """Windows paths do not fit the file: grammar: the drive lands where the
    host goes, or behind an extra slash, and Explorer hands out backslashes.
    All three have to name the same file."""
    for raw in ("file:///C:/pages/a.html", "file://C:/pages/a.html",
                "file://C:\\pages\\a.html", "file:///C|/pages/a.html"):
        u = URL(raw)
        eq(u.scheme, "file", raw)
        eq(u.path, "/C:/pages/a.html" if "|" not in raw
           else "/C|/pages/a.html", raw)
        # Whatever went in, one canonical spelling comes out, and it parses
        # back to the same place.
        eq(URL(str(u)).path, u.path, f"{raw} does not round-trip")
    eq(str(URL("file://C:/pages/a.html")), "file:///C:/pages/a.html")
    # POSIX paths are untouched.
    eq(URL("file:///etc/hosts").path, "/etc/hosts")
    eq(URL("file:///tmp/a b.html").path, "/tmp/a b.html")
    eq(URL("file://localhost/etc/hosts").path, "/etc/hosts", "host ignored")
    eq(URL("file:/etc/hosts").path, "/etc/hosts", "the one-slash form")


def test_a_file_url_converts_back_to_a_filesystem_path():
    """The URL path and the filesystem path are different strings: the
    leading slash in file:///C:/x is not part of the path, and the separator
    is whatever this platform uses."""
    sep = os.sep
    eq(URL("file:///C:/pages/a.html").local_path(),
       sep.join(["C:", "pages", "a.html"]))
    eq(URL("file://C:\\pages\\a.html").local_path(),
       sep.join(["C:", "pages", "a.html"]))
    eq(URL("file:///C|/x.html").local_path(), sep.join(["C:", "x.html"]),
       "the older bar spelling is still a drive")
    eq(URL("file:///etc/hosts").local_path(), sep.join(["", "etc", "hosts"]))
    # Percent-escapes come off, which is what makes the links a directory
    # listing writes openable again.
    eq(URL("file:///tmp/a%20b.html").local_path(),
       sep.join(["", "tmp", "a b.html"]))


def test_relative_links_resolve_inside_a_drive():
    base = URL("file:///C:/pages/index.html")
    eq(str(base.resolve("next.html")), "file:///C:/pages/next.html")
    eq(str(base.resolve("../other/x.html")), "file:///C:/other/x.html")
    eq(str(base.resolve("/C:/top.html")), "file:///C:/top.html")


def test_local_pages_may_only_reach_their_own_directory():
    """The same-origin rule for file: pages is a string prefix over URL
    paths. Comparing them with os.path.dirname would compare a backslash
    prefix against a slash path on Windows and deny everything."""
    tab = Tab(700, None)
    tab.base_url = URL("file:///C:/pages/index.html")
    assert tab._js_scheme_allowed(URL("file:///C:/pages/data.json"))
    assert tab._js_scheme_allowed(URL("file:///C:/pages/sub/deep.json"))
    assert not tab._js_scheme_allowed(URL("file:///C:/secrets.txt"))
    assert not tab._js_scheme_allowed(URL("file:///C:/pagesother/x.txt"))
    tab.base_url = URL("file:///home/u/pages/index.html")
    assert tab._js_scheme_allowed(URL("file:///home/u/pages/data.json"))
    assert not tab._js_scheme_allowed(URL("file:///home/u/secret.txt"))


def test_a_local_file_can_actually_be_opened():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write("<h1>local</h1>")
        # What a browser is handed is a path, and turning one into a URL is
        # exactly where a Windows path stops looking like a URL.
        url = URL("file:///" + path.replace(os.sep, "/").lstrip("/"))
        _headers, body, ctype = url.request()
        eq(ctype, "text/html")
        assert "local" in body, f"read back {body!r}"
        eq(URL(str(url)).local_path(), path, "round-trips through str()")
    finally:
        os.unlink(path)


def test_a_missing_file_reports_rather_than_raises():
    _headers, body, ctype = URL("file:///no/such/file.html").request()
    eq(ctype, "text/html")
    assert "Cannot open file" in body, body
def _lis(dom):
    return [n for n in tree_to_list(dom, [])
            if isinstance(n, Element) and n.tag == "li"]


def test_nth_child_with_a_zero_step_is_one_child():
    # `0n+3` used to divide by the step and take the whole page down with a
    # ZeroDivisionError raised from the middle of the cascade. It selects the
    # third child and nothing else.
    rules = CSSParser("li:nth-child(0n+3) { color: red }").parse()
    dom = HTMLParser("<ul><li>a</li><li>b</li><li>c</li><li>d</li></ul>").parse()
    style(dom, rules)
    colors = [li.style["color"] for li in _lis(dom)]
    eq(colors, ["black", "black", "red", "black"], "0n+3 is the third child")


def test_nth_child_expression_forms():
    rules = CSSParser(
        "li:nth-child(odd) { color: red } "
        "li:nth-child(even) { font-weight: bold } "
        "li:nth-child(-n+2) { font-style: italic } "
        "li:nth-child(3) { white-space: pre } "
        "li:nth-child(  4  ) { text-align: right } "
        "li:nth-child(banana) { font-family: serif } "
        "li:nth-last-child(1) { list-style-type: square }"
    ).parse()
    dom = HTMLParser(
        "<ul><li>a</li><li>b</li><li>c</li><li>d</li><li>e</li></ul>").parse()
    style(dom, rules)
    lis = _lis(dom)
    eq([li.style["color"] for li in lis],
       ["red", "black", "red", "black", "red"], "odd is 1, 3, 5")
    eq([li.style["font-weight"] for li in lis],
       ["normal", "bold", "normal", "bold", "normal"], "even is 2 and 4")
    # `-n+2` is the first two children: n counts from zero, so the terms are
    # 2 and 1. This used to be the first child alone, because the step count
    # was required to be at least 1 and that drops the n=0 term of every an+b
    # expression -- `2n+1` skipped the first child instead of selecting it and
    # `n+3` started at the fourth. `odd` and `even` are spelled out separately
    # and were always right, which is most of what pages use and is why it
    # survived. Selectors 4 6.6.5 is explicit that n is non-negative.
    eq([li.style["font-style"] for li in lis][:3],
       ["italic", "italic", "normal"], "-n+2 is the first two children")
    eq(lis[2].style["white-space"], "pre", "a bare number is that child")
    eq(lis[3].style["text-align"], "right", "spaces around the number are fine")
    eq([li.style["font-family"] for li in lis].count("serif"), 0,
       "an unparseable expression matches nothing rather than raising")
    eq([li.style["list-style-type"] for li in lis],
       ["disc", "disc", "disc", "disc", "square"],
       "nth-last-child counts from the end")


def test_of_type_pseudo_classes_count_by_tag():
    rules = CSSParser(
        "p:first-of-type { color: red } p:last-of-type { font-weight: bold } "
        "span:only-of-type { font-style: italic } "
        "p:nth-of-type(2) { white-space: pre } "
        "p:nth-last-of-type(1) { text-align: right }"
    ).parse()
    dom = HTMLParser(
        "<div><p>a</p><span>s</span><p>b</p><p>c</p></div>").parse()
    style(dom, rules)
    ps = [n for n in tree_to_list(dom, [])
          if isinstance(n, Element) and n.tag == "p"]
    span = [n for n in tree_to_list(dom, [])
            if isinstance(n, Element) and n.tag == "span"][0]
    eq(ps[0].style["color"], "red", "the span between does not shift the count")
    eq([p.style["font-weight"] for p in ps],
       ["normal", "normal", "bold"], "last-of-type is the third p")
    eq(span.style["font-style"], "italic", "the only span is only-of-type")
    eq(ps[1].style["white-space"], "pre", "nth-of-type counts p's only")
    eq(ps[2].style["text-align"], "right", "nth-last-of-type counts back")


def test_attribute_operators():
    rules = CSSParser(
        'a[href] { color: red } a[href="/x"] { font-weight: bold } '
        'a[class~="two"] { font-style: italic } a[lang|="en"] { white-space: pre } '
        'a[href^="/x"] { text-align: right } a[href$="z"] { line-height: 3 } '
        'a[href*="y"] { text-decoration: underline }'
    ).parse()
    dom = HTMLParser(
        '<div><a href="/x" class="one two" lang="en-GB">a</a>'
        '<a href="/xyz" lang="ends">b</a><a>c</a></div>').parse()
    style(dom, rules)
    a, b, c = [n for n in tree_to_list(dom, [])
               if isinstance(n, Element) and n.tag == "a"]
    eq(a.style["color"], "red", "presence matches")
    eq(c.style["color"], "black", "presence does not match an absent attribute")
    eq(a.style["font-weight"], "bold", "= is exact")
    eq(b.style["font-weight"], "normal", "= is not a prefix")
    eq(a.style["font-style"], "italic", "~= matches a whitespace-separated word")
    eq(a.style["white-space"], "pre", "|= matches the hyphenated prefix")
    eq(b.style["white-space"], "normal", "|= is not a bare prefix")
    eq(b.style["text-align"], "right", "^= is a prefix")
    eq(b.style["line-height"], "3", "$= is a suffix")
    eq(b.style["text-decoration"], "underline", "*= is a substring")


def test_is_and_where_match_their_argument():
    rules = CSSParser(
        "p:is(.x) { color: red } p:where(.y) { font-weight: bold } "
        "p:not(:is(.x)) { font-style: italic }"
    ).parse()
    dom = HTMLParser(
        '<div><p class="x">a</p><p id="keep">b</p><p class="y">c</p>'
        '<p>d</p></div>').parse()
    style(dom, rules)
    ps = [n for n in tree_to_list(dom, [])
          if isinstance(n, Element) and n.tag == "p"]
    eq([p.style["color"] for p in ps], ["red", "black", "black", "black"],
       ":is matches its argument")
    eq([p.style["font-weight"] for p in ps],
       ["normal", "normal", "bold", "normal"], ":where matches its argument")
    eq([p.style["font-style"] for p in ps],
       ["normal", "italic", "italic", "italic"], ":not(:is(...)) inverts it")
    # A comma inside the parentheses is read as the end of the selector by the
    # tokeniser, so the rule is dropped rather than matching either argument.
    eq(len(CSSParser("p:is(.x, #keep) { color: red }").parse()), 0,
       "a selector list inside :is() does not parse")


def test_a_non_string_attribute_does_not_lose_the_page():
    # Script can put anything in the attribute table. Splitting a number into
    # class names used to raise out of the cascade, which cost the whole page
    # rather than the one rule that asked.
    rules = CSSParser(".skip { color: red } #five { font-weight: bold } "
                      "p { font-style: italic }").parse()
    dom = HTMLParser("<div><p>a</p></div>").parse()
    p = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "p"][0]
    p.attributes["class"] = 5
    p.attributes["id"] = 5
    style(dom, rules)
    eq(p.style["color"], "black", "a number has no class names")
    eq(p.style["font-weight"], "normal", "a number is not an id either")
    eq(p.style["font-style"], "italic", "the rest of the cascade still runs")


def test_styling_a_subtree_starts_its_ancestor_sets_empty():
    # Script mutates one node and the tab restyles that subtree. The ancestor
    # feature sets a descendant selector consults are built from the styling
    # root down, so an ancestor above it is invisible to the fast path: a
    # subtree restyle can drop `div.outer p` that a full restyle applies.
    # Faithful to the Python this replaced, and load-bearing -- the corpus
    # renders the same only because it still behaves this way.
    rules = CSSParser("div.outer p { color: red } :root p { font-weight: bold } "
                      "p { font-style: italic }").parse()
    dom = HTMLParser('<div class="outer"><section><p>a</p></section></div>').parse()
    style(dom, rules)
    section = [n for n in tree_to_list(dom, [])
               if isinstance(n, Element) and n.tag == "section"][0]
    p = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "p"][0]
    eq(p.style["color"], "red", "a full restyle sees the outer div")
    style(section, rules)
    eq(p.style["color"], "black", "a subtree restyle starts its ancestors empty")
    eq(p.style["font-weight"], "bold",
       ":root is still the document's root, not the subtree's")
    eq(p.style["font-style"], "italic", "the subtree is restyled at all")


def test_a_selector_nested_past_the_limit_matches_nothing():
    # Absurd nesting compiles to a selector that never matches instead of
    # raising a RecursionError from inside the cascade.
    rules = CSSParser(" ".join(["div"] * 600) + " p { color: red }").parse()
    dom = HTMLParser("<div><p>a</p></div>").parse()
    style(dom, rules)
    p = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "p"][0]
    eq(p.style["color"], "black", "the page survives the selector")


def test_a_deeply_nested_document_styles_without_recursing():
    depth = 400
    rules = CSSParser("div p { color: red }").parse()
    dom = HTMLParser("<div>" * depth + "<p>x</p>" + "</div>" * depth).parse()
    style(dom, rules)
    p = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "p"][0]
    eq(p.style["color"], "red", "a 400-deep document still cascades")


def test_images_over_http_reach_the_display_list():
    """The shape of a real page: HTML off the wire, then images off the wire.

    Both halves are asynchronous and they finish in that order, so a caller
    that waits on `tab.loading` alone stops exactly one step early -- with
    the document rendered and every <img> still a placeholder. This is the
    regression that made photographs vanish from the raster backend.
    """
    import struct
    import time
    import zlib

    def png(width, height, rgb):
        def chunk(tag, payload):
            body = tag + payload
            return (struct.pack(">I", len(payload)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
        raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                             8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))

    pixels = png(6, 6, (0, 128, 255))

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/pic.png":
                body, ctype = pixels, "image/png"
            else:
                body = b"<h1>page</h1><p><img src='/pic.png'>"
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    from feetbrowser.layout import DrawImage

    srv = _start_server(H)
    browser = Browser()
    try:
        port = srv.server_address[1]
        browser.new_tab(f"http://127.0.0.1:{port}/page")
        tab = browser.tabs[0]
        assert tab.loading, "http in GUI mode loads off the UI thread"
        # Pump only until the document lands -- the old settle condition.
        deadline = time.time() + 10
        while tab.loading and time.time() < deadline:
            browser.window.flush_timers()
            time.sleep(0.01)
        assert not tab.loading, "document should have arrived"
        assert tab.pending_images(), "and its image should still be coming"
        assert browser.settle(20.0), "settle should not have timed out"
        eq(len(tab.image_cache), 1, "image decoded and cached")
        images = [c for c in tab.display_list if isinstance(c, DrawImage)]
        eq(len(images), 1, "image painted")
        assert not any("[img" in getattr(c, "text", "")
                       for c in tab.display_list), "placeholder replaced"
    finally:
        srv.shutdown()
        browser.window.destroy()


# -- <select> drop-downs ----------------------------------------------------

_SELECT_PAGE = (
    '<form><select name="fruit">'
    '<option value="a">Apple</option>'
    '<option value="b" selected>Banana</option>'
    '<option value="c" disabled>Cherry</option>'
    '<option value="d">Damson</option>'
    '</select></form>'
)


class _SelectBrowser(Browser):
    """A Browser with the painting taken out.

    Everything the drop-down does -- opening, walking, committing,
    dismissing -- is real Browser code; only the calls that would put pixels
    on a canvas are counted instead of drawn.
    """

    def __init__(self, tab):
        self.active_tab = tab
        self.focus = None
        self._tab_drag = None  # _on_escape asks the tab strip first
        self.select_popup = SelectPopup()
        # Real _scroll() tracks velocity, and this double reaches it. The
        # wheel also arms a momentum coast, so the timer half of a window is
        # here too -- it records requests without ever running one.
        self._scroll_ticks = []
        self._scroll_velocity = 0.0
        self._momentum_job = None
        self.settings = {"show_link_preview": True, "scroll_speed": 80,
                         "momentum": True, "momentum_strength": 100,
                         "search_engine": "duckduckgo"}
        self.paints = 0
        self.canvas = type("C", (), {"winfo_width": lambda s: 1000,
                                     "winfo_height": lambda s: 720})()
        self.window = type("W", (), {
            "after": lambda s, d, f=None, *a: "timer" if f else None,
            "after_cancel": lambda s, h: None})()

    def chrome_height(self):
        return 0

    def _draw_page(self):
        self.paints += 1

    def _draw_chrome(self):
        self.paints += 1

    def _draw_scrollbar(self):
        self.paints += 1

    def _draw_select_popup(self):
        self.paints += 1


def _select_node(tab):
    return next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == "select")


def _select_centre(tab):
    """Middle of the laid-out <select> control, in page coordinates."""
    node = _select_node(tab)
    lx, ty, rx, by = tab._control_rect(node)
    return (lx + rx) / 2, (ty + by) / 2


def _open_dropdown(body=_SELECT_PAGE):
    """Load a page and click its select, returning (browser, tab)."""
    tab = _make_tab(body)
    browser = _SelectBrowser(tab)
    x, y = _select_centre(tab)
    dest = tab.click(x, y)
    assert isinstance(dest, SelectAction), f"click gave {dest!r}, not a select"
    browser._open_select_popup(dest)
    return browser, tab


def _labels(tab):
    return [c.text for c in tab.display_list if isinstance(c, DrawText)]


def test_select_paints_the_selected_options_label():
    tab = _make_tab(_SELECT_PAGE)
    assert "Banana" in _labels(tab), \
        f"`selected` option's label not painted: {_labels(tab)}"
    # With nothing marked, the first option is what a form would submit, so
    # it is what the closed control has to show.
    plain = _make_tab('<select><option>Ant</option><option>Bee</option>'
                      '</select>')
    assert "Ant" in _labels(plain), f"no fallback label: {_labels(plain)}"


def test_clicking_a_select_opens_its_list():
    browser, tab = _open_dropdown()
    popup = browser.select_popup
    assert popup.open_, "the list did not open"
    eq([row.label for row in popup.rows],
       ["Apple", "Banana", "Cherry", "Damson"], "rows")
    eq(popup.rows[popup.hover].label, "Banana",
       "the highlight starts on the selected option")
    assert popup.y >= 0 and popup.width > 0, "the list has a place to be"


def test_a_disabled_select_does_not_open():
    tab = _make_tab('<select disabled><option>Nope</option></select>')
    x, y = _select_centre(tab)
    eq(tab.click(x, y), None, "a disabled select must not drop down")


def test_arrow_keys_walk_the_list_and_skip_disabled_options():
    browser, _tab = _open_dropdown()
    popup = browser.select_popup
    Browser._on_down(browser, None)
    # Cherry is disabled, so Down from Banana lands past it.
    eq(popup.rows[popup.hover].label, "Damson", "down skips disabled options")
    Browser._on_up(browser, None)
    eq(popup.rows[popup.hover].label, "Banana", "up comes back")
    Browser._on_up(browser, None)
    eq(popup.rows[popup.hover].label, "Apple", "up again")
    Browser._on_up(browser, None)
    eq(popup.rows[popup.hover].label, "Damson", "the ends wrap around")
    Browser._on_home(browser, None)
    eq(popup.rows[popup.hover].label, "Apple", "Home goes to the top")
    Browser._on_end(browser, None)
    eq(popup.rows[popup.hover].label, "Damson", "End goes to the bottom")


def test_enter_commits_the_highlighted_option():
    browser, tab = _open_dropdown()
    Browser._on_down(browser, None)          # Banana -> Damson
    Browser._on_enter(browser, None)
    assert not browser.select_popup.open_, "committing closes the list"
    chosen = selected_options(_select_node(tab))
    eq([option_value(o) for o in chosen], ["d"], "the DOM moved `selected`")
    eq(_select_node(tab).attributes.get("value"), "d", "select.value follows")
    assert "Damson" in _labels(tab), \
        f"the painted label did not follow: {_labels(tab)}"


def test_escape_dismisses_the_list_without_changing_the_value():
    browser, tab = _open_dropdown()
    Browser._on_down(browser, None)          # highlight moves...
    Browser._on_escape(browser, None)        # ...but is never taken
    assert not browser.select_popup.open_, "Escape closes the list"
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["b"],
       "Escape leaves the value alone")
    assert _select_node(tab).attributes.get("data-focused") is None, \
        "dismissing the list also drops the focus ring"


def test_clicking_an_option_selects_it_and_clicking_away_does_not():
    browser, tab = _open_dropdown()
    popup = browser.select_popup
    row_y = popup.y + popup.PAD + popup.ROW_H // 2  # first row: Apple
    browser._select_popup_click(popup.x + 10, row_y)
    assert not popup.open_, "choosing closes the list"
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["a"],
       "clicking a row selects it")

    # Now open it again and click somewhere else entirely.
    browser, tab = _open_dropdown()
    popup = browser.select_popup
    browser._select_popup_click(popup.x + popup.width + 40, popup.y + 4)
    assert not popup.open_, "clicking away dismisses the list"
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["b"],
       "clicking away leaves the value alone")


def test_a_disabled_option_cannot_be_clicked():
    browser, tab = _open_dropdown()
    popup = browser.select_popup
    cherry = next(i for i, r in enumerate(popup.rows) if r.label == "Cherry")
    y = popup.y + popup.PAD + (cherry + 0.5) * popup.ROW_H
    browser._select_popup_click(popup.x + 10, y)
    assert popup.open_, "a disabled option swallows the click, list stays up"
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["b"],
       "a disabled option cannot be chosen")


def test_scrolling_the_page_dismisses_the_list():
    browser, tab = _open_dropdown(
        "<p>tall</p>" * 200 + _SELECT_PAGE)
    assert browser.select_popup.open_
    Browser._scroll(browser, 80)
    assert not browser.select_popup.open_, "a scroll takes the list down"
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["b"],
       "and leaves the value alone")


def test_optgroup_labels_are_listed_but_cannot_be_chosen():
    browser, _tab = _open_dropdown(
        '<select>'
        '<optgroup label="Warm"><option>Red</option>'
        '<option>Orange</option></optgroup>'
        '<optgroup label="Cool"><option>Blue</option></optgroup>'
        '</select>')
    popup = browser.select_popup
    eq([r.label for r in popup.rows],
       ["Warm", "Red", "Orange", "Cool", "Blue"], "headings are listed")
    eq([r.label for r in popup.rows if r.enabled], ["Red", "Orange", "Blue"],
       "headings are not selectable")
    eq(popup.rows[popup.hover].label, "Red",
       "the highlight starts on the first real option, not a heading")
    Browser._on_down(browser, None)
    Browser._on_down(browser, None)
    eq(popup.rows[popup.hover].label, "Blue", "walking steps over headings")


def test_a_long_list_scrolls_the_highlight_into_view():
    options = "".join(f"<option>opt{i}</option>" for i in range(200))
    browser, _tab = _open_dropdown(f"<select>{options}</select>")
    popup = browser.select_popup
    assert popup.visible < len(popup.rows), "a 200-row list cannot all fit"
    Browser._on_end(browser, None)
    assert popup.top <= popup.hover < popup.top + popup.visible, \
        "the last option must be scrolled into the window"


def test_a_long_list_stays_inside_the_page_area():
    options = "".join(f"<option>opt{i}</option>" for i in range(200))
    tab = _make_tab(f"<select>{options}</select>")
    browser = _SelectBrowser(tab)
    # The real window has a tab bar and an address bar above the page; a list
    # long enough to reach them has to stop, not bury them.
    browser.chrome_height = lambda: 90
    x, y = _select_centre(tab)
    browser._open_select_popup(tab.click(x, y))
    popup = browser.select_popup
    assert popup.open_, "the list did not open"
    assert popup.y >= 90, f"the list starts at y={popup.y}, over the chrome"
    assert popup.y + popup.height <= 720, \
        f"the list ends at y={popup.y + popup.height}, past the window"


def test_resetting_a_form_puts_the_select_back():
    browser, tab = _open_dropdown()
    Browser._on_down(browser, None)
    Browser._on_enter(browser, None)
    eq(_select_node(tab).attributes.get("value"), "d", "moved first")
    form = next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == "form")
    tab.reset_form(form)
    eq([option_value(o) for o in selected_options(_select_node(tab))], ["b"],
       "reset returns to the markup's own choice")


# -- expanded <select>: size and multiple ------------------------------------

_LISTBOX_PAGE = (
    '<form><select name="city" size="4">'
    '<option value="a">Amsterdam</option>'
    '<option value="b" selected>Berlin</option>'
    '<option value="c" disabled>Copenhagen</option>'
    '<option value="d">Dublin</option>'
    '<option value="e">Edinburgh</option>'
    '<option value="f">Faroe</option>'
    '</select></form>'
)

_MULTI_PAGE = (
    '<select multiple>'
    '<option value="a" selected>Ant</option>'
    '<option value="b">Bee</option>'
    '<option value="c">Cricket</option>'
    '</select>'
)


class _Ev:
    """A stand-in for a GUI event, carrying only what a handler reads."""

    def __init__(self, x=0, y=0, delta=0, char="", keysym="", state=0):
        self.x, self.y, self.delta = x, y, delta
        self.char, self.keysym, self.state = char, keysym, state


def _listbox(body=_LISTBOX_PAGE):
    """Load a page whose select is expanded, returning (browser, tab)."""
    tab = _make_tab(body)
    return _SelectBrowser(tab), tab


def _click_row(browser, tab, i, node=None):
    """Click row `i` of the page's (only) expanded select."""
    node = node or _select_node(tab)
    lx, ty, _rx, _by = tab._control_rect(node)
    top = listbox_scroll(node)
    y = ty + LISTBOX_PAD + (i - top + 0.5) * LISTBOX_ROW_H
    tab.click(lx + 6, y - tab.scroll)


def _chosen(tab, node=None):
    return [option_label(o)
            for o in selected_options(node or _select_node(tab))]


def test_size_expands_a_select_into_a_listbox():
    _browser, tab = _listbox()
    node = _select_node(tab)
    eq(listbox_rows(node), 4, "size=4 shows four rows")
    lx, ty, rx, by = tab._control_rect(node)
    eq(by - ty, 4 * LISTBOX_ROW_H + 2 * LISTBOX_PAD, "the box is four rows tall")
    # The rows are on the page, not behind a control that has to be opened.
    labels = _labels(tab)
    for name in ("Amsterdam", "Berlin", "Copenhagen", "Dublin"):
        assert name in labels, f"{name} not painted: {labels}"
    assert "Edinburgh" not in labels, "the fifth row must be out of view"


def test_size_one_is_still_a_drop_down():
    tab = _make_tab('<select size="1"><option>Ant</option>'
                    '<option selected>Bee</option></select>')
    eq(listbox_rows(_select_node(tab)), 0, "size=1 is a combo, not a listbox")
    x, y = _select_centre(tab)
    assert isinstance(tab.click(x, y), SelectAction), \
        "size=1 must still open a drop-down"


def test_a_listbox_takes_up_room_in_the_flow():
    """The listbox is page content, so what follows it must start below it."""
    tab = _make_tab(_LISTBOX_PAGE + "<p>after</p>")
    _lx, _ty, _rx, by = tab._control_rect(_select_node(tab))
    after = next(c for c in tab.display_list
                 if isinstance(c, DrawText) and c.text == "after")
    assert after.top >= by, \
        f"the paragraph after the listbox starts at {after.top}, inside it"


def test_clicking_a_listbox_row_selects_it():
    browser, tab = _listbox()
    _click_row(browser, tab, 3)              # Dublin
    eq(_chosen(tab), ["Dublin"], "the clicked row is taken")
    eq(_select_node(tab).attributes.get("value"), "d", "select.value follows")
    assert "data-focused" in _select_node(tab).attributes, \
        "clicking a listbox focuses it, which is what the keyboard needs"


def test_a_disabled_option_or_heading_in_a_listbox_swallows_the_click():
    browser, tab = _listbox()
    _click_row(browser, tab, 2)              # Copenhagen, disabled
    eq(_chosen(tab), ["Berlin"], "a disabled row cannot be taken")

    browser, tab = _listbox(
        '<select size="4"><optgroup label="Warm">'
        '<option>Red</option></optgroup></select>')
    _click_row(browser, tab, 0)              # the "Warm" heading
    eq(_chosen(tab), ["Red"], "a heading cannot be taken")


def test_a_disabled_listbox_ignores_clicks():
    browser, tab = _listbox(
        '<select size="3" disabled><option>Locked</option>'
        '<option selected>Also</option></select>')
    _click_row(browser, tab, 0)
    eq(_chosen(tab), ["Also"], "a disabled listbox cannot be changed")


def test_arrows_move_and_commit_in_a_single_choice_listbox():
    browser, tab = _listbox()
    _click_row(browser, tab, 1)              # focus, on Berlin
    Browser._on_down(browser, None)
    # Copenhagen is disabled, so Down from Berlin lands past it.
    eq(_chosen(tab), ["Dublin"], "down skips disabled options and commits")
    Browser._on_up(browser, None)
    eq(_chosen(tab), ["Berlin"], "up comes back")
    Browser._on_home(browser, None)
    eq(_chosen(tab), ["Amsterdam"], "Home goes to the top")
    Browser._on_up(browser, None)
    eq(_chosen(tab), ["Amsterdam"],
       "the top does not wrap round to the bottom")
    Browser._on_end(browser, None)
    eq(_chosen(tab), ["Faroe"], "End goes to the bottom")


def test_multiple_expands_with_a_default_row_count():
    _browser, tab = _listbox(_MULTI_PAGE)
    node = _select_node(tab)
    eq(listbox_rows(node), 4, "a multiple with no size still shows rows")
    lx, ty, rx, by = tab._control_rect(node)
    eq(by - ty, 4 * LISTBOX_ROW_H + 2 * LISTBOX_PAD, "and is that tall")


def test_clicking_a_second_row_of_a_multiple_adds_it():
    browser, tab = _listbox(_MULTI_PAGE)
    _click_row(browser, tab, 1)
    eq(_chosen(tab), ["Ant", "Bee"], "a second click adds rather than moves")
    _click_row(browser, tab, 0)
    eq(_chosen(tab), ["Bee"], "clicking a taken row drops it")


def test_arrows_in_a_multiple_move_without_choosing_until_space():
    browser, tab = _listbox(_MULTI_PAGE)
    # Clicking the row that was already taken focuses the box and drops it,
    # which is the toggle a multiple is for.
    _click_row(browser, tab, 0)
    eq(_chosen(tab), [], "clicking a taken row of a multiple drops it")
    node = _select_node(tab)
    Browser._on_down(browser, None)
    eq(listbox_active(node), 1, "the keyboard moved")
    eq(_chosen(tab), [],
       "but walking a multiple must not sweep rows up as it goes")
    # Space and Enter both land here; _on_key routes the one, _on_enter the
    # other, and this is the work they share.
    assert browser._listbox_commit(), "Space belongs to the listbox"
    eq(_chosen(tab), ["Bee"], "it takes the row the keyboard is on")
    Browser._on_enter(browser, None)
    eq(_chosen(tab), [], "and pressing again drops it")


def test_a_long_listbox_scrolls_the_active_row_into_view():
    browser, tab = _listbox()
    _click_row(browser, tab, 1)
    node = _select_node(tab)
    Browser._on_end(browser, None)
    top = listbox_scroll(node)
    assert top <= listbox_active(node) < top + listbox_rows(node), \
        f"row {listbox_active(node)} is outside the window at top={top}"


def test_the_wheel_scrolls_a_listbox_instead_of_the_page():
    # Plenty of page below the control, so a turn that reaches the page has
    # somewhere to go and the assertion means something.
    browser, tab = _listbox(_LISTBOX_PAGE + "<p>tall</p>" * 200)
    node = _select_node(tab)
    lx, ty, _rx, _by = tab._control_rect(node)
    aim = lambda: _Ev(x=lx + 6, y=ty - tab.scroll + 10, delta=-3)
    browser._on_wheel(aim())
    eq(listbox_scroll(node), 1, "the listbox took the turn")
    eq(tab.scroll, 0, "and the page stayed put")
    # Wheeling past the end hands the turn back rather than trapping it.
    for _ in range(4):
        browser._on_wheel(aim())
    eq(listbox_scroll(node), 2, "a six-row list in a four-row box stops here")
    assert tab.scroll > 0, "the leftover turns went to the page"


def test_resetting_a_form_puts_a_listbox_back():
    browser, tab = _listbox()
    _click_row(browser, tab, 3)
    eq(_chosen(tab), ["Dublin"], "moved first")
    form = next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == "form")
    tab.reset_form(form)
    eq(_chosen(tab), ["Berlin"], "reset returns to the markup's own choice")
    node = _select_node(tab)
    eq(node.attributes.get("data-active"), None,
       "and the keyboard goes back to where the markup left it")


def test_submitting_a_select_carries_every_choice():
    """What is submitted has to equal what the control was showing.

    A `multiple` select is one name with several values, and an <option>
    inside an <optgroup> is not a child of the select at all, so a scan of
    the select's own children submits neither correctly.
    """
    tab = _make_tab(
        '<form method="post" action="/save">'
        '<input name="who" value="ada">'
        '<select name="lang" multiple>'
        '<option selected>Rust<option>Python<option selected>Zig</select>'
        '<select name="city">'
        '<optgroup label="DE"><option value="b">Berlin</option></optgroup>'
        '<optgroup label="IE"><option value="d" selected>Dublin</option>'
        '</optgroup></select>'
        '<input type="submit" name="go" value="Save"></form>')
    cx, cy, _ = _control_box(tab, value="Save")
    act = tab.click(cx, cy)
    assert isinstance(act, FormAction), type(act)
    fields = urllib.parse.parse_qsl(act.payload, keep_blank_values=True)
    eq(fields, [("who", "ada"), ("lang", "Rust"), ("lang", "Zig"),
                ("city", "d"), ("go", "Save")],
       "every choice submitted, alongside the ordinary fields")


def test_an_untouched_select_submits_its_fallback_choice():
    """Marking nothing `selected` still submits: a single-choice select
    falls back to the first option the reader could have picked, which is
    the one the closed control was showing all along."""
    tab = _make_tab(
        '<form method="post" action="/save">'
        '<select name="size"><option disabled>--<option>M<option>L</select>'
        '<input type="submit" value="Go"></form>')
    cx, cy, _ = _control_box(tab, value="Go")
    act = tab.click(cx, cy)
    fields = urllib.parse.parse_qsl(act.payload, keep_blank_values=True)
    eq(fields, [("size", "M")], "the first choosable option, not the disabled one")


class _FakeResolver:
    """Stands in for socket.getaddrinfo. Hosts named in `stall` block until
    released; everything else answers at once. Records what it was asked."""

    def __init__(self, stall=()):
        self.stall = set(stall)
        self.asked = []
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, host, port, *args, **kwargs):
        with self.lock:
            self.asked.append(host)
        if host in self.stall:
            # Bounded so a failing test cannot wedge the suite.
            self.release.wait(30)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", port))]


def _with_resolver(resolver):
    """Install `resolver` as socket.getaddrinfo; returns a restore callable."""
    real = socket.getaddrinfo
    socket.getaddrinfo = resolver

    def restore():
        resolver.release.set()
        socket.getaddrinfo = real
        with net_mod._DNS_LOCK:
            net_mod._DNS_INFLIGHT.clear()
            net_mod._DNS_CACHE.clear()
    return restore


def test_a_dns_lookup_that_never_answers_gives_up():
    """getaddrinfo cannot be interrupted and settimeout does not reach it, so
    an unreachable resolver used to hang the browser with nothing printed.
    The wait is bounded now; the lookup itself still cannot be cancelled."""
    resolver = _FakeResolver(stall=["stalled.invalid"])
    restore = _with_resolver(resolver)
    try:
        started = time.time()
        try:
            net_mod._resolve("stalled.invalid", 80, timeout=0.25)
            assert False, "a resolver that never answers should not return"
        except socket.timeout:
            pass
        waited = time.time() - started
        assert waited < 5, f"waited {waited:.1f}s, so the ceiling did not hold"
    finally:
        restore()


def test_one_stalled_host_does_not_block_lookups_of_another():
    """The regression this guards: the lookup used to happen while holding
    _DNS_LOCK, so a single unreachable host stalled every other thread that
    wanted any host at all -- background image fetches included."""
    resolver = _FakeResolver(stall=["stalled.invalid"])
    restore = _with_resolver(resolver)
    try:
        stuck = threading.Thread(
            target=lambda: _swallow(net_mod._resolve, "stalled.invalid", 80),
            daemon=True)
        stuck.start()
        # Wait until the stalled lookup is genuinely in flight.
        deadline = time.time() + 5
        while "stalled.invalid" not in resolver.asked and time.time() < deadline:
            time.sleep(0.01)
        assert "stalled.invalid" in resolver.asked, "the stall never started"

        started = time.time()
        infos = net_mod._resolve("fine.invalid", 80, timeout=5)
        waited = time.time() - started
        assert infos, "the second host should still resolve"
        assert waited < 2, f"second host waited {waited:.1f}s behind the first"
    finally:
        restore()


def test_a_stalled_host_does_not_stall_connections_to_another():
    """The same regression seen from where it actually bit: _connect, not the
    resolver underneath it. Written against _connect on purpose -- it is the
    entry point that existed when the lock was held across the lookup, so it
    is the one that can tell the two behaviours apart. The connection itself
    is expected to fail (nothing is listening); only the time taken matters.
    """
    resolver = _FakeResolver(stall=["stalled.invalid"])
    restore = _with_resolver(resolver)
    try:
        threading.Thread(
            target=lambda: _swallow(net_mod._connect, "stalled.invalid", 80),
            daemon=True).start()
        deadline = time.time() + 5
        while "stalled.invalid" not in resolver.asked and time.time() < deadline:
            time.sleep(0.01)
        assert "stalled.invalid" in resolver.asked, "the stall never started"

        started = time.time()
        _swallow(net_mod._connect, "fine.invalid", 80)
        waited = time.time() - started
        assert waited < 2, (
            f"connecting to a second host waited {waited:.1f}s behind an "
            "unrelated stalled lookup")
    finally:
        restore()


def test_callers_wanting_the_same_host_share_one_lookup():
    """A page with thirty images on one origin should cost one lookup, not
    thirty -- and on a slow resolver, one waiting thread rather than thirty."""
    resolver = _FakeResolver(stall=["slow.invalid"])
    restore = _with_resolver(resolver)
    try:
        done = []
        threads = [threading.Thread(
            target=lambda: done.append(
                _swallow(net_mod._resolve, "slow.invalid", 80, timeout=10)),
            daemon=True) for _ in range(5)]
        for t in threads:
            t.start()
        # Wait for the first caller to reach the resolver, then give the rest
        # a moment to arrive and coalesce onto it.
        deadline = time.time() + 5
        while "slow.invalid" not in resolver.asked and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        # Counted per host, not as a total: a stalled lookup from an earlier
        # test is still parked on its own thread, and when it is released it
        # lands in whichever fake resolver is installed by then.
        eq(resolver.asked.count("slow.invalid"), 1,
           "five callers should share one lookup")
        resolver.release.set()
        for t in threads:
            t.join(10)
        eq(len(done), 5, "every caller should receive the answer")
    finally:
        restore()


def test_a_resolver_failure_reaches_the_caller():
    """A name that does not exist must still raise, not time out: the worker
    re-raises the resolver's own error on the waiting thread."""
    def broken(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    real = socket.getaddrinfo
    socket.getaddrinfo = broken
    try:
        try:
            net_mod._resolve("nosuch.invalid", 80, timeout=5)
            assert False, "a name that does not resolve should raise"
        except socket.gaierror:
            pass
    finally:
        socket.getaddrinfo = real
        with net_mod._DNS_LOCK:
            net_mod._DNS_INFLIGHT.clear()
            net_mod._DNS_CACHE.clear()


def test_a_failed_request_reports_the_network_error_not_a_cleanup_error():
    """_request_http cleans up from `except` blocks that can be reached
    before any socket exists -- a lookup that fails leaves it unset. Closing
    None raised AttributeError, which is not an OSError and so slipped past
    the guard, and the caller saw that instead of the real failure."""
    resolver = _FakeResolver(stall=["stalled.invalid"])
    restore = _with_resolver(resolver)
    real_timeout = net_mod._DNS_TIMEOUT
    net_mod._DNS_TIMEOUT = 0.25
    try:
        try:
            URL("http://stalled.invalid/").request()
            assert False, "a request to an unresolvable host should raise"
        except AttributeError as exc:
            assert False, f"cleanup error masked the real one: {exc}"
        except OSError:
            pass  # socket.timeout, which is what the caller should see
    finally:
        net_mod._DNS_TIMEOUT = real_timeout
        restore()


def test_closing_a_socket_that_was_never_opened_is_harmless():
    """The narrow version of the same thing, so the intent is recorded even
    if _request_http's cleanup is restructured later."""
    net_mod._close_socket(None)


def test_a_finished_lookup_leaves_nothing_in_flight():
    """The in-flight entry has to be cleared however the lookup ended, or the
    next caller for that host waits on a worker that is already gone."""
    resolver = _FakeResolver()
    restore = _with_resolver(resolver)
    try:
        net_mod._resolve("quick.invalid", 80, timeout=5)
        eq(net_mod._DNS_INFLIGHT, {}, "in-flight entry outlived its lookup")
    finally:
        restore()


def _fixture(name):
    """The bytes of a file in tests/fixtures."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", name)
    with open(path, "rb") as handle:
        return handle.read()


def test_jpeg_photograph_decodes():
    """A photograph, decoded by us and nobody else.

    tests/fixtures/photo.jpg is a crop of NASA/JPL-Caltech PIA22228, which is
    in the public domain, taken exactly as the server sent it: baseline,
    Huffman coded, 4:2:0, which is the shape of very nearly every photograph
    on the web. The colours below are libjpeg's own answers, and one of them
    sits on a chroma edge where replicating a chroma sample instead of
    filtering it lands 46 levels away -- so this notices a decoder that has
    become merely close, which a size check never would.
    """
    from feetbrowser import imagecodec
    width, height, rgba = imagecodec.decode(_fixture("photo.jpg"))
    eq((width, height), (320, 224), "photo.jpg size")
    eq(len(rgba), 320 * 224 * 4, "four bytes a pixel")
    eq(rgba[3::4].count(255), 320 * 224, "a JPEG carries no transparency")
    for (x, y), colour in (((0, 0), (195, 153, 113)),
                           ((319, 223), (167, 108, 66)),
                           ((19, 191), (85, 53, 28)),
                           ((64, 113), (156, 124, 73))):
        at = (y * width + x) * 4
        eq(tuple(rgba[at:at + 3]), colour, "pixel (%d,%d)" % (x, y))


def test_jpeg_progressive_and_restarts_are_the_same_picture():
    """Two rearrangements of the baseline fixture, both made by jpegtran,
    which moves coefficients between scans without changing any of them. All
    three files are therefore the same photograph and have to decode to
    identical bytes: nothing about spectral selection, successive
    approximation, end-of-band runs or restart intervals is allowed to move a
    single sample. Restart markers are worth their own file because they are
    the one thing in the entropy decoder that a picture without them never
    exercises -- and the bit reader is where they go wrong."""
    from feetbrowser import imagecodec
    width, height, base = imagecodec.decode(_fixture("photo.jpg"))
    for name in ("photo-progressive.jpg", "photo-restart.jpg"):
        other_width, other_height, other = imagecodec.decode(_fixture(name))
        eq((other_width, other_height), (width, height), "%s size" % name)
        differing = sum(1 for i in range(len(base)) if base[i] != other[i])
        assert not differing, (
            "%d of %d bytes of %s differ from the baseline coding of the "
            "same photograph" % (differing, len(base), name))


def test_jpeg_greyscale_and_horizontal_subsampling():
    """Two more codings of the same photograph. One component instead of
    three has no chroma to put back and must come out with equal channels;
    4:2:2 halves the chroma across but not down, and goes through the
    horizontal half of the filter alone -- where libjpeg rounds the left and
    right of each output pair in opposite directions. Re-encoding moves the
    picture about a level, so anything much beyond that is the filter."""
    from feetbrowser import imagecodec
    _w, _h, base = imagecodec.decode(_fixture("photo.jpg"))
    width, height, grey = imagecodec.decode(_fixture("photo-grey.jpg"))
    eq((width, height), (320, 224), "greyscale size")
    coloured = sum(1 for i in range(0, len(grey), 4)
                   if not grey[i] == grey[i + 1] == grey[i + 2])
    assert not coloured, "%d greyscale pixels came out coloured" % coloured
    width, height, half = imagecodec.decode(_fixture("photo-422.jpg"))
    eq((width, height), (320, 224), "4:2:2 size")
    off = sum(abs(base[i] - half[i])
              for i in range(len(base)) if i % 4 != 3) / (width * height * 3)
    assert off < 3.0, (
        "the 4:2:2 coding is %.2f levels a channel from the 4:2:0 one, which "
        "is more than re-encoding explains" % off)


def test_a_cut_off_jpeg_decodes_as_far_as_it_arrived():
    """A connection that drops mid-photograph is ordinary, and the half that
    arrived is worth drawing -- which is what every other browser does. So a
    truncated scan is not an error: the bit reader runs out and hands back
    zeroes, the rest of the picture comes out flat, and the top of it is
    still exactly what the whole file decodes to."""
    from feetbrowser import imagecodec
    whole = _fixture("photo.jpg")
    width, height, full = imagecodec.decode(whole)
    _w, _h, part = imagecodec.decode(whole[:len(whole) // 2])
    eq((_w, _h), (width, height), "the header said how big it is")
    rows = sum(1 for y in range(height)
               if full[y * width * 4:(y + 1) * width * 4]
               == part[y * width * 4:(y + 1) * width * 4])
    assert rows > height // 3, (
        "only %d of %d rows survived half the file" % (rows, height))


def test_jpeg_unsupported_modes_raise_image_error():
    """The JPEG family is much larger than the part of it the web uses, and
    what is not implemented has to say so. The alternative -- running
    arithmetic-coded coefficients through a Huffman decoder, say -- is a
    picture made of noise, presented as though it were the page's."""
    from feetbrowser import imagecodec
    good = bytearray(_fixture("photo.jpg"))
    frame = 2
    while good[frame + 1] != 0xC0:      # walk the segments to the frame header
        frame += 2 + ((good[frame + 2] << 8) | good[frame + 3])
    cases = {"arithmetic coding": (frame + 1, 0xC9),
             "lossless": (frame + 1, 0xC3),
             "hierarchical": (frame + 1, 0xC5),
             "12-bit samples": (frame + 4, 12),
             "four components": (frame + 9, 4)}
    for name, (at, value) in cases.items():
        broken = bytearray(good)
        broken[at] = value
        try:
            imagecodec.decode(bytes(broken))
        except imagecodec.ImageError:
            continue
        except Exception as exc:  # noqa: BLE001 - that is the point
            raise AssertionError("%s raised %r, not ImageError" % (name, exc))
        raise AssertionError("%s decoded instead of being refused" % name)


def _one_block_jpeg(coefs, quant):
    """A whole JPEG file carrying a single 8x8 greyscale block.

    Written here so the transform below can be handed coefficients chosen to
    catch it out, which no photograph off the web can be made to contain.
    Both Huffman tables are picked to make the coding trivial rather than
    short: sixteen four-bit DC codes and 255 eight-bit AC ones, assigned in
    order, so the canonical code for a symbol is the symbol's own value and
    writing one is writing four or eight bits.
    """
    zigzag = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
              12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
              35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
              58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63]
    out = []
    pending = [0, 0]  # bits so far, how many of them

    def put(value, count):
        pending[0] = (pending[0] << count) | (value & ((1 << count) - 1))
        pending[1] += count
        while pending[1] >= 8:
            pending[1] -= 8
            byte = (pending[0] >> pending[1]) & 0xFF
            out.append(byte)
            if byte == 0xFF:
                out.append(0x00)  # the stuffing every decoder has to undo

    def magnitude(value):
        size = abs(value).bit_length()
        return size, (value if value > 0 else value + (1 << size) - 1)

    size, bits = magnitude(coefs[0])
    put(size, 4)
    if size:
        put(bits, size)
    run = 0
    for k in range(1, 64):
        value = coefs[zigzag[k]]
        if not value:
            run += 1
            continue
        while run > 15:
            put(0xF0, 8)   # sixteen zeroes and no coefficient
            run -= 16
        size, bits = magnitude(value)
        put((run << 4) | size, 8)
        put(bits, size)
        run = 0
    if run:
        put(0x00, 8)       # end of block
    if pending[1]:
        put((1 << (8 - pending[1])) - 1, 8 - pending[1])

    def segment(marker, body):
        return bytes([0xFF, marker]) + bytes([(len(body) + 2) >> 8,
                                              (len(body) + 2) & 0xFF]) + body

    return b"".join([
        b"\xff\xd8",
        segment(0xDB, bytes([0]) + bytes(quant[z] for z in zigzag)),
        segment(0xC0, bytes([8, 0, 8, 0, 8, 1, 1, 0x11, 0])),
        segment(0xC4, bytes([0x00] + [0] * 3 + [16] + [0] * 12
                            + list(range(16)))),
        segment(0xC4, bytes([0x10] + [0] * 7 + [255] + [0] * 8
                            + list(range(255)))),
        segment(0xDA, bytes([1, 1, 0x00, 0, 63, 0])),
        bytes(out),
        b"\xff\xd9"])


def test_jpeg_transform_matches_the_textbook_one():
    """The inverse transform is the AAN factorisation, which reaches the
    definition's answer by a much shorter route -- five multiplications a
    pass rather than sixty-four. Fast and wrong looks exactly like fast, so
    it is held against the definition, on coefficients quantised the way an
    encoder would have quantised them. That range is the only one where the
    comparison means anything: on coefficients no encoder could emit, both
    transforms run thousands of levels outside the sample range, where the
    only thing left answering is the clamp.

    Three shapes, because the transform takes a shortcut on two of them: a
    block whose AC coefficients are all zero is a flat colour and skips the
    transform entirely, a block with zeroed columns skips those columns, and
    a block with nothing zero in it takes the long way through.
    """
    import math
    import random
    from feetbrowser import imagecodec
    cosine = [[math.cos((2 * x + 1) * u * math.pi / 16)
               * (math.sqrt(0.5) if u == 0 else 1.0)
               for u in range(8)] for x in range(8)]

    def forward(samples, quant):
        """What an encoder would have written for these samples."""
        cols = [[sum((samples[y * 8 + x] - 128) * cosine[x][u]
                     for x in range(8)) / 2.0 for u in range(8)]
                for y in range(8)]
        return [int(round(sum(cols[y][u] * cosine[y][v] for y in range(8))
                          / 2.0 / quant[v * 8 + u]))
                for v in range(8) for u in range(8)]

    def textbook(coefs, quant):
        """The transform as its definition states it: a double sum."""
        f = [coefs[i] * quant[i] for i in range(64)]
        rows = [[sum(f[v * 8 + u] * cosine[x][u] for u in range(8)) / 2.0
                 for x in range(8)] for v in range(8)]
        return [min(255, max(0, int(round(
            sum(rows[v][x] * cosine[y][v] for v in range(8)) / 2.0 + 128))))
            for y in range(8) for x in range(8)]

    random.seed(20260814)
    worst = 0
    for case in range(60):
        quant = [random.choice([1, 2, 3, 4, 6, 10, 16, 25, 40, 99])
                 for _ in range(64)]
        samples = [random.randrange(256) for _ in range(64)]
        if case % 3 == 1:
            samples = [samples[0]] * 64            # flat: DC and nothing else
        coefs = forward(samples, quant)
        if case % 3 == 2:
            for u in range(0, 8, 2):               # empty every other column
                for v in range(8):
                    coefs[v * 8 + u] = 0
        width, height, rgba = imagecodec.decode(_one_block_jpeg(coefs, quant))
        eq((width, height), (8, 8), "the hand-built file is one block")
        ours = list(rgba[0::4])
        worst = max(worst, max(abs(a - b)
                               for a, b in zip(ours, textbook(coefs, quant))))
    assert worst <= 1, (
        "the fast transform is %d levels from the definition" % worst)


def test_images_do_not_reach_for_a_third_party_library():
    """Pillow decoded JPEG here and cairosvg rasterised SVG. Nothing does
    now, and this is the assertion that says so: on the one CI job that
    installs both, an import added back by reflex fails the suite instead of
    passing quietly. Everywhere else neither is installed and this costs
    nothing to run."""
    from feetbrowser import imagecodec
    imagecodec.decode(_fixture("photo.jpg"))
    Tab._decode_image(_fixture("photo.jpg"), "image/jpeg")
    for module in ("PIL", "PIL.Image", "cairosvg"):
        assert module not in sys.modules, \
            "%s was imported on the way to decoding an image" % module


# -- the tab strip: dragging a tab to reorder it -----------------------------


class _StripTab:
    """A stand-in for a Tab on the strip: a name to tell it apart from its
    neighbours, and the little the release and Escape paths read off a tab
    once the strip has decided the event is not theirs."""

    def __init__(self, title):
        self.title = title
        self.selection = None
        self.focused_input = None

    def stop_videos(self):
        pass

    def __repr__(self):
        return "<tab %s>" % self.title


class _TabBrowser(Browser):
    """A Browser holding a strip of tabs with the painting taken out.

    Every step of the gesture -- the press, the moves, the release, Escape --
    runs through the real Browser handlers; only the calls that would put
    pixels on a canvas are counted instead of drawn.
    """

    def __init__(self, count=4):
        self.tabs = [_StripTab(chr(ord("A") + i)) for i in range(count)]
        self.active_tab = self.tabs[0]
        self.focus = None
        self.toe_contexts = []
        self.select_popup = SelectPopup()
        self.context_menu = type("Menu", (), {"open_": False})()
        self.downloads_panel = type(
            "Panel", (), {"point_in": lambda s, x, y: False})()
        self.window = type("Win", (), {"destroy": lambda s: None})()
        self._scroll_grab = None
        self._tab_drag = None
        self._drag_moved = False
        self._click_count = 0
        self._momentum_job = None
        self._range_grab = None
        self.paints = 0
        self.canvas = type("C", (), {"winfo_width": lambda s: 1000,
                                     "winfo_height": lambda s: 720})()

    def chrome_height(self):
        return 80

    def draw(self):
        self.paints += 1

    def _draw_chrome(self):
        self.paints += 1


def _order(browser):
    """The strip's tab list, as a string like "BACD"."""
    return "".join(tab.title for tab in browser.tabs)


def _press_tab(browser, i, dx=None):
    """Press on tab `i`, `dx` pixels in from its left edge (its middle by
    default). Returns the x the press landed on."""
    x = TAB_LEFT + i * TAB_GAP + (TAB_WIDTH // 2 if dx is None else dx)
    browser._on_click(_Ev(x=x, y=20))
    return x


def test_tab_slot_matches_moving_the_tab_in_the_list():
    """The slots the strip draws mid-drag are exactly the arrangement the
    drop produces. If the two ever disagreed the tab would land somewhere
    other than the gap the user was looking at."""
    names = list("ABCDE")
    for home in range(len(names)):
        for target in range(len(names)):
            dropped = list(names)
            dropped.insert(target, dropped.pop(home))
            drawn = [None] * len(names)
            for j, name in enumerate(names):
                drawn[_tab_slot(j, home, target)] = name
            eq(drawn, dropped, f"home={home} target={target}")


def test_a_press_and_release_without_moving_is_a_plain_tab_click():
    browser = _TabBrowser()
    x = _press_tab(browser, 2)
    eq(browser.active_tab.title, "C", "the press did not switch tabs")
    assert browser._tab_drag is not None, "the press armed no drag"
    browser._on_release(_Ev(x=x, y=20))
    eq(_order(browser), "ABCD", "a plain click reordered the strip")
    eq(browser.active_tab.title, "C", "the click lost the tab it selected")
    assert browser._tab_drag is None, "the gesture outlived the release"

    # And the shake a hand puts into a click is not a drag either.
    x = _press_tab(browser, 1)
    browser._on_drag(_Ev(x=x + TAB_DRAG_SLOP - 1, y=20))
    assert not browser._tab_drag.moved, "jitter under the slop started a drag"
    browser._on_drag(_Ev(x=x + TAB_DRAG_SLOP, y=20))
    assert browser._tab_drag.moved, "a real move did not start the drag"
    browser._on_release(_Ev(x=x + TAB_DRAG_SLOP, y=20))
    eq(_order(browser), "ABCD", "a few pixels of travel moved the tab")


def test_a_press_on_the_close_box_is_still_a_close():
    browser = _TabBrowser()
    _press_tab(browser, 1, dx=TAB_WIDTH - TAB_CLOSE_W // 2)
    eq(_order(browser), "ACD", "the close box stopped closing")
    assert browser._tab_drag is None, "the close box armed a drag"


def test_dragging_a_tab_past_a_neighbour_reorders_it():
    browser = _TabBrowser()
    x = _press_tab(browser, 0)
    half = TAB_GAP // 2
    browser._on_drag(_Ev(x=x + half - 1, y=20))
    eq(browser._tab_drag.target, 0, "the drop moved before the midpoint")
    browser._on_drag(_Ev(x=x + half, y=20))
    eq(browser._tab_drag.target, 1, "crossing the midpoint moved nothing")
    eq(_order(browser), "ABCD", "the list moved before the drop")
    browser._on_release(_Ev(x=x + half, y=20))
    eq(_order(browser), "BACD", "the tab did not land where the gap was")
    eq(browser.active_tab.title, "A", "the dragged tab stopped being active")
    eq(browser.tabs.index(browser.active_tab), 1, "...at its new place")
    assert browser._tab_drag is None, "the drag outlived the drop"


def test_the_other_tabs_shift_to_show_where_the_drop_lands():
    browser = _TabBrowser()
    x = _press_tab(browser, 0)
    browser._on_drag(_Ev(x=x + 2 * TAB_GAP, y=20))
    eq(browser._tab_drag.target, 2, "two strides right is two slots along")
    places = {tab.title: px for tab, px, _drag in browser._tab_positions()}
    eq(places["B"], browser._tab_x(0), "B did not shift into the hole")
    eq(places["C"], browser._tab_x(1), "C did not shift into the hole")
    eq(places["D"], browser._tab_x(3), "D moved with no reason to")
    eq(places["A"], TAB_LEFT + 2 * TAB_GAP, "the tab left the pointer")
    carried, _px, dragged = browser._tab_positions()[-1]
    eq(carried.title, "A", "the carried tab is painted under its neighbours")
    assert dragged, "the carried tab is not marked as the one being carried"

    # Back the way it came: the strip closes up again without a release.
    browser._on_drag(_Ev(x=x, y=20))
    eq(browser._tab_drag.target, 0, "coming back did not undo the shift")
    places = {tab.title: px for tab, px, _drag in browser._tab_positions()}
    eq(places, {name: browser._tab_x(i)
                for i, name in enumerate("ABCD")},
       "the strip did not close up again")


def test_a_carried_tab_is_clamped_to_the_strip():
    browser = _TabBrowser()
    x = _press_tab(browser, 2)
    browser._on_drag(_Ev(x=x - 10 * TAB_GAP, y=20))
    eq(browser._tab_drag.left(), TAB_LEFT, "carried off the left end")
    eq(browser._tab_drag.target, 0, "the drop is not the first slot")
    browser._on_drag(_Ev(x=x + 10 * TAB_GAP, y=20))
    eq(browser._tab_drag.left(), TAB_LEFT + 3 * TAB_GAP,
       "carried off the right end")
    eq(browser._tab_drag.target, 3, "the drop is not the last slot")
    browser._on_release(_Ev(x=x + 10 * TAB_GAP, y=20))
    eq(_order(browser), "ABDC", "a tab dragged off the end did not land last")


def test_escape_puts_a_dragged_tab_back_where_it_started():
    browser = _TabBrowser()
    x = _press_tab(browser, 3)
    browser._on_drag(_Ev(x=x - 2 * TAB_GAP, y=20))
    eq(browser._tab_drag.target, 1, "the strip is not showing a drop")
    Browser._on_escape(browser, None)
    assert browser._tab_drag is None, "the drag survived Escape"
    eq(_order(browser), "ABCD", "Escape moved the tab anyway")
    eq([px for _t, px, _d in browser._tab_positions()],
       [browser._tab_x(i) for i in range(4)],
       "the strip did not settle back into its plain geometry")
    # The user still has the button down; the release that follows must not
    # drop a tab that has already gone home.
    browser._on_release(_Ev(x=x - 2 * TAB_GAP, y=20))
    eq(_order(browser), "ABCD", "the release after Escape moved the tab")


def test_a_drag_that_loses_the_pointer_is_cancelled_not_dropped():
    """A release delivered somewhere we never see it (the grab was broken)
    would otherwise leave a tab stuck to the pointer for the rest of the
    session. The next press is where that is noticed, and the tab goes back
    rather than landing at a place the user may never have seen it reach."""
    browser = _TabBrowser()
    x = _press_tab(browser, 0)
    browser._on_drag(_Ev(x=x + 2 * TAB_GAP, y=20))
    # The press that notices lands on bare strip -- right of the last tab and
    # clear of the "+" -- so nothing else claims it and the only thing that
    # can put the tab back is the cancel.
    browser._on_click(_Ev(x=browser._new_tab_x() + NEW_TAB_W + 20, y=20))
    assert browser._tab_drag is None, "the hanging drag survived a new press"
    browser._on_release(_Ev(x=x + 2 * TAB_GAP, y=20))
    eq(_order(browser), "ABCD", "the lost drag dropped the tab anyway")
    eq(browser.active_tab.title, "A", "the cancelled drag lost its tab")
    # A press on another tab still does what a press on a tab does.
    _press_tab(browser, 3)
    eq(browser.active_tab.title, "D", "the new press did not select its tab")
    assert browser._tab_drag is not None, "the new press armed no drag"

    # A tab list that changes under a running drag (Ctrl-W, Ctrl-T) invalidates
    # it too: the indices it holds no longer mean what they did.
    browser = _TabBrowser()
    x = _press_tab(browser, 0)
    browser._on_drag(_Ev(x=x + 2 * TAB_GAP, y=20))
    browser.close_tab()
    assert browser._tab_drag is None, "the drag survived the strip changing"
    browser._on_release(_Ev(x=x + 2 * TAB_GAP, y=20))
    eq(_order(browser), "BCD", "the stale drag moved a tab after the close")


def test_reordering_leaves_no_stale_tab_index():
    """Nothing keeps a tab index between events -- close_tab, _cycle_tab and
    _next_tab each ask self.tabs.index() at the moment they need one -- so
    every one of them has to walk the new order after a drop, and the active
    tab has to still be the tab that was dragged."""
    browser = _TabBrowser()
    x = _press_tab(browser, 0)
    browser._on_drag(_Ev(x=x + 2 * TAB_GAP, y=20))
    browser._on_release(_Ev(x=x + 2 * TAB_GAP, y=20))
    eq(_order(browser), "BCAD", "the drop")
    eq(browser.active_tab.title, "A", "the dragged tab stopped being active")
    browser._cycle_tab(1)
    eq(browser.active_tab.title, "D", "Ctrl-Tab walked the old order")
    browser._cycle_tab(-1)
    eq(browser.active_tab.title, "A", "Ctrl-Shift-Tab walked the old order")
    browser._next_tab(-1)
    eq(browser.active_tab.title, "C", "Ctrl-PageUp walked the old order")
    browser.close_tab()
    eq(_order(browser), "BAD", "closing took the wrong tab")
    eq(browser.active_tab.title, "A",
       "closing handed the strip to the tab at the old index")


def main():
    root = Tk(); root.withdraw()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
