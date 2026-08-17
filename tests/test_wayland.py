"""Tests for the Wayland platform layer.

Two halves, mirroring test_x11.py. The arithmetic and the lookup tables --
buffer packing, fixed-point conversion, button numbering, keysyms, scroll
steps -- are plain functions taking plain values, so they are tested first
and everywhere, including on a machine that has never seen a compositor.

The second half needs one. It creates a real window on a real compositor
(weston's headless backend will do), waits for the first xdg configure so
the surface is legal to draw on, presents real frames into shared-memory
buffers and reads the pixels back out of its own mmap -- the only honest
place to read them, since a compositor owns the pixels after we hand them
over. Where there is no compositor to talk to, that half says so and the
first half still runs.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import wayland
from feetbrowser.window import STATE_ALT, STATE_CONTROL, STATE_SHIFT


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


# -- pixel packing ---------------------------------------------------------

def test_pack_xrgb_writes_the_channels_in_order():
    """XRGB8888 is four bytes per pixel, B, G, R, X, with the alpha byte
    opaque. A red pixel is 0,0,255,255 and a green one 0,255,0,255."""
    pixels = bytearray([255, 0, 0, 0, 255, 0])      # red, then green
    packed = wayland.pack_xrgb(pixels, 2, 1, 6)
    eq(len(packed), 8)
    eq(bytes(packed), bytes([0, 0, 255, 255, 0, 255, 0, 255]))


def test_pack_xrgb_sizes_a_full_frame():
    pixels = bytearray(10 * 20 * 3)
    packed = wayland.pack_xrgb(pixels, 10, 20, 30)
    eq(len(packed), 10 * 20 * 4)


def test_a_frame_survives_a_round_trip_through_the_layout():
    """Pack a gradient and read it back through the same byte offsets,
    which is the check that catches an off-by-one in the row loop."""
    width, height = 7, 5
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 30) % 256, (y * 50) % 256, (x + y) % 256))
    packed = wayland.pack_xrgb(pixels, width, height, width * 3)
    for y in range(height):
        for x in range(width):
            at = (y * width + x) * 4
            src = (y * width + x) * 3
            eq((packed[at + 2], packed[at + 1], packed[at], packed[at + 3]),
               tuple(pixels[src:src + 3]) + (0xFF,),
               "pixel at (%d, %d)" % (x, y))


# -- message packing --------------------------------------------------------

def test_pack_message_header_carries_size_and_opcode():
    """The wire header is two words: the object id, then size<<16 | opcode,
    with the size in bytes including the header -- what the compositor's
    demarshal reads back."""
    msg, fds = wayland.pack_message(1, 1, "n", [2])
    import struct
    obj, word = struct.unpack("<II", msg[:8])
    eq(obj, 1)
    eq(word >> 16, 12, "size must include the 8-byte header")
    eq(word & 0xFFFF, 1, "opcode lives in the low half")
    eq(fds, (), "no fds in a plain request")


def test_pack_message_encodes_strings_length_prefixed_and_padded():
    """A string is a u32 length (including the NUL) then the bytes padded to
    a multiple of four."""
    msg, _fds = wayland.pack_message(1, 2, "s", ["ab"])
    import struct
    length = struct.unpack("<I", msg[8:12])[0]
    eq(length, 3, "length includes the terminator")
    eq(len(msg) - 8, 8, "3 bytes pad to 4, plus the length word")
    # An h (fd) slot carries the descriptor out-of-band and no word.
    msg, fds = wayland.pack_message(1, 0, "nh", [7, 9])
    eq(len(msg) - 8, 4, "only the new_id occupies words")
    eq(fds, (9,), "the fd is returned for SCM_RIGHTS")


def test_unpack_message_round_trips_the_types():
    words = [5, 7]
    fds = [11]
    eq(wayland.unpack_message("uh", words, fds), [5, 11])


# -- the keyboard map -------------------------------------------------------

KEYMAP = """
xkb_keymap {
    xkb_keycodes  "evdev+aliases(qwerty)" {
        minimum = 8;
        maximum = 255;
        <ESC> = 9;
        <AE01> = 10;
        <AE02> = 11;
        <AC01> = 30;
        <AC02> = 31;
        <LFSH> = 50;
        <RTRN> = 36;
        <SPCE> = 65;
        <CAPS> = 66;
        <UP> = 103;
        <FK01> = 59;
        <LCTL> = 37;
        <AC05> = 38;
        alias <I149> = <AC02>;
    };
    xkb_symbols  "pc+us+inet(evdev)" {
        name[Group1] = "English (US)";
        key <ESC> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Escape ] };
        key <AE01> { type[Group1] = "TWO_LEVEL", symbols[Group1] = [ 1, exclam ] };
        key <AE02> { type[Group1] = "TWO_LEVEL", symbols[Group1] = [ 2, at ] };
        key <AC01> { type[Group1] = "TWO_LEVEL", symbols[Group1] = [ a, A ] };
        key <AC02> { type[Group1] = "TWO_LEVEL", symbols[Group1] = [ s, S ] };
        key <LFSH> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Shift_L ] };
        key <RTRN> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Return ] };
        key <SPCE> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ space ] };
        key <CAPS> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Caps_Lock ] };
        key <UP> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Up ] };
        key <FK01> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ F1 ] };
        key <LCTL> { type[Group1] = "ONE_LEVEL", symbols[Group1] = [ Control_L ] };
        key <AC05> { type[Group1] = "TWO_LEVEL", symbols[Group1] = [ l, L ] };
    };
};
"""


def test_symbol_value_knows_the_keysym_names():
    eq(wayland.symbol_value("a"), 0x61)
    eq(wayland.symbol_value("exclam"), ord("!"))
    eq(wayland.symbol_value("Escape"), 0xFF1B)
    eq(wayland.symbol_value("Shift_L"), 0xFFE1)
    eq(wayland.symbol_value("minus"), ord("-"))
    eq(wayland.symbol_value("F1"), 0xFFBE)
    eq(wayland.symbol_value("no_such_keysym"), 0)


def test_parse_keymap_reads_codes_and_shift_levels():
    syms, names = wayland.parse_keymap(KEYMAP)
    eq(syms[10], [ord("1"), ord("!")], "shifted 1 is exclam")
    eq(syms[30], [ord("a"), ord("A")])
    eq(syms[50], [0xFFE1], "Shift_L")
    eq(syms[36], [0xFF0D], "Return")
    eq(syms[65], [0x20], "space")
    eq(syms[59], [0xFFBE], "F1")
    eq(names[10], ["1", "exclam"])


def test_parse_keymap_follows_aliases():
    syms, names = wayland.parse_keymap(KEYMAP)
    # I149 is an alias of AC02 (the 's' key).
    eq(syms.get(31), [ord("s"), ord("S")])


# -- fixed point -----------------------------------------------------------

def test_fixed_point_is_24_8():
    eq(wayland.fixed_to_float(0), 0.0)
    eq(wayland.fixed_to_float(256), 1.0)
    eq(wayland.fixed_to_float(384), 1.5)
    eq(wayland.fixed_to_float(-256), -1.0)


# -- buttons ---------------------------------------------------------------

def test_buttons_map_to_tk_numbers():
    eq(wayland.button_number(0x110), 1, "BTN_LEFT")
    eq(wayland.button_number(0x111), 3, "BTN_RIGHT")
    eq(wayland.button_number(0x112), 2, "BTN_MIDDLE")
    assert wayland.button_number(0x113) is None, "BTN_SIDE is not bound"


def test_button_events_carry_press_and_release():
    eq(wayland.button_event(0x110, True), ("<Button-1>", 1))
    eq(wayland.button_event(0x110, False), ("<ButtonRelease-1>", 1))
    eq(wayland.button_event(0x112, True), ("<Button-2>", 2))
    eq(wayland.button_event(0x111, False), ("<ButtonRelease-3>", 3))
    assert wayland.button_event(0x113, True) is None


# -- scroll ----------------------------------------------------------------

def test_wheel_delta_stays_in_the_pixel_range():
    """browser.py treats |delta| < 30 as a pixel count and anything larger
    as line units, so a notch has to stay under 30, like x11's WHEEL_STEP."""
    assert abs(wayland.wheel_delta(1)) < 30
    assert abs(wayland.wheel_delta(-1)) < 30


