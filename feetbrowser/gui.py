"""Where a window comes from.

This module used to pick between drawing backends as well, and re-exported
`Canvas`, `Font`, `PhotoImage` and the rest so callers need not know which
one they were talking to. There is only one now -- our own font engine,
rasteriser and event loop -- so those names come straight from `canvas` and
`window`, and nothing is left here that is not about getting a window.

What genuinely varies is where the pixels are put, and ``FEETBROWSER_DISPLAY``
is what selects it:

    cocoa     the AppKit window, via ctypes (macOS)
    wayland   the Wayland window, via ctypes (Linux)
    x11       the Xlib window, via ctypes (Linux, incl. under XWayland)
    none      stay headless even where a window is possible

Empty -- the default -- means "use whatever this platform offers". Naming a
backend that cannot run here is an error rather than a silent fallback,
because asking for one and quietly getting a headless root is how you end up
with a blank screenshot and no idea why.

`window.Tk` is always the headless root, which is what tests and
``--screenshot`` want. Opening a window on the screen is a separate, explicit
act: ``new_window()``. Nothing gets a native window by accident.
"""
import importlib
import os

from . import window

DISPLAY = os.environ.get("FEETBROWSER_DISPLAY", "").strip().lower()

# The native window backends, tried in order when nothing was asked for by
# name: (module, label, the names that select it, the root class). Each one
# answers `available()` for itself, so a backend that cannot run here simply
# says so and the next is tried. Cocoa comes first because on the one system
# that has both, XQuartz is the deliberate choice and Cocoa is the default.
# On Linux, Wayland comes before X11 so a session that offers both prefers
# the native compositor -- and X11 is still tried second, which is what keeps
# every X-only and XWayland user, and every machine with no Wayland library
# at all, exactly where they were. Win32 sits between the rest only because
# no system offers it alongside either.
NATIVE_BACKENDS = (
    ("cocoa", "Cocoa", ("cocoa", "macos", "darwin"), "CocoaTk"),
    ("win32", "Win32", ("win32", "windows"), "Win32Tk"),
    ("wayland", "Wayland", ("wayland", "wlroots", "sway"), "WaylandTk"),
    ("x11", "X11", ("x11", "linux", "xorg"), "X11Tk"),
)


def Toplevel(master=None, **kwargs):
    """A secondary window of the same kind as its master."""
    factory = getattr(master, "toplevel_class", None)
    if factory is not None:
        return factory(master, **kwargs)
    return window.Toplevel(master, **kwargs)


def platform_root():
    """The native root-window class for this platform, or None.

    Returns the class rather than an instance so callers can still decide not
    to open anything -- and so the import only happens when it is wanted.
    """
    del _PROBLEMS[:]
    if DISPLAY == "none":
        return None
    for module, label, names, root in NATIVE_BACKENDS:
        asked = DISPLAY in names
        if DISPLAY and not asked:
            continue
        try:
            backend_module = importlib.import_module("." + module, __package__)
        except ImportError as exc:
            # Asking for a backend by name and silently getting a headless
            # root is the kind of thing you discover from an empty screenshot.
            if asked:
                raise RuntimeError("no %s window available here: %s"
                                   % (label, exc)) from exc
            continue
        if backend_module.available():
            return getattr(backend_module, root)
        # Backends say why they cannot run -- "DISPLAY is not set" is a very
        # different problem from "this is not Linux", and the difference is
        # the whole of what a user needs to hear.
        reason = backend_module.unavailable_reason()
        if reason:
            _PROBLEMS.append("%s: %s" % (label, reason))
        if asked:
            raise RuntimeError("no %s window available here: %s"
                               % (label, reason or "unsupported platform"))
    return None


# Why the last platform_root() found nothing, for callers that want to say so.
_PROBLEMS = []


def display_problem():
    """A one-line explanation of why there is no window, or "".

    Only the reasons worth repeating survive: a backend that is simply for
    another operating system says nothing, because "Cocoa needs macOS" is
    noise on a Linux box that is missing its X server.
    """
    return "; ".join(_PROBLEMS)


def new_window(**kwargs):
    """A window on the screen if this platform has one, else a headless root.

    The fallback is not a failure mode: a headless root runs the whole browser
    faithfully, which is what tests and --screenshot rely on. It just has
    nowhere to put the pixels.
    """
    root = platform_root()
    if root is not None:
        return root(**kwargs)
    return window.Tk(**kwargs)


def has_display():
    """True when new_window() would open something visible."""
    return platform_root() is not None
