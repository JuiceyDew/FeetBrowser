"""Tests for the browser settings: the registry, non-destructive
persistence, the about:settings page, and applying a setting."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import settings
from feetbrowser.window import Tk
from feetbrowser.browser import (
    _SettingsURL, _SettingsApplyURL, settings_html,
)


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def _fresh_file():
    """Point the settings module at a throwaway file and return its path."""
    tmp = tempfile.mkdtemp()
    original = settings.SETTINGS_FILE
    path = os.path.join(tmp, "settings.json")
    settings.SETTINGS_FILE = path
    return path, original


def _restore(original):
    settings.SETTINGS_FILE = original


def test_every_setting_has_a_key_and_default():
    for setting in settings.SETTINGS:
        assert setting.key and setting.default is not None, \
            f"setting is missing key or default: {setting!r}"


def test_defaults_load_with_no_file():
    path, original = _fresh_file()
    try:
        values = settings.load()
        eq(values["search_engine"], "duckduckgo", "default engine")
        eq(values["show_link_preview"], True, "link preview defaults on")
        eq(values["scroll_speed"], 80, "default scroll speed")
        eq(values["momentum"], True, "momentum defaults on")
        eq(values["momentum_strength"], 100, "default strength")
    finally:
        _restore(original)


def test_save_and_reload_roundtrip():
    path, original = _fresh_file()
    try:
        settings.save({"search_engine": "bing", "scroll_speed": 120})
        values = settings.load()
        eq(values["search_engine"], "bing", "saved engine reloads")
        eq(values["scroll_speed"], 120, "saved speed reloads")
        eq(values["show_link_preview"], True, "untouched setting keeps default")
    finally:
        _restore(original)


def test_save_preserves_unknown_keys():
    path, original = _fresh_file()
    try:
        with open(path, "w", encoding="utf8") as f:
            f.write('{"background_image": "/tmp/pic.png"}')
        settings.save({"search_engine": "google"})
        with open(path, encoding="utf8") as f:
            data = f.read()
        assert "background_image" in data, "a toe's key was wiped out"
        assert "/tmp/pic.png" in data, "a toe's value was wiped out"
        assert "google" in data, "the browser's own key was written"
    finally:
        _restore(original)


def test_coerce_clamps_slider_values():
    path, original = _fresh_file()
    try:
        settings.save({"scroll_speed": 9999})
        settings.save({"momentum_strength": -5})
        values = settings.load()
        eq(values["scroll_speed"], 160, "speed clamps to its max")
        eq(values["momentum_strength"], 0, "strength clamps to its min")
        settings.save({"scroll_speed": "banana"})
        eq(settings.load()["scroll_speed"], 80, "garbage falls back to default")
    finally:
        _restore(original)


def test_coerce_choice_rejects_unknown():
    path, original = _fresh_file()
    try:
        settings.save({"search_engine": "yahoo"})
        eq(settings.load()["search_engine"], "duckduckgo",
           "unknown engine falls back to default")
    finally:
        _restore(original)


def test_coerce_toggle_reads_on_off_strings():
    path, original = _fresh_file()
    try:
        toggle = settings.by_key("momentum")
        eq(toggle.coerce("off"), False, "'off' turns a toggle off")
        eq(toggle.coerce("on"), True, "'on' turns a toggle on")
        eq(toggle.coerce("false"), False, "'false' turns a toggle off")
        eq(toggle.coerce("0"), False, "'0' turns a toggle off")
        eq(toggle.coerce(1), True, "a truthy number is on")
        eq(toggle.coerce(0), False, "a falsy number is off")
        settings.save({"momentum": "off"})
        eq(settings.load()["momentum"], False, "saved 'off' reloads as False")
    finally:
        _restore(original)


def test_search_url_builds_each_engine():
    eq(settings.search_url("duckduckgo", "cat pics"),
       "https://duckduckgo.com/html/?q=cat+pics")
    assert settings.search_url("bing", "a b").startswith(
        "https://www.bing.com/search?q=a+b")
    assert settings.search_url("google", "x").startswith(
        "https://www.google.com/search?q=x")
    assert "html" in settings.search_url("nope", "q"), \
        "unknown engine falls back to DuckDuckGo"


def test_momentum_peak_scales_with_strength():
    eq(settings.momentum_peak(0), 0.0, "zero strength is zero speed")
    eq(settings.momentum_peak(50), 20.0, "half strength is half speed")
    eq(settings.momentum_peak(100), settings.MOMENTUM_MAX_PX,
       "full strength is the full ceiling")


def test_settings_page_lists_every_control():
    theme = None
    body = settings_html(dict(settings.load()), theme)
    for setting in settings.SETTINGS:
        assert setting.label in body, \
            f"{setting.key!r} label missing from the settings page"
    assert "about:settings/search_engine/" in body, "search engine pills"
    assert "about:settings/show_link_preview/" in body, "link preview toggle"
    assert 'type="range"' in body, "sliders render as range inputs"
    assert "about:settings/momentum/" in body, "momentum toggle"
    assert "apply_setting" in body, "the range change handler is present"
    assert "peak" in body, "momentum slider shows its speed"


def test_settings_page_shows_current_values():
    body = settings_html({"scroll_speed": 120, "momentum_strength": 50,
                          "search_engine": "bing", "show_link_preview": True,
                          "momentum": True}, None)
    assert "120 px" in body, "current scroll speed is shown"
    assert "peak 20.0 px/frame" in body, "half-strength peak is shown"
    assert ('id="scroll_speed" name="scroll_speed" type="range" '
            'min="40" max="160" step="10" value="120"' in body), \
        "the scroll speed range carries its current value"


def test_settings_apply_url_calls_apply_and_rerenders():
    calls = []
    url = _SettingsApplyURL("scroll_speed", "60",
                            apply=lambda k, v: calls.append((k, v)))
    _headers, body, _ct = url.request()
    eq(calls, [("scroll_speed", "60")], "apply is called with key and value")
    assert "Settings" in body, "the page re-renders after an apply"


def test_settings_url_resolves_links():
    values = dict(settings.load())
    url = _SettingsURL(settings_provider=lambda: values, apply=lambda k, v: None)
    apply_url = url.resolve("about:settings/scroll_speed/60")
    assert isinstance(apply_url, _SettingsApplyURL), "control link resolves"
    eq(apply_url.key, "scroll_speed")
    eq(apply_url.value, "60")
    back = apply_url.resolve("about:settings")
    assert isinstance(back, _SettingsURL), "back link resolves to the page"
    assert str(url) == "about:settings", "string form is the settings URL"


# -- the draggable range control -------------------------------------------

_RANGE_PAGE = (
    '<!doctype html><html><body>'
    '<input id="a" name="a" type="range" min="40" max="160" step="10" '
    'value="80" onchange="go()">'
    '<div id="out"></div>'
    '<script>function go() { document.getElementById("out").textContent = '
    'document.getElementById("a").value; }</script>'
    '</body></html>'
)


def _range_browser(body=_RANGE_PAGE):
    """A real Browser showing `body`, with the settings file fenced off."""
    from feetbrowser.browser import Browser
    from feetbrowser.browser import tree_to_list
    from feetbrowser.htmlparser import Element
    from feetbrowser.window import Event

    path, original = _fresh_file()
    root = Tk(); root.withdraw()
    browser = Browser()
    browser.new_tab("data:text/html," + body)
    browser.draw()
    return browser, path, original, tree_to_list, Element, Event


def _range_input(browser, tree_to_list, Element):
    tab = browser.tabs[0]
    nodes = tree_to_list(tab.nodes, [])
    return next(n for n in nodes
                if isinstance(n, Element) and n.tag == "input"), tab


def _drag_range(browser, tab, node, Event, frac):
    ch = browser.chrome_height()
    lx, ty, rx, by = tab._control_rect(node)
    px = int(lx + (rx - lx) * frac)
    py = int(ty + 5) + ch
    browser._on_click(Event(x=px, y=py))
    browser._on_release(Event(x=px, y=py))
    browser.window.flush_timers()
    return px, py


def test_range_press_grabs_and_drag_moves_the_value():
    browser, path, original, ttl, Element, Event = _range_browser()
    try:
        node, tab = _range_input(browser, ttl, Element)
        eq(node.attributes["value"], "80", "starts at the marked value")
        browser._on_click(Event(x=0, y=0))  # chrome, not the range
        lx, ty, rx, by = tab._control_rect(node)
        ch = browser.chrome_height()
        browser._on_click(Event(x=int(lx + 10), y=int(ty + 5) + ch))
        assert browser._range_grab is not None, "press on the range grabs it"
        browser._on_drag(Event(x=int(lx + (rx - lx) * 0.9),
                               y=int(ty + 5) + ch))
        assert int(node.attributes["value"]) > 80, \
            "dragging toward the high end raises the value"
        browser._on_drag(Event(x=int(lx + (rx - lx) * 0.1),
                               y=int(ty + 5) + ch))
        assert int(node.attributes["value"]) < 80, \
            "dragging toward the low end lowers the value"
        browser._on_release(Event(x=0, y=0))
        assert browser._range_grab is None, "release ends the grab"
    finally:
        browser.window.destroy()
        _restore(original)


def test_range_release_fires_change_script():
    browser, path, original, ttl, Element, Event = _range_browser()
    try:
        node, tab = _range_input(browser, ttl, Element)
        from feetbrowser.htmlparser import Text
        _drag_range(browser, tab, node, Event, 0.5)
        out = next(n for n in ttl(tab.nodes, [])
                   if isinstance(n, Element)
                   and n.attributes.get("id") == "out")
        text = "".join(t.text for t in out.children if isinstance(t, Text))
        assert text == node.attributes["value"], \
            f"the change handler saw {text!r}, value is {node.attributes['value']!r}"
    finally:
        browser.window.destroy()
        _restore(original)


def test_settings_page_range_drag_applies_the_setting():
    from feetbrowser.browser import Browser
    from feetbrowser.browser import tree_to_list
    from feetbrowser.htmlparser import Element
    from feetbrowser.window import Event

    path, original = _fresh_file()
    root = Tk(); root.withdraw()
    browser = Browser()
    try:
        browser.new_tab("about:settings")
        browser.draw()
        tab = browser.tabs[0]
        nodes = tree_to_list(tab.nodes, [])
        node = next(n for n in nodes
                    if isinstance(n, Element) and n.tag == "input"
                    and n.attributes.get("name") == "scroll_speed")
        _drag_range(browser, tab, node, Event, 0.75)
        eq(browser.settings["scroll_speed"], 130,
           "dragging the scroll speed range applies it")
        eq(browser.scroll_step(), 130, "the new speed drives scrolling")
    finally:
        browser.window.destroy()
        _restore(original)


def test_range_press_on_the_thumb_at_the_max_end_grabs_it():
    # The thumb rides the right edge of the track at max value: pressing it
    # must still grab the range, or the slider is undraggable where it sits
    # most of the time.
    browser, path, original, ttl, Element, Event = _range_browser()
    try:
        node, tab = _range_input(browser, ttl, Element)
        node.attributes["value"] = "160"
        tab.render()
        lx, ty, rx, by = tab._control_rect(node)
        ch = browser.chrome_height()
        browser._on_click(Event(x=int(rx), y=int(ty + 5) + ch))
        assert browser._range_grab is not None, \
            "pressing the thumb at the max end grabs the range"
        browser._on_drag(Event(x=int(lx + (rx - lx) * 0.5),
                               y=int(ty + 5) + ch))
        assert int(node.attributes["value"]) < 160, \
            "dragging left from the max end lowers the value"
        browser._on_release(Event(x=0, y=0))
    finally:
        browser.window.destroy()
        _restore(original)


def _range_span_text(browser, tab, node, ttl, Text):
    """The live readout span's current text for a range input."""
    span_id = f"out-{node.attributes['name']}"
    span = next(n for n in ttl(tab.nodes, [])
                if getattr(n, "attributes", {}).get("id") == span_id)
    return "".join(c.text for c in span.children if isinstance(c, Text))


