"""A real macOS window, with no toolkit and no bindings package.

AppKit is an Objective-C framework, and Objective-C is a C library with a
message dispatcher. That is all ``objc_msgSend`` is, so ctypes is enough to
drive it: look up a class, register a selector, call the function. This module
does exactly that and nothing more -- no PyObjC, no Rubicon, no compiled
shim.

What it adds to ``window.Window`` is the two things a headless window cannot
have: real input (``poll_events``) and somewhere to put the pixels
(``present``). Everything above it still sees Tk's API, because translation
happens here -- Cocoa event types and key codes become ``<Button-1>``,
``<Control-l>``, ``<MouseWheel>``.

Presenting is a CGImage wrapped straight around the framebuffer: our RGB
bytes are already a valid 24-bit bitmap, so there is no format conversion in
the frame path, just a data provider handed to an ``NSImageView``.

Cocoa is the one backend where the two coordinate systems are already kept
apart for us. Everything AppKit reports -- a view's bounds, a mouse location,
a window frame -- is in points, and a point is exactly what the rest of the
browser calls a CSS pixel, so no event coordinate is ever converted here. The
only place the difference surfaces is the frame: on a Retina display one point
is two device pixels, and the framebuffer is allocated in device pixels, so
the CGImage is twice the size of the NSImage that carries it. Saying so is the
whole HiDPI fix -- an NSImage whose size is the image's pixel count claims a
density of 1 and gets stretched over the backing store, which is soft.
"""
import ctypes
import ctypes.util
import platform
import sys

from .window import (QUIET, STATE_ALT, STATE_CONTROL, STATE_SHIFT, Event,
                     Window, key_sequences)

_FRAMEWORKS = {
    "objc": "/usr/lib/libobjc.A.dylib",
    "appkit": "/System/Library/Frameworks/AppKit.framework/AppKit",
    "foundation": "/System/Library/Frameworks/Foundation.framework/Foundation",
    "cg": "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
}

# Cocoa constants we need. Named here rather than inline so the calls below
# read like the Objective-C they are.
_STYLE_TITLED = 1
_STYLE_CLOSABLE = 2
_STYLE_MINIATURIZABLE = 4
_STYLE_RESIZABLE = 8
_BACKING_BUFFERED = 2
_SCALE_AXES_INDEPENDENTLY = 1
_ACTIVATION_REGULAR = 0
# Accessory is Regular minus the demands for attention: no Dock icon, no
# entry in the application switcher, and no becoming the active application
# on its own. Windows still open, draw and receive posted events.
_ACTIVATION_ACCESSORY = 1
_EVENT_MASK_ANY = 0xFFFFFFFFFFFFFFFF

# NSEventType
_LEFT_DOWN, _LEFT_UP = 1, 2
_RIGHT_DOWN, _RIGHT_UP = 3, 4
_MOUSE_MOVED = 5
_LEFT_DRAGGED, _RIGHT_DRAGGED = 6, 7
_KEY_DOWN, _KEY_UP = 10, 11
_SCROLL_WHEEL = 22
_OTHER_DOWN, _OTHER_UP, _OTHER_DRAGGED = 25, 26, 27

# NSEventModifierFlags
_MOD_SHIFT = 1 << 17
_MOD_CONTROL = 1 << 18
_MOD_OPTION = 1 << 19
_MOD_COMMAND = 1 << 20

# Virtual key codes -> Tk keysyms, for the keys that carry no character.
_KEYSYMS = {
    36: "Return", 76: "KP_Enter", 48: "Tab", 51: "BackSpace", 53: "Escape",
    117: "Delete", 123: "Left", 124: "Right", 125: "Down", 126: "Up",
    115: "Home", 119: "End", 116: "Prior", 121: "Next",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
    98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
}

# Tk cursor names -> NSCursor factory selectors.
_CURSORS = {
    "": "arrowCursor",
    "arrow": "arrowCursor",
    "hand2": "pointingHandCursor",
    "hand1": "openHandCursor",
    "xterm": "IBeamCursor",
    "watch": "arrowCursor",  # no busy cursor without a spinner
}


class CocoaUnavailable(RuntimeError):
    """Raised when this platform cannot supply a Cocoa window."""


