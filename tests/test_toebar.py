"""Unit tests for the toe-bar toe and the chrome-band/popup framework."""
import os
import sys
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import toes
from feetbrowser.browser import Tab, PopupWindow
from feetbrowser.layout import DrawText


class StubBrowser:
    def __init__(self):
        self.tabs = []
        self.active_tab = None
        self.toes = [t for t in toes.discover_toes() if t.name == "toe-bar"]
        assert self.toes, "toe-bar toe not discovered"
        self.toe_contexts = [toes.Context(self, t.module) for t in self.toes]
        self.popups = []
        self.window = None

    def current_tab(self):
        return self.active_tab

    def draw(self):
        pass


def ctx_of(stub):
    return stub.toe_contexts[0]


def display_text(tab):
    return " ".join(c.text for c in tab.display_list
                    if isinstance(c, DrawText))


def test_discovery():
    names = {t.name for t in toes.discover_toes()}
    assert "toe-bar" in names, names


def test_band_declared():
    stub = StubBrowser()
    bands = toes.compute_bands(stub.toe_contexts)
    assert ("toe-bar", 30, 0) in bands, bands


def test_band_height():
    stub = StubBrowser()
    assert toes.band_height(toes.compute_bands(stub.toe_contexts)) == 30


def test_settings_page_renders():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://toebar")
    rendered = display_text(tab)
    assert "THE TOE BAR" in rendered
    assert "VISITOR NUMBER" in rendered


def test_toggle_bar_persists():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    ctx.settings["bar_on"] = True
    ctx.save_settings()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://toebar/toggle/bar")
    assert ctx.settings.get("bar_on") is False
    # Reload settings from disk to confirm persistence.
    ctx._settings = None
    assert ctx.settings.get("bar_on") is False
    # Restore for other tests.
    ctx.settings["bar_on"] = True
    ctx.save_settings()


def test_ad_page_renders():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://ad/1")
    rendered = display_text(tab)
    assert "CLOSE" in rendered
    assert "MORE FREE TOES" in rendered


def test_youve_got_toes_renders():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://ad/youve-got-toes")
    assert "YOU'VE GOT TOES!" in display_text(tab)


def test_ad_click_spawns_popup():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    ctx.popup = lambda url, width, height: stub.popups.append(url)
    ctx.settings["popup_blocker"] = False
    ctx.call("on_chrome_click", 400, 10, [("toe-bar", 30, 0)])
    assert stub.popups, "ad click did not spawn a popup"
    assert stub.popups[0].startswith("toe://ad/")


def test_popup_blocker_suppresses():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    ctx.popup = lambda url, width, height: stub.popups.append(url)
    ctx.settings["popup_blocker"] = True
    ctx.call("on_chrome_click", 400, 10, [("toe-bar", 30, 0)])
    assert not stub.popups, "popup blocker failed"


def test_band_click_toggles_bar():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    ctx.settings["bar_on"] = True
    ctx.save_settings()
    before = ctx.settings.get("bar_on")
    ctx.call("on_chrome_click", 20, 10, [("toe-bar", 30, 0)])
    assert ctx.settings.get("bar_on") is not before
    ctx.settings["bar_on"] = True
    ctx.save_settings()


def test_ring_hop_opens_page():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    opened = []
    ctx.open = lambda url: opened.append(str(url))
    ctx.call("on_chrome_click", 800, 10, [("toe-bar", 30, 0)])
    assert opened, "ring hop did not open a page"
    assert opened[0].startswith("toe://")


def test_toolbar_button_opens_settings():
    stub = StubBrowser()
    ctx = ctx_of(stub)
    opened = []
    ctx.open = lambda url: opened.append(str(url))
    ctx.call("on_click", "toebar")
    assert opened == ["toe://toebar"]


def test_band_draw_emits_items():
    stub = StubBrowser()
    root = tkinter.Tk(); root.withdraw()
    canvas = tkinter.Canvas(root, width=1000, height=30)
    ctx = ctx_of(stub)
    ctx.settings["bar_on"] = True
    ctx.call("on_chrome_draw", canvas, [("toe-bar", 30, 0)])
    assert len(canvas.find_all()) > 5, "band drew nothing"
    root.destroy()


def test_popup_window_renders():
    stub = StubBrowser()
    root = tkinter.Tk(); root.withdraw()
    popup = PopupWindow(stub, "data:text/html,<h1>pop</h1>", 320, 240)
    assert popup.tab.document is not None
    assert "pop" in display_text(popup.tab)
    popup.window.destroy()
    root.destroy()


def test_cli_scaffold():
    import tempfile
    import feetbrowser.toes as toes_mod
    with tempfile.TemporaryDirectory() as tmp:
        # Point discovery at a temp dir via a fake repo_root.
        orig = toes_mod.repo_root
        toes_mod.repo_root = lambda: tmp
        try:
            rc = toes_mod.new_toe("test-toe")
            assert rc == 0
            found = toes_mod.discover_toes()
            assert any(t.name == "test-toe" for t in found), found
        finally:
            toes_mod.repo_root = orig


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
    print(f"\nALL {len(tests)} TOE BAR TESTS PASSED")


if __name__ == "__main__":
    main()
