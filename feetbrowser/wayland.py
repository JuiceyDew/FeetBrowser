"""A real Wayland window, with no libraries at all.

Wayland is a wire protocol: a client and a compositor exchange length- and
type-prefixed messages over a unix socket. Everything a client needs to do --
enumerate globals, bind interfaces, create a surface, hand the compositor
shared-memory buffers, receive input -- is that protocol, so none of it needs
a library. There is no libwayland-client here: the messages are assembled and
parsed in this file with ``struct`` and ``sendmsg``/``recvmsg``, the same way
``net.py`` speaks HTTP/1.1 and ``imagecodec`` decodes PNGs with nothing
imported. Three things a libwayland-based client gets for free are built here
instead:

  * the wire format itself -- a two-word header (object id; opcode and byte
    length), then arguments, with file descriptors carried out-of-band as
    ``SCM_RIGHTS`` ancillary data;
  * the compositor's keyboard map. Wayland hands the *keymap* to the client
    and expects it to translate; the keymap is a text file, so a small parser
    here extracts the keycode -> keysym table and the Shift levels instead of
    pulling in xkbcommon. It covers the symbols and keycodes sections of the
    standard layouts (level 1 and 2, i.e. Shift, plus Caps Lock), which is
    what a browser needs to type and to navigate. Compose sequences, AltGr
    level-3 symbols and dead-key composition are out of scope, and documented
    as such;
  * there are no cursors. Setting no cursor at all is a legal request -- the
    compositor draws its default -- so the pointer works everywhere with
    nothing drawn by us. The hand/arrow/text pointers the X11 backend can
    show are a libwayland-cursor convenience this file deliberately does
    without.

Everything else is the same shape as x11.py: a ``WaylandWindow`` subclass of
``window.Window`` that presents the canvas into a shared-memory buffer and
translates seat events into the Tk-shaped bindings the browser binds. The
pure arithmetic -- message packing, buffer packing, button numbering, keysyms,
scroll steps, keymap parsing -- lives in module-level functions so a test can
reach it with no compositor anywhere; the rest only runs against one.
"""
import mmap
import os
import re
import select
import socket
import struct
import time

from .window import (STATE_ALT, STATE_CONTROL, STATE_SHIFT, Event, Window,
                     key_sequences)
from .x11 import keysym_event, key_release_sequences

# -- constants -------------------------------------------------------------

# wl_shm_format, the one we use: 4 bytes per pixel, B G R X, alpha opaque.
WL_SHM_FORMAT_XRGB8888 = 1

# wl_seat capabilities.
WL_SEAT_CAPABILITY_POINTER = 1
WL_SEAT_CAPABILITY_KEYBOARD = 2

# wl_pointer axis ids.
WL_POINTER_AXIS_VERTICAL_SCROLL = 0

# cursor-shape-v1: wp_cursor_shape_device_v1.shape values. The compositor
# draws the pointer, so a client only names the shape it wants -- no cursor
# images to ship, no libwayland-cursor.
SHAPE_DEFAULT = 1
SHAPE_POINTER = 2
SHAPE_TEXT = 3

# The Tk cursor names browser.py hands the canvas -> the shape above.
CURSOR_SHAPES = {
    "": SHAPE_DEFAULT,
    "arrow": SHAPE_DEFAULT,
    "hand2": SHAPE_POINTER,
    "pointer": SHAPE_POINTER,
    "hand": SHAPE_POINTER,
    "text": SHAPE_TEXT,
    "xterm": SHAPE_TEXT,
    "ibeam": SHAPE_TEXT,
}

# How many pixels one scroll "notch" moves, matching x11.py and the other
# backends. browser.py treats |delta| < 30 as pixels.
WHEEL_STEP = 20

# The id range the client owns. The display object is 1 and the server's
# own objects live at 0xff000000 and up, so a client that allocates its own
# objects at 2, 3, 4, ... can never collide with either -- which is exactly
# the dense sequence libwayland uses (its first client object, the registry,
# is id 2). The server rejects ids in its own high range.
CLIENT_ID_START = 2

# How long to wait for another application to hand over the clipboard
# before giving up, the same budget x11.py gives its selection round trip.
SELECTION_TIMEOUT = 0.5

# The clipboard MIME type we offer. Wayland has no "just text": every
# transfer is typed, and this is the one every text consumer accepts.
CLIPBOARD_MIME = "text/plain;charset=utf-8"

# Pointer button codes (linux/input-event-codes.h) -> Tk's button numbers,
# which are X11's: 1 left, 2 middle, 3 right.
BUTTONS = {0x110: 1, 0x111: 3, 0x112: 2}

# Keysyms in the keyboard map that are a modifier being held, mapped to the
# Tk state bit they imply. Names not in this table (Super, Hyper) are dropped
# the way X11 drops its lock bits: they are not bits Tk has.
MODIFIER_STATE = {
    "Shift_L": STATE_SHIFT, "Shift_R": STATE_SHIFT,
    "Control_L": STATE_CONTROL, "Control_R": STATE_CONTROL,
    "Alt_L": STATE_ALT, "Alt_R": STATE_ALT,
    "Meta_L": STATE_ALT, "Meta_R": STATE_ALT,
}

# Named keysyms in the compositor's keymap and their X11 values. Single
# characters and the ASCII punctuation names are handled separately; this is
# the rest, the names a keyboard layout actually uses.
NAMED_KEYSYMS = {
    "Escape": 0xff1b, "Tab": 0xff09, "Return": 0xff0d, "BackSpace": 0xff08,
    "Delete": 0xffff, "Insert": 0xff63, "Home": 0xff50, "End": 0xff57,
    "Page_Up": 0xff55, "Page_Down": 0xff56, "Left": 0xff51, "Right": 0xff53,
    "Up": 0xff52, "Down": 0xff54, "Shift_L": 0xffe1, "Shift_R": 0xffe2,
    "Control_L": 0xffe3, "Control_R": 0xffe4, "Alt_L": 0xffe9,
    "Alt_R": 0xffea, "Meta_L": 0xffe7, "Meta_R": 0xffe8, "Super_L": 0xffeb,
    "Super_R": 0xffec, "Hyper_L": 0xffed, "Hyper_R": 0xffee,
    "Caps_Lock": 0xffe5, "CapsLock": 0xffe5, "Num_Lock": 0xff7f,
    "Scroll_Lock": 0xff14, "ISO_Left_Tab": 0xfe20,
    "ISO_Level3_Shift": 0xfe03, "ISO_Level5_Shift": 0xfe11,
    "Mode_switch": 0xff7e, "KP_Enter": 0xff8d, "KP_Add": 0xffab,
    "KP_Subtract": 0xffad, "KP_Multiply": 0xffaa, "KP_Divide": 0xffaf,
    "KP_Decimal": 0xffae, "KP_Separator": 0xffac, "KP_Equal": 0xffbd,
    "Pause": 0xff13, "Print": 0xff61, "Menu": 0xff67,
    "dead_acute": 0xfe51, "dead_grave": 0xfe50, "dead_circumflex": 0xfe52,
    "dead_tilde": 0xfe53, "dead_diaeresis": 0xfe57, "dead_cedilla": 0xfe5b,
}
NAMED_KEYSYMS.update({"KP_%d" % n: 0xffb0 + n for n in range(10)})
NAMED_KEYSYMS.update({"F%d" % n: 0xffbe + n - 1 for n in range(1, 36)})

