# Architecture

FeetBrowser is a **functional web browser written from scratch**: the engine
(JS interpreter, DOM bridge, the CSS cascade, and the inner loops of the
renderer) is a native Rust extension, and the rest is pure Python. It does not wrap Chromium,
WebKit, Gecko, or any HTTP library; it implements its own:

- **Networking**: raw TCP sockets speaking HTTP/1.1, TLS for `https`,
  redirect following, `gzip`/`deflate` decoding, chunked transfer decoding,
  plus `data:`, `file:` and `view-source:` schemes, a small bounded response
  cache, and a keep-alive connection pool that reuses sockets per origin.
  When the server negotiates it over ALPN, the same sockets speak HTTP/2
  (RFC 7540): HPACK header compression (RFC 7541) and the frame types a
  client needs are implemented from scratch in this repo, and a page's
  resources share one multiplexed connection instead of each opening its
  own.
- **HTML parser**: `footnote`, a WHATWG tokenizer + tree builder of ours in
  a repository of its own, scoring 99.6% on the html5lib tree-construction
  suite. It builds a real DOM: entities, comments, void elements, raw-text
  `<script>`/`<style>`, implicit `<html>`/`<head>`/`<body>` +
  `<li>`/`<p>`/`<tr>` insertion, and the three algorithms that move nodes
  already in the tree (foster parenting, formatting reconstruction, the
  adoption agency). `rust/src/materialize.rs` hands its arena to Python.
- **CSS engine**: a parser for tag / class / id / descendant / grouped
  selectors (with pseudo-classes like `:hover` collapsed to their base
  selector), the cascade with specificity, inheritance, inline `style=""`,
  `@media` unwrapping, and a default user-agent stylesheet (`ua.css`). The
  parser is Python; the cascade is Rust, because it is the one part that runs
  once per node per candidate rule and a long article has thousands of both.
  The selector objects the parser produces are plain Python data, and the
  matcher compiles them on first use.
- **Layout engine**: a block-and-inline flow layout with line breaking and
  word wrapping, font size / weight / style, colors, backgrounds, list
  bullets, and `<hr>`, plus **CSS floats** (with text wrapping and `clear`),
  **`<table>` layout** (thead/tbody/tfoot, `colspan`/`rowspan`), a **flexbox**
  subset (`flex-direction` row/column, `gap`, `flex-grow`, `flex-basis`,
  `justify-content`, `align-items`), a **CSS grid** subset
  (`grid-template-columns` px/%/fr/auto, auto row placement,
  `grid-column`/`grid-row` spans, `gap`), and **`<img>` rendering** (PNG, GIF,
  JPEG and Netpbm, all decoded by us, fetched off the UI
  thread), plus form controls (text fields, checkboxes, submit/reset buttons,
  `<select>`), producing a display list of paint commands.
- **Rendering engine**: our own pixels, no GUI toolkit: a TrueType parser
  (`cmap`/`glyf`/`hmtx`/…, composite glyphs, real metrics), an antialiased
  scanline rasteriser owning its own framebuffer, PNG/GIF/JPEG/PNM decoders, a
  retained scene graph, and an event loop. The three layers that touch every
  pixel (the surface, the font parser and the image decoders) are in the
  same Rust extension as the JS engine; the scene graph, the event loop and
  font *discovery* stay in Python. See [docs/rendering.md](rendering.md).
- **Platform windows**: a real window on macOS (`cocoa.py`, ctypes into
  AppKit), on Linux and the BSDs (`wayland.py` for a native Wayland window,
  the wire protocol over a plain unix socket; `x11.py` for X11, ctypes into
  Xlib, which
  also covers XWayland when no Wayland compositor answers) and on Windows
  (`win32.py`, ctypes into `user32`/`gdi32`/`kernel32`), each translating
  native events into the same Tk-shaped bindings and pushing the same
  framebuffer to the screen. None of them needs a bindings package. Anywhere
  else, and anywhere with no display, the browser runs headless, which is
  also how `--screenshot` and the whole test suite run on every platform.
- **Browser UI**: a hand-drawn chrome on that canvas: tabs, an address bar
  with search fallback, back / forward / reload / home buttons, a settings
  menu off the hamburger button (the `about:` pages and the toe hub),
  hover + clickable links, middle-click / ctrl-click to open in a new tab,
  scrolling, a scrollbar, bookmark toggling, and a status bar. Repainting is
  layered: page, chrome, selection, and toe overlays are tracked by canvas
  tag, so a small change (a text selection drag, a focused address bar) only
  repaints the damaged region instead of the whole canvas.
- **Extensions (Toes)**: a from-scratch hooking system. See
  [docs/toes.md](toes.md).
- **JavaScript engine**: a from-scratch interpreter compiled to a native
  Rust extension (`feetbrowser_engine`, built with PyO3/maturin): a
  hand-written lexer + recursive-descent parser + tree-walking evaluator in
  `rust/`. It supports closures, `var`/`let`/`const`, objects, classes with
  `extends`/`super`, arrays (index growth, `length` truncation,
  `push`/`pop`/`map`/`reduce`/`join`), `if`/`while`/`for`/`for-of`/`for-in`
  with `break`/`continue`, `try`/`catch`/`throw`, arrow functions (lexical
  `this`), template literals, spread/rest, optional chaining, nullish
  coalescing, `Promise` + microtasks, `async`/`await`, timers, and operators
  with proper precedence and JS coercion rules (`NaN`/`Infinity` globals,
  `NaN` falsiness, `null + 1 === 1`, `[] + [] === ""`). Global builtins:
  `String`, `Number`, `Boolean`, `parseInt`, `parseFloat`, `Array`,
  `Object`, `Map`, `Set`, `Date`, `RegExp`, `Math`, `JSON`, `console.log`,
  `fetch`, `XMLHttpRequest`. Scripts in `<script>` tags run on page load;
  errors are captured instead of crashing the page.
