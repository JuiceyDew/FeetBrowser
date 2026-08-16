# 🦶 FeetBrowser
*See the web from a new ankle*

A web browser written **from scratch**. No Chromium, no WebKit, no borrowed
libraries: it does its own networking, HTML parsing, CSS, layout, JavaScript,
fonts, and pixels. No GUI toolkit either: the TrueType parser, the antialiased
rasteriser and the image decoders are all in this repo. The JavaScript engine,
the DOM bridge and the renderer's inner loops are compiled to a native Rust
extension; everything else (networking, parsing, layout, the scene graph,
chrome) is Python, standard library only.

## STRIDE: how code is judged

Every change should be a **stride forward**: one deliberate step, then iterate.
Code in this repo is evaluated on six principles:

- **S**imple: KISS + DRY: no repetition, no cognitive load
- **T**rue to spec: correctness against the web specs (HTTP/1.1, HTML tree-building, CSS cascade)
- **R**eadable: Clean Code + SOLID: modular, explicit, maintainable
- **I**terative: Agile + DevOps: small steps, continuous feedback, shared ownership
- **D**on't Repeat Yourself: no duplication
- **E**fficient: Unix + minimalism: one thing well, fewer resources

## Run it

```bash
./run.sh                 # macOS, Linux: opens the welcome page
./run.sh https://example.com
```

```bat
run.cmd                  :: Windows: the same script for cmd.exe
run.cmd https://example.com
```

No GUI toolkit to install: just Python 3 and a system font. The one thing
that does get built is the engine: `run.sh` and `run.cmd` compile the Rust
extension (`feetbrowser_engine`) into a local `.venv` when it isn't
importable, so a first run needs the Rust toolchain (the script installs
`maturin` into the venv for you). On Windows it needs a C++ linker as well,
which rustup does not bring with it; [usage.md](docs/usage.md) has the one
download and the one checkbox. Once the extension is built and installed
for the interpreter you're invoking, `python3 -m feetbrowser <url>` works
directly.

If you would rather not install a Rust toolchain at all, don't: the
[Wheels](.github/workflows/wheels.yml) workflow builds the extension for
macOS (Intel and Apple Silicon in one universal wheel), manylinux x86-64 and
Windows x86-64, on CPython 3.9 through 3.14. They are attached to every
tagged release, and every run of that workflow keeps the same files as
downloadable artifacts. Install one into the interpreter you run the browser
with (`pip install feetbrowser_engine-*.whl`), and `run.sh` finds the
engine already importable and skips the build.

The window itself is ours too. macOS gets one through AppKit, Linux one
through Xlib, and Windows one through user32/gdi32, all by ctypes, so there
is nothing to install for any of them, and X11 covers Wayland desktops
through XWayland. Anywhere else, and anywhere with no display, the browser
still renders: `--screenshot` writes the page to a PNG without opening
anything.

To render a page to a PNG without opening a window:

```bash
./run.sh --screenshot https://example.com page.png
```

## What you can do

- Open tabs, back/forward, reload, bookmarks, history, and page source
- Fill in forms, follow links, search from the address bar
- Download files, watching them arrive: `Ctrl-J` shows the manager
- Play a `<video>`, with play/pause and a scrubber: Motion JPEG,
  uncompressed, RLE, or H.264 with I, P and B slices under either entropy
  coder -- and, where the soundtrack is AAC-LC or uncompressed PCM, with
  the sound, in sync
  ([the honest list](docs/media.md#what-is-not-supported))
- Add extensions ("toes"): open **`toe://hub`** in the browser
- Restyle the browser with **Shoes** themes: open **`about:shoes`**
  (`Ctrl+Shift+S`)
- Use the hamburger settings menu (right of the address bar) for
  bookmarks, history, themes, and **Manage Toes** (`toe://hub`), each in
  a new tab
- Keyboard shortcuts: `Ctrl-T` new tab, `Ctrl-L` focus address bar,
  `Ctrl-W` close tab, and more

## Learn more

- [Usage & shortcuts](docs/usage.md)
- [Architecture: how the engine works](docs/architecture.md)
- [The rendering engine: fonts, rasteriser, pixels](docs/rendering.md)
- [Video: Motion JPEG, H.264 in Fortran, and the gaps](docs/media.md)
- [Extensions (Toes & ToeHub)](docs/toes.md)
- [What it does and doesn't do](docs/limitations.md)
- [Running the tests](docs/testing.md)
