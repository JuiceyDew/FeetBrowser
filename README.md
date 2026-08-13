# 🦶 FeetBrowser

A **functional web browser written from scratch** in pure Python. It does not
wrap Chromium, WebKit, Gecko, or any HTTP library — it implements its own:

- **Networking** — raw TCP sockets speaking HTTP/1.1, TLS for `https`,
  redirect following, `gzip`/`deflate` decoding, chunked transfer decoding,
  plus `data:`, `file:` and `view-source:` schemes and a small response cache.
- **HTML parser** — a tokenizer + tree builder producing a real DOM
  (entities, comments, void elements, raw-text `<script>`/`<style>`, and
  implicit `<html>`/`<head>`/`<body>` + `<li>`/`<p>`/`<tr>` insertion).
- **CSS engine** — a parser for tag / class / id / descendant / grouped
  selectors, the cascade with specificity, inheritance, inline `style=""`,
  `@media` unwrapping, and a default user-agent stylesheet (`ua.css`).
- **Layout engine** — a block-and-inline flow layout with line breaking and
  word wrapping, font size / weight / style, colors, backgrounds, list
  bullets, and `<hr>`, producing a display list of paint commands.
- **Browser UI** — a hand-drawn chrome on a Tk canvas: tabs, an address bar
  with search fallback, back / forward / reload, hover + clickable links,
  scrolling, a scrollbar, and a status bar.
- **Extensions (Toes)** — a from-scratch hooking system. A toe is a plain
  Python module dropped into `toes/` that can rewrite pages, inject CSS,
  take over navigations (custom schemes like `toe://`), draw on the canvas,
  add chrome bands (toolbars), and open popup windows. See `toes/README.md`.

Tk is used **only as the pixel surface** (a canvas to draw text and rectangles
on) and for font metrics — the browser engine itself is all in this repo.

## Running

```bash
./run.sh                 # opens the welcome page
./run.sh https://example.com
./run.sh view-source:https://example.com
```

`run.sh` uses your system Python if it has Tkinter; on NixOS it fetches one
on the fly via `nix-shell`. On other distros install Tk first
(`python3-tk` on Debian/Ubuntu, `python3-tkinter` on Fedora, `tk` on Arch)
and then `python3 -m feetbrowser <url>`.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Alt-←` / `Alt-→` | back / forward | `↑` `↓` / wheel | scroll |

Type a URL in the address bar and press Enter, or type words to search
(DuckDuckGo HTML).

## Layout of the code

```
feetbrowser/
  net.py         URL parsing + HTTP/HTTPS/data/file transport
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors, specificity, cascade
  layout.py      block/inline layout -> display list, painting
  browser.py     Tk window, chrome, tabs, history, event loop
  toes.py        extension hooking (Toes): discovery + dispatch
  ua.css         default user-agent stylesheet
toes/
  word-count/    sample toe: page word count (on_load + extra_css)
  toe-scheme/    sample toe: the toe:// scheme (handle)
  sock-detective/ sample toe: foot-themed devtools (sniff mode + toe://sock reports)
  toe-bar/       sample toe: a 2003-style toolbar (chrome bands + popups)
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, internal pages)
  test_nav.py    click-to-navigate, history, view-source
  test_toes.py   toe engine + sample toe tests
  test_sock.py   sock-detective toe tests
  test_toebar.py toe-bar + chrome-band/popup framework tests
  smoke.py       end-to-end pipeline on real pages
```

## What it does and doesn't do

**Does:** fetch and render real websites over HTTPS, apply their CSS
(text styling, colors, backgrounds, layout), follow links, keep per-tab
history, show page source, and run extensions ("toes") that can rewrite
pages, inject CSS, and register custom schemes.

**Doesn't (yet):** run JavaScript, decode images (drawn as placeholders),
float/flex/grid/table layout, or form submission wiring. These are the
natural next milestones — the architecture has clean seams for each.

## Tests

```bash
./test.sh          # pyflakes + unit + navigation + live smoke tests
```

`test_units.py` and `test_nav.py` are deterministic; `smoke.py` fetches a few
real sites, so it needs network access.
