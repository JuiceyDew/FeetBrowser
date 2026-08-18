# 🦶 FeetBrowser
*See the web from a new ankle*

A web browser written **from scratch** -- no Chromium, no WebKit, no borrowed
libraries, and no GUI toolkit. It parses its own HTML, styles it with its own
CSS, runs its own JavaScript, rasterises its own fonts, and draws its own
pixels. The JavaScript engine, the DOM bridge and the renderer's inner loops
are a native Rust extension; everything else -- networking, parsing, layout,
the scene graph, the chrome -- is Python, standard library only.

A browser that walks wherever you do: macOS, Windows, Linux, and headless.

## What it can do

**Browse.** Open tabs and drag them to reorder them; go back, forward and
reload; keep bookmarks and history; read the page source. Type a URL or a
search into the address bar. Open links in new tabs with a middle-click or a
`Ctrl`-click -- and grab the window itself by its top bar and drag it around,
like a title bar.

**Read.** Fill in forms and submit them (GET and POST), tick checkboxes,
follow links, and let the browser keep per-tab history for you.

**Watch.** Play `<video>`: Motion JPEG, uncompressed, RLE, and H.264 with I, P
and B slices -- plus animated GIFs -- and listen along in sync, with AAC-LC,
uncompressed PCM, or MP3, all decoded through feetplayer's own Fortran.
([The honest list of gaps.](docs/media.md))

**Extend.** Add extensions ("toes") from the built-in ToeHub, restyle the whole
browser with **Shoes** themes (`about:shoes`, `Ctrl+Shift+S`), and run it all
from the hamburger menu. It even tells Discord what you're reading, rich
presence and all.

Download files while you read: `Ctrl-J` shows the manager, each transfer with
its bar, its rate, and a resume where the server allows a `Range`.

A fast flick of the wheel keeps the page gliding, so quick scrolling doesn't
need a notch for every pixel.

### A handful of shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-T` | new tab | `Ctrl-W` | close tab |
| `Ctrl-L` | focus address bar | `Ctrl-R` | reload |
| `Ctrl-J` | downloads | `Ctrl-H` | history |
| `Ctrl-Tab` | next tab | `Ctrl-Shift-Tab` | previous tab |

[Everything, in one place.](docs/usage.md)

## Run it

```bash
./run.sh                              # macOS and Linux: the welcome page
./run.sh https://example.com          # straight to a page
./run.sh --screenshot https://example.com page.png   # no window needed
```

On Windows, `run.cmd` is the same script for `cmd.exe`.

There's no toolkit to install: just Python 3 and a system font. On a first run
the script does two things for you, into a local `.venv`:

1. **The Rust engine.** `feetbrowser_engine` is compiled there with maturin, so
   a first run needs the Rust toolchain -- the script installs maturin itself.
2. **feetplayer.** The media stack, our own code in a repository of its own, is
   installed from [`requirements.txt`](requirements.txt), pinned to a commit.

Prefer not to install a Rust toolchain at all? Don't: the
[Wheels](.github/workflows/wheels.yml) workflow ships prebuilt extension wheels
for macOS (one universal wheel), manylinux x86-64 and Windows x86-64, across
CPython 3.9-3.14, attached to every tagged release. Install one into your
interpreter and `run.sh` finds the engine ready and skips the build.

The window is ours too. macOS gets one through AppKit, Windows one through
user32/gdi32, and Linux gets a **native Wayland window** -- the protocol spoken
straight over a unix socket, no library in between -- with X11 (including
XWayland) as the fallback. No display at all? `--screenshot` still renders the
whole browser, chrome and all, to a PNG. `FEETBROWSER_DISPLAY` picks the
backend -- or `none`, for headless -- and
[the full list is one page away](docs/environment.md).

## STRIDE: how code is judged

Every change should be a **stride forward**: one deliberate step, then iterate.
Code in this repo is evaluated on six principles:

- **S**imple: KISS + DRY -- no repetition, no cognitive load
- **T**rue to spec: correctness against the web specs (HTTP/1.1, HTML tree-building, CSS cascade)
- **R**eadable: Clean Code + SOLID -- modular, explicit, maintainable
- **I**terative: Agile + DevOps -- small steps, continuous feedback, shared ownership
- **D**on't Repeat Yourself: no duplication
- **E**fficient: Unix + minimalism -- one thing well, fewer resources

## Learn more

*How it works:* [architecture](docs/architecture.md) · [the rendering engine](docs/rendering.md) · [video and media](docs/media.md)
*How to use it:* [usage & shortcuts](docs/usage.md) · [environment variables](docs/environment.md)
*How to extend it:* [toes & ToeHub](docs/toes.md)
*The honest parts:* [what it does and doesn't do](docs/limitations.md) · [running the tests](docs/testing.md) · [speed goals](docs/speed-goals.md)