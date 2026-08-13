"""sock-detective toe: devtools for people who are having a hard time.

A hard-boiled private investigator for page guts. Two modes of operation:

  * Sniff mode — the "sock" button toggles it. While sniffing, hover the
    page and the detective draws a red box around whatever element is under
    your cursor and names it in the status bar. Esc hangs it up.

  * Case files — toe://sock reports, rendered through the real pipeline:
      toe://sock         THE CASE FILE — page vitals
      toe://sock/dom     FOOTPRINTS   — the DOM tree
      toe://sock/layout  THE BONES    — every layout box with geometry
      toe://sock/style   FIBERS       — computed styles per element
      toe://sock/cases   PAPER TRAIL  — every navigation it has witnessed
      toe://sock/errors  DISTRESS     — pages that went wrong
      toe://sock/help    where to look
"""

import html as _html

from feetbrowser import toes
from feetbrowser.htmlparser import Element, Text
from feetbrowser.layout import get_font

CASE_STYLE = """
  body { font-family: Courier; margin: 40px; background: #fdf6e3; color: #222; }
  h1 { color: #8b0000; letter-spacing: 2px; }
  h2 { color: #444; margin-top: 24px; }
  .tag { color: #999; }
  .k { color: #8b0000; }
  .v { color: #1a73e8; }
  .dim { color: #999; }
  .box { border: 1px solid #bbb; background: #fff; padding: 6px 10px; margin: 4px 0; }
  pre { margin: 0; font-family: Courier; }
  a { color: #8b0000; }
  .alert { background: #ffecec; border: 1px solid #c00; padding: 8px 12px; }
"""

# Report names keyed by their path on toe://sock (besides the static ones).
_STATIC_REPORTS = {
    "dom": "FOOTPRINTS",
    "layout": "THE BONES",
    "style": "FIBERS",
    "help": "WHERE TO LOOK",
}


def activate(ctx):
    ctx.on("buttons", lambda: [toes.ButtonDef("sock", "sock", "🕵")])
    ctx.on("on_click", lambda btn_id: _click(ctx, btn_id))
    ctx.on("on_motion", lambda x, y: _motion(ctx, x, y))
    ctx.on("on_keypress", lambda e: _key(ctx, e))
    ctx.on("on_draw", lambda canvas, offset: _overlay(ctx, canvas, offset))
    ctx.on("handle", lambda url, tab: _handle(ctx, url, tab))


# -- sniff mode ----------------------------------------------------------


def _click(ctx, btn_id):
    if btn_id != "sock":
        return
    ctx.sniffing = not getattr(ctx, "sniffing", False)
    ctx.set_status(
        "Sock Detective is ON THE CASE. Hover the page. Esc to hang it up."
        if ctx.sniffing else "The Sock Detective has gone home. Zzz.")
    ctx.browser.draw()


def _key(ctx, e):
    if getattr(ctx, "sniffing", False) and getattr(e, "keysym", "") == "Escape":
        ctx.sniffing = False
        ctx.hover_box = None
        ctx.set_status("The Sock Detective has gone home. Zzz.")
        ctx.browser.draw()
        return True
    return False


def _motion(ctx, x, y):
    if not getattr(ctx, "sniffing", False):
        return
    tab = ctx.current_tab()
    if not tab or tab.nodes is None:
        return
    node = tab._node_at(x, y)
    # Text words don't get their own layout boxes; walk up to the nearest
    # element that does (hover a word, box the whole paragraph).
    box = None
    cur = node
    while cur is not None and box is None:
        box = tab._find_box(tab.document, cur)
        cur = cur.parent
    ctx.hover_node = node
    ctx.hover_box = box
    if node is not None:
        ctx.set_status(_describe(node, box))
    ctx.browser.draw()


def _overlay(ctx, canvas, offset):
    """Draw the red crime-scene box around the hovered element."""
    if not getattr(ctx, "sniffing", False):
        return
    tab = ctx.current_tab()
    box = getattr(ctx, "hover_box", None)
    node = getattr(ctx, "hover_node", None)
    if not tab or box is None or node is None:
        return
    top = box.y - tab.scroll + offset
    canvas.create_rectangle(box.x, top, box.x + box.width, top + box.height,
                            outline="red", width=2)
    label = _tag(node)
    font = get_font(10, "bold", "roman", "Helvetica")
    canvas.create_rectangle(box.x, top - 14, box.x + font.measure(label) + 4,
                            top, fill="red", outline="red")
    canvas.create_text(box.x + 2, top - 12, text=label, anchor="w",
                       fill="white", font=font)


