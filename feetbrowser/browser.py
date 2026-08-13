"""The FeetBrowser GUI and the load pipeline that ties every stage together.

Pipeline per navigation:
    URL.request -> HTMLParser -> collect stylesheets -> CSSParser + cascade
    -> DocumentLayout -> display list -> paint on a Tk canvas.

Chrome (tabs, address bar, back/forward, scrollbar) is drawn by hand on a
second canvas so the whole browser really is "from scratch".
"""

import os
import sys
import tkinter

from .net import URL
from .htmlparser import HTMLParser, Text, Element
from .cssparser import CSSParser, style
from .layout import DocumentLayout, paint_tree, get_font
from . import toes as toes

WIDTH, HEIGHT = 1000, 720
SCROLL_STEP = 80
CHROME_HEIGHT = 80  # tabs + address bar

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "ua.css")) as f:
    DEFAULT_STYLE_SHEET = CSSParser(f.read()).parse()


def tree_to_list(tree, out):
    out.append(tree)
    for child in tree.children:
        tree_to_list(child, out)
    return out


def find_links(node, out):
    """Collect <link rel=stylesheet href=...> hrefs."""
    if isinstance(node, Element) and node.tag == "link" \
            and node.attributes.get("rel", "").lower() == "stylesheet" \
            and "href" in node.attributes:
        out.append(node.attributes["href"])
    for child in node.children:
        find_links(child, out)
    return out


def inline_styles(node, out):
    if isinstance(node, Element) and node.tag == "style":
        text = "".join(c.text for c in node.children if isinstance(c, Text))
        out.append(text)
    for child in node.children:
        inline_styles(child, out)
    return out


def get_title(node):
    for n in tree_to_list(node, []):
        if isinstance(n, Element) and n.tag == "title":
            return "".join(c.text for c in n.children
                           if isinstance(c, Text)).strip()
    return None


