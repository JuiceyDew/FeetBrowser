"""Tests for the X11 platform layer.

Two halves, and the split is deliberate. The arithmetic and the lookup tables
-- scanline padding, pixel layouts, keysyms, button numbering -- are plain
functions taking plain values, so they are tested first and everywhere,
including on a machine that has never seen an X server. That is most of what
can go wrong, and none of it needs a display.

The second half needs one. It creates a real window on a real server, sends
real events through XSendEvent, blits through real XPutImage and reads the
pixels back out with XGetImage. Nothing is stubbed. Where there is no server
to talk to, that half says so and the first half still runs.
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import x11
from feetbrowser.window import (QUIET, STATE_ALT, STATE_CONTROL,
                                STATE_SHIFT)

# The two formats every ordinary server hands out: a little-endian machine
# stores 0x00RRGGBB as B,G,R,pad, and a big-endian one stores it as pad,R,G,B.
LSB_32 = x11.PixelFormat(24, 32, 32, x11.LSB_FIRST, 0xFF0000, 0xFF00, 0xFF)
MSB_32 = x11.PixelFormat(24, 32, 32, x11.MSB_FIRST, 0xFF0000, 0xFF00, 0xFF)
# 24 bits per pixel is rare but legal, and on a big-endian server it is byte
# for byte what our framebuffer already holds.
MSB_24 = x11.PixelFormat(24, 24, 8, x11.MSB_FIRST, 0xFF0000, 0xFF00, 0xFF)
# 16-bit 5-6-5, which no byte layout can describe.
RGB_565 = x11.PixelFormat(16, 16, 16, x11.LSB_FIRST, 0xF800, 0x07E0, 0x001F)


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


# -- padding and strides ---------------------------------------------------

def test_scanline_padding_rounds_up_to_the_servers_unit():
    """A row is padded, so a width that does not divide evenly leaves slack
    at the end of every line. Ignoring it offsets each row a little more than
    the last, which is the classic diagonal smear."""
    eq(x11.scanline_bytes(1000, 32, 32), 4000, "32bpp needs no padding")
    eq(x11.scanline_bytes(999, 32, 32), 3996, "32bpp still needs none")
    eq(x11.scanline_bytes(999, 24, 32), 3000, "24bpp pads 2997 up to 3000")
    eq(x11.scanline_bytes(999, 24, 8), 2997, "byte padding adds nothing")
    eq(x11.scanline_bytes(3, 16, 32), 8, "16bpp pads 6 up to 8")
    eq(x11.scanline_bytes(1, 8, 32), 4)


# -- pixel layout ----------------------------------------------------------

def test_a_mask_finds_its_byte_at_both_byte_orders():
    """The mask describes the pixel as a *number*, so which byte it lands in
    depends on how the server stores numbers. Getting this backwards swaps
    red and blue, which is the single most visible bug this file can catch."""
    eq(x11.mask_byte(0xFF0000, 4, x11.LSB_FIRST), 2, "red, little-endian")
    eq(x11.mask_byte(0x00FF00, 4, x11.LSB_FIRST), 1)
    eq(x11.mask_byte(0x0000FF, 4, x11.LSB_FIRST), 0)
    eq(x11.mask_byte(0xFF0000, 4, x11.MSB_FIRST), 1, "red, big-endian")
    eq(x11.mask_byte(0x00FF00, 4, x11.MSB_FIRST), 2)
    eq(x11.mask_byte(0x0000FF, 4, x11.MSB_FIRST), 3)
    eq(x11.mask_byte(0xFF0000, 3, x11.MSB_FIRST), 0, "24bpp big-endian")
    assert x11.mask_byte(0xF800, 4, x11.LSB_FIRST) is None, \
        "a five-bit mask is not a byte"


def test_byte_layout_covers_the_formats_and_refuses_the_rest():
    eq(x11.byte_layout(LSB_32), (4, 2, 1, 0), "BGRX")
    eq(x11.byte_layout(MSB_32), (4, 1, 2, 3), "XRGB")
    eq(x11.byte_layout(MSB_24), (3, 0, 1, 2), "RGB, which is ours already")
    assert x11.byte_layout(RGB_565) is None, "565 has no byte layout"
    # A visual with two channels claiming the same byte is nonsense, and has
    # to be refused rather than quietly painted.
    broken = x11.PixelFormat(24, 32, 32, x11.LSB_FIRST, 0xFF, 0xFF, 0xFF00)
    assert x11.byte_layout(broken) is None


def test_channel_table_spreads_a_byte_over_the_bits_it_has():
    """Five bits of red means 0xFF has to become 31 and 0x00 has to become 0,
    with everything between spread evenly -- or white comes out grey."""
    red = x11.channel_table(0xF800)
    eq(red[0], 0)
    eq(red[255], 0xF800, "full red must reach the top of the field")
    eq(red[128], (128 * 31 // 255) << 11)
    full = x11.channel_table(0xFF0000)
    eq(full[255], 0xFF0000)
    eq(full[7], 7 << 16, "eight bits into eight bits is the identity")


def test_pack_pixels_orders_the_channels_the_server_asked_for():
    pixels = bytearray([10, 20, 30, 40, 50, 60])     # two RGB pixels
    data, line = x11.pack_pixels(pixels, 2, 1, 6, LSB_32)
    eq(line, 8)
    eq(bytes(data), bytes([30, 20, 10, 0, 60, 50, 40, 0]), "BGRX")
    data, line = x11.pack_pixels(pixels, 2, 1, 6, MSB_32)
    eq(bytes(data), bytes([0, 10, 20, 30, 0, 40, 50, 60]), "XRGB")


def test_a_server_that_wants_exactly_our_bytes_gets_them_uncopied():
    """The one format that needs no conversion should cost no conversion:
    the framebuffer itself goes to XPutImage."""
    pixels = bytearray([1, 2, 3, 4, 5, 6])
    data, line = x11.pack_pixels(pixels, 2, 1, 6, MSB_24)
    eq(line, 6)
    assert data is pixels, "the framebuffer should have been handed over as-is"


def test_padded_rows_do_not_smear_into_each_other():
    """Three pixels at 24bpp is nine bytes in a twelve-byte row. The three
    slack bytes have to stay slack; writing through them shifts every row
    after the first."""
    fmt = x11.PixelFormat(24, 24, 32, x11.MSB_FIRST, 0xFF0000, 0xFF00, 0xFF)
    pixels = bytearray(range(18))       # two rows of three RGB pixels
    data, line = x11.pack_pixels(pixels, 3, 2, 9, fmt)
    eq(line, 12, "nine bytes of pixels padded to a four-byte boundary")
    eq(len(data), 24)
    eq(bytes(data[0:9]), bytes(range(0, 9)), "first row")
    eq(bytes(data[9:12]), b"\0\0\0", "the padding stays untouched")
    eq(bytes(data[12:21]), bytes(range(9, 18)), "second row starts on time")


def test_the_slow_path_still_produces_the_right_colours():
    """Nothing this century takes the 16-bit path, so it is only ever going
    to be exercised here."""
    pixels = bytearray([255, 255, 255, 0, 0, 0, 255, 0, 0])
    data, line = x11.pack_pixels(pixels, 3, 1, 9, RGB_565)
    eq(line, 6)
    values = [int.from_bytes(data[i:i + 2], "little") for i in (0, 2, 4)]
    eq(values[0], 0xFFFF, "white must set every bit")
    eq(values[1], 0x0000, "black must set none")
    eq(values[2], 0xF800, "red belongs in the top five bits")


def test_a_full_frame_survives_a_round_trip_through_the_layout():
    """Pack a gradient and read it back through the same offsets, which is
    the check that catches an off-by-one in the row loop."""
    width, height = 7, 5
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 30) % 256, (y * 50) % 256, (x + y) % 256))
    for fmt in (LSB_32, MSB_32, MSB_24):
        data, line = x11.pack_pixels(pixels, width, height, width * 3, fmt)
        size, r_off, g_off, b_off = x11.byte_layout(fmt)
        for y in range(height):
            for x in range(width):
                at = y * line + x * size
                src = (y * width + x) * 3
                got = (data[at + r_off], data[at + g_off], data[at + b_off])
                eq(got, tuple(pixels[src:src + 3]),
                   "%r at (%d, %d)" % (fmt, x, y))


# -- input translation -----------------------------------------------------

def test_modifier_state_keeps_the_three_bits_tk_reads():
    """X's mask is where Tk's came from, so the three that matter pass
    straight through -- and the lock and button bits, which the other
    backends cannot produce, do not."""
    eq(x11.modifier_state(0), 0)
    eq(x11.modifier_state(0x1), STATE_SHIFT)
    eq(x11.modifier_state(0x4), STATE_CONTROL)
    eq(x11.modifier_state(0x8), STATE_ALT)
    eq(x11.modifier_state(0x5), STATE_SHIFT | STATE_CONTROL)
    eq(x11.modifier_state(0x2), 0, "Caps Lock is not a modifier here")
    eq(x11.modifier_state(0x100 | 0x4), STATE_CONTROL, "Button1 is dropped")


def test_the_wheel_is_two_buttons_and_stays_in_the_pixel_range():
    """browser.py treats |delta| < 30 as a pixel count and anything larger as
    line units, so a notch has to stay under 30 or one flick of the wheel
    moves the page by a screenful."""
    assert x11.wheel_delta(4) > 0, "button 4 scrolls up"
    assert x11.wheel_delta(5) < 0, "button 5 scrolls down"
    assert abs(x11.wheel_delta(4)) < 30
    assert abs(x11.wheel_delta(5)) < 30
    eq(x11.wheel_delta(1), 0, "the left button is not the wheel")
    eq(x11.wheel_delta(6), 0, "the horizontal wheel scrolls nothing")
    eq(x11.wheel_delta(7), 0)


def test_buttons_keep_their_numbers():
    eq(x11.button_binding(1, True), ("<Button-1>", 1))
    eq(x11.button_binding(2, True), ("<Button-2>", 2))
    eq(x11.button_binding(3, False), ("<ButtonRelease-3>", 3))
    assert x11.button_binding(4, True) is None, "the wheel is not a button"
    assert x11.button_binding(8, True) is None, "back/forward are not bound"


def test_a_drag_is_told_apart_from_a_move():
    eq(x11.motion_binding(0), ("<Motion>", 0))
    eq(x11.motion_binding(1 << 8), ("<B1-Motion>", 1))
    eq(x11.motion_binding(1 << 9), ("<B2-Motion>", 2))
    eq(x11.motion_binding(1 << 10), ("<B3-Motion>", 3))
    eq(x11.motion_binding(STATE_CONTROL), ("<Motion>", 0),
       "a held modifier is not a held button")


def test_keysyms_become_characters():
    eq(x11.keysym_unicode(0x61), "a", "Latin-1 keysyms are their codepoint")
    eq(x11.keysym_unicode(0x41), "A")
    eq(x11.keysym_unicode(0x20), " ")
    eq(x11.keysym_unicode(0xE9), "é", "e-acute")
    eq(x11.keysym_unicode(0x01000439), "й", "Unicode keysyms carry theirs")
    eq(x11.keysym_unicode(0xFFB7), "7", "keypad 7")
    eq(x11.keysym_unicode(0xFFAB), "+")
    assert x11.keysym_unicode(0xFF0D) is None, "Return is not a character"
    assert x11.keysym_unicode(0xFFE1) is None, "Shift is not a character"


def test_key_events_land_on_the_names_the_browser_binds():
    eq(x11.keysym_event("a", 0x61, 0), ("a", "a"))
    eq(x11.keysym_event("A", 0x41, STATE_SHIFT), ("A", "A"),
       "Tk names a shifted letter by the shifted character")
    eq(x11.keysym_event("space", 0x20, 0), ("space", " "))
    eq(x11.keysym_event("Return", 0xFF0D, 0), ("Return", "\r"))
    eq(x11.keysym_event("Left", 0xFF51, 0), ("Left", ""))
    eq(x11.keysym_event("l", 0x6C, STATE_CONTROL), ("l", "l"),
       "the keysym ignores Control, which is what <Control-l> needs")
    eq(x11.keysym_event("ISO_Left_Tab", 0xFE20, STATE_SHIFT),
       ("ISO_Left_Tab", ""))
    eq(x11.keysym_event("Tab", 0xFF09, STATE_SHIFT), ("ISO_Left_Tab", ""),
       "a server that does not fold Shift-Tab itself still has to reach it")
    eq(x11.keysym_event("Tab", 0xFF09, 0), ("Tab", ""))


def test_a_modifier_held_down_is_not_a_keypress():
    """X reports these as ordinary KeyPress events and Cocoa does not, so
    without the filter the browser sees a phantom key every time anyone
    reaches for Control."""
    for name, code in (("Shift_L", 0xFFE1), ("Control_R", 0xFFE4),
                       ("Alt_L", 0xFFE9), ("Super_L", 0xFFEB),
                       ("Caps_Lock", 0xFFE5)):
        assert x11.keysym_event(name, code, 0) is None, "%s leaked" % name
    assert x11.keysym_event("", 0, 0) is None, "an unnamed keysym is nothing"


def test_a_release_cannot_be_caught_by_a_typing_binding():
    names = x11.key_release_sequences("Left")
    eq(names, ("<KeyRelease-Left>", "<KeyRelease>"))
    assert "<Key>" not in names, "a key going up is not a keypress"
    assert "<Left>" not in names


def test_the_window_icon_is_a_cardinal_header_followed_by_argb_pixels():
    rgba = bytes((0x11, 0x22, 0x33, 0x44, 0xFF, 0x00, 0x00, 0x80))
    got = x11.net_wm_icon(2, 1, rgba)
    assert got == [2, 1, 0x44112233, 0x80FF0000]


# -- connecting ------------------------------------------------------------

class _Refusing:
    """Enough of libX11 to answer XOpenDisplay, refusing the first N times.

    ECONNREFUSED because that is what a full listen backlog gives, which is
    the failure this retry loop exists for.
    """

    def __init__(self, failures, errno=111):
        self.failures, self.errno, self.calls = failures, errno, 0

    def XOpenDisplay(self, _name):
        self.calls += 1
        if self.calls <= self.failures:
            ctypes.set_errno(self.errno)
            return None
        return 0x5EE1


class _Lib:
    """The real libX11, put back however the test ends."""

    def __init__(self, lib):
        self.lib = lib

    def __enter__(self):
        self.saved = x11._libs.get("x11")
        x11._libs["x11"] = self.lib
        return self.lib

    def __exit__(self, *_exc):
        if self.saved is None:
            x11._libs.pop("x11", None)
        else:
            x11._libs["x11"] = self.saved
        return False


def test_a_connection_refused_once_is_tried_again():
    """The whole point: a server that says no and then says yes is reached."""
    lib = _Refusing(failures=3)
    with _Lib(lib):
        display, err, waited = x11._connect(attempts=6, pause=0.001)
    eq(display, 0x5EE1, "the fourth attempt should have connected")
    eq(lib.calls, 4, "it stops asking as soon as it is let in")
    eq(err, 0, "a connection that succeeded has no error to report")
    assert waited > 0, "it waited between the attempts"


def test_a_server_that_is_really_gone_is_not_waited_on_for_ever():
    """The other side of it: refusals do not become a hang, and the reason
    the last one gave survives to the caller."""
    lib = _Refusing(failures=1000, errno=2)
    with _Lib(lib):
        started = time.monotonic()
        display, err, waited = x11._connect(attempts=5, pause=0.001)
        spent = time.monotonic() - started
    assert display is None, "there was never a server"
    eq(lib.calls, 5, "it tried exactly as often as it was told to")
    eq(err, 2, "the last errno is what gets reported")
    assert spent < 1.0, "five attempts a millisecond apart took %.2f s" % spent
    # The wait it reports is the doubling series it was told to use -- 1, 2,
    # 4 and 8 ms, with no sleep after the attempt it does not retry. That is
    # arithmetic, so it is checked as arithmetic. Comparing it against
    # `spent` instead checks the clock: `time.monotonic` moves in steps of
    # 15.6 ms on Windows, which is longer than this whole budget, so the
    # elapsed time of a run that really did sleep for 15 ms reads as zero
    # about as often as not.
    eq(round(waited, 6), 0.015, "the pauses it reports are not the ones set")


def test_a_connection_that_works_first_time_costs_nothing():
    """A retry loop that slept before its first attempt would make every
    window on every machine a tenth of a second slower to open."""
    lib = _Refusing(failures=0)
    with _Lib(lib):
        started = time.monotonic()
        display, _err, waited = x11._connect(attempts=6, pause=5.0)
        spent = time.monotonic() - started
    eq(lib.calls, 1, "one attempt was enough, so one attempt is what it made")
    eq(display, 0x5EE1)
    eq(waited, 0.0, "it reported waiting that it did not do")
    assert spent < 0.5, "a first-attempt connection slept for %.2f s" % spent


def test_the_default_budget_stays_small_enough_to_fall_back_on():
    """A machine with DISPLAY set and no server falls back to a headless
    root, and pays this budget to find out. Doubling waits are easy to grow
    by one attempt and hard to notice, so the arithmetic is pinned."""
    pause, total = x11._CONNECT_PAUSE, 0.0
    for _ in range(x11._CONNECT_ATTEMPTS - 1):
        total += pause
        pause *= 2
    assert total < 0.5, "startup would stall for %.2f s with no server" % total
    assert x11._CONNECT_ATTEMPTS >= 3, "one retry is not a retry loop"


def test_a_display_name_says_which_socket_it_means():
    eq(x11._display_socket(":99"), "/tmp/.X11-unix/X99")
    eq(x11._display_socket(":0.0"), "/tmp/.X11-unix/X0")
    eq(x11._display_socket("unix:99.0"), "/tmp/.X11-unix/X99")
    # A remote display has no local socket, and guessing one would put a
    # confident, wrong sentence in front of somebody debugging ssh -X.
    eq(x11._display_socket("host.example:0"), "")
    eq(x11._display_socket("localhost:10.0"), "")
    eq(x11._display_socket(""), "")
    eq(x11._display_socket(":bogus"), "")


def test_the_failure_says_what_was_wrong_not_only_that_something_was():
    """Three causes wear the same NULL return; the message has to tell them
    apart on a runner nobody can log into."""
    missing = str(x11._unreachable(":99", 2, 0.31))
    assert "cannot reach the X server at :99" in missing, missing
    assert "0.31 s" in missing, missing
    # Whether it exists depends on whether this machine is running the very
    # server CI runs on, so the test asks the same question the message did.
    there = "" if os.path.exists("/tmp/.X11-unix/X99") else " not"
    assert ("/tmp/.X11-unix/X99 does%s exist" % there) in missing, missing
    assert os.strerror(2) in missing, missing
    # A display we cannot map to a socket claims nothing about one, and a
    # first-attempt failure does not claim to have retried.
    remote = str(x11._unreachable("host.example:0", 0, 0.0))
    assert ".X11-unix" not in remote, remote
    assert "retrying" not in remote, remote
    assert "last error" not in remote, remote


# -- the live half ---------------------------------------------------------

def _live_reason():
    if not x11.available():
        return x11.unavailable_reason() or "no X11 on this platform"
    return ""


LIVE_REASON = _live_reason()
LIVE = not LIVE_REASON

if LIVE:
    from feetbrowser import browser as browsermod
    from feetbrowser import canvas as canvasmod

    # A few signatures the backend itself never needs, declared the same way
    # it declares its own -- a missing restype truncates an XID to 32 bits.
    def _extra(name, restype, argtypes):
        fn = getattr(x11._libs["x11"], name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _string_to_keysym = _extra("XStringToKeysym", x11.KeySym,
                               [ctypes.c_char_p])
    _get_input_focus = _extra("XGetInputFocus", x11.Status,
                              [x11.Display, ctypes.POINTER(x11.XID),
                               ctypes.POINTER(ctypes.c_int)])
    _keysym_to_keycode = _extra("XKeysymToKeycode", ctypes.c_ubyte,
                                [x11.Display, x11.KeySym])
    _get_image = _extra("XGetImage", ctypes.POINTER(x11.XImage),
                        [x11.Display, x11.XID, ctypes.c_int, ctypes.c_int,
                         ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong,
                         ctypes.c_int])
    _get_geometry = _extra("XGetGeometry", x11.Status,
                           [x11.Display, x11.XID,
                            ctypes.POINTER(x11.XID),
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint)])
    _fetch_name = _extra("XFetchName", x11.Status,
                         [x11.Display, x11.XID,
                          ctypes.POINTER(ctypes.c_char_p)])


def send(win, event, mask=0):
    """Deliver one event to a window the way another client would.

    XSendEvent rather than XTestFakeInput: a synthetic event goes to the
    window named regardless of where the pointer is or who has the keyboard
    focus, which is the only way to be deterministic on a bare server with no
    window manager running.
    """
    lib = x11._libs["x11"]
    assert lib.XSendEvent(win._display, win._window, False, mask,
                          ctypes.byref(event)), "XSendEvent failed"
    lib.XFlush(win._display)


def key_event(win, name, state=0, press=True):
    keysym = _string_to_keysym(name.encode())
    assert keysym, "no keysym called %r" % name
    code = _keysym_to_keycode(win._display, keysym)
    assert code, "%r is not on this keyboard layout" % name
    event = x11.XEvent()
    event.xkey.type = x11.KEY_PRESS if press else x11.KEY_RELEASE
    event.xkey.display = win._display
    event.xkey.window = win._window
    event.xkey.root = x11._state["root"]
    event.xkey.keycode = code
    event.xkey.state = state
    event.xkey.same_screen = True
    return event


def press_key(win, name, state=0):
    send(win, key_event(win, name, state), x11.KEY_PRESS_MASK)


def button_event(win, button, x, y, state=0, press=True):
    event = x11.XEvent()
    event.xbutton.type = x11.BUTTON_PRESS if press else x11.BUTTON_RELEASE
    event.xbutton.display = win._display
    event.xbutton.window = win._window
    event.xbutton.root = x11._state["root"]
    event.xbutton.button = button
    event.xbutton.state = state
    event.xbutton.x, event.xbutton.y = x, y
    event.xbutton.same_screen = True
    return event


def click(win, button, x, y, state=0):
    send(win, button_event(win, button, x, y, state, True),
         x11.BUTTON_PRESS_MASK)
    send(win, button_event(win, button, x, y, state, False),
         x11.BUTTON_RELEASE_MASK)


def pump(win, times=3):
    """Let the window process everything the server has for it.

    XSync before each pass, and this is not belt and braces: XPending never
    waits, so an event we sent a microsecond ago is usually still in flight
    and a bare poll_events() sails straight past it. XSync makes the round
    trip, which puts the reply in our queue before we look.
    """
    for _ in range(times):
        if win._closed:     # the display is gone with it; do not touch it
            return
        x11._libs["x11"].XSync(win._display, False)
        win.poll_events()


def wait_ready(win, seconds=10.0):
    """Wait until the server will let us read the window back.

    A window is not on screen the instant XMapWindow returns -- with a window
    manager in the way it takes a round trip or two, and until then XGetImage
    on it simply fails and anything we blit is thrown away by the map. Asking
    for one pixel is that question put directly to the server.
    """
    lib = x11._libs["x11"]
    deadline = time.time() + seconds
    while True:
        lib.XSync(win._display, False)
        win.poll_events()
        image = _get_image(win._display, win._window, 0, 0, 1, 1,
                           0xFFFFFFFF, x11.Z_PIXMAP)
        if image:
            image.contents.data = None
            lib.XFree(image)
            return True
        if time.time() > deadline:
            return False
        time.sleep(0.02)


def geometry(win):
    """(width, height) as the X server currently has it."""
    root = x11.XID()
    x, y = ctypes.c_int(), ctypes.c_int()
    width, height = ctypes.c_uint(), ctypes.c_uint()
    border, depth = ctypes.c_uint(), ctypes.c_uint()
    x11._libs["x11"].XSync(win._display, False)
    assert _get_geometry(win._display, win._window, ctypes.byref(root),
                         ctypes.byref(x), ctypes.byref(y),
                         ctypes.byref(width), ctypes.byref(height),
                         ctypes.byref(border), ctypes.byref(depth)), \
        "XGetGeometry failed"
    return width.value, height.value


def wait_geometry(win, size, seconds=5.0):
    """A resize is a request, not a command -- a window manager answers it in
    its own time, and on a bare server it happens at once."""
    deadline = time.time() + seconds
    while geometry(win) != size and time.time() < deadline:
        win.poll_events()
        time.sleep(0.02)
    return geometry(win)


def grab(win):
    """(pixels, bytes_per_line) read straight back out of the X server.

    This is the only check that proves the frame arrived rather than merely
    being sent: XGetImage asks the server what is actually on the window.
    """
    lib = x11._libs["x11"]
    lib.XSync(win._display, False)
    image = _get_image(win._display, win._window, 0, 0, win.width, win.height,
                       0xFFFFFFFF, x11.Z_PIXMAP)
    assert image, "XGetImage came back empty"
    try:
        line = image.contents.bytes_per_line
        raw = ctypes.string_at(image.contents.data,
                               line * image.contents.height)
    finally:
        image.contents.data = None
        lib.XFree(image)
    return raw, line


def _channel(value, mask):
    """One channel of a packed pixel, scaled back up to 0..255."""
    shift = (mask & -mask).bit_length() - 1
    span = mask >> shift
    return ((value & mask) >> shift) * 255 // span


def pixel(win, x, y):
    """One pixel of the window as (r, g, b), read back off the server.

    Decoded through the visual's masks rather than the byte offsets the
    backend uses, so this still works at depth 15 or 16 where there are no
    byte offsets -- and so it is not simply the code under test run twice.
    """
    raw, line = grab(win)
    fmt = x11._state["format"]
    size = fmt.bits_per_pixel // 8
    at = y * line + x * size
    order = "little" if fmt.byte_order == x11.LSB_FIRST else "big"
    value = int.from_bytes(raw[at:at + size], order)
    return tuple(_channel(value, mask) for mask in
                 (fmt.red_mask, fmt.green_mask, fmt.blue_mask))


def tolerance():
    """How far a colour may drift on this server, and why.

    A 5-bit channel has 32 steps to hold 256 values, so a colour that goes
    out and comes back is only ever going to be close. On the 8-bit channels
    everything else has, this is zero and the comparisons are exact.
    """
    fmt = x11._state["format"]
    steps = min((mask >> ((mask & -mask).bit_length() - 1))
                for mask in (fmt.red_mask, fmt.green_mask, fmt.blue_mask))
    return 255 // steps


def same_colour(got, want, msg=""):
    slack = tolerance()
    for channel, (a, b) in enumerate(zip(got, want)):
        assert abs(a - b) <= slack, \
            "%s: %r != %r (channel %d, tolerance %d)" % (msg, got, want,
                                                         channel, slack)


class _Session:
    """A live window, torn down however the test ends."""

    def __init__(self, width=500, height=400):
        self.size = (width, height)

    def __enter__(self):
        self.win = x11.X11Tk(width=self.size[0], height=self.size[1],
                             title="test")
        assert wait_ready(self.win), "the window never reached the screen"
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        return False


class _Browser(_Session):
    """A live window driving a real Browser, on about:blank only."""

    def __init__(self):
        super().__init__(1000, 700)

    def __enter__(self):
        win = super().__enter__()
        self.browser = browsermod.Browser(win)
        # Browser.__init__ calls geometry(), so the window is a different size
        # now and XGetImage will not read past what the server actually has.
        wait_geometry(win, (win.width, win.height))
        self.browser.new_tab("about:blank")
        self.browser.draw()
        win.present()
        return self.browser


# -- the runtime bridge ----------------------------------------------------

def live_window_opens_at_the_size_asked_for():
    """The server's opinion, not ours: a window whose geometry we only ever
    read back from our own attribute proves nothing."""
    with _Session(640, 480) as win:
        eq(geometry(win), (640, 480), "the server disagrees")
        assert win.winfo_exists()


def live_a_quiet_window_does_not_take_the_keyboard():
    """The suite opens dozens of windows in a few seconds. Under QUIET each
    one must map without the window manager placing it, raising it or handing
    it the keyboard, while staying a real mapped window the live half can
    read pixels back from. Without this the fix regresses silently, and the
    only symptom is a machine nobody can type on while the tests run."""
    if not QUIET:
        print("  ..  quiet-window check needs FEETBROWSER_QUIET=1")
        return
    with _Session(640, 480) as win:
        focus, revert = x11.XID(), ctypes.c_int()
        _get_input_focus(win._display, ctypes.byref(focus),
                         ctypes.byref(revert))
        assert int(focus.value) != int(win._window), \
            "a quiet window took the keyboard"
        # Still real, mapped and the size asked for, or the quiet is
        # worthless: the live tests below read pixels off this window.
        eq(geometry(win), (640, 480), "the server disagrees")


def live_present_puts_the_right_colours_on_the_server():
    """The whole point of the module. A backwards byte order or a wrong
    stride still puts *something* on the screen, so the test has to read the
    pixels back and look at them."""
    with _Session(300, 200) as win:
        canvas = canvasmod.Canvas(win, width=300, height=200, bg="#3366cc")
        canvas.pack()
        canvas.create_rectangle(0, 0, 40, 40, fill="#ff0000", width=0)
        win.present()
        same_colour(pixel(win, 10, 10), (0xFF, 0x00, 0x00), "red is not red")
        same_colour(pixel(win, 200, 150), (0x33, 0x66, 0xCC), "the background is wrong")


def live_present_is_skipped_when_nothing_changed():
    with _Session(200, 120) as win:
        canvas = canvasmod.Canvas(win, width=200, height=120, bg="#101010")
        canvas.pack()
        win.present()
        first = win._frame
        win.present()
        assert win._frame is first, "a clean canvas should not be re-packed"
        canvas.create_rectangle(0, 0, 10, 10, fill="#ffffff")
        win.present()
        assert win._frame is not first, "a dirty canvas must be re-packed"


def live_an_odd_width_does_not_smear():
    """A width that is not a round number is where a stride bug shows up, and
    it shows up as a picture leaning to one side."""
    with _Session(333, 111) as win:
        canvas = canvasmod.Canvas(win, width=333, height=111, bg="#ffffff")
        canvas.pack()
        canvas.create_rectangle(0, 0, 333, 111, fill="#00ff00", width=0)
        canvas.create_rectangle(330, 0, 333, 111, fill="#0000ff", width=0)
        canvas.create_rectangle(0, 108, 320, 111, fill="#ff0000", width=0)
        win.present()
        # Not the very corner: some window managers round it off, and what
        # they do with the pixels underneath is their business, not ours.
        same_colour(pixel(win, 332, 55), (0x00, 0x00, 0xFF), "the last column slipped")
        same_colour(pixel(win, 5, 110), (0xFF, 0x00, 0x00), "the last row slipped")
        same_colour(pixel(win, 200, 50), (0x00, 0xFF, 0x00))


def live_expose_asks_for_another_frame():
    with _Session(200, 150) as win:
        canvas = canvasmod.Canvas(win, width=200, height=150, bg="#204060")
        canvas.pack()
        win.present()
        win._repaint = False
        event = x11.XEvent()
        event.xexpose.type = x11.EXPOSE
        event.xexpose.display = win._display
        event.xexpose.window = win._window
        event.xexpose.width, event.xexpose.height = 200, 150
        event.xexpose.count = 0
        send(win, event, x11.EXPOSURE_MASK)
        pump(win)
        assert win._repaint, "an expose did not ask for a repaint"
        win.present()
        same_colour(pixel(win, 100, 75), (0x20, 0x40, 0x60))


def live_a_resize_reaches_the_canvas():
    with _Session(400, 300) as win:
        canvas = canvasmod.Canvas(win, width=400, height=300, bg="#ffffff")
        canvas.pack()
        seen = []
        win.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
        event = x11.XEvent()
        event.xconfigure.type = x11.CONFIGURE_NOTIFY
        event.xconfigure.display = win._display
        event.xconfigure.window = win._window
        event.xconfigure.configured = win._window
        event.xconfigure.width, event.xconfigure.height = 520, 360
        send(win, event, x11.STRUCTURE_NOTIFY_MASK)
        pump(win)
        eq((win.width, win.height), (520, 360), "the window did not resize")
        assert (520, 360) in seen, "<Configure> never fired: %r" % seen


def live_our_own_resize_reaches_the_server():
    with _Session(400, 300) as win:
        win.resize(300, 220)
        eq(wait_geometry(win, (300, 220)), (300, 220))


# -- dense displays --------------------------------------------------------
#
# X measures everything in device pixels -- the window it creates, the sizes
# in a ConfigureNotify, the coordinates on every event -- and the browser
# measures everything in CSS pixels. The conversion happens in this backend
# and nowhere else. FEETBROWSER_SCALE stands in for an Xft.dpi of 192: a CI
# runner's Xvfb has no resource database and so reports nothing, which is the
# 1x case every other test here already covers.

class _DenseSession(_Session):
    """A live window on a pretend 2x display."""

    def __enter__(self):
        self.saved = os.environ.get("FEETBROWSER_SCALE")
        os.environ["FEETBROWSER_SCALE"] = "2"
        try:
            return super().__enter__()
        except Exception:
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


def live_a_dense_frame_lands_one_pixel_for_one():
    """The window really is twice the size the browser thinks it is, and the
    frame in it was read back off the server -- which is the only way to tell
    a blit that landed one to one from one the display stretched.

    A rectangle 10 CSS pixels across has to reach device pixel 19 and stop
    before device pixel 21. With a buffer allocated in CSS pixels it covered
    ten device pixels and the server scaled nothing, so pixel 19 was
    background -- which is what the whole page looked like.
    """
    with _DenseSession(320, 240) as win:
        eq(win.scale, 2.0, "the scale never reached the window")
        eq(geometry(win), (640, 480), "the server was asked in CSS pixels")
        canvas = canvasmod.Canvas(win, width=320, height=240, bg="#3366cc")
        canvas.pack()
        pump(win)
        eq((win.width, win.height), (320, 240),
           "a ConfigureNotify in device pixels resized the page")
        eq(canvas.device_size(), (640, 480), "the buffer is not the window")
        canvas.create_rectangle(0, 0, 10, 10, fill="#ff0000", width=0)
        win.present()
        eq(win._frame_dims, (640, 480), "the frame is not the window's size")
        same_colour(pixel(win, 19, 19), (0xFF, 0x00, 0x00),
                    "the rectangle stopped short of its device pixels")
        same_colour(pixel(win, 21, 21), (0x33, 0x66, 0xCC),
                    "the rectangle ran past its device pixels")


def live_a_click_at_a_device_pixel_lands_on_its_css_pixel():
    """Hit testing, which is the half that a sharper buffer alone would
    break: the server points at a device pixel and the browser has to be
    handed the CSS pixel containing it."""
    with _DenseSession(320, 240) as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
        win.bind("<Motion>", lambda e: seen.append((e.x, e.y)))
        for point in ((0, 0), (1, 1), (240, 80), (639, 479)):
            send(win, button_event(win, 1, point[0], point[1]),
                 x11.BUTTON_PRESS_MASK)
        motion = x11.XEvent()
        motion.xmotion.type = x11.MOTION_NOTIFY
        motion.xmotion.display = win._display
        motion.xmotion.window = win._window
        motion.xmotion.x, motion.xmotion.y = 300, 200
        send(win, motion, x11.POINTER_MOTION_MASK)
        pump(win)
        eq(seen, [(0, 0), (0, 0), (120, 40), (319, 239), (150, 100)],
           "device pixels reached the browser unconverted")


# -- event translation -----------------------------------------------------

def live_every_mouse_gesture_reaches_its_binding():
    with _Session() as win:
        seen = []
        for name in ("<Button-1>", "<ButtonRelease-1>", "<Button-2>",
                     "<Button-3>", "<ButtonRelease-3>", "<Motion>",
                     "<B1-Motion>"):
            win.bind(name, lambda e, n=name: seen.append(n))
        click(win, 1, 120, 90)
        click(win, 2, 120, 90)
        click(win, 3, 120, 90)
        motion = x11.XEvent()
        motion.xmotion.type = x11.MOTION_NOTIFY
        motion.xmotion.display = win._display
        motion.xmotion.window = win._window
        motion.xmotion.x, motion.xmotion.y = 130, 100
        send(win, motion, x11.POINTER_MOTION_MASK)
        motion.xmotion.state = 1 << 8       # Button1 held: this is a drag
        send(win, motion, x11.POINTER_MOTION_MASK)
        pump(win)
        for name in ("<Button-1>", "<ButtonRelease-1>", "<Button-2>",
                     "<Button-3>", "<ButtonRelease-3>", "<Motion>",
                     "<B1-Motion>"):
            assert name in seen, "%s never fired (%r)" % (name, seen)


def live_clicks_arrive_where_they_were_aimed():
    with _Session() as win:
        seen = []
        win.bind("<Button-1>", lambda e: seen.append((e.x, e.y, e.num)))
        for point in ((0, 0), (120, 40), (499, 399)):
            send(win, button_event(win, 1, point[0], point[1]),
                 x11.BUTTON_PRESS_MASK)
        pump(win)
        eq(seen, [(0, 0, 1), (120, 40, 1), (499, 399, 1)])


def live_the_wheel_scrolls_the_right_way():
    with _Session() as win:
        seen = []
        win.bind("<MouseWheel>", lambda e: seen.append(e.delta))
        click(win, 4, 100, 100)
        click(win, 5, 100, 100)
        pump(win)
        eq(len(seen), 2, "a notch should scroll once, not once per edge")
        assert seen[0] > 0 > seen[1], "wheel direction was lost: %r" % seen
        for delta in seen:
            assert abs(delta) < 30, "%r escapes the pixel path" % delta


def live_keys_carry_their_keysym_and_character():
    with _Session() as win:
        seen = []
        win.bind("<Key>", lambda e: seen.append((e.keysym, e.char)))
        press_key(win, "z")
        press_key(win, "space")
        press_key(win, "a", STATE_SHIFT)
        pump(win)
        assert ("z", "z") in seen, "letter key lost its char: %r" % seen
        assert ("space", " ") in seen, "space: %r" % seen
        assert ("A", "A") in seen, "Shift-a should arrive as A: %r" % seen


def live_named_keys_beat_the_generic_key_binding():
    """Tk fires only the most specific binding. A browser that binds both
    <Up> and <Key> must not see one keypress twice."""
    with _Session() as win:
        hits = []
        win.bind("<Up>", lambda e: hits.append("Up"))
        win.bind("<Key>", lambda e: hits.append("Key"))
        press_key(win, "Up")
        pump(win)
        eq(hits, ["Up"], "expected only <Up>")
        press_key(win, "q")
        pump(win)
        eq(hits, ["Up", "Key"], "a plain letter should fall to <Key>")


def live_control_shortcuts_reach_their_bindings():
    with _Session() as win:
        seen = []
        win.bind("<Control-l>", lambda e: seen.append(e.state))
        press_key(win, "l", STATE_CONTROL)
        pump(win)
        eq(len(seen), 1, "Control-L did not arrive")
        assert seen[0] & STATE_CONTROL


def live_shift_tab_reaches_the_previous_tab_binding():
    with _Session() as win:
        seen = []
        win.bind("<Control-ISO_Left_Tab>", lambda e: seen.append(1))
        # Sent as a shifted Tab, which is what a keyboard produces. Whether
        # the layout folds it to ISO_Left_Tab itself or keysym_event has to,
        # it has to end up at the same binding.
        press_key(win, "Tab", STATE_CONTROL | STATE_SHIFT)
        pump(win)
        assert seen, "shifted Tab did not reach <Control-ISO_Left_Tab>"


def live_a_modifier_on_its_own_delivers_nothing():
    with _Session() as win:
        hits = []
        win.bind("<Key>", lambda e: hits.append(e.keysym))
        press_key(win, "Shift_L")
        press_key(win, "Control_L")
        pump(win)
        eq(hits, [], "a modifier arrived as a keypress")


def live_a_key_going_up_is_its_own_event():
    with _Session() as win:
        hits = []
        win.bind("<Key>", lambda e: hits.append("down"))
        win.bind("<KeyRelease>", lambda e: hits.append("up"))
        send(win, key_event(win, "b", press=False), x11.KEY_RELEASE_MASK)
        pump(win)
        eq(hits, ["up"], "a release was mistaken for a keypress")


def live_the_close_button_actually_closes():
    """There is no close event in X: the window manager asks through
    WM_DELETE_WINDOW and a client that ignores it gets killed instead."""
    win = x11.X11Tk(width=200, height=150, title="closing")
    closed = []
    win.protocol("WM_DELETE_WINDOW", lambda: closed.append(True))
    event = x11.XEvent()
    event.xclient.type = x11.CLIENT_MESSAGE
    event.xclient.display = win._display
    event.xclient.window = win._window
    event.xclient.message_type = x11._state["WM_PROTOCOLS"]
    event.xclient.format = 32
    event.xclient.data.l[0] = x11._state["WM_DELETE_WINDOW"]
    send(win, event, 0)
    pump(win)
    assert closed, "WM_DELETE_WINDOW did not run the close handler"
    assert not win.winfo_exists(), "the window is still alive"


def live_handler_exceptions_do_not_stop_the_loop():
    with _Session() as win:
        errors = []
        win.on_callback_error = lambda where, exc: errors.append(where)
        win.bind("<Button-1>", lambda e: 1 // 0)
        send(win, button_event(win, 1, 50, 50), x11.BUTTON_PRESS_MASK)
        pump(win)
        assert errors, "a raising handler was not reported"
        assert win.winfo_exists(), "one bad handler took down the window"


def live_the_title_reaches_the_real_window():
    with _Session() as win:
        win.title("a new title")
        x11._libs["x11"].XSync(win._display, False)
        name = ctypes.c_char_p()
        assert _fetch_name(win._display, win._window, ctypes.byref(name))
        eq(name.value, b"a new title")
        x11._libs["x11"].XFree(name)


def live_toplevel_events_route_to_the_right_window():
    """One event queue per connection, so the root's loop has to hand a
    popup's events to the popup -- that is how PopupWindow has always run."""
    with _Session() as root:
        popup = x11.X11Toplevel(root, width=300, height=200)
        try:
            assert popup in root.children
            wait_ready(popup)
            pump(root)
            # Bind only once both windows are up and settled: these are real
            # windows on a real desktop, and a stray click of the user's own
            # landing on the root while it maps would look like a routing bug.
            hits = []
            root.bind("<Button-1>", lambda e: hits.append("root"))
            popup.bind("<Button-1>", lambda e: hits.append("popup"))
            send(popup, button_event(popup, 1, 100, 100),
                 x11.BUTTON_PRESS_MASK)
            pump(root)
            eq(hits, ["popup"], "popup events went astray")
        finally:
            popup.destroy()
        assert popup not in root.children


