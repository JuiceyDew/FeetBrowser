"""The FeetBrowser GUI and the load pipeline that ties every stage together.

Pipeline per navigation:
    URL.request -> htmlparser.parse -> collect stylesheets -> CSSParser + cascade
    -> DocumentLayout -> display list -> paint on a canvas.

Chrome (tabs, address bar, back/forward, scrollbar) is drawn by hand onto the
same canvas, and the canvas is our own rasteriser, so the whole browser
really is "from scratch", pixels included.
"""

import os
import re
import sys
import time
import json
import html
import threading
from . import gui
from .canvas import Canvas, CanvasError, PhotoImage
from .window import Tk
import urllib.parse
from collections import deque

from .net import URL, open_stream
from .htmlparser import Text, Element
from .htmlparser import parse as parse_html
from .cssparser import CSSParser, style, parse_inline, set_viewport, \
    media_matches, get_viewport
from .layout import DocumentLayout, paint_tree, get_font, _measure, \
    DrawVideoControls, \
    field_value, field_checked, \
    select_options, selected_options, option_value, \
    select_rows, listbox_rows, listbox_scroll, listbox_active, \
    LISTBOX_ROW_H, LISTBOX_PAD
from .selection import Index as SelectionIndex, Selection, \
    contrasting_text_color
from . import shoes as shoes
from . import settings as settings
from . import downloads as downloads
from . import media
from . import arch
from .jsdom import JSDocument, JSLocation, _JSStaticProps, _JSComputedStyle
from .jsengine import Interpreter, JSException, UNDEFINED
from . import toes as toes
from . import __version__

WIDTH, HEIGHT = 1000, 720
SCROLL_STEP = 80
# Wheel momentum: a fast flick keeps coasting after the last notch and
# decays to nothing instead of stopping dead, which is what makes quick
# scrolling feel fast. The coast starts at a fraction of the flick's
# pixels-per-second velocity (see _track_scroll_velocity) and advances one
# frame per timer tick.
MOMENTUM_FRAME_MS = 16
MOMENTUM_DECAY = 0.86
MOMENTUM_STOP = 1.0  # px per frame; below this the coast gives up
MOMENTUM_SETTLE_MS = 45  # no new notch in this long -> start coasting
MOMENTUM_GAIN = 0.012  # coast seed, as a fraction of the flick's speed
MOMENTUM_MAX = 40.0  # px per frame the coast starts at, at most
RANGE_GLIDE_MS = 16  # per-frame delay while the thumb glides to a press
RANGE_GLIDE_FRAMES = 8  # frames the glide takes (about an eighth of a second)
CHROME_HEIGHT = 80  # tabs + address bar
LOG_HEIGHT = 16  # slim strip under the toolbar reporting load errors
TAB_LEFT = 8  # first tab's left edge on the tab strip
TAB_WIDTH = 158  # each tab's drawn width
TAB_GAP = 160  # stride between tab left edges (TAB_WIDTH + 2px gutter)
TAB_CLOSE_W = 20  # hit width of the per-tab "×" close box
NEW_TAB_W = 34  # hit width of the "+" new-tab button
MENU_BTN_W = 26  # hit width of the hamburger settings button
# How far the pointer has to travel before a press on a tab becomes a drag.
# Small enough that a deliberate move starts one at once, large enough that
# the jitter a hand puts into a click never turns switching tabs into
# rearranging them.
TAB_DRAG_SLOP = 5
SCROLLBAR_RIGHT = 10  # the thumb's left edge, measured back from the right
SCROLLBAR_W = 6  # the thumb's drawn width
SCROLLBAR_MIN_THUMB = 30  # a thumb shorter than this is too small to grab
# The thumb is 6px wide and a pointer is not that accurate, so the region
# that answers to a press is the whole gutter: a little to the left of the
# thumb, and everything to the right of it out to the window edge.
SCROLLBAR_GRAB_PAD = 4
BOOKMARKS_FILE = os.path.expanduser("~/.feetbrowser_bookmarks.json")
MAX_CACHED_IMAGES = 300
# Cap the number of concurrent image fetches across the whole browser.
# Without a bound, a photo-heavy page spawns hundreds of threads and sockets
# at once; a small pool keeps memory and file-descriptor use flat while
# still fetching far faster than the layout can paint.
MAX_CONCURRENT_IMAGE_FETCHES = 6
# Two presses this close together, in seconds and in pixels, are one gesture.
# 0.5s is the macOS default double-click interval and the slop is what a hand
# moves between clicks it means as one.
MULTI_CLICK_SECONDS = 0.5
MULTI_CLICK_SLOP = 4
# How wide a strip down the right-hand edge of the page belongs to the
# scrollbar rather than to the text under it -- derived from the bar's own
# grab region rather than restated, so the strip that refuses a selection is
# exactly the strip that answers a press.
SCROLLBAR_GUTTER_W = SCROLLBAR_RIGHT + SCROLLBAR_GRAB_PAD
# What each click of a multi-click selects. Double-click takes the word and
# triple-click the line, which is the convention on both platforms we run on.
_CLICK_GRANULARITY = {2: "word", 3: "line"}
_image_fetch_sem = threading.Semaphore(MAX_CONCURRENT_IMAGE_FETCHES)
# The same bound for the sheets and scripts a document names. Those are
# fetched from the UI thread rather than from a worker, because the cascade
# and the execution order both depend on document order, so the window is
# frozen for as long as they take -- and a real page names a lot of them
# (discord.com: two stylesheets and eighteen scripts, spread over four
# hosts). Wider than the image bound because it is latency and not bandwidth
# being spent: every one of those is a fresh TLS handshake to somewhere, and
# the window is held still until the last of them answers. See `_fetch_all`.
MAX_CONCURRENT_SUBRESOURCE_FETCHES = 12
# How long Browser.settle() waits for a page to stop having work outstanding.
# It is a ceiling, not a delay: settling returns the moment the last image is
# in, and only a page pointing at something that never answers waits it out.
# Generous, because the alternative is a screenshot of a half-drawn page.
# How often the frame timer runs. 40 ms is 25 ticks a second: a ceiling on
# how often a frame can change, not a frame rate -- the scheduler shows
# whatever the clock says is current, so a faster file drops frames here
# rather than playing slowly. See docs/media.md.
VIDEO_TICK_MS = 40

# `<source type="...">` values we will even try. Filtering here means a page
# offering WebM first and AVI second gets the AVI, which is the entire purpose
# of the element. A type we can decode is not the same as a container we can
# decode -- `video/mp4` covers both an H.264 film we cannot play and a Motion
# JPEG .mov we can -- so the container types stay on this list and the codec
# question is settled by opening the file.
PLAYABLE_TYPES = ("video/x-msvideo", "video/avi", "video/msvideo",
                  "video/vnd.avi", "video/quicktime", "video/x-motion-jpeg",
                  "video/x-jpeg", "video/mjpeg", "multipart/x-mixed-replace")

# The same question asked of a URL, for the many pages that write `<source>`
# with no type on it at all.
PLAYABLE_EXTENSIONS = (".avi", ".mov", ".qt", ".mjpeg", ".mjpg", ".mjpe")


def _first_playable_source(node):
    """Pick a `<source>` for a `<video>` that has no src of its own.

    Prefers one whose `type` we know we can decode, then one whose URL has an
    extension we know, and falls back to the first source with a src at all --
    the last case being how the element still shows a real "MP4, H.264, no
    decoder" box instead of nothing.
    """
    sources = [n for n in tree_to_list(node, [])
               if isinstance(n, Element) and n.tag == "source"
               and n.attributes.get("src")]
    for candidate in sources:
        kind = candidate.attributes.get("type", "").split(";")[0].strip()
        if kind.lower() in PLAYABLE_TYPES:
            return candidate.attributes["src"]
    for candidate in sources:
        path = candidate.attributes["src"].lower().split("?")[0]
        if path.endswith(PLAYABLE_EXTENSIONS):
            return candidate.attributes["src"]
    return sources[0].attributes["src"] if sources else ""


SETTLE_TIMEOUT = 30.0

# Deeply nested documents walk DOM/layout trees recursively; give Python a
# comfortable margin so pathological pages degrade gracefully instead of
# crashing with RecursionError.
sys.setrecursionlimit(20000)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "ua.css"), encoding="utf8") as f:
    DEFAULT_STYLE_SHEET = CSSParser(f.read()).parse()


def tree_to_list(tree, out):
    stack = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in reversed(node.children):
            stack.append(child)
    return out


def find_links(node, out):
    """Collect <link rel=stylesheet href=...> hrefs."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "link" \
                and n.attributes.get("rel", "").lower() == "stylesheet" \
                and "href" in n.attributes:
            out.append(n.attributes["href"])
        for child in reversed(n.children):
            stack.append(child)
    return out


def inline_styles(node, out):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "style":
            out.append("".join(c.text for c in n.children if isinstance(c, Text)))
        for child in reversed(n.children):
            stack.append(child)
    return out


_JS_MIME_TYPES = {"text/javascript", "application/javascript",
                  "application/ecmascript", "text/ecmascript", "module"}


def _is_js_script_type(typ):
    """True when a <script> element should be executed: no `type` attribute
    (HTML4 default) or a JavaScript MIME type. Structured-data blocks like
    `application/ld+json` are data, not code, and must not be interpreted."""
    if not typ:
        return True
    return typ.strip().lower() in _JS_MIME_TYPES


def _fetch_all(urls):
    """Fetch every URL in `urls` at once; return what each one answered.

    The result maps `str(url)` to the `(headers, body, ctype)` triple
    `URL.request()` returned, or to the exception it raised -- both are
    answers, and the caller reports a failure in exactly the place it used to
    fetch, so the error text and the order it appears in are unchanged.

    Why this exists: `<link rel=stylesheet>` and `<script src>` have to be
    *used* in document order, because that is what the cascade and script
    semantics mean, but nothing says they have to be *fetched* that way.
    Fetching them one at a time on the UI thread costs the sum of every round
    trip with the window frozen for all of it; fetching them together costs
    the slowest one. The loops that consume this stay exactly as serial as
    they were.

    Duplicates collapse: a page naming the same sheet twice is one fetch,
    which is also what the per-URL caches above already assumed.
    """
    results = {}
    by_key = {}
    for u in urls:
        by_key.setdefault(str(u), u)
    keys = list(by_key)
    if not keys:
        return results

    def fetch(key):
        try:
            results[key] = by_key[key].request()
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            results[key] = exc

    if len(keys) == 1:
        # One URL is the common case and a thread for it buys nothing but a
        # context switch, so pay for the machinery only when it can pay back.
        fetch(keys[0])
        return results
    # A pool rather than a thread each: a page is allowed to name five
    # hundred scripts, and five hundred sockets opened at once is a denial of
    # service aimed at whoever is hosting them.
    pending = deque(keys)

    def drain():
        while True:
            try:
                fetch(pending.popleft())
            except IndexError:
                return

    width = min(len(keys), MAX_CONCURRENT_SUBRESOURCE_FETCHES)
    threads = [threading.Thread(target=drain, daemon=True)
               for _ in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _fetched(results, url):
    """The `(headers, body, ctype)` for `url` out of a `_fetch_all` map,
    re-raising whatever the fetch raised so the caller's existing `except`
    sees the same exception it would have caught fetching inline."""
    answer = results.get(str(url))
    if answer is None:
        return url.request()
    if isinstance(answer, BaseException):
        raise answer
    return answer


def find_base_href(node):
    """Return the href of the document's first <base> element, if any."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "base" and "href" in n.attributes:
            return n.attributes["href"]
        for child in n.children:
            stack.append(child)
    return None


