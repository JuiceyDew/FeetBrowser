"""Toes — FeetBrowser's extension hooking.

Every foot deserves toes. A toe is a plain Python module, living in its own
folder under `toes/`, that gets invited to dinner at a few well-placed points
in the load pipeline. No new dependencies, no sandboxing theater: a toe is
trusted local code, exactly like the browser's own modules, and it can do
anything the browser itself can do.

A toe folder looks like:

    toes/name-of-toe/
        toe.json     # { "name", "version", "description", "entry" }
        toe.py       # the code, exposing activate(ctx)

`toe.json` is read at startup; `toe.py` is imported and its `activate(ctx)`
is called once with a Context that wires up the toe's hooks. A toe that
raises while loading is skipped with a warning to stderr — one bad toe never
bricks the browser.
"""

import importlib.util
import json
import os
import sys

from .net import URL

# Folder, relative to the repo root, where toes live.
TOES_DIR = "toes"


class ButtonDef:
    """A toolbar button the chrome should draw for a toe.

    `glyph` is a short label drawn on the hand-rolled toolbar; `id` is
    passed back to the toe's `on_click` handler.
    """

    def __init__(self, id, glyph, label=None):
        self.id = id
        self.glyph = glyph
        self.label = label or glyph

    def __repr__(self):
        return f"ButtonDef({self.id!r}, {self.glyph!r})"