- **DOM bridge**: a Rust DOM (`rust/src/dom.rs`): `getElementById`/
  `querySelector`/`querySelectorAll`, `textContent`, `innerHTML`, `style`,
  `classList`, attributes, and `addEventListener`, exposing `document`,
  elements, node lists, and the `body`/`head`/`documentElement` shortcuts.
  Scripts mutate the page and wire up click handlers, which re-cascade the
  stylesheet and re-render. The DOM objects operate on the Python node tree
  that layout renders; `feetbrowser/jsdom.py` is a thin shim that delegates
  to the Rust functions.

Beyond the engine extension, which is our own code in another language, and
feetplayer, which is our own code in another repository, no third-party
Python package is used at all, and that now includes the pixels: there is no
Tk, Qt, GTK, SDL, Cairo, FreeType or Pillow anywhere, and the only thing the
renderer asks of the operating system is a font file to parse. Nothing is
imported conditionally and nothing is shrugged off when absent, so what the
browser can draw does not depend on what else the machine happens to have
installed. SVG is the format that costs: it used to render wherever cairosvg
was present, and now renders as alt text everywhere. docs/dependencies.md
says why that is the right trade and not a regression waiting to be fixed.

## Layout of the code

```
feetbrowser/
  net.py         URL parsing + HTTP/HTTPS/data/file transport + connection pool
  downloads.py   streaming a response to disk: names, progress, cancellation
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors and specificity; the cascade is Rust
  jsengine.py    thin shim over the Rust `feetbrowser_engine` extension
  jsdom.py       thin shim over the Rust DOM bridge (dom_get/dom_set/dom_call)
  layout.py      block/inline layout -> display list, painting
  fontengine.py  font discovery and the family index; parsing is Rust
  raster.py      thin shim over the Rust surface, glyph cache and PNG output
  imagecodec.py  thin shim over the Rust PNG / GIF / JPEG / PNM decoders
  canvas.py      retained scene graph, fonts, colors, images
  window.py      windows, event bindings, after() timers, main loop
  cocoa.py       the macOS window: AppKit through ctypes, no PyObjC
  wayland.py     the Linux Wayland window: the wire protocol over a plain
                 unix socket, no library at all
  x11.py         the Linux X11 window: Xlib through ctypes, no python-xlib
  win32.py       the Windows window: user32/gdi32 through ctypes, no pywin32
  gui.py         which native window to open, or none at all
  browser.py     window, chrome, tabs, history, event loop, layered repaint
  toes.py        extension hooking (Toes): discovery, dispatch, CLI
  toehub.py      the ToeHub: catalog fetch, install/uninstall/toggle
  ua.css         default user-agent stylesheet
rust/
  lib.rs         PyO3 module wiring; the JS engine, DOM and renderer bindings
  interp.rs      evaluator, host bridge, promises, microtasks, timers
  parser.rs      recursive-descent parser + AST construction
  token.rs       lexer
  ast.rs         AST node types
  value.rs       JsValue model, scopes, coercion, JsCallback
  stdlib.rs      built-ins (Array/Object/Map/Set/Date/RegExp/Math/JSON/...)
  dom.rs         DOM bridge (document/element/style/classList/...)
  css.rs         selector matching and the cascade walk
  pybind.rs      Python-facing classes (Interpreter, JsGlobals, PyJsValue)
  raster.rs      Surface, blitters, scanline rasteriser, text, PNG output
  font.rs        TrueType tables, cmap, metrics, outlines, flattening
  image.rs       PNG / GIF / PNM decoders and nearest-neighbour resize
  pyutil.rs      shared argument conversions (bytes, coordinates, strings)
(footnote)       not in this repository. The HTML tokenizer and tree
                 builder are a crate of their own, with no dependencies and
                 no knowledge that a browser exists, pinned to a commit sha
                 in rust/Cargo.toml. materialize.rs is the only thing that
                 calls it. Its 27k lines of html5lib fixtures went with it.
(feetplayer)     not in this repository. The media stack -- the containers,
                 the three FORTRAN 77 decoders (H.264, AAC-LC, MPEG Layer
                 III) and the audio output -- is a package of its own,
                 pinned to a commit sha in requirements.txt and installed
                 beside the engine. feetbrowser/media.py imports it. The
                 browser works without a Fortran compiler; it does not work
                 without feetplayer. See docs/media.md.
toes/            user-installed toes (gitignored; empty on a fresh checkout)
tests/
  test_render.py offline tests for fonts, rasteriser, image codecs, canvas
  test_cocoa.py  the macOS window, driven by real NSEvents (macOS only)
  test_x11.py    the X11 window, driven by real X events (skips with no server)
  x11_shot.py    photographs a real X11 window with XGetImage (CI artifact)
  test_wayland.py the Wayland window, driven by real compositor events
                 (skips with no compositor; CI runs weston headless)
  test_win32.py  the Windows window, driven by real messages (Windows only)
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  download_cases.py  downloads against a local server (run from test_nav.py)
  test_shoes.py  Shoes theme manager tests
  test_toes.py   toe engine + ToeHub tests (install/uninstall/toggle)
  smoke.py       end-to-end pipeline on real pages
```

The Rust engine is built with maturin into a local venv; `run.sh` and
`test.sh` build it on first use (`maturin develop --release`), as do their
Windows counterparts `run.cmd` and `test.cmd`. There is no pure-Python
fallback for it, so a Rust toolchain is a prerequisite on every platform.