# Named keysyms that are a single ASCII character under another name.
ASCII_NAMES = {
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "grave": "`",
    "comma": ",", "period": ".", "slash": "/", "space": " ", "exclam": "!",
    "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "parenleft": "(", "parenright": ")", "asterisk": "*",
    "plus": "+", "colon": ":", "less": "<", "greater": ">", "question": "?",
    "at": "@", "asciitilde": "~", "asciicircum": "^", "underscore": "_",
    "braceleft": "{", "braceright": "}", "bar": "|",
}

# -- module state ----------------------------------------------------------

_STATE = {}     # the connection, the bound globals, the clipboard
_SURFACES = {}  # surface id -> WaylandWindow, for routing seat events
_POINTER_WIN = None
_KEYBOARD_WIN = None
_HELD = set()   # modifier keysyms currently held down
_CAPS = [False]  # Caps Lock toggled
_problem = ""


class WaylandUnavailable(RuntimeError):
    """Raised when this platform cannot supply a Wayland window."""


# -- pure helpers ----------------------------------------------------------
#
# No socket below this line until the connection itself. These are the parts
# a test can reach with no compositor anywhere.


def fixed_to_float(value):
    """A ``wl_fixed_t`` (24.8) as a plain float."""
    return value / 256.0


def pack_xrgb(pixels, width, height, stride):
    """An RGB framebuffer as XRGB8888 bytes for a wl_shm buffer.

    Every compositor accepts XRGB8888, so unlike X11 there is nothing to
    negotiate -- the one layout is 4 bytes per pixel, B, G, R, X, with the
    alpha byte set opaque. Three strided slice assignments plus a fill of
    the alpha row, all of which run in C.
    """
    packed = bytearray(width * height * 4)
    packed[2::4] = pixels[0::3]
    packed[1::4] = pixels[1::3]
    packed[0::4] = pixels[2::3]
    packed[3::4] = b"\xff" * (width * height)
    return packed


def pack_message(obj, opcode, fmt, values, fds=()):
    """One request as wire bytes, plus the fds to pass out-of-band.

    The header is two words: the object id, then ``size << 16 | opcode``
    where size is the whole message in bytes -- exactly what the compositor
    parses out of ``wl_closure_send``. Arguments follow the signature; the
    ``n`` (new id) and ``o`` (object) slots carry object ids, and an ``h``
    (fd) slot occupies no bytes and instead hands its descriptor to the
    caller to send as SCM_RIGHTS.
    """
    out = bytearray()
    for ch, val in zip(fmt, values):
        if ch in "uio":
            out += struct.pack("<I", (val or 0) & 0xFFFFFFFF)
        elif ch == "n":
            out += struct.pack("<I", val & 0xFFFFFFFF)
        elif ch in "if":
            out += struct.pack("<i", val & 0xFFFFFFFF)
        elif ch == "s":
            if val is None:
                out += struct.pack("<I", 0)
            else:
                raw = val.encode("utf-8") + b"\x00"
                out += struct.pack("<I", len(raw))
                out += raw
                out += b"\x00" * (-len(out) % 4)
        elif ch == "a":
            raw = bytes(val or b"")
            out += struct.pack("<I", len(raw))
            out += raw
            out += b"\x00" * (-len(out) % 4)
        elif ch == "h":
            fds = fds + (val,)
    size = len(out) + 8
    return (struct.pack("<II", obj & 0xFFFFFFFF,
                        ((size << 16) | (opcode & 0xFFFF)) & 0xFFFFFFFF)
            + bytes(out)), fds


def unpack_message(fmt, words, fds):
    """Arguments of one event, from its signature and the raw words."""
    args = []
    wi = 0
    for ch in fmt:
        if ch == "u":
            args.append(words[wi]); wi += 1
        elif ch in "if":
            args.append(struct.unpack("<i", struct.pack("<I", words[wi]))[0])
            wi += 1
        elif ch in "on":
            args.append(words[wi]); wi += 1
        elif ch == "s":
            length = words[wi]; wi += 1
            nw = (length + 3) // 4
            raw = b"".join(struct.pack("<I", w) for w in words[wi:wi + nw])
            wi += nw
            args.append(raw[:max(0, length - 1)].decode("utf-8", "replace")
                        if length else None)
        elif ch == "a":
            length = words[wi]; wi += 1
            nw = (length + 3) // 4
            raw = b"".join(struct.pack("<I", w) for w in words[wi:wi + nw])
            wi += nw
            args.append(raw[:length])
        elif ch == "h":
            args.append(fds.pop(0))
    return args


def button_number(code):
    """The Tk button number for a Wayland pointer button code, or None."""
    return BUTTONS.get(code)


def button_event(code, pressed):
    """The binding name and number for a pointer button, or None.

    The wheel is not a button here as it is on X11: Wayland reports it as
    axis events, so buttons 4 and 5 never exist and there is no "the wheel
    is two buttons" special case to keep straight.
    """
    num = button_number(code)
    if num is None:
        return None
    prefix = "Button" if pressed else "ButtonRelease"
    return "<%s-%d>" % (prefix, num), num


def wheel_delta(steps):
    """Pixels to scroll for `steps` wheel notches, or 0 for none.

    The sign is inverted relative to X11, because a positive Wayland axis
    value means scrolling *down* and x11.py's positive delta means *up*.
    """
    return steps * WHEEL_STEP


def axis_delta(value_fixed, discrete):
    """(steps, used_discrete) for one axis event.

    Returns the number of notches to scroll (negative = down) and whether a
    real discrete count was available, which is what tells the caller not to
    double-count the axis value that accompanies it.
    """
    if discrete is not None:
        return -discrete, True
    notches = round(fixed_to_float(value_fixed) / 15.0)
    return -notches, False