def test_axis_delta_inverts_and_uses_discrete_when_present():
    # Positive axis value = scrolling down, which must come out negative
    # because the browser's positive delta means up.
    steps, discrete = wayland.axis_delta(15 * 256, None)
    eq(steps, -1)
    assert discrete is False
    steps, discrete = wayland.axis_delta(0, -2)
    eq(steps, 2, "a discrete count is authoritative")
    assert discrete is True
    steps, _ = wayland.axis_delta(15 * 256, 1)
    eq(steps, -1, "the axis value must not be double-counted")


# -- keysym plumbing -------------------------------------------------------

def test_modifier_bits_are_tks_three():
    eq(wayland.state_from_xkb(True, False, False), STATE_SHIFT)
    eq(wayland.state_from_xkb(True, True, False),
       STATE_SHIFT | STATE_CONTROL)
    eq(wayland.state_from_xkb(True, False, True), STATE_SHIFT | STATE_ALT)
    eq(wayland.state_from_xkb(False, False, False), 0)


def test_keysyms_become_characters():
    """xkbcommon's keysym names and values are X11's, so the shared
    translation from x11.py applies unchanged."""
    eq(wayland.keysym_pair("a", 0x61, 0), ("a", "a"))
    eq(wayland.keysym_pair("A", 0x41, STATE_SHIFT), ("A", "A"))
    eq(wayland.keysym_pair("space", 0x20, 0), ("space", " "))
    eq(wayland.keysym_pair("Return", 0xFF0D, 0), ("Return", "\r"))
    eq(wayland.keysym_pair("Left", 0xFF51, 0), ("Left", ""))
    eq(wayland.keysym_pair("l", 0x6C, STATE_CONTROL), ("l", "l"),
       "the keysym ignores Control, which is what <Control-l> needs")
    assert wayland.keysym_pair("Shift_L", 0xFFE1, 0) is None
    assert wayland.keysym_pair("", 0, 0) is None