def test_range_readout_updates_live_while_dragging():
    from feetbrowser.browser import Browser
    from feetbrowser.browser import tree_to_list
    from feetbrowser.htmlparser import Element, Text
    from feetbrowser.window import Event

    path, original = _fresh_file()
    root = Tk(); root.withdraw()
    browser = Browser()
    try:
        browser.new_tab("about:settings")
        browser.draw()
        tab = browser.tabs[0]
        nodes = tree_to_list(tab.nodes, [])
        node = next(n for n in nodes
                    if isinstance(n, Element) and n.tag == "input"
                    and n.attributes.get("name") == "scroll_speed")
        ch = browser.chrome_height()
        lx, ty, rx, by = tab._control_rect(node)
        px = int(lx + (rx - lx) * 0.75)
        py = int(ty + 5) + ch
        browser._on_click(Event(x=px, y=py))
        browser._on_drag(Event(x=px, y=py))
        browser._on_release(Event(x=px, y=py))
        eq(_range_span_text(browser, tab, node, tree_to_list, Text),
           "130 px", "the readout matches the committed scroll speed")
    finally:
        browser.window.destroy()
        _restore(original)


def test_range_readout_updates_mid_drag_before_release():
    from feetbrowser.browser import Browser
    from feetbrowser.browser import tree_to_list
    from feetbrowser.htmlparser import Element, Text
    from feetbrowser.window import Event

    path, original = _fresh_file()
    root = Tk(); root.withdraw()
    browser = Browser()
    try:
        browser.new_tab("about:settings")
        browser.draw()
        tab = browser.tabs[0]
        nodes = tree_to_list(tab.nodes, [])
        node = next(n for n in nodes
                    if isinstance(n, Element) and n.tag == "input"
                    and n.attributes.get("name") == "momentum_strength")
        ch = browser.chrome_height()
        lx, ty, rx, by = tab._control_rect(node)
        px = int(lx + (rx - lx) * 0.6)
        py = int(ty + 5) + ch
        browser._on_click(Event(x=px, y=py))
        browser._on_drag(Event(x=px, y=py))
        eq(_range_span_text(browser, tab, node, tree_to_list, Text),
           "60 %", "the readout tracks the thumb before release")
        browser._on_release(Event(x=px, y=py))
    finally:
        browser.window.destroy()
        _restore(original)