# -- selections ------------------------------------------------------------

def live_copying_claims_the_clipboard():
    with _Session() as win:
        win.clipboard_clear()
        win.clipboard_append("feetbrowser clipboard probe")
        owner = x11._libs["x11"].XGetSelectionOwner(win._display,
                                                    x11._state["CLIPBOARD"])
        eq(int(owner), int(win._window), "we did not become the owner")
        eq(win.clipboard_get(), "feetbrowser clipboard probe")


def live_another_client_gets_the_text_off_the_wire():
    """The paste path other applications use, exercised for real: a
    SelectionRequest goes in and the text comes back out of a property on the
    requesting window."""
    with _Session() as owner:
        other = x11.X11Toplevel(owner, width=200, height=150)
        try:
            owner.clipboard_clear()
            owner.clipboard_append("over the wire")
            request = x11.XEvent()
            request.xselectionrequest.type = x11.SELECTION_REQUEST
            request.xselectionrequest.display = owner._display
            request.xselectionrequest.owner = owner._window
            request.xselectionrequest.requestor = other._window
            request.xselectionrequest.selection = x11._state["CLIPBOARD"]
            request.xselectionrequest.target = x11._state["UTF8_STRING"]
            request.xselectionrequest.property = \
                x11._state["FEETBROWSER_SELECTION"]
            owner._on_selection(request)
            x11._libs["x11"].XSync(owner._display, False)
            eq(other._read_property(x11._state["FEETBROWSER_SELECTION"]),
               "over the wire")
        finally:
            other.destroy()