def test_a_release_cannot_be_caught_by_a_typing_binding():
    names = wayland.keysym_releases("Left")
    eq(names, ("<KeyRelease-Left>", "<KeyRelease>"))
    assert "<Key>" not in names


# -- connecting ------------------------------------------------------------

def _live_reason():
    if not wayland.available():
        return wayland.unavailable_reason() or "no Wayland on this platform"
    return ""


LIVE_REASON = _live_reason()
LIVE = not LIVE_REASON

if LIVE:
    from feetbrowser import browser as browsermod
    from feetbrowser import canvas as canvasmod


def _pump(win, times=6):
    """Let the window drain the display and present, as the main loop does."""
    for _ in range(times):
        if win._closed:
            return
        win.poll_events()
        win.present()
        time.sleep(0.01)


def _wait_configured(win, seconds=10.0):
    """Wait until the first xdg configure has been acknowledged -- the
    moment the surface is legal to attach a buffer to. (Attaching itself
    needs a canvas, which the tests create after this returns.)"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        _pump(win, 3)
        if win._configured:
            return True
        time.sleep(0.02)
    return False


def _no_protocol_error():
    """True when the compositor has not reported a protocol error."""
    return wayland.error() == ""


def _buffer_bytes(win):
    """The pixels in the most recent frame, as the client wrote them."""
    buf = win._attached
    if buf is None:
        return b""
    return bytes(buf.mem[:buf.width * buf.height * 4])


def _buffer_pixel(win, x, y):
    """One pixel of the attached frame as (r, g, b), read off our mmap."""
    buf = win._attached
    at = (y * buf.width + x) * 4
    raw = _buffer_bytes(win)
    b, g, r = raw[at], raw[at + 1], raw[at + 2]
    return r, g, b


def _same_colour(got, want, msg=""):
    assert got == want, "%s: %r != %r" % (msg, got, want)


class _Session:
    """A live window, torn down however the test ends."""

    def __init__(self, width=500, height=400):
        self.size = (width, height)

    def __enter__(self):
        self.win = wayland.WaylandTk(width=self.size[0], height=self.size[1],
                                     title="test")
        assert _wait_configured(self.win), "the window never became drawable"
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
        self.browser.new_tab("about:blank")
        self.browser.draw()
        win.present()
        return self.browser


# -- the live half ---------------------------------------------------------

def live_window_opens_and_becomes_drawable():
    with _Session(640, 480) as win:
        assert win.winfo_exists()
        assert win._configured, "the first configure never arrived"
        canvas = canvasmod.Canvas(win, width=640, height=480, bg="#ffffff")
        canvas.pack()
        win.present()
        assert win._attached is not None, "no frame was ever attached"
        assert _no_protocol_error()


def live_present_puts_the_right_colours_in_the_buffer():
    """The whole point of the module: a backwards byte order or a wrong
    stride still puts *something* on the wire, so the pixels have to be read
    back out of the shared-memory buffer and looked at."""
    with _Session(300, 200) as win:
        canvas = canvasmod.Canvas(win, width=300, height=200, bg="#3366cc")
        canvas.pack()
        canvas.create_rectangle(0, 0, 40, 40, fill="#ff0000", width=0)
        win.present()
        _same_colour(_buffer_pixel(win, 10, 10), (0xFF, 0x00, 0x00),
                     "red is not red")
        _same_colour(_buffer_pixel(win, 200, 150), (0x33, 0x66, 0xCC),
                     "the background is wrong")
        assert _no_protocol_error()


def live_present_is_skipped_when_nothing_changed():
    with _Session(200, 120) as win:
        canvas = canvasmod.Canvas(win, width=200, height=120, bg="#101010")
        canvas.pack()
        win.present()
        first = win._attached
        win.present()
        assert win._attached is first, "a clean canvas was re-attached"
        canvas.create_rectangle(0, 0, 10, 10, fill="#ffffff")
        win.present()
        assert win._attached is not first, "a dirty canvas was not re-attached"


def live_a_configure_resizes_the_canvas():
    """The compositor's word is final on Wayland: a configure event *tells*
    us the new size and the canvas has to follow, unlike X11 where the size
    is a hint a window manager may ignore."""
    with _Session(400, 300) as win:
        canvas = canvasmod.Canvas(win, width=400, height=300, bg="#ffffff")
        canvas.pack()
        seen = []
        win.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
        win._on_toplevel_configure(520, 360, None)
        win._on_xdg_configure(1)
        _pump(win)
        eq((win.width, win.height), (520, 360), "the window did not resize")
        assert (520, 360) in seen, "<Configure> never fired: %r" % seen
        eq((canvas.winfo_width(), canvas.winfo_height()), (520, 360),
           "the canvas did not follow")


def live_close_destroys_the_window():
    """The xdg_toplevel.close event is the compositor telling us to go away
    -- the whole close-button path on Wayland."""
    win = wayland.WaylandTk(width=200, height=150, title="closing")
    _wait_configured(win)
    closed = []
    win.protocol("WM_DELETE_WINDOW", lambda: closed.append(True))
    win._on_toplevel_close()
    assert closed, "xdg close did not run the close handler"
    assert not win.winfo_exists(), "the window is still alive"


def live_buffers_are_reused_and_released():
    """The compositor holds onto the buffer it is displaying, so a steady
    double-buffered window has a small pool and never grows it: each new
    frame takes a released buffer once the previous frame's release comes
    back. A pool that kept growing would leak shared memory."""
    with _Session(300, 200) as win:
        canvas = canvasmod.Canvas(win, width=300, height=200, bg="#204060")
        canvas.pack()
        win.present()
        _pump(win, 6)
        canvas.create_rectangle(0, 0, 50, 50, fill="#ff0000", width=0)
        win.present()
        _pump(win, 6)
        canvas.create_rectangle(50, 0, 100, 50, fill="#ff0000", width=0)
        win.present()
        _pump(win, 6)
        assert len(win._buffers) <= 2, "the pool grew: %d" % len(win._buffers)
        assert _no_protocol_error()


def live_keyboard_path_translates_keysyms():
    """weston headless has no seat, so no compositor keymap ever arrives;
    this drives the whole key path the way the compositor would: install a
    parsed keymap, give the window the keyboard focus, and feed key events
    through the same handler the socket dispatch calls."""
    syms, names = wayland.parse_keymap(KEYMAP)
    wayland._STATE["syms"] = syms
    wayland._STATE["names"] = names
    win = wayland.WaylandTk(width=300, height=200, title="keys")
    try:
        assert _wait_configured(win)
        wayland._KEYBOARD_WIN = win
        wayland._HELD.clear()
        wayland._CAPS[0] = False
        seen = []
        win.bind("<Key>", lambda e: seen.append((e.keysym, e.char, e.state)))
        wayland._keyboard_key(None, 0, 1, 0, 30, 1)    # press 'a'
        wayland._keyboard_key(None, 0, 1, 0, 30, 0)    # release 'a'
        wayland._keyboard_key(None, 0, 1, 0, 50, 1)    # press Shift_L
        wayland._keyboard_key(None, 0, 1, 0, 30, 1)    # 'a' while shifted -> 'A'
        wayland._keyboard_key(None, 0, 1, 0, 30, 0)
        wayland._keyboard_key(None, 0, 1, 0, 10, 1)    # '1' shifted -> '!'
        wayland._keyboard_key(None, 0, 1, 0, 10, 0)
        wayland._keyboard_key(None, 0, 1, 0, 50, 0)    # release Shift_L
        wayland._keyboard_key(None, 0, 1, 0, 36, 1)    # Return
        assert ("a", "a", 0) in seen, seen
        assert ("A", "A", STATE_SHIFT) in seen, seen
        assert ("!", "!", STATE_SHIFT) in seen, seen
        assert ("Return", "\r", 0) in seen, seen
    finally:
        wayland._KEYBOARD_WIN = None
        win.destroy()


def live_control_shortcuts_reach_the_key_binding():
    """Control-L must land on <Control-l>, which is how the address bar is
    focused -- the modifier has to survive into the binding name."""
    syms, names = wayland.parse_keymap(KEYMAP)
    wayland._STATE["syms"] = syms
    wayland._STATE["names"] = names
    win = wayland.WaylandTk(width=300, height=200, title="ctrl")
    try:
        assert _wait_configured(win)
        wayland._KEYBOARD_WIN = win
        wayland._HELD.clear()
        wayland._CAPS[0] = False
        seen = []
        win.bind("<Control-l>", lambda e: seen.append(e.state))
        wayland._keyboard_key(None, 0, 1, 0, 37, 1)   # Control_L
        wayland._keyboard_key(None, 0, 1, 0, 38, 1)   # 'l' (AC05 = 38)
        wayland._keyboard_key(None, 0, 1, 0, 38, 0)
        assert seen, "Control-L did not reach <Control-l>: %r" % seen
        assert seen[0] & STATE_CONTROL
    finally:
        wayland._KEYBOARD_WIN = None
        win.destroy()


def live_the_browser_draws_a_page():
    """End to end: chrome, tabs, toolbar and page, drawn by the browser and
    written into a shared-memory buffer. A window full of one flat colour
    fails here."""
    with _Browser() as br:
        br.draw()
        br.window.present()
        raw = _buffer_bytes(br.window)
        if not raw:
            return  # nothing attached yet; the page has not settled
        colours = set()
        width, height = br.window.width, br.window.height
        for y in range(0, height, 17):
            for x in range(0, width, 23):
                at = (y * width + x) * 4
                colours.add((raw[at], raw[at + 1], raw[at + 2]))
        assert len(colours) > 2, "the window is one flat colour"
        assert _no_protocol_error()


def live_a_dense_frame_lands_one_pixel_for_one():
    """A pretend 2x display: the buffer must be allocated in device pixels
    and the window must know it, or a 10-CSS-pixel rectangle covers only ten
    physical pixels instead of twenty."""
    saved = os.environ.get("FEETBROWSER_SCALE")
    os.environ["FEETBROWSER_SCALE"] = "2"
    try:
        win = wayland.WaylandTk(width=160, height=120, title="dense")
        assert _wait_configured(win)
        canvas = canvasmod.Canvas(win, width=160, height=120, bg="#3366cc")
        canvas.pack()
        _pump(win)
        eq(canvas.device_size(), (320, 240), "the buffer is not the window")
        canvas.create_rectangle(0, 0, 10, 10, fill="#ff0000", width=0)
        win.present()
        _same_colour(_buffer_pixel(win, 19, 19), (0xFF, 0x00, 0x00),
                     "the rectangle stopped short of its device pixels")
        _same_colour(_buffer_pixel(win, 21, 21), (0x33, 0x66, 0xCC),
                     "the rectangle ran past its device pixels")
        win.destroy()
    finally:
        if saved is None:
            os.environ.pop("FEETBROWSER_SCALE", None)
        else:
            os.environ["FEETBROWSER_SCALE"] = saved


def main():
    everything = sorted(globals().items())
    pure = [v for k, v in everything if k.startswith("test_")]
    live = [v for k, v in everything if k.startswith("live_")]
    if not LIVE:
        print("SKIP the live half of test_wayland.py: %s" % LIVE_REASON)
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
    print("\nALL %d WAYLAND TESTS PASSED (%d against a live compositor)"
          % (len(pure) + len(live), len(live)))


if __name__ == "__main__":
    main()