"""Unit tests for the toes extension engine."""
import os
import sys
import tempfile
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import toes
from feetbrowser.browser import Tab
from feetbrowser.htmlparser import Text, Element
from feetbrowser.net import URL


class StubBrowser:
    """Minimal Browser stand-in: toe contexts plus the bits a Tab touches."""

    def __init__(self, toe_list=None):
        self.tabs = []
        self.active_tab = None
        self.toes = toe_list if toe_list is not None else toes.discover_toes()
        self.toe_contexts = [toes.Context(self, t.module) for t in self.toes]


def find_element(node, tag, attrs):
    if isinstance(node, Element) and node.tag == tag \
            and all(node.attributes.get(k) == v for k, v in attrs.items()):
        return node
    for child in node.children:
        found = find_element(child, tag, attrs)
        if found:
            return found
    return None


def element_text(node):
    parts = []
    if isinstance(node, Text):
        parts.append(node.text)
    for child in node.children:
        parts.append(element_text(child))
    return "".join(parts)


def display_text(tab):
    return " ".join(
        c.text for c in tab.display_list if type(c).__name__ == "DrawText")


def test_discovery_finds_samples():
    toe_list = toes.discover_toes()
    names = {t.name for t in toe_list}
    assert "word-count" in names, names
    assert "toe-scheme" in names, names


def test_unknown_scheme_parses():
    u = URL("toe://hello")
    assert u.scheme == "toe"
    assert u.host == "hello"
    assert str(u) == "toe://hello"


def test_on_load_rewrites_body():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("data:text/html,<body><p>one two three</p></body>")
    div = find_element(tab.nodes, "div", {"class": "toe-word-count"})
    assert div is not None, "word-count did not inject its status line"
    assert "Toes counted" in element_text(div)


def test_extra_css_injected():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("data:text/html,<body><p>hi</p></body>")
    div = find_element(tab.nodes, "div", {"class": "toe-word-count"})
    assert div is not None
    assert div.style.get("font-size") == "13px", div.style
    assert div.style.get("color") == "#666", div.style


def test_toe_scheme_renders():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("toe://hello")
    assert tab.title == "toe://hello", tab.title
    assert tab.document is not None, "toe://hello must lay out"


def test_toe_gallery_lists_toes():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("toe://gallery")
    rendered = display_text(tab)
    assert "word-count" in rendered, rendered
    assert "toe-scheme" in rendered, rendered


def test_unknown_toe_host_is_404():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("toe://nope")
    assert "No such toe" in display_text(tab)


def test_broken_toe_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "broken-toe")
        os.makedirs(broken)
        with open(os.path.join(broken, "toe.json"), "w") as f:
            f.write('{"name": "broken", "entry": "toe.py"}')
        with open(os.path.join(broken, "toe.py"), "w") as f:
            f.write("raise ImportError('kaput')\n")
        toe_list = toes.discover_toes(tmp)
        assert all(t.name != "broken" for t in toe_list), toe_list


def test_buttons_hook_registered():
    class FakeToe:
        manifest = {"name": "fake", "version": "0", "description": ""}

        def activate(self, ctx):
            ctx.on("buttons", lambda: [toes.ButtonDef("fake-btn", "F")])

    toe = toes.Toe("fake", "0", "", "", FakeToe())
    stub = StubBrowser([toe])
    assert stub.toe_contexts[0].call("buttons")[0].id == "fake-btn"


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
    print(f"\nALL {len(tests)} TOE TESTS PASSED")


if __name__ == "__main__":
    main()