def _describe(node, box):
    bits = [f"Element <{_tag(node)}>"]
    for attr in ("id", "class", "href", "src", "alt", "title"):
        v = node.attributes.get(attr) if isinstance(node, Element) else None
        if v:
            bits.append(f"{attr}={v}")
    if box is not None:
        bits.append(f"box @({box.x:.0f},{box.y:.0f}) "
                    f"{box.width:.0f}x{box.height:.0f}")
    return " ".join(bits)


def _tag(node):
    if isinstance(node, Text):
        return "text"
    if isinstance(node, Element):
        out = node.tag
        if node.attributes.get("id"):
            out += "#" + node.attributes["id"]
        for cls in node.attributes.get("class", "").split():
            out += "." + cls
        return out
    return "?"


# -- case files ----------------------------------------------------------


def _handle(ctx, url, tab):
    if url.scheme != "toe" or url.host != "sock":
        return None
    path = (url.path or "/").rstrip("/") or "/"
    _log(ctx, tab, url, path)
    if path == "/":
        return {}, _page(tab, "THE CASE FILE", _case_file(ctx, tab)), "text/html"
    if path == "/cases":
        return {}, _page(tab, "THE PAPER TRAIL", _cases(ctx)), "text/html"
    if path == "/errors":
        return {}, _page(tab, "THE SOCK IS IN DISTRESS", _errors(ctx)), "text/html"
    name = path.split("/")[-1]
    if name in _STATIC_REPORTS:
        body = {"dom": _dom_report, "layout": _layout_report,
                "style": _style_report, "help": _help}[name](ctx, tab)
        return {}, _page(tab, _STATIC_REPORTS[name], body), "text/html"
    return {}, _page(tab, "NO SUCH CASE", _not_found(path)), "text/html"


def _log(ctx, tab, url, path):
    history = getattr(ctx, "case_log", [])
    history.append({
        "url": str(url),
        "path": path,
        "title": getattr(tab, "title", "") or "",
    })
    ctx.case_log = history[-200:]


def _page(tab, title, body):
    return f"""<!doctype html>
<html><head><title>{title}</title><style>{CASE_STYLE}</style></head>
<body>
<h1>🕵 THE SOCK DETECTIVE</h1>
<p class="tag">PRIVATE INVESTIGATIONS · PAGE GUTS DIVISION · CONFIDENTIAL</p>
<h2>{title}</h2>
{body}
<p class="dim">Case files: <a href="toe://sock">case file</a> ·
<a href="toe://sock/dom">footprints</a> ·
<a href="toe://sock/layout">bones</a> ·
<a href="toe://sock/style">fibers</a> ·
<a href="toe://sock/cases">paper trail</a> ·
<a href="toe://sock/errors">distress</a> ·
<a href="toe://sock/help">help</a></p>
</body></html>
"""


def _kv(k, v, extra=""):
    return f'<div class="box"><span class="k">{k}</span>: ' \
           f'<span class="v">{_html.escape(str(v))}</span>{extra}</div>'


def _case_file(ctx, tab):
    lines = []
    lines.append(_kv("SOLE LENGTH (content height)", f"{tab.content_height()}px"))
    lines.append(_kv("CURRENT LOCATION", tab.url))
    lines.append(_kv("PAGE TITLE", tab.title))
    if tab.nodes is not None:
        lines.append(_kv("TOE COUNT (DOM nodes)", len(_tree(tab.nodes))))
    lines.append(_kv("SKELETON SIZE (layout boxes)", _box_count(tab)))
    lines.append(_kv("PAINT COMMANDS", len(tab.display_list)))
    lines.append(_kv("SCROLL POSITION", f"{tab.scroll}px"))
    lines.append(_kv("HISTORY (back)", len(tab.history)))
    lines.append(_kv("HISTORY (forward)", len(tab.future)))
    lines.append(_kv("TOES ON THE CASE", ", ".join(
        t.name for t in tab.browser.toes) if tab.browser else "none"))
    lines.append(_kv("SOCK DRAWER (response cache)", _cache_size()))
    lines.append(_kv("CASES WITNESSED", len(getattr(ctx, "case_log", []))))
    return "\n".join(lines)


def _dom_report(ctx, tab):
    if tab.nodes is None:
        return "<p class='dim'>No page has been sniffed yet.</p>"
    return "<pre>" + "\n".join(_tree_line(n) for n in _tree(tab.nodes)) + "</pre>"