def state_from_xkb(shift, control, alt):
    """Tk's event.state bits from which modifiers are active.

    Tk's bits are 0x1 shift, 0x4 control, 0x8 alt -- the same three the
    other two backends produce, so browser.py can read them without knowing
    where they came from. Super, Meta and friends are not bits Tk has, so
    they are dropped exactly as the X11 backend drops the lock and button
    bits.
    """
    state = 0
    if shift:
        state |= STATE_SHIFT
    if control:
        state |= STATE_CONTROL
    if alt:
        state |= STATE_ALT
    return state


def keysym_pair(name, keysym, state):
    """(keysym, char) for a key, or None when it is not an event at all.

    The same function the X11 backend uses: the compositor's keysym names
    are X11's, because the keysym vocabulary is shared.
    """
    return keysym_event(name, keysym, state)


def keysym_releases(keysym):
    """Binding names for a key going back up, most specific first."""
    return key_release_sequences(keysym)


def symbol_value(name):
    """The X11 keysym value for a name out of the compositor's keymap.

    A single character is its own codepoint; the ASCII punctuation keys are
    named after their character; everything else is the small table of
    keysyms a keyboard layout actually uses. A compact keymap may hand us
    the value itself (``0x61``), which is read straight off.
    """
    if name.startswith("0x") or name.startswith("0X"):
        try:
            return int(name, 16)
        except ValueError:
            return 0
    if len(name) == 1:
        return ord(name)
    if name in NAMED_KEYSYMS:
        return NAMED_KEYSYMS[name]
    if name in ASCII_NAMES:
        return ord(ASCII_NAMES[name])
    return 0


def keysym_name(value):
    """The X11 name for a keysym value, the reverse of symbol_value.

    A compact keymap hands over only the values (``key <AC01> {
    [ 0x61, 0x41 ] };``); the key path keys off names, so the value has
    to become one. Single characters are their codepoint again, the named
    keysyms come out of the same tables symbol_value reads.
    """
    if 0x20 <= value < 0x7f:
        return chr(value)
    for key, val in NAMED_KEYSYMS.items():
        if val == value:
            return key
    for key, ch in ASCII_NAMES.items():
        if ord(ch) == value:
            return key
    return "0x%x" % value


def _section(text, name):
    """The body of an ``xkb_<name> "..." { ... };`` section, or ""."""
    m = re.search(r"xkb_%s\b" % name, text)
    if not m:
        return ""
    start = text.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return ""


def parse_keymap(text):
    """The parts of a compositor keymap this browser needs.

    Returns ``(syms, names, shift)``: ``syms`` and ``names`` keyed by the
    keymap's own keycodes (X11 keysym values for levels 1 and 2, and their
    names), and ``shift`` -- how far the keymap's keycodes sit above the
    compositor's. The ``wl_keyboard.key`` event carries the raw hardware
    scan code (linux evdev), while the keymap's keycodes section numbers
    the same physical keys 8 higher, so the lookup must add that gap. A
    keymap already written in evdev codes (AC01 at 30) shifts by nothing.
    Only the ``xkb_keycodes`` and ``xkb_symbols`` sections are read, and
    only Group1: that is what a browser needs to type and to navigate, and
    it deliberately does not chase xkb's compose sequences, level-3 (AltGr)
    symbols or dead-key composition.
    """
    codes = {}
    aliases = {}
    body = _section(text, "keycodes")
    if body:
        for m in re.finditer(r"<([^>]+)>\s*=\s*(\d+);", body):
            codes[m.group(1)] = int(m.group(2))
        for m in re.finditer(r"alias\s*<([^>]+)>\s*=\s*<([^>]+)>;", body):
            aliases[m.group(1)] = m.group(2)
    syms, names = {}, {}
    body = _section(text, "symbols")
    if body:
        for m in re.finditer(r"key\s*<([^>]+)>\s*\{([^}]*)\}", body):
            name = m.group(1)
            inner = m.group(2)
            sm = re.search(r"symbols\[Group1\]\s*=\s*\[\s*([^\]]*)\]", inner)
            if not sm:
                # The compact form a compositor may send: a key block of
                # just its symbols, ``key <AC01> { [ 0x61, 0x41 ] };``.
                sm = re.search(r"\[\s*([^\]]*)\]", inner)
            if not sm:
                continue
            level_names = [x.strip() for x in sm.group(1).split(",")
                           if x.strip()]
            code = codes.get(name)
            if code is None:
                code = codes.get(aliases.get(name))
            if code is None or not level_names:
                continue
            levels = [_level_value(x) for x in level_names[:2]]
            syms[code] = [value for value, _name in levels]
            names[code] = [nm for _value, nm in levels]
    shift = max(0, codes.get("AC01", 0) - 30)
    return syms, names, shift


def _level_value(text):
    """(keysym, name) for one key level, from either spelling a keymap uses:
    a name like ``a`` or ``Shift_L``, or a compact keymap's raw value like
    ``0x61``."""
    if text.startswith("0x"):
        value = int(text, 16)
        return value, keysym_name(value)
    return symbol_value(text), text


# -- the connection ---------------------------------------------------------
#
# One socket, one id space, one object table. The compositor is the server;
# everything here is a client speaking its wire format.