# NSWindow pointer -> CocoaWindow. Cocoa has one event queue per application,
# not per window, so whoever pumps drains events for everybody and routes each
# one to the window it belongs to. That is what lets the root's main loop feed
# a popup Toplevel, which is how the browser has always worked.
_WINDOWS = {}


# -- Objective-C runtime ---------------------------------------------------

class NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class NSRect(ctypes.Structure):
    _fields_ = [("origin", NSPoint), ("size", NSSize)]


_libs = {}
_sels = {}
_classes = {}
_protos = {}

# On x86_64 a struct larger than 16 bytes comes back through a hidden pointer,
# which is a different function (objc_msgSend_stret). arm64 has no such split.
_STRET = "objc_msgSend" if platform.machine() == "arm64" else \
    "objc_msgSend_stret"


def _load():
    """Open the frameworks. Idempotent; raises CocoaUnavailable off macOS."""
    if _libs:
        return
    if sys.platform != "darwin":
        raise CocoaUnavailable("Cocoa windows need macOS")
    try:
        for name, path in _FRAMEWORKS.items():
            _libs[name] = ctypes.cdll.LoadLibrary(path)
    except OSError as exc:
        _libs.clear()
        raise CocoaUnavailable("cannot load AppKit: %s" % exc) from exc
    objc = _libs["objc"]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]


def _sel(name):
    try:
        return _sels[name]
    except KeyError:
        value = _libs["objc"].sel_registerName(name.encode())
        _sels[name] = value
        return value


def _cls(name):
    try:
        return _classes[name]
    except KeyError:
        value = _libs["objc"].objc_getClass(name.encode())
        if not value:
            raise CocoaUnavailable("no Objective-C class %r" % name)
        _classes[name] = value
        return value


def _dispatcher(entry, restype, argtypes):
    """A calling convention for one (function, signature) pair, cached.

    ctypes needs the full prototype to marshal struct arguments and returns
    correctly, and objc_msgSend is variadic, so there is one of these per
    distinct signature rather than one overall.
    """
    key = (entry, restype, argtypes)
    try:
        return _protos[key]
    except KeyError:
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                 *argtypes)
        address = ctypes.cast(getattr(_libs["objc"], entry),
                              ctypes.c_void_p).value
        fn = proto(address)
        _protos[key] = fn
        return fn


def msg(receiver, selector, *args, restype=ctypes.c_void_p, argtypes=()):
    """Send `selector` to `receiver`. The whole Objective-C bridge."""
    return _dispatcher("objc_msgSend", restype, tuple(argtypes))(
        receiver, _sel(selector), *args)


def msg_rect(receiver, selector):
    """Send a selector that returns an NSRect, on either ABI."""
    return _dispatcher(_STRET, NSRect, ())(receiver, _sel(selector))


def nsstring(text):
    return msg(_cls("NSString"), "stringWithUTF8String:", text.encode(),
               argtypes=(ctypes.c_char_p,))


def from_nsstring(handle):
    if not handle:
        return ""
    raw = msg(handle, "UTF8String", restype=ctypes.c_char_p)
    return raw.decode("utf-8", "replace") if raw else ""


class pool:
    """An autorelease pool for one block of work.

    Cocoa hands back autoreleased objects -- events, strings, images -- and
    without a pool in place they simply accumulate. There is no
    NSApplicationMain here to open one, so every path that talks to AppKit
    repeatedly brackets itself with this.
    """

    __slots__ = ("_handle",)

    def __enter__(self):
        self._handle = msg(msg(_cls("NSAutoreleasePool"), "alloc"), "init")
        return self

    def __exit__(self, *_exc):
        msg(self._handle, "drain")
        return False


# -- the window ------------------------------------------------------------