def test_range_drag_is_continuous_not_step_snapped():
    # The thumb follows the pointer continuously: dragging partway between
    # two step values must land on the intermediate value, not jump to a
    # step. Only the release (commit) snaps back onto the grid.
    browser, path, original, ttl, Element, Event = _range_browser()
    try:
        node, tab = _range_input(browser, ttl, Element)
        ch = browser.chrome_height()
        lx, ty, rx, by = tab._control_rect(node)
        px = int(lx + (rx - lx) * 0.875)  # 145 on min 40 max 160 step 10
        py = int(ty + 5) + ch
        browser._on_click(Event(x=px, y=py))
        browser._on_drag(Event(x=px, y=py))
        assert int(node.attributes["value"]) == 145, \
            f"mid-drag value should be continuous, got " \
            f"{node.attributes['value']!r}"
        browser._on_release(Event(x=px, y=py))
        assert int(node.attributes["value"]) in (140, 150), \
            f"committed value snaps to a step, got " \
            f"{node.attributes['value']!r}"
    finally:
        browser.window.destroy()
        _restore(original)


def test_range_press_glides_instead_of_teleporting():
    # Pressing on the track animates the thumb over a few frames rather than
    # snapping it to the press point: the value is unchanged immediately and
    # only arrives after the glide timers run.
    import time

    browser, path, original, ttl, Element, Event = _range_browser()
    try:
        node, tab = _range_input(browser, ttl, Element)
        ch = browser.chrome_height()
        lx, ty, rx, by = tab._control_rect(node)
        px = int(lx + (rx - lx) * 0.9)
        py = int(ty + 5) + ch
        browser._on_click(Event(x=px, y=py))
        assert int(node.attributes["value"]) == 80, \
            "pressing on the track does not instantly teleport the thumb"
        assert browser._range_glide is not None, \
            "pressing on the track arms a glide toward the press point"
        deadline = time.monotonic() + 1.0
        while browser._range_glide is not None and time.monotonic() < deadline:
            time.sleep(0.005)
            browser.window.flush_timers()
        assert browser._range_glide is None, "the glide finishes"
        eq(node.attributes["value"], "148", "the glide lands on the target")
        browser._on_release(Event(x=0, y=0))
    finally:
        browser.window.destroy()
        _restore(original)
    from feetbrowser.browser import Browser
    from feetbrowser.window import Event

    path, original = _fresh_file()
    root = Tk(); root.withdraw()
    browser = Browser()
    try:
        browser.settings["show_link_preview"] = False
        browser.new_tab("data:text/html,<a href='http://example.com'>x</a>")
        browser.draw()
        ch = browser.chrome_height()
        browser._on_motion(Event(x=5, y=ch + 5))
        eq(browser.active_tab.status, "",
           "hovering a link shows no status text when preview is off")
    finally:
        browser.window.destroy()
        _restore(original)


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
    print(f"\nALL {len(tests)} SETTINGS TESTS PASSED")


if __name__ == "__main__":
    main()