class _Conn:
    """A Wayland connection: send requests, receive and route events."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.fds = []
        self.objects = {}      # object id -> {iface, window, extra}
        self.next_id = CLIENT_ID_START
        self.dead = False
        self.last_error = None
        # The display object is 1; every event's object is looked up here.
        self.objects[1] = {"iface": "wl_display"}

    def new_id(self, iface, window=None):
        """A fresh client-owned id, registered for events."""
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = {"iface": iface, "window": window}
        return oid

    def add_object(self, oid, iface, window=None):
        """Register a server-created object (a small id from an event)."""
        self.objects[oid] = {"iface": iface, "window": window}

    def request(self, obj, opcode, fmt, values=(), fds=()):
        """Send one request."""
        msg, fds = pack_message(obj, opcode, fmt, values, fds)
        anc = []
        if fds:
            anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                    struct.pack("i" * len(fds), *fds))]
        try:
            self.sock.sendmsg([msg], anc)
        except OSError:
            self.dead = True

    def pump(self):
        """Read everything waiting and route it, never blocking."""
        while True:
            try:
                data, anc, _flags, _addr = self.sock.recvmsg(
                    65536, socket.CMSG_SPACE(64))
            except BlockingIOError:
                return
            except OSError:
                self.dead = True
                return
            if not data:
                self.dead = True
                return
            for _level, _typ, raw in anc:
                self.fds.extend(struct.unpack("i" * (len(raw) // 4), raw))
            self.buf += data
            self._process()

    def _process(self):
        """Demarshal and dispatch every complete message in the buffer."""
        while True:
            if len(self.buf) < 8:
                return
            obj, word = struct.unpack("<II", self.buf[:8])
            opcode = word & 0xFFFF
            size = word >> 16
            if size < 8 or size & 3 or len(self.buf) < size:
                if size < 8 or size & 3:
                    self.last_error = "malformed message size %d" % size
                    self.dead = True
                    return
                return
            payload = self.buf[8:size]
            self.buf = self.buf[size:]
            words = list(struct.unpack("<%dI" % (len(payload) // 4), payload))
            self._dispatch(obj, opcode, words)

    def _dispatch(self, obj, opcode, words):
        rec = self.objects.get(obj)
        if rec is None:
            return
        spec = _EVENTS.get(rec["iface"], {}).get(opcode)
        if spec is None:
            return
        fmt, handler = spec
        args = unpack_message(fmt, words, self.fds)
        try:
            handler(rec, obj, *args)
        except Exception as exc:    # noqa: BLE001 - never lose the loop
            import traceback
            print("FeetBrowser: error in Wayland handler: %r" % (exc,))
            traceback.print_exc()


# -- connecting -------------------------------------------------------------

def _connect():
    """Open the socket and bind the globals we need."""
    if "conn" in _STATE:
        return _STATE["conn"]
    name = os.environ.get("WAYLAND_SOCKET")
    if not name:
        display = os.environ.get("WAYLAND_DISPLAY")
        if not display:
            raise WaylandUnavailable(
                "$WAYLAND_DISPLAY is not set, so there is no compositor to "
                "draw on")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        name = os.path.join(runtime, display) if runtime else display
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(name)
    except OSError as exc:
        sock.close()
        raise WaylandUnavailable("cannot connect to the Wayland compositor "
                                 "at %s: %s" % (name, exc)) from exc
    sock.setblocking(False)
    conn = _Conn(sock)
    _STATE["conn"] = conn
    _STATE.update(compositor=0, shm=0, xdg_wm_base=0, seat=0,
                  data_device_manager=0, cursor_shape_manager=0,
                  pointer=0, pointer_shape=0, pointer_serial=0,
                  outputs=[], scale=1.0,
                  pending_scale=1.0, serial=0)
    registry = conn.new_id("wl_registry")
    conn.request(1, 1, "n", [registry])     # wl_display.get_registry
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        conn.pump()
        if _STATE["compositor"] and _STATE["shm"] and _STATE["xdg_wm_base"]:
            return conn
        time.sleep(0.01)
    raise WaylandUnavailable(
        "the compositor offered no wl_compositor/wl_shm/xdg_wm_base")


def _global(rec, _sid, name, interface, version):
    """A global announced: bind the ones we know how to talk to."""
    conn = _conn()
    if interface == "wl_compositor":
        oid = conn.new_id("wl_compositor")
        conn.request(_sid, 0, "usun", [name, "wl_compositor", 4, oid])
        _STATE["compositor"] = oid
    elif interface == "wl_shm":
        oid = conn.new_id("wl_shm")
        conn.request(_sid, 0, "usun", [name, "wl_shm", 1, oid])
        _STATE["shm"] = oid
    elif interface == "xdg_wm_base":
        oid = conn.new_id("xdg_wm_base")
        conn.request(_sid, 0, "usun", [name, "xdg_wm_base", 1, oid])
        _STATE["xdg_wm_base"] = oid
    elif interface == "wl_seat":
        oid = conn.new_id("wl_seat")
        conn.request(_sid, 0, "usun", [name, "wl_seat", 7, oid])
        _STATE["seat"] = oid
    elif interface == "wl_output":
        oid = conn.new_id("wl_output")
        conn.request(_sid, 0, "usun", [name, "wl_output", 4, oid])
        _STATE["outputs"].append(oid)
    elif interface == "wl_data_device_manager":
        oid = conn.new_id("wl_data_device_manager")
        conn.request(_sid, 0, "usun", [name, "wl_data_device_manager", 3, oid])
        _STATE["data_device_manager"] = oid
    elif interface == "wp_cursor_shape_manager_v1":
        oid = conn.new_id("wp_cursor_shape_manager_v1")
        conn.request(_sid, 0, "usun",
                     [name, "wp_cursor_shape_manager_v1", 1, oid])
        _STATE["cursor_shape_manager"] = oid


def _global_remove(rec, _sid, _name):
    pass


def _display_error(rec, _sid, _object, _code, _message):
    _conn().last_error = "%s (code %d on object %d)" % (
        _message, _code, _object)


def _display_delete_id(rec, _sid, _id):
    _conn().objects.pop(_id, None)


def _ping(rec, _sid, serial):
    # A silent client is a dead client; the compositor pings and we must
    # answer.
    _conn().request(_sid, 3, "u", [serial])     # xdg_wm_base.pong


def _shm_format(rec, _sid, _format):
    pass


def _output_geometry(rec, _sid, *args):
    pass


def _output_mode(rec, _sid, *args):
    pass


def _output_done(rec, _sid):
    _apply_output_scale()


def _output_scale(rec, _sid, factor):
    _STATE["pending_scale"] = max(_STATE.get("pending_scale", 1.0), factor)
    _apply_output_scale()


def _output_name(rec, _sid, _name):
    pass


def _output_description(rec, _sid, _description):
    pass


def _apply_output_scale():
    """Adopt the largest scale any output reports, for the surface buffer."""
    scale = _STATE.get("pending_scale", 1.0)
    if scale == _STATE.get("scale"):
        return
    _STATE["scale"] = scale
    for window in _SURFACES.values():
        window.set_scale(scale)


def _surface_enter(rec, _sid, _output):
    pass


def _surface_leave(rec, _sid, _output):
    pass


def _surface_preferred_buffer_scale(rec, _sid, _scale):
    pass


def _surface_preferred_buffer_transform(rec, _sid, _transform):
    pass


def _callback_done(rec, _sid, _serial):
    pass


# -- input ------------------------------------------------------------------

def _seat_capabilities(rec, _sid, capabilities):
    conn = _conn()
    if capabilities & WL_SEAT_CAPABILITY_POINTER and not _STATE.get("pointer"):
        oid = conn.new_id("wl_pointer")
        conn.request(_sid, 0, "n", [oid])       # wl_seat.get_pointer
        _STATE["pointer"] = oid
    if capabilities & WL_SEAT_CAPABILITY_KEYBOARD and not _STATE.get(
            "keyboard"):
        oid = conn.new_id("wl_keyboard")
        conn.request(_sid, 1, "n", [oid])       # wl_seat.get_keyboard
        _STATE["keyboard"] = oid


def _seat_name(rec, _sid, _name):
    pass


def _ensure_pointer_shape():
    """The zwp_pointer_shape_v1 object for the seat's pointer, created once.

    Returns its id, or 0 when the compositor has no cursor-shape manager --
    in which case no cursor is set at all and the compositor falls back to
    its own default, exactly as it did before this existed.
    """
    if _STATE.get("pointer_shape"):
        return _STATE["pointer_shape"]
    m = _STATE.get("cursor_shape_manager")
    ptr = _STATE.get("pointer")
    if not m or not ptr:
        return 0
    oid = _conn().new_id("zwp_pointer_shape_v1")
    _conn().request(m, 0, "no", [oid, ptr])   # cursor_shape_manager.get_pointer
    _STATE["pointer_shape"] = oid
    return oid


def _pointer_enter(rec, _sid, serial, surface, x, y):
    global _POINTER_WIN
    _STATE["serial"] = serial
    _STATE["pointer_serial"] = serial
    _POINTER_WIN = _SURFACES.get(surface)
    if _POINTER_WIN is not None:
        win = _POINTER_WIN
        win._pointer_inside = True
        win._last_x, win._last_y = fixed_to_float(x), fixed_to_float(y)
        _ensure_pointer_shape()
        win._apply_cursor()


def _pointer_leave(rec, _sid, serial, _surface):
    global _POINTER_WIN
    _STATE["serial"] = serial
    win = _POINTER_WIN
    if win is not None:
        win._pointer_inside = False
        win._button_held = 0
    _POINTER_WIN = None


def _pointer_motion(rec, _sid, _time, x, y):
    win = _POINTER_WIN
    if win is None:
        return
    win._last_x, win._last_y = fixed_to_float(x), fixed_to_float(y)
    name, num = win._motion_binding()
    px, py = win._pointer_css()
    win.dispatch(name, Event(x=px, y=py, num=num,
                             state=_modifier_state(), type=name))
    win._apply_cursor()


def _pointer_button(rec, _sid, serial, _time, button, state):
    win = _POINTER_WIN
    if win is None:
        return
    _STATE["serial"] = serial
    pressed = bool(state)
    binding = button_event(button, pressed)
    if binding is None:
        return
    name, num = binding
    win._button_held = num if pressed else 0
    px, py = win._pointer_css()
    win.dispatch(name, Event(x=px, y=py, num=num,
                             state=_modifier_state(), type=name))


def _pointer_axis(rec, _sid, _time, axis, value):
    win = _POINTER_WIN
    if win is None:
        return
    if axis != WL_POINTER_AXIS_VERTICAL_SCROLL:
        return
    win._axis_notches += axis_delta(value, win._axis_discrete_pending)[0]
    win._axis_discrete_pending = None


def _pointer_frame(rec, _sid):
    """One batch of pointer events ended: deliver what accumulated."""
    win = _POINTER_WIN
    if win is None:
        return
    steps = win._axis_notches
    win._axis_notches = 0
    if steps:
        px, py = win._pointer_css()
        win.dispatch("<MouseWheel>",
                     Event(x=px, y=py, delta=wheel_delta(steps),
                           state=_modifier_state(), type="<MouseWheel>"))


def _pointer_axis_source(rec, _sid, _source):
    pass


def _pointer_axis_stop(rec, _sid, _time, _axis):
    pass


def _pointer_axis_discrete(rec, _sid, axis, discrete):
    win = _POINTER_WIN
    if win is None:
        return
    if axis != WL_POINTER_AXIS_VERTICAL_SCROLL:
        return
    win._axis_discrete_pending = discrete


def _pointer_axis_value120(rec, _sid, _axis, _value):
    pass


def _pointer_axis_relative_direction(rec, _sid, _axis, _direction):
    pass


# -- keyboard ---------------------------------------------------------------

def _keyboard_keymap(rec, _sid, _format, fd, _size):
    """The compositor handed us its keymap on a file descriptor.

    This is the moment a keyboard starts to exist. The keymap is a plain text
    file (format 1); it is read here and the keycode -> keysym tables are
    extracted by the small parser above, instead of compiling it with
    xkbcommon.
    """
    data = b""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        pass
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        if len(data) >= _size:
            break
    try:
        os.close(fd)
    except OSError:
        pass
    _STATE["syms"], _STATE["names"], _STATE["shift"] = parse_keymap(
        data.decode("utf-8", "replace"))


def _keyboard_enter(rec, _sid, serial, surface, _keys):
    global _KEYBOARD_WIN
    _STATE["serial"] = serial
    _KEYBOARD_WIN = _SURFACES.get(surface)


def _keyboard_leave(rec, _sid, serial, _surface):
    global _KEYBOARD_WIN
    _STATE["serial"] = serial
    _KEYBOARD_WIN = None


def _keyboard_key(rec, _sid, serial, _time, key, state):
    win = _KEYBOARD_WIN
    if win is None:
        return
    _STATE["serial"] = serial
    syms = _STATE.get("syms") or {}
    names = _STATE.get("names") or {}
    # The event's key is the raw hardware scan code; the keymap's keycodes
    # section numbers the same physical keys a few higher, and parse_keymap
    # returned how much higher (nothing when it is already evdev-coded).
    key = key + _STATE.get("shift", 0)
    key_syms = syms.get(key)
    if not key_syms:
        return
    pressed = state == 1
    _track_modifiers(names.get(key) or [], pressed)
    state_bits = _modifier_state()
    if pressed:
        level = 1 if (state_bits & STATE_SHIFT and len(key_syms) > 1) else 0
        sym = key_syms[level]
        name = (names.get(key) or [""])[level] or ""
        resolved = keysym_event(name, sym, state_bits)
        if resolved is None:
            return
        keysym_name, char = resolved
        obj = Event(keysym=keysym_name, char=char, state=state_bits,
                    type="<Key>")
        for sequence in key_sequences(keysym_name, state_bits):
            if win.dispatch(sequence, obj):
                return
    else:
        name = (names.get(key) or [""])[0] or ""
        obj = Event(keysym=name, char="", state=state_bits,
                    type="<KeyRelease>")
        for sequence in key_release_sequences(name):
            if win.dispatch(sequence, obj):
                return


def _track_modifiers(names, pressed):
    """Update the held-modifier set and the Caps Lock toggle."""
    for name in names:
        if name in MODIFIER_STATE:
            if pressed:
                _HELD.add(name)
            else:
                _HELD.discard(name)
        if pressed and name in ("Caps_Lock", "CapsLock"):
            _CAPS[0] = not _CAPS[0]


def _modifier_state():
    """Tk's state bits from the modifiers currently held."""
    shift = ("Shift_L" in _HELD or "Shift_R" in _HELD) != _CAPS[0]
    control = "Control_L" in _HELD or "Control_R" in _HELD
    alt = ("Alt_L" in _HELD or "Alt_R" in _HELD
           or "Meta_L" in _HELD or "Meta_R" in _HELD)
    return state_from_xkb(shift, control, alt)


