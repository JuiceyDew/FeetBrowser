"""Fast, offline unit tests for URL parsing, HTML, CSS, and internal pages."""
import sys, os, tkinter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.net import URL
from feetbrowser.htmlparser import HTMLParser, Element, Text
from feetbrowser.cssparser import CSSParser, style
from feetbrowser.browser import Tab, _AboutURL, tree_to_list


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


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


def test_google_js_wall_falls_back_to_nojs_search():
    # Google serves an "enable JavaScript" stub to JS-less clients. The tab
    # must continue the query on the JS-free results engine, not dead-end.
    from feetbrowser.net import URL
    tab = Tab(700)
    tab.url = URL("https://www.google.com/search?q=hello+world&gbv=1")
    calls = []
    tab.load = lambda url, **kw: calls.append((url, kw))
    wall_body = ('<html><head><title>Google Search</title><meta '
                 'http-equiv="refresh" content="0;url='
                 '/httpservice/retry/enablejs?sei=abc"></head></html>')
    hit = tab._maybe_escape_google_wall(tab.url, wall_body)
    assert hit, "wall should trigger fallback"
    assert calls, "fallback navigation not issued"
    dest, kw = calls[0]
    assert "html.duckduckgo.com/html/" in dest and "q=hello" in dest, dest
    assert kw.get("push") is False and kw.get("refresh") is True
    # Not a wall -> no navigation.
    calls.clear()
    assert not tab._maybe_escape_google_wall(tab.url, "<html>fine</html>")


def test_meta_refresh_followed():
    from feetbrowser.net import URL
    tab = Tab(700)
    tab.url = URL("https://example.com/start")
    calls = []
    tab.load = lambda url, **kw: calls.append((url, kw))
    body = ('<html><head><meta http-equiv="refresh" '
            'content="0; url=/new"> </head><body>x</body></html>')
    assert tab._maybe_meta_refresh(tab.url, body), "refresh should be followed"
    dest, kw = calls[0]
    assert str(dest) == "https://example.com/new", str(dest)
    assert kw.get("push") is False and kw.get("refresh") is True
    # Same-target refresh is not followed (avoids loops); cap at 8 hops.
    calls.clear()
    assert not tab._maybe_meta_refresh(tab.url, body.replace("/new", "/start"))
    tab._refresh_hops = 8
    calls.clear()
    assert not tab._maybe_meta_refresh(tab.url, body), "hop cap stops loops"


def test_error_page_fallback():
    tab = Tab(700)
    # A bad scheme raises in URL(); load() must render an error page, not crash.
    tab.load("https://nonexistent.invalid.example/")
    assert tab.document is not None, "error page laid out"


def test_resolve_color_sanitizes_css_values():
    from feetbrowser.layout import resolve_color
    # CSS custom properties resolve to their fallback, recursively.
    eq(resolve_color("var(--gm3-sys-color-on-primary,#fff)"), "#fff")
    eq(resolve_color("var(--og-link-color,var(--gm3-sys-color-on-surface,#1f1f1f))"),
       "#1f1f1f", "nested var() fallback")
    # Alpha hex loses its alpha; 3/6-digit hex and names pass through.
    eq(resolve_color("#1f1f0f0f"), "#1f1f0f", "8-digit hex alpha dropped")
    eq(resolve_color("black"), "black")
    eq(resolve_color("#1a73e8"), "#1a73e8")
    # Shorthand backgrounds / images / gradients are not colors.
    eq(resolve_color("url(/images/x.png) 0 -261px repeat-x"), None)
    eq(resolve_color("linear-gradient(red,blue)"), None)
    eq(resolve_color("transparent"), None)


def test_var_colors_paintable():
    import tkinter
    root = tkinter.Tk(); root.withdraw()
    try:
        from feetbrowser.layout import resolve_color
        colors = [
            "var(--gm3-sys-color-on-primary,#fff)",
            "var(--og-link-color,var(--gm3-sys-color-on-surface,#1f1f1f))",
            "url(/x.png) 0 -261px repeat-x",
        ]
        for c in colors:
            resolved = resolve_color(c)
            if resolved is not None:
                root.winfo_rgb(resolved)  # must not raise TclError
    finally:
        root.destroy()


def test_form_controls_are_visible():
    # <input>/<textarea> must render as visible boxes (regression: Google's
    # search bar was invisible because no paint command was emitted for it).
    from feetbrowser.cssparser import CSSParser, style
    from feetbrowser.layout import DocumentLayout, paint_tree, DrawOutline
    rules = CSSParser("").parse()
    dom = HTMLParser(
        '<form><input type="hidden" name="x"><input type="text" '
        'name="q" size="20" value=""> '
        '<input type="submit" value="Search"></form>'
    ).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 400)
    doc.layout()
    dl = []
    paint_tree(doc, dl)
    outlines = [c for c in dl if isinstance(c, DrawOutline)]
    assert len(outlines) >= 2, f"expected visible text field + button, got {len(outlines)}"
    widths = sorted(c.right - c.left for c in outlines)
    assert widths[0] >= 15, "field narrower than a box"
    texts = "".join(c.text for c in dl
                    if getattr(c, "text", "") in ("Search",))
    eq(texts, "Search", "submit button label drawn")


def test_controls_do_not_overlap_following_block():
    # A control-only block (e.g. a table cell holding just an <input>) must
    # occupy height so the next block stacks below, not on top of it.
    from feetbrowser.cssparser import CSSParser, style
    from feetbrowser.layout import DocumentLayout, paint_tree, DrawOutline, DrawText
    rules = CSSParser("").parse()
    dom = HTMLParser(
        '<div><td><input type="text" name="q" size="20"></td>'
        '<td><input type="submit" value="Go"></td>'
        '<p>below</p></div>'
    ).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 400)
    doc.layout()
    dl = []
    paint_tree(doc, dl)
    outlines = [c for c in dl if isinstance(c, DrawOutline)]
    below = [c for c in dl if isinstance(c, DrawText) and c.text == "below"][0]
    assert outlines, "no controls painted"
    assert below.top >= max(c.bottom for c in outlines) - 0.1, \
        f"following text overlaps controls: below.top={below.top}, boxes end at " \
        f"{max(c.bottom for c in outlines)}"


def test_input_typing_and_submit():
    tab = Tab(700)
    tab.load("data:text/html,"
             "<form action=/search><input type=hidden name=hl value=en>"
             "<input type=text name=q value=''></form>")
    # The data: base can't resolve "/search"; give a realistic base URL.
    from feetbrowser.net import URL
    tab.url = URL("https://example.com/")
    # Focus the text input.
    from feetbrowser.layout import DrawOutline
    box = None
    for cmd in tab.display_list:
        if isinstance(cmd, DrawOutline) and cmd.node is not None \
                and cmd.node.tag == "input" \
                and (cmd.node.attributes.get("type", "text") or "text").lower() \
                in ("text", "search", "email", "url"):
            box = cmd
            break
    assert box is not None, "search input not painted"
    tab.click((box.left + box.right) / 2, (box.top + box.bottom) / 2)
    assert tab.focused_input is not None, "click should focus the input"
    for ch in "hello world":
        tab.type_into(ch)
    eq(tab.focused_input.attributes["value"], "hello world")
    url = tab.submit_form()
    eq(str(url), "https://example.com/search?hl=en&q=hello+world",
       "form submission URL")


def main():
    root = tkinter.Tk(); root.withdraw()
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
