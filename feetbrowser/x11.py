"""A real X11 window, with no toolkit and no bindings package.

Xlib is a C library, so ctypes is all it takes to drive it: open the display,
declare the signatures, call the functions. No python-xlib, no PyGObject, no
compiled shim -- the same rule ``cocoa.py`` follows, and for the same reason.

What this adds to ``window.Window`` is the two things a headless window cannot
have: real input (``poll_events``) and somewhere to put the pixels
(``present``). Everything above it still sees Tk's API, because translation
happens here -- ``ButtonPress`` and keysyms become ``<Button-1>``,
``<Control-l>``, ``<MouseWheel>``. Rather less translation than the other two
backends need, as it turns out: Tk's keysym names and its ``event.state`` bits
are X11's, so those two tables are mostly the identity.

Presenting costs one conversion, and which one depends on the server. A
``Surface`` is 24-bit RGB with no row padding; X wants a Z-format image in the
server's own pixel layout, which on a normal 24-bit TrueColor visual is 32
bits per pixel with the channels in whatever order the server's byte order
implies. So the visual's masks and ``XImageByteOrder`` are read at startup and
turned into byte offsets, and the frame is assembled with three strided slice
assignments, which run in C. On the rare server whose format is exactly ours,
the framebuffer goes out with no copy at all.

``XShmPutImage`` is deliberately not used. The plain path works everywhere,
including over SSH forwarding and against a server on another machine, which
is where a browser you can run on anything wants to be.

Everything here that is arithmetic or a lookup table lives in a module-level
function, so the part that can only be exercised against a running X server is
as small as it can be made -- see ``tests/test_x11.py`` for both halves.
"""
import ctypes
import ctypes.util
import os
import time

from . import asmx11
from .window import (QUIET, STATE_ALT, STATE_CONTROL, STATE_SHIFT, Event,
                     Window, key_sequences)

# -- types -----------------------------------------------------------------
#
# Spelled out rather than guessed at. An XID is an unsigned long, which is 8
# bytes on a 64-bit Linux and 4 on a 32-bit one; ctypes' c_ulong is right on
# both, and getting it wrong is a wild window handle rather than an error.
XID = ctypes.c_ulong
Atom = ctypes.c_ulong
Time = ctypes.c_ulong
KeySym = ctypes.c_ulong
VisualID = ctypes.c_ulong
Bool = ctypes.c_int
Status = ctypes.c_int
Display = ctypes.c_void_p
GC = ctypes.c_void_p
VisualPtr = ctypes.c_void_p


class Visual(ctypes.Structure):
    """The server's description of a pixel layout.

    Only the three masks are read, but the fields before them have to be
    declared or the offsets are wrong -- which reads as a plausible-looking
    mask and a window full of the wrong colours.
    """

    _fields_ = [("ext_data", ctypes.c_void_p), ("visualid", VisualID),
                ("c_class", ctypes.c_int), ("red_mask", ctypes.c_ulong),
                ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
                ("bits_per_rgb", ctypes.c_int),
                ("map_entries", ctypes.c_int)]


class XPixmapFormatValues(ctypes.Structure):
    _fields_ = [("depth", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("scanline_pad", ctypes.c_int)]


class XImageFuncs(ctypes.Structure):
    """XImage's vtable. Never called from here, but it is part of the struct
    and XCreateImage fills it in, so it has to occupy its six pointers."""

    _fields_ = [("create_image", ctypes.c_void_p),
                ("destroy_image", ctypes.c_void_p),
                ("get_pixel", ctypes.c_void_p),
                ("put_pixel", ctypes.c_void_p),
                ("sub_image", ctypes.c_void_p),
                ("add_pixel", ctypes.c_void_p)]


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
                ("f", XImageFuncs)]


class XSizeHints(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_long), ("x", ctypes.c_int),
                ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("min_width", ctypes.c_int),
                ("min_height", ctypes.c_int), ("max_width", ctypes.c_int),
                ("max_height", ctypes.c_int), ("width_inc", ctypes.c_int),
                ("height_inc", ctypes.c_int),
                ("min_aspect_x", ctypes.c_int), ("min_aspect_y", ctypes.c_int),
                ("max_aspect_x", ctypes.c_int), ("max_aspect_y", ctypes.c_int),
                ("base_width", ctypes.c_int), ("base_height", ctypes.c_int),
                ("win_gravity", ctypes.c_int)]


class XSetWindowAttributes(ctypes.Structure):
    # Declared in full even though only one field is ever set: the value mask
    # tells the server which members to read, and it counts them by their
    # offset in this struct. A short version puts `override_redirect` at the
    # wrong offset and the server reads whatever happens to be there.
    _fields_ = [("background_pixmap", XID), ("background_pixel",
                ctypes.c_ulong), ("border_pixmap", XID),
                ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", Bool),
                ("event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", Bool), ("colormap", XID),
                ("cursor", XID)]


CW_OVERRIDE_REDIRECT = 1 << 9


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", Display),
                ("resourceid", XID), ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte),
                ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


# The event structures. Every one of them starts with the same five fields,
# which is what makes XAnyEvent a usable view over any of them.
_HEAD = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
         ("send_event", Bool), ("display", Display), ("window", XID)]


class XAnyEvent(ctypes.Structure):
    _fields_ = list(_HEAD)


class XKeyEvent(ctypes.Structure):
    _fields_ = _HEAD + [
        ("root", XID), ("subwindow", XID), ("time", Time),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("keycode", ctypes.c_uint),
        ("same_screen", Bool)]


class XButtonEvent(ctypes.Structure):
    _fields_ = _HEAD + [
        ("root", XID), ("subwindow", XID), ("time", Time),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("button", ctypes.c_uint),
        ("same_screen", Bool)]


class XMotionEvent(ctypes.Structure):
    _fields_ = _HEAD + [
        ("root", XID), ("subwindow", XID), ("time", Time),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("is_hint", ctypes.c_char),
        ("same_screen", Bool)]


class XExposeEvent(ctypes.Structure):
    _fields_ = _HEAD + [
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
        ("height", ctypes.c_int), ("count", ctypes.c_int)]


class XConfigureEvent(ctypes.Structure):
    # Two window fields, not one: `window` here is the *event* window and the
    # configured one follows it, so the shared header's `window` is the wrong
    # field to read a resize from.
    _fields_ = _HEAD + [
        ("configured", XID), ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("above", XID),
        ("override_redirect", Bool)]


class XClientMessageData(ctypes.Union):
    _fields_ = [("b", ctypes.c_char * 20), ("s", ctypes.c_short * 10),
                ("l", ctypes.c_long * 5)]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = _HEAD + [("message_type", Atom), ("format", ctypes.c_int),
                        ("data", XClientMessageData)]


class XSelectionRequestEvent(ctypes.Structure):
    _fields_ = _HEAD[:4] + [
        ("owner", XID), ("requestor", XID), ("selection", Atom),
        ("target", Atom), ("property", Atom), ("time", Time)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = _HEAD[:4] + [
        ("requestor", XID), ("selection", Atom), ("target", Atom),
        ("property", Atom), ("time", Time)]


class XSelectionClearEvent(ctypes.Structure):
    _fields_ = _HEAD + [("selection", Atom), ("time", Time)]


class XEvent(ctypes.Union):
    """The union every event arrives in. The padding is not optional: Xlib
    writes a full 24 longs into whatever XNextEvent is handed."""

    _fields_ = [("type", ctypes.c_int), ("xany", XAnyEvent),
                ("xkey", XKeyEvent), ("xbutton", XButtonEvent),
                ("xmotion", XMotionEvent), ("xexpose", XExposeEvent),
                ("xconfigure", XConfigureEvent),
                ("xclient", XClientMessageEvent),
                ("xselection", XSelectionEvent),
                ("xselectionrequest", XSelectionRequestEvent),
                ("xselectionclear", XSelectionClearEvent),
                ("pad", ctypes.c_long * 24)]


ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, Display,
                                 ctypes.POINTER(XErrorEvent))

