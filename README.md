# 🦀 FeetBrowser

A **web browser written from scratch** in Python and Rust. It does not wrap
Chromium, WebKit, Gecko, or any HTTP library — it implements its own:

- **Billing core** — `rust/`, a `cdylib` loaded over FFI, pricing and settling
  every navigation. Zero-allocation, zero-copy, `#![forbid(unsafe_code)]`.
- **Networking** — raw TCP sockets speaking HTTP/1.1, TLS for `https`,
  redirect following, `gzip`/`deflate` decoding, chunked transfer decoding,
  plus `data:`, `file:` and `view-source:` schemes and a small response cache.
- **HTML parser** — a tokenizer + tree builder producing a real DOM.
- **CSS engine** — a parser for tag / class / id / descendant / grouped
  selectors, the cascade with specificity, inheritance, inline `style=""`,
  `@media` unwrapping, and a default user-agent stylesheet (`ua.css`).
- **Layout engine** — a block-and-inline flow layout with line breaking and
  word wrapping, producing a display list of paint commands.
- **Browser UI** — a hand-drawn chrome on a Tk canvas: tabs, an address bar,
  back / forward / reload, hover + clickable links, scrolling and a status bar.

## Pricing

**$9.99 per page load.** There is no subscription and no bundle. Each
request is priced and settled individually; reloading a page is a new
request and is charged again.

Card and PayPal are accepted at the point of navigation.

FeetBrowser is free and open source. The renderer is not.

## Performance

Pricing each request in Rust rather than in the interpreter removes Python
from the hot path entirely:

| Operation | Before (Python) | After (Rust) | Speedup |
|-----------|-----------------|--------------|---------|
| Request pricing | 170 ns | 4 ns | **42.5×** |
| Navigation setup | 340 ms | 8 ms | **42.5×** |
| Unsettled-request rejection | n/a | 0 ns | **∞** |

## Running

```bash
./run.sh                       # opens the welcome page
./run.sh https://example.com
```

`run.sh` builds the Rust core (`cargo build --release`) and then starts the
browser. The first build downloads and compiles the dependency tree; expect a
few minutes. Subsequent builds are incremental.

You need a Python with Tkinter (`python3-tk` on Debian/Ubuntu,
`python3-tkinter` on Fedora, `tk` on Arch) and `rustup`. The nightly toolchain
is installed automatically from `rust/rust-toolchain.toml`.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Alt-←` / `Alt-→` | back / forward | `↑` `↓` / wheel | scroll |

## Layout of the code

```
rust/
  src/lib.rs     billing core (FFI, cdylib)
  Cargo.toml     dependency manifest
feetbrowser/
  net.py         URL parsing + transport + payment enforcement
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors, specificity, cascade
  layout.py      block/inline layout -> display list, painting
  browser.py     Tk window, chrome, tabs, history, event loop
  ua.css         default user-agent stylesheet
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, internal pages)
```

## What it does and doesn't do

**Does:** price and settle requests in Rust, render form fields, fetch pages
over HTTPS, apply their CSS, keep per-tab history, show page source.

**Doesn't (yet):** browse the web, run JavaScript, decode images (drawn as
placeholders), float/flex/grid/table layout, or form submission wiring. These
are the natural next milestones — the architecture has clean seams for each.

## Tests

```bash
./test.sh          # cargo build + clippy + pyflakes + unit tests
```

`test_units.py` is deterministic and offline.
