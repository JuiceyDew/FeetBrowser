# Extensions (Toes & ToeHub)

FeetBrowser ships with **no toes by default**: the framework is built in, the
toes are opt-in. To install toes, open the browser and visit **`toe://hub`**
(or `toehub://`). The ToeHub pulls a catalog from
[xplosivex/feetbrowser-toes](https://github.com/xplosivex/feetbrowser-toes)
and lets you **install, uninstall, enable, and disable** toes from inside the
browser, no restart needed. A toe is a plain Python module that can rewrite
pages, inject CSS, take over navigations (custom schemes like `toe://`), draw
on the canvas, add chrome bands (toolbars), and open popup windows.

## Anatomy of a toe

A toe is a plain Python module in its own folder:

```
toes/name-of-your-toe/
    toe.json     # { "name", "version", "description", "entry" }
    toe.py       # the code, exposing activate(ctx)
```

`toe.json` is read at discovery; `toe.py` is imported and its `activate(ctx)`
is called once with a `Context`. Register whatever hooks you care about; the
rest are optional. A toe that raises while loading is skipped with a warning;
one bad toe never bricks the browser.

## Hooks

| Hook | Called when | Return |
|------|-------------|--------|
| `on_load(url, body)` | before the HTML is parsed | rewritten `body`, or `None` |
| `extra_css(url)` | gathering stylesheets (after the UA sheet) | CSS text, or `None` |
| `handle(url, tab)` | a navigation starts (before fetching) | `(headers, body, content_type)` to take over, or `None` |
| `on_draw(canvas, offset)` | each repaint, after the page | None |
| `buttons()` | building the toolbar | list of `toes.ButtonDef(id, glyph)` |
| `on_click(button_id)` | a toe toolbar button is clicked | None |
| `on_keypress(event)` | a key is pressed (no address-bar focus) | `True` to swallow |
| `on_motion(x, y)` | the mouse moved over the page | None |
| `on_new_tab()` | a new tab is created | None |
| `chrome_bands()` | declare chrome bands above the tabs | `[(id, height), ...]` |
| `on_chrome_draw(canvas, bands)` | paint the toe's chrome bands | None |
| `on_chrome_click(x, y, bands)` | a click in the band region | `True` to consume |

Helpers on the context: `ctx.current_tab()`, `ctx.tabs()`, `ctx.set_status(msg)`,
`ctx.open(url)`, `ctx.popup(url, width, height)`, and
`ctx.settings` / `ctx.save_settings()` (per-toe persisted settings).

## Drawing: what the canvas is now

The object `on_draw` and `on_chrome_draw` hand you is
`feetbrowser.canvas.Canvas`. FeetBrowser draws every pixel itself, and there
is no toolkit underneath any of this, but the method names and keyword
arguments are the ones the catalog toes were written against and they are kept
as a compatibility surface, so those toes run unchanged. There is no shim in
between; the compatible object *is* the canvas.

**Provided**, with the spellings and the semantics you already know:

- `create_rectangle`, `create_line`, `create_text`, `create_image`,
  `create_oval`, `create_arc`, `create_polygon`, taking `fill`, `outline`,
  `width`, `anchor`, `font`, `text`, `image`, `stipple` and `tags`. A
  rectangle still gets a black 1px border unless you say `width=0` or
  `outline=""`, and items still stack in creation order.
- `find_all`, `find_withtag`, `delete`, `itemconfigure`, `coords`, `bbox`,
  `addtag_withtag`, plus `winfo_width` / `winfo_height` and
  `config(cursor=…, bg=…)`.
- Fonts from `feetbrowser.layout.get_font(size, weight, slant, family)` or
  `canvas.Font(...)`: `measure`, `metrics`, `actual`, `cget`. Characters the
  requested face lacks are looked up in a fallback chain, so the arrows,
  stars and houses toolbars label their buttons with measure and paint
  properly rather than coming out as blank width.
- `canvas.PhotoImage`: `width()`, `height()`, `subsample()`, `zoom()`.
- `canvas.CanvasError`, raised by a bad colour name. This was called
  `gui.TclError` until Tk was removed; a toe that catches it by the old name
  needs a one-line change.
- On `ctx.browser.window`: `after`, `after_idle`, `after_cancel`, `bind` /
  `unbind` with the usual `<Button-1>`-style sequence names, `title`,
  `geometry`, `clipboard_get` /
  `clipboard_clear` / `clipboard_append`, `update_idletasks`, `destroy`.
  Events carry `.x`, `.y`, `.char`, `.keysym`, `.delta`, `.num`, `.state`.

**Not provided.** There is no widget toolkit behind any of this, so none of
these exist and reaching for them raises `AttributeError` immediately rather
than drawing nothing:

- `ttk` anything, and the plain widgets (`Frame`, `Label`, `Button`,
  `Entry`, `Text`, `Menu`, `Scrollbar`), along with `messagebox`,
  `filedialog` and `simpledialog`.
- `StringVar` / `IntVar` / `BooleanVar` and the rest of the variable classes.
  Keep state on the context object; `ctx.settings` persists it.
- `canvas.create_window()`, which exists to embed a widget.
- `canvas.bind()` and `canvas.tag_bind()`. Input reaches a toe through
  `on_click`, `on_chrome_click`, `on_keypress` and `on_motion`, which is the
  same information with the hit-testing already done.
- `tag_raise` / `tag_lower` / `itemcget` / `scan_mark` / `xview` /
  `postscript`, and `PhotoImage.put()` / `.get()` / `.copy()`.

Import these from `feetbrowser.canvas` and `feetbrowser.window`, or take
fonts from `layout.get_font`. There is nothing else to import: a widget built
from some other toolkit has no window to appear in.

## Managing toes

**From the browser**: `toe://hub`, or **Manage Toes** in the hamburger
settings menu (right of the address bar), which opens the hub in a new tab:
- install / uninstall any toe from the catalog
- enable / disable installed toes (disabled toes stay installed but no hooks fire)

**From the CLI:**

```bash
python3 -m feetbrowser --toes                 # list installed toes + status
python3 -m feetbrowser --toe-search <term>    # search the catalog
python3 -m feetbrowser --toe-install <name>   # install a toe
python3 -m feetbrowser --toe-uninstall <name> # uninstall a toe
python3 -m feetbrowser --toe-enable <name>    # enable a disabled toe
python3 -m feetbrowser --toe-disable <name>   # disable an installed toe
python3 -m feetbrowser --new-toe <name>       # scaffold a new toe
python3 -m feetbrowser --toe-docs             # generate a markdown reference
```

`toe://gallery` shows installed toes; `toe://hello` is the "no toes yet"
placeholder. Install state (enabled/disabled) lives in `toes/config.json`,
which is gitignored. Installed toes themselves live under `toes/` and are
never committed.

## The catalog

The ToeHub reads `index.json` from the configured toe repository (default:
`https://raw.githubusercontent.com/xplosivex/feetbrowser-toes/main/index.json`).
Add your own toes by forking that repo and following its README.
