"""Tests for the Windows platform layer.

The rest of the suite runs headless, which means the one layer it cannot
reach is the one that turns real window messages into Tk-shaped bindings --
and that is exactly where a typo costs you every mouse click in the browser
with nothing else looking wrong. So these tests create an actual window,
send and post actual messages through the real window procedure, and blit
through real GDI. Nothing is stubbed.

The arithmetic behind all of this -- DIB strides, the RGB-to-BGR conversion,
the keysym tables -- is in plain functions tested from tests/test_units.py on
every platform. What is left here is the part that genuinely needs Windows.

Skipped with a clear message anywhere else.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _skip(reason):
    print("SKIP test_win32.py: %s" % reason)
    sys.exit(0)


if sys.platform != "win32":
    _skip("not Windows")

from feetbrowser import browser as browsermod  # noqa: E402
from feetbrowser import win32  # noqa: E402

if not win32.available():
    _skip("no Win32 window station here")


# -- synthetic input -------------------------------------------------------
#
# A few signatures the backend itself never needs, declared the same way it
# declares its own: a missing restype truncates a handle to 32 bits.

def _extra(lib, name, restype, argtypes):
    fn = getattr(win32._libs[lib], name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


HANDLE, LONG, UINT = win32.HANDLE, win32.LONG, win32.UINT
_send = _extra("user32", "SendMessageW", win32.LRESULT,
               [HANDLE, UINT, win32.WPARAM, win32.LPARAM])
_post = _extra("user32", "PostMessageW", win32.BOOL,
               [HANDLE, UINT, win32.WPARAM, win32.LPARAM])
_get_keyboard_state = _extra("user32", "GetKeyboardState", win32.BOOL,
                             [ctypes.c_void_p])
_set_keyboard_state = _extra("user32", "SetKeyboardState", win32.BOOL,
                             [ctypes.c_void_p])
_client_to_screen = _extra("user32", "ClientToScreen", win32.BOOL,
                           [HANDLE, ctypes.POINTER(win32.POINT)])
_create_dc = _extra("gdi32", "CreateCompatibleDC", HANDLE, [HANDLE])
_create_bitmap = _extra("gdi32", "CreateCompatibleBitmap", HANDLE,
                        [HANDLE, ctypes.c_int, ctypes.c_int])
_select = _extra("gdi32", "SelectObject", HANDLE, [HANDLE, HANDLE])
_delete_object = _extra("gdi32", "DeleteObject", win32.BOOL, [HANDLE])
_delete_dc = _extra("gdi32", "DeleteDC", win32.BOOL, [HANDLE])
_get_pixel = _extra("gdi32", "GetPixel", win32.DWORD,
                    [HANDLE, ctypes.c_int, ctypes.c_int])


def pack(x, y):
    """Two coordinates in one lParam, the way Windows packs them."""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def send(win, message, wparam=0, lparam=0):
    """Deliver a message straight to the window procedure.

    This is what SendMessageW does for a window on the calling thread, so the
    real procedure and the real translation run; only the queue is skipped.
    """
    return _send(win._hwnd, message, wparam, lparam)


def post(win, message, wparam=0, lparam=0):
    """Queue a message, to be picked up by poll_events()."""
    assert _post(win._hwnd, message, wparam, lparam), "PostMessageW failed"


def pump(win, times=4):
    for _ in range(times):
        win.poll_events()


class holding:
    """Hold modifier keys down for real, as far as GetKeyState can tell.

    The backend reads modifiers from the keyboard rather than from the
    message, which is the right thing to do -- it survives the window losing
    focus with a key held -- but it means a synthetic Ctrl-L needs Control
    genuinely down. SetKeyboardState writes the calling thread's own key
    state table, which is the table GetKeyState reads, and touches no other
    process.
    """

    def __init__(self, *vks):
        self.vks = vks

    def __enter__(self):
        self.saved = (ctypes.c_ubyte * 256)()
        _get_keyboard_state(ctypes.byref(self.saved))
        state = (ctypes.c_ubyte * 256)()
        ctypes.memmove(state, self.saved, 256)
        for vk in self.vks:
            state[vk] = 0x80
        _set_keyboard_state(ctypes.byref(state))
        return self

    def __exit__(self, *_exc):
        _set_keyboard_state(ctypes.byref(self.saved))
        return False


class _Session:
    """A live window, torn down however the test ends."""

    def __enter__(self):
        self.win = win32.Win32Tk(width=900, height=600, title="test")
        # Drain what a window makes on its way up: ShowWindow delivers
        # WM_SIZE and WM_PAINT before it returns, and the activation
        # messages follow through the queue.
        pump(self.win, 6)
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        pump(self.win, 2)
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


class _MemoryTarget:
    """A bitmap in memory to blit into.

    Reading pixels back from the window itself would depend on the window
    being visible and unoccluded, which on a build agent it is not. A memory
    DC goes through the same StretchDIBits with the same BITMAPINFO, so it
    proves the same things and proves them the same way every time.
    """

    def __init__(self, win, width, height):
        self.win = win
        self.width, self.height = width, height

    def __enter__(self):
        user32 = win32._libs["user32"]
        self.screen = user32.GetDC(self.win._hwnd)
        self.hdc = _create_dc(self.screen)
        self.bitmap = _create_bitmap(self.screen, self.width, self.height)
        assert self.hdc and self.bitmap, "no memory bitmap"
        self.old = _select(self.hdc, self.bitmap)
        return self

    def pixel(self, x, y):
        """(r, g, b) at a point. GetPixel hands back 0x00bbggrr."""
        value = _get_pixel(self.hdc, x, y)
        assert value != 0xFFFFFFFF, "no pixel at (%d, %d)" % (x, y)
        return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF

    def __exit__(self, *_exc):
        _select(self.hdc, self.old)
        _delete_object(self.bitmap)
        _delete_dc(self.hdc)
        win32._libs["user32"].ReleaseDC(self.win._hwnd, self.screen)
        return False


# -- the window itself -----------------------------------------------------

def test_window_opens_with_the_client_size_asked_for():
    """CreateWindowExW takes the *outer* size, so the title bar and borders
    have to be added first or every window is short by their height."""
    with _Session() as win:
        assert win._content_size() == (900, 600), \
            "client area is %r, not the size asked for" % (win._content_size(),)
        assert win.winfo_exists()
        assert win32._libs["user32"].IsWindow(win._hwnd)


def test_handles_are_not_truncated():
    """ctypes defaults a return type to c_int, which lops the top half off a
    64-bit HWND and hands back a handle that belongs to nobody. A handle that
    survives a round trip through Windows is proof the full width came back."""
    with _Session() as win:
        assert win._hwnd, "CreateWindowExW returned null"
        buffer = ctypes.create_unicode_buffer(64)
        win32._libs["user32"].GetWindowTextW(win._hwnd, buffer, 64)
        assert buffer.value == "test", \
            "the handle does not name our window: %r" % buffer.value
        assert win32._WINDOWS[win32._handle_key(win._hwnd)] is win


def test_the_window_is_dpi_aware():
    """Without this Windows draws at 96 DPI and lets the compositor scale the
    result up, which is a blurry browser on any modern laptop panel."""
    user32 = win32._libs["user32"]
    getter = getattr(user32, "GetThreadDpiAwarenessContext", None)
    if getter is None:      # pragma: no cover - pre-1607
        return
    getter.restype = ctypes.c_void_p
    reader = user32.GetAwarenessFromDpiAwarenessContext
    reader.restype = ctypes.c_int
    reader.argtypes = [ctypes.c_void_p]
    with _Session():
        awareness = reader(getter())
        assert awareness > 0, \
            "the process is DPI-unaware; the window will be scaled and blurry"


def test_present_puts_the_right_colour_in_the_right_corner():
    """Everything that can go wrong in a DIB goes wrong quietly: red and blue
    swap, or the rows come out upside down. Read the pixels back."""
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#123456")
        canvas.pack()
        canvas.create_rectangle(0, 0, 100, 100, fill="#ff0000")
        canvas.create_rectangle(0, 500, 100, 600, fill="#00ff00")
        win.present()
        with _MemoryTarget(win, 900, 600) as target:
            win._blit(target.hdc)
            assert target.pixel(500, 300) == (0x12, 0x34, 0x56), \
                "background came back %r; red and blue may be swapped" % \
                (target.pixel(500, 300),)
            assert target.pixel(50, 50) == (0xFF, 0, 0), \
                "the top-left square is %r" % (target.pixel(50, 50),)
            assert target.pixel(50, 550) == (0, 0xFF, 0), \
                "the bottom-left square is %r -- the image is upside down" % \
                (target.pixel(50, 550),)


def test_the_bitmap_header_describes_a_top_down_frame():
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#ffffff")
        canvas.pack()
        win.present()
        header = win._bitmap.bmiHeader
        assert header.biSize == 40, "biSize is %d" % header.biSize
        assert header.biWidth == 900
        assert header.biHeight == -600, \
            "biHeight is %d; a positive height is bottom-up" % header.biHeight
        assert header.biBitCount == 32
        assert len(win._frame) == 900 * 600 * 4


def test_present_is_skipped_when_nothing_changed():
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=200, height=100)
        canvas.pack()
        win.present()
        first = win._frame
        win.present()
        assert win._frame is first, "a clean canvas was converted again"
        canvas.create_rectangle(0, 0, 10, 10, fill="red")
        win.present()
        assert win._frame is not first, "a dirty canvas must be reconverted"


def test_a_paint_message_repaints_the_last_frame():
    """WM_PAINT arrives while the user is dragging a window edge, when the
    main loop is not running at all, so it has to blit what it already has."""
    from feetbrowser import canvas as canvasmod
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#204060")
        canvas.pack()
        win.present()
        win32._libs["user32"].InvalidateRect(win._hwnd, None, False)
        pump(win)
        assert win._frame is not None, "the frame was lost across a repaint"


def test_a_resize_message_resizes_the_page():
    with _Session() as win:
        seen = []
        win.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
        send(win, win32.WM_SIZE, 0, pack(640, 480))
        assert (win.width, win.height) == (640, 480), \
            "the window still thinks it is %rx%r" % (win.width, win.height)
        assert seen and seen[-1] == (640, 480), \
            "no <Configure> for the new size: %r" % seen


def test_a_minimum_size_reaches_the_resize_drag():
    with _Session() as win:
        win.minsize(400, 300)
        info = win32.MINMAXINFO()
        send(win, win32.WM_GETMINMAXINFO, 0,
             ctypes.cast(ctypes.byref(info), ctypes.c_void_p).value)
        assert info.ptMinTrackSize.x >= 400, \
            "minimum width came back %d" % info.ptMinTrackSize.x
        assert info.ptMinTrackSize.y > 300, \
            "the minimum height should include the title bar, got %d" \
            % info.ptMinTrackSize.y


def test_closing_the_window_is_not_mistaken_for_anything_else():
    win = win32.Win32Tk(width=400, height=300, title="closing")
    pump(win, 4)
    hwnd = win._hwnd
    closed = []
    win.protocol("WM_DELETE_WINDOW", lambda: closed.append(1))
    send(win, win32.WM_CLOSE)
    pump(win, 4)
    assert closed, "the close button did not reach WM_DELETE_WINDOW"
    assert not win.winfo_exists(), "the window survived WM_CLOSE"
    assert win32._handle_key(hwnd) not in win32._WINDOWS, \
        "a destroyed window is still in the registry"
    # Everything below is a no-op on a dead window rather than a crash: the
    # main loop gets one more turn after the user clicks the close button.
    win.present()
    win.poll_events()
    win.destroy()


def test_withdraw_is_not_mistaken_for_the_user_closing_the_window():
    with _Session() as win:
        win.withdraw()
        pump(win)
        assert win.winfo_exists(), "a hidden window was treated as closed"
        win.deiconify()
        pump(win)
        assert win.winfo_exists()


# -- dense displays --------------------------------------------------------
#
# A build agent runs at 96 DPI with no monitor attached, so the interesting
# path -- the one every laptop panel takes -- would never be exercised here.
# FEETBROWSER_SCALE forces it: the window is still created and messaged for
# real, and every physical pixel below is a pixel Windows agrees exists.

class _DenseSession(_Session):
    """A live window on a display that claims twice the density.

    Deliberately a small page: the window Windows creates is twice the size
    in every direction, and an overlapped window is clamped to the desktop
    on the way up. A build agent's desktop is 1024x768, so 900 CSS pixels
    would come back as 514 and the test would be measuring the clamp.
    """

    css = (320, 240)

    def __enter__(self):
        self.saved = os.environ.get("FEETBROWSER_SCALE")
        os.environ["FEETBROWSER_SCALE"] = "2"
        try:
            self.win = win32.Win32Tk(width=self.css[0], height=self.css[1],
                                     title="dense")
            pump(self.win, 6)
            return self.win
        except BaseException:
            self._restore()
            raise

    def _restore(self):
        if self.saved is None:
            os.environ.pop("FEETBROWSER_SCALE", None)
        else:
            os.environ["FEETBROWSER_SCALE"] = self.saved

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._restore()


def test_a_dense_display_gets_a_window_of_device_pixels():
    """The client area is asked for in physical pixels, so a 320-CSS-pixel
    page at 2x needs Windows to hand back 640 of them."""
    from feetbrowser import canvas as canvasmod
    with _DenseSession() as win:
        assert win.scale == 2.0, "scale came back %r" % (win.scale,)
        assert win._content_size() == (640, 480), \
            "client area is %r physical pixels" % (win._content_size(),)
        assert (win.width, win.height) == (320, 240), \
            "the page is %rx%r CSS pixels" % (win.width, win.height)
        canvas = canvasmod.Canvas(win, width=320, height=240, bg="#ffffff")
        canvas.pack()
        assert canvas.device_size() == (640, 480), \
            "the buffer is %r" % (canvas.device_size(),)
        win.present()
        assert win._frame_dims == (640, 480)
        assert len(win._frame) == 640 * 480 * 4
        assert win._bitmap.bmiHeader.biWidth == 640
        assert win._bitmap.bmiHeader.biHeight == -480


def test_a_dense_frame_lands_one_pixel_for_one():
    """The proof that nothing is stretched: a rectangle whose CSS edge is at
    10 has to have its physical edge at exactly 20, with the pixel on either
    side of it the colour that belongs there."""
    from feetbrowser import canvas as canvasmod
    with _DenseSession() as win:
        canvas = canvasmod.Canvas(win, width=320, height=240, bg="#123456")
        canvas.pack()
        canvas.create_rectangle(10, 10, 20, 20, fill="#ff0000")
        win.present()
        with _MemoryTarget(win, 640, 480) as target:
            win._blit(target.hdc)
            # Where the frame came from and where it went, in the message:
            # a bitmap that stayed black means the blit never landed, which
            # is a different bug from one that landed in the wrong place.
            where = "frame %r into a client area of %r" % \
                (win._frame_dims, win._content_size())
            assert target.pixel(300, 200) == (0x12, 0x34, 0x56), \
                "the background is %r -- %s" % \
                (target.pixel(300, 200), where)
            assert target.pixel(21, 21) == (0xFF, 0, 0), \
                "physical 21,21 is %r, inside the square -- %s" % \
                (target.pixel(21, 21), where)
            assert target.pixel(39, 39) == (0xFF, 0, 0), \
                "physical 39,39 is %r; the square stopped short -- %s" % \
                (target.pixel(39, 39), where)
            assert target.pixel(41, 41) == (0x12, 0x34, 0x56), \
                "physical 41,41 is %r; the square overran -- %s" % \
                (target.pixel(41, 41), where)


def test_a_dense_resize_keeps_the_buffer_the_size_of_the_window():
    from feetbrowser import canvas as canvasmod
    with _DenseSession() as win:
        canvas = canvasmod.Canvas(win, width=320, height=240, bg="#ffffff")
        canvas.pack()
        seen = []
        win.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
        send(win, win32.WM_SIZE, 0, pack(500, 300))
        assert (win.width, win.height) == (250, 150), \
            "the page thinks it is %rx%r" % (win.width, win.height)
        assert canvas.device_size() == (500, 300), \
            "the buffer is %r, not the client area" % (canvas.device_size(),)
        assert seen and seen[-1] == (250, 150), \
            "<Configure> reported %r" % seen


def test_a_dense_click_lands_on_the_css_pixel_under_it():
    """WM_LBUTTONDOWN carries physical pixels. A page that laid itself out in
    CSS pixels has to be told where the click landed in *those*, or every hit
    test in the browser is out by the scale factor."""
    with _DenseSession() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
        for x, y in ((0, 0), (1, 1), (240, 80), (639, 479)):
            send(win, win32.WM_LBUTTONDOWN, 0, pack(x, y))
        assert seen == [(0, 0), (0, 0), (120, 40), (319, 239)], \
            "clicks landed at %r" % (seen,)


def test_a_dpi_change_resizes_the_buffer_to_match():
    """Dragging the window to a second monitor: the DPI in wParam is adopted
    before the move, so the WM_SIZE that SetWindowPos sends back converts the
    new client size with the new scale rather than the old one."""
    from feetbrowser import canvas as canvasmod
    get_window_rect = _extra("user32", "GetWindowRect", win32.BOOL,
                             [HANDLE, ctypes.POINTER(win32.RECT)])
    with _Session() as win:
        canvas = canvasmod.Canvas(win, width=900, height=600, bg="#ffffff")
        canvas.pack()
        win.present()
        before = win.scale
        rect = win32.RECT()
        assert get_window_rect(win._hwnd, ctypes.byref(rect))
        # What Windows itself would suggest for twice the density: the same
        # top-left corner, twice the size.
        rect.right = rect.left + 2 * (rect.right - rect.left)
        rect.bottom = rect.top + 2 * (rect.bottom - rect.top)
        send(win, win32.WM_DPICHANGED, (192 << 16) | 192,
             ctypes.cast(ctypes.byref(rect), ctypes.c_void_p).value)
        assert win.scale == 2.0, "scale came back %r" % (win.scale,)
        assert canvas.device_size() == win._content_size(), \
            "the buffer is %r for a client area of %r" % \
            (canvas.device_size(), win._content_size())
        # Only if the scale actually moved: an agent already running at 192
        # DPI would have drawn the first frame correctly to begin with.
        assert before == 2.0 or canvas.dirty, \
            "the frame was left as it was drawn for the old density"


# -- event translation -----------------------------------------------------

def test_mouse_coordinates_arrive_in_canvas_space():
    with _Session() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
        for x, y in ((120, 40), (0, 0), (899, 599), (450, 300)):
            send(win, win32.WM_LBUTTONDOWN, 0, pack(x, y))
        assert seen == [(120, 40), (0, 0), (899, 599), (450, 300)], \
            "clicks landed at %r" % (seen,)


def test_a_drag_off_the_top_left_reports_negative_coordinates():
    """The mouse is captured for the length of a drag, so a selection that
    leaves the window keeps reporting -- with coordinates that go negative,
    packed as unsigned 16-bit fields."""
    with _Session() as win:
        seen = []
        win.bind("<B1-Motion>", lambda e: seen.append((e.x, e.y)))
        send(win, win32.WM_LBUTTONDOWN, win32.MK_LBUTTON, pack(20, 20))
        send(win, win32.WM_MOUSEMOVE, win32.MK_LBUTTON, pack(-10, -5))
        send(win, win32.WM_LBUTTONUP, 0, pack(-10, -5))
        assert seen == [(-10, -5)], "drag reported %r" % (seen,)


def test_a_click_reaches_the_window_through_the_queue():
    """The other mouse tests go straight to the procedure; this one proves
    the PeekMessage pump delivers at all."""
    with _Session() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append(e))
        post(win, win32.WM_LBUTTONDOWN, 0, pack(300, 300))
        pump(win)
        assert seen, "no <Button-1> came out of poll_events()"
        assert seen[0].num == 1 and (seen[0].x, seen[0].y) == (300, 300)


def test_every_mouse_gesture_reaches_its_binding():
    with _Session() as win:
        seen = []
        names = ("<Button-1>", "<ButtonRelease-1>", "<B1-Motion>", "<Motion>",
                 "<Button-2>", "<Button-3>", "<ButtonRelease-3>")
        for name in names:
            win.bind(name, lambda e, n=name: seen.append(n))
        send(win, win32.WM_LBUTTONDOWN, win32.MK_LBUTTON, pack(200, 200))
        send(win, win32.WM_MOUSEMOVE, win32.MK_LBUTTON, pack(220, 220))
        send(win, win32.WM_LBUTTONUP, 0, pack(220, 220))
        send(win, win32.WM_MOUSEMOVE, 0, pack(240, 240))
        send(win, win32.WM_MBUTTONDOWN, win32.MK_MBUTTON, pack(240, 240))
        send(win, win32.WM_MBUTTONUP, 0, pack(240, 240))
        send(win, win32.WM_RBUTTONDOWN, win32.MK_RBUTTON, pack(240, 240))
        send(win, win32.WM_RBUTTONUP, 0, pack(240, 240))
        for name in names:
            assert name in seen, "%s never fired" % name


def test_the_wheel_is_converted_out_of_screen_coordinates():
    """A wheel message is the one mouse message that carries screen
    coordinates. Forgetting that scrolls whatever is under the wrong point."""
    with _Session() as win:
        seen = []
        win.bind("<MouseWheel>", lambda e: seen.append(e))
        point = win32.POINT(400, 300)
        _client_to_screen(win._hwnd, ctypes.byref(point))
        for notches in (1, -1):
            send(win, win32.WM_MOUSEWHEEL,
                 (notches * win32.WHEEL_DELTA & 0xFFFF) << 16,
                 pack(point.x, point.y))
        assert len(seen) == 2, "expected two wheel events, got %r" % seen
        assert (seen[0].x, seen[0].y) == (400, 300), \
            "wheel landed at (%r, %r), not the point under the cursor" % \
            (seen[0].x, seen[0].y)
        assert seen[0].delta > 0 > seen[1].delta, "wheel direction was lost"
        for event in seen:
            assert abs(event.delta) < 30, \
                "wheel delta %r escapes the pixel path" % event.delta


def test_control_shortcuts_reach_their_bindings():
    """Under Control, WM_CHAR carries a control code (Ctrl-L is 0x0C), so the
    letter has to come from the virtual key instead."""
    with _Session() as win:
        seen = []
        win.bind("<Control-l>", lambda e: seen.append(e.state))
        with holding(win32.VK_CONTROL):
            send(win, win32.WM_KEYDOWN, 0x4C, 0)
            send(win, win32.WM_CHAR, 0x0C, 0)   # what TranslateMessage makes
        assert len(seen) == 1, \
            "expected exactly one <Control-l>, got %d" % len(seen)
        assert seen[0] & 0x4, "the Control bit never reached event.state"


def test_view_source_needs_the_shifted_spelling():
    """browser.py binds <Control-Shift-s>, and the keysym for that keypress
    is 'S'. Both spellings have to be offered or view-source is unreachable."""
    with _Session() as win:
        seen = []
        win.bind("<Control-Shift-s>", lambda e: seen.append(e.keysym))
        with holding(win32.VK_CONTROL, win32.VK_SHIFT):
            send(win, win32.WM_KEYDOWN, 0x53, 0)
        assert seen == ["S"], "Ctrl-Shift-S produced %r" % (seen,)


def test_shift_tab_becomes_iso_left_tab():
    with _Session() as win:
        seen = []
        win.bind("<Control-ISO_Left_Tab>", lambda e: seen.append(1))
        with holding(win32.VK_CONTROL, win32.VK_SHIFT):
            send(win, win32.WM_KEYDOWN, 0x09, 0)
        assert seen, "shifted Tab did not reach <Control-ISO_Left_Tab>"


def test_named_keys_beat_the_generic_key_binding():
    """Tk fires only the most specific binding. A browser that binds both
    <Up> and <Key> must not see one keypress twice."""
    with _Session() as win:
        hits = []
        win.bind("<Up>", lambda e: hits.append("Up"))
        win.bind("<Key>", lambda e: hits.append("Key"))
        send(win, win32.WM_KEYDOWN, 0x26, 0)
        assert hits == ["Up"], "expected only <Up>, got %r" % hits
        send(win, win32.WM_KEYDOWN, 0x51, 0)    # a plain Q carries no keysym
        send(win, win32.WM_CHAR, ord("q"), 0)
        assert hits == ["Up", "Key"], "a plain letter should fall to <Key>"


def test_printable_keys_carry_their_character():
    with _Session() as win:
        seen = []
        win.bind("<Key>", lambda e: seen.append((e.keysym, e.char)))
        for text in "z ":
            post(win, win32.WM_CHAR, ord(text), 0)
        pump(win)
        assert ("z", "z") in seen, "letter key lost its char: %r" % seen
        assert ("space", " ") in seen, \
            "space should have keysym 'space': %r" % seen


def test_a_character_outside_the_basic_plane_survives_two_messages():
    """WM_CHAR carries one UTF-16 code unit, so an emoji arrives as a
    surrogate pair and the first half means nothing on its own."""
    with _Session() as win:
        seen = []
        win.bind("<Key>", lambda e: seen.append(e.char))
        for unit in (0xD83D, 0xDC10):   # U+1F410, as UTF-16
            send(win, win32.WM_CHAR, unit, 0)
        assert seen == ["\U0001F410"], \
            "the pair came out as %r" % (seen,)


def test_the_cursor_is_ours_only_inside_the_client_area():
    """Windows asks on every mouse move. Answering for the frame as well
    would take over the resize borders."""
    with _Session() as win:
        assert send(win, win32.WM_SETCURSOR, win._hwnd,
                    win32.HTCLIENT) == 1, "the client cursor was not set"
        # HTCAPTION: not ours, and DefWindowProc must get it.
        assert send(win, win32.WM_SETCURSOR, win._hwnd, 2) == 0, \
            "the title bar cursor was taken over"


def test_handler_exceptions_do_not_stop_the_loop():
    with _Session() as win:
        errors = []
        win.on_callback_error = lambda where, exc: errors.append(where)
        win.bind("<Button-1>", lambda e: 1 // 0)
        post(win, win32.WM_LBUTTONDOWN, 0, pack(300, 300))
        pump(win)
        assert errors, "a raising handler was not reported"
        assert win.winfo_exists(), "one bad handler took down the window"


def test_clipboard_round_trips_through_the_windows_clipboard():
    with _Session() as win:
        win.clipboard_clear()
        win.clipboard_append("feetbrowser clipboard probe é中")
        assert win.clipboard_get() == "feetbrowser clipboard probe é中"


def test_title_reaches_the_real_window():
    with _Session() as win:
        win.title("a new title — unicode")
        buffer = ctypes.create_unicode_buffer(128)
        win32._libs["user32"].GetWindowTextW(win._hwnd, buffer, 128)
        assert buffer.value == "a new title — unicode", \
            "the title bar says %r" % buffer.value


def test_toplevel_events_route_to_the_right_window():
    """One message queue per thread, so the root's pump drains a popup's
    messages too and the shared procedure has to route them."""
    with _Session() as root:
        popup = win32.Win32Toplevel(root, width=400, height=300)
        try:
            assert popup in root.children
            hits = []
            root.bind("<Button-1>", lambda e: hits.append("root"))
            popup.bind("<Button-1>", lambda e: hits.append("popup"))
            pump(popup, 4)
            post(popup, win32.WM_LBUTTONDOWN, 0, pack(150, 150))
            pump(root)
            assert hits == ["popup"], \
                "popup events went to %r" % (hits or "nobody")
        finally:
            popup.destroy()
        assert popup not in root.children


# -- the browser, driven for real ------------------------------------------

def test_clicking_the_new_tab_button_opens_a_tab():
    """A stray attribute error anywhere in the mouse path swallows every
    click, and nothing else looks wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = br._new_tab_x() + browsermod.NEW_TAB_W / 2
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = browsermod.toes.band_height(br.chrome_bands()) + 20
        send(br.window, win32.WM_LBUTTONDOWN, win32.MK_LBUTTON, pack(x, y))
        send(br.window, win32.WM_LBUTTONUP, 0, pack(x, y))
        assert len(br.tabs) == before + 1, \
            "clicking + did not open a tab (%d -> %d)" % (before, len(br.tabs))


def test_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x54, 0)      # Ctrl-T
            assert len(br.tabs) == before + 1, "Ctrl-T did not open a tab"
            send(br.window, win32.WM_KEYDOWN, 0x4C, 0)      # Ctrl-L
        assert br.focus == "address", "Ctrl-L did not focus the address bar"


def test_typing_into_the_address_bar_works():
    with _Browser() as br:
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x4C, 0)
        for char in "abc":
            send(br.window, win32.WM_CHAR, ord(char), 0)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def test_a_frame_is_presented_after_interaction():
    with _Browser() as br:
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x54, 0)
        br.draw()
        br.window.present()
        assert br.window._frame, "nothing was presented after a tab opened"
        with _MemoryTarget(br.window, 900, 600) as target:
            br.window._blit(target.hdc)
            # The chrome is drawn at the top of every frame, so a blank
            # window here means the browser never reached the screen.
            assert target.pixel(450, 10) != (0, 0, 0), \
                "the top of the window is black"


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
    print(f"\nALL {len(tests)} WIN32 TESTS PASSED")


if __name__ == "__main__":
    main()
