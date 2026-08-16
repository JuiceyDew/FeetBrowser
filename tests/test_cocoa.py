"""Tests for the macOS platform layer.

The rest of the suite runs headless, which means the one layer it cannot
reach is the one that translates real Cocoa events into Tk-shaped bindings --
and that is exactly where a typo costs you every mouse click in the browser
with nothing else looking wrong. So these tests open an actual NSWindow, post
actual NSEvents into the application queue, and let the real ``poll_events``
pick them up. Nothing is stubbed.

Skipped with a clear message on any platform that is not macOS.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skip(reason):
    print("SKIP test_cocoa.py: %s" % reason)
    sys.exit(0)


if sys.platform != "darwin":
    _skip("not macOS")

from feetbrowser import browser as browsermod  # noqa: E402
from feetbrowser import cocoa  # noqa: E402
from feetbrowser import window  # noqa: E402

if not cocoa.available():
    _skip("AppKit is not loadable here")


# -- synthetic input -------------------------------------------------------

def _window_number(win):
    return cocoa.msg(win._window, "windowNumber", restype=ctypes.c_long)


def make_mouse(win, kind, x, y, flags=0):
    """A real mouse NSEvent at canvas (top-left) coordinates."""
    _width, height = win._content_size()
    location = cocoa.NSPoint(float(x), float(height - y))
    event = cocoa.msg(
        cocoa._cls("NSEvent"),
        "mouseEventWithType:location:modifierFlags:timestamp:windowNumber:"
        "context:eventNumber:clickCount:pressure:",
        kind, location, flags, 0.0, _window_number(win), None, 0, 1, 1.0,
        argtypes=(ctypes.c_ulonglong, cocoa.NSPoint, ctypes.c_ulonglong,
                  ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
                  ctypes.c_long, ctypes.c_long, ctypes.c_float))
    assert event, "could not build a mouse NSEvent"
    return event


def send_mouse(win, kind, x, y, flags=0):
    """Hand a real mouse NSEvent straight to the translator.

    Mouse events do not survive the queue with their location intact: once the
    app is active, AppKit re-resolves a posted event's location against where
    the physical cursor happens to be, which is not something a test can pin
    down. Everything from ``_translate`` inward is the code under test anyway,
    so the events go in there. ``post_mouse`` covers the queue itself.
    """
    win._translate(make_mouse(win, kind, x, y, flags))


def post_mouse(win, kind, x, y, flags=0):
    """Queue a real mouse NSEvent, to be picked up by ``poll_events``.

    Keep well clear of the window's edges. ``[NSApp sendEvent:]`` is what
    makes the close button and the resize border work, so a press within a few
    pixels of the frame starts AppKit's drag-hysteresis loop -- which then
    waits for a real mouse the test does not have, and hangs.
    """
    cocoa.msg(win._app, "postEvent:atStart:", make_mouse(win, kind, x, y,
                                                         flags),
              True, argtypes=(ctypes.c_void_p, ctypes.c_bool))


def post_key(win, chars, keycode, flags=0):
    """Queue a real key-down NSEvent."""
    text = cocoa.nsstring(chars)
    event = cocoa.msg(
        cocoa._cls("NSEvent"),
        "keyEventWithType:location:modifierFlags:timestamp:windowNumber:"
        "context:characters:charactersIgnoringModifiers:isARepeat:keyCode:",
        cocoa._KEY_DOWN, cocoa.NSPoint(0.0, 0.0), flags, 0.0,
        _window_number(win), None, text, text, False, keycode,
        argtypes=(ctypes.c_ulonglong, cocoa.NSPoint, ctypes.c_ulonglong,
                  ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
                  ctypes.c_ushort))
    assert event, "could not build a key NSEvent"
    cocoa.msg(win._app, "postEvent:atStart:", event, True,
              argtypes=(ctypes.c_void_p, ctypes.c_bool))


def post_scroll(win, lines):
    """Queue a real scroll NSEvent.

    ``+mouseEventWithType:`` rejects the scroll-wheel type outright -- it
    raises an ObjC exception, which takes the process with it -- so a scroll
    has to come the way the window server makes one, through a CGEvent.
    """
    cg = cocoa._libs["cg"]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_int64]
    cg.CFRelease.argtypes = [ctypes.c_void_p]
    handle = cg.CGEventCreate(None)
    cg.CGEventSetType(handle, 22)              # kCGEventScrollWheel
    cg.CGEventSetIntegerValueField(handle, 11, lines)  # DeltaAxis1
    event = cocoa.msg(cocoa._cls("NSEvent"), "eventWithCGEvent:", handle,
                      argtypes=(ctypes.c_void_p,))
    assert event, "could not build a scroll NSEvent"
    cocoa.msg(win._app, "postEvent:atStart:", event, True,
              argtypes=(ctypes.c_void_p, ctypes.c_bool))
    cg.CFRelease(handle)


def pump(win, times=6):
    for _ in range(times):
        win.poll_events()


class _Session:
    """A live window, torn down however the test ends."""

    def __enter__(self):
        self.win = cocoa.CocoaTk(width=900, height=600, title="test")
        # Drain the events a window makes on its way up. Until they are gone
        # the app is still activating, and AppKit re-resolves the location of
        # the first event posted into it -- a real app has the same warm-up,
        # it just spends it in the run loop instead.
        pump(self.win, 8)
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        return False


class _Browser(_Session):
    """A live window driving a real Browser, on about:blank only."""

    def __enter__(self):
        win = super().__enter__()
        self.browser = browsermod.Browser(win)
        self.browser.new_tab("about:blank")
        self.browser.draw()
        win.present()
        return self.browser


# -- the runtime bridge ----------------------------------------------------

def test_window_opens_with_the_size_asked_for():
    with _Session() as win:
        assert win._content_size() == (900, 600), \
            "content size does not match the requested size"
        assert win.winfo_exists()


def test_a_quiet_window_does_not_take_the_keyboard():
    """The suite opens dozens of windows in a few seconds. Under QUIET each
    one must stay out of the way -- no Dock icon, and above all no stealing
    focus from whatever the user is typing into -- while still being a real
    window the rest of this file can post events at. Without this, the fix
    regresses the moment someone reinstates makeKeyAndOrderFront: and the
    only symptom is a machine nobody can use while the tests run."""
    if not window.QUIET:
        print("  ..  quiet-window check needs FEETBROWSER_QUIET=1")
        return
    with _Session() as win:
        assert not cocoa.msg(win._window, "isKeyWindow",
                             restype=ctypes.c_bool), \
            "a quiet window took the keyboard"
        policy = cocoa.msg(win._app, "activationPolicy",
                           restype=ctypes.c_long)
        assert policy == cocoa._ACTIVATION_ACCESSORY, \
            "a quiet run still asks for a Dock icon"
        # Still a real window, or the quiet is worthless.
        assert win.winfo_exists() and win._content_size() == (900, 600)


def test_struct_returning_selectors_use_the_right_abi():
    """objc_msgSend vs objc_msgSend_stret is chosen by CPU, and getting it
    wrong returns garbage rather than failing, so assert real numbers."""
    with _Session() as win:
        frame = cocoa.msg_rect(win._window, "frame")
        assert frame.size.width == 900.0, \
            "NSRect came back wrong: width=%r" % frame.size.width
        assert frame.size.height > 600.0, "frame should include the titlebar"
        bounds = cocoa.msg_rect(win._view, "bounds")
        assert (bounds.size.width, bounds.size.height) == (900.0, 600.0)


def test_pointer_returns_are_not_truncated():
    """A missing restype makes ctypes hand back a 32-bit int, which is how a
    valid object silently becomes a wild pointer -- the segfault this layer
    shipped with once. A pointer that survives a round trip through AppKit is
    proof the full 64 bits came back."""
    with _Session() as win:
        for name, handle in (("window", win._window), ("view", win._view),
                             ("app", win._app),
                             ("colorspace", win._colorspace),
                             ("distantPast", win._distant_past),
                             ("runMode", win._run_mode)):
            assert handle, "%s came back null" % name
        assert cocoa.msg(win._window, "contentView") == int(win._view), \
            "the window does not point back at the view we made"
        assert cocoa.msg(win._view, "window") == int(win._window), \
            "the view does not point back at its window"


def test_present_pushes_the_framebuffer():
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#123456")
        canvas.pack()
        win.present()
        image = cocoa.msg(win._view, "image")
        assert image, "the view has no image after present()"
        size = cocoa.msg(image, "size", restype=cocoa.NSSize)
        assert (size.width, size.height) == (900.0, 600.0), \
            "presented image is %rx%r" % (size.width, size.height)


def test_present_is_skipped_when_nothing_changed():
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=200, height=100)
        canvas.pack()
        win.present()
        first = cocoa.msg(win._view, "image")
        win.present()
        assert cocoa.msg(win._view, "image") == first, \
            "a clean canvas should not be re-uploaded"
        canvas.create_rectangle(0, 0, 10, 10, fill="red")
        win.present()
        assert cocoa.msg(win._view, "image") != first, \
            "a dirty canvas must be re-uploaded"


def test_the_frame_is_pushed_at_the_displays_own_resolution():
    """The HiDPI bug, asked of the machine actually running the test.

    A Retina display is two device pixels per point, so the framebuffer has
    to be twice the window in each direction while the NSImage carrying it
    stays the window's size in points -- that pairing is what makes AppKit
    draw one image pixel onto one device pixel. Declaring the image at its
    pixel count instead makes it a 1x image that gets stretched, which is
    exactly the softness this is here to prevent.

    On a 1x display every number below collapses to the same one, so this
    proves the arithmetic degrades correctly rather than proving sharpness.
    The override test underneath it is the one that runs the 2x path on any
    Mac, including a CI runner with no Retina display attached.
    """
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        scale = win._backing_scale()
        assert scale >= 1.0, "backingScaleFactor came back as %r" % scale
        assert win.scale == scale, \
            "the window did not adopt the display's scale"
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#123456")
        canvas.pack()
        win.present()
        device = (int(round(900 * scale)), int(round(600 * scale)))
        assert canvas.device_size() == device, \
            "buffer is %r, display wants %r" % (canvas.device_size(), device)
        assert len(win._buffers[-1]) == device[0] * device[1] * 3, \
            "the bytes handed to CoreGraphics are not the buffer's"
        size = cocoa.msg(cocoa.msg(win._view, "image"), "size",
                         restype=cocoa.NSSize)
        assert (size.width, size.height) == (900.0, 600.0), \
            "the NSImage is %rx%r, so it is not a %gx representation" \
            % (size.width, size.height, scale)


def test_an_overridden_scale_reaches_the_screen():
    """The same thing again with FEETBROWSER_SCALE forcing 2x, so the dense
    path is exercised on any Mac rather than only on a Retina one."""
    from feetbrowser import canvas as canvasmod
    saved = os.environ.get("FEETBROWSER_SCALE")
    os.environ["FEETBROWSER_SCALE"] = "2"
    try:
        with _Session() as win:
            assert win.scale == 2.0, "the override never reached the window"
            canvas = canvasmod.Canvas(win, width=900, height=600, bg="#654321")
            canvas.pack()
            win.present()
            assert canvas.device_size() == (1800, 1200), canvas.device_size()
            assert len(win._buffers[-1]) == 1800 * 1200 * 3
            size = cocoa.msg(cocoa.msg(win._view, "image"), "size",
                             restype=cocoa.NSSize)
            assert (size.width, size.height) == (900.0, 600.0), \
                "a 1800x1200 image declared as %rx%r points would be " \
                "shrunk, not drawn one to one" % (size.width, size.height)
            # A point is a CSS pixel on this platform, so a click is not
            # converted at all -- and must not be, or it would be halved.
            seen = []
            win.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
            send_mouse(win, cocoa._LEFT_DOWN, 400, 300)
            assert seen == [(400, 300)], \
                "a click moved when the buffer got denser: %r" % seen
    finally:
        if saved is None:
            os.environ.pop("FEETBROWSER_SCALE", None)
        else:
            os.environ["FEETBROWSER_SCALE"] = saved


def test_withdraw_is_not_mistaken_for_the_user_closing_the_window():
    with _Session() as win:
        win.withdraw()
        pump(win)
        assert win.winfo_exists(), \
            "an ordered-out window was treated as closed"
        win.deiconify()
        pump(win)
        assert win.winfo_exists()


# -- event translation -----------------------------------------------------

def test_mouse_coordinates_are_flipped_to_canvas_space():
    """Cocoa's origin is bottom-left and the canvas's is top-left. Getting
    this wrong puts every click in the wrong place."""
    with _Session() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
        for point in ((120, 40), (0, 0), (899, 599), (450, 300)):
            send_mouse(win, cocoa._LEFT_DOWN, *point)
        assert len(seen) == 4, "expected four clicks, got %r" % seen
        for got, want in zip(seen, ((120, 40), (0, 0), (899, 599),
                                    (450, 300))):
            assert got == want, "click landed at %r, expected %r" % (got, want)


def test_a_click_reaches_the_window_through_the_queue():
    """The other mouse tests skip the queue for the sake of exact
    coordinates; this one is here to prove the queue itself delivers."""
    with _Session() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append(e))
        post_mouse(win, cocoa._LEFT_DOWN, 300, 300)
        pump(win)
        assert seen, "no <Button-1> came out of poll_events()"
        assert seen[0].num == 1, "button number was lost"


def test_every_mouse_gesture_reaches_its_binding():
    with _Session() as win:
        seen = []
        for name in ("<Button-1>", "<ButtonRelease-1>", "<B1-Motion>",
                     "<Motion>", "<Button-3>", "<ButtonRelease-3>"):
            win.bind(name, lambda e, n=name: seen.append(n))
        send_mouse(win, cocoa._LEFT_DOWN, 200, 200)
        send_mouse(win, cocoa._LEFT_DRAGGED, 220, 220)
        send_mouse(win, cocoa._LEFT_UP, 220, 220)
        send_mouse(win, cocoa._MOUSE_MOVED, 240, 240)
        send_mouse(win, cocoa._RIGHT_DOWN, 240, 240)
        send_mouse(win, cocoa._RIGHT_UP, 240, 240)
        for name in ("<Button-1>", "<ButtonRelease-1>", "<B1-Motion>",
                     "<Motion>", "<Button-3>", "<ButtonRelease-3>"):
            assert name in seen, "%s never fired" % name


def test_command_and_control_both_arrive_as_tk_control():
    """The browser reads ``event.state & 0x4``, and a Mac user reaches for
    Command. Both have to land on the same bit."""
    with _Session() as win:
        seen = []
        win.bind("<Control-l>", lambda e: seen.append(e.state))
        post_key(win, "l", 37, cocoa._MOD_COMMAND)
        post_key(win, "l", 37, cocoa._MOD_CONTROL)
        pump(win)
        assert len(seen) == 2, "expected both Cmd-L and Ctrl-L, got %d" % \
            len(seen)
        assert all(state & 0x4 for state in seen)


def test_named_keys_beat_the_generic_key_binding():
    """Tk fires only the most specific binding. A browser that binds both
    <Up> and <Key> must not see one keypress twice."""
    with _Session() as win:
        hits = []
        win.bind("<Up>", lambda e: hits.append("Up"))
        win.bind("<Key>", lambda e: hits.append("Key"))
        post_key(win, "", 126)
        pump(win)
        assert hits == ["Up"], "expected only <Up>, got %r" % hits
        post_key(win, "q", 12)
        pump(win)
        assert hits == ["Up", "Key"], "a plain letter should fall to <Key>"


def test_printable_keys_carry_their_character():
    with _Session() as win:
        seen = []
        win.bind("<Key>", lambda e: seen.append((e.keysym, e.char)))
        post_key(win, "z", 6)
        post_key(win, " ", 49)
        pump(win)
        assert ("z", "z") in seen, "letter key lost its char: %r" % seen
        assert any(keysym == "space" for keysym, _c in seen), \
            "space should have keysym 'space': %r" % seen


def test_shift_tab_becomes_iso_left_tab():
    """browser.py binds <Control-ISO_Left_Tab> for previous-tab, which is the
    keysym X11 and Tk use for a shifted Tab."""
    with _Session() as win:
        seen = []
        win.bind("<Control-ISO_Left_Tab>", lambda e: seen.append(1))
        post_key(win, "\t", 48, cocoa._MOD_COMMAND | cocoa._MOD_SHIFT)
        pump(win)
        assert seen, "shifted Tab did not reach <Control-ISO_Left_Tab>"


def test_wheel_keeps_its_sign_and_stays_in_the_pixel_range():
    """browser.py treats |delta| < 30 as a pixel count and anything larger as
    line units, so a scroll has to stay under 30 or it moves the page by a
    screenful per notch."""
    with _Session() as win:
        seen = []
        win.bind("<MouseWheel>", lambda e: seen.append(e.delta))
        post_scroll(win, 3)
        pump(win)
        post_scroll(win, -3)
        pump(win)
        assert len(seen) == 2, "expected two wheel events, got %r" % seen
        assert seen[0] > 0 > seen[1], "wheel direction was lost: %r" % seen
        for delta in seen:
            assert abs(delta) < 30, \
                "wheel delta %r escapes the pixel path" % delta


def test_handler_exceptions_do_not_stop_the_loop():
    with _Session() as win:
        errors = []
        win.on_callback_error = lambda where, exc: errors.append(where)
        win.bind("<Button-1>", lambda e: 1 // 0)
        post_mouse(win, cocoa._LEFT_DOWN, 300, 300)
        pump(win)
        assert errors, "a raising handler was not reported"
        assert win.winfo_exists(), "one bad handler took down the window"


def test_clipboard_round_trips_through_nspasteboard():
    with _Session() as win:
        win.clipboard_clear()
        win.clipboard_append("feetbrowser clipboard probe")
        assert win.clipboard_get() == "feetbrowser clipboard probe"


def test_title_reaches_the_real_window():
    with _Session() as win:
        win.title("a new title")
        assert cocoa.from_nsstring(
            cocoa.msg(win._window, "title")) == "a new title"


def test_toplevel_events_route_to_the_right_window():
    """One event queue per application, so the root's loop has to hand a
    popup's events to the popup -- that is how PopupWindow has always run."""
    with _Session() as root:
        popup = cocoa.CocoaToplevel(root, width=400, height=300)
        try:
            assert popup in root.children
            hits = []
            root.bind("<Button-1>", lambda e: hits.append("root"))
            popup.bind("<Button-1>", lambda e: hits.append("popup"))
            pump(popup, 8)  # let the popup finish coming up, as above
            post_mouse(popup, cocoa._LEFT_DOWN, 150, 150)
            pump(root)
            assert hits == ["popup"], \
                "popup events went to %r" % (hits or "nobody")
        finally:
            popup.destroy()
        assert popup not in root.children


