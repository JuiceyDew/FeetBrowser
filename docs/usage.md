# Usage

## Running

```bash
./run.sh                 # opens the welcome page
./run.sh https://example.com
./run.sh view-source:https://example.com
```

On Windows, `run.cmd` is the same script for `cmd.exe`:

```bat
run.cmd
run.cmd https://example.com
```

Either script builds the Rust JS engine (`feetbrowser_engine`) into a local
`.venv` with maturin if it isn't importable yet, then runs the browser from
that venv (so a first run needs the Rust toolchain; maturin is installed into
the venv automatically). Once the extension is built and installed for your
interpreter, `python3 -m feetbrowser <url>` works directly.

There is nothing else to install. The renderer is ours (see [the rendering
engine](rendering.md)), so no GUI toolkit is needed: only Python 3, a Rust
toolchain for that one extension, and at least one system font. The window is
ours as well: AppKit on macOS, Wayland or X11 on Linux, and user32/gdi32 on
Windows, all reached by ctypes with no bindings package in between. On a
Wayland desktop the native Wayland window is preferred and X11 (including
XWayland) is the fallback, so every existing X11 user is unaffected. The
Wayland window needs no library at all: the protocol is a wire format the
browser speaks over a plain unix socket, exactly the way `net.py` speaks
HTTP/1.1.