def _tree_line(node, depth=0):
    pad = "  " * depth
    if isinstance(node, Text):
        t = node.text.strip()
        if not t:
            return ""
        return f"{pad}{_html.escape(t[:60])}"
    attrs = ""
    if isinstance(node, Element) and node.attributes:
        attrs = " " + " ".join(
            f"{k}={_html.escape(str(v))}" for k, v in node.attributes.items())
    return f"{pad}&lt;{_html.escape(node.tag)}{_html.escape(attrs)}&gt;"


def _layout_report(ctx, tab):
    if tab.document is None:
        return "<p class='dim'>No page has been sniffed yet.</p>"
    lines = []
    for box in _boxes(tab.document):
        lines.append(f"{_box_indent(box)}{_box_tag(box)} "
                     f"({box.x:.0f},{box.y:.0f}) "
                     f"{box.width:.0f}x{box.height:.0f}")
    return "<pre>" + "\n".join(lines) + "</pre>"


def _box_indent(box):
    depth = 0
    p = box.parent
    while p is not None:
        depth += 1
        p = p.parent
    return "  " * depth


def _box_tag(box):
    node = box.node
    if isinstance(node, Text):
        return "text"
    if isinstance(node, Element):
        return f"<{_html.escape(node.tag)}>"
    return "?"


def _style_report(ctx, tab):
    if tab.nodes is None:
        return "<p class='dim'>No page has been sniffed yet.</p>"
    lines = []
    for node in _tree(tab.nodes):
        if isinstance(node, Element) and node.style:
            styles = " ".join(f"{k}={_html.escape(str(v))}"
                              for k, v in sorted(node.style.items()))
            lines.append(f"{_tag(node)} — {styles}")
    return "<pre>" + "\n".join(lines) + "</pre>"


def _cases(ctx):
    log = getattr(ctx, "case_log", [])
    if not log:
        return "<p class='dim'>The detective has witnessed nothing. Suspicious.</p>"
    rows = []
    for i, entry in enumerate(log, 1):
        rows.append(_kv(f"CASE {i}", entry["url"],
                        f' <span class="dim">({_html.escape(entry["title"])})</span>'))
    return "\n".join(rows)


def _errors(ctx):
    log = getattr(ctx, "case_log", [])
    bad = [e for e in log if "Could not load" in e.get("title", "")
           or "Rendering error" in e.get("title", "")]
    if not bad:
        return ("<div class='box'>No distress calls yet. "
                "The sock is holding together. For now.</div>")
    rows = []
    for i, entry in enumerate(bad, 1):
        rows.append(_kv(f"DISTRESS {i}", entry["url"],
                        f' <span class="dim">({_html.escape(entry["title"])})</span>'))
    intro = ("<div class='alert'>The detective detected {0} page(s) in "
             "distress. Check the URL, and remember: a broken page is just a "
             "sock with a hole — the browser shows you the tear.</div>".format(
                 len(bad)))
    return intro + "\n" + "\n".join(rows)


def _help(ctx, tab):
    return """
<div class="box"><b>Sniff mode</b>: hit the "sock" button, then hover the
page. A red box follows your cursor and names the element in the status
bar. Esc exits.</div>
<div class="box"><b>Case files</b> (rendered by this very toe):
<a href="toe://sock">toe://sock</a> — vitals ·
<a href="toe://sock/dom">/dom</a> — the DOM tree ·
<a href="toe://sock/layout">/layout</a> — layout boxes ·
<a href="toe://sock/style">/style</a> — computed styles ·
<a href="toe://sock/cases">/cases</a> — the paper trail ·
<a href="toe://sock/errors">/errors</a> — pages in distress.</div>
<div class="box"><b>Why "sock"?</b> The page is the sock. The detective
pulls it on, looks at the seams, and tells you exactly what's holding your
feet together.</div>
<div class="box"><b>Also on the case</b>: the <a href="toe://toebar">Toe
Bar</a> (a 2003 toolbar that clutters your screen) and the
<a href="toe://gallery">toe gallery</a>.</div>
"""


def _not_found(path):
    return ("<div class='alert'>No such case on file: "
            f"<b>{_html.escape(path)}</b>."
            "<br>Try <a href='toe://sock'>the case file</a>.</div>")


# -- tree walking helpers ------------------------------------------------


def _tree(node):
    """Flatten a DOM tree, parents before children."""
    out = [node]
    for child in node.children:
        out.extend(_tree(child))
    return out


def _boxes(box):
    """Flatten a layout tree."""
    out = [box]
    for child in getattr(box, "children", []):
        out.extend(_boxes(child))
    return out


def _box_count(tab):
    return len(_boxes(tab.document)) if tab.document is not None else 0


def _cache_size():
    from feetbrowser.net import _CACHE
    return len(_CACHE)
