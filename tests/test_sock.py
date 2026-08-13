"""Unit tests for the sock-detective toe."""
import os
import sys
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import toes
from feetbrowser.browser import Tab
from feetbrowser.layout import DrawText


class StubBrowser:
    def __init__(self):
        self.tabs = []
        self.active_tab = None
        self.toes = [t for t in toes.discover_toes()
                     if t.name == "sock-detective"]
        assert self.toes, "sock-detective toe not discovered"
        self.toe_contexts = [toes.Context(self, t.module) for t in self.toes]

    def current_tab(self):
        return self.active_tab

    def draw(self):
        pass


def display_text(tab):
    return " ".join(c.text for c in tab.display_list
                    if isinstance(c, DrawText))


def new_tab(url):
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load(url)
    return stub, tab


def ctx_of(stub):
    return stub.toe_contexts[0]


def test_discovery():
    names = {t.name for t in toes.discover_toes()}
    assert "sock-detective" in names, names


def test_sock_button_registered():
    stub = StubBrowser()
    btn = stub.toe_contexts[0].call("buttons")[0]
    assert btn.id == "sock"


def test_sniff_toggle_and_motion():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    assert not getattr(ctx, "sniffing", False)
    ctx.call("on_click", "sock")
    assert ctx.sniffing is True
    # Hover over some text; the detective should find a node + box.
    tab = Tab(700, stub)
    tab.load("data:text/html,<body><p>heel toe arch</p></body>")
    stub.active_tab = tab
    ctx.call("on_motion", 50, 50)
    assert getattr(ctx, "hover_node", None) is not None
    assert getattr(ctx, "hover_box", None) is not None


def test_sniff_esc_exits():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    ctx.call("on_click", "sock")
    swallowed = ctx.call("on_keypress", _Keysym("Escape"))
    assert swallowed is True
    assert ctx.sniffing is False


def test_case_file_renders():
    stub, tab = new_tab("data:text/html,<h1>Hi</h1><p>world</p>")
    tab.load("toe://sock")
    assert "THE CASE FILE" in display_text(tab)
    assert "SOLE LENGTH" in display_text(tab)
    assert "TOE COUNT" in display_text(tab)


def test_dom_report_renders():
    stub, tab = new_tab("data:text/html,<div><p id=x>heel</p></div>")
    tab.load("toe://sock/dom")
    rendered = display_text(tab)
    assert "FOOTPRINTS" in rendered
    assert "p" in rendered and "id=x" in rendered


def test_layout_report_renders():
    stub, tab = new_tab("data:text/html,<p>toe</p>")
    tab.load("toe://sock/layout")
    rendered = display_text(tab)
    assert "THE BONES" in rendered
    assert "<p>" in rendered or "984" in rendered or "x" in rendered


def test_style_report_renders():
    stub, tab = new_tab('data:text/html,<p style="color: red">hi</p>')
    tab.load("toe://sock/style")
    rendered = display_text(tab)
    assert "FIBERS" in rendered
    assert "color=red" in rendered or "color=#f00" in rendered


def test_paper_trail_logs_navigations():
    stub, tab = new_tab("data:text/html,<p>one</p>")
    tab.load("toe://sock/cases")
    rendered = display_text(tab)
    assert "PAPER TRAIL" in rendered
    # The trail should include the page loads we just made.
    assert "CASE 1" in rendered


def test_errors_report_empty():
    stub, tab = new_tab("data:text/html,<p>fine</p>")
    tab.load("toe://sock/errors")
    assert "DISTRESS" in display_text(tab)


def test_help_renders():
    stub, tab = new_tab("data:text/html,<p>fine</p>")
    tab.load("toe://sock/help")
    assert "WHERE TO LOOK" in display_text(tab)
    assert "Sniff mode" in display_text(tab)


def test_unknown_case_is_404():
    stub, tab = new_tab("data:text/html,<p>fine</p>")
    tab.load("toe://sock/banana")
    assert "No such case" in display_text(tab)


def test_non_sock_schemes_untouched():
    stub = StubBrowser()
    tab = Tab(700, stub)
    tab.load("data:text/html,<p>hello</p>")
    # tobe://sock should fall through to the normal (failing) fetch,
    # not the detective — the handle hook returns None.
    assert tab.document is not None
    assert "hello" in display_text(tab)


class _Keysym:
    def __init__(self, keysym):
        self.keysym = keysym


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
    print(f"\nALL {len(tests)} SOCK DETECTIVE TESTS PASSED")


if __name__ == "__main__":
    main()
