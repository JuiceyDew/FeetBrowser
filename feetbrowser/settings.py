"""Browser settings: a small registry of typed preferences persisted to
`~/.feetbrowser_settings.json`.

The file is treated as an owned dict: unknown keys in it (say a toe's
`background_image`) are left untouched on write, and reading never trusts a
stored value to be the right type -- each setting coerce()s what it finds
and falls back to its default. Adding a new setting is one descriptor in
SETTINGS; the settings page renders every descriptor, so nothing else has
to change.

Setting kinds:
    "toggle"    on/off switch, stored as a bool
    "choice"    one of `options` [(value, label), ...], stored as the value
    "slider"    an integer on [min, max] stepping by `step`, unit for display
"""

import json
import os

SETTINGS_FILE = os.path.expanduser("~/.feetbrowser_settings.json")


class Setting:
    """One setting: a key, a kind, and how to display and coerce it."""

    __slots__ = ("key", "kind", "label", "help", "default", "options",
                 "min", "max", "step", "unit")

    def __init__(self, key, kind, label, default, help="", options=None,
                 min=0, max=100, step=1, unit=""):
        self.key = key
        self.kind = kind
        self.label = label
        self.default = default
        self.help = help
        self.options = options or []
        self.min = min
        self.max = max
        self.step = step
        self.unit = unit

    def coerce(self, value):
        """Force an untrusted stored value back into this setting's shape."""
        if self.kind == "toggle":
            if isinstance(value, str):
                value = value.strip().lower()
                return value not in ("", "0", "false", "off", "no")
            return bool(value)
        if self.kind == "choice":
            value = str(value)
            for candidate, _label in self.options:
                if candidate == value:
                    return candidate
            return self.default
        # slider
        try:
            value = int(value)
        except (TypeError, ValueError):
            return self.default
        low, high = self.min, self.max
        if self.step:
            value = low + round((value - low) / self.step) * self.step
        return max(low, min(high, value))


#: Address-bar search engines, value -> (display name, query URL prefix).
SEARCH_ENGINES = {
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/html/?q="),
    "bing": ("Bing", "https://www.bing.com/search?q="),
    "google": ("Google", "https://www.google.com/search?q="),
}

#: Peak coast speed in px/frame; must track browser.MOMENTUM_MAX, which is
#: what the momentum readout in the settings page translates to.
MOMENTUM_MAX_PX = 40.0

SETTINGS = [
    Setting("search_engine", "choice", "Search engine",
            "duckduckgo",
            "Which engine the address bar sends plain terms to.",
            options=[(value, label)
                     for value, (label, _url) in SEARCH_ENGINES.items()]),
    Setting("show_link_preview", "toggle", "Show link preview",
            True,
            "Show the page under the cursor in the status bar."),
    Setting("scroll_speed", "slider", "Scroll speed",
            80,
            "Pixels per wheel notch or arrow key.",
            min=40, max=160, step=10, unit="px"),
    Setting("momentum", "toggle", "Sidebar momentum",
            True,
            "Coast the page on after a fast wheel flick."),
    Setting("momentum_strength", "slider", "Sidebar acceleration",
            100,
            "How hard a flick feeds the coast; 0 turns it off.",
            min=0, max=100, step=5, unit="%"),
]


def by_key(key):
    """The Setting descriptor for `key`, or None."""
    for setting in SETTINGS:
        if setting.key == key:
            return setting
    return None


def search_url(engine, query):
    """Build the search-engine URL for a plain-term `query`."""
    name, prefix = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["duckduckgo"])
    return prefix + query.replace(" ", "+")


def search_engine_label(engine):
    """Human label for a search engine value, falling back to the default."""
    return SEARCH_ENGINES.get(engine, SEARCH_ENGINES["duckduckgo"])[0]


def _read():
    try:
        with open(SETTINGS_FILE, encoding="utf8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load():
    """Return every setting coerced from the file, defaults filling gaps."""
    data = _read()
    return {s.key: s.coerce(data.get(s.key, s.default)) for s in SETTINGS}


def save(updates):
    """Persist `updates` (a {key: value} dict), preserving unknown keys.

    Reading the file fresh and writing the whole dict back is what keeps a
    key this module does not own (a toe's background_image, say) intact.
    """
    data = _read()
    for key, value in updates.items():
        setting = by_key(key)
        data[key] = setting.coerce(value) if setting else value
    try:
        with open(SETTINGS_FILE, "w", encoding="utf8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def momentum_peak(strength):
    """The peak coast speed (px/frame) a strength percentage produces."""
    return MOMENTUM_MAX_PX * max(0, min(100, strength)) / 100.0