class CocoaWindow(Window):
    """A titled, resizable macOS window presenting a raster surface."""

    def __init__(self, width=1000, height=720, title="FeetBrowser"):
        _load()
        super().__init__(width, height, title)
        self._app = msg(_cls("NSApplication"), "sharedApplication")
        msg(self._app, "setActivationPolicy:",
            _ACTIVATION_ACCESSORY if QUIET else _ACTIVATION_REGULAR,
            argtypes=(ctypes.c_long,))
        style = (_STYLE_TITLED | _STYLE_CLOSABLE | _STYLE_MINIATURIZABLE
                 | _STYLE_RESIZABLE)
        rect = NSRect(NSPoint(0.0, 0.0), NSSize(float(width), float(height)))
        self._window = msg(
            msg(_cls("NSWindow"), "alloc"),
            "initWithContentRect:styleMask:backing:defer:",
            rect, style, _BACKING_BUFFERED, False,
            argtypes=(NSRect, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool))
        msg(self._window, "setReleasedWhenClosed:", False,
            argtypes=(ctypes.c_bool,))
        msg(self._window, "setAcceptsMouseMovedEvents:", True,
            argtypes=(ctypes.c_bool,))
        msg(self._window, "setTitle:", nsstring(title),
            argtypes=(ctypes.c_void_p,))
        self._view = msg(msg(_cls("NSImageView"), "alloc"), "initWithFrame:",
                         rect, argtypes=(NSRect,))
        msg(self._view, "setImageScaling:", _SCALE_AXES_INDEPENDENTLY,
            argtypes=(ctypes.c_long,))
        msg(self._window, "setContentView:", self._view,
            argtypes=(ctypes.c_void_p,))
        if QUIET:
            # Ordered in, so it is on screen and drawable, but behind
            # everything and never made key: the keyboard stays wherever the
            # user left it.
            msg(self._window, "orderBack:", None, argtypes=(ctypes.c_void_p,))
        else:
            msg(self._window, "center")
            msg(self._window, "makeKeyAndOrderFront:", None,
                argtypes=(ctypes.c_void_p,))
            msg(self._app, "activateIgnoringOtherApps:", True,
                argtypes=(ctypes.c_bool,))
        msg(self._app, "finishLaunching")
        # These outlive any autorelease pool, so they are retained explicitly.
        self._distant_past = msg(msg(_cls("NSDate"), "distantPast"), "retain")
        self._run_mode = msg(nsstring("kCFRunLoopDefaultMode"), "retain")
        # Signatures must be declared before the first call: ctypes defaults a
        # return type to c_int, which silently truncates a 64-bit pointer.
        self._prepare_cg()
        self._colorspace = _libs["cg"].CGColorSpaceCreateDeviceRGB()
        self._buffers = []      # keeps frame data alive while AppKit draws it
        self._images = []
        self._cursor = None
        self._closed = False
        # Now that there is a window there is a display behind it, so the
        # scale is knowable. It has to be settled before anyone makes a
        # canvas, because the canvas reads it off us to size its buffer.
        self.set_scale(self._backing_scale())
        _WINDOWS[int(self._window)] = self

    def _prepare_cg(self):
        cg = _libs["cg"]
        cg.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p
        cg.CGDataProviderCreateWithData.restype = ctypes.c_void_p
        cg.CGDataProviderCreateWithData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p]
        cg.CGImageCreate.restype = ctypes.c_void_p
        cg.CGImageCreate.argtypes = [
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_bool, ctypes.c_uint32]
        cg.CGImageRelease.argtypes = [ctypes.c_void_p]
        cg.CGDataProviderRelease.argtypes = [ctypes.c_void_p]

    # -- geometry ----------------------------------------------------------

    def _content_size(self):
        bounds = msg_rect(self._view, "bounds")
        return int(round(bounds.size.width)), int(round(bounds.size.height))

    def _backing_scale(self):
        """Device pixels per point, as the display currently says.

        The window is asked rather than the screen because a window can be
        dragged from a Retina display to an external one and the answer
        changes under it; the screen is only the fallback for the moment
        before the window is on one. `backingScaleFactor` returns a CGFloat,
        which is a double here and has to be declared as one -- ctypes would
        otherwise read the integer register, which holds nothing, and every
        display would look like scale 0.
        """
        for receiver in (self._window,
                         msg(_cls("NSScreen"), "mainScreen")):
            if not receiver:
                continue
            value = msg(receiver, "backingScaleFactor",
                        restype=ctypes.c_double)
            if value:
                return float(value)
        return 1.0

    def resize(self, width, height):
        """Resize from our side, e.g. a geometry() call before the first frame."""
        super().resize(width, height)
        if self._closed:
            return
        frame = msg_rect(self._window, "frame")
        content = NSRect(frame.origin,
                         NSSize(float(self.width), float(self.height)))
        wanted = _dispatcher(_STRET, NSRect, (NSRect,))(
            self._window, _sel("frameRectForContentRect:"), content)
        msg(self._window, "setFrame:display:", wanted, True,
            argtypes=(NSRect, ctypes.c_bool))

    def _sync_size(self):
        """Pick up a resize the user made by dragging the window edge, and a
        change of display density under it.

        The scale is polled rather than observed: there is no delegate here to
        receive `windowDidChangeBackingProperties:`, and a poll once per event
        drain is a single message send against a value that changes when
        somebody drags a window between monitors. Reallocating the buffer is
        enough on its own -- nothing above cares -- but the frame currently on
        screen was drawn for the old density, so the canvas is marked dirty
        and the next present redraws it.
        """
        width, height = self._content_size()
        if (width, height) != (self.width, self.height) and width and height:
            Window.resize(self, width, height)
        if self.set_scale(self._backing_scale()) and self.canvas is not None:
            self.canvas.dirty = True

    # -- presenting --------------------------------------------------------

    def present(self):
        canvas = self.canvas
        if canvas is None or self._closed:
            return
        if not canvas.dirty and self._images:
            return
        surface = canvas.render()
        with pool():
            self._push(surface)

    def _push(self, surface):
        cg = _libs["cg"]
        # The provider must outlive the CGImage, and the CGImage outlives this
        # call because AppKit draws asynchronously. Holding the last couple of
        # frames is cheaper than copying into a CFData.
        data = (ctypes.c_char * len(surface.pixels)).from_buffer_copy(
            surface.pixels)
        provider = cg.CGDataProviderCreateWithData(None, data,
                                                   len(surface.pixels), None)
        image = cg.CGImageCreate(surface.width, surface.height, 8, 24,
                                 surface.stride, self._colorspace, 0,
                                 provider, None, False, 0)
        # An NSImage's size is in points, not pixels, and the difference
        # between the two is what makes a Retina frame sharp: a 1600x1200
        # CGImage declared as 800x600 points is a 2x representation that
        # AppKit draws one image pixel to one device pixel, while the same
        # CGImage declared as 1600x1200 points is a 1x image that the view
        # then shrinks -- or, before this buffer was allocated in device
        # pixels at all, an 800x600 1x image stretched over twice its area,
        # which was the blur.
        size = NSSize(surface.width / self.scale, surface.height / self.scale)
        nsimage = msg(msg(_cls("NSImage"), "alloc"), "initWithCGImage:size:",
                      image, size,
                      argtypes=(ctypes.c_void_p, NSSize))
        msg(self._view, "setImage:", nsimage, argtypes=(ctypes.c_void_p,))
        cg.CGImageRelease(image)
        cg.CGDataProviderRelease(provider)
        self._buffers.append(data)
        self._images.append(nsimage)
        if len(self._buffers) > 2:
            msg(self._images.pop(0), "release")
            self._buffers.pop(0)

    # -- input -------------------------------------------------------------

    def poll_events(self):
        """Drain the Cocoa queue, translating each event into a binding."""
        if self._closed:
            return False
        with pool():
            return self._drain()

    def _drain(self):
        delivered = False
        while True:
            event = msg(self._app,
                        "nextEventMatchingMask:untilDate:inMode:dequeue:",
                        _EVENT_MASK_ANY, self._distant_past, self._run_mode,
                        True,
                        argtypes=(ctypes.c_ulonglong, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_bool))
            if not event:
                break
            delivered = True
            # AppKit first: this is what makes the close button, the title bar
            # drag and live resize work at all.
            handle = msg(event, "window")
            msg(self._app, "sendEvent:", event, argtypes=(ctypes.c_void_p,))
            owner = _WINDOWS.get(int(handle)) if handle else self
            if owner is None:
                continue
            try:
                owner._translate(event)
            except Exception as exc:  # noqa: BLE001 - never lose the loop
                owner.on_callback_error("event", exc)
        self._sync_size()
        self._apply_cursor()
        # No window delegate, so the close button is detected by the window
        # going away. Only when we did not put it away ourselves: withdraw()
        # orders it out too, and that must not read as "the user quit".
        if self.visible and not msg(self._window, "isVisible",
                                    restype=ctypes.c_bool):
            self._closed = True
            self.destroy()
        return delivered

    def _translate(self, event):
        kind = msg(event, "type", restype=ctypes.c_ulonglong)
        if kind in (_KEY_DOWN, _KEY_UP):
            if kind == _KEY_DOWN:
                self._on_key(event)
            return
        if kind == _SCROLL_WHEEL:
            self._on_wheel(event)
            return
        x, y = self._mouse(event)
        state = self._state(event)
        if kind == _MOUSE_MOVED:
            self.dispatch("<Motion>", Event(x=x, y=y, state=state,
                                            type="<Motion>"))
        elif kind == _LEFT_DOWN:
            self.dispatch("<Button-1>", Event(x=x, y=y, num=1, state=state,
                                              type="<Button-1>"))
        elif kind == _LEFT_UP:
            self.dispatch("<ButtonRelease-1>",
                          Event(x=x, y=y, num=1, state=state,
                                type="<ButtonRelease-1>"))
        elif kind == _LEFT_DRAGGED:
            self.dispatch("<B1-Motion>", Event(x=x, y=y, num=1, state=state,
                                               type="<B1-Motion>"))
        elif kind == _RIGHT_DOWN:
            self.dispatch("<Button-3>", Event(x=x, y=y, num=3, state=state,
                                              type="<Button-3>"))
        elif kind == _RIGHT_UP:
            self.dispatch("<ButtonRelease-3>",
                          Event(x=x, y=y, num=3, state=state,
                                type="<ButtonRelease-3>"))
        elif kind == _RIGHT_DRAGGED:
            self.dispatch("<B3-Motion>", Event(x=x, y=y, num=3, state=state,
                                               type="<B3-Motion>"))
        elif kind in (_OTHER_DOWN, _OTHER_UP, _OTHER_DRAGGED):
            number = msg(event, "buttonNumber", restype=ctypes.c_long)
            if number == 2:  # the wheel button; Tk calls it Button-2
                name = {_OTHER_DOWN: "<Button-2>",
                        _OTHER_UP: "<ButtonRelease-2>",
                        _OTHER_DRAGGED: "<B2-Motion>"}[kind]
                self.dispatch(name, Event(x=x, y=y, num=2, state=state,
                                          type=name))

    def _mouse(self, event):
        """Cocoa is bottom-left origin; the canvas is top-left."""
        location = msg(event, "locationInWindow", restype=NSPoint)
        local = _dispatcher("objc_msgSend", NSPoint,
                            (NSPoint, ctypes.c_void_p))(
            self._view, _sel("convertPoint:fromView:"), location, None)
        _width, height = self._content_size()
        return int(local.x), int(height - local.y)

    def _state(self, event):
        flags = msg(event, "modifierFlags", restype=ctypes.c_ulonglong)
        state = 0
        if flags & _MOD_SHIFT:
            state |= STATE_SHIFT
        # Command is where a Mac user's muscle memory puts Tk's Control, and
        # the browser reads state & 0x4 for its shortcuts, so both map there.
        if flags & (_MOD_CONTROL | _MOD_COMMAND):
            state |= STATE_CONTROL
        if flags & _MOD_OPTION:
            state |= STATE_ALT
        return state

    def _on_wheel(self, event):
        dy = msg(event, "scrollingDeltaY", restype=ctypes.c_double)
        if not dy:
            return
        precise = msg(event, "hasPreciseScrollingDeltas",
                      restype=ctypes.c_bool)
        # browser.py treats |delta| < 30 as a pixel count, so stay under it
        # and let a fast flick arrive as several events.
        delta = int(dy * (3 if precise else 20))
        if not delta:
            delta = 1 if dy > 0 else -1
        delta = max(-29, min(29, delta))
        x, y = self._mouse(event)
        self.dispatch("<MouseWheel>", Event(x=x, y=y, delta=delta,
                                            state=self._state(event),
                                            type="<MouseWheel>"))

    def _on_key(self, event):
        code = msg(event, "keyCode", restype=ctypes.c_ushort)
        chars = from_nsstring(msg(event, "charactersIgnoringModifiers"))
        state = self._state(event)
        keysym = _KEYSYMS.get(code)
        char = ""
        if keysym is None:
            keysym = chars[:1]
            if keysym and keysym.isprintable():
                char = keysym
            if keysym == " ":
                keysym = "space"
        elif keysym == "Return":
            char = "\r"
        elif keysym == "Tab":
            keysym = "ISO_Left_Tab" if state & STATE_SHIFT else "Tab"
        if not keysym:
            return
        event_obj = Event(keysym=keysym, char=char, state=state, type="<Key>")
        for sequence in key_sequences(keysym, state):
            if self.dispatch(sequence, event_obj):
                return

    def _apply_cursor(self):
        wanted = getattr(self.canvas, "cursor", "") if self.canvas else ""
        if wanted == self._cursor:
            return
        if not msg(self._window, "isKeyWindow", restype=ctypes.c_bool):
            return  # only the focused window owns the pointer
        self._cursor = wanted
        selector = _CURSORS.get(wanted, "arrowCursor")
        msg(msg(_cls("NSCursor"), selector), "set")

    # -- window chrome -----------------------------------------------------

    def on_title_changed(self, title):
        if self._closed:
            return
        with pool():
            msg(self._window, "setTitle:", nsstring(title),
                argtypes=(ctypes.c_void_p,))

    def minsize(self, width, height):
        super().minsize(width, height)
        msg(self._window, "setContentMinSize:",
            NSSize(float(width), float(height)), argtypes=(NSSize,))

    def on_destroy(self):
        _WINDOWS.pop(int(self._window), None)
        if self._window and not self._closed:
            msg(self._window, "close")
        self._closed = True
        for image in self._images:
            msg(image, "release")
        self._images.clear()
        self._buffers.clear()

    def withdraw(self):
        super().withdraw()
        msg(self._window, "orderOut:", None, argtypes=(ctypes.c_void_p,))

    def deiconify(self):
        super().deiconify()
        self._order_in()

    def lift(self, *_args):
        self._order_in()

    def _order_in(self):
        if QUIET:
            msg(self._window, "orderBack:", None, argtypes=(ctypes.c_void_p,))
        else:
            msg(self._window, "makeKeyAndOrderFront:", None,
                argtypes=(ctypes.c_void_p,))

    def lower(self, *_args):
        msg(self._window, "orderBack:", None, argtypes=(ctypes.c_void_p,))

    # -- clipboard ---------------------------------------------------------

    def on_clipboard_set(self, text):
        with pool():
            board = msg(_cls("NSPasteboard"), "generalPasteboard")
            msg(board, "clearContents")
            msg(board, "setString:forType:", nsstring(text),
                nsstring("public.utf8-plain-text"),
                argtypes=(ctypes.c_void_p, ctypes.c_void_p))

    def on_clipboard_get(self):
        with pool():
            board = msg(_cls("NSPasteboard"), "generalPasteboard")
            value = msg(board, "stringForType:",
                        nsstring("public.utf8-plain-text"),
                        argtypes=(ctypes.c_void_p,))
            return from_nsstring(value) if value else ""


class CocoaToplevel(CocoaWindow):
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


class CocoaTk(CocoaWindow):
    """The root window, matching ``window.Tk``."""

    def __init__(self, *_args, **kwargs):
        super().__init__(**kwargs)
        self.tk = None  # layout's batched-measure path checks for this


# gui.Toplevel reads this off the master, so a popup opened from a real window
# is a real window and one opened from a headless root stays headless.
CocoaWindow.toplevel_class = CocoaToplevel


_problem = ""


def available():
    """True when a Cocoa window can actually be created here."""
    global _problem
    _problem = ""
    if sys.platform != "darwin":
        return False
    try:
        _load()
    except CocoaUnavailable as exc:
        _problem = str(exc)
        return False
    return True


def unavailable_reason():
    """Why available() last said no, or "" when this is simply not macOS.

    "Cocoa needs macOS" is not news to anyone running Linux, so the wrong
    platform says nothing at all and only a real failure speaks up.
    """
    return _problem