def _keyboard_modifiers(rec, _sid, serial, _depressed, _latched, _locked,
                        _group):
    _STATE["serial"] = serial


def _keyboard_repeat_info(rec, _sid, _rate, _delay):
    pass


# -- clipboard --------------------------------------------------------------

def _data_device():
    """The wl_data_device for the seat, created once."""
    if _STATE.get("data_device"):
        return _STATE["data_device"]
    ddm = _STATE.get("data_device_manager")
    seat = _STATE.get("seat")
    if not ddm or not seat:
        return None
    oid = _conn().new_id("wl_data_device")
    _conn().request(ddm, 1, "no", [oid, seat])
    _STATE["data_device"] = oid
    return oid


def _data_offer(rec, _sid, offer):
    # A server-created object; register it so its own events route.
    _conn().add_object(offer, "wl_data_offer")


def _data_enter(rec, _sid, serial, _surface, _x, _y, _offer):
    _STATE["serial"] = serial


def _data_leave(rec, _sid):
    pass


def _data_motion(rec, _sid, _time, _x, _y):
    pass


def _data_drop(rec, _sid):
    pass


def _data_selection(rec, _sid, offer):
    """The compositor told us what is on the clipboard."""
    _STATE["clipboard_offer"] = offer


def _data_source_target(rec, _sid, _mime):
    pass