class Context:
    """The only thing a toe gets to hold. It wraps the browser and the
    active tab and dispatches calls out to the toe's hook handlers.

    Every hook is optional; a toe simply defines the ones it cares about
    as plain methods on the object returned from `activate(ctx)`.

    Supported hooks:

        on_load(url, body)          -> body or None
            Rewrite the raw HTML before it is parsed. Return the new body,
            or None to leave it alone.

        extra_css(url)              -> css string or None
            Inject an author stylesheet for this page, applied after the
            user-agent sheet and before any <style>/<link> sheets.

        handle(url, tab)            -> (headers, body, content_type) or None
            First crack at a navigation. Return a response tuple to render
            the page yourself (this is how toe:// and friends work); return
            None to fall through to normal fetching.

        on_draw(canvas, offset)     -> None
            Paint directly onto the Tk canvas (after the page, before the
            chrome). `offset` is how much the page is shifted by the chrome.

        buttons()                   -> [ButtonDef]
            Extra toolbar buttons, drawn on the hand-rolled toolbar.

        on_click(button_id)         -> None
            A toe toolbar button was clicked.

        on_keypress(event)          -> bool
            A key was pressed while no address bar had focus. Return True to
            swallow the key, False to let the browser handle it.

        on_motion(x, y)             -> None
            The mouse moved over the page (document coordinates, below the
            chrome). Fired on every motion event, so keep it cheap.

        on_new_tab()                -> None
            A new tab was created.

        chrome_bands()              -> [(id, height), ...]
            Declare horizontal bands this toe wants drawn in the browser
            chrome, stacked above the tabs. Each band is a (unique id,
            height-in-px) tuple. The browser grows its chrome to fit every
            toe's bands.

        on_chrome_draw(canvas, bands) -> None
            Paint the toe's chrome bands. `bands` is the list of
            (id, height, y_offset) tuples for the current frame, in draw
            order. Called after the chrome background, before the tabs.

        on_chrome_click(x, y, bands) -> bool
            A click landed inside the chrome band region. `bands` is the
            same list as on_chrome_draw. Return True if the click was
            consumed, False to let the normal chrome handle it.
    """

    def __init__(self, browser, toe):
        self.browser = browser
        self.toe = toe
        self._callbacks = {}
        self._settings = None
        if hasattr(toe, "activate"):
            toe.activate(self)

    # -- helpers the toe can call -----------------------------------------

    def current_tab(self):
        return self.browser.active_tab

    def tabs(self):
        return list(self.browser.tabs)

    def set_status(self, msg):
        tab = self.browser.active_tab
        if tab:
            tab.status = msg

    def open(self, url):
        """Open a URL in the active tab through the full pipeline."""
        tab = self.browser.active_tab
        if tab:
            tab.load(URL(str(url)) if isinstance(url, str) else url)

    def popup(self, url, width=320, height=240):
        """Open a real popup window rendering `url` through the pipeline.

        Popups are separate Tk windows (not redirects) with their own
        canvas, a hand-drawn title bar, scrolling, and a scrollbar. They
        share the browser's toe contexts, so toe:// pages, the detective's
        paper trail, and link navigation all work inside them.
        """
        from .browser import PopupWindow
        return PopupWindow(self.browser, url, width, height)

    # -- per-toe settings -------------------------------------------------

    @property
    def settings(self):
        """A dict of this toe's persisted settings (loaded lazily)."""
        if self._settings is None:
            self._settings = self._load_settings()
        return self._settings

    def save_settings(self):
        """Persist the current settings dict to toes/<name>/settings.json."""
        path = self._settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._settings, f, indent=2)
        except OSError as e:
            sys.stderr.write(
                f"toe {self.toe_name()}: could not save settings: {e}\n")

    def _settings_path(self):
        folder = getattr(self.toe, "folder", None)
        if not folder:
            return os.path.join(repo_root(), TOES_DIR, "settings.json")
        return os.path.join(folder, "settings.json")

    def _load_settings(self):
        path = self._settings_path()
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -- hook registration ------------------------------------------------

    def on(self, event, callback):
        self._callbacks[event] = callback

    # -- dispatch ---------------------------------------------------------

    def call(self, event, *args, **kwargs):
        cb = self._callbacks.get(event)
        if cb is None:
            return None
        try:
            return cb(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - a toe failure is not fatal
            sys.stderr.write(
                f"toe {self.toe_name()}: hook {event} raised "
                f"{type(e).__name__}: {e}\n")
            return None

    def toe_name(self):
        manifest = getattr(self.toe, "manifest", None)
        if manifest and manifest.get("name"):
            return manifest["name"]
        return getattr(self.toe, "__name__", "?")


class Toe:
    """A loaded toe: its manifest plus the module exposing activate()."""

    def __init__(self, name, version, description, folder, module):
        self.name = name
        self.version = version
        self.description = description
        self.folder = folder
        self.module = module


def repo_root():
    """Absolute path of the repo root (where toes/ sits)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def discover_toes(toes_dir=None):
    """Scan toes/ for toe.json manifests and return a list of Toe objects.

    A manifest missing required fields, or whose entry module cannot be
    imported, is skipped with a warning — a broken toe must not stop the
    browser from starting.
    """
    root = os.path.join(repo_root(), toes_dir or TOES_DIR)
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        manifest_path = os.path.join(folder, "toe.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            entry = manifest["entry"]
            module_path = os.path.join(folder, entry)
            spec = importlib.util.spec_from_file_location(
                f"toe_{name.replace('-', '_')}", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 - skip broken toes
            sys.stderr.write(
                f"toes: skipping {name}: {type(e).__name__}: {e}\n")
            continue
        module.manifest = manifest
        found.append(Toe(
            name=manifest.get("name", name),
            version=manifest.get("version", "0"),
            description=manifest.get("description", ""),
            folder=folder,
            module=module,
        ))
    return found


def dispatch(ctxs, event, *args, **kwargs):
    """Call `event` on every toe context; return the list of non-None results."""
    results = []
    for c in ctxs:
        r = c.call(event, *args, **kwargs)
        if r is not None:
            results.append(r)
    return results


def first(ctxs, event, *args, **kwargs):
    """Like dispatch but stops at the first non-None result."""
    for c in ctxs:
        r = c.call(event, *args, **kwargs)
        if r is not None:
            return r
    return None


def rewrite(ctxs, url, body):
    """Chain on_load: each toe may rewrite the body; last write wins."""
    for c in ctxs:
        r = c.call("on_load", url, body)
        if r is not None:
            body = r
    return body


def extra_css(ctxs, url):
    """Collect injected stylesheets from every toe, concatenated in order."""
    sheets = []
    for c in ctxs:
        r = c.call("extra_css", url)
        if r:
            sheets.append(r)
    return "\n".join(sheets) if sheets else None


def compute_bands(ctxs):
    """Collect every toe's chrome bands as [(id, height, y_offset), ...].

    Bands are stacked above the tabs in declaration order; each entry's
    y_offset is the top of its strip in canvas coordinates.
    """
    bands = []
    y = 0
    for c in ctxs:
        declared = c.call("chrome_bands") or []
        for band_id, height in declared:
            bands.append((band_id, height, y))
            y += height
    return bands


def band_height(bands):
    """Total height consumed by chrome bands."""
    return sum(h for _id, h, _y in bands)


# -- CLI helpers ----------------------------------------------------------


def list_toes():
    """Print a table of installed toes and their load status."""
    found = discover_toes()
    if not found:
        print("No toes installed. Drop a folder with toe.json into toes/.")
        return
    width = max(len(t.name) for t in found)
    for t in found:
        print(f"{t.name:<{width}}  v{t.version}  {t.description}")


def new_toe(name):
    """Scaffold a new toe folder with a manifest and an entry stub."""
    import re
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    if not safe:
        print("error: toe name must contain letters or digits")
        return 1
    folder = os.path.join(repo_root(), TOES_DIR, safe)
    if os.path.exists(folder):
        print(f"error: {folder} already exists")
        return 1
    os.makedirs(folder)
    manifest = {
        "name": safe,
        "version": "0.1.0",
        "description": "A brand new toe.",
        "entry": "toe.py",
    }
    with open(os.path.join(folder, "toe.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    with open(os.path.join(folder, "toe.py"), "w") as f:
        f.write('"""%s toe."""\n\n\n'
                'def activate(ctx):\n'
                '    # ctx.on("on_load", on_load)\n'
                '    pass\n' % safe)
    print(f"created {folder}")
    return 0


def toe_docs():
    """Print a markdown reference generated from every toe's manifest."""
    found = discover_toes()
    if not found:
        print("No toes installed.")
        return
    print("# Toe reference\n")
    for t in found:
        print(f"## {t.name} v{t.version}\n")
        print(f"{t.description or 'No description.'}\n")
        doc = getattr(t.module, "__doc__", "") or ""
        if doc.strip():
            print("```\n" + doc.strip() + "\n```\n")
