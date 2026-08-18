# Environment variables

The browser reads a handful of its own variables and a few standard ones.
Almost none of them has to be set for the browser to work: every one has a
default that is the right answer on a normal machine. They exist for the
moments the default is not the right answer -- picking a window backend, or
running the tests on a machine that is not yours.

Set them the usual way, and they are read once at startup:

```bash
FEETBROWSER_DISPLAY=wayland ./run.sh
FEETBROWSER_SCALE=2 ./run.sh https://example.com
```

On Windows, the same with `run.cmd`, or `set FEETBROWSER_DISPLAY=wayland`
first in a command prompt.

## FeetBrowser's own

| Variable | What it does |
| --- | --- |
| `FEETBROWSER_DISPLAY` | which native window backend opens the window: `cocoa`, `win32`, `wayland`, `x11`, or `none`. Unset, the first that works is tried, in the order Cocoa, Win32, Wayland, X11. See below. |
| `FEETBROWSER_DOWNLOAD_DIR` | where saved files land. Unset, the platform's own download directory is used. A leading `~` is expanded, and the directory is created if it is missing. |
| `FEETBROWSER_DISCORD` | set `0` to switch off the Discord Rich Presence that says what you are reading. Unset (or anything but `0`), presence is on. |
| `FEETBROWSER_DISCORD_CLIENT_ID` | the Discord application the presence reports to. Unset, the browser's own application is used. Point it at yours if you run a fork. |
| `FEETBROWSER_SCALE` | device pixels per CSS pixel. Unset, the platform's own reported scale is used (1.0 with no display). A value outside 0.25-8.0 is ignored rather than obeyed, so a typo cannot become a framebuffer the size of a city block. |
| `FEETBROWSER_QUIET` | tames a window's manners -- no centring, no raising, no stealing the keyboard. It is for test runs that open dozens of real windows; set it for a suite, leave it unset for the browser. |

The tests read one more: `FEETBROWSER_TEST_TIMEOUT` overrides the seconds a
suite is allowed before the watchdog dumps every thread and kills the run
(`0` disables the deadline, for stepping through a suite in a debugger).

## `FEETBROWSER_DISPLAY`, in detail

The browser draws its own pixels, but it still needs somewhere to put a
window. Each backend speaks a platform's own protocol by ctypes, with no
bindings package in between -- AppKit on macOS, user32/gdi32 on Windows, a
native Wayland client on Linux (the protocol spoken straight over a unix
socket), and Xlib for X11, including XWayland, as the fallback.

With `FEETBROWSER_DISPLAY` unset, the first backend that can run is used:
Cocoa, then Win32, then Wayland, then X11. Name one and it is the only one
tried -- and a backend that cannot run here is an error you are told about,
not a silent fallback to headless (a silent fallback is how you end up with
an empty screenshot and no idea why).

| value | effect |
| --- | --- |
| unset or empty | try Cocoa, Win32, Wayland, X11, and take the first that works |
| `cocoa`, `macos`, `darwin` | demand the macOS window |
| `win32`, `windows` | demand the Windows window |
| `wayland`, `wlroots`, `sway` | demand the native Wayland window |
| `x11`, `linux`, `xorg` | demand the X11 window |
| `none` | stay headless even where a window is possible |

The value is read once, stripped, and lowercased, so `X11` and ` x11 ` are the
same as `x11`. An unrecognised value is the exception to "name a backend and
it must run": it matches nothing, so every backend is skipped and the browser
runs headless without a word. A `none` that leaves you wondering why there is
no window is the price of having a `none` at all.

## The platform's own, that the browser consults

| Variable | Where it matters |
| --- | --- |
| `DISPLAY` | the X11 backend needs it to find a server. Unset, that backend reports there is nothing to draw on -- which is one of the ways the browser ends up headless under the default. |
| `WAYLAND_DISPLAY`, `WAYLAND_SOCKET`, `XDG_RUNTIME_DIR` | the Wayland backend finds the compositor's socket through these. `WAYLAND_SOCKET` is the rarest and wins; otherwise the socket is `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`. |
| `XDG_DOWNLOAD_DIR`, `XDG_CONFIG_HOME` | the Linux default download directory (and where the download-dir setting is read from) when `FEETBROWSER_DOWNLOAD_DIR` is unset. |
| `XAUTHORITY` | forwarded to the X11 backend's subprocess calls, so it can authenticate. |

## What is *not* an environment variable

The browser's settings -- scroll, momentum, the download directory, and the
rest -- live in `~/.feetbrowser_settings.json`, read on startup and written
as you change them in the hamburger menu. There is no environment variable for
any of them; the environment only steers *this run*, and settings are meant
to outlive it.