def _data_source_send(rec, _sid, _mime, fd):
    """Another client pasted something we own; write it down the pipe."""
    text = _STATE.get("clipboard_text", "").encode("utf-8")
    try:
        os.write(fd, text)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _data_source_cancelled(rec, _sid):
    if _STATE.get("clipboard_source"):
        _conn().objects.pop(_STATE["clipboard_source"], None)
        _STATE["clipboard_source"] = None
    _STATE["clipboard_text"] = ""


def _data_source_dnd_drop_performed(rec, _sid):
    pass


def _data_source_dnd_finished(rec, _sid):
    pass


def _data_source_action(rec, _sid, _action):
    pass


def _data_offer_offer(rec, _sid, _mime):
    pass


def _data_offer_source_actions(rec, _sid, _actions):
    pass


def _data_offer_action(rec, _sid, _action):
    pass


def _set_clipboard(text):
    """Offer `text` as the system clipboard via the data device."""
    conn = _conn()
    dev = _data_device()
    ddm = _STATE.get("data_device_manager")
    if dev is None or not ddm:
        return
    src = conn.new_id("wl_data_source")
    conn.request(ddm, 0, "n", [src])            # create_data_source
    conn.request(src, 0, "s", [CLIPBOARD_MIME])  # data_source.offer
    conn.request(dev, 1, "ou", [src, _STATE["serial"]])  # set_selection
    old = _STATE.get("clipboard_source")
    if old:
        conn.objects.pop(old, None)
    _STATE["clipboard_source"] = src
    _STATE["clipboard_text"] = text


def _get_clipboard():
    """Read the system clipboard, best-effort.

    A paste is an asynchronous transfer: the compositor hands us a
    wl_data_offer, we ask it to receive into a pipe, and the other
    application writes into that pipe at its own pace. So this pumps the
    socket for up to SELECTION_TIMEOUT while the data arrives, exactly the
    way x11.py waits for its selection round trip.
    """
    offer = _STATE.get("clipboard_offer")
    if not offer:
        return ""
    read_fd, write_fd = os.pipe()
    _conn().request(offer, 1, "sh", [CLIPBOARD_MIME, read_fd])
    os.close(read_fd)
    chunks = []
    deadline = time.monotonic() + SELECTION_TIMEOUT
    while time.monotonic() < deadline:
        ready, _, _ = select.select([write_fd], [], [], 0.01)
        if ready:
            try:
                data = os.read(write_fd, 65536)
            except OSError:
                data = b""
            if data:
                chunks.append(data)
                break
        _drain()
    os.close(write_fd)
    if _STATE.get("clipboard_offer"):
        _conn().objects.pop(_STATE["clipboard_offer"], None)
        _STATE["clipboard_offer"] = None
    return b"".join(chunks).decode("utf-8", "replace")





def _buffer_release(rec, _sid):
    """The compositor is done with a buffer; hand it back to its window."""
    win = rec.get("window")
    if win is not None:
        win._release_buffer(_sid)


def _xdg_configure(rec, _sid, serial):
    win = rec.get("window")
    if win is not None:
        win._on_xdg_configure(serial)


def _toplevel_configure(rec, _sid, width, height, states):
    win = rec.get("window")
    if win is not None:
        win._on_toplevel_configure(width, height, states)


def _toplevel_close(rec, _sid):
    win = rec.get("window")
    if win is not None:
        win.destroy()


def _conn():
    return _STATE["conn"]



# -- event tables -----------------------------------------------------------
#
# interface -> opcode -> (signature, handler). Only the events we can
# receive are listed; an opcode we never see needs no entry.