# -- the browser, driven for real ------------------------------------------

def test_clicking_the_new_tab_button_opens_a_tab():
    """The regression that started this file: a stray attribute error in the
    mouse path swallowed every click, and nothing else looked wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = br._new_tab_x() + browsermod.NEW_TAB_W / 2
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = browsermod.toes.band_height(br.chrome_bands()) + 20
        send_mouse(br.window, cocoa._LEFT_DOWN, x, y)
        send_mouse(br.window, cocoa._LEFT_UP, x, y)
        assert len(br.tabs) == before + 1, \
            "clicking + did not open a tab (%d -> %d)" % (before,
                                                          len(br.tabs))


def test_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        post_key(br.window, "t", 17, cocoa._MOD_COMMAND)
        pump(br.window)
        assert len(br.tabs) == before + 1, "Cmd-T did not open a tab"
        post_key(br.window, "l", 37, cocoa._MOD_COMMAND)
        pump(br.window)
        assert br.focus == "address", "Cmd-L did not focus the address bar"


def test_typing_into_the_address_bar_works():
    with _Browser() as br:
        post_key(br.window, "l", 37, cocoa._MOD_COMMAND)
        pump(br.window)
        for ch, code in (("a", 0), ("b", 11), ("c", 8)):
            post_key(br.window, ch, code)
            pump(br.window, 2)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def test_a_frame_is_presented_after_interaction():
    with _Browser() as br:
        post_key(br.window, "t", 17, cocoa._MOD_COMMAND)
        pump(br.window)
        br.draw()
        br.window.present()
        assert cocoa.msg(br.window._view, "image"), \
            "nothing was presented after a tab opened"


def _tall_page(br):
    """Load a page far taller than the window and return its tab."""
    br.new_tab("data:text/html," + "".join("<p>line %d</p>" % i
                                           for i in range(300)))
    br.draw()
    tab = br.active_tab
    assert tab.content_height() > br.tab_height(), "the page is not tall"
    return tab


def test_dragging_the_scrollbar_scrolls_the_page():
    """AppKit's own three events -- mouseDown, mouseDragged, mouseUp -- are
    what the scrollbar is dragged with, and mouseDragged is the one nothing
    used to be listening for on the bar."""
    with _Browser() as br:
        tab = _tall_page(br)
        # An unscrolled page puts the thumb at the very top of the track.
        thumb_top = br.chrome_height()
        x = br.canvas.winfo_width() - 7
        send_mouse(br.window, cocoa._LEFT_DOWN, x, thumb_top + 5)
        assert tab.scroll == 0, "pressing the thumb jumped the page"
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, thumb_top + 105)
        assert tab.scroll > 0, "mouseDragged on the thumb did not scroll"
        send_mouse(br.window, cocoa._LEFT_UP, x, thumb_top + 105)
        settled = tab.scroll
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, thumb_top + 300)
        assert tab.scroll == settled, "the drag survived mouseUp"


def test_a_drag_that_leaves_the_window_still_scrolls():
    """AppKit keeps sending the drag to the window the press went to, so the
    coordinates run off the top and bottom of the window -- and dragging the
    bar past the end of the document has to stop where the wheel stops."""
    with _Browser() as br:
        tab = _tall_page(br)
        tab.scroll_by(10 ** 9)
        bottom = tab.scroll
        tab.set_scroll(0)
        br.draw()
        thumb_top = br.chrome_height()
        x = br.canvas.winfo_width() - 7
        send_mouse(br.window, cocoa._LEFT_DOWN, x, thumb_top + 5)
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, br.window.height + 4000)
        assert tab.scroll == bottom, \
            "dragged off the bottom to %r, the wheel stops at %r" % (tab.scroll,
                                                                    bottom)
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, -4000)
        assert tab.scroll == 0, "dragged off the top to %r" % tab.scroll
        send_mouse(br.window, cocoa._LEFT_UP, x, -4000)


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
    print(f"\nALL {len(tests)} COCOA TESTS PASSED")


if __name__ == "__main__":
    main()