def get_title(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "title":
            text = "".join(c.text for c in n.children
                           if isinstance(c, Text)).strip()
            if text:
                return text
        for child in reversed(n.children):
            stack.append(child)
    return None


# @import url("..."); / @import "..." [media]; matched at a statement
# boundary so a bare "@import" mention inside a rule can't be grabbed.
_IMPORT_RE = re.compile(
    r"(?P<lead>(?:^|[\s{};]))@import\s+"
    r"(?:url\(\s*)?(?P<url>[^'\";\s()]+|'[^']*'|\"[^\"]*\")"
    r"(?:\s*\))?\s*(?P<media>[^;]*);",
    re.IGNORECASE)


def _expand_imports(css, base_url, depth=0, seen=None, log=None):
    """Inline `@import`ed stylesheets so pages that ship their CSS via
    @import don't come out unstyled.

    Imported sheets are fetched relative to `base_url`, nested imports are
    expanded (bounded depth), and the import's own media query is honored
    against the current viewport. A `seen` set guards against cycles.
    """
    if depth > 4 or "@import" not in css:
        return css
    if seen is None:
        seen = set()
    out = []
    last = 0
    width, height = get_viewport()
    for m in _IMPORT_RE.finditer(css):
        out.append(css[last:m.start()])
        out.append(m.group("lead"))
        last = m.end()
        url = (m.group("url") or "").strip().strip("\"'")
        media = (m.group("media") or "").strip()
        if not url or url in seen:
            continue
        if media and not media_matches(media, width, height):
            continue
        seen.add(url)
        imp_url = None
        try:
            imp_url = base_url.resolve(url)
            _h, imported, _c = imp_url.request()
        except Exception as e:  # noqa: BLE001 - a broken import shouldn't stop the page
            if log is not None:
                log(f"CSS {imp_url or url} ({type(e).__name__})")
            continue
        out.append(_expand_imports(imported, imp_url, depth + 1, seen, log))
    out.append(css[last:])
    return "".join(out)


class FormAction:
    """Returned by Tab.click() when a <form> is submitted: load url+payload."""

    __slots__ = ("url", "payload")

    def __init__(self, url, payload):
        self.url = url
        self.payload = payload


class SelectAction:
    """Returned by Tab.click() when a <select> is clicked: drop its list.

    The Tab cannot open the list itself -- the list is painted on the browser
    canvas, above the chrome, and only the Browser knows how big that is --
    so it hands back the control and the box it occupies in page coordinates
    and lets the Browser place the popup.
    """

    __slots__ = ("node", "rect")

    def __init__(self, node, rect):
        self.node = node
        self.rect = rect


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
        self.base_url = None
        self.focused_input = None
        self.form_values = {}
        # Absolute URL -> decoded PhotoImage, shared with the layout
        # so <img> elements render their actual pixels.
        self.image_cache = {}
        # Absolute URL -> media.VideoPlayer, shared with the layout so a
        # <video> lays out at the file's real size and paints its own frames.
        # Separate from image_cache because a player is not a picture: it owns
        # a decode thread and has to be closed when the page goes away.
        self.video_players = []
        # The arch.AudioPlayer behind each of those, when the file had sound
        # we could decode. Held separately because a video can outlive its
        # soundtrack -- attaching is allowed to decline -- and because these
        # are what has to let go of the sound card when the tab dies.
        self.audio_players = []
        self._video_queue = []
        self._video_nodes = {}
        self._video_results = deque()
        self._video_failures = deque()
        self._image_queue = []
        self._image_results = deque()
        # Absolute URLs whose bytes arrived and could not be turned into
        # pixels -- an SVG, a format we have no decoder for, a 404 page served
        # as the image. They are as finished as a decoded one: there is
        # nothing further to wait for and nothing to gain by asking again.
        # Without this, _missing_images() saw a source that was neither
        # cached nor queued and started the fetch over, several times a
        # second, for as long as the tab was open -- so pending_images() was
        # never false and settle() spent its whole timeout on a page that had
        # in fact finished. A dict, not a set, for the same reason the cache
        # beside it is one: insertion order is what makes "drop the oldest" a
        # sentence rather than a coin toss.
        self._image_undecodable = {}
        self._image_root = None
        self._image_done = None
        # Console output accumulated from JS (errors + console.log lines).
        self.js_logs = []
        # Network/load failures worth surfacing in the browser's log strip
        # (CSS/script/image fetches that failed or were dropped).
        self.net_errors = []
        # Image URLs that failed to download, filled by background threads
        # and drained into net_errors on the UI thread.
        self._image_failures = deque()
        # How much of the interpreter's append-only log has already been
        # scanned for JS errors, so _capture_js_errors never double-counts.
        self._js_log_cursor = 0
        # Stylesheet rules for the current document, kept so JS-driven DOM
        # mutations can be re-styled, and the live interpreter reused across
        # script runs and click-handler dispatch.
        self._last_rules = None
        # The <style>/<link> set `_last_rules` was built from, and the parsed
        # bodies of sheets already fetched. Together they let a restyle after
        # a JS mutation notice a newly inserted sheet without re-fetching the
        # ones that were already there.
        self._rule_sources = None
        self._sheet_cache = {}
        self._js_interp = None
        self._js_doc = None
        # Deferred JS/meta-refresh navigation, honored after the current
        # script batch finishes (see _flush_pending_nav). Kept as a
        # (URL, replace) tuple so location.assign/replace/href map to
        # history push vs. replace semantics.
        self._pending_nav = None
        # Background-thread results for JS `fetch()`/`XMLHttpRequest`,
        # drained on the UI thread by `_drain_js`.
        self._js_fetch_results = deque()
        self._js_xhr_results = deque()
        # Async page loading (GUI mode): a generation counter discards stale
        # fetches if the user navigates again mid-load, and the result queue
        # hands bytes from the fetch thread back to the UI thread.
        self.loading = False
        self._load_gen = 0
        self._load_queue = deque()
        self._load_meta = None
        # Page text selection: a selection.Selection (an anchor and a focus,
        # each a DOM text node plus a character offset), or None when nothing
        # is selected. Used by drag-selection + Ctrl+C.
        self.selection = None
        # The selectable text of the current display list, rebuilt lazily
        # because repaint() throws the display list away on every scroll.
        self._sel_index = None

    # -- navigation ------------------------------------------------------

    def load(self, url, payload=None, push=True, refresh=False,
             pending_scroll=0):
        if isinstance(url, str):
            base = self.url
            url = base.resolve(url) if (base and "://" not in url
                                        and not url.startswith(("data:", "file:",
                                                                "view-source:"))) \
                else URL(url)

        # Remote pages must not navigate into the local filesystem: a link to
        # file:///etc/passwd (or view-source:file://...) would be a one-click
        # local-file disclosure. Local pages may still browse locally, and a
        # fresh tab (about:blank) may still open a local file.
        if isinstance(url, URL) and getattr(url, "scheme", "") == "file":
            origin = getattr(self.url, "scheme", "") if self.url else ""
            if origin in ("http", "https"):
                self._complete_load(
                    url, payload, push, pending_scroll,
                    "<h1>Blocked</h1><p>Cannot open a local file while "
                    "viewing a remote page.</p>", "text/html")
                self.status = ("Blocked: local files are not reachable from "
                               "a remote page")
                return
        self.status = f"Loading {url}..."
        if url.view_source:
            self.status = "Loading source..."
        self.focused_input = None
        self.form_values = {}
        self.selection = None

        # Toe-handled (internal) URLs are cheap and stay synchronous. The
        # built-in ToeHub handles toehub:// and framework toe:// pages before
        # any installed toe gets a say.
        handled = None
        if self.browser and isinstance(url, URL):
            from . import toehub
            handled = toehub.handle(url, self)
            if handled is None:
                handled = toes.first(self.browser.toe_contexts, "handle",
                                     url, self)
        if handled is not None:
            _headers, body, ctype = handled
            self._complete_load(url, payload, push, pending_scroll, body, ctype)
            return
        # In the GUI, fetch http(s) off the UI thread so the loading spinner
        # can animate while the network is slow.
        if self._gui_mode() and isinstance(url, URL) \
                and url.scheme in ("http", "https"):
            self._start_async_load(url, payload, push, refresh, pending_scroll)
            return
        try:
            body, ctype, doc_error = self._fetch_document(url, payload, refresh)
        except TypeError:
            # Internal URL objects (about:blank, bookmarks, history) expose a
            # simpler request(); retry without the refresh flag.
            body, ctype, doc_error = self._fetch_document(url, payload, False)
        if body is None and ctype is None:
            # The response was a file, not a page: it is downloading now and
            # this tab stays where it was, the way every browser behaves
            # when a link turns out to be an attachment.
            self.loading = False
            return
        self._complete_load(url, payload, push, pending_scroll, body, ctype,
                            doc_error=doc_error)

    def _stream_document(self, url, payload, refresh):
        """Fetch an http(s) GET as a stream and decide, from the headers
        alone, whether this navigation is a page or a file.

        This is the only place a link to a 2 GB installer can be told apart
        from a link to a page without paying for it: the headers arrive, the
        body is still on the socket, and if the answer is "file" the live
        connection is handed to the download manager, which keeps reading it
        straight to disk. One request, no buffering, no navigation.

        Returns the (body, ctype, doc_error) triple _fetch_document owes its
        caller, or (None, None, None) when the response became a download,
        or None when this path does not apply or did not work out -- in
        which case _fetch_document falls back to the buffered request() it
        has always used. Falling back costs a second request and only
        happens on an error, which is the case that was going to be slow
        anyway.
        """
        manager = getattr(self.browser, "downloads", None) \
            if self.browser is not None else None
        if manager is None or payload is not None or not isinstance(url, URL) \
                or url.scheme not in ("http", "https"):
            return None
        stream = None
        try:
            extra = {"Cache-Control": "no-cache"} if refresh else None
            stream = open_stream(url, extra_headers=extra,
                                 accept_encoding="gzip, deflate")
            if stream.status < 400 and downloads.should_download(stream.headers):
                manager.start(stream.url, stream=stream)
                self.status = "Downloading %s" % downloads.filename_for(
                    stream.url, stream.headers)
                stream = None  # the manager owns the connection now
                return (None, None, None)
            body = stream.read_all()
            ctype = stream.content_type or "text/html"
            text = body.decode(stream.charset(), "replace")
            if self._is_google_js_wall(url, text):
                return None  # let the buffered path do its impersonation
            if str(stream.url) != str(url):
                url._adopt(stream.url)
            return (text, ctype, None)
        except Exception:  # noqa: BLE001 - any trouble here: use the old path
            return None
        finally:
            if stream is not None:
                stream.close()

    def _fetch_document(self, url, payload, refresh):
        """Fetch a document body, surfacing load errors and retrying through a
        Chrome-impersonating transport when a JS-gated site (Google) serves an
        'enable JavaScript' wall instead of its real application."""
        streamed = self._stream_document(url, payload, refresh)
        if streamed is not None:
            return streamed
        try:
            _headers, body, ctype = url.request(payload=payload,
                                                refresh=refresh)
            doc_error = None
        except TypeError:
            _headers, body, ctype = url.request(payload=payload)
            doc_error = None
        except Exception as e:  # noqa: BLE001 - surface any network error in-page
            body = f"<h1>Could not load page</h1><pre>{type(e).__name__}: {e}</pre>"
            doc_error = f"DOC {url} ({type(e).__name__})"
            ctype = "text/html"
        if doc_error is None and self._is_google_js_wall(url, body) \
                and isinstance(url, URL) and url.scheme in ("http", "https"):
            try:
                _headers, body, ctype = url.request_impersonated()
            except Exception:  # noqa: BLE001 - keep the walled response
                pass
        return body, ctype, doc_error

    def _is_google_js_wall(self, url, body):
        host = (getattr(url, "host", "") or "").lower()
        if "google.com" not in host:
            return False
        low = (body or "").lower()
        return ("enablejs" in low or "/httpservice/retry/" in low
                or "isn't supported anymore" in low)

    def _gui_mode(self):
        return self.browser is not None \
            and getattr(self.browser, "window", None) is not None

    def _start_async_load(self, url, payload, push, refresh, pending_scroll):
        """Fetch the page body on a background thread; the UI thread applies
        it in `_poll_async` so canvas and DOM work never leaves the main loop."""
        self.loading = True
        self._load_gen += 1
        gen = self._load_gen
        self._load_meta = {"gen": gen, "url": url, "payload": payload,
                           "push": push, "pending_scroll": pending_scroll}

        def worker():
            try:
                body, ctype, _err = self._fetch_document(url, payload,
                                                         refresh)
                exc = None
            except Exception as e:  # noqa: BLE001 - surfaced as an error page
                body, ctype, exc = None, None, e
            self._load_queue.append((gen, body, ctype, exc))

        threading.Thread(target=worker, daemon=True).start()
        # Self-schedule the drain on the UI thread so the load completes even
        # for tabs not owned by the main window (e.g. popups).
        self.browser.window.after(60, self._poll_async)

    def _poll_async(self):
        """UI thread: apply a finished background fetch, keep polling while
        the load is still in flight."""
        self._drain_async_load()
        if self.loading and self.browser is not None \
                and getattr(self.browser, "window", None) is not None:
            self.browser.window.after(60, self._poll_async)

    def _drain_async_load(self):
        """UI thread: apply a finished background fetch, if it's still the
        current load. Stale results (a newer navigation started meanwhile)
        are discarded."""
        if not self._load_queue:
            return
        gen, body, ctype, exc = self._load_queue.popleft()
        meta = self._load_meta
        if meta is None or gen != meta["gen"]:
            return  # stale load from before the latest navigation
        self._load_meta = None
        if exc is not None:
            body = (f"<h1>Could not load page</h1>"
                    f"<pre>{type(exc).__name__}: {exc}</pre>")
            doc_error = f"DOC {meta['url']} ({type(exc).__name__})"
            ctype = "text/html"
        else:
            doc_error = None
        if body is None and ctype is None:
            # A download took the navigation over (see _stream_document);
            # the tab keeps the page it was showing.
            self.loading = False
            return
        self._complete_load(meta["url"], meta["payload"], meta["push"],
                            meta["pending_scroll"], body, ctype,
                            doc_error=doc_error)

    def _complete_load(self, url, payload, push, pending_scroll, body, ctype,
                       doc_error=None):
        """Shared tail of load(): apply a fetched body to the tab."""
        if push and self.url is not None:
            self.history.append((self.url, self.scroll))
            self.future.clear()
        self.url = url
        self.scroll = pending_scroll or 0

        if url.view_source or ctype.startswith("text/plain"):
            escaped = (body.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
            body = f"<pre>{escaped}</pre>"
            ctype = "text/html"

        try:
            self._build(url, body, ctype)
        except Exception as e:  # noqa: BLE001 - never leave the tab half-rendered
            err = (f"<h1>Rendering error</h1>"
                   f"<pre>{type(e).__name__}: {e}</pre>")
            self.title = "Error"
            self._build(url, err, "text/html")

        if doc_error is not None:
            self._add_error(doc_error)

        # A script or <meta http-equiv=refresh> may have asked to navigate
        # away (e.g. DuckDuckGo's /l/ redirect page). Honor it before this
        # intermediate document settles into view: the redirect target
        # replaces the current entry rather than being pushed on top of it.
        if self._pending_nav is not None:
            self._flush_pending_nav()
            return

        self.loading = False
        self.status = str(url)
        if getattr(url, "fragment", ""):
            self.scroll_to_fragment(url.fragment)
        self._clamp_scroll()
        # With the DOM ready, start image loading and repaint.
        if self._gui_mode():
            self.load_images(self.browser.window, done=self.browser.draw)
        else:
            # Headless (tests / no Browser): fetch and decode synchronously so
            # the display list shows real images instead of placeholders.
            self.load_images()
        self.load_videos(self.browser.window if self._gui_mode() else None)

    def _build(self, url, body, ctype="text/html"):
        """Parse, collect stylesheets, cascade, and lay out `body`."""
        self.stop_videos()
        # Fresh document: drop any previous form focus/values and JS state.
        self.focused_input = None
        self.form_values = {}
        self.js_logs = []
        self.net_errors = []
        self._image_failures = deque()
        self._image_queue = []
        self._image_results = deque()
        self._js_log_cursor = 0
        self._js_interp = None
        self._js_doc = None
        # A new document means a new cascade; the old page's sheets must not
        # leak into it.
        self._last_rules = None
        self._rule_sources = None
        self._sheet_cache = {}
        self._pending_nav = None
        self._js_fetch_results.clear()
        self._js_xhr_results.clear()

        if ctype.startswith("image/"):
            # A document that *is* an image: show the image itself, fetched
            # through the normal <img> pipeline (which decodes and displays
            # it at its natural size), rather than a placeholder that dumps
            # raw bytes. The image URL is the document URL.
            esc_url = html.escape(str(url), quote=True)
            body = (
                "<!doctype html><html><head></head>"
                "<body style='background:#202124;text-align:center;"
                "padding:24px;margin:0'>"
                f"<img src='{esc_url}'>"
                "</body></html>"
            )

        if self.browser:
            body = toes.rewrite(self.browser.toe_contexts, url, body)

        self.nodes = parse_html(body)
        self.title = get_title(self.nodes) or str(url)

        # <base href> (if any) overrides where relative URLs resolve from.
        base_href = find_base_href(self.nodes)
        self.base_url = url.resolve(base_href) if base_href else url
        resolve_from = self.base_url

        # <meta http-equiv="refresh"> is a server-agnostic redirect: honor a
        # zero-delay one before we bother styling/laying out this page.
        self._check_meta_refresh()

        # Sheets and scripts go on the wire together, before either is
        # wanted. They are wanted at different moments -- the cascade needs
        # the sheets, and only the finished layout runs the scripts -- and
        # asking for them in that order would put two waits end to end when
        # the document has already named everything both of them will fetch.
        prefetched = _fetch_all(self._subresource_urls(resolve_from))

        rules = self._gather_rules(url, resolve_from, prefetched)

        style(self.nodes, rules)
        # Keep the rules around so JS mutations can re-style the tree.
        self._last_rules = rules

        self._absolutize_media_srcs()
        self.render()
        self._run_scripts(prefetched)

    def _subresource_urls(self, resolve_from):
        """Every stylesheet and script URL this document names, absolute.

        Both lists are fixed the moment the HTML is parsed: `_gather_rules`
        reads the same `<link>`s and `_run_scripts` runs the `<script>`s it
        collected before any of them ran. An href that will not resolve is
        left out and reported where it is used, not here.
        """
        hrefs = list(find_links(self.nodes, []))
        hrefs.extend(el.attributes["src"]
                     for el in tree_to_list(self.nodes, [])
                     if isinstance(el, Element) and el.tag == "script"
                     and el.attributes.get("src")
                     and _is_js_script_type(el.attributes.get("type")))
        out = []
        for href in hrefs:
            try:
                out.append(resolve_from.resolve(href))
            except Exception:  # noqa: BLE001 - reported where it is used
                continue
        return out

    def _stylesheet_sources(self):
        """What the cascade is currently built from: every <style> element's
        text and every <link rel=stylesheet> href, in document order.

        This is the cache key for `_gather_rules`. A script that inserts a
        <style> or a <link> changes it; a script that only moves nodes around
        or edits text does not, and re-fetching every linked sheet on every
        such mutation would put a network round trip behind `el.textContent =
        x`.
        """
        return (tuple(inline_styles(self.nodes, [])),
                tuple(find_links(self.nodes, [])))

    def _gather_rules(self, url, resolve_from, prefetched=None):
        """Collect the cascade: UA + toe-injected + <style> + <link>.

        Linked sheets are fetched, so results are memoised against
        `_stylesheet_sources()` and the fetched bodies against their URL.

        `prefetched` is a `_fetch_all` map the caller already has -- the
        initial build hands over the one it started before styling began.
        Anything missing from it is fetched here, which is what a re-style
        after a script inserted a `<link>` does.
        """
        sources = self._stylesheet_sources()
        if self._last_rules is not None and sources == self._rule_sources:
            return self._last_rules

        rules = list(DEFAULT_STYLE_SHEET)
        if self.browser:
            injected = toes.extra_css(self.browser.toe_contexts, url)
            if injected:
                try:
                    rules.extend(CSSParser(injected).parse())
                except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                    pass
        for sheet in sources[0]:
            try:
                sheet = _expand_imports(sheet, resolve_from, log=self._add_error)
                rules.extend(CSSParser(sheet).parse())
            except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                pass
        # Every linked sheet we do not already hold and nobody fetched for
        # us, fetched together. The loop below is unchanged and still runs in
        # document order; all it does differently is read the body out of
        # this map.
        fetched = dict(prefetched or {})
        wanted = []
        for href in sources[1]:
            try:
                candidate = resolve_from.resolve(href)
            except Exception:  # noqa: BLE001 - the loop below reports it
                continue
            key = str(candidate)
            if key not in self._sheet_cache and key not in fetched:
                wanted.append(candidate)
        fetched.update(_fetch_all(wanted))
        for href in sources[1]:
            sheet_url = None
            try:
                sheet_url = resolve_from.resolve(href)
                key = str(sheet_url)
                if key in self._sheet_cache:
                    rules.extend(self._sheet_cache[key])
                    continue
                _h, css_body, _c = _fetched(fetched, sheet_url)
                css_body = _expand_imports(css_body, sheet_url,
                                           log=self._add_error)
                parsed = CSSParser(css_body).parse()
                self._sheet_cache[key] = parsed
                rules.extend(parsed)
            except Exception as e:  # noqa: BLE001 - skip stylesheets that fail
                self._add_error(
                    f"CSS {sheet_url or href} ({type(e).__name__})")
                continue

        self._rule_sources = sources
        return rules

    def _absolutize_media_srcs(self):
        """Resolve <img>/<video> src attributes to absolute URLs so the
        layout's cache lookup keys (absolute) always match what load_images()
        fetches. Run on the initial build and again after a JS mutation,
        which can create media elements after the first scan."""
        for node in tree_to_list(self.nodes, []):
            if isinstance(node, Element) and node.tag == "video" \
                    and not node.attributes.get("src"):
                # <video><source src=...></video>: hoist the first source we
                # could conceivably play onto the element, so everything below
                # only ever has to look at one attribute.
                picked = _first_playable_source(node)
                if picked:
                    node.attributes["src"] = picked
            if isinstance(node, Element) and node.tag in ("img", "video") \
                    and node.attributes.get("src"):
                try:
                    node.attributes["src"] = str(
                        self.base_url.resolve(node.attributes["src"]))
                except Exception:  # noqa: BLE001 - bad src renders placeholder
                    pass

    def render(self):
        self._sync_selects()
        self.document = DocumentLayout(self.nodes, WIDTH)
        self.document.image_cache = self.image_cache
        self.document.layout()
        self.document.input_boxes = self.document.collect_inputs([])
        self.repaint()
        # A relayout that only rewrapped (a resize, an image arriving) leaves
        # every node and offset meaningful, so the highlight stays on the
        # words it was on; one that replaced the text does not, and dropping
        # it here is what stops a stale highlight sitting over whatever moved
        # into those pixels.
        self.selection = self.selection_index().revalidate(self.selection)
        # Content changed: flag the repaint loop so the canvas is redrawn even
        # when no event handler happens to call draw() directly (e.g. JS DOM
        # mutations and background image completion funnel through here).
        if self.browser is not None:
            self.browser._repaint_needed = True

    def repaint(self):
        """Recompute the display list for the current scroll. Layout is
        unchanged; only the positions of `position:sticky/fixed` elements
        depend on the scroll offset, so this is cheap enough to run on every
        scroll tick (see Tab.set_scroll)."""
        if not self.document:
            return
        self.display_list = []
        self._sel_index = None
        paint_tree(self.document, self.display_list, scroll=self.scroll)

    # -- scripting ------------------------------------------------------

    def _run_scripts(self, prefetched=None):
        """Execute every <script> (inline or external) against a fresh
        interpreter bridged to the document, then restyle and re-render.

        `prefetched` is a `_fetch_all` map of sources the caller already put
        on the wire; anything absent from it is fetched here."""
        scripts = [el for el in tree_to_list(self.nodes, [])
                   if isinstance(el, Element) and el.tag == "script"
                   and _is_js_script_type(el.attributes.get("type"))]
        if not scripts:
            return
        self._js_interp = Interpreter()
        location = JSLocation(base_url=self.base_url, navigate=self._js_navigate)
        doc = JSDocument(self.nodes, base_url=self.base_url,
                         mark_dirty=self._js_mutated, interp=self._js_interp,
                         location=location)
        self._js_doc = doc
        self._js_interp.globals["document"] = doc
        # Location/navigation: the window, its aliases, and the document all
        # share one location object so `window.parent.location.replace(...)`
        # (DuckDuckGo redirects) and `location.href = ...` navigate the tab.
        self._js_interp.globals["location"] = location
        self._js_interp.globals["parent"] = self._js_interp.globals["window"]
        self._js_interp.globals["self"] = self._js_interp.globals["window"]
        self._js_interp.globals["top"] = self._js_interp.globals["window"]
        # Browser-provided host APIs (network, and nothing that draws).
        self._js_interp.globals["fetch"] = self._js_fetch
        self._js_interp.globals["XMLHttpRequest"] = self._js_xhr_ctor()
        # Rendering/event APIs. rAF rides the existing virtual-clock timer
        # machinery (setTimeout), which the GUI advances every 60ms.
        self._js_interp.globals["getComputedStyle"] = self._js_get_computed_style
        self._js_interp.globals["requestAnimationFrame"] = self._js_request_frame
        self._js_interp.globals["cancelAnimationFrame"] = self._js_cancel_frame
        self._js_interp.globals["addEventListener"] = self._js_add_listener
        self._js_interp.globals["removeEventListener"] = self._js_remove_listener
        self._js_interp.globals["matchMedia"] = self._js_match_media
        # Read-only window-ish globals scripts love to sniff.
        self._js_interp.globals["devicePixelRatio"] = 1
        self._js_interp.globals["innerWidth"] = WIDTH
        self._js_interp.globals["innerHeight"] = HEIGHT
        self._js_interp.globals["screen"] = _JSStaticProps({
            "width": WIDTH, "height": HEIGHT,
            "availWidth": WIDTH, "availHeight": HEIGHT,
        })
        self._js_interp.globals["navigator"] = _JSStaticProps({
            "userAgent": (
                f"Mozilla/5.0 FeetBrowser/{__version__} "
                "(X11; Linux) AppleWebKit/537.36 (KHTML, like Gecko)"),
            "platform": "Linux",
            "language": "en-US",
            "languages": ["en-US", "en"],
            "vendor": "",
            "onLine": True,
            "hardwareConcurrency": 4,
            "productSub": "20030107",
        })
        # The list of scripts is already fixed -- it was collected before any
        # of them ran -- so their sources can all be on the wire at once while
        # the loop below still executes them strictly in order, which is the
        # part of script semantics that actually matters.
        fetched = dict(prefetched or {})
        wanted = []
        for el in scripts:
            src = el.attributes.get("src")
            if not src:
                continue
            try:
                candidate = (self.base_url.resolve(src) if self.base_url
                             else URL(src))
            except Exception:  # noqa: BLE001 - the loop below reports it
                continue
            if str(candidate) not in fetched:
                wanted.append(candidate)
        fetched.update(_fetch_all(wanted))
        for el in scripts:
            try:
                code = None
                src = el.attributes.get("src")
                if src:
                    sheet_url = None
                    try:
                        sheet_url = self.base_url.resolve(src) \
                            if self.base_url else URL(src)
                        _h, code, _c = _fetched(fetched, sheet_url)
                        if not _is_js_script_type(_c):
                            # `file://` and error pages return an HTML body
                            # instead of raising; never execute it as JS.
                            self._add_error(
                                f"JS {sheet_url} (not a script: {_c})")
                            code = None
                    except Exception as e:  # noqa: BLE001 - skip bad/unreachable src
                        self._add_error(
                            f"JS {sheet_url or src} ({type(e).__name__})")
                        code = None
                else:
                    code = "".join(ch.text for ch in el.children
                                   if isinstance(ch, Text))
                if code:
                    self._js_interp.run(code)
            except JSException as e:
                self._js_interp.logs.append(f"JS error: {e}")
        self.js_logs.extend(self._js_interp.logs)
        self._capture_js_errors(self._js_interp.logs)
        # Run microtasks/timers the scripts scheduled (promise .then chains,
        # setTimeout(0), ...) before deciding whether anything changed.
        self._drain_js()
        # Only re-render when a script actually mutated the DOM. Most pages'
        # scripts run read-only (feature detection, counters) and forcing a
        # full restyle+layout for them dominates page-load time.
        if doc._flag["dirty"]:
            self._js_mutated()

    def _add_error(self, msg):
        self.net_errors.append(msg)
        if len(self.net_errors) > 500:
            del self.net_errors[:len(self.net_errors) - 500]

    def _capture_js_errors(self, logs):
        """Append any not-yet-scanned JS error lines to net_errors."""
        start = self._js_log_cursor
        self._js_log_cursor = len(logs)
        for line in logs[start:]:
            if line.startswith("JS error"):
                msg = line[len("JS error: "):]
                self._add_error(f"JS {msg}")

    def _js_navigate(self, target, replace):
        """Record a JS-initiated navigation (location.href/assign/replace).
        Deferred so it runs after the current script batch, never mid-script
        (which would clobber the live interpreter)."""
        try:
            url = self.base_url.resolve(str(target)) if self.base_url \
                else URL(str(target))
        except Exception:  # noqa: BLE001 - malformed URL: ignore the navigation
            return
        self._pending_nav = (url, replace)

    def _flush_pending_nav(self):
        """Navigate to the deferred target, if any. `replace` requests drop
        the current history entry instead of pushing it (location.replace /
        meta refresh / location.href assignment)."""
        nav = self._pending_nav
        if nav is None:
            return False
        self._pending_nav = None
        url, replace = nav
        self.load(url, push=not replace)
        return True

    def _check_meta_refresh(self):
        """Honor a zero-delay <meta http-equiv="refresh" content="0;url=...">
        redirect. Positive delays are left alone (they're scheduled auto-
        refreshes, not redirects) so no timer machinery is needed here, and
        anything inside <noscript> is ignored: scripting is enabled, so that
        fallback is not the one a real browser would follow."""
        for node in tree_to_list(self.nodes, []):
            if not (isinstance(node, Element) and node.tag == "meta"):
                continue
            if (node.attributes.get("http-equiv", "").lower() != "refresh"):
                continue
            inside_noscript = False
            parent = node.parent
            while parent is not None:
                if isinstance(parent, Element) and parent.tag == "noscript":
                    inside_noscript = True
                    break
                parent = parent.parent
            if inside_noscript:
                continue
            content = node.attributes.get("content", "")
            delay, _, rest = content.partition(";")
            try:
                seconds = float(delay.strip())
            except ValueError:
                seconds = 0
            if seconds > 0:
                continue
            rest = rest.strip()
            if not rest.lower().startswith("url="):
                continue
            target = rest[4:].strip().strip("'\"")
            if not target:
                continue
            try:
                url = self.base_url.resolve(target) if self.base_url \
                    else URL(target)
            except Exception:  # noqa: BLE001 - malformed target: ignore
                continue
            # Replace, like an HTTP redirect: the refresh page isn't a
            # meaningful history entry.
            self._pending_nav = (url, True)
            return

    def _js_mutated(self):
        """Re-style the tree with the stored rules and re-render after a
        script or click handler finished mutating the DOM."""
        if self.nodes is None:
            return
        # Consume the flag. The DOM bindings set it on every mutating call
        # and nothing cleared it, so the first line of script that touched
        # the document left the tab restyling and re-laying-out the whole
        # page on every 60 ms poll, for as long as it was open -- and, with
        # _fetch_js_added_images() on the end of that, re-scanning for images
        # sixteen times a second as well. Clearing it here rather than at the
        # two call sites keeps it true that this method is the only consumer;
        # a mutation made *during* the restyle sets it again and is picked up
        # on the next pass, which is what it is for.
        if self._js_doc is not None:
            self._js_doc._flag["dirty"] = False
        # style() reassigns node.style and recomputes inheritance, so JS-driven
        # overrides must be folded into the inline style attribute first (this
        # also keeps them winning over author rules on restyle).
        for node in tree_to_list(self.nodes, []):
            overrides = getattr(node, "_js_style_overrides", None)
            if not overrides:
                continue
            merged = dict(parse_inline(node.attributes.get("style", "")))
            merged.update(overrides)
            node.attributes["style"] = "; ".join(
                f"{k}: {v}" for k, v in merged.items())
        # Re-gather before restyling. A script that inserts a <style> or a
        # <link rel=stylesheet> -- which is how most component libraries ship
        # their CSS -- adds rules that are not in `_last_rules`, so restyling
        # with the stored list alone would silently ignore the new sheet.
        # `_gather_rules` returns the stored list unchanged when the set of
        # sheets has not moved, so the common mutation costs one comparison.
        if self.base_url is not None:
            self._last_rules = self._gather_rules(self.base_url, self.base_url)
        if self._last_rules is not None:
            style(self.nodes, self._last_rules)
        fresh_title = get_title(self.nodes)
        if fresh_title:
            self.title = fresh_title
        # A script may have created media elements after the first scan; make
        # their src absolute (layout keys its image cache on absolute URLs)
        # and fetch the ones we do not have yet.
        self._absolutize_media_srcs()
        self._fetch_js_added_images()
        self.render()

    # -- JS host APIs (fetch, XMLHttpRequest) ------------------------------

    def _js_scheme_allowed(self, target):
        """Which schemes page-context JS may fetch. Remote pages may only
        reach http(s)/data; `file:` is reserved for pages that were
        themselves loaded from a local file, and even then only within the
        page's own directory; a hostile local HTML file cannot wander the
        whole filesystem. Everything else (toehub:, toe:, ...) is off-limits
        so a hostile site cannot read arbitrary local files and exfiltrate
        them."""
        if target.scheme in ("http", "https", "data"):
            return True
        if target.scheme == "file":
            base = self.base_url
            if base is None or base.scheme != "file":
                return False
            # URL space, not os.path: a file: URL's path is slash-separated
            # whatever the platform, and os.path.dirname would hand back a
            # backslash-separated prefix on Windows that never matches.
            base_path = base.path
            base_dir = base_path if base_path.endswith("/") \
                else base_path.rpartition("/")[0] + "/"
            return target.path.startswith(base_dir)
        return False

    def _js_fetch(self, url, options=UNDEFINED):
        """Host `fetch()`: resolve relative to the document, fetch on a
        background thread, and settle the returned Promise on the UI thread."""
        interp = self._js_interp
        promise = interp.create_promise()
        try:
            target = self.base_url.resolve(str(url)) if self.base_url \
                else URL(str(url))
        except Exception as e:  # noqa: BLE001 - malformed URL
            promise.reject(str(e))
            return promise
        if not self._js_scheme_allowed(target):
            promise.reject(
                f"Blocked fetch of '{target.scheme}:' scheme from a page")
            return promise

        def worker():
            try:
                headers, body, ctype = target.request()
                err = None
                status = 200
            except Exception as e:  # noqa: BLE001 - network failure
                headers, body, ctype, status, err = {}, "", "text/plain", 0, str(e)
            self._js_fetch_results.append((promise, headers, body, ctype,
                                           status, err))

        threading.Thread(target=worker, daemon=True).start()
        return promise

    def _js_xhr_ctor(self):
        return _JSXHRCtor(self)

    def _js_get_computed_style(self, el, *rest):
        """Host `getComputedStyle(el)`: wraps the node so property reads and
        `getPropertyValue()` resolve against the cascaded `.style` dict."""
        if hasattr(el, "js_unwrap"):
            el = el.js_unwrap()
        if el is UNDEFINED or el is None or not hasattr(el, "node"):
            return _JSComputedStyle(None)
        return _JSComputedStyle(el.node)

    def _js_request_frame(self, cb, *rest):
        """rAF rides the virtual-clock timer machinery so GUI polling (which
        calls interp.advance every 60ms) fires the callback on schedule."""
        try:
            return self._js_interp.call(
                self._js_interp.globals["setTimeout"], cb, 16)
        except Exception:  # noqa: BLE001 - no timer infra in headless mode
            return 0

    def _js_cancel_frame(self, handle, *rest):
        try:
            return self._js_interp.call(
                self._js_interp.globals["clearTimeout"], handle)
        except Exception:  # noqa: BLE001 - no timer infra in headless mode
            return None

    def _js_add_listener(self, *args):
        return None

    def _js_remove_listener(self, *args):
        return None

    def _js_match_media(self, query):
        return _JSStaticProps({
            "matches": False,
            "media": str(query),
            "addListener": lambda *a: None,
            "removeListener": lambda *a: None,
            "addEventListener": lambda *a: None,
        })

    def _drain_js(self):
        """UI thread: settle JS network results and run pending microtasks /
        due timers, re-rendering if any handler mutated the DOM."""
        interp = self._js_interp
        if interp is None:
            return
        while self._js_fetch_results:
            promise, headers, body, ctype, status, err = \
                self._js_fetch_results.popleft()
            if err:
                promise.reject(err)
            else:
                promise.resolve(JSResponse(interp, headers, body, ctype,
                                           status))
        while self._js_xhr_results:
            xhr, headers, body, ctype, status, err = self._js_xhr_results.popleft()
            xhr._finish(headers, body, status, err)
        try:
            interp.drain()
        except JSException as e:
            self.js_logs.append(f"JS error: {e}")
            self._add_error(f"JS {e}")
        if self._js_doc is not None and self._js_doc._flag["dirty"]:
            self._js_mutated()

    def _dispatch_js_click(self, node):
        return self._dispatch_js_event(node, "click")

    def _dispatch_js_event(self, node, event_type):
        """Run handlers for `event_type` (addEventListener + the matching
        on<type> attribute) registered on `node` or any ancestor, bubbling
        outwards. Returns True if any handler attempted to run."""
        interp = self._js_interp
        if interp is None:
            return False
        attr_name = "on" + event_type
        handled = False
        cur = node
        while cur is not None:
            if isinstance(cur, Element):
                handlers = getattr(cur, "_js_handlers", None)
                if handlers:
                    for fn in handlers.get(event_type, []):
                        try:
                            interp.call(fn)
                        except JSException as e:
                            interp.logs.append(f"JS error: {e}")
                        handled = True
                inline = cur.attributes.get(attr_name)
                if inline:
                    try:
                        interp.run(inline)
                    except JSException as e:
                        interp.logs.append(f"JS error: {e}")
                    handled = True
            cur = cur.parent
        if handled:
            self.js_logs.extend(interp.logs)
            self._capture_js_errors(interp.logs)
            self._drain_js()
            self._js_mutated()
            self._flush_pending_nav()
            return True
        return False

    # -- images ----------------------------------------------------------

    def load_images(self, root=None, done=None):
        """Collect <img> sources missing from the cache and fetch them
        asynchronously (off the UI thread), re-rendering as each arrives."""
        self._image_root = root
        self._image_done = done
        # Re-entrant: a script may already have queued fetches
        # (_fetch_js_added_images) before the load path calls this. Keep them
        # in flight rather than dropping them (settle() would think the page
        # was done) or starting their threads again (the same URL fetched
        # twice) -- only the genuinely new sources get threads here.
        in_flight = dict(self._image_queue)
        new = list(self._missing_images(in_flight))
        self._image_queue = list(in_flight.items()) + new
        if not self._image_queue:
            if done:
                done()
            return
        if root is None:
            # No UI loop (tests / headless): fetch and decode synchronously so
            # results are available immediately and deterministically.
            for key, url in new:
                try:
                    _headers, data, ctype = url.request_bytes()
                except Exception:  # noqa: BLE001 - keep placeholder on failure
                    data, ctype = None, None
                self._decode_and_finish(key, data, ctype)
            return
        for key, url in new:
            threading.Thread(
                target=self._fetch_image, args=(key, url), daemon=True).start()

    def _missing_images(self, skip=()):
        """Yield ``(key, url)`` for every <img> source that is neither decoded
        nor on its way: not in the image cache, not already found undecodable,
        and not in `skip` (keys already being fetched). Shared by the initial
        scan (load_images) and the re-scan after a script adds elements
        (_fetch_js_added_images)."""
        skip = set(skip) | set(self._image_undecodable)
        if self.nodes is None:
            return
        for node in tree_to_list(self.nodes, []):
            if not (isinstance(node, Element) and node.tag == "img"):
                continue
            src = node.attributes.get("src")
            if not src:
                continue
            try:
                url = self.base_url.resolve(src) if self.base_url else URL(src)
            except Exception:  # noqa: BLE001 - bad src shouldn't kill the page
                continue
            key = str(url)
            if key in self.image_cache or key in skip:
                continue
            skip.add(key)
            yield key, url

    def _fetch_js_added_images(self):
        """Fetch <img> sources a script created after the page's first scan.

        Scripts build their own content (a banner strip, say) by creating
        <img> elements after load_images() has already run, and those must be
        fetched like any other. Sources already cached or already being
        fetched are skipped; new ones join _image_queue so pending_images()
        and settle() keep accounting for them, and the same synchronous /
        threaded split as load_images() applies."""
        in_flight = {key for key, _ in self._image_queue}
        queued = list(self._missing_images(in_flight))
        if not queued:
            return
        for key, url in queued:
            self._image_queue.append((key, url))
        if not self._gui_mode():
            for key, _url in queued:
                try:
                    _headers, data, ctype = _url.request_bytes()
                except Exception:  # noqa: BLE001 - keep placeholder on failure
                    data, ctype = None, None
                self._decode_and_finish(key, data, ctype)
            return
        for key, url in queued:
            threading.Thread(
                target=self._fetch_image, args=(key, url), daemon=True).start()

    def tick_images(self):
        """Move every animated GIF on to the frame that is due now.

        Returns True when something on screen changed, exactly like
        `tick_videos`, and is called from the same frame timer -- an animated
        GIF is a video that happens to arrive through `<img>`, and giving it
        its own clock would mean two timers disagreeing about what "now" is.

        Deliberately not part of `busy()`: a GIF looping for ever is the
        normal case, and a browser that called that "still working" would
        never let `settle()` return.
        """
        now = time.monotonic()
        changed = False
        for photo in self.image_cache.values():
            if getattr(photo, "animated", False) and photo.advance(now):
                changed = True
        return changed

    def pending_images(self):
        """True while image fetches started by load_images() are outstanding.

        A tab whose document has arrived is not finished: `loading` goes
        false the moment the HTML is in, and only then does load_images()
        start fetching what the page points at. Entries leave the queue in
        _drain_images(), on the UI thread, at the moment decoded pixels reach
        the image cache -- so waiting on this is waiting for the render that
        stops drawing "[img]" placeholders.
        """
        return bool(self._image_queue)

    def _fetch_image(self, key, url):
        """Background thread: fetch bytes, hand them back to the UI thread via
        the results queue. Never touches the canvas directly. The semaphore bounds
        how many image fetches run at once browser-wide."""
        try:
            with _image_fetch_sem:
                _headers, data, ctype = url.request_bytes()
        except Exception as e:  # noqa: BLE001 - failed image fetch: keep placeholder
            data, ctype = None, None
            self._image_failures.append(f"{url} ({type(e).__name__})")
        self._image_results.append((key, data, ctype))

    def _drain_images(self):
        """Called on the UI thread: decode any finished downloads and
        re-render when the last one arrives."""
        while self._image_failures:
            url = self._image_failures.popleft()
            self._add_error(f"IMG {url}")
        if not self._image_results:
            return
        pending = []
        try:
            while True:
                pending.append(self._image_results.popleft())
        except IndexError:
            pass
        for key, data, ctype in pending:
            self._decode_and_finish(key, data, ctype)

    def _decode_and_finish(self, key, data, ctype):
        photo = self._decode_image(data, ctype) if data else None
        if photo is not None:
            self.image_cache[key] = photo
            # Bound the per-tab image cache so long browsing sessions
            # cannot grow it (and the X/PhotoImage resources behind it)
            # without limit. Dict preserves insertion order: drop oldest.
            while len(self.image_cache) > MAX_CACHED_IMAGES:
                self.image_cache.pop(next(iter(self.image_cache)))
        else:
            # This source is settled too -- it just settled on a placeholder.
            # Recording that is what stops the next re-scan (a script mutates,
            # so _fetch_js_added_images runs again) seeing an <img> that is in
            # neither the cache nor the queue and fetching it all over again,
            # for ever. Same ceiling as the cache, for the same reason.
            self._image_undecodable[key] = True
            while len(self._image_undecodable) > MAX_CACHED_IMAGES:
                self._image_undecodable.pop(
                    next(iter(self._image_undecodable)))
        # Remove this URL (not necessarily the head); background threads
        # finish in arbitrary order, so popping the head would reorder the
        # remaining queue and skip images.
        self._image_queue = [q for q in self._image_queue if q[0] != key]
        if self._image_queue:
            return  # still waiting on the remaining threads
        self.render()
        if self._image_done:
            self._image_done()

    # -- video ----------------------------------------------------------

    def load_videos(self, root=None):
        """Fetch every `<video src>` the page names and give each element a
        player of its own.

        Two decisions worth naming. It is a separate queue from
        load_images(): the two produce different things (a decoded picture
        against a live player with a decode thread behind it), fail
        differently, and a video that never arrives must not hold up the
        "images are in, stop drawing placeholders" signal that
        pending_images() drives.

        And the player belongs to the *element*, not to the URL. Two
        `<video>` tags pointing at one file are two independent playheads --
        one can be paused at 3s while the other plays, and each scales its
        own frames to its own box. The bytes are still fetched once.
        """
        self._video_results = deque()
        self._video_queue = []
        if self.nodes is None:
            return
        by_src = {}
        for node in tree_to_list(self.nodes, []):
            if not (isinstance(node, Element) and node.tag == "video"):
                continue
            src = node.attributes.get("src")
            if src:
                by_src.setdefault(src, []).append(node)
        if not by_src:
            return
        self._video_queue = list(by_src)
        self._video_nodes = by_src
        if root is None:
            for key in list(by_src):
                try:
                    _headers, data, _ctype = URL(key).request_bytes()
                except Exception:  # noqa: BLE001 - a bad URL shows the box
                    data = None
                self._finish_video(key, data)
            return
        for key in by_src:
            threading.Thread(target=self._fetch_video, args=(key,),
                             daemon=True).start()

    def _fetch_video(self, key):
        """Background thread: the bytes, and the decode they imply. Nothing
        here touches the canvas, the DOM or an audio device."""
        try:
            with _image_fetch_sem:
                _headers, data, _ctype = URL(key).request_bytes()
        except Exception as exc:  # noqa: BLE001 - reported on the UI thread
            self._video_failures.append((key, str(exc)))
            self._video_results.append((key, None, None))
            return
        self._video_results.append((key, data, self._build_players(key, data)))

    def _build_players(self, key, data):
        """One ready player per element that named this URL, first frame
        already decoded.

        This used to happen in `_finish_video`, on the UI thread, and it is
        not cheap: opening the container walks the whole sample table, and
        `first_frame` decodes a keyframe in Python. Six autoplaying clips --
        which is what discord.com's front page is -- froze the window for
        about four seconds between one timer tick and the next.

        Nothing here needs the UI thread. What does -- attaching a sound
        device, publishing the player on the node, starting playback -- stays
        in `_finish_video`, which finds the work already done and says so by
        `first_frame` returning False the second time.

        A player that would not build is stored as its exception, so the
        failure is still reported from the thread that reports failures.
        """
        built = []
        for node in self._video_nodes.get(key, ()):
            try:
                # `loop` is per element, not per file: the same clip can be a
                # looping background in one place on the page and a thing you
                # watch once in another.
                player = media.VideoPlayer(
                    data=data, loop="loop" in node.attributes)
            except Exception as exc:  # noqa: BLE001 - a page must not die
                built.append(exc)
                continue
            player.first_frame()
            built.append(player)
        return built

    def _drain_videos(self):
        """UI thread: build players for whatever finished downloading."""
        while self._video_failures:
            key, _why = self._video_failures.popleft()
            self._add_error(f"VIDEO {key}")
        if not self._video_results:
            return
        arrived = []
        try:
            while True:
                arrived.append(self._video_results.popleft())
        except IndexError:
            pass
        for key, data, players in arrived:
            self._finish_video(key, data, players)
        self.render()

    def _finish_video(self, key, data, players=None):
        """Attach a player to every element that named this URL.

        `players` is what `_build_players` produced on the fetch thread. The
        synchronous path (`load_videos` with no event loop to hand the work
        to) passes None and builds them here instead, which is the same work
        on the only thread there is.
        """
        if key in self._video_queue:
            self._video_queue.remove(key)
        nodes = self._video_nodes.get(key, ())
        if not data:
            return
        if players is None:
            players = self._build_players(key, data)
        for node, player in zip(nodes, players):
            if isinstance(player, BaseException):
                self._add_error(f"VIDEO {key}: {player}")
                return
            # Show frame zero straight away. A paused <video> displaying its
            # own first frame is what a browser does, and it is also the
            # cheapest proof that the file really decoded.
            # Sound, if the file has any. `attach_audio` is what makes the
            # pictures follow the soundtrack rather than the wall clock, and
            # it declines -- leaving the video exactly as it was -- when
            # there is no audio track or no device that can be heard.
            self._attach_video_audio(key, node, data, player)
            # A no-op when the fetch thread got there first; the decode when
            # it did not.
            player.first_frame()
            node.video_player = player
            self.video_players.append(player)
            if "autoplay" in node.attributes and player.track is not None:
                player.play()
                if self.browser is not None:
                    self.browser._ensure_video_tick()

    def _attach_video_audio(self, key, node, data, player):
        """Give a freshly built `VideoPlayer` its soundtrack, if it has one.

        Silence is never a failure here. A file with no audio track, and a
        machine with no sound card, are both videos that play exactly as
        they did before this method existed; the only thing worth saying out
        loud is a track the container names and we cannot decode, which is a
        page the user can see is missing something.
        """
        try:
            audio = arch.AudioPlayer(
                data=data, loop="loop" in node.attributes)
        except Exception as exc:  # noqa: BLE001 - a page must not die
            self._add_error(f"AUDIO {key}: {exc}")
            return None
        info = audio.info
        if not audio.playable and info is not None and info.codec:
            self._add_error(f"AUDIO {key}: {audio.error or info.reason}")
        if "muted" in node.attributes:
            audio.muted = True
        if not player.attach_audio(audio):
            audio.close()
            return None
        node.audio_player = audio
        self.audio_players.append(audio)
        return audio

    def pending_videos(self):
        return bool(self._video_queue)

    def tick_videos(self):
        """Advance every player to the frame that is due. Returns True when
        anything on screen changed. Called from the browser's frame timer;
        it decodes nothing itself and never blocks."""
        changed = False
        for player in self.video_players:
            if player.tick():
                changed = True
        return changed

    def playing_videos(self):
        return any(p.playing for p in self.video_players)

    def stop_videos(self):
        """Drop every player and its decode thread. Called when the tab
        navigates away or closes -- a daemon thread still decoding a film
        nobody is watching is a leak with a picture on it."""
        for player in self.video_players:
            player.close()
        for audio in self.audio_players:
            audio.close()
        self.video_players = []
        self.audio_players = []
        self._video_queue = []
        self._video_nodes = {}

    @staticmethod
    def _enclosing_video(node):
        while node is not None:
            if isinstance(node, Element) and node.tag == "video":
                return node
            node = node.parent
        return None

    def _toggle_video(self, node):
        """Play/pause the player behind a `<video>`. True if we handled it."""
        player = getattr(node, "video_player", None)
        if player is None or player.track is None:
            return False
        player.toggle()
        self._after_transport(player)
        return True

    def _after_transport(self, player):
        """What every transport control does once it has done its own bit:
        make sure the frame timer is running, say where we are, and ask for a
        repaint. The scrubber has to move even while paused, so the repaint
        is not conditional on playing."""
        if self.browser is not None:
            self.browser._ensure_video_tick()
            self.browser._repaint_needed = True
        self.status = player.status()

    def _video_controls_at(self, x, y):
        """The transport bar under a point in document space, or None.

        Scanned in paint order like `_node_at`, and before it, because the
        bar sits over the picture: a click on the play button must not also
        read as a click on the film behind it and toggle twice.
        """
        for cmd in reversed(self.display_list):
            if isinstance(cmd, DrawVideoControls) and cmd.hit(x, y):
                return cmd
        return None

    def _activate_video_controls(self, bar, x, y):
        """Act on a click inside a transport bar. Always True: the bar
        swallows clicks that land on nothing in particular rather than
        letting them fall through and pause the film."""
        action = bar.action_at(x, y)
        player = bar.player
        if action is None or player is None:
            return True
        what, value = action
        if what == "toggle":
            player.toggle()
        elif what == "seek":
            player.seek(value)
        self._after_transport(player)
        return True

    @staticmethod
    def _decode_image(data, _ctype):
        """Decode image bytes to a PhotoImage, or None for the placeholder.

        The content type is not consulted. Servers label images wrongly often
        enough that the bytes are the only reliable answer, and `imagecodec`
        sniffs them: what it recognises it decodes, and everything else --
        SVG, WebP, BMP, ICO, TIFF, and the corners of JPEG we refuse -- draws
        as the alt text, which is what the caller does with None.
        """
        try:
            return PhotoImage(data=data)
        except Exception:  # noqa: BLE001 - undecodable data -> placeholder
            return None

    def content_height(self):
        return self.document.height if self.document else 0

    def go_back(self):
        if not self.history:
            return
        self.future.append((self.url, self.scroll))
        url, scroll = self.history.pop()
        self.load(url, push=False, pending_scroll=scroll)

    def go_forward(self):
        if not self.future:
            return
        self.history.append((self.url, self.scroll))
        url, scroll = self.future.pop()
        self.load(url, push=False, pending_scroll=scroll)

    # -- interaction -----------------------------------------------------

    def scroll_by(self, delta):
        self.set_scroll(self.scroll + delta)

    def set_scroll(self, value):
        """Change the scroll offset. In the GUI the repaint is coalesced to
        the next frame (latest-wins), so a fast scrollbar drag pays one full
        redraw per frame rather than one per mouse-move event. Headless
        callers (tests) and popup tabs repaint synchronously for
        determinism."""
        self.scroll = value
        self._clamp_scroll()
        if self._gui_mode() and (self is self.browser.active_tab
                                 or self in self.browser.tabs):
            self.browser._schedule_scroll_repaint()
        else:
            self.repaint()

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
        """Return the DOM node under (x, y), first checking form controls."""
        y += self.scroll
        if self.document:
            for lx, ty, rx, by, node in self.document.input_boxes:
                if lx <= x < rx and ty <= y < by:
                    return node
        # The display list is in paint order, so the topmost command under a
        # point is the *last* match. Scanning in reverse returns the same node
        # as the old full scan but exits on the first hit, which matters on
        # text-heavy pages where this runs on every mouse-move for hover.
        for cmd in reversed(self.display_list):
            if getattr(cmd, "node", None) is not None and hasattr(cmd, "hit") \
                    and cmd.hit(x, y):
                return cmd.node
        return None

    @staticmethod
    def _enclosing_link(node):
        while node:
            if isinstance(node, Element) and node.tag == "a" \
                    and "href" in node.attributes:
                return node.attributes["href"]
            node = node.parent
        return None

    def click(self, x, y):
        """Handle a click at document coords.

        Returns a URL to load, a FormAction (form submit), or None.
        """
        bar = self._video_controls_at(x, y + self.scroll)
        if bar is not None:
            self._activate_video_controls(bar, x, y + self.scroll)
            return None
        node = self._node_at(x, y)
        video = self._enclosing_video(node)
        if video is not None and self._toggle_video(video):
            # A click on the picture itself is play/pause, with or without a
            # control bar -- which is what every browser does and the only
            # transport a `<video>` without `controls` has.
            return None
        control = self._hit_control(node)
        if control is not None:
            result = self._activate_control(control, x, y + self.scroll)
        else:
            href = self._enclosing_link(node)
            if not href:
                result = None
            elif href.startswith(("javascript:", "mailto:", "tel:")):
                self.status = href
                result = None
            else:
                result = self.base_url.resolve(href) if self.base_url \
                    else self.url.resolve(href)
        # A JS click handler (if any) consumes the click and cancels navigation.
        if node is not None and self._dispatch_js_click(node):
            # A drop-down still drops down: the handler ran, but there is no
            # navigation for it to have cancelled.
            return result if isinstance(result, SelectAction) else None
        return result

    def link_at(self, x, y):
        """Return href under the cursor for hover feedback, else None."""
        return self._enclosing_link(self._node_at(x, y))

    # -- text selection --------------------------------------------------

    def selection_index(self):
        """The selectable text of the current display list, in document order.

        Rebuilt whenever the display list is (see `repaint`), which is what
        keeps the highlight glued to the words rather than to the screen: the
        positions in `self.selection` are node offsets, and each rebuild
        resolves them against wherever those words have just been painted.
        """
        if self._sel_index is None:
            self._sel_index = SelectionIndex(self.display_list)
        return self._sel_index

    def start_selection(self, x, y, granularity="char"):
        """Begin (or reset) a selection at viewport coords (x, y).

        `granularity` is "char" for a press, "word" for a double-click and
        "line" for a triple-click; a drag after a multi-click keeps extending
        in the same unit.
        """
        index = self.selection_index()
        doc_y = y + self.scroll
        if granularity == "word":
            self.selection = index.word_around(x, doc_y) or \
                self._collapsed_at(index, x, doc_y)
            return
        if granularity == "line":
            self.selection = index.line_around(x, doc_y) or \
                self._collapsed_at(index, x, doc_y)
            return
        self.selection = self._collapsed_at(index, x, doc_y)

    @staticmethod
    def _collapsed_at(index, x, doc_y):
        point = index.point_at(x, doc_y)
        return Selection(point) if point is not None else None

    def extend_selection(self, x, y):
        """Extend the selection to viewport coords (x, y)."""
        if self.selection is None:
            self.start_selection(x, y)
            return
        self.selection = self.selection_index().extend(
            self.selection, x, y + self.scroll)

    def _selection_spans(self):
        """Selected character ranges as (run, start_char, end_char) tuples,
        in document order, or [] when nothing is selected."""
        if self.selection is None:
            return []
        return self.selection_index().spans(self.selection)

    def selected_text(self):
        """The selected text, as drawn, for clipboard copying."""
        if self.selection is None:
            return ""
        return self.selection_index().text(self.selection)

    # -- forms -----------------------------------------------------------

    @staticmethod
    def _hit_control(node):
        while node is not None:
            if isinstance(node, Element) and \
                    node.tag in ("input", "button", "textarea", "select"):
                return node
            node = node.parent
        return None

    @staticmethod
    def _enclosing_form(node):
        while node is not None:
            if isinstance(node, Element) and node.tag == "form":
                return node
            node = node.parent
        return None

    def _activate_control(self, control, px=0, py=0):
        """React to a click on a form control. (px, py) are the click in page
        coordinates, which only an expanded <select> needs -- it is one hit
        box holding many rows, so it has to know where inside it the click
        landed."""
        itype = control.attributes.get("type", "").lower() \
            if control.tag == "input" else ""
        if itype in ("checkbox", "radio"):
            checked = field_checked(control)
            if itype == "radio" and not checked:
                # A radio group is one field: ticking a button unticks the
                # rest of its name group, or the submission carries every
                # button the user has ever clicked.
                self._clear_radio_group(control)
            control.attributes["data-checked"] = "off" if checked else "on"
            self.render()
            return None
        if itype == "reset":
            form = self._enclosing_form(control)
            if form:
                self.reset_form(form)
            return None
        is_submit = (control.tag == "button" or itype in ("submit", "image"))
        if is_submit:
            form = self._enclosing_form(control)
            if form:
                return self._submit_form(form, control)
            return None
        if control.tag == "select":
            if "disabled" in control.attributes:
                return None
            if listbox_rows(control):
                return self._click_listbox(control, px, py)
            rect = self._control_rect(control)
            if rect is None:
                return None
            self._focus(control)
            return SelectAction(control, rect)
        if itype == "range":
            # A range is grabbed by _press_range on the browser, which owns
            # the drag gesture; a bare click without a press flow does not
            # arrive here. Nothing to focus, nothing to submit.
            return None
        # Focusable field.
        self._focus(control)
        return None

    def _focus(self, control):
        """Move form focus to `control`. The `data-focused` marker is what the
        painter reads to draw the focus ring, so moving it means a re-render."""
        if self.focused_input is not None:
            self.focused_input.attributes.pop("data-focused", None)
        self.focused_input = control
        control.attributes["data-focused"] = ""
        self.render()

    def _control_rect(self, node):
        """The laid-out box of a form control in page coordinates, or None
        when it is not in the current layout."""
        if not self.document:
            return None
        for lx, ty, rx, by, other in self.document.input_boxes:
            if other is node:
                return (lx, ty, rx, by)
        return None

    def _range_rect_at(self, x, y):
        """(node, lx, ty, rx, by) for a range input whose box covers (x, y),
        in page coordinates, or None when the point is not on a range."""
        if not self.document:
            return None
        for lx, ty, rx, by, other in self.document.input_boxes:
            if other.tag != "input":
                continue
            if (other.attributes.get("type", "text").lower() != "range"):
                continue
            if lx <= x <= rx and ty <= y <= by:
                return (other, lx, ty, rx, by)
        return None

    def _clear_radio_group(self, radio):
        """Untick every other radio sharing this one's name within its form."""
        name = radio.attributes.get("name")
        if not name:
            return
        scope = self._enclosing_form(radio) or self.nodes
        for node in tree_to_list(scope, []):
            if isinstance(node, Element) and node is not radio \
                    and node.tag == "input" \
                    and node.attributes.get("type", "").lower() == "radio" \
                    and node.attributes.get("name") == name:
                node.attributes["data-checked"] = "off"

    def reset_form(self, form):
        for node in tree_to_list(form, []):
            if not isinstance(node, Element):
                continue
            if node.tag == "input":
                itype = node.attributes.get("type", "text").lower()
                if itype in ("checkbox", "radio"):
                    # Drop the recorded state so the markup's own `checked`
                    # attribute is what decides again.
                    node.attributes.pop("data-checked", None)
                elif itype not in ("submit", "button", "reset", "image"):
                    node.attributes["value"] = ""
            elif node.tag == "textarea":
                # Dropping `value` restores the markup's own content.
                node.attributes.pop("value", None)
            elif node.tag == "select":
                # Back to the markup's own choice: whatever the author marked
                # `selected`, or the first option when they marked none.
                for opt, _group in select_options(node):
                    if "data-selected" in opt.attributes:
                        opt.attributes["selected"] = ""
                    else:
                        opt.attributes.pop("selected", None)
                node.attributes.pop("value", None)
                # An expanded one goes back to the top with the keyboard on
                # whatever the markup chose, the same as a freshly loaded page.
                node.attributes.pop("data-active", None)
                node.attributes.pop("data-scroll", None)
        self.render()

    def choose_option(self, select, option):
        """Commit a drop-down choice into the DOM and tell the page about it.

        The `selected` attribute is the document's own record of the choice,
        so moving it is what makes `select.value` read right from JavaScript
        and what a form submission later finds. `change` fires only when the
        choice actually moved, as it does in a real browser.
        """
        options = [opt for opt, _group in select_options(select)]
        if option not in options or "disabled" in option.attributes:
            return False
        if "multiple" in select.attributes:
            changed = True
            if "selected" in option.attributes:
                del option.attributes["selected"]
            else:
                option.attributes["selected"] = ""
        else:
            changed = "selected" not in option.attributes
            for opt in options:
                opt.attributes.pop("selected", None)
            option.attributes["selected"] = ""
        # The selection just moved, so it -- not the mirrored attribute a
        # script may have left behind -- is the truth to sync from.
        select.attributes.pop("value", None)
        self.render()
        if changed:
            self._dispatch_js_event(select, "change")
        return changed

    # -- expanded <select> (size / multiple) ------------------------------
    #
    # An expanded <select> is one hit box with rows inside it rather than a
    # control with a list behind it, so everything here is arithmetic on that
    # box. Which row the keyboard is on, and how far a list too long for the
    # box has been scrolled, are kept on the node itself -- layout is rebuilt
    # from the DOM on every render, so anything the painter has to read has
    # to survive there.

    def listbox_at(self, x, y):
        """The expanded <select> at page coords (x, y), or None."""
        if not self.document:
            return None
        for lx, ty, rx, by, node in self.document.input_boxes:
            if isinstance(node, Element) and node.tag == "select" \
                    and listbox_rows(node) and lx <= x <= rx and ty <= y <= by:
                return node
        return None

    def listbox_row_at(self, node, x, y):
        """Index of the row of an expanded <select> under page coords (x, y).

        -1 for a point outside the box, past the last row, or on a heading or
        a disabled option -- everywhere, that is, that a click cannot choose.
        """
        rect = self._control_rect(node)
        if rect is None:
            return -1
        lx, ty, rx, by = rect
        if not (lx <= x <= rx and ty <= y <= by):
            return -1
        rows = select_rows(node)
        top = listbox_scroll(node, len(rows))
        i = top + int((y - ty - LISTBOX_PAD) // LISTBOX_ROW_H)
        if top <= i < min(len(rows), top + listbox_rows(node)) \
                and rows[i].enabled:
            return i
        return -1

    def _click_listbox(self, node, x, y):
        """A click inside an expanded <select>: take the row under it.

        Nothing drops down here -- the options are already on the page -- so
        the click goes straight to the DOM. Clicking the box but missing a
        usable row still focuses it, which is what gives the keyboard
        somewhere to start.
        """
        i = self.listbox_row_at(node, x, y)
        rows = select_rows(node)
        if i >= 0:
            node.attributes["data-active"] = str(i)
        self._focus(node)
        if i >= 0:
            self.choose_option(node, rows[i].option)
        return None

    def move_listbox(self, node, delta, to_end=False, last=False):
        """Walk the keyboard row of an expanded <select>.

        A single-choice listbox takes each row as the cursor passes over it:
        that is what a real one does, and with no Enter to confirm with it is
        the only way an arrow can change the value at all. A `multiple` one
        only moves, because taking every row walked over would select the
        lot; there, Space is what commits.

        The ends do not wrap. In a drop-down wrapping costs nothing, but here
        it would mean Down at the bottom of the list silently changing the
        value to the first option.
        """
        rows = select_rows(node)
        if not rows:
            return
        n = len(rows)
        if to_end:
            order = range(n - 1, -1, -1) if last else range(n)
            i = next((k for k in order if rows[k].enabled), -1)
        else:
            i, j = -1, listbox_active(node, rows)
            while True:
                j += delta
                if not 0 <= j < n:
                    break
                if rows[j].enabled:
                    i = j
                    break
        if i < 0:
            return
        node.attributes["data-active"] = str(i)
        self._listbox_reveal(node, i, n)
        if "multiple" in node.attributes:
            self.render()
        else:
            self.choose_option(node, rows[i].option)

    def toggle_listbox_active(self, node):
        """Take (or drop) the row the keyboard is on. Multi-choice only: a
        single-choice listbox has already taken it on the way past."""
        rows = select_rows(node)
        i = listbox_active(node, rows)
        if 0 <= i < len(rows) and rows[i].enabled:
            self.choose_option(node, rows[i].option)

    def scroll_listbox(self, node, steps):
        """Scroll an expanded <select> by whole rows.

        False when there is nothing left to scroll, which lets the caller
        hand the turn back to the page rather than swallowing it.
        """
        nrows = len(select_rows(node))
        visible = listbox_rows(node)
        if nrows <= visible:
            return False
        top = listbox_scroll(node, nrows)
        new = max(0, min(top + steps, nrows - visible))
        if new == top:
            return False
        node.attributes["data-scroll"] = str(new)
        self.render()
        return True

    def _listbox_reveal(self, node, i, nrows):
        """Scroll row `i` of an expanded <select> into its box."""
        visible = listbox_rows(node)
        top = listbox_scroll(node, nrows)
        if i < top:
            top = i
        elif i >= top + visible:
            top = i - visible + 1
        node.attributes["data-scroll"] = str(
            max(0, min(top, max(0, nrows - visible))))

    def _sync_selects(self):
        """Keep every <select>'s `value` attribute and its selected <option>
        agreeing, in whichever direction moved last.

        Two parties read the choice and they read it in different places.
        JavaScript asks for `.value`, which the DOM bridge answers from the
        attribute dictionary; the painter and the form submitter ask which
        option carries `selected`. Rather than teach the bridge about
        <select>, the value attribute is kept as a mirror of the selection --
        and when a script writes it, the mirror is believed and the selection
        follows it.
        """
        if self.nodes is None:
            return
        for node in tree_to_list(self.nodes, []):
            if not isinstance(node, Element) or node.tag != "select":
                continue
            options = [opt for opt, _group in select_options(node)]
            if not options:
                continue
            if "data-selected" not in node.attributes:
                # Remember the markup's own choice once, so resetting the
                # form can put it back after the reader has moved it.
                for opt in options:
                    if "selected" in opt.attributes:
                        opt.attributes["data-selected"] = ""
                node.attributes["data-selected"] = ""
            chosen = selected_options(node)
            current = option_value(chosen[0]) if chosen else ""
            wanted = node.attributes.get("value")
            if wanted is not None and wanted != current and \
                    "multiple" not in node.attributes:
                match = next((opt for opt in options
                              if option_value(opt) == wanted), None)
                if match is not None:
                    for opt in options:
                        opt.attributes.pop("selected", None)
                    match.attributes["selected"] = ""
                    current = wanted
            node.attributes["value"] = current

    def blur_input(self):
        if self.focused_input is not None:
            self.focused_input.attributes.pop("data-focused", None)
            self.focused_input = None
            self.render()

    def type_char(self, ch):
        return self.insert_text(ch)

    def insert_text(self, text):
        """Append `text` to the focused field. Returns True if it landed
        somewhere, so the caller knows whether a repaint is due."""
        inp = self.focused_input
        if inp is None or not text:
            return False
        if inp.tag not in ("input", "textarea"):
            # A select has a focus ring but no text to edit, and its `value`
            # attribute mirrors the chosen option -- typing must not touch it.
            return False
        if inp.tag == "input":
            if inp.attributes.get("type", "text").lower() in ("checkbox",
                                                              "radio"):
                return False
            # A single-line field has nowhere to put a line break, so a
            # pasted multi-line block folds onto one line rather than
            # silently losing everything after the first newline.
            text = " ".join(text.splitlines())
        inp.attributes["value"] = field_value(inp) + text
        self.render()
        return True

    def delete_char(self):
        inp = self.focused_input
        if inp is None or inp.tag == "select":
            return False
        inp.attributes["value"] = field_value(inp)[:-1]
        self.render()
        return True

    def submit_focused(self):
        inp = self.focused_input
        if inp is None:
            return None
        form = self._enclosing_form(inp)
        if form:
            return self._submit_form(form)
        return None

    def _submit_form(self, form, submitter=None):
        method = form.attributes.get("method", "get").lower()
        action = form.attributes.get("action", "")
        base = (self.base_url if self.base_url else self.url)
        if action:
            url = base.resolve(action)
        else:
            url = URL(str(self.url).split("#", 1)[0])

        params = []
        for node in tree_to_list(form, []):
            if not isinstance(node, Element):
                continue
            name = node.attributes.get("name")
            if not name or "disabled" in node.attributes:
                continue
            if node.tag == "input":
                itype = node.attributes.get("type", "text").lower()
                if itype in ("submit", "button", "reset", "image"):
                    # Only the control that was actually pressed speaks for
                    # the form; the other buttons stay silent, which is how a
                    # form with several submit buttons says which one ran.
                    if node is submitter:
                        params.append((name, field_value(node)))
                    continue
                if itype in ("checkbox", "radio"):
                    if field_checked(node):
                        params.append((name, field_value(node) or "on"))
                    continue
                params.append((name, field_value(node)))
            elif node.tag == "textarea":
                params.append((name, field_value(node)))
            elif node.tag == "button":
                if node is submitter:
                    params.append((name, node.attributes.get("value", "")))
            elif node.tag == "select":
                # One parameter per chosen option: a `multiple` select
                # submits every selection, and a single-choice one submits
                # the one option it settled on. Reading the choice through
                # the same helper the painter uses is what keeps what was
                # submitted equal to what was on the screen -- including
                # options nested in an <optgroup>, which a scan of the
                # select's own children never sees.
                for opt in selected_options(node):
                    params.append((name, option_value(opt)))

        query = urllib.parse.urlencode(params)
        if method == "post":
            plain = str(url).split("#", 1)[0]
            return FormAction(URL(plain), query)
        # GET: merge the query into any query the action already carries.
        plain = str(url).split("#", 1)[0]
        new_url = URL(plain)
        base_path, _, existing = new_url.path.partition("?")
        parts = [p for p in (existing, query) if p]
        new_url.path = base_path + ("?" + "&".join(parts) if parts else "")
        return FormAction(new_url, None)

    def draw(self, canvas, offset, region=None):
        """Paint the page. `region` is an optional (x0, y0, x1, y1) rect in
        viewport space (y relative to the page top, i.e. after scroll): when
        given, only commands intersecting it are deleted and repainted, so a
        small change (a text selection drag, a single mutated node) does not
        repaint the whole page."""
        view_bottom = self.scroll + self.tab_height
        if region is None:
            canvas.delete("page")
            for cmd in self.display_list:
                if cmd.top > view_bottom or cmd.bottom < self.scroll:
                    continue
                cmd.execute(self.scroll - offset, canvas,
                            (f"c{id(cmd)}", "page"))
        else:
            x0, y0, x1, y1 = region
            ry0, ry1 = y0 + self.scroll, y1 + self.scroll
            for cmd in self.display_list:
                if cmd.top > view_bottom or cmd.bottom < self.scroll:
                    continue
                if cmd.bottom <= ry0 or cmd.top > ry1 \
                        or cmd.right <= x0 or cmd.left > x1:
                    continue
                canvas.delete(f"c{id(cmd)}")
                cmd.execute(self.scroll - offset, canvas,
                            (f"c{id(cmd)}", "page"))
        self._draw_selection(canvas, offset)

    def selection_colors(self):
        """(highlight fill, text on top of it) for the active shoe.

        The fill is the shoe's `accent`, which is the role the palette
        already names "selection / spinner / focus highlight", so the
        highlight changes with the theme like the rest of the chrome. The
        text is whichever of black or white stays readable on it -- the pale
        shoes and the near-black ones both have accents that one fixed
        foreground would disappear into.
        """
        fill = self.browser.c("accent") if self.browser is not None \
            else shoes.SHOES[shoes.DEFAULT_SHOE]["accent"]
        return fill, contrasting_text_color(fill)

    def _draw_selection(self, canvas, offset):
        """Paint the text-selection highlight over whatever was drawn.

        A run the selection only partly covers is split here rather than
        highlighted whole: the fill starts and stops at the measured x of the
        selected characters, and only those characters are repainted on top.
        """
        canvas.delete("selection")
        fill, ink = self.selection_colors()
        dy = offset - self.scroll
        previous = None
        for run, s, e in self._selection_spans():
            y1, y2 = run.top, run.bottom
            visible = not (y2 < self.scroll
                           or y1 > self.scroll + self.tab_height)
            x1, x2 = run.x_at(s), run.x_at(e)
            try:
                if previous is not None and visible:
                    # The space between two words is not drawn by anything, so
                    # without this the highlight comes out as a row of
                    # separate boxes instead of the continuous band every
                    # other browser paints.
                    gap_run, gap_end = previous
                    if gap_run.line == run.line and gap_end == len(gap_run.text) \
                            and s == 0 and gap_run.right < x1:
                        canvas.create_rectangle(
                            gap_run.right, min(gap_run.top, y1) + dy,
                            x1, max(gap_run.bottom, y2) + dy,
                            fill=fill, width=0, tags=("selection",))
                if visible:
                    canvas.create_rectangle(x1, y1 + dy, x2, y2 + dy,
                                            fill=fill, width=0,
                                            tags=("selection",))
                    canvas.create_text(x1, y1 + dy, text=run.text[s:e],
                                       font=run.font, fill=ink, anchor="nw",
                                       tags=("selection",))
            except CanvasError:
                pass
            previous = (run, e)


_MENU_FALLBACK = {
    "menu_bg": "#ffffff", "menu_border": "#666666",
    "menu_text": "#111111", "menu_hover": "#1a73e8",
    "menu_sep": "#dddddd", "menu_shadow": "#d0d0d0",
    "menu_disabled": "#aaaaaa",
}


def _menu_color(browser, key):
    """Color from a browser's active shoe (or a fallback palette)."""
    if browser is not None:
        return browser.c(key)
    return _MENU_FALLBACK.get(key, key)


def _handle_context_menu_click(menu, x, y, draw):
    """Route a click on an open menu: dismiss, select, or leave it."""
    if not menu.point_in_menu(x, y):
        menu.close()
        draw()
        return
    idx = menu.hit(x, y)
    if idx < 0:
        menu.close()
        draw()
        return
    menu.hover = idx
    cb = menu.activate()
    menu.close()
    draw()
    if cb:
        cb()


def _copy_text(window, text):
    try:
        window.clipboard_clear()
        window.clipboard_append(text)
    except CanvasError:
        pass


class ContextMenu:
    """A hand-drawn context menu painted on the browser canvas.

    Stays true to the "chrome is drawn by hand" design: no native menu
    widgets, just rectangles and text, so it looks and behaves like the rest
    of the UI. Items are None (a separator) or (label, callback, enabled).

    It renders on top of everything in Browser.draw() and tracks its own
    hover state; the browser feeds it mouse/keyboard events while open.
    """

    ITEM_H = 26
    PAD = 4
    PAD_X = 10
    SEP = 8

    def __init__(self, browser=None):
        self.browser = browser
        self.items = []
        self.x = self.y = 0
        self.width = self.height = 0
        self.hover = -1
        self.open_ = False
        # What the menu is anchored to, when it hangs off chrome rather
        # than a click point: "burger" for the settings menu, None for a
        # right-click menu. A resize moves the thing it hangs off, and the
        # owner re-anchors only menus that name one.
        self.anchor = None

    def c(self, key):
        """Color from the owning browser's active shoe (or a fallback)."""
        return _menu_color(self.browser, key)

    def open(self, x, y, items, canvas_w, canvas_h):
        self.items = items
        self.hover = -1
        # A fresh open is positioned explicitly; whoever wants the menu
        # anchored to chrome (the settings menu) re-sets `anchor` itself.
        self.anchor = None
        font = get_font(12, "normal", "roman", "Helvetica")
        width = 170
        for item in items:
            if item is not None:
                width = max(width, _measure(font, item[0])
                            + 2 * self.PAD_X + 8)
        height = self.PAD
        for item in items:
            height += self.SEP if item is None else self.ITEM_H
        height += self.PAD
        self.width = max(120, min(width, canvas_w - 4))
        self.height = height
        self.x = max(2, min(x, canvas_w - self.width - 2))
        self.y = max(2, min(y, canvas_h - self.height - 2))
        self.open_ = True

    def close(self):
        self.open_ = False
        self.items = []
        self.hover = -1
        self.anchor = None

    def point_in_menu(self, x, y):
        return (self.open_ and self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)

    def hit(self, x, y):
        """Index of the item under (x, y), or -1 (separators never hit)."""
        if not self.point_in_menu(x, y):
            return -1
        y0 = self.y + self.PAD
        for i, item in enumerate(self.items):
            if item is None:
                y0 += self.SEP
                continue
            if y0 <= y < y0 + self.ITEM_H:
                return i
            y0 += self.ITEM_H
        return -1

    def set_hover(self, x, y):
        idx = self.hit(x, y)
        changed = idx != self.hover
        self.hover = idx
        return changed

    def _enabled_indices(self):
        return [i for i, item in enumerate(self.items)
                if item is not None and item[2]]

    def move(self, delta):
        """Move keyboard focus to the next/previous enabled item."""
        enabled = self._enabled_indices()
        if not enabled:
            return
        if self.hover in enabled:
            pos = enabled.index(self.hover)
        else:
            pos = -1 if delta > 0 else 0
        self.hover = enabled[(pos + delta) % len(enabled)]

    def activate(self):
        """Return the callback of the hovered enabled item, else None."""
        if 0 <= self.hover < len(self.items):
            item = self.items[self.hover]
            if item is not None and item[2]:
                return item[1]
        return None

    def draw(self, canvas):
        if not self.open_:
            return
        c = canvas
        # Tagged so a repaint can replace the menu rather than stack a fresh
        # copy of it over the one a page/chrome redraw has just covered.
        c.delete("menu")
        x, y = self.x, self.y
        c.create_rectangle(x - 1, y - 1, x + self.width + 1,
                           y + self.height + 1, fill=self.c("menu_shadow"),
                           width=0, tags=("menu",))
        c.create_rectangle(x, y, x + self.width, y + self.height,
                           fill=self.c("menu_bg"),
                           outline=self.c("menu_border"), width=1,
                           tags=("menu",))
        y0 = y + self.PAD
        for i, item in enumerate(self.items):
            if item is None:
                y0 += self.SEP / 2
                c.create_line(x + 8, y0, x + self.width - 8, y0,
                              fill=self.c("menu_sep"), width=1,
                              tags=("menu",))
                y0 += self.SEP / 2
                continue
            label, _callback, enabled = item
            if i == self.hover and enabled:
                c.create_rectangle(x + 1, y0, x + self.width - 1,
                                   y0 + self.ITEM_H, fill=self.c("menu_hover"),
                                   width=0, tags=("menu",))
                color = self.c("menu_bg")
            else:
                color = self.c("menu_text" if enabled else "menu_disabled")
            c.create_text(x + self.PAD_X, y0 + self.ITEM_H / 2, text=label,
                          anchor="w", font=get_font(12, "normal", "roman",
                                                    "Helvetica"), fill=color,
                          tags=("menu",))
            y0 += self.ITEM_H


class SelectPopup:
    """The list a <select> drops down, drawn by hand on the browser canvas.

    There is no native widget to borrow here, so the list is the same sort of
    thing as the context menu: rectangles and text in the current shoe's menu
    colours, created last so they land on top of the page. It holds nothing
    but presentation state -- which row is highlighted, and how far a list
    too tall for the window has been scrolled. Choosing an option is the
    Tab's business, because that is a change to the document.
    """

    ROW_H = 22
    PAD = 3
    PAD_X = 9
    INDENT = 12

    def __init__(self, browser=None):
        self.browser = browser
        self.node = None
        self.rows = []
        self.chosen_ids = frozenset()
        self.hover = -1
        self.top = 0        # index of the first row drawn
        self.visible = 0    # how many rows fit
        self.x = self.y = self.width = self.height = 0
        self.open_ = False

    def c(self, key):
        """Colour from the owning browser's active shoe (or a fallback)."""
        return _menu_color(self.browser, key)

    def open(self, node, rect, bounds):
        """Drop the list for `node`, whose control occupies `rect`.

        Both boxes are in canvas coordinates, and `bounds` is the region the
        list may use -- the page area, not the whole window, so a list too
        long to fit stops at the chrome instead of burying the address bar.
        Returns False (and opens nothing) for a select with no options.
        """
        rows = select_rows(node)
        if not rows:
            return False
        bx0, by0, bx1, by1 = bounds
        self.node = node
        self.rows = rows
        # Which options count as chosen is worked out once, here, because it
        # is the same question the closed control answers -- and it has an
        # answer even when the markup marks nothing.
        self.chosen_ids = frozenset(id(opt) for opt in selected_options(node))
        self.hover = next(
            (i for i, row in enumerate(rows)
             if row.option is not None and id(row.option) in self.chosen_ids),
            -1)
        if self.hover < 0:
            self.hover = self._step(-1, 1)

        font = self._font()
        # Room for the widest label, plus the tick every option leaves space
        # for so the list does not shift as the choice moves.
        indent = self.INDENT if any(row.heading for row in rows) else 0
        width = max(rect[2] - rect[0], 80)
        for row in rows:
            width = max(width, _measure(font, "✓ " + row.label)
                        + 2 * self.PAD_X + (0 if row.heading else indent))
        self.width = min(width, max(80, bx1 - bx0 - 4))

        # A list taller than the space is windowed rather than clipped: the
        # rows around the highlight are the ones worth seeing.
        room = max(by1 - by0 - 8, self.ROW_H)
        self.visible = max(1, min(len(rows),
                                  int((room - 2 * self.PAD) // self.ROW_H)))
        self.height = self.visible * self.ROW_H + 2 * self.PAD

        self.x = max(bx0 + 2, min(rect[0], bx1 - self.width - 2))
        # Below the control if it fits, above it if not -- the same rule a
        # real drop-down follows, and the reason the control stays visible.
        if rect[3] + self.height <= by1 - 2:
            self.y = rect[3]
        elif rect[1] - self.height >= by0 + 2:
            self.y = rect[1] - self.height
        else:
            self.y = max(by0 + 2, by1 - self.height - 2)

        self.top = 0
        self._scroll_into_view()
        self.open_ = True
        return True

    def close(self):
        self.open_ = False
        self.node = None
        self.rows = []
        self.chosen_ids = frozenset()
        self.hover = -1
        self.top = 0

    def point_in_popup(self, x, y):
        return (self.open_ and self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)

    def hit(self, x, y):
        """Index of the row under (x, y), or -1."""
        if not self.point_in_popup(x, y):
            return -1
        i = self.top + int((y - self.y - self.PAD) // self.ROW_H)
        if self.top <= i < min(len(self.rows), self.top + self.visible):
            return i
        return -1

    def set_hover(self, x, y):
        """Track the mouse. Returns True when the drawn highlight moved."""
        i = self.hit(x, y)
        if i >= 0 and not self.rows[i].enabled:
            return False
        if i < 0 or i == self.hover:
            return False
        self.hover = i
        return True

    def _font(self):
        return get_font(12, "normal", "roman", "Helvetica")

    def _step(self, start, delta):
        """First selectable row at or after `start` walking by `delta`,
        wrapping around the ends; -1 when the list has none."""
        n = len(self.rows)
        for k in range(1, n + 1):
            i = (start + k * delta) % n
            if self.rows[i].enabled:
                return i
        return -1

    def move(self, delta):
        """Move the highlight to the next/previous selectable option."""
        i = self._step(self.hover if self.hover >= 0 else -1, delta)
        if i < 0:
            return
        self.hover = i
        self._scroll_into_view()

    def move_to_end(self, last=False):
        """Jump the highlight to the first (or last) selectable option."""
        i = self._step(0, -1) if last else self._step(-1, 1)
        if i >= 0:
            self.hover = i
            self._scroll_into_view()

    def _scroll_into_view(self):
        if self.hover < 0 or self.visible <= 0:
            return
        if self.hover < self.top:
            self.top = self.hover
        elif self.hover >= self.top + self.visible:
            self.top = self.hover - self.visible + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - self.visible)))

    def chosen(self):
        """The highlighted <option>, or None when nothing is on a real one."""
        if 0 <= self.hover < len(self.rows) and self.rows[self.hover].enabled:
            return self.rows[self.hover].option
        return None

    def draw(self, canvas):
        if not self.open_:
            return
        c = canvas
        x, y, w = self.x, self.y, self.width
        tags = ("select-popup",)
        c.create_rectangle(x - 1, y - 1, x + w + 1, y + self.height + 1,
                           fill=self.c("menu_shadow"), width=0, tags=tags)
        c.create_rectangle(x, y, x + w, y + self.height,
                           fill=self.c("menu_bg"),
                           outline=self.c("menu_border"), width=1, tags=tags)
        font = self._font()
        indented = any(row.heading for row in self.rows)
        y0 = y + self.PAD
        for i in range(self.top, min(len(self.rows), self.top + self.visible)):
            row = self.rows[i]
            if row.heading:
                c.create_text(x + self.PAD_X, y0 + self.ROW_H / 2,
                              text=row.label, anchor="w",
                              font=get_font(12, "bold", "roman", "Helvetica"),
                              fill=self.c("menu_disabled"), tags=tags)
                y0 += self.ROW_H
                continue
            if i == self.hover:
                c.create_rectangle(x + 1, y0, x + w - 1, y0 + self.ROW_H,
                                   fill=self.c("menu_hover"), width=0,
                                   tags=tags)
                fill = self.c("menu_bg")
            else:
                fill = self.c("menu_text" if row.enabled else "menu_disabled")
            tick = "✓ " if id(row.option) in self.chosen_ids else ""
            c.create_text(x + self.PAD_X + (self.INDENT if indented else 0),
                          y0 + self.ROW_H / 2, text=tick + row.label,
                          anchor="w", font=font, fill=fill, tags=tags)
            y0 += self.ROW_H


class DownloadsPanel:
    """The download manager, drawn by hand on the browser canvas.

    Same deal as ContextMenu and SelectPopup: rectangles and text in the
    active shoe's colors, because there is no widget toolkit here to borrow
    a list view from. It shows one row per download with its name, a
    progress bar, a line of status, and an × that cancels while the transfer
    is still running.

    It owns no state about the transfers themselves -- it reads the
    DownloadManager every time it paints, on the UI thread, while workers
    write to those records from their own threads. That is the whole
    synchronisation story: short locks inside Download, and a panel that
    only ever reads.
    """

    WIDTH = 400
    HEADER_H = 32
    ITEM_H = 64
    FOOTER_H = 26
    PAD = 12
    MAX_ROWS = 6
    BAR_H = 6

    def __init__(self, browser):
        self.browser = browser
        self.open_ = False
        self.x = self.y = 0
        self.width = self.WIDTH
        self.height = self.HEADER_H + self.FOOTER_H
        self._rows = []  # (download, y0) laid out by the last draw()

    # -- geometry --------------------------------------------------------

    def _visible(self):
        return self.browser.downloads.items()[:self.MAX_ROWS]

    def _layout(self):
        canvas = self.browser.canvas
        rows = max(1, len(self._visible()))
        self.width = min(self.WIDTH, max(220, canvas.winfo_width() - 24))
        self.height = self.HEADER_H + rows * self.ITEM_H + self.FOOTER_H
        self.x = max(8, canvas.winfo_width() - self.width - 12)
        self.y = self.browser.chrome_height() + 6

    def point_in(self, x, y):
        return (self.open_ and self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)

    def toggle(self):
        self.open_ = not self.open_
        return self.open_

    def close(self):
        self.open_ = False

    # -- input -----------------------------------------------------------

    def hit(self, x, y):
        """What a click at (x, y) means: (action, download) or None.

        Actions are "close", "clear", "cancel" and "row"; a click anywhere
        else inside the panel is swallowed ("row" with no download) so it
        does not fall through to the page behind it.
        """
        if not self.point_in(x, y):
            return None
        if y <= self.y + self.HEADER_H:
            if x >= self.x + self.width - 28:
                return ("close", None)
            return ("row", None)
        if y >= self.y + self.height - self.FOOTER_H:
            if x <= self.x + 120:
                return ("clear", None)
            return ("row", None)
        for download, y0 in self._rows:
            if y0 <= y < y0 + self.ITEM_H:
                if x >= self.x + self.width - 34 and download.is_active():
                    return ("cancel", download)
                return ("row", download)
        return ("row", None)

    # -- painting --------------------------------------------------------

    def draw(self, canvas):
        canvas.delete("downloads")
        if not self.open_:
            self._rows = []
            return
        self._layout()
        items = self._visible()
        tags = ("downloads",)
        x, y, w, h = self.x, self.y, self.width, self.height
        canvas.create_rectangle(x + 2, y + 2, x + w + 2, y + h + 2,
                                fill=self.browser.c("menu_shadow"), width=0,
                                tags=tags)
        canvas.create_rectangle(x, y, x + w, y + h,
                                fill=self.browser.c("menu_bg"),
                                outline=self.browser.c("menu_border"), width=1,
                                tags=tags)
        canvas.create_text(x + self.PAD, y + self.HEADER_H / 2,
                           text="Downloads", anchor="w",
                           font=self.browser.bold_font,
                           fill=self.browser.c("menu_text"), tags=tags)
        canvas.create_text(x + w - 14, y + self.HEADER_H / 2, text="×",
                           font=self.browser.bold_font,
                           fill=self.browser.c("menu_text"), tags=tags)
        canvas.create_line(x + 1, y + self.HEADER_H, x + w - 1,
                           y + self.HEADER_H,
                           fill=self.browser.c("menu_sep"), tags=tags)

        self._rows = []
        y0 = y + self.HEADER_H
        if not items:
            canvas.create_text(x + self.PAD, y0 + self.ITEM_H / 2,
                               text="Nothing downloaded yet.", anchor="w",
                               font=get_font(12, "normal", "roman",
                                             "Helvetica"),
                               fill=self.browser.c("menu_disabled"), tags=tags)
        for download in items:
            self._rows.append((download, y0))
            self._draw_row(canvas, download, y0, tags)
            y0 += self.ITEM_H

        canvas.create_line(x + 1, y + h - self.FOOTER_H, x + w - 1,
                           y + h - self.FOOTER_H,
                           fill=self.browser.c("menu_sep"), tags=tags)
        canvas.create_text(x + self.PAD, y + h - self.FOOTER_H / 2,
                           text="Clear finished", anchor="w",
                           font=get_font(11, "normal", "roman", "Helvetica"),
                           fill=self.browser.c("link_color"), tags=tags)
        canvas.create_text(x + w - self.PAD, y + h - self.FOOTER_H / 2,
                           text=self.browser.downloads.directory(), anchor="e",
                           font=get_font(10, "normal", "roman", "Helvetica"),
                           fill=self.browser.c("menu_disabled"), tags=tags)

    def _draw_row(self, canvas, download, y0, tags):
        x, w = self.x, self.width
        name_font = get_font(12, "normal", "roman", "Helvetica")
        info_font = get_font(10, "normal", "roman", "Helvetica")
        name = _elide(download.filename or "download", name_font,
                      w - 2 * self.PAD - 30)
        canvas.create_text(x + self.PAD, y0 + 16, text=name, anchor="w",
                           font=name_font,
                           fill=self.browser.c("menu_text"), tags=tags)
        if download.is_active():
            canvas.create_text(x + w - 18, y0 + 16, text="×",
                               font=self.browser.bold_font,
                               fill=self.browser.c("menu_text"), tags=tags)

        bar_x0 = x + self.PAD
        bar_x1 = x + w - self.PAD
        bar_y = y0 + 30
        canvas.create_rectangle(bar_x0, bar_y, bar_x1, bar_y + self.BAR_H,
                                fill=self.browser.c("menu_sep"), width=0,
                                tags=tags)
        fraction = download.percent()
        state = download.state
        if state == downloads.COMPLETE:
            fill, span = self.browser.c("accent"), (bar_x0, bar_x1)
        elif state == downloads.FAILED:
            fill, span = self.browser.c("log_text"), (bar_x0, bar_x1)
        elif state == downloads.CANCELLED:
            fill, span = self.browser.c("menu_disabled"), (bar_x0, bar_x1)
        elif fraction is None:
            # No Content-Length, so there is no share of the whole to draw.
            # A band sliding along the track says "still going" without
            # claiming a percentage nobody sent us.
            fill = self.browser.c("accent")
            width = (bar_x1 - bar_x0) * 0.25
            travel = (bar_x1 - bar_x0) - width
            phase = (self.browser._downloads_phase % 100) / 100.0
            start = bar_x0 + travel * abs(1 - 2 * phase)
            span = (start, start + width)
        else:
            fill = self.browser.c("accent")
            span = (bar_x0, bar_x0 + (bar_x1 - bar_x0) * fraction)
        if span[1] > span[0]:
            canvas.create_rectangle(span[0], bar_y, span[1],
                                    bar_y + self.BAR_H, fill=fill, width=0,
                                    tags=tags)
        status = _elide(download.describe(), info_font, w - 2 * self.PAD)
        canvas.create_text(x + self.PAD, y0 + 48, text=status, anchor="w",
                           font=info_font,
                           fill=self.browser.c("status_text"), tags=tags)


def _elide(text, font, width):
    """Trim `text` with an ellipsis until it fits `width` pixels."""
    if _measure(font, text) <= width:
        return text
    while text and _measure(font, text + "…") > width:
        text = text[:-1]
    return text + "…"


def _tab_slot(j, home, target):
    """Which slot tab `j` is drawn in while the tab from `home` is being
    carried over `target`.

    This is the arrangement ``tabs.insert(target, tabs.pop(home))`` would
    produce, worked out without disturbing the list: everything the dragged
    tab has passed shuffles one slot back towards the hole it left behind.
    """
    if j == home:
        return target
    if home < j <= target:
        return j - 1
    if target <= j < home:
        return j + 1
    return j


class _TabDrag:
    """A tab being carried along the tab strip.

    The browser's tab list is left alone until the drop, so nothing that reads
    `browser.tabs` in the middle of the gesture -- a repaint, a toe, a
    keyboard shortcut -- ever sees a half-finished reorder, and cancelling is
    just forgetting this object. `home` is the index the tab came from and
    `target` the slot it would land in if the pointer let go now; the two are
    equal until the pointer carries it past a neighbour.
    """

    def __init__(self, home, press_x, grab, count):
        self.home = home
        self.press_x = press_x  # where the press landed; the slop is from here
        self.grab = grab  # how far into the tab the pointer went down
        self.count = count  # tabs on the strip when the gesture began
        self.x = press_x
        self.moved = False  # past the slop: a reorder, not a click
        self.target = home

    def track(self, x):
        """Follow the pointer to `x`. True once this is a drag rather than a
        click that has not moved yet."""
        self.x = x
        if abs(x - self.press_x) >= TAB_DRAG_SLOP:
            # Once a gesture is a drag it stays one, even if the pointer
            # wanders back to where it started: what happens on release is
            # settled by the first real movement, not by the last.
            self.moved = True
        if self.moved:
            self.target = self._target()
        return self.moved

    def left(self):
        """Left edge of the dragged tab, clamped to the strip so the tab can
        be carried past either end without being drawn off it."""
        return min(max(self.x - self.grab, TAB_LEFT),
                   TAB_LEFT + (self.count - 1) * TAB_GAP)

    def _target(self):
        """The slot the dragged tab is over: the nearest one to where it is
        drawn, which -- every tab being the same width -- means the target
        changes exactly as the dragged tab crosses a neighbour's midpoint."""
        slot = (self.left() - TAB_LEFT + TAB_GAP // 2) // TAB_GAP
        return int(max(0, min(self.count - 1, slot)))


class Browser:
    # How far back _track_scroll_velocity reads. Long enough to hold the
    # several ticks one flick of a wheel sends, short enough that a pause
    # in the middle of a scroll ends the flick rather than averaging over
    # it.
    SCROLL_VELOCITY_WINDOW = 0.15

    def __init__(self, window=None):
        self.tabs = []
        self.active_tab = None
        self.focus = None  # "address" or None
        self.address_text = ""
        self.bookmarks = self._load_bookmarks()
        # Shoes theme: the active color palette for the chrome.
        self.shoe = shoes.load()
        self.theme = shoes.merge(shoes.resolve(self.shoe))
        # Browser settings: search engine, scroll speed, momentum, and the
        # rest, loaded once from ~/.feetbrowser_settings.json.
        self.settings = settings.load()
        self.address_caret = 0
        self.address_sel = None  # (start, end) while selecting, else None
        self.address_view = 0  # horizontal scroll offset in px
        self._drag_moved = False  # a press+move (vs. a plain click) happened
        # Scrollbar drag: how far below the top of the thumb the pointer went
        # down, so the grabbed point stays under it. None when not dragging.
        self._scroll_grab = None
        # The tab being dragged along the tab strip, or None between
        # gestures. See _TabDrag: the reorder itself only happens on the drop.
        self._tab_drag = None
        # A <input type=range> being dragged: the (node, thumb center x).
        # None between gestures.
        self._range_grab = None
        # Where a grabbed range's thumb is headed: the target fraction the
        # press (or last drag) aimed for. _commit_range lands on it snapped
        # to step, even when a press was released before the glide finished.
        self._range_target = 0.0
        # The glide itself: (start frac, target frac, progress 0..1) plus the
        # pending `after` handle that advances it, both None when still.
        self._range_glide = None
        self._range_anim = None
        self._resize_after = None
        self._last_size = (WIDTH, HEIGHT)
        # Multi-click tracking for word/line selection. No platform backend
        # reports a click count, so it is counted here: presses close enough
        # in time and in space are one gesture, and the count cycles 1-2-3 so
        # a fourth click starts over the way it does elsewhere.
        self._click_count = 0
        self._click_time = 0.0
        self._click_pos = (0, 0)
        # Chrome-style loading spinner: current arc start angle (degrees).
        self._loading_angle = 0
        # Dirty flag for the repaint loop: set by render() whenever page
        # content changes, cleared by draw(). Lets the repaint timer skip the
        # canvas entirely while the page is idle instead of repainting every
        # 120ms forever.
        self._repaint_needed = True
        # Scroll repaint coalescing: at most one pending, run at the next
        # frame at the latest scroll position, so a fast scrollbar drag pays
        # one full redraw per frame instead of one per mouse-move event.
        self._scroll_repaint_pending = False
        # Scroll velocity, for the momentum-easing curve: the ticks of the
        # flick in progress, oldest first. See _track_scroll_velocity.
        # `_momentum_job` is the pending settle or coast timer handle, or
        # None when nothing is coasting.
        self._scroll_ticks = []
        self._scroll_velocity = 0.0
        self._momentum_job = None
        # Whether the _poll_images() after-chain is already running. It is
        # started by whoever needs it first -- run(), or settle() in a
        # headless render -- and there must only ever be one of it.
        self._polling_images = False
        self._video_ticking = False

        # Toes: one Context per loaded toe, all optional hooks.
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, toe.module) for toe in self.toes]
        self.toe_handlers = {}
        for ctx in self.toe_contexts:
            for btn in (ctx.call("buttons") or []):
                self.toe_handlers[btn.id] = ctx

        # A headless root by default, so tests and --screenshot never open
        # anything; main() passes a real one from gui.new_window().
        self.window = window if window is not None else Tk()
        self.window.title("FeetBrowser")
        self.window.geometry(f"{WIDTH}x{HEIGHT}")
        self.window.minsize(480, 320)
        self.canvas = Canvas(
            self.window, width=WIDTH, height=HEIGHT,
            bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.chrome_font = get_font(14, "normal", "roman", "Helvetica")
        self.bold_font = get_font(14, "bold", "roman", "Helvetica")

        self.context_menu = ContextMenu(self)
        self.select_popup = SelectPopup(self)
        # Downloads: the manager owns the worker threads and the records,
        # the panel is a view of them. `_downloads_phase` advances on the
        # UI timer and drives the indeterminate progress bar for transfers
        # whose total nobody stated.
        self.downloads = downloads.DownloadManager()
        self.downloads_panel = DownloadsPanel(self)
        self._downloads_phase = 0

        self._bind()

    def _bind(self):
        w = self.window
        w.bind("<Down>", self._on_down)
        w.bind("<Up>", self._on_up)
        w.bind("<Next>", self._on_page_down)   # PageDown
        w.bind("<Prior>", self._on_page_up)    # PageUp
        w.bind("<Left>", self._on_left)
        w.bind("<Right>", self._on_right)
        w.bind("<Home>", self._on_home)
        w.bind("<End>", self._on_end)
        w.bind("<Control-Home>", self._on_home_key)
        w.bind("<Control-End>", self._on_end_key)
        w.bind("<MouseWheel>", self._on_wheel)
        w.bind("<Button-4>", lambda e: self._scroll(-self.scroll_step()))
        w.bind("<Button-5>", lambda e: self._scroll(self.scroll_step()))
        w.bind("<Button-1>", self._on_click)
        w.bind("<B1-Motion>", self._on_drag)
        w.bind("<ButtonRelease-1>", self._on_release)
        w.bind("<Button-2>", self._on_middle_click)
        w.bind("<Button-3>", self._on_context_menu)
        w.bind("<Motion>", self._on_motion)
        w.bind("<Key>", self._on_key)
        w.bind("<Return>", self._on_enter)
        w.bind("<BackSpace>", self._on_backspace)
        w.bind("<Delete>", self._on_delete)
        w.bind("<Escape>", self._on_escape)
        w.bind("<Configure>", self._on_resize)
        w.bind("<Control-l>", lambda e: self._focus_address())
        w.bind("<Control-t>", lambda e: self.new_tab("about:blank",
                                                     focus_address=True))
        w.bind("<Control-w>", lambda e: self.close_tab())
        w.bind("<Control-r>", lambda e: self._reload())
        w.bind("<Control-d>", lambda e: self._toggle_bookmark())
        w.bind("<Control-h>", lambda e: self._open_history_page())
        w.bind("<Control-j>", lambda e: self._toggle_downloads())
        w.bind("<Control-Shift-s>", lambda e: self._open_shoes_page())
        w.bind("<Control-Tab>", lambda e: self._cycle_tab(1))
        w.bind("<Control-ISO_Left_Tab>", lambda e: self._cycle_tab(-1))
        w.bind("<Control-Prior>", lambda e: self._next_tab(-1))
        w.bind("<Control-Next>", lambda e: self._next_tab(1))
        w.bind("<Alt-Left>", lambda e: self._back())
        w.bind("<Alt-Right>", lambda e: self._forward())

    # -- tab management --------------------------------------------------

    def chrome_bands(self):
        """Chrome bands declared by toes, as [(id, height, y), ...]."""
        return toes.compute_bands(self.toe_contexts)

    def reload_toes(self):
        """Re-discover installed toes and rebuild their contexts live.

        Called by the ToeHub after an install/uninstall so changes take
        effect without restarting the browser.
        """
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, toe.module)
                             for toe in self.toes]
        self.toe_handlers = {}
        for ctx in self.toe_contexts:
            for btn in (ctx.call("buttons") or []):
                self.toe_handlers[btn.id] = ctx
        self.draw()

    def chrome_height(self):
        """Total chrome height: the fixed chrome, the log strip, and any toe
        bands."""
        return CHROME_HEIGHT + LOG_HEIGHT + toes.band_height(self.chrome_bands())

    def tab_height(self):
        h = self.canvas.winfo_height()
        if h <= 1:  # window not mapped yet
            h = HEIGHT
        return max(50, h - self.chrome_height())

    def _tab_x(self, i):
        """Left edge of tab `i` on the strip (tabs flow left-to-right)."""
        return TAB_LEFT + i * TAB_GAP

    def _new_tab_x(self):
        """Left edge of the "+" new-tab button, after the last tab but kept
        on-screen when many tabs overflow the window width."""
        return min(TAB_LEFT + len(self.tabs) * TAB_GAP,
                   max(TAB_LEFT, self.canvas.winfo_width() - NEW_TAB_W))

    def _tab_positions(self):
        """(tab, x, dragged) for every tab, in the order they are painted.

        With no drag running this is the strip's plain geometry. During one
        the tabs the dragged tab has passed sit in the slot it vacated, so the
        gap that opens up shows where a release would put it before the user
        commits to it, and the dragged tab itself comes last -- it follows the
        pointer, and it has to ride over its neighbours rather than under.
        """
        drag = self._tab_drag
        if drag is None or not drag.moved:
            return [(tab, self._tab_x(i), False)
                    for i, tab in enumerate(self.tabs)]
        out = [(tab, self._tab_x(_tab_slot(j, drag.home, drag.target)), False)
               for j, tab in enumerate(self.tabs) if j != drag.home]
        out.append((self.tabs[drag.home], drag.left(), True))
        return out

    def _drop_tab(self):
        """End a tab drag, leaving the tab in the slot the strip has been
        showing it in.

        A gesture that never passed the slop was a click, and the press
        already switched to that tab, so there is nothing left to do for it.
        """
        drag = self._tab_drag
        self._tab_drag = None
        if not drag.moved:
            return
        if drag.target != drag.home:
            self.tabs.insert(drag.target, self.tabs.pop(drag.home))
        # The move is over the list of tabs themselves, and what is active is
        # a tab and not a position, so the active tab stays active across it.
        # Nothing else keeps a tab index between events either -- close_tab,
        # _cycle_tab and _next_tab all ask self.tabs.index() at the moment
        # they need one -- so there is no stale index left pointing at
        # whichever tab has moved into the old one's place.
        self.draw()

    def _cancel_tab_drag(self):
        """Abandon a tab drag, putting the strip back in the order it started
        in; True if there was one to abandon.

        Nothing has moved in self.tabs yet (see _TabDrag), so undoing the
        gesture is forgetting it and painting the strip again.
        """
        if self._tab_drag is None:
            return False
        moved = self._tab_drag.moved
        self._tab_drag = None
        if moved:
            self._draw_chrome()
        return True

    def new_tab(self, url, focus_address=False):
        self._cancel_momentum()
        self._dismiss_select_popup()
        # A tab appearing under a running drag would leave that drag holding
        # indices into a strip that has changed shape, so the gesture is
        # abandoned rather than applied to the wrong tab. Ctrl-T mid-drag is
        # the way to get here; the pointer grab keeps clicks from doing it.
        self._tab_drag = None
        tab = Tab(self.tab_height(), self)
        page = self._coerce_url(url)
        if isinstance(page, _AboutURL):
            tab.load(page)  # routes welcome page through the full pipeline
            tab.status = "Type a URL and press Enter"
        else:
            tab.load(page)
        self.tabs.append(tab)
        self.active_tab = tab
        toes.dispatch(self.toe_contexts, "on_new_tab")
        self.draw()
        if focus_address:
            self._focus_address()

    def close_tab(self):
        if not self.active_tab:
            return
        self._cancel_momentum()
        self._dismiss_select_popup()
        self._tab_drag = None  # same reason as new_tab: the strip changed
        idx = self.tabs.index(self.active_tab)
        self.active_tab.stop_videos()
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
        self._dismiss_select_popup()
        global WIDTH, HEIGHT
        WIDTH = self.canvas.winfo_width()
        HEIGHT = self.canvas.winfo_height()
        set_viewport(WIDTH, HEIGHT)
        for tab in self.tabs:
            tab.tab_height = self.tab_height()
            if tab.nodes:
                tab.render()
                tab._clamp_scroll()
        self._reanchor_menu()
        self.draw()

    def _on_down(self, e):
        if self._select_popup_move(1) or self._listbox_move(1):
            return "break"
        if self.focus == "address":
            return
        self._scroll(self.scroll_step())

    def _on_up(self, e):
        if self._select_popup_move(-1) or self._listbox_move(-1):
            return "break"
        if self.focus == "address":
            return
        self._scroll(-self.scroll_step())

    def _select_popup_move(self, delta):
        """Walk an open drop-down's highlight; True when the key was ours."""
        if not self.select_popup.open_:
            return False
        self.select_popup.move(delta)
        self._draw_select_popup()
        return True

    def _on_left(self, e):
        if self.focus == "address":
            self._address_move_caret(-1, extend=bool(e.state & 0x1))
            self.draw()

    def _on_right(self, e):
        if self.focus == "address":
            self._address_move_caret(1, extend=bool(e.state & 0x1))
            self.draw()

    def _on_page_down(self, e):
        if self.focus == "address":
            return
        self._scroll(max(1, self.tab_height() - 120))
        return "break"

    def _on_page_up(self, e):
        if self.focus == "address":
            return
        self._scroll(-max(1, self.tab_height() - 120))
        return "break"

    def _on_home(self, e):
        if self.select_popup.open_:
            self.select_popup.move_to_end(last=False)
            self._draw_select_popup()
            return "break"
        if self._listbox_move(0, to_end=True, last=False):
            return "break"
        if self.focus == "address" or not self.active_tab:
            return
        self._cancel_momentum()
        self.active_tab.set_scroll(0)
        self.draw()
        return "break"

    def _on_end(self, e):
        if self.select_popup.open_:
            self.select_popup.move_to_end(last=True)
            self._draw_select_popup()
            return "break"
        if self._listbox_move(0, to_end=True, last=True):
            return "break"
        if self.focus == "address" or not self.active_tab:
            return
        self._cancel_momentum()
        self.active_tab.set_scroll(self.active_tab.content_height())
        self.draw()
        return "break"

    def _on_wheel(self, e):
        step = self.scroll_step()
        delta = -e.delta if abs(e.delta) < 30 \
            else -int(e.delta / 30) * step
        if self._listbox_wheel(getattr(e, "x", -1), getattr(e, "y", -1), delta):
            return
        self._scroll(delta)
        # A wheel turn arms the coast: if no new notch lands within a
        # settle window, the flick feeds the page on and decays.
        if self.settings.get("momentum", True):
            self._momentum_job = self.window.after(
                MOMENTUM_SETTLE_MS, self._momentum_settle)

    def _momentum_settle(self):
        """Turn the tracked wheel velocity into a coasting animation.

        A fresh wheel tick within the settle window re-arms instead of
        starting a coast, so a continuous flick is one unbroken glide rather
        than a jerk, coast, jerk.
        """
        if self._scroll_ticks and \
                time.monotonic() - self._scroll_ticks[-1][1] < 0.04:
            self._momentum_job = self.window.after(
                MOMENTUM_SETTLE_MS, self._momentum_settle)
            return
        if not self.settings.get("momentum", True):
            self._momentum_job = None
            return
        speed = self._scroll_velocity
        if not self.active_tab or not speed:
            self._momentum_job = None
            return
        strength = self.settings.get("momentum_strength", 100) / 100.0
        gain = MOMENTUM_GAIN * strength
        ceiling = MOMENTUM_MAX * strength
        seed = min(abs(speed) * gain, ceiling)
        if seed < MOMENTUM_STOP:
            self._momentum_job = None
            return
        self._coast(seed if speed > 0 else -seed)

    def _coast(self, speed):
        frame = round(speed)
        if not self.active_tab or abs(frame) < MOMENTUM_STOP:
            self._momentum_job = None
            return
        self.active_tab.scroll_by(frame)
        self._draw_page()
        self._momentum_job = self.window.after(
            MOMENTUM_FRAME_MS, self._coast, speed * MOMENTUM_DECAY)

    def _cancel_momentum(self):
        if self._momentum_job is not None:
            self.window.after_cancel(self._momentum_job)
            self._momentum_job = None

    def _scroll(self, delta):
        # Scrolling the page out from under a drop-down would leave it
        # pointing at nothing, so the list goes rather than travels.
        # A non-wheel scroll has no momentum of its own, and it must stop
        # whatever coast was running.
        self._cancel_momentum()
        self._dismiss_select_popup()
        self._track_scroll_velocity(delta)
        if self.active_tab:
            self.active_tab.scroll_by(delta)
            # The thumb follows the pointer immediately; the page content is
            # coalesced to the next frame so a fast scroll pays one full page
            # redraw per frame instead of freezing.
            self._draw_scrollbar()

    def _track_scroll_velocity(self, delta):
        """Record a scroll tick and re-read the velocity from the last
        SCROLL_VELOCITY_WINDOW seconds of them, in pixels per second.

        A momentum curve wants to know how fast the wheel is turning now,
        which is a distance over a time. The mean of every tick this
        session is neither: it is a distance over a count, and after a
        minute of scrolling no flick can move it far. Dropping the ticks
        that fell out of the window is also what stops the history growing
        for as long as the browser is open.
        """
        now = time.monotonic()
        ticks = self._scroll_ticks
        ticks.append((delta, now))
        # The tick just appended is at `now`, so this never empties the
        # list and ticks[0] below is always there.
        cutoff = now - self.SCROLL_VELOCITY_WINDOW
        while ticks[0][1] < cutoff:
            ticks.pop(0)
        span = now - ticks[0][1]
        # One tick on its own has no duration to be a speed over.
        total = sum(d for d, _ts in ticks)
        self._scroll_velocity = total / span if span else 0.0

    def _on_home_key(self, e):
        if self.focus == "address":
            self.address_caret = 0
            self.address_sel = None
            self._address_ensure_visible()
            self._draw_chrome()
            return
        if self.active_tab:
            self.active_tab.set_scroll(0)
            self.draw()

    def _on_end_key(self, e):
        if self.focus == "address":
            self.address_caret = len(self.address_text)
            self.address_sel = None
            self._address_ensure_visible()
            self._draw_chrome()
            return
        if self.active_tab:
            self.active_tab.scroll_by(10 ** 9)
            self.draw()

    def _on_click(self, e):
        if self.context_menu.open_:
            self._context_menu_click(e.x, e.y)
            return
        if self.select_popup.open_:
            self._select_popup_click(e.x, e.y)
            return
        if self.downloads_panel.point_in(e.x, e.y):
            self._downloads_click(e.x, e.y)
            return
        was_address = self.focus == "address"
        self.focus = None
        self._drag_moved = False
        # A press ends any scrollbar drag still on the books -- normally the
        # release did, but a release delivered somewhere else (the pointer
        # left the window at the wrong moment) must not leave the bar stuck
        # to the pointer for the rest of the session.
        self._scroll_grab = None
        # A tab drag is in the same position, and worse: a press arriving
        # while one is running means the release that should have ended it
        # went somewhere we never saw (the pointer grab was broken), so the
        # tab is put back where it came from rather than dropped at a place
        # the user may never have seen it reach.
        self._cancel_tab_drag()
        if e.y < self.chrome_height():
            self._chrome_click(e.x, e.y, was_address)
            return
        if not self.active_tab:
            return
        if self._scrollbar_press(e.x, e.y):
            if was_address:
                self._draw_chrome()  # drop the address bar's focus ring
            return
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        dest = self.active_tab.click(e.x, e.y - self.chrome_height())
        if isinstance(dest, SelectAction):
            self._open_select_popup(dest)
            return
        if isinstance(dest, FormAction):
            self.active_tab.selection = None
            self._navigate(self.active_tab, dest.url, payload=dest.payload)
        elif dest and ctrl:
            self.active_tab.selection = None
            self.new_tab(str(dest))
        elif dest:
            self.active_tab.selection = None
            self._navigate(self.active_tab, dest)
        else:
            node = self.active_tab._node_at(e.x, e.y - self.chrome_height())
            # Pressing a <input type=range> grabs it: the thumb follows the
            # pointer for the rest of the gesture and the value is committed
            # on release (a change event, so scripts see it).
            if self._press_range(e.x, e.y - self.chrome_height()):
                return
            if self.active_tab._hit_control(node):
                # A form control activation (checkbox, field focus, select)
                # already re-rendered the page; repaint the page layer now
                # instead of waiting for the repaint tick.
                self._draw_page()
                return
            # Anchoring a text selection changes nothing visible yet (the
            # highlight grows during the drag), so no full canvas wipe is
            # needed here; that wipe is what blanked the page on heavy pages.
            self.canvas.delete("selection")
            self.active_tab.selection = None
            if self._in_scrollbar_gutter(e.x):
                # The strip the scrollbar lives in belongs to the chrome, not
                # to the page: a press there is aimed at the bar, and hit
                # testing that resolves any point to the nearest line would
                # otherwise turn a grab at the scrollbar into a selection of
                # whatever text happens to end nearest it. _scrollbar_press
                # has already claimed the presses it wants; this covers the
                # rest of the strip, above and below the track, so the whole
                # column behaves like one thing.
                if was_address:
                    self._draw_chrome()
                return
            self._count_click(e.x, e.y)
            self.active_tab.start_selection(
                e.x, e.y - self.chrome_height(),
                _CLICK_GRANULARITY.get(self._click_count, "char"))
            if self._click_count > 1:
                # A double- or triple-click has selected something already,
                # so it has to show without waiting for a drag.
                self._repaint_selection()
            if was_address:
                # Dropping the address-bar focus ring is a chrome-only change.
                self._draw_chrome()

    def _in_scrollbar_gutter(self, x):
        """Whether `x` lands in the strip the scrollbar occupies.

        Only when there is a bar to occupy it: on a page that fits, the strip
        is ordinary page and the last word on a line is selectable like any
        other.
        """
        tab = self.active_tab
        if not tab or tab.content_height() <= self.tab_height():
            return False
        return x >= self.canvas.winfo_width() - SCROLLBAR_GUTTER_W

    def _count_click(self, x, y):
        """Update the multi-click counter for a press at (x, y)."""
        now = time.monotonic()
        near = abs(x - self._click_pos[0]) <= MULTI_CLICK_SLOP \
            and abs(y - self._click_pos[1]) <= MULTI_CLICK_SLOP
        if near and now - self._click_time <= MULTI_CLICK_SECONDS:
            self._click_count = self._click_count % 3 + 1
        else:
            self._click_count = 1
        self._click_time = now
        self._click_pos = (x, y)

    def _on_middle_click(self, e):
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()
            return
        if self.select_popup.open_:
            self._close_select_popup()
            return
        band_h = toes.band_height(self.chrome_bands())
        if e.y < band_h + 40:
            # Tab bar: middle-click a tab to close it, empty space (or the
            # "+" zone) to open a fresh one.
            for i, tab in enumerate(self.tabs):
                x0 = self._tab_x(i)
                if x0 <= e.x < x0 + TAB_WIDTH:
                    self.active_tab = tab
                    self.close_tab()
                    return
            self.new_tab("about:blank", focus_address=True)
            self.draw()
            return
        if not self.active_tab or e.y < self.chrome_height():
            return
        dest = self.active_tab.click(e.x, e.y - self.chrome_height())
        if isinstance(dest, SelectAction):
            return  # middle-clicking a drop-down opens nothing
        if isinstance(dest, FormAction):
            self._navigate(self.active_tab, dest.url, payload=dest.payload)
        elif dest:
            self.new_tab(str(dest))

    def _on_release(self, e):
        if self._tab_drag is not None:
            self._drop_tab()
            return
        if self._scroll_grab is not None:
            # Letting go anywhere -- inside the window or well outside it --
            # ends the drag and leaves the page where the bar put it.
            self._scroll_grab = None
            self._drag_moved = False
            return
        if self._range_grab is not None:
            self._commit_range()
            self._drag_moved = False
            return
        if self.focus == "address":
            return
        tab = self.active_tab
        if not tab or tab.selection is None:
            return
        if not self._drag_moved and self._click_count < 2:
            # A plain click (press + release, no drag) clears the selection.
            # A double- or triple-click is a click too, and it selected on the
            # way down, so it is exempt.
            tab.selection = None
        self._drag_moved = False
        # Refresh only the highlight layer; a full canvas wipe here would
        # blank the page after every selection.
        c = self.canvas
        c.delete("selection")
        if tab.selection is not None:
            tab._draw_selection(c, self.chrome_height())
            self._publish_primary(tab.selected_text())

    def _on_drag(self, e):
        if self._tab_drag is not None:
            # The tab strip holds the pointer for the length of the gesture:
            # follow it wherever it has got to (both backends keep sending
            # drags to the window the press went to, so e.x may be off either
            # end of the strip) and repaint the chrome, which is what shows
            # the carried tab and the hole it would drop into. Nothing below
            # can also be running: the press that armed this went to the
            # chrome, so it anchored no selection and grabbed no scrollbar.
            if self._tab_drag.track(e.x):
                self._draw_chrome()
            return
        if self._scroll_grab is not None:
            # Dragging the scrollbar, wherever the pointer has got to by now:
            # both backends keep sending the drag to the window the press
            # went to, so e.y may be above the track or below the window.
            self._drag_moved = True
            self._scrollbar_drag_to(e.y)
            return
        if self._range_grab is not None:
            # A grabbed range follows the pointer on every event, wherever
            # it has got to, so the thumb stays under the cursor.
            self._drag_moved = True
            self._range_drag_to(e.x)
            return
        if self.focus == "address" and e.x >= self._address_bar_x() - 10:
            self._drag_moved = True
            if self.address_sel is None:
                self.address_sel = (self.address_caret, self.address_caret)
            anchor = self.address_sel[0]
            self.address_caret = self._caret_from_x(e.x)
            self.address_sel = (anchor, self.address_caret)
            self._address_ensure_visible()
            self._draw_chrome()
            return
        # Dragging on the page extends the text selection.
        if self.select_popup.open_:
            return
        if self.active_tab and e.y >= self.chrome_height():
            if self.active_tab.selection is None:
                # Nothing was anchored on the way down -- the press went to a
                # link, a form control or the scrollbar gutter -- so this drag
                # is not a selection and must not become one halfway through.
                return
            self._drag_moved = True
            self.active_tab.extend_selection(e.x, e.y - self.chrome_height())
            self._repaint_selection()

    def _chrome_click(self, x, y, was_address=False):
        # Toe chrome bands (above the tabs).
        bands = self.chrome_bands()
        band_h = toes.band_height(bands)
        if band_h and y < band_h:
            if toes.dispatch(self.toe_contexts, "on_chrome_click",
                             x, y, bands):
                return
        # Tab bar (top 40px).
        if y < band_h + 40:
            for i, tab in enumerate(self.tabs):
                x0 = self._tab_x(i)
                if x0 <= x < x0 + TAB_WIDTH:
                    # close box
                    if x >= x0 + TAB_WIDTH - TAB_CLOSE_W:
                        self.active_tab = tab
                        self.close_tab()
                        return
                    # A press on the body of a tab switches to it at once, the
                    # way every browser does, and arms a drag at the same
                    # time: which of the two the gesture is only becomes clear
                    # once the pointer has moved, or been let go without
                    # moving, so both are prepared for here.
                    self.active_tab = tab
                    self._tab_drag = _TabDrag(i, x, x - x0, len(self.tabs))
                    self.draw()
                    return
            # New-tab button (right of the last tab).
            nx = self._new_tab_x()
            if nx <= x < nx + NEW_TAB_W:
                self.new_tab("about:blank", focus_address=True)
            return
        # Toolbar (40..80).
        if 8 <= x < 34 and band_h + 48 <= y < band_h + 72:
            self._back()
            return
        if 40 <= x < 66 and band_h + 48 <= y < band_h + 72:
            self._forward()
            return
        if 72 <= x < 98 and band_h + 48 <= y < band_h + 72:
            self._reload()
            return
        if 104 <= x < 130 and band_h + 48 <= y < band_h + 72:
            self._home()
            return
        # Toe toolbar buttons.
        bx = 136
        for btn in self._toe_buttons():
            if bx <= x < bx + 26 and band_h + 48 <= y < band_h + 72:
                ctx = self.toe_handlers.get(btn.id)
                if ctx:
                    ctx.call("on_click", btn.id)
                self.draw()
                return
            bx += 30
        # Bookmark star (after toe buttons).
        star_x = 136 + self._toe_buttons_offset()
        if star_x <= x < star_x + 26 and band_h + 48 <= y < band_h + 72:
            self._toggle_bookmark()
            return
        # Hamburger settings button (right of the address bar).
        menu_x = self.canvas.winfo_width() - MENU_BTN_W - 8
        if menu_x <= x < menu_x + MENU_BTN_W and band_h + 48 <= y < band_h + 72:
            self._toggle_menu()
            return
        # Address bar.
        if x >= 136 + self._toe_buttons_offset() + 30:
            self.focus = "address"
            if not was_address:
                self._address_reset_from_tab()
                self._address_select_all()
            else:
                self._set_address_caret_from_x(x)
                self.address_sel = None
            self._address_ensure_visible()
            self._draw_chrome()

    def _on_motion(self, e):
        if self.select_popup.open_:
            if self.select_popup.set_hover(e.x, e.y):
                self._draw_select_popup()
            return
        if self.context_menu.open_:
            if self.context_menu.set_hover(e.x, e.y):
                # Redraw just the menu, not the whole page, on hover moves.
                self.context_menu.draw(self.canvas)
            return
        if not self.active_tab:
            return
        if e.y >= self.chrome_height():
            doc_x, doc_y = e.x, e.y - self.chrome_height()
            toes.dispatch(self.toe_contexts, "on_motion", doc_x, doc_y)
            href = self.active_tab.link_at(doc_x, doc_y)
            self.canvas.config(cursor="hand2" if href else "")
            if self.settings.get("show_link_preview", True):
                new_status = href or str(self.active_tab.url or "")
            else:
                new_status = ""
            if new_status != self.active_tab.status:
                self.active_tab.status = new_status
                self._draw_status()
        else:
            self.canvas.config(cursor="")

    # -- context menu ----------------------------------------------------

    def _on_context_menu(self, e):
        self._dismiss_select_popup()
        items = self._context_items(e.x, e.y)
        self.context_menu.open(e.x, e.y, items,
                               self.canvas.winfo_width(),
                               self.canvas.winfo_height())
        self.draw()

    def _context_menu_click(self, x, y):
        _handle_context_menu_click(self.context_menu, x, y, self.draw)

    @staticmethod
    def _enclosing_image(node):
        while node is not None:
            if isinstance(node, Element) and node.tag == "img" \
                    and node.attributes.get("src"):
                return node.attributes["src"]
            node = node.parent
        return None

    def _copy_text(self, text):
        _copy_text(self.window, text)

    def _view_source(self):
        tab = self.active_tab
        if tab and isinstance(tab.url, URL):
            self._navigate(tab, URL("view-source:" + str(tab.url)))

    # -- downloads -------------------------------------------------------

    def _toggle_downloads(self):
        self.downloads_panel.toggle()
        self.draw()

    def _download(self, url):
        """Save `url` to the download directory, without navigating.

        This is what "Download Link" does. There is no file picker to open
        -- a native save dialog is exactly the sort of thing this browser
        does not have -- so the file lands in the download directory under
        the name the server suggests, and the panel says where.
        """
        if not isinstance(url, URL):
            try:
                url = URL(str(url))
            except Exception:  # noqa: BLE001 - a malformed href downloads nothing
                return
        if url.scheme not in ("http", "https"):
            return
        self.downloads.start(url)
        self.downloads_panel.open_ = True
        self.draw()

    def _downloads_click(self, x, y):
        action = self.downloads_panel.hit(x, y)
        if action is None:
            return
        what, download = action
        if what == "close":
            self.downloads_panel.close()
        elif what == "clear":
            self.downloads.clear_finished()
        elif what == "cancel" and download is not None:
            download.cancel()
        self.draw()

    # -- <select> drop-down ----------------------------------------------

    def _open_select_popup(self, action):
        """Drop the list for the clicked <select>.

        The control's box arrives in page coordinates; shifting it by the
        scroll and the chrome height puts it where the reader actually sees
        it, which is the only frame the popup knows about.
        """
        tab = self.active_tab
        if not tab:
            return
        # A click handler may have re-laid the page out from under us, so ask
        # where the control is now rather than trusting the captured box.
        lx, ty, rx, by = tab._control_rect(action.node) or action.rect
        dy = self.chrome_height() - tab.scroll
        rect = (lx, ty + dy, rx, by + dy)
        bounds = (0, self.chrome_height(),
                  self.canvas.winfo_width(), self.canvas.winfo_height())
        if not self.select_popup.open(action.node, rect, bounds):
            return
        self._draw_page()

    def _dismiss_select_popup(self):
        """Drop the list, leaving the value alone and the canvas untouched.

        For the callers that are about to repaint anyway -- scrolling,
        resizing, navigating -- since those are exactly the moments a
        drop-down has to go away.
        """
        if not self.select_popup.open_:
            return
        self.select_popup.close()
        if self.active_tab:
            self.active_tab.blur_input()

    def _close_select_popup(self):
        """Dismiss the list without changing the value, and repaint."""
        if not self.select_popup.open_:
            return
        self._dismiss_select_popup()
        self._draw_page()

    def _commit_select(self):
        """Take the highlighted option. A `multiple` select keeps the list up
        so the reader can go on toggling; a single-choice one closes."""
        popup = self.select_popup
        option, node = popup.chosen(), popup.node
        # The document can move under an open list (a script rewriting the
        # form, a load finishing); if the control is no longer laid out there
        # is nothing left to choose in.
        if option is None or not self.active_tab or \
                self.active_tab._control_rect(node) is None:
            self._close_select_popup()
            return
        multiple = "multiple" in node.attributes
        self.active_tab.choose_option(node, option)
        if multiple:
            self._draw_page()
        else:
            self._close_select_popup()

    def _select_popup_click(self, x, y):
        """A click while the list is open: on a row it chooses, anywhere else
        it dismisses."""
        popup = self.select_popup
        i = popup.hit(x, y)
        if i < 0:
            self._close_select_popup()
            return
        if not popup.rows[i].enabled:
            return  # a heading or a disabled option: swallow the click
        popup.hover = i
        self._commit_select()

    def _draw_select_popup(self):
        """Repaint the drop-down layer alone.

        The canvas is retained, so the list is simply its own set of tagged
        items: dropping the tag uncovers the page underneath without
        repainting a pixel of it, and creating the items again puts the list
        back on top of whatever was drawn in between. That is why every path
        that repaints the page or the chrome ends by calling this.
        """
        self.canvas.delete("select-popup")
        self.select_popup.draw(self.canvas)

    # -- expanded <select> (size / multiple) ------------------------------

    def _focused_listbox(self):
        """The expanded <select> holding form focus, or None. This is what
        decides whether an arrow key belongs to a listbox or to the page."""
        tab = self.active_tab
        node = tab.focused_input if tab else None
        if node is not None and node.tag == "select" \
                and "disabled" not in node.attributes and listbox_rows(node):
            return node
        return None

    def _listbox_move(self, delta, to_end=False, last=False):
        """Walk a focused listbox; True when the key was ours."""
        node = self._focused_listbox()
        if node is None:
            return False
        self.active_tab.move_listbox(node, delta, to_end=to_end, last=last)
        self._draw_page()
        return True

    def _listbox_commit(self):
        """Space on a focused multi-choice listbox toggles the row the
        keyboard is on; True when the key was ours."""
        node = self._focused_listbox()
        if node is None or "multiple" not in node.attributes:
            return False
        self.active_tab.toggle_listbox_active(node)
        self._draw_page()
        return True

    def _listbox_wheel(self, x, y, delta):
        """Give a wheel turn over a listbox to the listbox.

        A listbox with more options than rows is its own scrolling area, so
        the page must not slide out from under the reader's pointer. When
        there is nothing left to scroll the turn is handed back, which is
        what stops a short list from trapping the wheel.
        """
        tab = self.active_tab
        if not tab or x < 0 or y < self.chrome_height():
            return False
        node = tab.listbox_at(x, y - self.chrome_height() + tab.scroll)
        if node is None or "disabled" in node.attributes:
            return False
        if not tab.scroll_listbox(node, 1 if delta > 0 else -1):
            return False
        self._draw_page()
        return True

    def _context_items(self, x, y):
        """Build the context-menu entries for a right-click at (x, y)."""
        tab = self.active_tab
        if not tab or y < self.chrome_height():
            return [
                ("Back", self._back, bool(tab and tab.history)),
                ("Forward", self._forward, bool(tab and tab.future)),
                ("Reload", self._reload, bool(tab)),
                None,
                ("New Tab", lambda: self.new_tab("about:blank"), True),
                ("Close Tab", self.close_tab, len(self.tabs) > 1),
                None,
                ("Home", self._home, bool(tab)),
                ("Bookmark This Page", self._toggle_bookmark,
                 bool(tab and self._bookmark_key(tab.url))),
                ("View Source", self._view_source,
                 bool(tab and isinstance(tab.url, URL))),
                ("History", self._open_history_page, bool(tab)),
            ]
        doc_y = y - self.chrome_height()
        node = tab._node_at(x, doc_y)
        href = tab._enclosing_link(node)
        img_src = self._enclosing_image(node)
        items = []
        if tab.selected_text():
            items.append(("Copy", self._copy_selection, True))
            items.append(None)
        if href:
            try:
                resolved = tab.base_url.resolve(href) if tab.base_url \
                    else tab.url.resolve(href)
            except Exception:  # noqa: BLE001 - malformed href: skip link actions
                resolved = None
            if resolved is not None:
                items.append(("Open Link",
                              lambda r=resolved: self._navigate(tab, r), True))
                items.append(("Open Link in New Tab",
                              lambda r=resolved: self.new_tab(str(r)), True))
                items.append(("Download Link",
                              lambda r=resolved: self._download(r),
                              getattr(resolved, "scheme", "")
                              in ("http", "https")))
            items.append(("Copy Link Address",
                          lambda h=href: self._copy_text(h), True))
            items.append(None)
        if img_src:
            try:
                img_url = tab.base_url.resolve(img_src) if tab.base_url \
                    else URL(img_src)
            except Exception:  # noqa: BLE001 - malformed src: skip image actions
                img_url = None
            if img_url is not None:
                items.append(("Open Image",
                              lambda u=img_url: self._navigate(tab, u), True))
                items.append(("Download Image",
                              lambda u=img_url: self._download(u),
                              getattr(img_url, "scheme", "")
                              in ("http", "https")))
                items.append(("Copy Image URL",
                              lambda u=str(img_url): self._copy_text(u), True))
            items.append(None)
        items.extend([
            ("Back", self._back, bool(tab.history)),
            ("Forward", self._forward, bool(tab.future)),
            ("Reload", self._reload, True),
            None,
            ("Bookmark This Page", self._toggle_bookmark,
             bool(self._bookmark_key(tab.url))),
            ("View Source", self._view_source, isinstance(tab.url, URL)),
            ("Copy Page URL",
             lambda u=str(tab.url): self._copy_text(u),
             bool(tab.url and not isinstance(tab.url, _AboutURL))),
            None,
            ("New Tab", lambda: self.new_tab("about:blank"), True),
            ("Close Tab", self.close_tab, len(self.tabs) > 1),
        ])
        return items

    def _on_key(self, e):
        if self.select_popup.open_:
            # Arrows, Enter and Escape have their own bindings; everything
            # else is swallowed so it cannot leak into the page behind.
            return
        if self.context_menu.open_:
            keysym = getattr(e, "keysym", "")
            if keysym == "Up":
                self.context_menu.move(-1)
                self.context_menu.draw(self.canvas)
            elif keysym == "Down":
                self.context_menu.move(1)
                self.context_menu.draw(self.canvas)
            elif keysym in ("Return", "KP_Enter"):
                cb = self.context_menu.activate()
                self.context_menu.close()
                self.draw()
                if cb:
                    cb()
            return
        if self.focus == "address":
            self._address_key(e)
            return
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        key = getattr(e, "keysym", "").lower()
        if ctrl and key == "c":
            self._copy_selection()
            return
        if ctrl and key == "v" and self.active_tab \
                and self.active_tab.focused_input is not None:
            # Paste reaches a page field the same way it reaches the address
            # bar: the modified keypress carries no printable char, so it has
            # to be recognised here or nothing at all happens.
            if self.active_tab.insert_text(self._clipboard_text()):
                self._draw_page()
            return
        # Toes get first crack at keys when no address bar has focus, but
        # only consume the key when a toe explicitly returns True (a False
        # return means "not handled").
        if any(r is True for r in toes.dispatch(
                self.toe_contexts, "on_keypress", e)):
            return
        # Space on a focused multi-choice listbox toggles a row rather than
        # typing a space nowhere.
        if e.char == " " and self._listbox_commit():
            return
        # Typing into a focused form field.
        if self.active_tab and self.active_tab.focused_input and \
                len(e.char) == 1 and e.char.isprintable():
            self.active_tab.type_char(e.char)
            self._draw_page()

    def _on_backspace(self, e):
        if self.focus == "address":
            self._address_backspace()
            self._draw_chrome()
            return
        if self.active_tab and self.active_tab.delete_char():
            self._draw_page()

    def _on_delete(self, e):
        if self.focus == "address":
            self._address_forward_delete()
            self._draw_chrome()

    def _address_key(self, e):
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        if ctrl:
            k = getattr(e, "keysym", "").lower()
            if k == "a":
                self._address_select_all()
                self._draw_chrome()
                return
            if k == "c":
                self._address_copy()
                return
            if k == "x":
                self._address_cut()
                self._draw_chrome()
                return
            if k == "v":
                self._address_paste()
                self._draw_chrome()
                return
            if k == "u":
                self.address_text = ""
                self.address_caret = 0
                self.address_sel = None
                self._address_ensure_visible()
                self._draw_chrome()
                return
        if len(e.char) == 1 and ord(e.char) >= 32 and e.char.isprintable():
            self._address_insert(e.char)
            self._draw_chrome()

    def _copy_selection(self):
        """Copy the active tab's selected text to the system clipboard."""
        if not self.active_tab:
            return
        text = self.active_tab.selected_text()
        if not text:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(text)

    def _publish_primary(self, text):
        """Offer a finished mouse selection as X11's PRIMARY selection.

        On X, selecting with the mouse is itself a copy -- middle-click
        pastes it -- and that is a separate selection from the CLIPBOARD an
        explicit Ctrl+C claims, so a page selection must not overwrite what
        was copied earlier. Every other platform ignores this: the base
        window's hook does nothing and macOS does not override it, because a
        NSPasteboard only changes when the user asks it to.
        """
        if not text:
            return
        try:
            self.window.on_primary_set(text)
        except Exception as exc:  # noqa: BLE001 - a clipboard is not the page
            self.window.on_callback_error("primary selection", exc)

    def _address_bar_x(self):
        """Canvas x where the address-bar text starts (after toe buttons
        and the bookmark star)."""
        return 136 + self._toe_buttons_offset() + 30 + 10

    def _address_bar_right(self):
        """Canvas x where the address bar ends, before the hamburger
        settings button (8px window margin + the button + a 6px gap)."""
        return self.canvas.winfo_width() - MENU_BTN_W - 14

    def _address_reset_from_tab(self):
        url = str(self.active_tab.url) if \
            (self.active_tab and self.active_tab.url and
             not isinstance(self.active_tab.url, _AboutURL)) else ""
        # Nothing should put a break in a URL, but this is the one route into
        # the bar that does not go through _address_insert, and a URL that
        # came in over the wire is not ours to trust.
        url = self._flatten_address_text(url).strip()
        self.address_text = url
        self.address_caret = len(url)
        self.address_sel = None
        self.address_view = 0

    def _caret_from_x(self, x):
        """Index of the address-bar caret under a canvas x coordinate."""
        font = self.chrome_font
        text = self.address_text
        rel = max(0.0, x - self._address_bar_x() + self.address_view)
        i = 0
        while i < len(text) and _measure(font, text[:i + 1]) <= rel:
            i += 1
        return i

    def _set_address_caret_from_x(self, x):
        self.address_caret = self._caret_from_x(x)

    def _address_selection(self):
        if self.address_sel is None:
            return None
        s, e = self.address_sel
        s = max(0, min(s, len(self.address_text)))
        e = max(0, min(e, len(self.address_text)))
        if s == e:
            return None
        return (s, e) if s < e else (e, s)

    def _address_delete_selection(self):
        sel = self._address_selection()
        if sel is None:
            return False
        s, e = sel
        self.address_text = self.address_text[:s] + self.address_text[e:]
        self.address_caret = s
        self.address_sel = None
        return True

    @staticmethod
    def _flatten_address_text(text):
        """`text` with everything that breaks a line folded away.

        The address bar is one line of text drawn inside one box, and the
        renderer honours a line break wherever it finds one: paste a couple
        of bullet points in and the second line is painted below the box,
        floating over the page. A URL cannot hold a break either, so there
        is nothing to preserve.

        The break family is bigger than "\\n". A copy out of rendered text
        can carry CR and CRLF from a Windows or classic-Mac source, the
        vertical tab and form feed, NEL (U+0085) and the Unicode line and
        paragraph separators U+2028/U+2029 -- `str.splitlines` knows all of
        them, so the split is delegated to it rather than spelled out here
        and left to rot. The tab goes the same way: not valid in a URL, and
        drawn as a missing glyph.

        A break becomes a *space*, not nothing. This bar is a search box as
        much as it is a URL bar, and welding "...bullet one" onto "bullet
        two..." quietly corrupts the query, where a space is what the line
        break meant in the first place. Blank lines and the whitespace
        hugging each break collapse into that one space, so a wrapped
        paragraph does not arrive full of gaps.

        Text with no break in it is returned as it stands, spaces included:
        this runs on every inserted character, and a bar that eats the
        space bar is worse than the bug it fixes.
        """
        lines = text.replace("\t", " ").splitlines()
        if len(lines) <= 1:
            return lines[0] if lines else ""
        return " ".join(part for part in (line.strip() for line in lines)
                        if part)

    def _address_insert(self, text):
        # Every route into the address text -- typing, pasting, whatever a
        # future one is -- lands here, so this is where a line break is
        # stopped rather than at each caller.
        text = self._flatten_address_text(text)
        self._address_delete_selection()
        self.address_text = (self.address_text[:self.address_caret] + text
                             + self.address_text[self.address_caret:])
        self.address_caret += len(text)
        self.address_sel = None
        self._address_ensure_visible()

    def _address_backspace(self):
        if self._address_delete_selection():
            return
        if self.address_caret > 0:
            self.address_text = (self.address_text[:self.address_caret - 1]
                                 + self.address_text[self.address_caret:])
            self.address_caret -= 1
            self._address_ensure_visible()

    def _address_forward_delete(self):
        if self._address_delete_selection():
            return
        if self.address_caret < len(self.address_text):
            self.address_text = (self.address_text[:self.address_caret]
                                 + self.address_text[self.address_caret + 1:])
            self._address_ensure_visible()

    def _address_select_all(self):
        self.address_caret = len(self.address_text)
        self.address_sel = (0, len(self.address_text))
        self._address_ensure_visible()

    def _address_move_caret(self, delta, extend=False):
        lo, hi = 0, len(self.address_text)
        if extend:
            if self.address_sel is None:
                self.address_sel = (self.address_caret, self.address_caret)
            anchor = self.address_sel[0]
            self.address_caret = max(lo, min(hi, self.address_caret + delta))
            self.address_sel = (anchor, self.address_caret)
        else:
            self.address_caret = max(lo, min(hi, self.address_caret + delta))
            self.address_sel = None
        self._address_ensure_visible()

    def _address_copy(self):
        sel = self._address_selection()
        if sel is None:
            return
        s, e = sel
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.address_text[s:e])
        except CanvasError:
            pass

    def _address_cut(self):
        if self._address_selection() is None:
            return
        self._address_copy()
        self._address_delete_selection()

    def _clipboard_text(self):
        """The clipboard's text, or "" when there is none to be had. Reading
        raises for an empty clipboard and for one whose owner offers no text
        flavour, and neither deserves a traceback in the user's face."""
        try:
            return self.window.clipboard_get()
        except CanvasError:
            return ""

    def _address_paste(self):
        # A paste arrives as a block, so the whitespace around the block goes
        # too -- a URL copied off a page usually brings a trailing newline
        # with it, and one typed space is not what the user asked for.
        data = self._flatten_address_text(self._clipboard_text()).strip()
        if data:
            self._address_insert(data)

    def _address_ensure_visible(self):
        """Horizontal scroll of the address text so the caret stays in view."""
        font = self.chrome_font
        caret_x = _measure(font, self.address_text[:self.address_caret])
        box_w = max(40, self._address_bar_right() - self._address_bar_x() - 8)
        if caret_x < self.address_view:
            self.address_view = max(0, caret_x - 8)
        elif caret_x > self.address_view + box_w:
            self.address_view = caret_x - box_w + 8

    def _on_escape(self, e):
        # A drag in progress owns Escape: the key that backs out of a menu or
        # a drop-down also puts a half-carried tab back where it came from,
        # and it has to be asked first because the drag is the thing the
        # pointer and the eye are both on.
        if self._cancel_tab_drag():
            return
        if self.select_popup.open_:
            self._close_select_popup()
        elif self.context_menu.open_:
            self.context_menu.close()
            self.draw()
        elif self.focus == "address":
            self.focus = None
            self._draw_chrome()
        elif self.active_tab and self.active_tab.focused_input:
            self.active_tab.blur_input()
            self._draw_page()

    def _on_enter(self, e):
        if self.select_popup.open_:
            self._commit_select()
            return
        if self.focus != "address" and self._listbox_commit():
            return
        if self.focus == "address":
            if not self.address_text.strip():
                return
            self.focus = None
            query = self.address_text.strip()
            if query == "about:blank":
                dest = _AboutURL(theme=self.theme, apply=self.apply_shoe,
                                 active=lambda: self.shoe)
            else:
                if not self._looks_like_url(query):
                    query = settings.search_url(
                        self.settings.get("search_engine", "duckduckgo"),
                        query)
                elif "://" not in query and not query.startswith(
                        ("file:", "data:", "view-source:", "about:")):
                    query = "https://" + query
                dest = query
            if self.active_tab:
                self._navigate(self.active_tab, self._coerce_url(dest))
            return
        # Enter in a focused form field submits its form.
        if self.active_tab and self.active_tab.focused_input:
            action = self.active_tab.submit_focused()
            self.active_tab.blur_input()
            if action:
                self._navigate(self.active_tab, action.url,
                               payload=action.payload)

    @staticmethod
    def _looks_like_url(text):
        if " " in text.strip():
            return False
        if text.startswith(("http://", "https://", "file:", "data:",
                            "view-source:", "about:")):
            return True
        if text.startswith("."):
            return False
        if ":" in text:
            host, _, rest = text.partition(":")
            if rest.isdigit():
                return True  # hostname:port / IPv4:port / [v6]:port
            if "]" in text and text.startswith("["):
                return True
        return "." in text

    def _focus_address(self):
        self.focus = "address"
        if self.active_tab:
            self.active_tab.blur_input()
        self._address_reset_from_tab()
        self._address_select_all()
        self.draw()

    @staticmethod
    def _bookmark_key(url):
        if not url or isinstance(url, (_AboutURL, _BookmarksURL)):
            return None
        return str(url)

    def _is_bookmarked(self, url):
        key = self._bookmark_key(url)
        return bool(key and key in self.bookmarks)

    def _toggle_bookmark(self):
        if not self.active_tab:
            return
        key = self._bookmark_key(self.active_tab.url)
        if not key:
            self.active_tab.status = "This page can't be bookmarked"
            self._draw_chrome()
            return
        if key in self.bookmarks:
            self.bookmarks.remove(key)
            self.active_tab.status = "Bookmark removed"
        else:
            self.bookmarks.append(key)
            self.active_tab.status = "Bookmarked"
        self._save_bookmarks()
        self._draw_chrome()

    def _menu_items(self):
        """Items for the hamburger settings menu: the about pages and the
        toe hub, each opening in a fresh tab."""
        return [
            ("Settings", lambda: self.new_tab("about:settings"), True),
            ("Bookmarks", lambda: self.new_tab("about:bookmarks"), True),
            ("History", lambda: self.new_tab("about:history"), True),
            ("Downloads", self._toggle_downloads, True),
            None,
            ("Manage Shoes", lambda: self.new_tab("about:shoes"), True),
            None,
            ("Manage Toes", lambda: self.new_tab("toe://hub"), True),
        ]

    def _toggle_menu(self):
        """Open (or close) the hamburger settings menu under its button."""
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()
            return
        w = self.canvas.winfo_width()
        menu_x = w - MENU_BTN_W - 8
        self.context_menu.open(menu_x + MENU_BTN_W, 0, self._menu_items(),
                               w, self.canvas.winfo_height())
        self.context_menu.anchor = "burger"
        self._reanchor_menu()
        self.draw()

    def _reanchor_menu(self):
        """Put the open settings menu back under the hamburger button.

        The button is pinned to the right edge of the window, so a resize
        moves it out from under the menu; right-click menus are anchored to
        their click point and never move. One rule for both places the menu
        is positioned (open and re-anchor) keeps them from drifting apart.
        """
        menu = self.context_menu
        if not menu.open_ or menu.anchor != "burger":
            return
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        menu_x = w - MENU_BTN_W - 8
        menu.x = max(2, min(menu_x + MENU_BTN_W - menu.width,
                            w - menu.width - 2))
        menu.y = max(2, min(toes.band_height(self.chrome_bands()) + 74,
                            h - menu.height - 2))

    def _history_snapshot(self):
        tab = self.active_tab
        if not tab:
            return {"back": [], "current": "", "forward": []}
        return {
            "back": [str(url) for url, _scroll in tab.history],
            "current": str(tab.url) if tab.url else "",
            "forward": [str(url) for url, _scroll in reversed(tab.future)],
        }

    def _open_history_page(self):
        if self.active_tab:
            self.active_tab.load(self._coerce_url("about:history"))
            self.draw()

    def _open_shoes_page(self):
        if self.active_tab:
            self.active_tab.load(self._coerce_url("about:shoes"))
            self.draw()

    def _cycle_tab(self, step):
        if not self.tabs or not self.active_tab:
            return "break"
        self._dismiss_select_popup()
        i = self.tabs.index(self.active_tab)
        self.active_tab = self.tabs[(i + step) % len(self.tabs)]
        self.draw()
        return "break"

    def _coerce_url(self, raw):
        if not isinstance(raw, str):
            return raw
        text = raw.strip().lower()
        active = lambda: self.shoe
        if text in ("about:blank", "about:newtab"):
            return _AboutURL(lambda: list(self.bookmarks), theme=self.theme,
                             apply=self.apply_shoe, active=active)
        if text == "about:bookmarks":
            return _BookmarksURL(lambda: list(self.bookmarks),
                                 theme=self.theme, apply=self.apply_shoe,
                                 active=active)
        if text == "about:history":
            return _HistoryURL(self._history_snapshot, theme=self.theme,
                               apply=self.apply_shoe, active=active)
        if text == "about:shoes":
            return _ShoesURL(apply=self.apply_shoe, theme=self.theme,
                             active=active)
        if text.startswith("about:shoes/"):
            return _ShoesApplyURL(text[len("about:shoes/"):],
                                  apply=self.apply_shoe, theme=self.theme,
                                  active=active)
        if text == "about:settings":
            return _SettingsURL(settings_provider=lambda: self.settings,
                                apply=self._apply_setting, theme=self.theme,
                                active=active)
        if text.startswith("about:settings/"):
            rest = text[len("about:settings/"):]
            key, _, value = rest.partition("/")
            return _SettingsApplyURL(key, value,
                                     settings_provider=lambda: self.settings,
                                     apply=self._apply_setting,
                                     theme=self.theme, active=active)
        return raw

    @staticmethod
    def _sanitize_bookmarks(values):
        if not isinstance(values, list):
            return []
        out = []
        seen = set()
        for item in values:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value.startswith("about:"):
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _load_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, "r", encoding="utf8") as f:
                data = json.load(f)
            return self._sanitize_bookmarks(data)
        except (OSError, json.JSONDecodeError):
            return []

    def _save_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, "w", encoding="utf8") as f:
                json.dump(self.bookmarks, f, indent=2)
        except OSError:
            pass

    def _back(self):
        self._dismiss_select_popup()
        if self.active_tab:
            self.active_tab.go_back()
            self.draw()

    def _forward(self):
        self._dismiss_select_popup()
        if self.active_tab:
            self.active_tab.go_forward()
            self.draw()

    def _reload(self):
        # Pass the URL object (not its string) so internal pages like the
        # about:blank welcome page reload without being re-parsed as a URL.
        # `refresh=True` bypasses the response cache so the page actually
        # re-fetches.
        if self.active_tab and self.active_tab.url:
            self.active_tab.load(self.active_tab.url, push=False, refresh=True)
            self.draw()

    def _home(self):
        if self.active_tab:
            self.active_tab.load(_AboutURL(theme=self.theme, apply=self.apply_shoe,
                                     active=lambda: self.shoe))
            self.active_tab.status = "Type a URL and press Enter"
            self.draw()

    def _next_tab(self, direction):
        if not self.tabs:
            return
        self._dismiss_select_popup()
        idx = self.tabs.index(self.active_tab)
        self.active_tab = self.tabs[(idx + direction) % len(self.tabs)]
        self.draw()

    def _navigate(self, tab, url, payload=None):
        """Load `url` on `tab`; image fetching + repaint happen when the
        document is ready (see Tab._complete_load)."""
        self._cancel_momentum()
        self._dismiss_select_popup()
        tab.load(url, payload=payload)
        self.draw()

    # -- painting --------------------------------------------------------

    def draw(self):
        """Full repaint: wipe the canvas and redraw page + chrome. Used for
        things that invalidate everything (navigation, resize, tab switches);
        cheaper layered paths exist for common cases (see _draw_page,
        _draw_page_region, _draw_chrome)."""
        self._repaint_needed = False
        self.canvas.delete("all")
        c = self.canvas
        chrome = self.chrome_height()
        if self.active_tab:
            self.active_tab.tab_height = self.tab_height()
            self.active_tab.draw(c, chrome)
        # Toe page overlays: tagged so _draw_page can clear + re-run them on
        # scroll instead of leaving stale copies behind.
        self._dispatch_toe_draw(c, chrome)
        self._draw_chrome()
        self.downloads_panel.draw(self.canvas)
        self.context_menu.draw(self.canvas)
        self._draw_select_popup()
        self._update_title()

    def _update_title(self):
        self.window.title(
            (self.active_tab.title if self.active_tab else "FeetBrowser")
            + " — FeetBrowser")

    def _dispatch_toe_draw(self, c, chrome):
        """Run toe on_draw hooks, tagging new items so a repaint clears them."""
        before = set(c.find_all())
        toes.dispatch(self.toe_contexts, "on_draw", c, chrome)
        for item_id in c.find_all():
            if item_id not in before:
                c.addtag_withtag("toe-draw", item_id)

    def _schedule_scroll_repaint(self):
        """Coalesce scroll repaints: at most one pending, run at the next
        frame at the latest scroll position. Fast scrolls (scrollbar drags,
        wheel bursts) then pay one full redraw per frame instead of one per
        input event, which is what froze the UI when scrolling faster than
        the renderer could keep up."""
        if self._scroll_repaint_pending:
            return
        self._scroll_repaint_pending = True
        self.window.after(0, self._run_scroll_repaint)

    def _run_scroll_repaint(self):
        self._scroll_repaint_pending = False
        tab = self.active_tab
        if tab is None:
            return
        # Re-flatten the display list for the latest offset (sticky/fixed
        # elements move with scroll), then repaint once. Intermediate scroll
        # positions between this frame and the last were already skipped.
        tab.repaint()
        self._draw_page()

    def _draw_page(self):
        """Repaint the page layer, then re-assert the chrome on top of it.

        The canvas paints in insertion order, so page commands re-executed
        here land after the chrome that was drawn earlier and would cover it
        -- once the page is scrolled, its full-viewport background rectangles
        start above the window top and span the whole chrome strip. Re-drawing
        the chrome after the page is what keeps the nav bar over the page, and
        it is the cheap layer: a few dozen items, against hundreds of page
        commands."""
        self._repaint_needed = False
        c = self.canvas
        c.delete("toe-draw")
        if self.active_tab:
            self.active_tab.tab_height = self.tab_height()
            self.active_tab.draw(c, self.chrome_height())
        chrome = self.chrome_height()
        self._dispatch_toe_draw(c, chrome)
        # An open drop-down (or the downloads panel) must stay on top of the
        # page it floats over; _draw_chrome ends by re-drawing both.
        self._draw_chrome()
        self._update_title()

    def _draw_page_region(self, rect):
        """Repaint only the damaged rectangle `rect` (viewport page-space
        coords) of the page, plus whatever selection overlaps it."""
        if self.active_tab:
            self.active_tab.draw(self.canvas, self.chrome_height(), rect)

    def c(self, key):
        """Return the current shoe's color for a chrome role. Always defined:
        missing keys fall back to the default shoe's palette."""
        return self.theme.get(key, shoes.SHOES[shoes.DEFAULT_SHOE].get(key))

    def apply_shoe(self, name):
        """Switch to the named shoe: update the palette, persist, and repaint
        the chrome so the change applies instantly."""
        if shoes.find(name) is None:
            return
        self.shoe = shoes.find(name)
        self.theme = shoes.merge(shoes.resolve(self.shoe))
        shoes.save(self.shoe)
        self._repaint_needed = True

    def scroll_step(self):
        """Pixels per wheel notch / arrow key, from the saved settings."""
        return int(self.settings.get("scroll_speed", SCROLL_STEP))

    def _apply_setting(self, key, value):
        """Persist a browser setting and apply its side effects live."""
        setting = settings.by_key(key)
        if setting is None:
            return
        value = setting.coerce(value)
        self.settings[key] = value
        settings.save({key: value})
        if key == "search_engine":
            pass  # read again at the next address-bar search
        elif key == "scroll_speed":
            pass  # read again on the next scroll
        elif key == "momentum":
            if not value:
                self._cancel_momentum()
        elif key == "momentum_strength":
            pass  # read again when the next coast starts
        elif key == "show_link_preview":
            pass  # read again on the next hover
        # Re-render internal pages (welcome/shoes/bookmarks/history) so their
        # themed colors update too.
        internal = (_AboutURL, _BookmarksURL, _HistoryURL, _ShoesURL)
        for tab in self.tabs:
            if isinstance(tab.url, internal):
                tab.load(tab.url, push=False)
        self.draw()

    def _draw_chrome(self):
        """Repaint only the chrome (tabs, toolbar, address bar, status,
        scrollbar, spinner), leaving the page layer intact. The chrome items
        created here are tagged "chrome" afterwards so a later chrome repaint
        can drop the stale ones."""
        self._repaint_needed = False
        c = self.canvas
        c.delete("chrome")
        before = set(c.find_all())
        chrome = self.chrome_height()
        # Chrome background covers page content that scrolled up under it.
        c.create_rectangle(0, 0, c.winfo_width(), chrome,
                           fill=self.c("chrome_bg"), width=0)
        # Toe chrome bands paint on top of the chrome background.
        bands = self.chrome_bands()
        if bands:
            toes.dispatch(self.toe_contexts, "on_chrome_draw", c, bands)
        self._draw_tabs()
        self._draw_toolbar()
        self._draw_log()
        self._draw_toe_buttons()
        self._draw_status()
        self._draw_scrollbar()
        self._draw_spinner()
        for item_id in c.find_all():
            if item_id not in before:
                c.addtag_withtag("chrome", item_id)
        # After the tagging, so neither the drop-down nor the downloads
        # panel is mistaken for chrome and wiped by the next chrome repaint.
        self.downloads_panel.draw(self.canvas)
        self._draw_select_popup()
        # The settings menu hangs off the toolbar into the chrome band, and
        # the context menu floats over the page; a chrome repaint would paint
        # over either, so draw them back on top.
        self.context_menu.draw(self.canvas)

    def _repaint_selection(self):
        """Redraw just the selection highlight layer. The page behind the
        highlight never changes while dragging to select, so re-executing page
        commands (the old approach) only slowed the drag and could blank the
        canvas on heavy pages."""
        tab = self.active_tab
        if not tab or tab.selection is None:
            self._draw_page()
            return
        c = self.canvas
        c.delete("selection")
        tab._draw_selection(c, self.chrome_height())

    def _draw_log(self):
        """Draw the load-error strip under the toolbar: the most recent
        network/JS failure (if any) for the active tab, plus a count."""
        c = self.canvas
        tab = self.active_tab
        top = toes.band_height(self.chrome_bands()) + CHROME_HEIGHT
        c.create_rectangle(0, top, c.winfo_width(), top + LOG_HEIGHT,
                           fill=self.c("log_bg"), width=0)
        c.create_line(0, top, c.winfo_width(), top, fill=self.c("log_border"))
        if not tab or not tab.net_errors:
            return
        latest = tab.net_errors[-1]
        total = len(tab.net_errors)
        msg = f"[{total} load error{'s' if total != 1 else ''}] {latest}"
        width = max(0, c.winfo_width() - 16)
        font = get_font(10, "normal", "roman", "Helvetica")
        if _measure(font, msg) > width:
            while msg and _measure(font, msg + "\u2026") > width:
                msg = msg[:-1]
            msg += "\u2026"
        c.create_text(8, top + LOG_HEIGHT / 2, text=msg, anchor="w",
                      font=font, fill=self.c("log_text"))

    def _draw_tabs(self):
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        c.create_rectangle(0, top, c.winfo_width(), top + 40,
                           fill=self.c("tab_bar"), width=0)
        for tab, x0, dragged in self._tab_positions():
            active = tab is self.active_tab
            # A tab being carried is drawn a couple of pixels taller than the
            # ones it is passing over, which is the whole of "picked up": it
            # is already painted last, so the extra lip is what makes the
            # overlap read as one tab above another.
            c.create_rectangle(x0, top + (2 if dragged else 4),
                               x0 + TAB_WIDTH, top + 40,
                               fill=self.c("tab_active" if active
                                           else "tab_inactive"),
                               width=0)
            title = tab.title or "New Tab"
            # Tabs are TAB_WIDTH wide; fit the title in the space before the
            # close box (which starts at x0 + TAB_WIDTH - TAB_CLOSE_W) so long
            # page titles never spill out past the tab edge.
            title_w = TAB_WIDTH - 20
            if _measure(self.chrome_font, title) > title_w:
                t = title
                while t and _measure(self.chrome_font, t + "…") > title_w:
                    t = t[:-1]
                title = t + "…"
            c.create_text(x0 + 10, top + 20, text=title, anchor="w",
                          font=self.chrome_font, fill=self.c("tab_text"))
            c.create_text(x0 + TAB_WIDTH - 10, top + 20, text="×",
                          font=self.bold_font, fill=self.c("tab_close"))
        # New-tab button, to the right of the last tab (browser convention).
        nx = self._new_tab_x()
        c.create_rectangle(nx, top + 4, nx + NEW_TAB_W, top + 40,
                           fill=self.c("tab_inactive"), width=0)
        c.create_text(nx + NEW_TAB_W / 2, top + 20, text="+",
                      font=self.bold_font, fill=self.c("plus_button"))

    def _draw_toolbar(self):
        c = self.canvas

        def btn(x, glyph, enabled):
            c.create_rectangle(x, top + 48, x + 26, top + 72,
                               outline=self.c("button_border"),
                               fill=self.c("button_bg"), width=1)
            c.create_text(x + 13, top + 60, text=glyph,
                          fill=self.c("button_glyph" if enabled
                                      else "button_glyph_disabled"),
                          font=self.bold_font)

        top = toes.band_height(self.chrome_bands())
        tab = self.active_tab
        btn(8, "‹", bool(tab and tab.history))
        btn(40, "›", bool(tab and tab.future))
        btn(72, "↻", bool(tab))
        btn(104, "⌂", bool(tab))
        marked = bool(tab and self._is_bookmarked(tab.url))
        btn(136 + self._toe_buttons_offset(), "★" if marked else "☆",
            bool(tab))

        # Address bar (after the toe buttons and bookmark star), ending
        # before the hamburger settings button.
        addr_x = 136 + self._toe_buttons_offset() + 30
        c.create_rectangle(addr_x, top + 48, self._address_bar_right(),
                           top + 72,
                           outline=self.c("addr_focus_border"
                                          if self.focus == "address"
                                          else "addr_border"),
                           fill=self.c("addr_bg"),
                           width=2 if self.focus == "address" else 1)
        if self.focus == "address":
            self._draw_address_editor(c, addr_x, top)
        else:
            url = ""
            if tab and tab.url and not isinstance(tab.url, _AboutURL):
                # The unfocused bar echoes the tab's URL rather than the text
                # the user typed, and a link href can carry a literal break
                # (`&#10;` survives the parser and the URL), so it is flattened
                # here too or it escapes the box by the back door.
                url = self._flatten_address_text(str(tab.url))
            c.create_text(addr_x + 10, top + 60, text=url, anchor="w",
                          font=self.chrome_font, fill=self.c("addr_text"))

        # Hamburger settings button (right of the address bar).
        btn_x = self.canvas.winfo_width() - MENU_BTN_W - 8
        c.create_rectangle(btn_x, top + 48, btn_x + MENU_BTN_W, top + 72,
                           outline=self.c("button_border"),
                           fill=self.c("button_bg"), width=1)
        for bar_y in (54, 60, 66):
            c.create_rectangle(btn_x + 6, top + bar_y, btn_x + MENU_BTN_W - 6,
                               top + bar_y + 2, fill=self.c("button_glyph"),
                               width=0)

    def _draw_address_editor(self, c, addr_x, top):
        """Paint the focused address bar: text (with horizontal scroll),
        selection highlight, and the caret."""
        font = self.chrome_font
        text = self.address_text
        x0 = addr_x + 10
        x1 = self._address_bar_right() - 8
        if x1 - x0 < 30:
            x1 = x0 + 30
        sel = self._address_selection()
        view = self.address_view

        if not text:
            c.create_text(x0, top + 60, text="Type a URL or search term…",
                          anchor="w", font=font, fill=self.c("addr_placeholder"))
            c.create_line(x0, top + 52, x0, top + 68, fill=self.c("caret"))
            return

        def char_x(i):
            return x0 + (_measure(font, text[:i]) - view)

        # Visible slice of the text.
        start = 0
        while start < len(text) and _measure(font, text[:start + 1]) <= view:
            start += 1
        end = start
        while end < len(text) and _measure(font, text[start:end + 1]) <= (x1 - x0):
            end += 1
        if self.address_caret < start:
            start = self.address_caret
        if self.address_caret > end:
            end = self.address_caret

        # Selection highlight.
        if sel is not None and sel[1] > start and sel[0] < end:
            c.create_rectangle(char_x(max(start, sel[0])), top + 51,
                               char_x(min(end, sel[1])), top + 69,
                               fill=self.c("accent"), width=0)

        y = top + 60
        if sel is not None and sel[0] < end and sel[1] > start:
            s1, s2 = max(start, sel[0]), min(end, sel[1])
            part1, part2, part3 = text[start:s1], text[s1:s2], text[s2:end]
            if part1:
                c.create_text(char_x(start), y, text=part1, anchor="w",
                              font=font, fill=self.c("addr_text"))
            if part2:
                c.create_text(char_x(s1), y, text=part2, anchor="w",
                              font=font, fill=self.c("addr_bg"))
            if part3:
                c.create_text(char_x(s2), y, text=part3, anchor="w",
                              font=font, fill=self.c("addr_text"))
        else:
            c.create_text(char_x(start), y, text=text[start:end], anchor="w",
                          font=font, fill=self.c("addr_text"))

        # Caret.
        cx = char_x(self.address_caret)
        c.create_line(cx, top + 52, cx, top + 68, fill=self.c("caret"))

    def _toe_buttons(self):
        return [btn for ctx in self.toe_contexts
                for btn in (ctx.call("buttons") or [])]

    def _toe_buttons_offset(self):
        return len(self._toe_buttons()) * 30

    def _draw_toe_buttons(self):
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        x = 136
        for btn in self._toe_buttons():
            c.create_rectangle(x, top + 48, x + 26, top + 72,
                               outline=self.c("button_border"),
                               fill=self.c("toe_btn_bg"), width=1)
            c.create_text(x + 13, top + 60, text=btn.glyph[:2],
                          fill=self.c("button_glyph"), font=self.bold_font)
            x += 30

    def _draw_status(self):
        c = self.canvas
        c.delete("statusbar")
        h = c.winfo_height()
        c.create_rectangle(0, h - 22, c.winfo_width(), h,
                           fill=self.c("status_bg"), width=0,
                           tags=("statusbar",))
        c.create_line(0, h - 22, c.winfo_width(), h - 22,
                      fill=self.c("status_border"), tags=("statusbar",))
        status = self.active_tab.status if self.active_tab else ""
        c.create_text(8, h - 11, text=status[:200], anchor="w",
                      font=get_font(11, "normal", "roman", "Helvetica"),
                      fill=self.c("status_text"), tags=("statusbar",))

    def _scrollbar_metrics(self):
        """Where the scrollbar is right now, or None when there is not one.

        One description of the geometry, used both to draw the thumb and to
        decide what a press on it means -- drawing and hit-testing that each
        work it out for themselves drift apart, and a bar you cannot grab
        where you can see it is the bug this is here to avoid.

        Returns (track_x, track_top, track_h, thumb_top, thumb_h, span),
        where `span` is the scroll offset the bottom of the track stands for.
        """
        tab = self.active_tab
        if not tab:
            return None
        view = self.tab_height()
        total = tab.content_height()
        if total <= view:
            return None  # the page fits: nothing to scroll and no bar drawn
        track_x = self.canvas.winfo_width() - SCROLLBAR_RIGHT
        track_top = self.chrome_height()
        track_h = view
        thumb_h = max(SCROLLBAR_MIN_THUMB, track_h * (view / total))
        thumb_h = min(thumb_h, track_h)
        span = total - view
        thumb_top = track_top + (track_h - thumb_h) * (tab.scroll / span)
        return track_x, track_top, track_h, thumb_top, thumb_h, span

    def _draw_scrollbar(self):
        c = self.canvas
        c.delete("scrollbar")
        metrics = self._scrollbar_metrics()
        if metrics is None:
            return
        track_x, _track_top, _track_h, thumb_top, thumb_h, _span = metrics
        c.create_rectangle(track_x, thumb_top, track_x + SCROLLBAR_W,
                           thumb_top + thumb_h,
                           fill=self.c("scroll_thumb"), width=0,
                           tags=("scrollbar",))

    # -- scrollbar dragging ----------------------------------------------

    def _scrollbar_press(self, x, y):
        """Handle a left press at (x, y) if it landed on the scrollbar.

        True means the press was the bar's and the page must not also treat
        it as a click. Pressing the thumb starts a drag that keeps the
        grabbed point under the pointer; pressing the empty track jumps the
        thumb to centre on the press and then drags from there, so one
        gesture can start anywhere on the bar.
        """
        metrics = self._scrollbar_metrics()
        if metrics is None:
            return False
        self._cancel_momentum()
        track_x, track_top, track_h, thumb_top, thumb_h, _span = metrics
        if x < track_x - SCROLLBAR_GRAB_PAD:
            return False
        if not track_top <= y < track_top + track_h:
            return False
        if thumb_top <= y < thumb_top + thumb_h:
            self._scroll_grab = y - thumb_top
        else:
            self._scroll_grab = thumb_h / 2
            self._scrollbar_drag_to(y)
        return True

    def _scrollbar_drag_to(self, y):
        """Put the thumb where a pointer at `y` says it should be."""
        if self._scroll_grab is None:
            return
        metrics = self._scrollbar_metrics()
        if metrics is None:
            # The page shrank under the drag (an image failed, a script
            # rewrote it) and there is nothing left to scroll.
            self._scroll_grab = None
            return
        _track_x, track_top, track_h, _thumb_top, thumb_h, span = metrics
        travel = track_h - thumb_h
        if travel <= 0:
            return  # a thumb that fills the track has nowhere to go
        # Dragging past either end of the track is ordinary -- the pointer
        # leaves the window all the time -- and set_scroll() clamps to the
        # same limits the wheel gets.
        offset = (y - self._scroll_grab - track_top) / travel * span
        self._dismiss_select_popup()
        # The thumb tracks the pointer on every event; the page content is
        # coalesced to the next frame (latest-wins) so a fast drag does not
        # freeze by queueing a full redraw per mouse-move.
        self.active_tab.set_scroll(offset)
        self._draw_scrollbar()

    # -- <input type=range> dragging --------------------------------------

    def _press_range(self, x, y):
        """Start dragging a range input whose box the press landed in.

        True when the press was a range's and nothing else should handle it.
        The thumb glides to the press point over a few frames rather than
        snapping there, and the value is committed on release.
        """
        if not self.active_tab:
            return False
        rect = self.active_tab._range_rect_at(x, y)
        if rect is None:
            return False
        node, lx, ty, rx, by = rect
        self._cancel_momentum()
        self._cancel_range_glide()
        self._range_grab = (node, lx, rx)
        self._range_target = self._range_frac(x)
        start = self._range_frac_from_value(node)
        if abs(self._range_target - start) >= 0.02:
            self._range_glide = (start, self._range_target, 0.0)
            self._range_anim = self.window.after(
                RANGE_GLIDE_MS, self._range_glide_tick)
        return True

    def _range_frac(self, x):
        """The grabbed range's fraction under pointer x."""
        if self._range_grab is None:
            return 0.0
        node, lx, rx = self._range_grab
        if rx <= lx:
            return 0.0
        return max(0.0, min(1.0, (x - lx) / (rx - lx)))

    def _range_frac_from_value(self, node):
        """The fraction the node's current value sits at on the track."""
        try:
            lo = float(node.attributes.get("min", 0))
            hi = float(node.attributes.get("max", 100))
        except ValueError:
            lo, hi = 0.0, 100.0
        span = (hi - lo) or 1.0
        try:
            cur = float(field_value(node) or lo)
        except ValueError:
            cur = lo
        return max(0.0, min(1.0, (cur - lo) / span))

    def _range_glide_tick(self):
        """Advance a thumb glide one frame, then reschedule until it lands."""
        self._range_anim = None
        if self._range_grab is None:
            self._range_glide = None
            return
        if self._range_glide is None:
            return
        node, _lx, _rx = self._range_grab
        start, target, t = self._range_glide
        t += 1.0 / RANGE_GLIDE_FRAMES
        if t >= 1.0:
            self._range_glide = None
            frac = target
        else:
            self._range_glide = (start, target, t)
            # Ease out of the old spot and glide into the new one.
            eased = 1.0 - (1.0 - t) ** 3
            frac = start + (target - start) * eased
            self._range_anim = self.window.after(
                RANGE_GLIDE_MS, self._range_glide_tick)
        self._range_drag_to_frac(node, frac)

    def _cancel_range_glide(self):
        if self._range_anim is not None:
            self.window.after_cancel(self._range_anim)
            self._range_anim = None
        self._range_glide = None

    def _range_drag_to_frac(self, node, frac):
        """Set a grabbed range's value from a track fraction, updating the
        live readout beside the slider as it goes."""
        try:
            lo = float(node.attributes.get("min", 0))
            hi = float(node.attributes.get("max", 100))
        except ValueError:
            lo, hi = 0.0, 100.0
        span = (hi - lo) or 1.0
        value = lo + frac * span
        value = max(lo, min(hi, value))
        node.attributes["value"] = str(int(value))
        self._update_range_readout(node, int(value))
        self.active_tab.render()
        self._draw_page()

    def _range_drag_to(self, x):
        """Set a grabbed range's value from a pointer x, following it
        continuously (no step snapping) so the thumb moves smoothly."""
        if self._range_grab is None:
            return
        node, _lx, _rx = self._range_grab
        self._cancel_range_glide()
        self._range_target = self._range_frac(x)
        self._range_drag_to_frac(node, self._range_target)

    def _update_range_readout(self, node, value):
        """Rewrite the live value readout (and momentum peak, when present)
        that sits beside a range slider, so the text tracks the thumb."""
        name = node.attributes.get("name", "")
        parent = node.parent
        if not name or parent is None:
            return
        for child in getattr(parent, "children", ()):
            if not isinstance(child, Element):
                continue
            if child.attributes.get("id") == f"out-{name}":
                setting = settings.by_key(name)
                unit = setting.unit if setting else ""
                child.children = [Text(f"{value} {unit}".strip(), child)]
            elif child.attributes.get("class") == "speed":
                child.children = [Text(
                    f"peak {settings.momentum_peak(value):.1f} px/frame",
                    child)]

    def _commit_range(self):
        """Release of a range drag: persist the value and fire `change`.

        The stored value is snapped back onto the step grid the input
        declares, so what gets persisted is a real setting value even though
        the drag (and glide) moved the thumb continuously.
        """
        if self._range_grab is None:
            return
        node, _lx, _rx = self._range_grab
        self._range_grab = None
        self._cancel_range_glide()
        try:
            lo = float(node.attributes.get("min", 0))
            hi = float(node.attributes.get("max", 100))
        except ValueError:
            lo, hi = 0.0, 100.0
        step = float(node.attributes.get("step", 1) or 1)
        span = (hi - lo) or 1.0
        value = lo + self._range_target * span
        if step:
            value = lo + round((value - lo) / step) * step
        value = max(lo, min(hi, value))
        node.attributes["value"] = str(int(value))
        self._update_range_readout(node, int(value))
        self.active_tab._dispatch_js_event(node, "change")
        self.active_tab.render()
        self._draw_page()

    def run(self):
        self.window.update_idletasks()
        self.draw()
        # Coalesced repaint: only redraw when a page marked itself dirty
        # (render()) or an event handler drew directly, never on a bare
        # timer. Previously this loop repainted the whole canvas every 120ms
        # forever, which burned CPU for idle pages.
        self.window.after(120, self._repaint_tick)
        self._poll_images()
        self._ensure_video_tick()
        self.window.mainloop()

    def _repaint_tick(self):
        if self._repaint_needed:
            self._repaint_needed = False
            self._draw_page()
        self.window.after(120, self._repaint_tick)

    def _ensure_video_tick(self):
        """Arm the frame timer once, on demand.

        Not armed in __init__ because a browser with no video in it should
        not have a 25 Hz timer in it either, and not armed twice because two
        chains would tick every player twice a frame -- which is harmless for
        correctness (the clock decides what is due) and pure waste.
        """
        if self._video_ticking:
            return
        self._video_ticking = True
        self._video_tick()

    def _video_tick(self):
        """The frame timer, on its own chain because it has to run far more
        often than the 120 ms repaint coalescer and the 60 ms image sweep.

        It asks each tab for the frame that is due now and repaints only if
        one changed, so an idle page with no video costs a dictionary walk.
        The interval is the ceiling on frame rate, not the frame rate: what
        gets shown is whatever the clock says is current, so a 30 fps file on
        a 40 ms tick drops frames rather than slowing down. Raising it is a
        one-line change once the decoders are fast enough to deserve it.
        """
        for tab in self.tabs:
            # Both, every tick, and not short-circuited: `or` would stop
            # asking the images the moment a video said yes, and an animated
            # GIF on a page with a film on it would freeze whenever the film
            # was playing.
            moved = tab.tick_videos()
            moved = tab.tick_images() or moved
            if moved and tab is self.active_tab:
                self._repaint_needed = True
        self.window.after(VIDEO_TICK_MS, self._video_tick)

    def busy(self):
        """True while any tab still has work that changes what is on screen.

        Three different things count, and conflating them is what made
        screenshots come out full of placeholders: a document still on the
        wire, images still on the wire for a document that has already
        arrived, and -- for the same reason -- a video file still on the
        wire, whose element is drawing a "[video: loading]" box until it
        lands. A video that has *arrived* never makes us busy: playing is not
        loading, and a looping film would make settle() wait for ever.
        """
        return any(tab.loading or tab.pending_images() or tab.pending_videos()
                   for tab in self.tabs)

    def settle(self, timeout=SETTLE_TIMEOUT):
        """Run the timer queue until nothing is outstanding, or `timeout`.

        Without a platform event loop nothing drains the timers, and image
        loading lives entirely on them: fetches finish on background threads
        and only become pixels when _poll_images() picks them up on the UI
        thread. Anything that wants a finished frame without calling run()
        -- --screenshot, tests -- has to pump here first.

        Returns True if everything settled, False if the timeout arrived
        first (a page can always point at an image that never answers).

        False is a verdict, not a failure: the tab is left drawable either
        way. Every render this call reached has already run, the timers it
        did not get to are still on the queue with their due times intact,
        and draw() paints the last complete display list. Giving up on a page
        that will not stop working and showing what there is beats a browser
        that never comes back -- which is what the alternative actually is.

        The deadline reaches the timer queue as well as this loop. A batch of
        due timers is unbounded work -- a callback can fetch and parse a
        stylesheet or lay out the document -- so bounding only the loop
        around it bounds nothing.
        """
        deadline = time.monotonic() + timeout
        if not self._polling_images:
            self._poll_images()
        while True:
            wait = self.window.flush_timers(deadline)
            if not self.busy():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(wait, 0.01) if wait is not None else 0.01)

    def _poll_images(self):
        """Periodic UI-thread sweep: pick up decoded image bytes left by the
        fetch threads, re-render, and spin the loading indicator while any
        tab is still fetching. The `after` chain lives for the whole session,
        which keeps the loop alive across navigations."""
        # Whoever gets here first owns the chain; settle() checks this so a
        # headless render does not start a second one alongside run()'s.
        self._polling_images = True
        loading = False
        for tab in self.tabs:
            tab._drain_images()
            tab._drain_videos()
            if tab._js_interp is not None:
                # Advance the JS virtual clock so setTimeout/setInterval fire
                # on schedule, then run microtasks/timers/fetch settlements.
                tab._js_interp.advance(60)
                tab._drain_js()
            tab._flush_pending_nav()
            if tab.loading:
                loading = True
        self._poll_downloads()
        if loading:
            self._loading_angle = (self._loading_angle + 18) % 360
            self.canvas.delete("spinner")
            self._draw_spinner()
        self.window.after(60, self._poll_images)

    def _poll_downloads(self):
        """UI thread: repaint the downloads panel as transfers move.

        The workers never touch the canvas; they update their own Download
        record and set a flag. This picks the flag up on the same 60ms timer
        that drains image fetches, so a progress bar advances without a
        thread anywhere near the rasteriser -- and when nothing is
        downloading, nothing repaints.
        """
        panel = self.downloads_panel
        if self.downloads.take_announcement():
            panel.open_ = True
        moved = self.downloads.take_changed()
        active = self.downloads.active()
        if active:
            self._downloads_phase += 1
        if panel.open_ and (moved or active):
            panel.draw(self.canvas)

    def _draw_spinner(self):
        """Chrome-style spinning arc at the left of the address bar."""
        tab = self.active_tab
        if not tab or not tab.loading:
            return
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        addr_x = 136 + self._toe_buttons_offset() + 30
        cx = addr_x + 16
        cy = top + 60
        c.create_arc(cx - 6, cy - 6, cx + 6, cy + 6,
                     start=self._loading_angle, extent=250,
                     style="arc", outline=self.c("accent"), width=2,
                     tags=("spinner",))