_EVENTS = {
    "wl_display": {
        0: ("ous", _display_error),
        1: ("u", _display_delete_id),
    },
    "wl_registry": {
        0: ("usu", _global),
        1: ("u", _global_remove),
    },
    "wl_surface": {
        0: ("o", _surface_enter),
        1: ("o", _surface_leave),
        2: ("i", _surface_preferred_buffer_scale),
        3: ("u", _surface_preferred_buffer_transform),
    },
    "wl_shm": {
        0: ("u", _shm_format),
    },
    "wl_buffer": {
        0: ("", _buffer_release),
    },
    "wl_callback": {
        0: ("u", _callback_done),
    },
    "wl_seat": {
        0: ("u", _seat_capabilities),
        1: ("s", _seat_name),
    },
    "wl_pointer": {
        0: ("uoff", _pointer_enter),
        1: ("uo", _pointer_leave),
        2: ("uff", _pointer_motion),
        3: ("uuuu", _pointer_button),
        4: ("uuf", _pointer_axis),
        5: ("", _pointer_frame),
        6: ("u", _pointer_axis_source),
        7: ("uu", _pointer_axis_stop),
        8: ("ui", _pointer_axis_discrete),
        9: ("ui", _pointer_axis_value120),
        10: ("uu", _pointer_axis_relative_direction),
    },
    "wl_keyboard": {
        0: ("uhu", _keyboard_keymap),
        1: ("uoa", _keyboard_enter),
        2: ("uo", _keyboard_leave),
        3: ("uuuu", _keyboard_key),
        4: ("uuuuu", _keyboard_modifiers),
        5: ("ii", _keyboard_repeat_info),
    },
    "wl_output": {
        0: ("iiiiissi", _output_geometry),
        1: ("uiii", _output_mode),
        2: ("", _output_done),
        3: ("i", _output_scale),
        4: ("s", _output_name),
        5: ("s", _output_description),
    },
    "xdg_wm_base": {
        0: ("u", _ping),
    },
    "xdg_surface": {
        0: ("u", _xdg_configure),
    },
    "xdg_toplevel": {
        0: ("iia", _toplevel_configure),
        1: ("", _toplevel_close),
    },
    "wl_data_device": {
        0: ("n", _data_offer),
        1: ("uoffo", _data_enter),
        2: ("", _data_leave),
        3: ("uff", _data_motion),
        4: ("", _data_drop),
        5: ("o", _data_selection),
    },
    "wl_data_source": {
        0: ("s", _data_source_target),
        1: ("sh", _data_source_send),
        2: ("", _data_source_cancelled),
        3: ("", _data_source_dnd_drop_performed),
        4: ("", _data_source_dnd_finished),
        5: ("u", _data_source_action),
    },
    "wl_data_offer": {
        0: ("s", _data_offer_offer),
        1: ("u", _data_offer_source_actions),
        2: ("u", _data_offer_action),
    },
}

# -- the window -------------------------------------------------------------