def live_we_say_which_forms_we_can_offer():
    with _Session() as owner:
        other = x11.X11Toplevel(owner, width=200, height=150)
        try:
            owner.clipboard_append("anything")
            request = x11.XEvent()
            request.xselectionrequest.type = x11.SELECTION_REQUEST
            request.xselectionrequest.display = owner._display
            request.xselectionrequest.owner = owner._window
            request.xselectionrequest.requestor = other._window
            request.xselectionrequest.selection = x11._state["CLIPBOARD"]
            request.xselectionrequest.target = x11._state["TARGETS"]
            request.xselectionrequest.property = \
                x11._state["FEETBROWSER_SELECTION"]
            assert owner._serve_target(int(other._window),
                                       x11._state["TARGETS"],
                                       x11._state["FEETBROWSER_SELECTION"])
            assert not owner._serve_target(int(other._window), 12345,
                                           x11._state["FEETBROWSER_SELECTION"])
        finally:
            other.destroy()


def live_losing_the_selection_drops_the_stale_text():
    with _Session() as win:
        win.clipboard_append("mine for now")
        event = x11.XEvent()
        event.xselectionclear.type = x11.SELECTION_CLEAR
        event.xselectionclear.display = win._display
        event.xselectionclear.window = win._window
        event.xselectionclear.selection = x11._state["CLIPBOARD"]
        win._on_selection(event)
        eq(win._selection, "", "we kept handing out text we no longer own")