class PopupWindow:
    """A real popup window (a separate Toplevel), not a redirect.

    Each popup is a mini-browser: its own canvas, a hand-drawn title bar
    with a close button, a Tab rendering the URL through the full pipeline,
    wheel scrolling, and a scrollbar. Popups share the browser's toe
    contexts, so toe:// pages, the detective's paper trail, and link
    navigation all work inside them.

    Special links a page can use:
        popup:close            close this popup
        popup:spawn:<url>      open another popup (the classic adware chain)
    """

    TITLE_BAR = 22

    def __init__(self, browser, url, width=320, height=240):
        self.browser = browser
        self.width = width
        self.height = height
        self.window = gui.Toplevel(browser.window)
        self.window.title("")
        self.window.geometry(f"{width}x{height}")
        self.canvas = Canvas(
            self.window, width=width, height=height,
            bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.tab = Tab(height - self.TITLE_BAR, browser)
        self.tab.load(URL(str(url)) if isinstance(url, str) else url)
        self.context_menu = ContextMenu(browser)
        self._bind()
        self.draw()

    def _bind(self):
        self.window.bind("<MouseWheel>", self._on_wheel)
        self.window.bind("<Button-4>",
                         lambda e: self._scroll(-self.browser.scroll_step()))
        self.window.bind("<Button-5>",
                         lambda e: self._scroll(self.browser.scroll_step()))
        self.window.bind("<Button-1>", self._on_click)
        self.window.bind("<Button-3>", self._on_context_menu)
        self.window.bind("<Motion>", self._on_motion)
        self.window.bind("<Escape>", self._on_escape)

    def _on_wheel(self, e):
        self._scroll(-e.delta if abs(e.delta) < 30
                     else -int(e.delta / 30) * self.browser.scroll_step())

    def _scroll(self, delta):
        self.tab.scroll_by(delta)
        self.draw()

    def _on_click(self, e):
        if self.context_menu.open_:
            self._context_menu_click(e.x, e.y)
            return
        if e.y < self.TITLE_BAR:
            if e.x >= self.width - 20:
                self.window.destroy()
            return
        dest = self.tab.click(e.x, e.y - self.TITLE_BAR)
        if dest:
            self._navigate(dest)
        self.draw()

    # -- context menu ----------------------------------------------------

    def _on_context_menu(self, e):
        items = self._context_items(e.x, e.y)
        self.context_menu.open(e.x, e.y, items, self.width, self.height)
        self.draw()

    def _context_menu_click(self, x, y):
        _handle_context_menu_click(self.context_menu, x, y, self.draw)

    def _on_motion(self, e):
        if self.context_menu.open_ and self.context_menu.set_hover(e.x, e.y):
            self.context_menu.draw(self.canvas)

    def _on_escape(self, e):
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()

    def _context_items(self, x, y):
        if y < self.TITLE_BAR:
            return [
                ("Reload", lambda: self.tab.load(self.tab.url, push=False),
                 True),
                None,
                ("Close Popup", self.window.destroy, True),
            ]
        items = [
            ("Back", self.tab.go_back, bool(self.tab.history)),
            ("Forward", self.tab.go_forward, bool(self.tab.future)),
            ("Reload", lambda: self.tab.load(self.tab.url, push=False), True),
            None,
            ("Copy Page URL",
             lambda u=str(self.tab.url): self._copy_text(u),
             bool(self.tab.url)),
            ("Close Popup", self.window.destroy, True),
        ]
        return items

    def _copy_text(self, text):
        _copy_text(self.window, text)

    def _navigate(self, dest):
        if isinstance(dest, FormAction):
            s = str(dest.url)
            if s == "popup:close":
                self.window.destroy()
                return
            if s.startswith("popup:spawn:"):
                for ctx in self.browser.toe_contexts:
                    if hasattr(ctx, "popup"):
                        ctx.popup(s[len("popup:spawn:"):])
                return
            self.tab.load(dest.url, payload=dest.payload)
        else:
            s = str(dest)
            if s == "popup:close":
                self.window.destroy()
                return
            if s.startswith("popup:spawn:"):
                for ctx in self.browser.toe_contexts:
                    if hasattr(ctx, "popup"):
                        ctx.popup(s[len("popup:spawn:"):])
                return
            self.tab.load(dest)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        self.tab.tab_height = self.height - self.TITLE_BAR
        self.tab.draw(c, self.TITLE_BAR)
        c.create_rectangle(0, 0, self.width, self.TITLE_BAR,
                           fill=self.browser.c("popup_titlebar"), width=0)
        c.create_line(0, self.TITLE_BAR, self.width, self.TITLE_BAR,
                      fill=self.browser.c("popup_border"))
        c.create_text(6, self.TITLE_BAR // 2, text=str(self.tab.url)[:40],
                      anchor="w", font=get_font(10, "normal", "roman",
                                                "Helvetica"),
                      fill=self.browser.c("popup_text"))
        c.create_text(self.width - 10, self.TITLE_BAR // 2, text="×",
                      font=get_font(12, "bold", "roman", "Helvetica"),
                      fill=self.browser.c("popup_text"))
        # Scrollbar.
        view = self.height - self.TITLE_BAR
        total = self.tab.content_height()
        if total > view:
            frac = view / total
            thumb_h = max(20, view * frac)
            thumb_top = self.TITLE_BAR + (view - thumb_h) * (
                self.tab.scroll / (total - view))
            c.create_rectangle(self.width - 6, thumb_top,
                                self.width - 2, thumb_top + thumb_h,
                                fill=self.browser.c("scroll_thumb"), width=0)
        self.context_menu.draw(self.canvas)


class JSResponse:
    """Host `fetch()` Response: ok/status/statusText/headers, text(), json()."""

    def __init__(self, interp, headers, body, ctype, status):
        self._interp = interp
        self._headers = {k: v for k, v in headers.items()}
        self._body = body
        self._ctype = ctype
        self._status = status

    def js_get(self, name):
        if name == "ok":
            return 200 <= self._status < 300
        if name == "status":
            return self._status
        if name == "statusText":
            return "OK" if 200 <= self._status < 300 else "error"
        if name == "headers":
            return dict(self._headers)
        if name == "text":
            return self._text
        if name == "json":
            return self._json
        return UNDEFINED

    def _text(self):
        p = self._interp.create_promise()
        p.resolve(self._body)
        return p

    def _json(self):
        p = self._interp.create_promise()
        try:
            p.resolve(json.loads(self._body))
        except Exception as e:  # noqa: BLE001 - bad JSON body
            p.reject(str(e))
        return p


class _JSXHRCtor:
    """The `XMLHttpRequest` global: a constructor object."""

    def __init__(self, tab):
        self._tab = tab

    def js_new(self, *args):
        return _JSXHR(self._tab)


class _JSXHR:
    """A minimal XMLHttpRequest: open/send, readyState/status/responseText,
    and onreadystatechange/onload/onerror handlers."""

    def __init__(self, tab):
        self._tab = tab
        self._method = "GET"
        self._url = None
        self._headers = {}
        self._ready = 0
        self._status = 0
        self._text = ""
        self.onreadystatechange = None
        self.onload = None
        self.onerror = None

    def js_get(self, name):
        if name == "open":
            return self.open
        if name == "send":
            return self.send
        if name == "setRequestHeader":
            return self.set_request_header
        if name == "readyState":
            return self._ready
        if name == "status":
            return self._status
        if name == "responseText":
            return self._text
        if name == "onreadystatechange":
            return self.onreadystatechange
        if name == "onload":
            return self.onload
        if name == "onerror":
            return self.onerror
        return UNDEFINED

    def js_set(self, name, value):
        if name in ("onreadystatechange", "onload", "onerror"):
            setattr(self, name, value)

    def open(self, method, url, async_=True):
        self._method = str(method).upper()
        try:
            target = self._tab.base_url.resolve(str(url)) \
                if self._tab.base_url else URL(str(url))
        except Exception:  # noqa: BLE001 - malformed URL
            self._url = None
            self._ready = 0
            return
        if not self._tab._js_scheme_allowed(target):
            self._url = None
            self._ready = 0
            return
        self._url = target
        self._ready = 1

    def set_request_header(self, name, value):
        self._headers[str(name)] = str(value)

    def send(self, body=None):
        if self._ready < 1 or self._url is None:
            return UNDEFINED
        self._ready = 2
        target = self._url
        payload = None
        if isinstance(body, str) and body:
            payload = body
        elif body is not None and not (body is True or body is False):
            payload = self._tab._js_interp.repr(body) if body is not UNDEFINED \
                else None

        def worker():
            try:
                headers, resp, ctype = target.request(payload=payload)
                err = None
                status = 200
            except Exception as e:  # noqa: BLE001 - network failure
                headers, resp, ctype, status, err = {}, "", "text/plain", 0, str(e)
            self._tab._js_xhr_results.append((self, headers, resp, ctype,
                                              status, err))

        threading.Thread(target=worker, daemon=True).start()
        return UNDEFINED

    def _finish(self, headers, body, status, err):
        self._ready = 4
        self._status = status if not err else 0
        self._text = body
        interp = self._tab._js_interp
        for handler in (self.onreadystatechange, self.onload if not err
                        else self.onerror):
            if handler is not None and handler is not UNDEFINED:
                try:
                    interp.call(handler)
                except JSException as e:
                    if interp is not None:
                        interp.logs.append(f"JS error: {e}")


def _resolve_internal(url, bookmarks=None, snapshot=None, theme=None,
                      apply=None, active=None):
    """Resolve an internal about: URL against the active tab's providers.

    `bookmarks` and `snapshot` are callables that supply the data for the
    bookmarks and history pages; the shoes classes do not carry them.
    """
    if url == "about:blank":
        return _AboutURL(bookmarks, theme, apply, active)
    if url == "about:bookmarks":
        return _BookmarksURL(bookmarks, theme, apply, active)
    if url == "about:history":
        return _HistoryURL(snapshot, theme, apply, active)
    if url == "about:shoes":
        return _ShoesURL(apply, theme, active)
    if url.startswith("about:shoes/"):
        return _ShoesApplyURL(url[len("about:shoes/"):],
                              apply, theme, active)
    if url == "about:settings":
        return _SettingsURL(settings_provider=None, apply=None,
                            theme=theme, active=active)
    if url.startswith("about:settings/"):
        rest = url[len("about:settings/"):]
        key, _, value = rest.partition("/")
        return _SettingsApplyURL(key, value, settings_provider=None,
                                 apply=None, theme=theme, active=active)
    return URL(url) if "://" in url else URL("https://" + url)


class _AboutURL:
    """Placeholder URL for the internal welcome page."""
    view_source = False
    fragment = ""

    def __init__(self, bookmarks_provider=None, theme=None, apply=None,
                 active=None):
        self.bookmarks_provider = bookmarks_provider
        self.theme = theme
        self.apply = apply
        self.active = active

    def resolve(self, url):
        return _resolve_internal(
            url, self.bookmarks_provider,
            lambda: {"back": [], "current": "", "forward": []},
            self.theme, self.apply, self.active)

    def request(self, payload=None):
        return {}, welcome_html(self.theme), "text/html"

    def __str__(self):
        return "about:blank"


class _BookmarksURL:
    """Internal URL for the bookmarks page."""
    view_source = False
    fragment = ""

    def __init__(self, bookmarks_provider=None, theme=None, apply=None,
                 active=None):
        self.bookmarks_provider = bookmarks_provider or (lambda: [])
        self.theme = theme
        self.apply = apply
        self.active = active

    def resolve(self, url):
        return _resolve_internal(
            url, self.bookmarks_provider,
            lambda: {"back": [], "current": "", "forward": []},
            self.theme, self.apply, self.active)

    def request(self, payload=None):
        return {}, bookmarks_html(self.bookmarks_provider(), self.theme), \
            "text/html"

    def __str__(self):
        return "about:bookmarks"


class _HistoryURL:
    """Internal URL for the current tab's history page."""
    view_source = False
    fragment = ""

    def __init__(self, snapshot_provider=None, theme=None, apply=None,
                 active=None):
        self.snapshot_provider = snapshot_provider or (
            lambda: {"back": [], "current": "", "forward": []})
        self.theme = theme
        self.apply = apply
        self.active = active

    def resolve(self, url):
        return _resolve_internal(
            url, None, self.snapshot_provider,
            self.theme, self.apply, self.active)

    def request(self, payload=None):
        return {}, history_html(self.snapshot_provider(), self.theme), \
            "text/html"

    def __str__(self):
        return "about:history"


class _ShoesURL:
    """Internal URL for the theme picker page (about:shoes)."""
    view_source = False
    fragment = ""

    def __init__(self, apply=None, theme=None, active=None):
        self.apply = apply
        self.theme = theme
        self.active = active

    def resolve(self, url):
        return _resolve_internal(url, None, None,
                                 self.theme, self.apply, self.active)

    def request(self, payload=None):
        active = self.active() if callable(self.active) else self.active
        return {}, shoes_html(self.theme, active), "text/html"

    def __str__(self):
        return "about:shoes"


class _ShoesApplyURL:
    """Internal URL that applies a shoe when visited (about:shoes/<Name>)."""
    view_source = False
    fragment = ""

    def __init__(self, name, apply=None, theme=None, active=None):
        self.name = name
        self.apply = apply
        self.theme = theme
        self.active = active

    def resolve(self, url):
        return _resolve_internal(url, None, None,
                                 self.theme, self.apply, self.active)

    def request(self, payload=None):
        canonical = shoes.find(self.name)
        if canonical is not None and callable(self.apply):
            self.apply(canonical)
        applied = html.escape(canonical or self.name)
        p = _page_palette(self.theme)
        return {}, f"""
<!doctype html>
<html><head><title>Applied</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: {p['text']};
         background: {p['bg']}; }}
  h1 {{ font-size: 36px; color: {p['accent']}; }}
  a {{ color: {p['link']}; }}
</style></head>
<body>
  <h1>{applied} on.</h1>
  <p><a href="about:shoes">Back to Shoes</a> or keep browsing.</p>
</body></html>
""", "text/html"

    def __str__(self):
        return f"about:shoes/{self.name}"


class _SettingsURL:
    """Internal URL for the browser settings page (about:settings)."""
    view_source = False
    fragment = ""

    def __init__(self, settings_provider=None, apply=None, theme=None,
                 active=None):
        self.settings_provider = settings_provider or (lambda: {})
        self.apply = apply
        self.theme = theme
        self.active = active

    def resolve(self, url):
        if url == "about:settings":
            return self
        if url.startswith("about:settings/"):
            rest = url[len("about:settings/"):]
            key, _, value = rest.partition("/")
            return _SettingsApplyURL(key, value, self.settings_provider,
                                     self.apply, self.theme, self.active)
        return _resolve_internal(url, None, None,
                                 self.theme, self.apply, self.active)

    def request(self, payload=None):
        values = self.settings_provider()
        active = self.active() if callable(self.active) else self.active
        return {}, settings_html(values, self.theme, active), "text/html"

    def __str__(self):
        return "about:settings"


class _SettingsApplyURL:
    """Internal URL that sets one setting (about:settings/<key>/<value>)."""
    view_source = False
    fragment = ""

    def __init__(self, key, value, settings_provider=None, apply=None,
                 theme=None, active=None):
        self.key = key
        self.value = value
        self.settings_provider = settings_provider or (lambda: {})
        self.apply = apply
        self.theme = theme
        self.active = active

    def resolve(self, url):
        if url == "about:settings":
            return _SettingsURL(self.settings_provider, self.apply,
                                self.theme, self.active)
        if url.startswith("about:settings/"):
            rest = url[len("about:settings/"):]
            key, _, value = rest.partition("/")
            return _SettingsApplyURL(key, value, self.settings_provider,
                                     self.apply, self.theme, self.active)
        return _resolve_internal(url, None, None,
                                 self.theme, self.apply, self.active)

    def request(self, payload=None):
        if callable(self.apply):
            self.apply(self.key, self.value)
        values = self.settings_provider()
        active = self.active() if callable(self.active) else self.active
        return {}, settings_html(values, self.theme, active), "text/html"

    def __str__(self):
        return f"about:settings/{self.key}/{self.value}"


def _page_palette(theme):
    """Map a shoe palette onto the colors used by the internal pages."""
    t = _page_theme(theme)
    return {
        "bg": t["page_bg"], "text": t["page_text"], "accent": t["accent"],
        "link": t["link_color"], "muted": t["status_text"],
        "surface": t["addr_bg"], "border": t["button_border"],
    }


def _page_theme(theme):
    if theme is None:
        return shoes.merge(shoes.resolve(shoes.DEFAULT_SHOE))
    return theme


def bookmarks_html(bookmarks, theme=None):
    p = _page_palette(theme)
    items = []
    for entry in bookmarks:
        safe = html.escape(entry, quote=True)
        items.append(f'<li><a href="{safe}">{safe}</a></li>')
    listing = "\n".join(items) if items else "<li>No bookmarks yet.</li>"
    return f"""
<!doctype html>
<html><head><title>Bookmarks</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: {p['text']};
         background: {p['bg']}; }}
  h1 {{ font-size: 40px; color: {p['accent']}; }}
  .sub {{ color: {p['muted']}; font-size: 18px; }}
  li {{ margin-top: 8px; }}
  a {{ color: {p['link']}; word-break: break-all; }}
</style></head>
<body>
  <h1>Bookmarks</h1>
  <p class="sub">Saved pages from Ctrl-D or the star button.</p>
  <ul>{listing}</ul>
</body></html>
"""


def history_html(snapshot, theme=None):
    p = _page_palette(theme)
    back_items = []
    for url in snapshot.get("back", []):
        safe = html.escape(url, quote=True)
        back_items.append(f'<li><a href="{safe}">{safe}</a></li>')
    current = html.escape(snapshot.get("current", "") or "(none)", quote=True)
    forward_items = []
    for url in snapshot.get("forward", []):
        safe = html.escape(url, quote=True)
        forward_items.append(f'<li><a href="{safe}">{safe}</a></li>')
    back_list = "\n".join(back_items) if back_items else "<li>None</li>"
    forward_list = "\n".join(forward_items) if forward_items else "<li>None</li>"
    return f"""
<!doctype html>
<html><head><title>History</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: {p['text']};
         background: {p['bg']}; }}
  h1 {{ font-size: 40px; color: {p['accent']}; }}
  h2 {{ margin-top: 30px; }}
  .sub {{ color: {p['muted']}; font-size: 18px; }}
  li {{ margin-top: 8px; }}
  a {{ color: {p['link']}; word-break: break-all; }}
  .current {{ background: {p['surface']}; padding: 10px;
              border-left: 4px solid {p['accent']}; }}
</style></head>
<body>
  <h1>History</h1>
  <p class="sub">Current tab timeline. Open with <b>Ctrl-H</b>.</p>
  <h2>Back stack (oldest → newest)</h2>
  <ul>{back_list}</ul>
  <h2>Current page</h2>
  <p class="current">{current}</p>
  <h2>Forward stack (next first)</h2>
  <ul>{forward_list}</ul>
</body></html>
"""


def welcome_html(theme=None):
    """Render the New Tab page: a clean centered card on a themed backdrop."""
    p = _page_palette(theme)
    return f"""
<!doctype html>
<html><head><title>New Tab</title>
<style>
  body {{ font-family: Helvetica; margin: 0; color: {p['text']};
         background: {p['bg']}; }}
  .stage {{ display: flex; justify-content: center; }}
  .shell {{ width: 540px; margin-top: 70px; background: {p['surface']};
            padding: 36px 44px; box-shadow: 2px 3px 12px {p['border']}; }}
  h1 {{ font-size: 42px; color: {p['accent']}; margin-top: 0; }}
  .tagline {{ color: {p['muted']}; font-size: 17px; }}
  h3 {{ margin-top: 26px; color: {p['text']}; }}
  ul {{ margin-left: 24px; }}
  li {{ margin-top: 6px; }}
  a {{ color: {p['link']}; }}
  .foot {{ margin-top: 30px; }}
</style></head>
<body>
  <div class="stage">
    <div class="shell">
      <h1>FeetBrowser</h1>
      <p class="tagline">A browser built from scratch — its own HTTP client,
      HTML parser, CSS engine, and layout engine.</p>
      <h3>Try these</h3>
      <ul>
        <li><a href="https://example.com">example.com</a> — the classic test page</li>
        <li><a href="https://info.cern.ch/hypertext/WWW/TheProject.html">the first web page ever</a></li>
        <li><a href="https://news.ycombinator.com">Hacker News</a></li>
        <li><a href="https://en.wikipedia.org/wiki/Web_browser">Wikipedia: Web browser</a></li>
        <li><a href="about:bookmarks">about:bookmarks</a> — your saved pages</li>
        <li><a href="about:history">about:history</a> — back/forward timeline</li>
        <li><a href="about:shoes">about:shoes</a> — satisfy your sole with a fitting theme</li>
        <li><a href="view-source:https://example.com">view-source:example.com</a></li>
      </ul>
      <h3>Your toes</h3>
      <ul>
        <li><a href="toe://hub">toe://hub</a> — browse and install toes</li>
      </ul>
      <h3>Shortcuts</h3>
      <ul>
        <li><b>Ctrl-L</b> focus address bar &nbsp; <b>Ctrl-T</b> new tab &nbsp;
            <b>Ctrl-W</b> close tab &nbsp; <b>Ctrl-Tab</b> / <b>Ctrl-PgUp/Dn</b> cycle tabs</li>
        <li><b>Ctrl-R</b> reload &nbsp; <b>Ctrl-D</b> bookmark page &nbsp;
            <b>Ctrl-H</b> history &nbsp; <b>Alt-Left/Right</b> back / forward</li>
        <li><b>↑ ↓ / wheel</b> scroll &nbsp; <b>PgUp/Dn</b> scroll by page &nbsp;
            <b>Home / End</b> jump to top / bottom &nbsp; <b>Esc</b> blur</li>
      </ul>
      <p class="tagline">Type a URL or a search term in the address bar to begin.</p>
    </div>
  </div>
</body></html>
"""


def _shoe_cards(theme, active):
    """The picker's cards: one per shoe, a swatch strip each, the one in use
    flagged. Shared by the standalone Shoes page and the Settings tab."""
    p = _page_palette(theme)
    cards = []
    for name in shoes.shoe_names():
        pal = shoes.merge(shoes.resolve(name))
        strip_keys = ("chrome_bg", "tab_bar", "addr_bg", "accent",
                      "status_bg")
        swatches = "".join(
            f'<span style="background:{pal[k]};display:inline-block;'
            f'width:26px;height:26px;border:1px solid {p["border"]};'
            f'border-radius:4px;"></span>'
            for k in strip_keys)
        in_use = ' <span class="inuse">in use</span>' if name == active else ""
        cards.append(
            f'<li class="shoe{" current" if name == active else ""}">'
            f'<a href="about:shoes/{html.escape(name, quote=True)}">'
            f'<div class="swatches">{swatches}</div>'
            f'<div class="name">{html.escape(name)}{in_use}</div>'
            f'</a></li>')
    return "\n".join(cards)


def shoes_html(theme, active):
    """Render the Shoes theme picker: one card per shoe, with a swatch strip
    showing its palette. Clicking a card applies it instantly."""
    p = _page_palette(theme)
    listing = _shoe_cards(theme, active)
    return f"""
<!doctype html>
<html><head><title>Shoes</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: {p['text']};
         background: {p['bg']}; }}
  h1 {{ font-size: 40px; color: {p['accent']}; }}
  .sub {{ color: {p['muted']}; font-size: 18px; }}
  ul.shoes {{ list-style: none; padding: 0; display: flex; flex-wrap: wrap;
              gap: 16px; margin-top: 24px; }}
  li.shoe {{ background: {p['surface']}; border: 2px solid {p['border']};
             border-radius: 10px; padding: 12px; width: 220px; }}
  li.shoe.current {{ border-color: {p['accent']}; }}
  li.shoe a {{ text-decoration: none; color: {p['text']}; }}
  .swatches {{ display: flex; gap: 4px; margin-bottom: 10px; }}
  .name {{ font-weight: bold; }}
  .inuse {{ color: {p['accent']}; font-weight: bold; }}
  .foot {{ margin-top: 30px; color: {p['muted']}; }}
</style></head>
<body>
  <h1>Shoes</h1>
  <p class="sub">Pick a pair. The chrome and built-in pages restyle instantly.</p>
  <ul class="shoes">{listing}</ul>
  <p class="foot">Your choice is saved and used on the next launch.</p>
</body></html>
"""


def _slider_control(setting, value):
    """A real <input type=range> for a slider setting.

    The engine paints a track with a thumb the reader drags with the mouse;
    the value readout sits beside it. Changing the control fires `change`,
    and the inline handler commits the value by navigating to the setting's
    apply URL (the same address a tap on a shoe card uses to apply a theme).
    """
    readout = (f'<span id="out-{setting.key}" class="val">'
               f'{value} {setting.unit}</span>')
    return (f'<input id="{setting.key}" name="{setting.key}" '
            f'type="range" min="{setting.min}" max="{setting.max}" '
            f'step="{setting.step}" value="{value}" '
            f'onchange="apply_setting(\'{setting.key}\')">'
            + readout)


def _toggle_link(setting, value, p):
    """An on/off slider-style button for a toggle setting."""
    state = "on" if value else "off"
    fill = p["accent"] if value else p["border"]
    text = "ON" if value else "OFF"
    return (f'<a class="tgl {state}" href="about:settings/{setting.key}/'
            f'{"off" if value else "on"}" '
            f'style="display:inline-block;border:1px solid {p["border"]};'
            f'background:{fill};color:{p["bg"]};padding:4px 14px;'
            f'font-weight:bold;">{text}</a>')


def settings_html(values, theme=None, active=None):
    """Render the Settings page: search, scrolling, momentum and the rest.

    Every control is a link back into about:settings/<key>/<value>, so a
    click re-renders the page with the new value already in place, exactly
    like the Shoes picker applies a theme.
    """
    p = _page_palette(theme)
    rows = []
    for setting in settings.SETTINGS:
        value = values.get(setting.key, setting.default)
        label = html.escape(setting.label)
        help_ = html.escape(setting.help)
        if setting.kind == "toggle":
            control = _toggle_link(setting, value, p)
        elif setting.kind == "choice":
            pills = []
            for opt_val, option_label in setting.options:
                sel = " selected" if opt_val == value else ""
                pills.append(
                    f'<a class="pill{sel}" '
                    f'href="about:settings/{setting.key}/{opt_val}" '
                    f'style="display:inline-block;border:1px solid '
                    f'{p["border"]};background:'
                    f'{p["accent"] if opt_val == value else p["surface"]};'
                    f'color:{p["text"]};padding:4px 14px;'
                    f'margin-right:6px;">{option_label}</a>')
            control = "".join(pills)
        else:  # slider
            speed = ""
            if setting.key == "momentum_strength":
                speed = (f' <span class="speed">peak '
                         f'{settings.momentum_peak(value):.1f} px/frame'
                         f'</span>')
            control = (_slider_control(setting, value)
                       + speed)
        rows.append(
            f'<li class="row">'
            f'<div class="lab"><span class="name">{label}</span>'
            f'<span class="help">{help_}</span></div>'
            f'<div class="ctl">{control}</div></li>')
    listing = "\n".join(rows)
    theme_cards = ""
    if active is not None:
        theme_cards = f"""
  <h2 class="sec">Theme</h2>
  <ul class="shoes">{_shoe_cards(theme, active)}</ul>"""
    return f"""
<!doctype html>
<html><head><title>Settings</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: {p['text']};
         background: {p['bg']}; }}
  h1 {{ font-size: 40px; color: {p['accent']}; }}
  h2.sec {{ font-size: 24px; color: {p['accent']}; margin-top: 36px; }}
  .sub {{ color: {p['muted']}; font-size: 18px; }}
  ul.rows {{ list-style: none; padding: 0; margin-top: 24px;
             max-width: 760px; }}
  li.row {{ background: {p['surface']}; border: 1px solid {p['border']};
            padding: 14px 18px; margin-bottom: 12px; }}
  .lab .name {{ font-weight: bold; font-size: 18px; }}
  .lab .help {{ display: block; color: {p['muted']}; font-size: 14px;
                margin-top: 2px; }}
  .ctl {{ margin-top: 10px; }}
  .speed {{ color: {p['muted']}; font-size: 14px; }}
  a {{ color: {p['link']}; }}
  ul.shoes {{ list-style: none; padding: 0; display: flex; flex-wrap: wrap;
              gap: 16px; margin-top: 16px; }}
  li.shoe {{ background: {p['surface']}; border: 2px solid {p['border']};
             border-radius: 10px; padding: 12px; width: 220px; }}
  li.shoe.current {{ border-color: {p['accent']}; }}
  li.shoe a {{ text-decoration: none; color: {p['text']}; }}
  .swatches {{ display: flex; gap: 4px; margin-bottom: 10px; }}
  .name {{ font-weight: bold; }}
  .inuse {{ color: {p['accent']}; font-weight: bold; }}
  .foot {{ margin-top: 30px; color: {p['muted']}; }}
</style></head>
<body>
  <h1>Settings</h1>
  <p class="sub">Tune the browser. Every control saves itself the moment
  you click it.</p>
  <ul class="rows">{listing}</ul>{theme_cards}
  <p class="foot">Stored in ~/.feetbrowser_settings.json, alongside any
  keys other toes keep there.</p>
<script>
function apply_setting(id) {{
  var el = document.getElementById(id);
  if (!el) return;
  location.href = "about:settings/" + el.name + "/" + el.value;
}}
</script>
</body></html>
"""


def screenshot(url, path, width=WIDTH, height=HEIGHT, settle=SETTLE_TIMEOUT):
    """Load `url` and write the rendered window to `path` as a PNG.

    No display is opened. The canvas draws into an ordinary buffer, so a full
    render -- chrome, page, images, whatever scripts produced -- is just a
    file write, which makes the renderer inspectable from a shell and
    diffable in a test.
    """
    browser = Browser()
    browser.window.geometry("%dx%d" % (width, height))
    browser.canvas.resize(width, height)
    # Resizing the canvas normally reaches layout via a debounced <Configure>
    # handler, which needs a timer flush that has not happened yet. Apply it
    # now, or the page lays out for the default viewport and gets cropped.
    browser._apply_resize()
    browser.new_tab(url)
    # Images and deferred scripts land on the timer queue, so the frame is
    # only finished once that queue has run itself out.
    browser.settle(settle)
    browser.draw()
    browser.canvas.render().save_png(path)
    return browser


def main():
    args = [a for a in sys.argv[1:] if a != "--screenshot"]
    if "--screenshot" in sys.argv:
        url = args[0] if args else "about:blank"
        out = args[1] if len(args) > 1 else "feetbrowser.png"
        screenshot(url, out)
        print("wrote %s" % out)
        return
    try:
        usable = gui.has_display()
    except RuntimeError as exc:
        # FEETBROWSER_DISPLAY named a backend that cannot run here. Asking for
        # one by name and quietly getting a headless root is how you end up
        # with a black screenshot and no idea why, so this is an error -- but
        # a sentence, not a traceback.
        print("FeetBrowser: %s." % exc, file=sys.stderr)
        return 1
    if not usable:
        # Say why where we can. "No window available" on a Linux box that has
        # a perfectly good X server two lines of setup away is a dead end;
        # "$DISPLAY is not set" is a thing the user can act on.
        problem = gui.display_problem()
        print("FeetBrowser: no window available on this platform%s; "
              "use --screenshot <url> [out.png] to render to a file."
              % (" (%s)" % problem if problem else ""), file=sys.stderr)
        return 1
    browser = Browser(gui.new_window())
    start = args[0] if args else "about:blank"
    browser.new_tab(start)
    browser.run()
    return 0


if __name__ == "__main__":
    main()