# -- constants -------------------------------------------------------------
#
# Named here rather than inline so the calls below read like the Xlib they
# are.

# Event types.
KEY_PRESS, KEY_RELEASE = 2, 3
BUTTON_PRESS, BUTTON_RELEASE = 4, 5
MOTION_NOTIFY = 6
EXPOSE = 12
DESTROY_NOTIFY = 17
CONFIGURE_NOTIFY = 22
SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 29, 30, 31
CLIENT_MESSAGE = 33

# Event masks.
KEY_PRESS_MASK = 1 << 0
KEY_RELEASE_MASK = 1 << 1
BUTTON_PRESS_MASK = 1 << 2
BUTTON_RELEASE_MASK = 1 << 3
POINTER_MOTION_MASK = 1 << 6
EXPOSURE_MASK = 1 << 15
STRUCTURE_NOTIFY_MASK = 1 << 17
EVENT_MASK = (KEY_PRESS_MASK | KEY_RELEASE_MASK | BUTTON_PRESS_MASK
              | BUTTON_RELEASE_MASK | POINTER_MOTION_MASK | EXPOSURE_MASK
              | STRUCTURE_NOTIFY_MASK)

# Images, properties and selections.
Z_PIXMAP = 2
LSB_FIRST, MSB_FIRST = 0, 1
PROP_MODE_REPLACE = 0
ANY_PROPERTY_TYPE = 0
CURRENT_TIME = 0
XA_PRIMARY, XA_ATOM, XA_STRING = 1, 4, 31
P_MIN_SIZE = 1 << 4
P_SIZE = 1 << 3

# How long to wait for another application to hand over the clipboard before
# giving up. A wedged owner is not allowed to wedge the browser.
SELECTION_TIMEOUT = 0.5

# Pixels per notch of the scroll wheel. browser.py treats ``|delta| < 30`` as
# a pixel count and anything larger as line units, so a notch has to land well
# under 30; 20 is what the other two backends send for one line.
WHEEL_STEP = 20

# The soname first, because that is what a distro actually ships -- the bare
# `libX11.so` is in the -dev package and is missing on most installed systems.
SONAMES = ("libX11.so.6", "libX11.so", "libX11.6.dylib", "libX11.dylib")

# How hard to try to connect before deciding there is nothing to connect to:
# six attempts with the wait doubling from 10 ms, so 0.31 s in all. See
# `_connect` for why a refusal is worth doubting and why the budget is small.
_CONNECT_ATTEMPTS = 6
_CONNECT_PAUSE = 0.01

# Tk cursor names -> the standard X cursor font shapes.
CURSORS = {
    "": 68,             # XC_left_ptr
    "arrow": 68,
    "hand2": 60,        # XC_hand2
    "hand1": 58,        # XC_hand1
    "xterm": 152,       # XC_xterm
    "watch": 150,       # XC_watch
}

# Keysyms that are a modifier being held rather than a keypress. X reports
# these as ordinary KeyPress events; Cocoa and Win32 do not, and browser.py
# would see a stream of phantom keys if they were passed on.
MODIFIER_KEYSYMS = frozenset((
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Caps_Lock",
    "Shift_Lock", "Meta_L", "Meta_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
    "Hyper_L", "Hyper_R", "Num_Lock", "Mode_switch", "ISO_Level3_Shift",
    "ISO_Level5_Shift", "ISO_Group_Shift",
))

# The keypad keys that produce a character. Everything else printable is
# either Latin-1 (where the keysym *is* the codepoint) or in the 0x01000000
# Unicode range, and needs no table at all.
KEYPAD_CHARS = {
    0xFF80: " ", 0xFFAA: "*", 0xFFAB: "+", 0xFFAD: "-", 0xFFAE: ".",
    0xFFAF: "/", 0xFFBD: "=",
}
KEYPAD_CHARS.update({0xFFB0 + n: chr(ord("0") + n) for n in range(10)})

# Motion state bits -> the button they mean.
BUTTON_MASKS = ((1 << 8, 1), (1 << 9, 2), (1 << 10, 3))


class X11Unavailable(RuntimeError):
    """Raised when this platform cannot supply an X11 window."""


# Window XID -> X11Window. There is one event queue per *display connection*,
# not per window, so whoever pumps drains events for everybody and routes each
# one to the window it belongs to. That is what lets the root's main loop feed
# a popup Toplevel, which is how the browser has always worked.
_WINDOWS = {}

_libs = {}
_state = {}     # everything the open display gave us: screen, visual, atoms
_problem = ""


# -- pure helpers ----------------------------------------------------------
#
# No ctypes below this line until the display itself. These are the parts a
# test can reach with no X server anywhere.

class PixelFormat:
    """How one X server wants a Z-format image laid out.

    Six numbers, all of them read from the server rather than assumed: two
    machines with the same depth can still disagree about bits per pixel, row
    padding and byte order, and every one of those disagreements looks like a
    different flavour of scrambled window.
    """

    __slots__ = ("depth", "bits_per_pixel", "scanline_pad", "byte_order",
                 "red_mask", "green_mask", "blue_mask")

    def __init__(self, depth, bits_per_pixel, scanline_pad, byte_order,
                 red_mask, green_mask, blue_mask):
        self.depth = depth
        self.bits_per_pixel = bits_per_pixel
        self.scanline_pad = scanline_pad
        self.byte_order = byte_order
        self.red_mask = red_mask
        self.green_mask = green_mask
        self.blue_mask = blue_mask

    def __repr__(self):
        return ("PixelFormat(depth=%d, bpp=%d, pad=%d, %s, "
                "masks=%#x/%#x/%#x)"
                % (self.depth, self.bits_per_pixel, self.scanline_pad,
                   "LSBFirst" if self.byte_order == LSB_FIRST else "MSBFirst",
                   self.red_mask, self.green_mask, self.blue_mask))