`FEETBROWSER_DISPLAY` decides which one, and normally wants leaving alone; it
and the rest of the environment are described under [environment
variables](#environment-variables) below.

With no display at all (no `$DISPLAY`, no `$WAYLAND_DISPLAY`, no compositor
answering, or a platform with no backend), the browser says which of those it
was and carries on headless, where `--screenshot` still works.

On Windows, "a Rust toolchain" is two installs rather than one, and the
second is the one that catches people out. rustup selects the MSVC toolchain
by default (`stable-x86_64-pc-windows-msvc`), but selecting it is not the
same as having it: `rustc` compiles the code and then hands it to a C++
linker, and Windows does not ship one. Without it the first `run.cmd` gets a
long way (venv made, maturin installed, crates downloaded), and then stops
with

```
error: linker `link.exe` not found
```

Install **Build Tools for Visual Studio** from
<https://visualstudio.microsoft.com/downloads/> and tick the **Desktop
development with C++** workload in its installer. Ticking that workload is
the step that gets missed, and the Build Tools installed without it give the
identical error, so it is worth checking rather than assuming. A full Visual
Studio installation with the same workload does just as well. Open a new
command prompt afterwards so the linker is on `PATH`, then run `run.cmd`
again. `run.cmd` and `test.cmd` both say all of this themselves if the build
fails, so nobody has to find this page first.

There is a second route, and it is worth knowing what it does and does not
save you. rustup's GNU toolchain links with MinGW rather than MSVC:

```bat
rustup toolchain install stable-x86_64-pc-windows-gnu
rustup default stable-x86_64-pc-windows-gnu
```

That does build a working engine (the extension imports and the whole suite
passes on it), but it is a swap, not a saving, because it has a prerequisite
of its own that rustup does not install. Without MinGW-w64's binutils on
`PATH` the build gets *past* the linker, spends a while compiling, and then
stops at

```
error: error calling dlltool 'dlltool.exe': program not found
error: could not compile `pyo3-ffi` (lib) due to 1 previous error
```

Install MinGW-w64 ([MSYS2](https://www.msys2.org) is one way) and put its
`bin` directory on `PATH`. The `dlltool.exe` that rustup installs next to the
toolchain is not a substitute: it is there, but it shells out to an assembler
that ships in the same MinGW package, and without that it fails with
`dlltool.exe: CreateProcess` instead. Of the two routes, Visual Studio is the
one CI builds against, so it is the better-trodden one; the GNU route was
verified by experiment on a Windows runner rather than by a job in this
repository.

If `python` isn't on your `PATH` (a fresh install from the Microsoft Store
often leaves only the launcher), use `py -3` in place of `python` in the two
commands `run.cmd` runs.

To render a page without opening a window:

```bash
./run.sh --screenshot https://example.com page.png
```

This runs the whole browser (chrome, tabs, toolbar, page, scrollbar), waits
for images to load, and writes a PNG.

To ask a copy of the browser whether it can play video:

```bash
./run.sh --check-video
./run.sh --check-video tests/fixtures/h264/mb1.264 tests/fixtures/h264/mb1.i420.z
```

The first prints whether the H.264 decoder loaded and, if it did not, why.
The second decodes a frame and compares it with the picture a reference
decoder produced. It is mostly there for the packaging, which runs it inside
the `.app`, the AppImage and the Windows bundle -- that is the one place the
answer cannot be taken on trust, because a build that ships no decoder looks
perfect until somebody opens a video.

## Environment variables

The browser reads two variables of its own, and neither of them has to be set
for it to work: every one has a default that is the right answer on a normal
machine. One of them exists because the browser has two of some things (two
window backends), and a choice that is only ever made at build time cannot be
tested both ways; the other names a directory whose right answer is different
on every platform. A third, the standard `DISPLAY`, is not ours but decides
whether the X11 window can open, so it is described here too.

The first is read as text, stripped of surrounding whitespace and lowercased,
so `X11` and ` x11 ` are the same as `x11`. It is read once and the choice is
then fixed for the life of the process; changing it from inside a running
browser does nothing. The second is a path, so it is taken as written apart
from a leading `~`.

There is nothing here that picks a renderer. There is one: our own font
engine, rasteriser and event loop, and every window backend and the
headless root draws through it.

### `FEETBROWSER_DISPLAY`

Which native window backend opens a window, from `feetbrowser/gui.py`. It
picks where the pixels are put, not what draws them: the renderer is the same
either way, and the same again when there is no window at all.

| value | effect |
| --- | --- |
| unset or empty | try Cocoa, then Win32, then Wayland, then X11, and take the first that works (the default) |
| `cocoa`, `macos`, `darwin` | demand the macOS window; fail loudly if there is none |
| `win32`, `windows` | demand the Windows window; fail loudly if there is none |
| `wayland`, `wlroots`, `sway` | demand the native Wayland window; fail loudly if there is none |
| `x11`, `linux`, `xorg` | demand the X11 window; fail loudly if there is none |
| `none` | stay headless even where a window is possible |

The order of the first row costs nothing to get right and would be confusing
to get wrong: no machine offers Win32 alongside either of the others, so its
position only matters on paper. Cocoa is ahead of X11 for a real reason,
which is that macOS with XQuartz installed has both and the Mac window is the
one you meant. On Linux, Wayland comes before X11 so a session that offers
both -- which is every Wayland desktop, because of XWayland -- gets the native
compositor, and X11 is still there second for the X-only and XWayland-only
machines. (The ``linux`` spelling still means X11: it is the fallback, not a
synonym for "Wayland".)

Naming a backend that cannot run here is an error rather than a quiet
fallback, and it is reported as a sentence rather than a traceback. Silently
handing back a headless root is how you end up with an empty screenshot and
no idea why.

An unrecognised value is the exception to that, and not a helpful one: it
matches no backend, so every backend is skipped and the browser runs headless
without complaining. `FEETBROWSER_DISPLAY=mir` therefore opens no window
and says nothing about it.

### `FEETBROWSER_DOWNLOAD_DIR`

Where saved files land, from `feetbrowser/downloads.py`. Unset, the directory
is the platform's own: `XDG_DOWNLOAD_DIR` (from the environment, or from
`~/.config/user-dirs.dirs`) on Linux, the shell's Downloads known folder on
Windows, and `~/Downloads` on macOS and wherever those two have nothing to
say. Set, it is that path, with a leading `~` expanded.

Either way the directory is created if it is missing, when the first download
starts rather than at import. A name that cannot be created (a file already
sitting there, a volume that is not writable) is reported as a failed
download, not raised.

Nothing a server sends can put a file outside this directory. See
[downloads](#downloads).

### `DISPLAY`

Not ours, but read: the X11 backend needs the standard X11 variable to find a
server. With it unset the backend reports that there is no server to draw on,
which is the reason `FEETBROWSER_DISPLAY=x11` fails on a machine with no X
session. Under the default `FEETBROWSER_DISPLAY` this is simply one of the
ways the browser ends up headless.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Ctrl-D` | toggle bookmark | `about:bookmarks` | open bookmarks page |
| `Ctrl-H` | open `about:history` | `Ctrl-Tab` / `Ctrl-Shift-Tab` | next / previous tab |
| `PgUp` / `PgDn` / `Home` / `End` | page scroll controls | `Alt-←` / `Alt-→` | back / forward |
| `↑` / `↓` / wheel | scroll | `Esc` | blur address / input |
| middle / `Ctrl`-click | open link in new tab | `Ctrl-PgUp/Dn` | cycle tabs |
| `Ctrl-J` | show / hide downloads | | |

The scrollbar down the right-hand edge works with the mouse as well: drag the
thumb and the page follows it, keeping whatever part of the thumb you grabbed
under the pointer, and press the empty track above or below the thumb to jump
there and carry straight on dragging. Both stop at the same top and bottom of
the document the wheel does.

Type a URL in the address bar and press Enter, or type words to search
(DuckDuckGo HTML). Bare hosts without a scheme (`example.com:8080`,
`localhost:8000`) are assumed to be `https://`.

## Tabs and the toolbar

New tabs open with **`Ctrl-T`**, with the **`+`** button at the right of
the tab strip, by middle-clicking empty tab-bar space, or by middle- or
`Ctrl`-clicking a link. The **`×`** on a tab closes it (`Ctrl-W` closes the
active one), `Ctrl-Tab` / `Ctrl-Shift-Tab` walk between tabs, and a tab can
be closed from its close box regardless of which tab is active.

The toolbar row under the tabs is, left to right: back `‹` and forward `›`
(also `Alt-←` / `Alt-→`), reload `↻`, home `⌂`, any toe toolbar buttons,
the bookmark star (`Ctrl-D`), the address bar, and the hamburger settings
button. The settings menu holds **Bookmarks**, **History** and **Manage
Shoes**, which open the `about:` pages `about:bookmarks`, `about:history`
and `about:shoes`, and **Manage Toes**, which opens `toe://hub`; every item
opens its page in a new tab, leaving the page you were reading alone.

## Forms

Basic form support is wired up: `input[type=text/password]` fields are
focusable and typeable, checkboxes toggle, and submitting a form (clicking a
submit button or pressing Enter in a field) sends `GET` or `POST` to the form
`action`, which is resolved against the document's `<base href>` when one is
present.

## Downloads

A response that is a file rather than a page is saved instead of rendered.
Three things start a download:

* a `Content-Disposition: attachment` header, whatever the content type;
* a content type this browser cannot put on screen (anything that is not
  HTML, plain text, an image, CSS, JavaScript, JSON or XML;
* **Download Link** or **Download Image** from the right-click menu, which
  saves what a click would otherwise have opened.

`Ctrl-J`, or **Downloads** in the right-click menu, shows the panel. Each
transfer has a bar, its size and rate, and a `×` to stop it with while it is
running; `Clear finished` takes the ended ones off the list. Up to four run at
once and none of them blocks the page you are reading.

The bar tracks `Content-Length` when the server sends one. A chunked response
has no total and no honest percentage, so that bar animates instead of
filling, and the status line reads `unknown size` with the byte count and rate
next to it. An ETA is shown only where there is a total to divide by.

Bytes go to `name.part` and are moved onto `name` only once the last one has
arrived, so an interrupted download never leaves something that looks
complete. A second file of the same name becomes `file (1).txt` rather than
overwriting the first. A transfer that dies against a server supporting
`Range` is resumed from where it stopped rather than started again.

The filename is the server's suggestion (`Content-Disposition`, else the last
segment of the URL), reduced to one safe component before it is used:
percent-escapes are decoded first, directories are dropped, and NULs, control
characters, `/`, `\`, and the characters Windows reserves are removed. `.` and
`..` are refused, and a name that is a DOS device (`CON`, `NUL`, `LPT1`, and
the rest) is prefixed. Nothing a server sends can write outside the download
directory.

A dropped connection, a full disk, a directory that cannot be written to and a
404 all end as a failed download that says why, in the panel. None of them is
a traceback and none is silence.

## CLI reference

```bash
python3 -m feetbrowser --help             # full CLI reference
python3 -m feetbrowser --version          # print the version
python3 -m feetbrowser --toes                 # installed toes + status
python3 -m feetbrowser --toe-search <term>    # search the catalog
python3 -m feetbrowser --toe-install <name>   # install a toe
python3 -m feetbrowser --toe-uninstall <name> # uninstall a toe
python3 -m feetbrowser --toe-enable <name>    # enable a disabled toe
python3 -m feetbrowser --toe-disable <name>   # disable an installed toe
```