class WaylandWindow(Window):
    """A titled, resizable Wayland toplevel presenting a raster surface."""

    def __init__(self, width=1000, height=720, title="FeetBrowser"):
        conn = _connect()
        super().__init__(width, height, title)
        self._conn = conn
        self._closed = False
        self._repaint = True
        self._configured = False
        self._configure_serial = 0
        self._surface = conn.new_id("wl_surface")
        _SURFACES[self._surface] = self
        conn.request(_STATE["compositor"], 0, "n", [self._surface])
        self._xdg_surface = conn.new_id("xdg_surface", window=self)
        conn.request(_STATE["xdg_wm_base"], 2, "no",
                     [self._xdg_surface, self._surface])
        self._toplevel = conn.new_id("xdg_toplevel", window=self)
        conn.request(self._xdg_surface, 1, "n", [self._toplevel])
        conn.request(self._toplevel, 3, "s", ["feetbrowser"])  # set_app_id
        conn.request(self._toplevel, 8, "ii", [0, 0])           # set_min_size
        self.on_title_changed(title)
        self._buffers = []
        self._attached = None
        self._buffer_size = (0, 0)
        self._pointer_inside = False
        self._axis_notches = 0
        self._axis_discrete_pending = None
        self._button_held = 0
        self._cursor_name = None
        self._last_x = 0.0
        self._last_y = 0.0
        self.set_scale(_STATE["scale"])
        conn.request(self._surface, 8, "i",
                     [max(1, int(round(self.scale)))])   # set_buffer_scale
        # First commit: map the toplevel.
        self._ack_configure()
        conn.request(self._surface, 6, "", [])            # commit

    # -- buffers ------------------------------------------------------------

    def _acquire_buffer(self, width, height):
        """A free wl_shm buffer of this size, creating one if needed."""
        for buf in self._buffers:
            if buf.free and buf.width == width and buf.height == height:
                buf.free = False
                return buf
        buf = self._make_buffer(width, height)
        if buf is None:
            return None
        self._buffers.append(buf)
        return buf

    def _make_buffer(self, width, height):
        size = width * height * 4
        try:
            fd = os.memfd_create("feetbrowser-shm", 0)
        except (AttributeError, OSError):
            fd = _shm_fd()
        if fd is None:
            return None
        try:
            os.ftruncate(fd, size)
            mem = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ
                            | mmap.PROT_WRITE)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        conn = self._conn
        pool = conn.new_id("wl_shm_pool")
        conn.request(_STATE["shm"], 0, "nhi", [pool, fd, size])
        buffer = conn.new_id("wl_buffer", window=self)
        conn.request(pool, 0, "niiiiu",
                     [buffer, 0, width, height, width * 4,
                      WL_SHM_FORMAT_XRGB8888])
        return _ShmBuffer(proxy=buffer, pool=pool, fd=fd, mem=mem,
                          width=width, height=height, free=False)

    def _release_buffer(self, buffer_id):
        """The compositor is done with a buffer; it is safe to reuse."""
        for buf in self._buffers:
            if buf.proxy == buffer_id:
                buf.free = True
                return

    def present(self):
        if self._closed:
            return
        canvas = self.canvas
        if canvas is None:
            return
        if not self._configured:
            # No configure acknowledged yet, so attaching a buffer would be a
            # protocol violation; the compositor has not told us the surface
            # is drawable.
            return
        if not canvas.dirty and not self._repaint and self._attached:
            return
        surface = canvas.render()
        width, height = surface.width, surface.height
        if (width, height) != self._buffer_size:
            self._drop_buffers()
            self._buffer_size = (width, height)
        buf = self._acquire_buffer(width, height)
        if buf is None:
            return
        buf.mem[:] = pack_xrgb(surface.pixels, width, height, surface.stride)
        self._ack_configure()
        conn = self._conn
        conn.request(self._surface, 1, "oii", [buf.proxy, 0, 0])   # attach
        conn.request(self._surface, 9, "iiii", [0, 0, width, height])
        conn.request(self._surface, 6, "", [])                      # commit
        self._attached = buf
        self._repaint = False

    def _drop_buffers(self):
        for buf in self._buffers:
            try:
                buf.mem.close()
            except BufferError:
                pass
            self._conn.objects.pop(buf.proxy, None)
            self._conn.objects.pop(buf.pool, None)
            try:
                os.close(buf.fd)
            except OSError:
                pass
        self._buffers = []
        self._attached = None

    # -- geometry -----------------------------------------------------------

    def _ack_configure(self):
        if self._configure_serial:
            self._conn.request(self._xdg_surface, 4, "u",
                               [self._configure_serial])
            self._configure_serial = 0

    def resize(self, width, height, device=None):
        super().resize(width, height, device)
        self._repaint = True

    def set_scale(self, scale, device=None):
        if super().set_scale(scale, device):
            self._repaint = True
            self._conn.request(self._surface, 8, "i",
                               [max(1, int(round(self.scale)))])

    # -- input --------------------------------------------------------------

    def poll_events(self):
        """Drain the socket without blocking, and route events.

        All Wayland windows share one connection, so each window drains the
        whole queue; the router hands each event to the surface it names and
        the rest of the loop never sees a duplicate, because a drained
        socket is empty.
        """
        if self._closed or self._conn.dead:
            return False
        before = len(self._conn.objects)
        _drain()
        return len(self._conn.objects) != before

    def _motion_binding(self):
        held = getattr(self, "_button_held", 0)
        if held:
            return "<B%d-Motion>" % held, held
        return "<Motion>", 0

    def _pointer_css(self):
        return self.to_css(int(self._last_x), int(self._last_y))

    # -- window chrome ------------------------------------------------------

    def on_title_changed(self, title):
        if self._closed:
            return
        self._conn.request(self._toplevel, 2, "s", [title])  # set_title

    def _apply_cursor(self):
        """Honour the pointer the canvas asked for, when it changes.

        The compositor draws the pointer, so naming the shape is a two-word
        request on the cursor-shape object rather than an image to ship. The
        serial is the one from the last pointer-enter, which is what lets the
        compositor tell a stale request from a live one; with no shape object
        (no cursor-shape manager) nothing is sent and the compositor shows
        its default."""
        wanted = getattr(self.canvas, "cursor", "") if self.canvas else ""
        if wanted == self._cursor_name or self._closed:
            return
        self._cursor_name = wanted
        shape = CURSOR_SHAPES.get(wanted, SHAPE_DEFAULT)
        oid = _STATE.get("pointer_shape")
        ptr = _STATE.get("pointer")
        serial = _STATE.get("pointer_serial")
        if oid and ptr and serial:
            self._conn.request(oid, 0, "ouu", [ptr, serial, shape])

    def _on_xdg_configure(self, serial):
        """The configure sequence for a state change is complete.

        This is the event that makes the surface legal to draw on: the
        xdg-shell spec forbids attaching a buffer before the first configure
        has been acknowledged, so until this arrives ``present()`` stays
        quiet. The ack is sent here, before the commit it authorises.
        """
        self._configure_serial = serial
        self._configured = True
        self._ack_configure()

    def _on_toplevel_configure(self, width, height, _states):
        # The compositor's word is final; honour it even when it is "no
        # opinion" (0x0), which means keep what we have.
        if width and height:
            scale = self.scale
            self.resize(width, height,
                        (int(round(width * scale)),
                         int(round(height * scale))))
        self._repaint = True

    def _on_toplevel_close(self):
        self.destroy()

    # -- clipboard ----------------------------------------------------------

    def on_clipboard_set(self, text):
        self._clipboard = text
        _set_clipboard(text)

    def on_primary_set(self, text):
        # Wayland has no PRIMARY selection; nothing to do. The selection
        # path that does exist is the data device, which the clipboard uses.
        pass

    def on_clipboard_get(self):
        if _STATE.get("clipboard_source"):
            # Ours. Asking the compositor for our own text is a round trip
            # with nothing at the other end but us; the text is right here.
            return self._clipboard
        got = _get_clipboard()
        return got or self._clipboard

    # -- teardown ----------------------------------------------------------

    def on_destroy(self):
        if self._closed:
            return
        self._closed = True
        _SURFACES.pop(self._surface, None)
        self._drop_buffers()
        for oid in (self._toplevel, self._xdg_surface, self._surface):
            self._conn.objects.pop(oid, None)
        self._conn.sock.close()
        self._conn.dead = True
        _close_display()


class _ShmBuffer:
    """A shared-memory buffer: object ids, the fd, and the mmap."""

    __slots__ = ("proxy", "pool", "fd", "mem", "width", "height", "free")

    def __init__(self, proxy, pool, fd, mem, width, height, free):
        self.proxy = proxy
        self.pool = pool
        self.fd = fd
        self.mem = mem
        self.width = width
        self.height = height
        self.free = free


def _shm_fd():
    """A shared-memory fd via shm_open, when memfd_create is unavailable."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        name = b"/feetbrowser-%d" % os.getpid()
        libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                  ctypes.c_uint]
        libc.shm_open.restype = ctypes.c_int
        fd = libc.shm_open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        if fd >= 0:
            libc.shm_unlink(name)
            return fd
    except Exception:    # noqa: BLE001 - fall through to failure
        pass
    return None


def _close_display():
    """Drop the connection once the last window has gone."""
    if _SURFACES or "conn" not in _STATE:
        return
    _STATE["conn"].sock.close()
    _STATE.clear()


def _drain():
    """Read and dispatch everything the compositor has sent, never blocking."""
    conn = _STATE.get("conn")
    if conn:
        conn.pump()


def error():
    """The last protocol error the compositor reported, or ""."""
    conn = _STATE.get("conn")
    return conn.last_error if conn and conn.last_error else ""


class WaylandToplevel(WaylandWindow):
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


class WaylandTk(WaylandWindow):
    """The root window, matching ``window.Tk``."""

    def __init__(self, *_args, **kwargs):
        super().__init__(**kwargs)
        self.tk = None  # layout's batched-measure path checks for this


# gui.Toplevel reads this off the master, so a popup opened from a real
# window is a real window and one opened from a headless root stays headless.
WaylandWindow.toplevel_class = WaylandToplevel


def available():
    """True when a Wayland window can actually be created here."""
    global _problem
    _problem = ""
    try:
        _connect()
    except WaylandUnavailable as exc:
        _problem = str(exc)
        return False
    return True


def unavailable_reason():
    """Why available() last said no, or "" when there is nothing to say."""
    return _problem
