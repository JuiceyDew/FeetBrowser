# 🦶 Toes

Every foot deserves toes. Toes is FeetBrowser's extension hooking: a toe is a
plain Python module, living in its own folder under `toes/`, that gets a few
well-placed hooks into the load pipeline. No new dependencies, no sandboxing
theater — a toe is trusted local code, exactly like the browser's own modules,
so it can do anything the browser can do.

## Anatomy of a toe

```
toes/name-of-your-toe/
    toe.json     # { "name", "version", "description", "entry" }
    toe.py       # the code
```

`toe.json` is read at startup; `toe.py` is imported and its `activate(ctx)`
is called once with a `Context`. Register whatever hooks you care about; the
rest are optional.

```python
# toes/hello/toe.py
def activate(ctx):
    ctx.on("on_load", on_load)

def on_load(url, body):
    return body.replace("</body>", "<p>hello from a toe</p></body>", 1)
```

A toe that raises while loading is skipped with a warning to stderr — one bad
toe never bricks the browser. Toes run in folder-name order; on conflicts the
last one wins.

## Hooks

| Hook | Called when | Return |
|------|-------------|--------|
| `on_load(url, body)` | before the HTML is parsed | rewritten `body`, or `None` |
| `extra_css(url)` | gathering stylesheets (right after the UA sheet) | CSS text, or `None` |
| `handle(url, tab)` | a navigation starts (before fetching) | `(headers, body, content_type)` to take over, or `None` to fetch normally |
| `on_draw(canvas, offset)` | each repaint, after the page | — |
| `buttons()` | building the toolbar | list of `toes.ButtonDef(id, glyph)` |
| `on_click(button_id)` | a toe toolbar button is clicked | — |
| `on_keypress(event)` | a key is pressed (no address-bar focus) | `True` to swallow the key |
| `on_motion(x, y)` | the mouse moved over the page (document coords) | — |
| `on_new_tab()` | a new tab is created | — |

Helpers on the context: `ctx.current_tab()`, `ctx.tabs()`, `ctx.set_status(msg)`,
`ctx.open(url)`.

## Writing pages from a toe

The `handle` hook is how custom schemes like `toe://` work: return a
`(headers, body, content_type)` tuple and the page flows through the normal
pipeline, so links are clickable, `view-source:` works, and history behaves.
See `toes/toe-scheme/toe.py` for a complete example.

## Bundled toes

- **word-count** — injects a "Toes counted N words on this page" status line
  (demonstrates `on_load` + `extra_css`).
- **toe-scheme** — registers the `toe://` scheme: `toe://hello` says hi,
  `toe://gallery` lists every installed toe (demonstrates `handle`).
- **sock-detective** — devtools, foot themed and hard-boiled. The "sock"
  toolbar button toggles sniff mode: hover the page and a red box names the
  element under your cursor. `toe://sock` and friends (`/dom`, `/layout`,
  `/style`, `/cases`, `/errors`, `/help`) are full case files on the page's
  guts, all rendered through the normal pipeline (demonstrates `on_draw`,
  `on_motion`, `on_click`, `on_keypress`, `handle`).