# -- the browser, driven for real ------------------------------------------

def live_clicking_the_new_tab_button_opens_a_tab():
    """A stray attribute error anywhere in the mouse path swallows every
    click in the browser with nothing else looking wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = int(br._new_tab_x() + browsermod.NEW_TAB_W / 2)
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = int(browsermod.toes.band_height(br.chrome_bands()) + 20)
        click(br.window, 1, x, y)
        pump(br.window)
        eq(len(br.tabs), before + 1, "clicking + did not open a tab")


def live_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        press_key(br.window, "t", STATE_CONTROL)
        pump(br.window)
        eq(len(br.tabs), before + 1, "Ctrl-T did not open a tab")
        press_key(br.window, "l", STATE_CONTROL)
        pump(br.window)
        eq(br.focus, "address", "Ctrl-L did not focus the address bar")


def live_typing_into_the_address_bar_works():
    with _Browser() as br:
        press_key(br.window, "l", STATE_CONTROL)
        pump(br.window)
        for char in "abc":
            press_key(br.window, char)
            pump(br.window, 2)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def live_a_real_page_reaches_the_screen():
    """End to end: chrome, tabs, toolbar and page, drawn by the browser and
    read back off the X server. A window that stayed blank fails here."""
    with _Browser() as br:
        br.draw()
        br.window.present()
        raw, line = grab(br.window)
        fmt = x11._state["format"]
        size = fmt.bits_per_pixel // 8
        order = "little" if fmt.byte_order == x11.LSB_FIRST else "big"
        colours = set()
        for y in range(0, br.window.height, 17):
            for x in range(0, br.window.width, 23):
                at = y * line + x * size
                colours.add(int.from_bytes(raw[at:at + size], order))
        assert len(colours) > 2, \
            "the window is one flat colour; nothing was drawn"


def motion_event(win, x, y, state=0):
    event = x11.XEvent()
    event.xmotion.type = x11.MOTION_NOTIFY
    event.xmotion.display = win._display
    event.xmotion.window = win._window
    event.xmotion.root = x11._state["root"]
    event.xmotion.x, event.xmotion.y = x, y
    event.xmotion.state = state
    event.xmotion.same_screen = True
    return event


def drag_to(win, x, y):
    """A pointer move with Button 1 held, which is X11's way of saying drag:
    one event type for moving and dragging, told apart by the state mask."""
    send(win, motion_event(win, x, y, 1 << 8), x11.POINTER_MOTION_MASK)


def _tall_page(br):
    """Load a page far taller than the window and return its tab."""
    br.new_tab("data:text/html," + "".join("<p>line %d</p>" % i
                                           for i in range(300)))
    br.draw()
    tab = br.active_tab
    assert tab.content_height() > br.tab_height(), "the page is not tall"
    return tab


def live_dragging_the_scrollbar_scrolls_the_page():
    """ButtonPress, then MotionNotify with Button1Mask, then ButtonRelease --
    the three the scrollbar is dragged with. The middle one is the event
    nothing used to be listening for on the bar."""
    with _Browser() as br:
        tab = _tall_page(br)
        # An unscrolled page puts the thumb at the very top of the track.
        thumb_top = int(br.chrome_height())
        x = br.canvas.winfo_width() - 7
        send(br.window, button_event(br.window, 1, x, thumb_top + 5),
             x11.BUTTON_PRESS_MASK)
        pump(br.window)
        eq(tab.scroll, 0, "pressing the thumb jumped the page")
        drag_to(br.window, x, thumb_top + 105)
        pump(br.window)
        assert tab.scroll > 0, "dragging the thumb did not scroll the page"
        send(br.window, button_event(br.window, 1, x, thumb_top + 105,
                                     press=False), x11.BUTTON_RELEASE_MASK)
        pump(br.window)
        settled = tab.scroll
        drag_to(br.window, x, thumb_top + 300)
        pump(br.window)
        eq(tab.scroll, settled, "the drag survived the button coming up")


def live_a_drag_that_leaves_the_window_still_scrolls():
    """The press grabs the pointer, so X keeps reporting the drag to this
    window with coordinates outside it -- and dragging past the end of the
    document has to stop exactly where the wheel stops."""
    with _Browser() as br:
        tab = _tall_page(br)
        tab.scroll_by(10 ** 9)
        bottom = tab.scroll
        tab.set_scroll(0)
        br.draw()
        thumb_top = int(br.chrome_height())
        x = br.canvas.winfo_width() - 7
        send(br.window, button_event(br.window, 1, x, thumb_top + 5),
             x11.BUTTON_PRESS_MASK)
        pump(br.window)
        drag_to(br.window, x, br.window.height + 4000)
        pump(br.window)
        eq(tab.scroll, bottom, "dragging off the bottom missed the end")
        drag_to(br.window, x, -4000)
        pump(br.window)
        eq(tab.scroll, 0, "dragging off the top missed the start")
        send(br.window, button_event(br.window, 1, x, -4000, press=False),
             x11.BUTTON_RELEASE_MASK)
        pump(br.window)


def main():
    everything = sorted(globals().items())
    pure = [v for k, v in everything if k.startswith("test_")]
    live = [v for k, v in everything if k.startswith("live_")]
    if not LIVE:
        print("SKIP the live half of test_x11.py: %s" % LIVE_REASON)
        live = []
    failed = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    for test in pure + live:
        if only and test.__name__ not in only:
            continue
        try:
            test()
            print("  ok  %s" % test.__name__, flush=True)
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print(" FAIL %s: %s" % (test.__name__, exc), flush=True)
    if failed:
        print("\n%d FAILED" % failed)
        sys.exit(1)
    print("\nALL %d X11 TESTS PASSED (%d against a live server)"
          % (len(pure) + len(live), len(live)))


if __name__ == "__main__":
    main()