class Tab:
    """One document: its DOM, layout, scroll position and history."""

    def __init__(self, tab_height, browser=None):
        self.history = []
        self.future = []
        self.url = None
        self.scroll = 0
        self.tab_height = tab_height
        self.browser = browser
        self.display_list = []
        self.document = None
        self.nodes = None
        self.title = "New Tab"
        self.status = ""

    # -- navigation ------------------------------------------------------

    def load(self, url, payload=None, push=True):
        if isinstance(url, str):
            base = self.url
            url = base.resolve(url) if (base and "://" not in url
                                        and not url.startswith(("data:", "file:",
                                                                "view-source:"))) \
                else URL(url)
        self.status = f"Loading {url}..."
        try:
            handled = None
            if self.browser:
                handled = toes.first(self.browser.toe_contexts, "handle", url, self)
            if handled is not None:
                _headers, body, ctype = handled
            else:
                _headers, body, ctype = url.request(payload=payload)
        except Exception as e:  # noqa: BLE001 - surface any network error in-page
            body = f"<h1>Could not load page</h1><pre>{type(e).__name__}: {e}</pre>"
            ctype = "text/html"

        if push and self.url is not None:
            self.history.append((self.url, self.scroll))
            self.future.clear()
        self.url = url
        self.scroll = 0

        if url.view_source or ctype.startswith("text/plain"):
            escaped = (body.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
            body = f"<pre>{escaped}</pre>"

        try:
            self._build(url, body)
        except Exception as e:  # noqa: BLE001 - never leave the tab half-rendered
            err = (f"<h1>Rendering error</h1>"
                   f"<pre>{type(e).__name__}: {e}</pre>")
            self.title = "Error"
            self._build(url, err)

        self.status = str(url)
        if getattr(url, "fragment", ""):
            self.scroll_to_fragment(url.fragment)

    def _build(self, url, body):
        """Parse, collect stylesheets, cascade, and lay out `body`."""
        if self.browser:
            body = toes.rewrite(self.browser.toe_contexts, url, body)
        self.nodes = HTMLParser(body).parse()
        self.title = get_title(self.nodes) or str(url)

        # Gather stylesheets: UA + toe-injected + <style> + <link rel=stylesheet>.
        rules = list(DEFAULT_STYLE_SHEET)
        if self.browser:
            injected = toes.extra_css(self.browser.toe_contexts, url)
            if injected:
                try:
                    rules.extend(CSSParser(injected).parse())
                except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                    pass
        for sheet in inline_styles(self.nodes, []):
            try:
                rules.extend(CSSParser(sheet).parse())
            except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                pass
        for href in find_links(self.nodes, []):
            try:
                sheet_url = url.resolve(href)
                _h, css_body, _c = sheet_url.request()
                rules.extend(CSSParser(css_body).parse())
            except Exception:  # noqa: BLE001 - skip stylesheets that fail to load
                continue

        style(self.nodes, rules)
        self.render()

    def render(self):
        self.document = DocumentLayout(self.nodes, WIDTH)
        self.document.layout()
        self.display_list = []
        paint_tree(self.document, self.display_list)

    def content_height(self):
        return self.document.height if self.document else 0

    def go_back(self):
        if not self.history:
            return
        self.future.append((self.url, self.scroll))
        url, scroll = self.history.pop()
        self.load(url, push=False)
        self.scroll = scroll
        self._clamp_scroll()

    def go_forward(self):
        if not self.future:
            return
        self.history.append((self.url, self.scroll))
        url, scroll = self.future.pop()
        self.load(url, push=False)
        self.scroll = scroll
        self._clamp_scroll()

    # -- interaction -----------------------------------------------------

    def scroll_by(self, delta):
        self.scroll += delta
        self._clamp_scroll()

    def _clamp_scroll(self):
        max_y = max(0, self.content_height() - self.tab_height)
        self.scroll = max(0, min(self.scroll, max_y))

    def scroll_to_fragment(self, frag):
        for node in tree_to_list(self.nodes, []):
            if isinstance(node, Element) and \
                    (node.attributes.get("id") == frag or
                     (node.tag == "a" and node.attributes.get("name") == frag)):
                box = self._find_box(self.document, node)
                if box:
                    self.scroll = max(0, box.y - 20)
                    self._clamp_scroll()
                return

    def _find_box(self, box, node):
        if getattr(box, "node", None) is node:
            return box
        for child in box.children:
            found = self._find_box(child, node)
            if found:
                return found
        return None

    def _node_at(self, x, y):
        """Return the DOM node whose painted word is under (x, y)."""
        y += self.scroll
        hit = None
        for cmd in self.display_list:
            if hasattr(cmd, "hit") and cmd.node is not None and cmd.hit(x, y):
                hit = cmd.node  # last match wins (painted on top)
        return hit

    @staticmethod
    def _enclosing_link(node):
        while node:
            if isinstance(node, Element) and node.tag == "a" \
                    and "href" in node.attributes:
                return node.attributes["href"]
            node = node.parent
        return None

    def click(self, x, y):
        """Handle a click at document coords; returns a URL to load or None."""
        href = self._enclosing_link(self._node_at(x, y))
        if not href:
            return None
        if href.startswith(("javascript:", "mailto:", "tel:")):
            self.status = href
            return None
        return self.url.resolve(href)

    def link_at(self, x, y):
        """Return href under the cursor for hover feedback, else None."""
        return self._enclosing_link(self._node_at(x, y))

    def draw(self, canvas, offset):
        for cmd in self.display_list:
            if cmd.top > self.scroll + self.tab_height:
                continue
            if cmd.bottom < self.scroll:
                continue
            cmd.execute(self.scroll - offset, canvas)


class Browser:
    def __init__(self):
        self.tabs = []
        self.active_tab = None
        self.focus = None  # "address" or None
        self.address_text = ""
        self._resize_after = None
        self._last_size = (WIDTH, HEIGHT)

        # Toes: one Context per loaded toe, all optional hooks.
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, toe.module) for toe in self.toes]
        self.toe_handlers = {}
        for ctx in self.toe_contexts:
            for btn in (ctx.call("buttons") or []):
                self.toe_handlers[btn.id] = ctx

        self.window = tkinter.Tk()
        self.window.title("FeetBrowser")
        self.window.geometry(f"{WIDTH}x{HEIGHT}")
        self.window.minsize(480, 320)
        self.canvas = tkinter.Canvas(
            self.window, width=WIDTH, height=HEIGHT,
            bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.chrome_font = get_font(14, "normal", "roman", "Helvetica")
        self.bold_font = get_font(14, "bold", "roman", "Helvetica")

        self._bind()

    def _bind(self):
        w = self.window
        w.bind("<Down>", self._on_down)
        w.bind("<Up>", self._on_up)
        w.bind("<MouseWheel>", self._on_wheel)
        w.bind("<Button-4>", lambda e: self._scroll(-SCROLL_STEP))
        w.bind("<Button-5>", lambda e: self._scroll(SCROLL_STEP))
        w.bind("<Button-1>", self._on_click)
        w.bind("<Motion>", self._on_motion)
        w.bind("<Key>", self._on_key)
        w.bind("<Return>", self._on_enter)
        w.bind("<BackSpace>", self._on_backspace)
        w.bind("<Configure>", self._on_resize)
        w.bind("<Control-l>", lambda e: self._focus_address())
        w.bind("<Control-t>", lambda e: self.new_tab("about:blank"))
        w.bind("<Control-w>", lambda e: self.close_tab())
        w.bind("<Control-r>", lambda e: self._reload())
        w.bind("<Alt-Left>", lambda e: self._back())
        w.bind("<Alt-Right>", lambda e: self._forward())

    # -- tab management --------------------------------------------------

    def tab_height(self):
        h = self.canvas.winfo_height()
        if h <= 1:  # window not mapped yet
            h = HEIGHT
        return max(50, h - CHROME_HEIGHT)

    def new_tab(self, url):
        tab = Tab(self.tab_height(), self)
        if url == "about:blank":
            tab.load(_AboutURL())  # routes welcome page through the full pipeline
            tab.status = "Type a URL and press Enter"
        else:
            tab.load(url)
        self.tabs.append(tab)
        self.active_tab = tab
        toes.dispatch(self.toe_contexts, "on_new_tab")
        self.draw()

    def close_tab(self):
        if not self.active_tab:
            return
        idx = self.tabs.index(self.active_tab)
        self.tabs.remove(self.active_tab)
        if not self.tabs:
            self.window.destroy()
            return
        self.active_tab = self.tabs[min(idx, len(self.tabs) - 1)]
        self.draw()

    # -- event handlers --------------------------------------------------

    def _on_resize(self, e):
        # <Configure> fires continuously during a drag and also on window
        # moves. Only react to real size changes, and debounce the (possibly
        # expensive) re-layout until the drag settles.
        if e.widget is not self.window:
            return
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size == self._last_size or size[0] <= 1:
            return
        self._last_size = size
        if self._resize_after is not None:
            self.window.after_cancel(self._resize_after)
        self._resize_after = self.window.after(100, self._apply_resize)

    def _apply_resize(self):
        self._resize_after = None
        global WIDTH, HEIGHT
        WIDTH = self.canvas.winfo_width()
        HEIGHT = self.canvas.winfo_height()
        for tab in self.tabs:
            tab.tab_height = self.tab_height()
            if tab.nodes:
                tab.render()
                tab._clamp_scroll()
        self.draw()

    def _on_down(self, e):
        self._scroll(SCROLL_STEP)

    def _on_up(self, e):
        self._scroll(-SCROLL_STEP)

    def _on_wheel(self, e):
        self._scroll(-e.delta if abs(e.delta) < 30 else -int(e.delta / 30) * SCROLL_STEP)

    def _scroll(self, delta):
        if self.active_tab:
            self.active_tab.scroll_by(delta)
            self.draw()

    def _on_click(self, e):
        self.focus = None
        if e.y < CHROME_HEIGHT:
            self._chrome_click(e.x, e.y)
        else:
            if not self.active_tab:
                return
            dest = self.active_tab.click(e.x, e.y - CHROME_HEIGHT)
            if dest:
                self.active_tab.load(dest)
            self.draw()

    def _chrome_click(self, x, y):
        # Tab bar (top 40px).
        if y < 40:
            # New-tab button.
            if x < 34:
                self.new_tab("about:blank")
                return
            for i, tab in enumerate(self.tabs):
                x0 = 40 + i * 160
                if x0 <= x < x0 + 158:
                    # close box
                    if x >= x0 + 158 - 20:
                        self.active_tab = tab
                        self.close_tab()
                        return
                    self.active_tab = tab
                    self.draw()
                    return
            return
        # Toolbar (40..80).
        if 8 <= x < 34 and 48 <= y < 72:
            self._back()
            return
        if 40 <= x < 66 and 48 <= y < 72:
            self._forward()
            return
        if 72 <= x < 98 and 48 <= y < 72:
            self._reload()
            return
        # Toe toolbar buttons.
        bx = 104
        for btn in self._toe_buttons():
            if bx <= x < bx + 26 and 48 <= y < 72:
                ctx = self.toe_handlers.get(btn.id)
                if ctx:
                    ctx.call("on_click", btn.id)
                self.draw()
                return
            bx += 30
        # Address bar.
        if x >= 104 + self._toe_buttons_offset():
            self.focus = "address"
            self.address_text = str(self.active_tab.url) if \
                (self.active_tab and self.active_tab.url and
                 not isinstance(self.active_tab.url, _AboutURL)) else ""
            self.draw()

    def _on_motion(self, e):
        if not self.active_tab:
            return
        if e.y >= CHROME_HEIGHT:
            href = self.active_tab.link_at(e.x, e.y - CHROME_HEIGHT)
            self.canvas.config(cursor="hand2" if href else "")
            new_status = href or str(self.active_tab.url or "")
            if new_status != self.active_tab.status:
                self.active_tab.status = new_status
                self._draw_status()
        else:
            self.canvas.config(cursor="")

    def _on_key(self, e):
        if self.focus != "address":
            if toes.dispatch(self.toe_contexts, "on_keypress", e):
                return
            return
        if len(e.char) == 1 and ord(e.char) >= 32 and e.char.isprintable():
            self.address_text += e.char
            self.draw()

    def _on_backspace(self, e):
        if self.focus == "address":
            self.address_text = self.address_text[:-1]
            self.draw()

    def _on_enter(self, e):
        if self.focus == "address" and self.address_text.strip():
            self.focus = None
            query = self.address_text.strip()
            if not self._looks_like_url(query):
                query = "https://duckduckgo.com/html/?q=" + \
                    query.replace(" ", "+")
            elif "://" not in query and not query.startswith(
                    ("file:", "data:", "view-source:", "about:")):
                query = "https://" + query
            if self.active_tab:
                self.active_tab.load(query)
            self.draw()

    @staticmethod
    def _looks_like_url(text):
        if " " in text.strip():
            return False
        if text.startswith(("http://", "https://", "file:", "data:",
                            "view-source:", "about:")):
            return True
        return "." in text and not text.startswith(".")

    def _focus_address(self):
        self.focus = "address"
        self.address_text = str(self.active_tab.url) if \
            (self.active_tab and self.active_tab.url) else ""
        self.draw()

    def _back(self):
        if self.active_tab:
            self.active_tab.go_back()
            self.draw()

    def _forward(self):
        if self.active_tab:
            self.active_tab.go_forward()
            self.draw()

    def _reload(self):
        # Pass the URL object (not its string) so internal pages like the
        # about:blank welcome page reload without being re-parsed as a URL.
        if self.active_tab and self.active_tab.url:
            self.active_tab.load(self.active_tab.url, push=False)
            self.draw()

    # -- painting --------------------------------------------------------

    def draw(self):
        self.canvas.delete("all")
        if self.active_tab:
            self.active_tab.tab_height = self.tab_height()
            self.active_tab.draw(self.canvas, CHROME_HEIGHT)
        toes.dispatch(self.toe_contexts, "on_draw", self.canvas, CHROME_HEIGHT)
        # Chrome background covers page content that scrolled up under it.
        self.canvas.create_rectangle(0, 0, self.canvas.winfo_width(),
                                     CHROME_HEIGHT, fill="#e8e8e8", width=0)
        self._draw_tabs()
        self._draw_toolbar()
        self._draw_toe_buttons()
        self._draw_status()
        self._draw_scrollbar()
        self.window.title(
            (self.active_tab.title if self.active_tab else "FeetBrowser")
            + " — FeetBrowser")

    def _draw_tabs(self):
        c = self.canvas
        c.create_rectangle(0, 0, c.winfo_width(), 40, fill="#d0d0d0", width=0)
        # New-tab button.
        c.create_text(17, 20, text="+", font=self.bold_font, fill="#333")
        for i, tab in enumerate(self.tabs):
            x0 = 40 + i * 160
            active = tab is self.active_tab
            c.create_rectangle(x0, 4, x0 + 158, 40,
                               fill="white" if active else "#c4c4c4",
                               width=0)
            title = tab.title or "New Tab"
            if len(title) > 18:
                title = title[:17] + "…"
            c.create_text(x0 + 10, 20, text=title, anchor="w",
                          font=self.chrome_font, fill="#222")
            c.create_text(x0 + 148, 20, text="×", font=self.bold_font, fill="#666")

    def _draw_toolbar(self):
        c = self.canvas

        def btn(x, glyph, enabled):
            c.create_rectangle(x, 48, x + 26, 72, outline="#999",
                               fill="#f4f4f4", width=1)
            c.create_text(x + 13, 60, text=glyph,
                          fill="#333" if enabled else "#bbb",
                          font=self.bold_font)

        tab = self.active_tab
        btn(8, "‹", bool(tab and tab.history))
        btn(40, "›", bool(tab and tab.future))
        btn(72, "⟳", bool(tab))

        # Address bar.
        addr_x = 104 + self._toe_buttons_offset()
        c.create_rectangle(addr_x, 48, c.winfo_width() - 8, 72,
                           outline="#3b82f6" if self.focus == "address" else "#999",
                           fill="white", width=2 if self.focus == "address" else 1)
        if self.focus == "address":
            text = self.address_text
            c.create_text(addr_x + 10, 60, text=text, anchor="w",
                          font=self.chrome_font, fill="#111")
            w = self.chrome_font.measure(text)
            c.create_line(addr_x + 12 + w, 52, addr_x + 12 + w, 68, fill="#111")
        else:
            url = ""
            if tab and tab.url and not isinstance(tab.url, _AboutURL):
                url = str(tab.url)
            c.create_text(addr_x + 10, 60, text=url, anchor="w",
                          font=self.chrome_font, fill="#111")

    def _toe_buttons(self):
        return [btn for ctx in self.toe_contexts for btn in (ctx.call("buttons") or [])]

    def _toe_buttons_offset(self):
        return len(self._toe_buttons()) * 30

    def _draw_toe_buttons(self):
        c = self.canvas
        x = 104
        for btn in self._toe_buttons():
            c.create_rectangle(x, 48, x + 26, 72, outline="#999",
                               fill="#fdf6e3", width=1)
            c.create_text(x + 13, 60, text=btn.glyph[:2], fill="#333",
                          font=self.bold_font)
            x += 30

    def _draw_status(self):
        c = self.canvas
        h = c.winfo_height()
        c.create_rectangle(0, h - 22, c.winfo_width(), h,
                           fill="#efefef", width=0)
        c.create_line(0, h - 22, c.winfo_width(), h - 22, fill="#ccc")
        status = self.active_tab.status if self.active_tab else ""
        c.create_text(8, h - 11, text=status[:200], anchor="w",
                      font=get_font(11, "normal", "roman", "Helvetica"),
                      fill="#444")

    def _draw_scrollbar(self):
        tab = self.active_tab
        if not tab:
            return
        view = self.tab_height()
        total = tab.content_height()
        if total <= view:
            return
        c = self.canvas
        track_x = c.winfo_width() - 10
        track_top = CHROME_HEIGHT
        track_h = view
        frac = view / total
        thumb_h = max(30, track_h * frac)
        thumb_top = track_top + (track_h - thumb_h) * (tab.scroll / (total - view))
        c.create_rectangle(track_x, thumb_top, track_x + 6, thumb_top + thumb_h,
                           fill="#9aa0a6", width=0)

    def run(self):
        self.window.update_idletasks()
        self.draw()
        # Redraw once the window is actually mapped and sized.
        self.window.after(120, self.draw)
        self.window.mainloop()


class _AboutURL:
    """Placeholder URL for the internal welcome page."""
    view_source = False
    fragment = ""

    def resolve(self, url):
        return URL(url) if "://" in url else URL("https://" + url)

    def request(self, payload=None):
        return {}, WELCOME_HTML, "text/html"

    def __str__(self):
        return "about:blank"


WELCOME_HTML = """
<!doctype html>
<html><head><title>New Tab</title>
<style>
  body { font-family: Helvetica; margin: 60px; color: #222; }
  h1 { font-size: 40px; color: #1a73e8; }
  .sub { color: #666; font-size: 18px; }
  ul { margin-top: 20px; }
  li { margin-top: 6px; }
  code { background: #f0f0f0; }
  a { color: #1a73e8; }
</style></head>
<body>
  <h1>🦶 FeetBrowser</h1>
  <p class="sub">A web browser built from scratch — its own HTTP client,
  HTML parser, CSS engine, and layout engine.</p>
  <h3>Try these</h3>
  <ul>
    <li><a href="https://example.com">example.com</a> — the classic test page</li>
    <li><a href="https://info.cern.ch/hypertext/WWW/TheProject.html">the first web page ever</a></li>
    <li><a href="https://news.ycombinator.com">Hacker News</a></li>
    <li><a href="https://en.wikipedia.org/wiki/Web_browser">Wikipedia: Web browser</a></li>
    <li><a href="view-source:https://example.com">view-source:example.com</a></li>
  </ul>
  <h3>Shortcuts</h3>
  <ul>
    <li><b>Ctrl-L</b> focus address bar &nbsp; <b>Ctrl-T</b> new tab &nbsp;
        <b>Ctrl-W</b> close tab</li>
    <li><b>Ctrl-R</b> reload &nbsp; <b>Alt-Left/Right</b> back / forward &nbsp;
        <b>↑ ↓ / wheel</b> scroll</li>
  </ul>
  <p class="sub">Type a URL or a search term in the address bar to begin.</p>
</body></html>
"""


def main():
    browser = Browser()
    start = sys.argv[1] if len(sys.argv) > 1 else "about:blank"
    browser.new_tab(start)
    browser.run()


if __name__ == "__main__":
    main()