def scanline_bytes(width, bits_per_pixel, scanline_pad):
    """Bytes per row of a Z-format image, padded the way the server asks.

    Padding is per *row*, so a width that does not divide evenly leaves slack
    bytes at the end of every line. Ignoring them offsets each row by a little
    more than the last, which is the classic diagonal smear.
    """
    pad = max(8, int(scanline_pad))
    return ((int(width) * int(bits_per_pixel) + pad - 1) // pad) * pad // 8


def mask_byte(mask, pixel_bytes, byte_order):
    """Which stored byte a channel mask occupies, or None if it is not one.

    A TrueColor mask covering exactly one whole byte -- 0xFF0000 and friends
    -- means the channel *is* a byte of the stored pixel, and the only
    question left is which one. The mask describes the pixel as a number, so
    an LSBFirst server stores its lowest byte first and an MSBFirst server
    stores it last; that reversal is the whole of the endianness handling.
    """
    for index in range(pixel_bytes):
        if mask == 0xFF << (8 * index):
            if byte_order == LSB_FIRST:
                return index
            return pixel_bytes - 1 - index
    return None


def byte_layout(fmt):
    """(pixel_bytes, r_off, g_off, b_off), or None for an exotic format.

    Every TrueColor server anyone is likely to meet lands here: depth 24 or 32
    with whole-byte channels, at 32 or 24 bits per pixel. Anything else --
    depth 15 and 16, where a channel is five or six bits -- has to go the slow
    way instead.
    """
    if fmt.bits_per_pixel not in (24, 32):
        return None
    pixel_bytes = fmt.bits_per_pixel // 8
    offsets = [mask_byte(mask, pixel_bytes, fmt.byte_order) for mask
               in (fmt.red_mask, fmt.green_mask, fmt.blue_mask)]
    if None in offsets or len(set(offsets)) != 3:
        return None
    return (pixel_bytes,) + tuple(offsets)


def channel_table(mask):
    """An 8-bit channel value -> its bits in place, for all 256 values.

    The scaling matters on the narrow visuals: five bits of red means 0xFF has
    to become 31 and 0x00 has to become 0, with everything in between spread
    evenly, or white comes out grey.
    """
    if not mask:
        return [0] * 256
    shift = (mask & -mask).bit_length() - 1
    top = mask >> shift
    return [((value * top) // 255) << shift for value in range(256)]


def contiguous_rgb(pixels, width, height, stride):
    """The framebuffer with any row padding removed.

    A ``Surface`` never has any -- its stride is always ``width * 3`` -- so
    this is the identity in practice, and exists for a caller that hands us a
    cropped view of a larger buffer.
    """
    if stride == width * 3:
        return pixels
    packed = bytearray(width * height * 3)
    for row in range(height):
        packed[row * width * 3:(row + 1) * width * 3] = \
            pixels[row * stride:row * stride + width * 3]
    return packed


def pack_pixels(pixels, width, height, stride, fmt):
    """A Surface's RGB bytes as one Z-format image. Returns (data, stride).

    The returned buffer is the Surface's own when the server's format happens
    to be exactly ours -- 24 bits per pixel, red first, no row padding -- so a
    big-endian server, which is the one that gets that layout, presents with
    no conversion at all. Every other server pays three strided slice
    assignments, which run in C.
    """
    line = scanline_bytes(width, fmt.bits_per_pixel, fmt.scanline_pad)
    layout = byte_layout(fmt)
    if layout is None:
        return _pack_generic(pixels, width, height, stride, fmt, line), line
    pixel_bytes, r_off, g_off, b_off = layout
    if (pixel_bytes == 3 and (r_off, g_off, b_off) == (0, 1, 2)
            and stride == width * 3 and line == width * 3):
        return pixels, line
    rgb = contiguous_rgb(pixels, width, height, stride)
    out = bytearray(line * height)
    if line == width * pixel_bytes:
        # No padding, so the whole frame is three strides over one buffer.
        out[r_off::pixel_bytes] = rgb[0::3]
        out[g_off::pixel_bytes] = rgb[1::3]
        out[b_off::pixel_bytes] = rgb[2::3]
        return out, line
    span = width * pixel_bytes
    for row in range(height):
        src = row * width * 3
        dst = row * line
        end = src + width * 3
        for offset, channel in ((r_off, 0), (g_off, 1), (b_off, 2)):
            out[dst + offset:dst + span:pixel_bytes] = rgb[src + channel:end:3]
    return out, line


def _pack_generic(pixels, width, height, stride, fmt, line):
    """The slow path: any TrueColor layout at all, a pixel at a time.

    Depth 15 and 16 are what this is for, and on anything made this century
    nothing reaches it. The packing loop itself is a raw-assembly kernel on
    Linux/x86-64 (``asmx11``) and a byte-identical Python loop elsewhere;
    either way a window on such a server works, and feels like it is working
    hard.
    """
    pixel_bytes = (fmt.bits_per_pixel + 7) // 8
    out = bytearray(line * height)
    asmx11.pack_rows(pixels, out, width, height, stride, line,
                     channel_table(fmt.red_mask),
                     channel_table(fmt.green_mask),
                     channel_table(fmt.blue_mask),
                     pixel_bytes, fmt.byte_order != LSB_FIRST)
    return out


def modifier_state(x_state):
    """Tk's event.state bits from an X11 modifier mask.

    They are the same three bits, because Tk's state field on X11 *is* the X
    modifier mask -- this is where 0x1, 0x4 and 0x8 came from in the first
    place. Masking rather than passing the value straight through drops the
    lock and button bits, which the other two backends cannot produce and
    browser.py does not read.
    """
    return int(x_state) & (STATE_SHIFT | STATE_CONTROL | STATE_ALT)


def wheel_delta(button):
    """Pixels to scroll for one press of an X11 wheel button, or 0.

    X11 has no scroll event: the wheel is buttons 4 (up) and 5 (down), one
    press and release per notch. Buttons 6 and 7 are the horizontal wheel,
    which nothing above binds, so they scroll nothing rather than scrolling
    the page sideways by accident.
    """
    if button == 4:
        return WHEEL_STEP
    if button == 5:
        return -WHEEL_STEP
    return 0


def button_binding(button, pressed):
    """(binding name, button number) for an X11 button, or None.

    No renumbering: Tk's button numbers are X11's, left to right, because Tk
    took them from here.
    """
    if button not in (1, 2, 3):
        return None
    prefix = "Button" if pressed else "ButtonRelease"
    return "<%s-%d>" % (prefix, button), button


def motion_binding(x_state):
    """(binding name, button number) for a pointer move.

    A drag is ``<B1-Motion>`` rather than ``<Motion>``, and which one it is
    lives in the event's state mask -- X sends one event type for both.
    """
    for mask, num in BUTTON_MASKS:
        if x_state & mask:
            return "<B%d-Motion>" % num, num
    return "<Motion>", 0


def keysym_unicode(keysym):
    """The character a keysym stands for, or None if it stands for none.

    Two ranges and a small table cover everything: a keysym below 0x100 is its
    own Latin-1 codepoint, one above 0x01000000 is its Unicode codepoint plus
    that offset, and the keypad is neither.
    """
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if 0x01000100 <= keysym <= 0x0110FFFF:
        return chr(keysym - 0x01000000)
    return KEYPAD_CHARS.get(keysym)


def keysym_event(name, keysym, state):
    """(keysym, char) for a key, or None when it is not an event at all.

    Much less work than the other backends need. X11 keysym *names* are where
    Tk's came from -- ``Return``, ``Left``, ``ISO_Left_Tab`` are spelled
    identically -- and XLookupString has already applied the user's keyboard
    layout, so a shifted 'a' arrives as the keysym 'A' exactly as Tk reports
    it. What is left is Tk's two habits: a printable key is named by its
    character, and a modifier held down on its own is not a keypress.
    """
    if not name or name in MODIFIER_KEYSYMS:
        return None
    if name in ("Return", "KP_Enter"):
        return name, "\r"
    if name == "Tab" and state & STATE_SHIFT:
        # Most servers hand back ISO_Left_Tab for a shifted Tab already; the
        # ones that do not still have to reach <Control-ISO_Left_Tab>, which
        # is what browser.py binds for previous-tab.
        return "ISO_Left_Tab", ""
    char = keysym_unicode(keysym)
    if char is None or not char.isprintable():
        return name, ""
    if char == " ":
        return "space", " "
    return char, char


def key_release_sequences(keysym):
    """Binding names for a key going back up, most specific first.

    Deliberately not run through ``key_sequences``: a release is not a
    keypress, and nothing above should be able to catch one with a plain
    ``<Key>`` binding meant for typing.
    """
    return ("<KeyRelease-%s>" % keysym, "<KeyRelease>")


# X asserts no opinion about how big a pixel is, so 96 is the convention
# everything else in the ecosystem measures against: Xft.dpi of 192 means a
# 2x display, and a toolkit that scales does it by that ratio.
BASELINE_DPI = 96.0


def xft_dpi(resources):
    """The ``Xft.dpi`` setting out of an X resource database dump, or None.

    This is where a scale factor lives on X11. There is no protocol request
    for "how dense is this display": RANDR reports a physical size in
    millimetres, which is a different question and famously a lie on half the
    monitors that answer it, and a laptop panel at 141 real DPI is still meant
    to be drawn at 1x. What actually decides is a setting the desktop
    environment writes into the root window's resource database, and every
    toolkit reads the same one, so a browser that reads it agrees with the
    rest of the session by construction.

    The database is a flat ``name:\\tvalue`` per line. Lines are matched on
    the exact resource name -- ``Xft.dpi`` and nothing else, since a wildcard
    like ``*dpi`` would also catch settings belonging to some other program.
    A value that is missing, blank or not a number is no answer rather than a
    wrong one, and the caller falls back.
    """
    for line in (resources or "").splitlines():
        name, sep, value = line.partition(":")
        if not sep or name.strip() != "Xft.dpi":
            continue
        try:
            dpi = float(value.strip())
        except ValueError:
            continue
        if dpi > 0:
            return dpi
    return None


def net_wm_icon(width, height, rgba):
    """The CARDINAL array ``_NET_WM_ICON`` wants, from a decoded PNG.

    The property is a width, a height and then one ``0xAARRGGBB`` pixel per
    RGBA quadruple, all as 32-bit cardinals, which is the format that has to
    be spelled out here because ctypes has no unsigned 32-bit by default.
    """
    pixels = [width, height]
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i + 4]
        pixels.append((a << 24) | (r << 16) | (g << 8) | b)
    return pixels


# -- loading ---------------------------------------------------------------

def _load():
    """Open libX11 and declare every signature. Idempotent.

    Declaring signatures is not optional. ctypes defaults a return type to
    ``c_int``, which truncates a 64-bit ``Display *`` to a wild pointer -- the
    same class of bug that shipped once in the Cocoa backend as a segfault on
    the first frame, and here it would be a segfault on a machine none of us
    has.
    """
    if _libs:
        return
    names = list(SONAMES)
    found = ctypes.util.find_library("X11")
    if found:
        names.append(found)
    problem = None
    for name in names:
        try:
            # use_errno so a failed connection can say *why* it failed:
            # ctypes swaps the thread's errno around each call, and without
            # this the value read afterwards is whatever ctypes did last.
            _libs["x11"] = ctypes.CDLL(name, use_errno=True)
            break
        except OSError as exc:
            problem = exc
    else:
        raise X11Unavailable("cannot load libX11 (tried %s): %s"
                             % (", ".join(names), problem))
    _declare()


def _declare():
    x11 = _libs["x11"]
    void = ctypes.c_void_p
    cint, cuint, clong = ctypes.c_int, ctypes.c_uint, ctypes.c_long
    event = ctypes.POINTER(XEvent)
    signatures = [
        ("XOpenDisplay", Display, [ctypes.c_char_p]),
        ("XCloseDisplay", cint, [Display]),
        ("XDefaultScreen", cint, [Display]),
        ("XRootWindow", XID, [Display, cint]),
        ("XDefaultVisual", VisualPtr, [Display, cint]),
        ("XDefaultDepth", cint, [Display, cint]),
        ("XBlackPixel", ctypes.c_ulong, [Display, cint]),
        ("XWhitePixel", ctypes.c_ulong, [Display, cint]),
        ("XImageByteOrder", cint, [Display]),
        ("XListPixmapFormats", ctypes.POINTER(XPixmapFormatValues),
         [Display, ctypes.POINTER(cint)]),
        ("XCreateSimpleWindow", XID,
         [Display, XID, cint, cint, cuint, cuint, cuint, ctypes.c_ulong,
          ctypes.c_ulong]),
        ("XDestroyWindow", cint, [Display, XID]),
        ("XChangeWindowAttributes", cint,
         [Display, XID, ctypes.c_ulong,
          ctypes.POINTER(XSetWindowAttributes)]),
        ("XSelectInput", cint, [Display, XID, clong]),
        ("XMapWindow", cint, [Display, XID]),
        ("XUnmapWindow", cint, [Display, XID]),
        ("XRaiseWindow", cint, [Display, XID]),
        ("XLowerWindow", cint, [Display, XID]),
        ("XResizeWindow", cint, [Display, XID, cuint, cuint]),
        ("XStoreName", cint, [Display, XID, ctypes.c_char_p]),
        ("XSetWMNormalHints", cint,
         [Display, XID, ctypes.POINTER(XSizeHints)]),
        ("XSetWMProtocols", Status,
         [Display, XID, ctypes.POINTER(Atom), cint]),
        ("XInternAtom", Atom, [Display, ctypes.c_char_p, Bool]),
        ("XCreateGC", GC, [Display, XID, ctypes.c_ulong, void]),
        ("XFreeGC", cint, [Display, GC]),
        ("XCreateImage", ctypes.POINTER(XImage),
         [Display, VisualPtr, cuint, cint, cint, ctypes.c_char_p, cuint,
          cuint, cint, cint]),
        ("XPutImage", cint,
         [Display, XID, GC, ctypes.POINTER(XImage), cint, cint, cint, cint,
          cuint, cuint]),
        ("XPending", cint, [Display]),
        ("XNextEvent", cint, [Display, event]),
        ("XCheckTypedWindowEvent", Bool, [Display, XID, cint, event]),
        ("XSendEvent", Status, [Display, XID, Bool, clong, event]),
        ("XFlush", cint, [Display]),
        ("XSync", cint, [Display, Bool]),
        ("XLookupString", cint,
         [ctypes.POINTER(XKeyEvent), ctypes.c_char_p, cint,
          ctypes.POINTER(KeySym), void]),
        ("XLookupKeysym", KeySym, [ctypes.POINTER(XKeyEvent), cint]),
        ("XKeysymToString", ctypes.c_char_p, [KeySym]),
        ("XCreateFontCursor", XID, [Display, cuint]),
        ("XDefineCursor", cint, [Display, XID, XID]),
        ("XFreeCursor", cint, [Display, XID]),
        ("XSetSelectionOwner", cint, [Display, Atom, XID, Time]),
        ("XGetSelectionOwner", XID, [Display, Atom]),
        ("XConvertSelection", cint, [Display, Atom, Atom, Atom, XID, Time]),
        ("XChangeProperty", cint,
         [Display, XID, Atom, Atom, cint, cint, ctypes.c_char_p, cint]),
        ("XGetWindowProperty", cint,
         [Display, XID, Atom, clong, clong, Bool, Atom,
          ctypes.POINTER(Atom), ctypes.POINTER(cint),
          ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
          ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]),
        ("XSetErrorHandler", void, [ERROR_HANDLER]),
        ("XResourceManagerString", ctypes.c_char_p, [Display]),
        ("XFree", cint, [void]),
    ]
    for name, restype, argtypes in signatures:
        fn = getattr(x11, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _on_x_error(_display, error):
    """Swallow X errors instead of letting Xlib call exit().

    Xlib's default handler prints and terminates the process, which is an
    absurd response to, say, a stale request against a window the user just
    closed. Errors arrive asynchronously, so there is rarely anything useful
    to do with one beyond not dying.
    """
    _state["last_error"] = int(error.contents.error_code)
    return 0


def _display_socket(name):
    """The unix socket a display name refers to, or "" if it names none.

    ``:99`` and ``unix:99.0`` are the server on this machine; ``host:0`` and
    anything with a transport in front of it are not, and this says nothing
    about those rather than inventing a path they would never use.
    """
    head, _, rest = name.partition(":")
    if not rest or head not in ("", "unix"):
        return ""
    number = rest.split(".")[0]
    if not number.isdigit():
        return ""
    return "/tmp/.X11-unix/X%s" % number


def _connect(attempts=_CONNECT_ATTEMPTS, pause=_CONNECT_PAUSE):
    """Open a connection, allowing for a server that refuses one for a moment.

    XOpenDisplay returning NULL does not mean there is no server. It also
    covers a server that has one, briefly: a connection arriving while the
    listening socket's backlog is full, or during the moment a server that has
    just been started is not yet answering. tests/test_x11.py opens and closes
    about thirty connections a second against one Xvfb, and CI has seen a
    single one of them fail in the middle of a run whose connections either
    side of it were fine -- a server that was demonstrably up.

    So try again for a third of a second before believing it. The waiting is
    doubled each time rather than spread evenly because a refusal that clears
    at all clears in milliseconds, and because the cost of this loop is paid in
    full on a machine that really has no server -- a container with DISPLAY
    exported into it, which falls back to a headless root and should not spend
    a second finding that out.

    Returns ``(display, errno, waited)``; ``display`` is None if every attempt
    failed, and ``errno`` is from the last of them.
    """
    x11 = _libs["x11"]
    err, waited = 0, 0.0
    for attempt in range(attempts):
        ctypes.set_errno(0)
        display = x11.XOpenDisplay(None)
        if display:
            return display, 0, waited
        err = ctypes.get_errno()
        if attempt + 1 < attempts:
            time.sleep(pause)
            waited += pause
            pause *= 2
    return None, err, waited


def _unreachable(name, err, waited):
    """Why the connection failed, in as much detail as we actually have.

    The bare "cannot reach the X server at :99" that this replaces is true of
    a server that is absent, one that is present and refused us, and one that
    threw us out over authorisation, and those want three different fixes. The
    next failure will be on a runner nobody can log into, so it has to say
    which it was on its own.
    """
    parts = ["cannot reach the X server at %s" % name]
    if waited:
        parts.append("after retrying for %.2f s" % waited)
    socket = _display_socket(name)
    if socket:
        there = "" if os.path.exists(socket) else " not"
        parts.append("%s does%s exist" % (socket, there))
    if err > 0:
        parts.append("last error: %s" % os.strerror(err))
    if os.environ.get("XAUTHORITY"):
        parts.append("XAUTHORITY=%s" % os.environ["XAUTHORITY"])
    return X11Unavailable("; ".join(parts))


def _open_display():
    """Connect to the X server and describe it. Idempotent.

    The connection is per process, not per window, exactly as on the other two
    platforms: one queue, one registry, and the root's loop feeds the popups.
    """
    if "display" in _state:
        return _state["display"]
    _load()
    x11 = _libs["x11"]
    if not os.environ.get("DISPLAY"):
        raise X11Unavailable(
            "$DISPLAY is not set, so there is no X server to draw on")
    display, err, waited = _connect()
    if not display:
        raise _unreachable(os.environ["DISPLAY"], err, waited)
    # Installed before anything is asked of the server, because the first
    # error is as fatal as any other with the default handler in place.
    handler = ERROR_HANDLER(_on_x_error)
    x11.XSetErrorHandler(handler)
    _state["error_handler"] = handler   # the reference that keeps it alive
    screen = x11.XDefaultScreen(display)
    visual = x11.XDefaultVisual(display, screen)
    depth = x11.XDefaultDepth(display, screen)
    fmt = _describe_format(display, visual, depth)
    if not (fmt.red_mask and fmt.green_mask and fmt.blue_mask):
        x11.XCloseDisplay(display)
        raise X11Unavailable(
            "the default visual is not TrueColor (depth %d); FeetBrowser has "
            "no colormap allocator" % depth)
    _state.update(display=display, screen=screen, visual=visual, depth=depth,
                  format=fmt, root=x11.XRootWindow(display, screen))
    for name in ("WM_PROTOCOLS", "WM_DELETE_WINDOW", "UTF8_STRING",
                 "CLIPBOARD", "TARGETS", "TEXT", "_NET_WM_NAME", "CARDINAL",
                 "_NET_WM_ICON", "FEETBROWSER_SELECTION"):
        _state[name] = int(x11.XInternAtom(display, name.encode(), False))
    return display


def _describe_format(display, visual, depth):
    """Read the server's pixel layout rather than assuming the usual one."""
    x11 = _libs["x11"]
    bits_per_pixel, scanline_pad = depth, 32
    count = ctypes.c_int(0)
    formats = x11.XListPixmapFormats(display, ctypes.byref(count))
    if formats:
        for index in range(count.value):
            if formats[index].depth == depth:
                bits_per_pixel = formats[index].bits_per_pixel
                scanline_pad = formats[index].scanline_pad
                break
        x11.XFree(formats)
    info = ctypes.cast(visual, ctypes.POINTER(Visual)).contents
    return PixelFormat(depth, bits_per_pixel, scanline_pad,
                       x11.XImageByteOrder(display), int(info.red_mask),
                       int(info.green_mask), int(info.blue_mask))


def _close_display():
    """Drop the connection once the last window has gone.

    Kept until then because reconnecting costs a round trip and re-interning
    every atom, and because a browser that closes its last window is usually
    on its way out anyway.
    """
    if _WINDOWS or "display" not in _state:
        return
    _libs["x11"].XCloseDisplay(_state["display"])
    _state.clear()


def _event_window(event):
    """The XID an event belongs to.

    Not always ``xany.window``: the selection events name their window in a
    field of their own, and a ConfigureNotify has two.
    """
    kind = event.type
    if kind == SELECTION_REQUEST:
        return int(event.xselectionrequest.owner)
    if kind == SELECTION_NOTIFY:
        return int(event.xselection.requestor)
    return int(event.xany.window)


# -- the window ------------------------------------------------------------

class X11Window(Window):
    """A titled, resizable X11 window presenting a raster surface."""

    def __init__(self, width=1000, height=720, title="FeetBrowser"):
        display = _open_display()
        super().__init__(width, height, title)
        x11 = _libs["x11"]
        self._display = display
        self._closed = False
        self._repaint = True
        self._frame = None
        self._frame_line = 0
        self._frame_dims = (0, 0)
        self._image = None
        self._image_dims = (0, 0)
        self._cursor_name = None
        self._cursor = 0
        self._selection = ""    # what we last put on the clipboard
        self._primary = ""      # ...and on PRIMARY, the mouse selection
        screen = _state["screen"]
        # X speaks in device pixels throughout -- window sizes, configure
        # notifications, pointer positions -- so the scale has to be known
        # before the window exists, and every number crossing this boundary
        # from here down is converted. It also has to be settled before
        # anybody makes a canvas, which reads it off us to size its buffer.
        # The resource string belongs to Xlib and must not be freed; it is
        # also a snapshot, so it is read per window rather than cached.
        raw = x11.XResourceManagerString(display)
        dpi = xft_dpi(raw.decode("latin-1", "replace") if raw else "")
        self.set_scale(None if dpi is None else dpi / BASELINE_DPI)
        dev_width, dev_height = self.to_device(self.width, self.height)
        self._window = x11.XCreateSimpleWindow(
            display, _state["root"], 0, 0, dev_width, dev_height, 0,
            x11.XBlackPixel(display, screen), x11.XWhitePixel(display, screen))
        if not self._window:
            raise X11Unavailable("the X server would not create a window")
        # Registered before the window is mapped, because the server starts
        # sending events the moment it is and the router has to find us.
        _WINDOWS[int(self._window)] = self
        x11.XSelectInput(display, self._window, EVENT_MASK)
        protocols = (Atom * 1)(_state["WM_DELETE_WINDOW"])
        x11.XSetWMProtocols(display, self._window, protocols, 1)
        self._gc = x11.XCreateGC(display, self._window, 0, None)
        self._apply_hints()
        self._set_icon()
        self.on_title_changed(title)
        if QUIET:
            # Override-redirect takes the window out of the window manager's
            # hands entirely, which is the only portable way to say "map this
            # without placing it, decorating it, raising it or focusing it" --
            # every other route is a hint the manager is free to ignore. The
            # server still maps and renders it, so it is readable with
            # XGetImage and events still arrive.
            attrs = XSetWindowAttributes()
            attrs.override_redirect = True
            x11.XChangeWindowAttributes(
                display, self._window, CW_OVERRIDE_REDIRECT,
                ctypes.byref(attrs))
        x11.XMapWindow(display, self._window)
        x11.XFlush(display)

    # -- geometry ----------------------------------------------------------

    def _apply_hints(self):
        """Tell the window manager the size we want and the size we need.

        In device pixels: a size hint is a promise to the window manager
        about the geometry it will hand back in ConfigureNotify, and that is
        the one currency the server has. A minimum stated in CSS pixels on a
        2x display would let the user drag the window down to half the size
        the browser said it could survive.
        """
        hints = XSizeHints()
        hints.flags = P_SIZE | (P_MIN_SIZE if self.min_width or self.min_height
                                else 0)
        hints.width, hints.height = self.to_device(self.width, self.height)
        hints.min_width, hints.min_height = self.to_device(self.min_width,
                                                           self.min_height)
        _libs["x11"].XSetWMNormalHints(self._display, self._window,
                                       ctypes.byref(hints))

    def resize(self, width, height):
        """Resize from our side, e.g. a geometry() call before the first
        frame. A resize the *user* made arrives as ConfigureNotify instead."""
        super().resize(width, height)
        if self._closed:
            return
        x11 = _libs["x11"]
        dev_width, dev_height = self.to_device(self.width, self.height)
        x11.XResizeWindow(self._display, self._window, dev_width, dev_height)
        x11.XFlush(self._display)

    def minsize(self, width, height):
        super().minsize(width, height)
        if not self._closed:
            self._apply_hints()

    # -- presenting --------------------------------------------------------

    def present(self):
        canvas = self.canvas
        if canvas is None or self._closed:
            return
        if not canvas.dirty and not self._repaint and self._frame is not None:
            return
        if canvas.dirty or self._frame is None:
            surface = canvas.render()
            self._frame, self._frame_line = pack_pixels(
                surface.pixels, surface.width, surface.height, surface.stride,
                _state["format"])
            self._frame_dims = (surface.width, surface.height)
        self._repaint = False
        self._blit()

    def _blit(self):
        """Push the last converted frame at its own size.

        Which is the window's size: the surface is allocated in device pixels,
        so one pixel of it is one pixel of the window and XPutImage lands them
        one for one. No scaling: X has no stretching blit for images, and the
        frame or two during a live resize where the surface and the window
        disagree simply leaves a strip of background until the canvas catches
        up -- which is one pump later, because ConfigureNotify asks for a
        repaint.
        """
        width, height = self._frame_dims
        if not width or not height or self._frame is None or self._closed:
            return
        x11 = _libs["x11"]
        image = self._ensure_image(width, height)
        # The XImage borrows the frame buffer for exactly this call. XPutImage
        # copies into the request stream before it returns, so nothing outlives
        # the borrow -- and clearing it again means the struct can never be
        # holding a pointer into a buffer Python has since moved.
        buffer = (ctypes.c_char * len(self._frame)).from_buffer(self._frame)
        image.contents.data = ctypes.cast(buffer, ctypes.c_void_p)
        try:
            x11.XPutImage(self._display, self._window, self._gc, image,
                          0, 0, 0, 0, width, height)
        finally:
            image.contents.data = None
            del buffer
        x11.XFlush(self._display)

    def _ensure_image(self, width, height):
        if self._image is not None and self._image_dims == (width, height):
            return self._image
        self._release_image()
        fmt = _state["format"]
        image = _libs["x11"].XCreateImage(
            self._display, _state["visual"], fmt.depth, Z_PIXMAP, 0, None,
            width, height, max(8, fmt.scanline_pad), self._frame_line)
        if not image:
            raise X11Unavailable("the X server would not make an image")
        self._image = image
        self._image_dims = (width, height)
        return image

    def _release_image(self):
        """Free the XImage struct without freeing our framebuffer with it.

        ``XDestroyImage`` is a macro rather than an exported symbol, so it
        cannot be called from here at all -- but it only frees ``data`` and
        the struct, and ``data`` is never ours to give away. Clearing it and
        freeing the struct is the same thing minus the double free.
        """
        if self._image is None:
            return
        self._image.contents.data = None
        _libs["x11"].XFree(self._image)
        self._image = None
        self._image_dims = (0, 0)

    # -- input -------------------------------------------------------------

    def poll_events(self):
        """Drain the connection's queue without blocking.

        ``XPending`` rather than a blocking ``XNextEvent``: the base main loop
        calls this every iteration and does its own waiting, so a pump that
        blocked would stop timers and animation dead.
        """
        if self._closed:
            return False
        x11 = _libs["x11"]
        event = XEvent()
        delivered = False
        while not self._closed and x11.XPending(self._display):
            x11.XNextEvent(self._display, ctypes.byref(event))
            delivered = True
            owner = _WINDOWS.get(_event_window(event))
            if owner is None:
                continue
            try:
                owner._translate(event)
            except Exception as exc:    # noqa: BLE001 - never lose the loop
                owner.on_callback_error("event", exc)
        # Not just tidiness: a handler is entitled to close the window -- the
        # close button does exactly that -- and by here the display may be
        # gone, so anything still holding it would be reading freed memory.
        if not self._closed:
            self._apply_cursor()
        return delivered

    def _translate(self, event):
        """Turn one X event into a Tk-shaped binding."""
        kind = event.type
        if kind == EXPOSE:
            # Only the last rectangle of a burst: repainting is whole-window
            # here, so doing it once per exposed strip is pure waste.
            if event.xexpose.count == 0:
                self._repaint = True
            return
        if kind == CONFIGURE_NOTIFY:
            self._on_configure(event)
            return
        if kind == CLIENT_MESSAGE:
            self._on_client_message(event)
            return
        if kind == DESTROY_NOTIFY:
            self._closed = True
            _WINDOWS.pop(int(self._window), None)
            return
        if kind in (KEY_PRESS, KEY_RELEASE):
            self._on_key(event, kind == KEY_PRESS)
            return
        if kind in (BUTTON_PRESS, BUTTON_RELEASE):
            self._on_button(event, kind == BUTTON_PRESS)
            return
        if kind == MOTION_NOTIFY:
            self._on_motion(event)
            return
        if kind in (SELECTION_REQUEST, SELECTION_CLEAR):
            self._on_selection(event)

    def _on_configure(self, event):
        device = (int(event.xconfigure.width), int(event.xconfigure.height))
        if not device[0] or not device[1]:
            return
        width, height = self.to_css(*device)
        stale = (self.canvas is not None
                 and self.canvas.device_size() != device)
        if stale or (width, height) != (self.width, self.height):
            # The base implementation, not ours: the window is already this
            # size, and asking the server to resize it again mid-drag fights
            # the user's mouse. The server's own figure is passed along
            # rather than re-derived from the CSS size, because at a
            # fractional scale the round trip does not come back where it
            # started and the buffer would end a pixel short of the window.
            Window.resize(self, width, height, device)
        self._repaint = True

    def _on_client_message(self, event):
        """The close button, which is a message from the window manager.

        There is no such thing as a close event in X: the WM asks politely
        through WM_DELETE_WINDOW and a well-behaved client goes away. A client
        that ignores it gets killed instead, which is what a browser that
        never saved anything looks like.
        """
        if (int(event.xclient.message_type) == _state["WM_PROTOCOLS"]
                and int(event.xclient.data.l[0]) == _state["WM_DELETE_WINDOW"]):
            self.destroy()

    def _on_key(self, event, pressed):
        x11 = _libs["x11"]
        key = ctypes.cast(ctypes.byref(event), ctypes.POINTER(XKeyEvent))
        state = modifier_state(event.xkey.state)
        if pressed:
            keysym = KeySym(0)
            buffer = ctypes.create_string_buffer(32)
            # The string is thrown away -- under Control it is a control code
            # rather than a letter -- but XLookupString is what applies the
            # user's keyboard layout to reach the keysym, which is the part
            # that matters.
            x11.XLookupString(key, buffer, 32, ctypes.byref(keysym), None)
            code = int(keysym.value)
        else:
            # XLookupString is documented for KeyPress only. A release still
            # has to name the same key, so it goes through the layout table
            # directly, picking the shifted column the same way.
            code = int(x11.XLookupKeysym(key, 1 if state & STATE_SHIFT else 0))
        raw = x11.XKeysymToString(code)
        resolved = keysym_event(raw.decode("ascii") if raw else "", code,
                                state)
        if resolved is None:
            return
        keysym_name, char = resolved
        x, y = self.to_css(event.xkey.x, event.xkey.y)
        obj = Event(keysym=keysym_name, char=char, state=state, x=x, y=y,
                    type="<Key>" if pressed else "<KeyRelease>")
        sequences = (key_sequences(keysym_name, state) if pressed
                     else key_release_sequences(keysym_name))
        for sequence in sequences:
            if self.dispatch(sequence, obj):
                return

    def _on_button(self, event, pressed):
        button = int(event.xbutton.button)
        state = modifier_state(event.xbutton.state)
        # The server points at a device pixel; everything above expects the
        # CSS pixel that contains it. This is the conversion that keeps a
        # click on a link landing on the link and not a quarter of the way
        # down the page.
        x, y = self.to_css(event.xbutton.x, event.xbutton.y)
        delta = wheel_delta(button)
        if delta:
            # A notch is a press and a release. Only the press scrolls, or the
            # page moves twice as far as the wheel did.
            if pressed:
                self.dispatch("<MouseWheel>",
                              Event(x=x, y=y, delta=delta, state=state,
                                    type="<MouseWheel>"))
            return
        binding = button_binding(button, pressed)
        if binding is None:
            return
        name, num = binding
        self.dispatch(name, Event(x=x, y=y, num=num, state=state, type=name))

    def _on_motion(self, event):
        name, num = motion_binding(event.xmotion.state)
        x, y = self.to_css(event.xmotion.x, event.xmotion.y)
        self.dispatch(name, Event(x=x, y=y, num=num,
                                  state=modifier_state(event.xmotion.state),
                                  type=name))

    def _apply_cursor(self):
        """Honour the pointer the canvas asked for, when it changes."""
        wanted = getattr(self.canvas, "cursor", "") if self.canvas else ""
        if wanted == self._cursor_name or self._closed:
            return
        self._cursor_name = wanted
        x11 = _libs["x11"]
        cursor = x11.XCreateFontCursor(self._display,
                                       CURSORS.get(wanted, CURSORS[""]))
        x11.XDefineCursor(self._display, self._window, cursor)
        if self._cursor:
            x11.XFreeCursor(self._display, self._cursor)
        self._cursor = cursor

    # -- window chrome -----------------------------------------------------

    def on_title_changed(self, title):
        if self._closed:
            return
        x11 = _libs["x11"]
        raw = title.encode("utf-8")
        # Both spellings: WM_NAME is Latin-1 and is what an old window manager
        # reads, _NET_WM_NAME is UTF-8 and is what every current one reads.
        x11.XStoreName(self._display, self._window, raw)
        x11.XChangeProperty(self._display, self._window, _state["_NET_WM_NAME"],
                            _state["UTF8_STRING"], 8, PROP_MODE_REPLACE, raw,
                            len(raw))
        x11.XFlush(self._display)

    def _set_icon(self):
        """Put the bundled art into ``_NET_WM_ICON``, so a window manager has
        something better than the generic 'this is a program' icon.

        The browser ships as ``feetbrowser/icon.png`` next to this module, the
        same artwork the Windows and macOS bundles draw from. It is decoded at
        runtime -- the server gets one canonical 256x256 image and scales it,
        which is what the WM does with the property whatever size is asked for.
        If the art is somehow missing or unreadable, the window is created
        without an icon rather than the window creation failing.
        """
        if self._closed:
            return
        try:
            from . import imagecodec
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "icon.png")
            with open(path, "rb") as f:
                width, height, rgba = imagecodec.decode_png(f.read())
        except (OSError, imagecodec.ImageError):
            return
        x11 = _libs["x11"]
        cardinals = net_wm_icon(width, height, rgba)
        # Format 32 means one long per element -- 8 bytes on a 64-bit build,
        # even though the property values themselves never exceed 32 bits --
        # so the buffer has to be sized and typed as longs or Xlib reads past
        # the end of it.
        data = (ctypes.c_ulong * len(cardinals))(*cardinals)
        x11.XChangeProperty(
            self._display, self._window, _state["_NET_WM_ICON"],
            _state["CARDINAL"], 32, PROP_MODE_REPLACE,
            ctypes.cast(data, ctypes.c_char_p), len(cardinals))
        x11.XFlush(self._display)

    def on_destroy(self):
        if self._closed:
            return
        self._closed = True
        window = self._window
        _WINDOWS.pop(int(window), None)
        x11 = _libs["x11"]
        self._release_image()
        self._frame = None
        if self._cursor:
            x11.XFreeCursor(self._display, self._cursor)
            self._cursor = 0
        if self._gc:
            x11.XFreeGC(self._display, self._gc)
            self._gc = None
        x11.XDestroyWindow(self._display, window)
        x11.XFlush(self._display)
        _close_display()

    def withdraw(self):
        super().withdraw()
        _libs["x11"].XUnmapWindow(self._display, self._window)
        _libs["x11"].XFlush(self._display)

    def deiconify(self):
        super().deiconify()
        _libs["x11"].XMapWindow(self._display, self._window)
        _libs["x11"].XFlush(self._display)

    def lift(self, *_args):
        if QUIET:
            return      # asking to be looked at is the one thing QUIET drops
        _libs["x11"].XRaiseWindow(self._display, self._window)
        _libs["x11"].XFlush(self._display)

    def lower(self, *_args):
        _libs["x11"].XLowerWindow(self._display, self._window)
        _libs["x11"].XFlush(self._display)

    # -- clipboard ---------------------------------------------------------
    #
    # X has no clipboard. It has selections, which are a protocol between two
    # running clients: the copier keeps the text and answers for it, and the
    # paster asks. So copying is claiming ownership, pasting is a round trip,
    # and there is a third case the other platforms do not have -- copying
    # from an application that has since quit gets you nothing, because there
    # is nobody left to ask.

    def on_clipboard_set(self, text):
        self._selection = self._primary = text
        x11 = _libs["x11"]
        # Both selections: CLIPBOARD is Ctrl-V and PRIMARY is middle-click,
        # and a Linux user expects a copy to satisfy either.
        for selection in (_state["CLIPBOARD"], XA_PRIMARY):
            x11.XSetSelectionOwner(self._display, selection, self._window,
                                   CURRENT_TIME)
        x11.XFlush(self._display)

    def on_primary_set(self, text):
        """Claim PRIMARY only: a mouse selection is a copy, but not *the*
        copy. Leaving CLIPBOARD alone is the whole point -- dragging over a
        paragraph must not throw away what the user pressed Ctrl+C on a
        minute ago, which is what would happen if this took both."""
        self._primary = text
        x11 = _libs["x11"]
        x11.XSetSelectionOwner(self._display, XA_PRIMARY, self._window,
                               CURRENT_TIME)
        x11.XFlush(self._display)

    def on_clipboard_get(self):
        x11 = _libs["x11"]
        owner = int(x11.XGetSelectionOwner(self._display, _state["CLIPBOARD"]))
        if not owner:
            return ""
        if owner in _WINDOWS:
            # Ours. Converting our own selection would mean answering our own
            # request from inside this call, which is a deadlock dressed up as
            # a round trip; the text is right here.
            return _WINDOWS[owner]._selection
        return self._fetch_selection(_state["CLIPBOARD"])

    def _fetch_selection(self, selection):
        x11 = _libs["x11"]
        prop = _state["FEETBROWSER_SELECTION"]
        x11.XConvertSelection(self._display, selection, _state["UTF8_STRING"],
                              prop, self._window, CURRENT_TIME)
        x11.XFlush(self._display)
        event = XEvent()
        deadline = time.monotonic() + SELECTION_TIMEOUT
        # A typed check rather than draining the queue, so a paste does not
        # swallow the clicks and keys queued up behind it.
        while time.monotonic() < deadline:
            if x11.XCheckTypedWindowEvent(self._display, self._window,
                                          SELECTION_NOTIFY,
                                          ctypes.byref(event)):
                break
            time.sleep(0.005)
        else:
            return ""   # a wedged owner is not allowed to wedge the browser
        if not event.xselection.property:
            return ""   # the owner has no text to offer
        return self._read_property(prop)

    def _read_property(self, prop):
        x11 = _libs["x11"]
        actual_type, actual_format = Atom(0), ctypes.c_int(0)
        items, remaining = ctypes.c_ulong(0), ctypes.c_ulong(0)
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = x11.XGetWindowProperty(
            self._display, self._window, prop, 0, 0x1FFFFFFF, True,
            ANY_PROPERTY_TYPE, ctypes.byref(actual_type),
            ctypes.byref(actual_format), ctypes.byref(items),
            ctypes.byref(remaining), ctypes.byref(data))
        if status != 0 or not data:
            return ""
        try:
            width = max(1, actual_format.value // 8)
            raw = ctypes.string_at(data, items.value * width)
        finally:
            x11.XFree(data)
        return raw.decode("utf-8", "replace")

    def _on_selection(self, event):
        """Answer another application asking for what we copied."""
        if event.type == SELECTION_CLEAR:
            # Somebody else copied something. Ours is no longer the answer,
            # and claiming otherwise would hand out stale text. Only the
            # selection we actually lost: losing PRIMARY to another window's
            # drag says nothing about what we copied.
            lost = int(event.xselectionclear.selection)
            if lost == XA_PRIMARY:
                self._primary = ""
            else:
                self._selection = ""
            return
        request = event.xselectionrequest
        x11 = _libs["x11"]
        target = int(request.target)
        # A requestor from before ICCCM leaves the property unset and means
        # "put it where the target says"; honouring that costs one line.
        prop = int(request.property) or target
        granted = self._serve_target(int(request.requestor), target, prop,
                                     int(request.selection))
        reply = XEvent()
        reply.xselection.type = SELECTION_NOTIFY
        reply.xselection.display = self._display
        reply.xselection.requestor = request.requestor
        reply.xselection.selection = request.selection
        reply.xselection.target = request.target
        reply.xselection.property = prop if granted else 0
        reply.xselection.time = request.time
        x11.XSendEvent(self._display, request.requestor, False, 0,
                       ctypes.byref(reply))
        x11.XFlush(self._display)

    def _serve_target(self, requestor, target, prop, selection=None):
        """Write the requested form of the selection, or say we cannot.

        `selection` says which of ours is being asked for: PRIMARY is the
        mouse selection and anything else is the clipboard. It defaults to
        the clipboard, which is what a caller with only one in mind means.
        """
        x11 = _libs["x11"]
        if target == _state["TARGETS"]:
            offered = (Atom * 3)(_state["TARGETS"], _state["UTF8_STRING"],
                                 XA_STRING)
            x11.XChangeProperty(self._display, requestor, prop, XA_ATOM, 32,
                                PROP_MODE_REPLACE,
                                ctypes.cast(offered, ctypes.c_char_p), 3)
            return True
        if target not in (_state["UTF8_STRING"], _state["TEXT"], XA_STRING):
            return False
        text = self._primary if selection == XA_PRIMARY else self._selection
        raw = text.encode("utf-8")
        # TEXT means "whatever encoding you like, tell me which"; the answer
        # is always UTF-8, so that is what the property is typed as.
        kind = XA_STRING if target == XA_STRING else _state["UTF8_STRING"]
        x11.XChangeProperty(self._display, requestor, prop, kind, 8,
                            PROP_MODE_REPLACE, raw, len(raw))
        return True


class X11Toplevel(X11Window):
    """A secondary window, used for the browser's link previews."""

    def __init__(self, master=None, **kwargs):
        super().__init__(**kwargs)
        self.master = master
        if master is not None:
            master.children.append(self)

    def destroy(self):
        if self.master is not None and self in self.master.children:
            self.master.children.remove(self)
        super().destroy()


class X11Tk(X11Window):
    """The root window, matching ``window.Tk``."""

    def __init__(self, *_args, **kwargs):
        super().__init__(**kwargs)
        self.tk = None  # layout's batched-measure path checks for this


# gui.Toplevel reads this off the master, so a popup opened from a real window
# is a real window and one opened from a headless root stays headless.
X11Window.toplevel_class = X11Toplevel


def available():
    """True when an X11 window can actually be created here."""
    global _problem
    _problem = ""
    try:
        _open_display()
    except X11Unavailable as exc:
        # A missing libX11 means this is not an X11 system, which is no more
        # worth reporting than Cocoa being absent from Linux. A libX11 that
        # loads and then cannot connect is the entire story, so that one is
        # kept for whoever wants to print it.
        _problem = str(exc) if _libs else ""
        return False
    return True


def unavailable_reason():
    """Why available() last said no, or "" when there is nothing to say."""
    return _problem
