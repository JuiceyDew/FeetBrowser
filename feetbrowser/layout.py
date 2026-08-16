"""A from-scratch layout engine.

Produces a layout tree from a styled DOM and a display list of paint
commands. Implements block-and-inline flow: block boxes stack vertically,
inline content flows into lines with word wrapping. Supports font size /
weight / style, colors, backgrounds, list bullets, and horizontal rules.

Coordinates are in CSS px == canvas px. Fonts come from canvas.py, which
measures them with our own font engine, and are cached here.
"""

import copy
import re
from collections import namedtuple
from .canvas import CanvasError, Font
from . import cssparser

from .htmlparser import Text, Element

# Tags whose default flow is inline.
INLINE_ELEMENTS = {
    "a", "b", "i", "em", "strong", "span", "small", "big", "sub", "sup",
    "code", "tt", "kbd", "samp", "u", "abbr", "cite", "q", "s", "strike",
    "font", "label", "br", "img", "input", "button", "mark", "time", "var",
    "select", "textarea", "option", "optgroup", "video",
}

_FONT_CACHE = {}

# Measuring a character means finding the face that covers it and scaling
# that glyph's advance, and reading metrics means the same work over a whole
# face. Repeatedly measuring the same word with the same font dominates
# layout time on text-heavy pages, so both are memoized keyed by
# (font key, arg). Bounded so a wild page full of unique strings cannot grow
# the cache without limit.
_MEASURE_CACHE = {}
_METRICS_CACHE = {}
_MEASURE_CACHE_MAX = 100_000
# Measuring applies no kerning and no ligatures, so
# measure("abc") == measure("a") + measure("b") + measure("c") exactly. Only
# each unique (font, char) therefore has to be measured at all; a word's width
# is a Python sum over this table. Bounded because a page full of exotic
# codepoints must not grow it without limit.
_CHAR_CACHE = {}
_CHAR_CACHE_MAX = 50_000


def get_font(size, weight, style, family=""):
    key = (size, weight, style, family)
    if key not in _FONT_CACHE:
        fam = family if family else "Times"
        font = Font(size=size, weight=weight, slant=style, family=fam)
        font._ftbs_key = key  # stable cache identity for memo tables
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


def _measure(font, text):
    """Memoized font.measure(text). With no kerning applied, this is exactly
    the sum of the per-character widths, so only unique characters are ever
    measured."""
    if not text:
        return 0.0
    key = (font._ftbs_key, text)
    try:
        return _MEASURE_CACHE[key]
    except KeyError:
        pass
    width = 0.0
    for ch in text:
        ckey = (font._ftbs_key, ch)
        try:
            width += _CHAR_CACHE[ckey]
        except KeyError:
            cw = font.measure(ch)
            if len(_CHAR_CACHE) < _CHAR_CACHE_MAX:
                _CHAR_CACHE[ckey] = cw
            width += cw
    if len(_MEASURE_CACHE) < _MEASURE_CACHE_MAX:
        _MEASURE_CACHE[key] = width
    return width


def _metrics(font, name):
    """Memoized font.metrics(name): ascent/descent/linespace are constant per
    font, and flush() queries them for every line item."""
    key = (font._ftbs_key, name)
    try:
        return _METRICS_CACHE[key]
    except KeyError:
        pass
    value = font.metrics(name)
    _METRICS_CACHE[key] = value
    return value


def _linespace(font):
    return _metrics(font, "linespace")


# `line-height` values that mean "use the font's own idea of a line".
_LINE_HEIGHT_AUTO = frozenset({
    "", "normal", "auto", "inherit", "initial", "unset", "revert",
})


def _line_height(node, font):
    """The used `line-height` for an inline box, in px (CSS 2.1 sec. 10.8).

    Four forms, and the difference between them is *what inherits*:

    * `normal` -- the face's own line spacing, ascent + descent + the line gap
      the designer put in the font. That is what `_linespace` reads out of
      hhea, and for a typical text face it lands near 1.12-1.15em, which is
      also where Chrome lands. Nothing is hardcoded here.
    * a bare number (`1.5`) -- the *factor* is what inherits, not the result,
      so it is multiplied by each box's own font-size afresh. A 32px heading
      inside a `line-height: 1.5` body gets 48px, not 24px.
    * a length (`24px`, `1.5em`) -- computes to a length on the element that
      declares it, and that length is what inherits.
    * a percentage (`150%`) -- likewise resolved against the declaring
      element's font-size, with the *result* inherited.

    The last two are why `_lh_font_size` exists: the cascade hands every
    descendant the same declaration text, so `em` and `%` have to be resolved
    against the element the declaration came from rather than the box using
    it.
    """
    style = getattr(node, "style", None) or {}
    raw = style.get("line-height") or "normal"
    # The overwhelmingly common value, and it arrives already normalised from
    # the cascade's defaults, so spend nothing on it.
    if raw == "normal" or raw.strip().lower() in _LINE_HEIGHT_AUTO:
        return float(_linespace(font))
    raw = raw.strip()
    cached = getattr(node, "_ftbs_lh", None)
    if cached is not None and cached[0] == raw and cached[1] is font:
        return cached[2]
    value = _compute_line_height(node, raw, font)
    try:
        # Keyed by the declaration and the font, so a restyle or a font-size
        # change recomputes. Only a change to an *ancestor's* font-size that
        # leaves this box's own untouched can go stale, and that combination
        # needs a length line-height declared upstream of a JS font-size
        # change -- rare enough to be worth one attribute lookup per word.
        node._ftbs_lh = (raw, font, value)
    except AttributeError:
        pass
    return value


def _compute_line_height(node, raw, font):
    low = raw.lower()
    try:
        # A unitless number is a factor on this box's own font-size.
        return float(low) * float(font.size)
    except ValueError:
        pass
    if low.endswith("%"):
        try:
            return float(low[:-1]) / 100.0 * _lh_font_size(node, raw, font)
        except ValueError:
            return float(_linespace(font))
    if low.endswith("em") and not low.endswith("rem"):
        try:
            return float(low[:-2]) * _lh_font_size(node, raw, font)
        except ValueError:
            return float(_linespace(font))
    # px, rem, pt, vw/vh -- absolute once computed, so parse_px is enough.
    value = parse_px(raw, -1.0)
    if value < 0:
        return float(_linespace(font))  # negative or unparseable: invalid
    return value


def _lh_font_size(node, raw, font):
    """Font-size, in px, of the element a `line-height` declaration came from.

    Inheritance copies the declaration text unchanged, so every box under a
    `line-height: 1.2em` carries the identical string. The element that wrote
    it is therefore the topmost of the unbroken run of ancestors carrying it,
    and its font-size is the one the length resolves against.
    """
    owner = None
    n = node
    while n is not None:
        style = getattr(n, "style", None) or {}
        if (style.get("line-height") or "").strip() != raw:
            break
        if isinstance(n, Element):
            owner = n
        n = n.parent
    if owner is None:
        return float(font.size)
    return float(_node_font(owner).size)


def _warm_chars(font, chars):
    """Measure every character in `chars` that is not cached yet.

    Measuring past the cache ceiling is still worth doing: the font keeps its
    own per-character widths, and resolving a character to the face that
    covers it is the expensive half.
    """
    key = font._ftbs_key
    for c in chars:
        if (key, c) in _CHAR_CACHE:
            continue
        width = font.measure(c)
        if len(_CHAR_CACHE) < _CHAR_CACHE_MAX:
            _CHAR_CACHE[(key, c)] = width


def _prewarm(root_node):
    """Measure every distinct character the text layout will need up front.

    A page's text is a few dozen distinct characters repeated thousands of
    times. Because a word's width is the sum of its characters' widths, doing
    the whole alphabet once per font here makes every later _measure() call a
    dictionary lookup and an addition.
    """
    pending = {}  # font._ftbs_key -> (font, set of chars)
    stack = [root_node]
    while stack:
        node = stack.pop()
        if isinstance(node, Text):
            font = _node_font(node)
            key = font._ftbs_key
            if key not in pending:
                pending[key] = (font, set())
            pending[key][1].add(" ")
            pending[key][1].update(node.text)
        else:
            stack.extend(node.children)
    for font, chars in pending.values():
        _warm_chars(font, chars)


# Map common web font-family names onto the three generics the font engine
# keeps fallback chains for. We can't know which fonts are actually
# installed, so we walk the whole family stack and stop at the first name we
# can map; an unrecognised first name is handed over verbatim, and canvas.py
# falls back for it if nothing on the system answers to it.
_FAMILY_GENERICS = {
    # sans-serif
    "sans-serif": "Helvetica", "system-ui": "Helvetica",
    "-apple-system": "Helvetica", "blinkmacsystemfont": "Helvetica",
    "segoe ui": "Helvetica", "roboto": "Helvetica", "open sans": "Helvetica",
    "arial": "Helvetica", "helvetica": "Helvetica", "helvetica neue": "Helvetica",
    "verdana": "Helvetica", "tahoma": "Helvetica", "trebuchet ms": "Helvetica",
    "dejavu sans": "Helvetica", "liberation sans": "Helvetica",
    "noto sans": "Helvetica", "source sans": "Helvetica", "calibri": "Helvetica",
    "candara": "Helvetica", "century gothic": "Helvetica", "gill sans": "Helvetica",
    "futura": "Helvetica", "lucida grande": "Helvetica",
    "lucida sans unicode": "Helvetica", "pt sans": "Helvetica",
    "ui-sans-serif": "Helvetica",
    # serif
    "serif": "Times", "times": "Times", "times new roman": "Times",
    "georgia": "Times", "palatino linotype": "Times", "book antiqua": "Times",
    "linux libertine": "Times", "garamond": "Times", "dejavu serif": "Times",
    "bitstream vera serif": "Times", "cambria": "Times", "noto serif": "Times",
    "charter": "Times", "hoefler text": "Times", "source serif": "Times",
    "ui-serif": "Times", "liberation serif": "Times",
    # monospace
    "monospace": "Courier", "courier": "Courier", "courier new": "Courier",
    "consolas": "Courier", "menlo": "Courier", "monaco": "Courier",
    "dejavu sans mono": "Courier", "liberation mono": "Courier",
    "bitstream vera sans mono": "Courier", "source code pro": "Courier",
    "fira mono": "Courier", "inconsolata": "Courier", "ui-monospace": "Courier",
}


def _node_font(node):
    style = getattr(node, "style", {}) or {}
    size = int(round(parse_px(style.get("font-size", "16px"), 16)))
    size = max(6, min(size, 80))
    weight = "bold" if style.get("font-weight") in ("bold", "bolder", "600",
                                                     "700", "800", "900") else "normal"
    slant = "italic" if style.get("font-style") in ("italic", "oblique") else "roman"
    fam = style.get("font-family", "")
    if fam:
        resolved = None
        for part in fam.split(","):
            name = part.strip().strip("'\"")
            if not name:
                continue
            generic = _FAMILY_GENERICS.get(name.lower())
            if generic:
                resolved = generic
                break
        fam = resolved if resolved else fam.split(",")[0].strip().strip("'\"")
        if fam.lower() in ("inherit", "initial", "unset"):
            fam = ""
    return get_font(size, weight, slant, fam)


class DrawText:
    def __init__(self, x1, y1, text, font, color, node=None):
        self.top = y1
        self.left = x1
        self.text = text
        self.font = font
        self.color = color
        self.node = node  # source DOM node, for hit-testing links
        self.right = x1 + _measure(font, text)
        self.bottom = y1 + _metrics(font, "linespace")

    def hit(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill=self.color or "black", anchor="nw",
                tags=tags)
        except CanvasError:
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill="black", anchor="nw", tags=tags)


class _DrawShape:
    """Shared rectangle geometry for the fill/line/outline commands."""

    def __init__(self, x1, y1, x2, y2, color, thickness=0):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.color = color
        self.thickness = thickness


class DrawRect(_DrawShape):
    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill=self.color, tags=tags)
        except CanvasError:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill="black", tags=tags)


class DrawOval(_DrawShape):
    """An ellipse, filled or hollow. List markers are the only user so far:
    `disc` is a filled dot, `circle` the same dot as a ring."""

    def __init__(self, x1, y1, x2, y2, fill=None, outline=None):
        super().__init__(x1, y1, x2, y2, fill or outline, 1 if outline else 0)
        self.fill = fill
        self.outline = outline

    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_oval(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill=self.fill or "", outline=self.outline or "",
                width=1 if self.outline else 0, tags=tags)
        except CanvasError:
            canvas.create_oval(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill="black", outline="", width=0, tags=tags)


class DrawLine(_DrawShape):
    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_line(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill=self.color, width=self.thickness, tags=tags)
        except CanvasError:
            canvas.create_line(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill="black", width=self.thickness, tags=tags)


class DrawOutline(_DrawShape):
    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=self.thickness, outline=self.color, tags=tags)
        except CanvasError:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=self.thickness, outline="black", tags=tags)


class DrawShadow(_DrawShape):
    """A dithered (semi-transparent-looking) rectangle used for box-shadow."""

    def execute(self, scroll, canvas, tags=()):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill=self.color, stipple="gray50", tags=tags)
        except CanvasError:
            pass


class DrawImage:
    """Draws a decoded PhotoImage at the given rectangle."""

    def __init__(self, x1, y1, x2, y2, photo, node=None):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.photo = photo
        self.node = node  # source <img>, for hit-testing links

    def hit(self, x, y):
        return (self.left <= x <= self.right
                and self.top <= y <= self.bottom)

    def execute(self, scroll, canvas, tags=()):
        canvas.create_image(
            self.left, self.top - scroll, anchor="nw", image=self.photo,
            tags=tags)


class DrawVideo:
    """The current frame of a `<video>`.

    Separate from `DrawImage` for one reason, not the hit-testing one:
    both answer `hit()`, so a click on either reaches its element and can
    play or pause a video or follow an `<a>` around an image. What
    `DrawVideo` carries that `DrawImage` does not is a `photo` that is a
    buffer the player rewrites in place rather than a decoded file, so the
    command stays valid across frames and the retained canvas item is not
    rebuilt sixty times a second.
    """

    def __init__(self, x1, y1, x2, y2, photo, node=None):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.photo = photo
        self.node = node

    def hit(self, x, y):
        return (self.left <= x <= self.right
                and self.top <= y <= self.bottom)

    def execute(self, scroll, canvas, tags=()):
        canvas.create_image(
            self.left, self.top - scroll, anchor="nw", image=self.photo,
            tags=tags)


# The transport bar drawn over the bottom of a `<video controls>`. Sizes are
# in device pixels and deliberately not scaled by anything: a control you
# have to aim at is worse than one that always looks the same.
CONTROLS_HEIGHT = 28
CONTROLS_MIN_WIDTH = 120        # below this the bar has nowhere to put a groove
CONTROLS_MIN_HEIGHT = 72        # below this it would cover the picture
CONTROLS_PAD = 8
GROOVE_HEIGHT = 4
KNOB_RADIUS = 5


def format_media_time(seconds):
    """`m:ss`, or `h:mm:ss` once there is an hour of it. What every player
    writes, and short enough to fit beside a scrubber on a small video."""
    if seconds != seconds or seconds < 0:        # NaN from a duration of 0/0
        seconds = 0.0
    total = int(seconds)
    if total >= 3600:
        return "%d:%02d:%02d" % (total // 3600, (total // 60) % 60, total % 60)
    return "%d:%02d" % (total // 60, total % 60)


class DrawVideoControls:
    """The play/pause button and scrubber over the foot of a `<video>`.

    This one command paints several primitives, which none of the others do,
    and the reason is that it is the only thing on the page whose *geometry*
    changes without layout running again. A film advancing means the playhead
    moves every frame, and the frame timer repaints the existing display list
    rather than rebuilding it -- `DrawVideo` gets away with that because its
    photo is rewritten in place, and a scrubber cannot, because where the
    knob goes is a number rather than a buffer. So the bar reads the player
    at execute() time and draws itself from that.

    It is also why hit-testing lives here: the rectangles a click has to be
    compared against are the same ones execute() draws, and having two copies
    of that arithmetic is how a button ends up half a pixel from where it
    looks.
    """

    def __init__(self, x1, y1, x2, y2, node=None, player=None, font=None):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.node = node
        self.player = player
        self.font = font

    def hit(self, x, y):
        return (self.left <= x <= self.right
                and self.top <= y <= self.bottom)

    # -- geometry, shared by the painter and the hit test -------------------

    def button_rect(self):
        size = self.bottom - self.top
        return (self.left, self.top, self.left + size, self.bottom)

    def groove_rect(self):
        """The scrubber's track: from the right of the button to the left of
        the time readout, which is measured rather than guessed so a clip an
        hour long does not have its digits drawn over."""
        left = self.left + (self.bottom - self.top) + CONTROLS_PAD
        right = self.right - CONTROLS_PAD - self._time_width()
        middle = (self.top + self.bottom) / 2
        return (left, middle - GROOVE_HEIGHT / 2,
                max(left + 1, right), middle + GROOVE_HEIGHT / 2)

    def _time_width(self):
        if self.font is None:
            return 0
        return _measure(self.font, self._time_text()) + CONTROLS_PAD

    def _time_text(self):
        player = self.player
        if player is None:
            return "0:00"
        duration = getattr(player.info, "duration", 0.0) if player.info else 0.0
        return "%s / %s" % (format_media_time(player.position()),
                            format_media_time(duration))

    def _fraction(self):
        player = self.player
        duration = getattr(player.info, "duration", 0.0) \
            if player is not None and player.info else 0.0
        if not duration:
            return 0.0
        return max(0.0, min(1.0, player.position() / duration))

    # -- what a click on it means -------------------------------------------

    def action_at(self, x, y):
        """("toggle", None), ("seek", seconds), or None for a click that the
        bar swallows without doing anything."""
        player = self.player
        if player is None:
            return None
        bx0, _by0, bx1, _by1 = self.button_rect()
        if bx0 <= x <= bx1:
            return ("toggle", None)
        gx0, _gy0, gx1, _gy1 = self.groove_rect()
        # The whole height of the bar counts as the groove. A four-pixel
        # target is not a target, and there is nothing else along that strip
        # to hit by accident.
        if gx0 - KNOB_RADIUS <= x <= gx1 + KNOB_RADIUS and gx1 > gx0:
            duration = getattr(player.info, "duration", 0.0) \
                if player.info else 0.0
            fraction = max(0.0, min(1.0, (x - gx0) / (gx1 - gx0)))
            return ("seek", fraction * duration)
        return None

    # -- painting ------------------------------------------------------------

    def execute(self, scroll, canvas, tags=()):
        top = self.top - scroll
        bottom = self.bottom - scroll
        try:
            canvas.create_rectangle(self.left, top, self.right, bottom,
                                    width=0, fill="#000000", stipple="gray50",
                                    tags=tags)
            self._draw_button(canvas, top, bottom, tags)
            self._draw_groove(canvas, scroll, tags)
            if self.font is not None:
                canvas.create_text(self.right - CONTROLS_PAD,
                                   (top + bottom) / 2, text=self._time_text(),
                                   font=self.font, fill="#ffffff", anchor="e",
                                   tags=tags)
        except CanvasError:
            pass

    def _draw_button(self, canvas, top, bottom, tags):
        x0, _y0, x1, _y1 = self.button_rect()
        cx = (x0 + x1) / 2
        cy = (top + bottom) / 2
        size = min(x1 - x0, bottom - top) * 0.42
        if self.player is not None and self.player.playing:
            bar = size * 0.34
            canvas.create_rectangle(cx - size * 0.55, cy - size,
                                    cx - size * 0.55 + bar, cy + size,
                                    width=0, fill="#ffffff", tags=tags)
            canvas.create_rectangle(cx + size * 0.55 - bar, cy - size,
                                    cx + size * 0.55, cy + size,
                                    width=0, fill="#ffffff", tags=tags)
        else:
            canvas.create_polygon(cx - size * 0.6, cy - size,
                                  cx + size * 0.85, cy,
                                  cx - size * 0.6, cy + size,
                                  fill="#ffffff", tags=tags)

    def _draw_groove(self, canvas, scroll, tags):
        x0, y0, x1, y1 = self.groove_rect()
        if x1 <= x0:
            return
        y0 -= scroll
        y1 -= scroll
        canvas.create_rectangle(x0, y0, x1, y1, width=0, fill="#ffffff",
                                stipple="gray50", tags=tags)
        played = x0 + (x1 - x0) * self._fraction()
        if played > x0:
            canvas.create_rectangle(x0, y0, played, y1, width=0,
                                    fill="#ffffff", tags=tags)
        canvas.create_oval(played - KNOB_RADIUS, (y0 + y1) / 2 - KNOB_RADIUS,
                           played + KNOB_RADIUS, (y0 + y1) / 2 + KNOB_RADIUS,
                           fill="#ffffff", outline="", tags=tags)


def _video_attr(node, name):
    """A `<video>` width/height attribute as a positive int, or 0.

    HTML says these are bare integers, and a page that writes `width="80%"`
    is asking for the CSS property instead. Anything we cannot read as a
    plain number is treated as absent rather than guessed at.
    """
    raw = node.attributes.get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 10000 else 0


def _video_controls(node, left, top, width, height):
    """A `DrawVideoControls` for this element, or None.

    Two reasons to say None, and both are the same reason a real browser
    gives: the page did not ask for controls, or the box is too small to put
    them in without covering the film. HTML makes the attribute the whole
    switch -- a `<video>` without it is a picture, not a player -- and the
    click-anywhere-to-toggle behaviour is what such an element still has.
    """
    if not isinstance(node, Element) or "controls" not in node.attributes:
        return None
    player = getattr(node, "video_player", None)
    if player is None or player.track is None:
        return None
    if width < CONTROLS_MIN_WIDTH or height < CONTROLS_MIN_HEIGHT:
        return None
    return DrawVideoControls(left, top + height - CONTROLS_HEIGHT,
                             left + width, top + height, node, player,
                             get_font(11, "normal", "roman"))


def _video_label(node, player):
    """What to write in the box when there is no picture to show. The point
    is to say something true and specific -- the codec and the reason -- so a
    page that does not play tells you why."""
    if player is None:
        src = node.attributes.get("src", "")
        return "[video: loading]" if src else "[video: no source]"
    if player.error:
        return "[video: %s]" % player.error
    return "[video]"


# `calc()` and its relatives. A page written this decade puts arithmetic in
# nearly every length it cares about -- `calc(100% - 240px)` for a column
# beside a fixed sidebar, `min(100%, 60rem)` for a measure that stops growing
# -- and a length we cannot read falls back to zero, which collapses the box.
_MATH_FUNCS = ("calc", "min", "max", "clamp")
_MATH_RE = re.compile(r"^(-?)(%s)\(" % "|".join(_MATH_FUNCS))


def _is_math(value):
    return bool(_MATH_RE.match((value or "").strip().lower()))


def _calc_tokens(expr):
    """Split a calc() body into operands, operators, commas and parens.

    `+` and `-` are operators only with a space in front of them, which is
    exactly the rule CSS uses -- without it there is no telling `10px -5px`
    (two lengths) from `10px - 5px` (one subtraction).
    """
    tokens = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "(),*/":
            tokens.append(ch)
            i += 1
            continue
        if ch in "+-" and (not tokens or i == 0 or expr[i - 1].isspace()) \
                and i + 1 < n and expr[i + 1].isspace():
            tokens.append(ch)
            i += 1
            continue
        # An operand is at least one character long, and the character at `i`
        # is that one: everything the scan below breaks on -- whitespace, a
        # paren, a comma, an operator -- was already handled above, so
        # whatever is here belongs to this token whatever it looks like.
        # Starting at `i` instead let `calc(100% -1px)` break immediately (a
        # '-' after a space, but without one after it, is not the operator
        # the branch above accepts), emit a zero-width token, and leave `i`
        # where it was: an infinite loop reached from every length in the
        # sheet. It also read expr[-1] -- the far end of the string -- when
        # the expression began with one.
        j = i + 1
        depth = 0
        while j < n:
            c = expr[j]
            if c == "(":
                depth += 1
            elif c == ")" and depth:
                depth -= 1
            elif not depth and (c.isspace() or c in "(),*/"):
                break
            elif not depth and c in "+-" and expr[j - 1].isspace():
                break
            j += 1
        tokens.append(expr[i:j])
        i = j
    return tokens


class _CalcParser:
    """Arithmetic over lengths. Operands go through the ordinary length
    parser, so everything it understands -- px, rem, %, vh -- works here."""

    def __init__(self, tokens, base, default):
        self.tokens = tokens
        self.pos = 0
        self.base = base
        self.default = default

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def sum(self):
        value = self.product()
        while self._peek() in ("+", "-"):
            op = self.tokens[self.pos]
            self.pos += 1
            right = self.product()
            value = value + right if op == "+" else value - right
        return value

    def product(self):
        value = self.unary()
        while self._peek() in ("*", "/"):
            op = self.tokens[self.pos]
            self.pos += 1
            right = self.unary()
            if op == "*":
                value *= right
            else:
                value = value / right if right else 0.0
        return value

    def unary(self):
        tok = self._peek()
        if tok is None:
            return 0.0
        self.pos += 1
        if tok == "(":
            value = self.sum()
            if self._peek() == ")":
                self.pos += 1
            return value
        if tok == ")":
            return 0.0
        return _resolve_len(tok, self.base, 0.0)

    def args(self):
        """Comma-separated sums, for min()/max()/clamp()."""
        values = [self.sum()]
        while self._peek() == ",":
            self.pos += 1
            values.append(self.sum())
        return values


def _eval_math(value, base, default=0.0):
    """Resolve `calc()`, `min()`, `max()` or `clamp()` to pixels."""
    v = (value or "").strip()
    sign = 1.0
    if v.startswith("-"):
        sign, v = -1.0, v[1:]
    name, _, rest = v.partition("(")
    name = name.strip().lower()
    if not rest.endswith(")"):
        return default
    parser = _CalcParser(_calc_tokens(rest[:-1]), base, default)
    try:
        if name == "calc":
            return sign * parser.sum()
        args = parser.args()
        if not args:
            return default
        if name == "min":
            return sign * min(args)
        if name == "max":
            return sign * max(args)
        # clamp(low, preferred, high)
        low, pref, high = (args + args[-1:] * 2)[:3]
        return sign * max(low, min(pref, high))
    except (ValueError, TypeError, ZeroDivisionError):
        return default


def parse_px(value, default=0.0):
    if _is_math(value):
        # No percentage base on this path; `calc(50% - 8px)` needs the width
        # only _resolve_len knows, so it lands on the default there.
        return _eval_math(value, 0.0, default)
    try:
        if value.endswith("px"):
            return float(value[:-2])
        if value.endswith("rem"):
            return float(value[:-3]) * 16.0
        if value.endswith("em"):
            # No element context here, so this is the root size. Where the
            # element's own font matters -- font-size itself -- the cascade
            # has already resolved it against the parent.
            return float(value[:-2]) * 16.0
        if value.endswith("pt"):
            return float(value[:-2]) * 4.0 / 3.0
        for unit in ("vw", "vh", "vmin", "vmax"):
            if value.endswith(unit):
                width, height = cssparser.get_viewport()
                extent = {"vw": width, "vh": height,
                          "vmin": min(width, height),
                          "vmax": max(width, height)}[unit]
                return float(value[:-len(unit)]) / 100.0 * extent
        if value.endswith("%"):
            return default
        return float(value)
    except (ValueError, AttributeError):
        return default


def _resolve_len(value, base, default=0.0):
    """Parse a CSS length for a horizontal axis: px/rem/bare numbers via
    parse_px, and percentages resolved against `base` (the containing width)."""
    v = (value or "").strip()
    if _is_math(v):
        return _eval_math(v, base, default)
    if v.endswith("%"):
        try:
            return float(v[:-1]) / 100.0 * base
        except ValueError:
            return default
    return parse_px(v, default)


def _margin_side(style, side):
    """Resolve one horizontal margin (longhand or the `margin` shorthand,
    which the cascade stores un-expanded) to (px, is_auto)."""
    v = style.get("margin-left" if side == "left" else "margin-right", "")
    if not v:
        sh = style.get("margin", "")
        if sh:
            parts = sh.split()
            if len(parts) == 1:
                v = parts[0]
            elif len(parts) in (2, 3):
                v = parts[1]
            else:
                v = parts[1] if side == "right" else parts[3]
    v = v.strip()
    if not v:
        return 0.0, False
    if v.lower() == "auto":
        return 0.0, True
    return parse_px(v, 0.0), False


def _padding_box(style):
    """Expand the `padding` shorthand (1-4 values) into the per-side longhands
    that layout reads, falling back to whatever explicit sides are set."""
    top = parse_px(style.get("padding-top", "0"))
    right = parse_px(style.get("padding-right", "0"))
    bottom = parse_px(style.get("padding-bottom", "0"))
    left = parse_px(style.get("padding-left", "0"))
    shorthand = style.get("padding")
    if shorthand:
        parts = shorthand.split()
        if len(parts) == 1:
            v = parse_px(parts[0]); top = right = bottom = left = v
        elif len(parts) == 2:
            v = parse_px(parts[0]); h = parse_px(parts[1])
            top = bottom = v; right = left = h
        elif len(parts) == 3:
            top = parse_px(parts[0])
            h = parse_px(parts[1]); right = left = h
            bottom = parse_px(parts[2])
        elif len(parts) == 4:
            top = parse_px(parts[0]); right = parse_px(parts[1])
            bottom = parse_px(parts[2]); left = parse_px(parts[3])
    return top, right, bottom, left


_COLOR_FUNC_RE = re.compile(r"^(rgba?|hsla?)\((.*)\)$", re.DOTALL)


def _color_channels(name):
    """Split the inside of rgb()/rgba()/hsl()/hsla() into channel strings,
    accepting both comma and modern space/slash syntax."""
    m = _COLOR_FUNC_RE.match(name)
    if not m:
        return None
    inner = re.sub(r"[,/]", " ", m.group(2)).strip()
    parts = [p for p in inner.split() if p]
    if len(parts) not in (3, 4):
        return None
    return m.group(1), parts


def _color_channel(v):
    """Convert a CSS channel value (0-255 or percentage) to an int 0-255."""
    v = v.strip()
    try:
        if v.endswith("%"):
            val = float(v[:-1]) / 100.0 * 255.0
        else:
            val = float(v)
    except ValueError:
        return 0
    return max(0, min(255, int(round(val))))


def _color_alpha(v):
    """Convert a CSS alpha value (0-1 or percentage) to a float."""
    if v is None:
        return 1.0
    v = v.strip()
    try:
        if v.endswith("%"):
            return max(0.0, min(1.0, float(v[:-1]) / 100.0))
        return max(0.0, min(1.0, float(v)))
    except ValueError:
        return 1.0


def _hsl_to_rgb(h, s, l):
    h = (h % 360) / 360.0
    s = max(0.0, min(1.0, s))
    l = max(0.0, min(1.0, l))
    if s == 0:
        return l, l, l
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    def hue(t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p
    return hue(h + 1 / 3), hue(h), hue(h - 1 / 3)


def _parse_hue(v):
    v = v.strip().lower()
    if v.endswith("%"):
        return float(v[:-1]) / 100.0 * 360.0
    for unit, factor in (("rad", 180 / 3.141592653589793),
                         ("grad", 0.9), ("turn", 360.0), ("deg", 1.0)):
        if v.endswith(unit):
            return float(v[:-len(unit)]) * factor
    return float(v)


_COLOR_NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")
_COLOR_HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


def _color_function_args(name, func):
    """The comma-separated arguments of `func(...)`, nesting respected."""
    if not name.startswith(func + "(") or not name.endswith(")"):
        return None
    inner = name[len(func) + 1:-1]
    args, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
    args.append(inner[start:].strip())
    return [a for a in args if a]


def resolve_color(name):
    if not name:
        return None
    name = name.strip().lower()
    if name in ("transparent", "none", "currentcolor", "inherit", "initial"):
        return None
    # light-dark() asks which scheme the browser is showing. This one has a
    # light chrome and reports `prefers-color-scheme: light`, so it takes the
    # first argument -- and taking it here is what keeps a site that themes
    # itself entirely through light-dark() from rendering as a black slab.
    if name.startswith("light-dark("):
        args = _color_function_args(name, "light-dark")
        return resolve_color(args[0]) if args else None
    # Gradients / image() / url() are not flat colors; hand them to the
    # caller (or ignore them) rather than paint an unreadable black box.
    if "gradient(" in name or name.startswith("url(") \
            or name.startswith("image("):
        return None
    parsed = _color_channels(name)
    if parsed:
        kind, parts = parsed
        a = parts[3] if len(parts) == 4 else None
        if _color_alpha(a) <= 0:
            return None
        if kind.startswith("rgb"):
            r, g, b = parts[:3]
            return "#%02x%02x%02x" % (
                _color_channel(r), _color_channel(g), _color_channel(b))
        h, s, l = parts[:3]
        sval = float(s.rstrip("%")) / 100.0 if s.endswith("%") else float(s)
        lval = float(l.rstrip("%")) / 100.0 if l.endswith("%") else float(l)
        r, g, b = _hsl_to_rgb(_parse_hue(h), sval, lval)
        return "#%02x%02x%02x" % (
            int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
    # 3/4/6/8-digit hex: canvas.color() reads #rgb and #rrggbb, so expand the
    # four- and eight-digit forms here and drop the alpha channel.
    if name.startswith("#") and len(name) in (4, 5):
        n = "".join(c * 2 for c in name[1:])
        if len(n) == 8 and n[6:] == "00":
            return None  # alpha 0
        return "#" + n[:6]
    if len(name) == 9 and name.startswith("#"):
        if name[7:] == "00":
            return None  # #rrggbbaa with alpha 0
        return name[:7]
    if name.startswith("#"):
        return name if _COLOR_HEX_RE.match(name) else None
    if not _COLOR_NAME_RE.match(name):
        # Something we do not understand: a function we have no answer for, a
        # malformed hex, or several tokens where one colour belongs. Saying
        # "no colour" leaves the box unpainted, which is nearly always closer
        # to the page's intent than the flat black the canvas falls back to.
        return None
    return name  # a colour name the canvas can look up


_ROMAN_NUMERALS = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
                   (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                   (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def _roman(number):
    if number <= 0:
        return str(number)
    out = []
    for value, numeral in _ROMAN_NUMERALS:
        while number >= value:
            out.append(numeral)
            number -= value
    return "".join(out)


def _alpha(number):
    """1 -> a, 26 -> z, 27 -> aa: how a list counts in letters."""
    if number <= 0:
        return str(number)
    out = []
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        out.append(chr(ord("a") + remainder))
    return "".join(reversed(out))


def _marker_text(kind, index):
    """The text of a counting list marker, or None for the bullet shapes."""
    if kind == "decimal":
        return "%d." % index
    if kind == "decimal-leading-zero":
        return "%02d." % index
    if kind in ("lower-alpha", "lower-latin"):
        return "%s." % _alpha(index)
    if kind in ("upper-alpha", "upper-latin"):
        return "%s." % _alpha(index).upper()
    if kind == "lower-roman":
        return "%s." % _roman(index)
    if kind == "upper-roman":
        return "%s." % _roman(index).upper()
    return None


def _int_attr(node, name, fallback):
    if not isinstance(node, Element):
        return fallback
    try:
        return int(node.attributes.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


def _list_index(node):
    """What number this list item shows.

    There is one counter per list, not one number per item: `<ol start>` seeds
    it, `<li value>` sets it, and every item after that carries on from
    wherever it was left -- so a `value="9"` renumbers its whole tail.
    """
    parent = node.parent
    if not isinstance(parent, Element):
        return _int_attr(node, "value", 1)
    index = _int_attr(parent, "start", 1)
    for child in parent.children:
        if not (isinstance(child, Element) and
                child.style.get("display") == "list-item"):
            continue
        index = _int_attr(child, "value", index)
        if child is node:
            return index
        index += 1
    return _int_attr(node, "value", index)


def _is_inline_level(node):
    """True when `node` lays out inside a line rather than starting one.

    An element is inline either because CSS said so or because it is one of
    the tags that is inline by default and nothing overrode it.
    """
    if not isinstance(node, Element):
        return False
    display = node.style.get("display")
    if display == "none":
        return False
    if display is None:
        return node.tag in INLINE_ELEMENTS
    return display in ("inline", "inline-block")


def _is_atomic_inline(node):
    """Whether this box sits on the line as a single unbreakable thing.

    An inline-block takes part in the line like a word does, but inside it
    lays out as a block -- so nothing within it can break the line it sits
    on, and nothing outside it needs to know what is in there.
    """
    return isinstance(node, Element) \
        and node.style.get("display") == "inline-block"


def _wraps_block_content(node):
    """True when an inline element contains block-level content.

    The shape is `<a><div class="thumb"></div><div class="title"></div></a>`
    -- a whole card wrapped in one link. Treating that as inline would run
    the thumbnail and the title together on a single line, so the enclosing
    box has to lay out as a block. Only inline descendants are walked
    through: once a block turns up, the answer is settled.

    An inline-block is where the walk stops. It is one atom on the line and
    the blocks inside it are its own business -- that is the entire reason
    the value exists. Descending into one is how a byline with a <details>
    dropdown in the middle of it comes apart into a line per word.
    """
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if isinstance(child, Text):
            continue
        if child.style.get("display") == "none":
            continue
        if _is_atomic_inline(child):
            continue
        if _is_inline_level(child):
            stack.extend(child.children)
            continue
        return True
    return False


def _block_padding(node):
    """Vertical padding (top + bottom) of a block's own style."""
    pt, _pr, pb, _pl = _padding_box(getattr(node, "style", {}) or {})
    return pt + pb


def _dispatch_layout(box):
    """Route a laid-out box to its display-type layout algorithm."""
    node = box.node
    disp = node.style.get("display", "") if isinstance(node, Element) else ""
    if disp == "flex":
        box._layout_flex()
    elif disp == "grid":
        box._layout_grid()
    elif disp in ("table", "inline-table") or \
            (isinstance(node, Element) and node.tag == "table"):
        box._layout_table()
    elif box.layout_mode() == "block":
        box._layout_block()
    else:
        box._layout_inline()


_BORDER_SIDES = ("top", "right", "bottom", "left")
_BORDER_STYLES = ("none", "hidden", "solid", "dashed", "dotted", "double",
                  "groove", "ridge", "inset", "outset")
# CSS names three widths instead of giving numbers; these are the pixel
# values browsers settled on.
_BORDER_WIDTH_KEYWORDS = {"thin": 1.0, "medium": 3.0, "thick": 5.0}
# A value is a run of non-space characters, except that a function call
# keeps its parentheses (and the spaces inside them) together, so
# `1px solid rgb(0, 0, 0)` is three tokens rather than five.
_VALUE_TOKEN_RE = re.compile(r"[^\s(]+\([^)]*\)|\S+")


def _value_tokens(value):
    return _VALUE_TOKEN_RE.findall(value or "")


def _four_sides(value):
    """Map a 1-to-4 value box property onto its sides, CSS's clock order."""
    parts = _value_tokens(value)
    if not parts:
        return {}
    if len(parts) == 1:
        top = right = bottom = left = parts[0]
    elif len(parts) == 2:
        top, bottom, right, left = parts[0], parts[0], parts[1], parts[1]
    elif len(parts) == 3:
        top, right, bottom = parts
        left = right
    else:
        top, right, bottom, left = parts[:4]
    return {"top": top, "right": right, "bottom": bottom, "left": left}


def _border_width(token):
    token = token.strip().lower()
    if token in _BORDER_WIDTH_KEYWORDS:
        return _BORDER_WIDTH_KEYWORDS[token]
    return parse_px(token, 0.0)


def _parse_border_shorthand(value):
    """Split `2px solid #ccc` -- in any order, with any part missing -- into
    (width, style, color).

    The shorthand resets all three, so an omitted width becomes `medium` and
    an omitted style becomes `none`, exactly as the spec says. An omitted
    colour comes back as None, meaning `currentColor`.
    """
    width = style = color = None
    for token in _value_tokens(value):
        low = token.lower()
        if low in _BORDER_STYLES:
            style = low
        elif low in _BORDER_WIDTH_KEYWORDS or re.match(
                r"^[\d.]+(px|rem|em|pt|%)?$", low):
            width = _border_width(token)
        else:
            color = token
    return (width if width is not None else 3.0, style or "none", color)


def _border_box(style):
    """Resolve every border declaration into an (width, color) per side.

    The same edge can be described four ways, each more specific than the
    last: `border`, the `border-width`/`border-style`/`border-color` boxes,
    `border-left`, and `border-left-width`. They are applied in that order,
    which is what the cascade would have done had the shorthands been
    expanded when they were parsed. A side with no style, no width, or
    `style: none` reports a width of zero and is not painted.
    """
    widths = dict.fromkeys(_BORDER_SIDES, 0.0)
    styles = dict.fromkeys(_BORDER_SIDES, "none")
    colors = dict.fromkeys(_BORDER_SIDES, None)

    shorthand = style.get("border")
    if shorthand:
        width, kind, color = _parse_border_shorthand(shorthand)
        for side in _BORDER_SIDES:
            widths[side], styles[side], colors[side] = width, kind, color
    for side, token in _four_sides(style.get("border-width", "")).items():
        widths[side] = _border_width(token)
    for side, token in _four_sides(style.get("border-style", "")).items():
        styles[side] = token.lower()
    for side, token in _four_sides(style.get("border-color", "")).items():
        colors[side] = token
    for side in _BORDER_SIDES:
        edge = style.get("border-%s" % side)
        if edge:
            widths[side], styles[side], colors[side] = \
                _parse_border_shorthand(edge)
        width = style.get("border-%s-width" % side)
        if width:
            widths[side] = _border_width(width)
        kind = style.get("border-%s-style" % side)
        if kind:
            styles[side] = kind.strip().lower()
        color = style.get("border-%s-color" % side)
        if color:
            colors[side] = color

    resolved = {}
    for side in _BORDER_SIDES:
        if styles[side] in ("none", "hidden") or widths[side] <= 0:
            resolved[side] = (0.0, None)
            continue
        color = resolve_color(colors[side] or style.get("color", "black")) \
            or "black"
        resolved[side] = (widths[side], color)
    return resolved


def _paint_border(box, cmds):
    """Draw the box's borders as four filled edges.

    They are drawn *inside* the box rather than around it, which keeps a
    border from shifting the layout of everything after it -- the same
    bargain `box-sizing: border-box` makes. Every drawable style is painted
    solid; dashes and ridges would need stroke patterns the canvas does not
    have, and a solid line of the right weight and colour is much closer to
    the intent than nothing at all.
    """
    node = box.node
    if not isinstance(node, Element):
        return
    sides = _border_box(node.style)
    if not any(width for width, _color in sides.values()):
        return
    left, top = box.x, box.y
    right, bottom = box.x + box.width, box.y + box.height
    if right <= left or bottom <= top:
        return
    width, color = sides["top"]
    if width:
        cmds.append(DrawRect(left, top, right, min(top + width, bottom), color))
    width, color = sides["bottom"]
    if width:
        cmds.append(DrawRect(left, max(bottom - width, top), right, bottom,
                             color))
    width, color = sides["left"]
    if width:
        cmds.append(DrawRect(left, top, min(left + width, right), bottom,
                             color))
    width, color = sides["right"]
    if width:
        cmds.append(DrawRect(max(right - width, left), top, right, bottom,
                             color))


def _paint_bg(box, cmds, require_size=True):
    """Emit background paint for `box`: box-shadow (behind), then either a
    linear-gradient (bands) or the resolved flat background color, then the
    borders on top of both."""
    node = box.node
    if not isinstance(node, Element):
        return
    if require_size and not (box.width > 0 and box.height > 0):
        return
    _paint_box_shadow(box, cmds)
    grad = _gradient_spec(node)
    if grad is not None:
        cmds.extend(_gradient_rects(box, *grad))
        _paint_border(box, cmds)
        return
    bg = resolve_color(node.style.get("background-color")) or \
        resolve_color(node.style.get("background"))
    if bg:
        cmds.append(DrawRect(box.x, box.y, box.x + box.width,
                             box.y + box.height, bg))
    _paint_border(box, cmds)


def _paint_box_shadow(box, cmds):
    """Draw a dithered rectangle for a simple `box-shadow: x y [blur] [spread] color`."""
    node = box.node
    shadow = node.style.get("box-shadow") or ""
    if not shadow or shadow.strip() == "none" \
            or shadow.strip().startswith("inset"):
        return
    nums = []
    rest = []
    for tok in shadow.split():
        m = re.match(r"^([+-]?[\d.]+)(?:px)?$", tok)
        if m:
            nums.append(float(m.group(1)))
        else:
            rest.append(tok)
    if len(nums) < 2:
        return
    ox, oy = nums[0], nums[1]
    blur = nums[2] if len(nums) > 2 else 0
    color = resolve_color(" ".join(rest)) if rest else "#9a9a9a"
    if color is None:
        color = "#9a9a9a"
    cmds.append(DrawShadow(
        box.x + ox - blur, box.y + oy - blur,
        box.x + box.width + ox + blur, box.y + box.height + oy + blur,
        color))


def _gradient_spec(node):
    """Return (direction, [(color, pos%), ...]) from a linear-gradient
    background, or None. Direction is 'bottom'/'top'/'left'/'right'."""
    style = node.style
    spec = style.get("background-image") or ""
    if "linear-gradient(" not in spec:
        spec = style.get("background") or ""
    if "linear-gradient(" not in spec:
        return None
    inner = spec.split("linear-gradient(", 1)[1].rsplit(")", 1)[0]
    parts = [p.strip() for p in inner.split(",")]
    if not parts:
        return None
    direction = "bottom"
    first = parts[0].lower()
    if first.startswith("to "):
        direction = first[3:]
        parts = parts[1:]
    elif first.endswith("deg"):
        return None
    if direction not in ("top", "bottom", "left", "right"):
        return None
    stops = []
    for p in parts:
        m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)%$", p)
        if m:
            color, pos = m.group(1).strip(), float(m.group(2))
        else:
            color, pos = p, None
        rc = resolve_color(color)
        if rc is None:
            return None
        stops.append((rc, pos))
    if len(stops) < 2:
        return None
    if stops[0][1] is None:
        stops[0] = (stops[0][0], 0.0)
    if stops[-1][1] is None:
        stops[-1] = (stops[-1][0], 100.0)
    for i in range(1, len(stops) - 1):
        if stops[i][1] is not None:
            continue
        lo = stops[i - 1][1]
        hi = next((s[1] for s in stops[i + 1:] if s[1] is not None), 100.0)
        if lo is None:
            lo = 0.0
        stops[i] = (stops[i][0], lo + (hi - lo) / 2.0)
    return direction, stops


def _interp_color(c1, c2, t):
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    r1, g1, b1 = rgb(c1)
    r2, g2, b2 = rgb(c2)
    return "#%02x%02x%02x" % (
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t))


def _gradient_rects(box, direction, stops):
    """Turn a parsed gradient into a small set of solid bands."""
    w, h = box.width, box.height
    if w <= 0 or h <= 0:
        return []
    rects = []
    total = h if direction in ("top", "bottom") else w
    for i in range(len(stops) - 1):
        c1, p1 = stops[i]
        c2, p2 = stops[i + 1]
        a0 = total * p1 / 100.0
        a1 = total * p2 / 100.0
        n = max(1, min(8, int(abs(a1 - a0))))
        for b in range(n):
            t0 = b / n
            t1 = (b + 1) / n
            ba0 = a0 + (a1 - a0) * t0
            ba1 = a0 + (a1 - a0) * t1
            col = _interp_color(c1, c2, (t0 + t1) / 2)
            if direction == "bottom":
                rects.append(DrawRect(box.x, box.y + ba0,
                                      box.x + w, box.y + ba1, col))
            elif direction == "top":
                rects.append(DrawRect(box.x, box.y + total - ba1,
                                      box.x + w, box.y + total - ba0, col))
            elif direction == "left":
                rects.append(DrawRect(box.x + ba0, box.y,
                                      box.x + ba1, box.y + h, col))
            else:
                rects.append(DrawRect(box.x + total - ba1, box.y,
                                      box.x + total - ba0, box.y + h, col))
    return rects


def field_value(node):
    """The text a form field currently holds.

    The `value` attribute is the live store once anything has written to it,
    but a field that has never been touched still has to report what the
    markup gave it -- and for <textarea> that initial text is the element's
    content, not an attribute, so reading `value` alone loses a server-filled
    textarea on submit.
    """
    if "value" in node.attributes:
        return node.attributes["value"]
    if node.tag == "textarea":
        return "".join(c.text for c in node.children if isinstance(c, Text))
    return ""


def field_checked(node):
    """Whether a checkbox or radio is ticked right now.

    Clicks are recorded in `data-checked` rather than in `value`, so that the
    value the form wants submitted survives being toggled and the markup's
    own `checked` attribute still says what the initial state was.
    """
    state = node.attributes.get("data-checked")
    if state is not None:
        return state != "off"
    return "checked" in node.attributes


# -- <select> ---------------------------------------------------------------
#
# The painter here and the drop-down list in browser.py have to agree on what
# a <select> currently holds, so the reading of it lives in one place. The
# `selected` attribute on an <option> is the single source of truth; the
# select's own `value` attribute is a mirror the Tab keeps in step, because
# that attribute is what JavaScript sees when it reads `.value`.


def select_options(node):
    """The <option>s of a <select>, in document order, each paired with the
    label of the <optgroup> it came out of (None when it was not in one).

    Groups are flattened rather than nested because nothing about choosing an
    option cares about the nesting -- only the drawing of the list does, and
    the group label is all it needs.
    """
    out = []
    for child in node.children:
        if not isinstance(child, Element):
            continue
        if child.tag == "option":
            out.append((child, None))
        elif child.tag == "optgroup":
            label = child.attributes.get("label", "")
            for inner in child.children:
                if isinstance(inner, Element) and inner.tag == "option":
                    out.append((inner, label))
    return out


def option_label(option):
    """The text an <option> shows. HTML lets the label be given either as the
    element's text or, when there is none, as its `label` attribute."""
    text = "".join(c.text for c in option.children
                   if isinstance(c, Text)).strip()
    return text or option.attributes.get("label", "")


def option_value(option):
    """What an <option> submits: its `value` attribute, or its label when it
    has no `value` at all."""
    if "value" in option.attributes:
        return option.attributes["value"]
    return option_label(option)


def selected_options(node):
    """The chosen <option>s of a <select>.

    A single-choice select always has one even when the markup marks none:
    browsers fall back to the first option the user could have picked, and
    forms submit that, so the painter must show it too.
    """
    options = [opt for opt, _group in select_options(node)]
    chosen = [opt for opt in options if "selected" in opt.attributes]
    multiple = "multiple" in node.attributes
    if chosen:
        return chosen if multiple else chosen[-1:]
    if multiple:
        return []
    for opt in options:
        if "disabled" not in opt.attributes:
            return [opt]
    return []


# One line of a <select>'s list. A row with no `option` is an <optgroup>
# heading: it is drawn, but it can never be highlighted or chosen.
SelectRow = namedtuple("SelectRow", "option label enabled heading")


def select_rows(node):
    """Flatten a <select> into the lines its list shows.

    The drop-down and the expanded listbox show exactly the same lines, so
    they read them from here rather than each flattening the tree its own
    way and drifting apart.
    """
    rows = []
    group = None
    for option, group_label in select_options(node):
        if group_label is not None and group_label != group:
            rows.append(SelectRow(None, group_label, False, True))
        group = group_label
        rows.append(SelectRow(option, option_label(option),
                              "disabled" not in option.attributes, False))
    return rows


# Geometry of an expanded <select>. Hit-testing a click on one happens in
# browser.py against these same numbers, so they live next to the painter
# that draws them rather than being written down twice.
LISTBOX_ROW_H = 20
LISTBOX_PAD = 3
LISTBOX_PAD_X = 7
LISTBOX_INDENT = 12
LISTBOX_DEFAULT_ROWS = 4


def listbox_rows(node):
    """How many option rows an expanded <select> shows, or 0 when it is an
    ordinary drop-down.

    `size` asks for the expanded form outright. `multiple` implies it: a
    control offering more than one choice at a time has to show more than
    one row, or there is nothing to choose between, and browsers settle on
    about four when the markup does not say.
    """
    size = None
    raw = node.attributes.get("size")
    if raw is not None:
        try:
            size = int(str(raw).strip())
        except ValueError:
            size = None
    if size is not None and size > 1:
        return size
    if "multiple" in node.attributes:
        return size if size is not None and size > 0 else LISTBOX_DEFAULT_ROWS
    return 0


def listbox_scroll(node, nrows=None):
    """Index of the first row an expanded <select> is showing.

    Clamped on the way out, because the offset lives on the node and the
    list under it can shrink -- a script removing options must not leave
    the box scrolled past its own end.
    """
    visible = listbox_rows(node)
    if nrows is None:
        nrows = len(select_rows(node))
    raw = node.attributes.get("data-scroll")
    if raw is None:
        # Nobody has scrolled it yet. Opening at the top would be wrong for a
        # short box whose chosen option is a long way down it -- the reader
        # would be looking at rows their own choice is not among -- so the
        # resting position is wherever the choice is.
        i = listbox_active(node)
        top = 0 if i < visible else i - visible + 1
    else:
        try:
            top = int(raw)
        except (TypeError, ValueError):
            top = 0
    return max(0, min(top, max(0, nrows - visible)))


def listbox_active(node, rows=None):
    """Index of the row an expanded <select>'s keyboard cursor is on.

    Before any arrow has been pressed there is no explicit one, and the
    answer is the chosen option: that is where the reader is looking, so it
    is where the first arrow press should move from.
    """
    if rows is None:
        rows = select_rows(node)
    raw = node.attributes.get("data-active")
    if raw is not None:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            i = -1
        if 0 <= i < len(rows) and rows[i].enabled:
            return i
    chosen = {id(opt) for opt in selected_options(node)}
    for i, row in enumerate(rows):
        if row.option is not None and id(row.option) in chosen:
            return i
    return next((i for i, row in enumerate(rows) if row.enabled), -1)


class LayoutBox:
    """Base class carrying geometry."""

    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.x = self.y = self.width = self.height = 0
        # (left, top, right, bottom, node) rects for form controls, used for
        # hit-testing clicks on inputs and submit buttons.
        self.input_boxes = []
        # Floats placed by a containing block; entries are dicts with edge
        # coordinates and the side they occupy. Consulted by inline layout to
        # wrap text and by `clear` to push content below floats.
        self.float_regions = []
        # A float may never sit higher than one that came before it, whichever
        # side either is on (CSS 2.1 rule 5). Relative to this block's top.
        self._float_min_top = 0.0


class _LineItem:
    """A word or inline image pending on the current output line."""

    __slots__ = ("kind", "x", "text", "font", "color", "node", "w", "h", "photo",
                 "bg", "pl", "pr", "pt", "pb")

    def __init__(self, kind, x, text, font, color, node, w, h, photo=None,
                 bg=None, pl=0, pr=0, pt=0, pb=0):
        self.kind = kind  # "text", "img", "video", "pill", "block", "listbox"
        self.x = x
        self.text = text
        self.font = font
        self.color = color
        self.node = node
        self.w = w
        self.h = h
        self.photo = photo
        self.bg = bg
        self.pl = pl
        self.pr = pr
        self.pt = pt
        self.pb = pb

    @property
    def ascent(self):
        if self.kind in ("img", "video"):
            return int(self.h * 0.82)
        if self.kind in ("block", "listbox"):
            # An inline-block sits on the baseline rather than straddling it,
            # so the whole of it counts as height above the line. An expanded
            # <select> is the same shape of thing, and this is what makes the
            # line grow to hold it instead of the text below running through
            # it.
            return self.h
        return _metrics(self.font, "ascent")

    @property
    def descent(self):
        if self.kind in ("img", "video"):
            return int(self.h * 0.18)
        if self.kind in ("block", "listbox"):
            return 0.0
        return _metrics(self.font, "descent")

    # -- half-leading (CSS 2.1 sec. 10.8.1) ------------------------------
    #
    # A text run's box is `line-height` tall, and the difference between that
    # and the font's own ascent + descent is the leading, split evenly above
    # and below. What `extents` returns is how far the run reaches from the
    # baseline once that split is applied; the line box is the union over its
    # items. Half the leading can be negative -- `line-height: 1` on a face
    # whose ascent + descent exceeds its em, or anything smaller -- and the
    # lines then overlap, which is what the author asked for, not something
    # to clamp away.
    def extents(self):
        """How far this item reaches above and below the baseline.

        Both at once, and reading each font metric once: `flush` asks every
        item on every line, and a long article is a great many lines.
        """
        if self.kind == "text" or self.kind == "pill":
            font = self.font
            ascent = _metrics(font, "ascent")
            descent = _metrics(font, "descent")
            half = (_line_height(self.node, font)
                    - (ascent + descent)) / 2.0
            return ascent + half, descent + half
        # An atomic inline (image, video, inline-block, expanded <select>) is
        # not a text run: line-height does not apply to it, its own box is
        # what joins the line, so it reaches exactly ascent/descent.
        return self.ascent, self.descent


class BlockLayout(LayoutBox):
    def _parent_content_box(self):
        """Where the parent leaves room for its children: (left, width, top),
        the parent's box inset by the parent's own padding."""
        parent = self.parent
        pstyle = getattr(parent.node, "style", {}) or {}
        pt, pr, _pb, pl = _padding_box(pstyle)
        return (parent.x + pl,
                max(0.0, parent.width - pl - pr),
                pt)

    def layout(self):
        node = self.node
        style = getattr(node, "style", {}) or {}
        ml, ml_auto = _margin_side(style, "left")
        mr, mr_auto = _margin_side(style, "right")
        # A child fills its parent's *content* box, which is the parent's box
        # inset by the parent's padding. Ignoring that is why a padded card
        # used to have its contents sitting flush against its own border.
        parent_left, parent_width, parent_top = self._parent_content_box()
        base = max(0.0, parent_width - ml - mr)

        # CSS 2.1 §10.3.3 for block-level, non-replaced elements in normal
        # flow: resolve the used width (auto fills the parent), clamp it with
        # max-width, THEN re-resolve auto margins against the clamped width so
        # `margin: 0 auto` centers the final (not the tentative) width.
        css_w = style.get("width", "")
        if css_w.strip().lower() not in ("", "auto", "fit-content",
                                         "min-content", "max-content"):
            content_width = max(0.0, _resolve_len(css_w, parent_width, base))
        else:
            content_width = base
        mw = style.get("max-width", "")
        if mw.strip().lower() not in ("", "none"):
            content_width = min(
                content_width,
                max(0.0, _resolve_len(mw, parent_width, content_width)))
        mnw = style.get("min-width", "")
        if mnw.strip().lower() not in ("", "auto"):
            content_width = max(
                content_width,
                max(0.0, _resolve_len(mnw, parent_width, content_width)))

        remaining = parent_width - content_width - ml - mr
        if ml_auto and mr_auto:
            ml = mr = (remaining / 2) if remaining > 0 else 0
        elif ml_auto:
            ml = max(0.0, remaining + mr)
        elif mr_auto:
            mr = max(0.0, remaining + ml)
        elif remaining < 0:
            # Over-constrained in LTR: margin-right absorbs the shortfall.
            mr = max(0.0, mr + remaining)

        self.x = parent_left + ml
        self.width = content_width
        if self.previous:
            # margin-bottom is "0" for text nodes, so this is safe for them too.
            self.y = self.previous.y + self.previous.height \
                + parse_px(self.previous.node.style.get("margin-bottom", "0"))
        else:
            self.y = self.parent.y + parent_top
        self.y += parse_px(node.style.get("margin-top", "0"))
        # `clear` pushes a block (or a float/line box) below its side's floats.
        if getattr(self, "_y_floor", None) is not None:
            self.y = max(self.y, self._y_floor)

        if getattr(self, "_float_pos", None) is not None:
            # A float shrinks to fit its content and is pinned to a given
            # x/y; its children are laid out within that box.
            self.x, self.y, self.width = self._float_pos
        if getattr(self, "_absolute_pos", None) is not None:
            # An absolutely positioned box is out of flow and pinned to the
            # containing block; it never pushes siblings or grows the parent.
            self.x, self.y, self.width = self._absolute_pos
        _dispatch_layout(self)
        # CSS 2.1 §10.6: explicit `height` and `min-height` act as a floor on
        # the content height computed by the dispatcher. Growing past a stated
        # height rather than spilling is a deliberate cheat -- it keeps a box
        # whose contents we measured slightly too large from swallowing them.
        css_h = _resolve_len(style.get("height", ""), 0, 0)
        min_h = _resolve_len(style.get("min-height", ""), 0, 0)
        floor = max(css_h, min_h)
        if floor:
            self.height = max(self.height, floor)
        # ... but where the page says what to do with the spill, the stated
        # height is the real one. A box that clips is asking to be exactly
        # this tall, and honouring that is what makes `overflow: hidden` mean
        # anything at all.
        if css_h and _clips(style):
            self.height = css_h
        max_h = _resolve_len(style.get("max-height", ""), 0, 0)
        if max_h and _clips(style) and self.height > max_h:
            self.height = max_h

    def layout_mode(self):
        node = self.node
        if isinstance(node, Text):
            return "inline"
        for child in node.children:
            if isinstance(child, Text):
                # Text next to a block does not make the box inline: CSS
                # wraps it in an anonymous block instead, which is what
                # `<li>item<ul>...</ul></li>` needs to stack rather than run
                # the sub-list onto the item's own line. _layout_block gives
                # a bare text child its own box, so that fall-out is free.
                continue
            if child.style.get("display") == "none":
                continue
            if _is_inline_level(child):
                # An inline element can still hide block content: <a><div>
                # ...</div></a> is the card link on every listing page. CSS
                # splits the inline box around the block, so the box holding
                # it is a block box no matter what its own display says.
                # An inline-block is exempt -- it keeps its blocks to itself.
                if not _is_atomic_inline(child) and _wraps_block_content(child):
                    return "block"
                continue
            return "block"
        # No block children -> inline (even if empty).
        return "inline"

    def _layout_block(self):
        previous = None
        float_boxes = []
        # A list item whose content is blocks -- one holding a nested list,
        # or a paragraph -- gets its marker here; the inline path only ever
        # reached the items that were a single line of text.
        if isinstance(self.node, Element) and \
                self.node.style.get("display") == "list-item":
            self.display_list = []
            self._draw_bullet(
                self.y + _padding_box(self.node.style)[0])
        # Children are laid out as they are reached, not collected and laid
        # out afterwards, because a float needs to know how far down the flow
        # already reaches: one that follows two paragraphs starts below them,
        # not at the top of the block. Laying every float out first is how a
        # "Page 2" link at the foot of a listing ends up over the first story.
        content_h = 0
        for child in self.node.children:
            if isinstance(child, Element) and child.style.get("display") == "none":
                continue
            if isinstance(child, Text) and not child.text.strip():
                continue
            if isinstance(child, Element) and \
                    child.style.get("float") in ("left", "right"):
                fb = self._layout_float(child, content_h)
                float_boxes.append(fb)
                continue
            if isinstance(child, Element) and \
                    child.style.get("position") in ("absolute", "fixed"):
                # position:absolute/fixed boxes are out of flow: they don't
                # push siblings or stretch the parent (e.g. hidden dropdowns
                # and overlays that must not take up layout space).
                box = self._layout_absolute(child)
                self.children.append(box)
                box.layout()
                continue
            box = BlockLayout(child, self, previous)
            clear = child.style.get("clear") if isinstance(child, Element) else ""
            if clear:
                box._y_floor = self._cleared(clear, getattr(box, "_y_floor", 0.0))
            self.children.append(box)
            box.layout()
            previous = box
            content_h = (box.y + box.height
                         + parse_px(box.node.style.get("margin-bottom", "0"))
                         - self.y)
        for f in self.float_regions:
            content_h = max(content_h, f["bottom"] - self.y)
        self.children.extend(float_boxes)
        # Only the bottom padding is still missing: the first child was placed
        # below this box's padding-top, so the height measured down from
        # self.y has counted it once already.
        self.height = content_h + _padding_box(
            getattr(self.node, "style", {}) or {})[2]

    # -- floats ----------------------------------------------------------

    def _layout_absolute(self, el):
        """Lay out a `position:absolute/fixed` element out of the flow, pinned
        to this box's origin plus any top/left offsets. Real sites use these
        for dropdowns, modals and tooltips so they never stretch the parent;
        hidden ones are skipped by painting."""
        box = BlockLayout(el, self, None)
        left_css = el.style.get("left")
        right_css = el.style.get("right")
        y = self.y + parse_px(el.style.get("top", ""), 0)
        right = parse_px(right_css or "", 0)
        if left_css is None and right_css is not None:
            # Right-anchored (and no explicit left): shrink to the parent minus
            # the offset and pin the box's right edge to the containing block's
            # right edge. `right: 0` is a real offset, not "absent".
            w = max(0.0, self.width - right)
            x = self.x + self.width - right - w
        else:
            x = self.x + parse_px(left_css or "", 0)
            w = max(0.0, self.width - right) if right_css is not None \
                else self.width
        box._absolute_pos = (x, y, w)
        return box

    def _layout_float(self, el, flow_top=0.0):
        """Position a `float: left/right` box out of flow, shrink-to-fit its
        width, and record its region so inline content wraps around it.

        `flow_top` is how far down this block the normal flow has already
        reached: a float never rises above the content that precedes it.
        """
        side = el.style.get("float")
        ml = _resolve_len(el.style.get("margin-left", "0"), self.width)
        mr = _resolve_len(el.style.get("margin-right", "0"), self.width)
        mb = parse_px(el.style.get("margin-bottom", "0"))
        mt = parse_px(el.style.get("margin-top", "0"))
        avail = max(0.0, self.width - ml - mr)
        mi, ma = self._measure_width(el)
        w = max(1.0, min(avail, max(mi, ma)))
        css_w = el.style.get("width")
        if css_w and css_w.strip().lower() not in (
                "auto", "fit-content", "min-content", "max-content"):
            # A percentage is of the containing block, not of what fits --
            # `width: 16.6667%` is how a six-across nav bar was built before
            # anyone had flexbox.
            w = max(1.0, min(avail, _resolve_len(css_w, self.width, avail)))

        clear = el.style.get("clear")
        top = max(self._cleared(clear, 0.0), self._float_min_top, flow_top)
        # The slot is for the margin box; the border box starts ml inside it.
        if self.float_regions:
            # Which floats this one must sit beside or below depends on how
            # tall it is, and its height depends only on its width -- so
            # measure it first, in a box we then throw away.
            probe = BlockLayout(el, self, None)
            probe._float_pos = (self.x, self.y, w)
            probe.layout()
            probe_h = max(probe.height,
                          parse_px(el.style.get("height", ""), 0.0))
            x, y = self._float_slot(side, w + ml + mr, top, mt, probe_h + mb)
        else:
            y = self.y + mt + top
            x = self.x if side == "left" \
                else self.x + self.width - w - ml - mr
        x += ml
        self._float_min_top = y - self.y - mt

        box = BlockLayout(el, self, None)
        box._float_pos = (x, y, w)
        box.layout()
        h = box.height
        css_h = el.style.get("height")
        if css_h:
            h = max(h, parse_px(css_h, h))
            box.height = h
        self.float_regions.append({
            "side": side, "top": y, "bottom": y + h + mb,
            "left": x, "right": x + w, "box": box})
        return box

    def _float_slot(self, side, outer_w, top, mt, outer_h):
        """Where a float goes: as high as it is allowed, then as far to its
        own side as the floats already there leave room for.

        Floats run along a line and only drop to the next one when they stop
        fitting -- that is the whole reason a page built in 2012 has columns.
        """
        # Every float bottom is somewhere the next one might newly fit.
        candidates = [top] + sorted(
            f["bottom"] - self.y for f in self.float_regions
            if f["bottom"] - self.y > top)
        for i, candidate in enumerate(candidates):
            y = self.y + mt + candidate
            left, right = self.x, self.x + self.width
            for f in self.float_regions:
                if f["bottom"] <= y or f["top"] >= y + outer_h:
                    continue
                if f["side"] == "left":
                    left = max(left, f["right"])
                else:
                    right = min(right, f["left"])
            # Half a pixel of slack, because `width: 16.6667%` six times over
            # is 100.0002% and the sixth item is meant to stay on the line.
            # The last candidate is below everything, so it always wins --
            # a float wider than the block sticks out rather than vanishing.
            if right - left >= outer_w - 0.5 or i == len(candidates) - 1:
                return (left if side == "left" else right - outer_w), y
        return self.x, self.y + mt + top

    def _clear_bottom(self, side):
        bottom = 0.0
        for f in self.float_regions:
            if f["side"] == side:
                bottom = max(bottom, f["bottom"])
        return bottom

    def _cleared(self, clear, base):
        """Raise `base` to clear the floats `clear` targets (left/right/both)."""
        if clear:
            for side in ("left", "right"):
                if clear == "both" or clear == side:
                    base = max(base, self._clear_bottom(side))
        return base

    def _all_float_regions(self):
        """Float regions visible from this box: its own plus every ancestor's,
        all expressed in absolute page coordinates."""
        regions = []
        box = self
        while box is not None:
            regions.extend(box.float_regions)
            box = box.parent
        return regions

    def _line_bounds(self):
        """Horizontal span available to the current line, clipped by any
        floats whose vertical span covers the line's top."""
        _pt, pr, _pb, pl = _padding_box(getattr(self.node, "style", {}) or {})
        x0 = self.x + pl
        x1 = max(x0, self.x + self.width - pr)
        y = self.cursor_y
        for f in self._all_float_regions():
            if f["top"] <= y < f["bottom"]:
                if f["side"] == "left":
                    x0 = max(x0, f["right"])
                else:
                    x1 = min(x1, f["left"])
        return x0, max(x0, x1)

    # -- tables ----------------------------------------------------------

    def _layout_table(self):
        node = self.node
        self.children = []

        rows = []
        for child in node.children:
            if not isinstance(child, Element):
                continue
            if child.tag == "tr":
                rows.append(child)
            elif child.tag in ("thead", "tbody", "tfoot", "caption"):
                for g in child.children:
                    if isinstance(g, Element) and g.tag == "tr":
                        rows.append(g)
        if not rows:
            self.height = 0
            return

        # Put every cell on a (row, column) grid, skipping columns blocked by
        # an upward rowspan.
        grid, num_cols, occupied = [], 0, {}
        for tr in rows:
            row_cells, c = [], 0
            for child in tr.children:
                if not isinstance(child, Element) or child.tag not in ("td", "th"):
                    continue
                while occupied.get(c, 0) > 0:
                    c += 1
                try:
                    cs = max(1, int(child.attributes.get("colspan", "1") or 1))
                    rs = max(1, int(child.attributes.get("rowspan", "1") or 1))
                except ValueError:
                    cs = rs = 1
                row_cells.append((c, cs, rs, child))
                if rs > 1:
                    occupied[c] = max(occupied.get(c, 0), rs - 1)
                num_cols = max(num_cols, c + cs)
                c += cs
            grid.append(row_cells)

        # Column min/max content widths (spanning cells share their width
        # across the columns they cover so a single full-width cell still
        # gives the table usable columns).
        col_min = [0.0] * num_cols
        col_max = [0.0] * num_cols
        for row_cells in grid:
            for c, cs, rs, el in row_cells:
                mi, ma = self._measure_width(el)
                # A column has to be wide enough for the text *and* the
                # cell's padding, which the cell lays out inside itself.
                _pt, pr, _pb, pl = _padding_box(el.style)
                mi += pl + pr
                ma += pl + pr
                share = max(1, cs)
                for k in range(c, c + cs):
                    col_min[k] = max(col_min[k], mi / share)
                    col_max[k] = max(col_max[k], ma / share)

        # Compute final column widths up front so cell content is measured at
        # its real width instead of wrapping onto a line per word. Auto tables
        # shrink to fit their content (width: fit-content); an explicit width
        # (px or %) stretches the table to that width when content is short.
        avail = self.width
        explicit = None
        css_w = node.style.get("width")
        if css_w:
            cw = css_w.strip()
            if cw.endswith("%"):
                try:
                    explicit = avail * min(100.0, max(0.0, float(cw[:-1]))) / 100.0
                except ValueError:
                    pass
            elif cw.lower() in ("auto", "fit-content", "min-content",
                                "max-content"):
                # Intrinsic keywords mean shrink-to-fit, not an explicit width.
                explicit = None
            else:
                explicit = parse_px(cw, avail)
        self._widths = self._distribute_column_widths(avail, col_min, col_max)
        if explicit is not None:
            used = sum(self._widths)
            if explicit > used:
                grow = [max(0.0, m - n0) for m, n0 in zip(self._widths, col_min)]
                gsum = sum(grow) or 1.0
                extra = explicit - used
                self._widths = [mi + extra * (g / gsum)
                                for mi, g in zip(self._widths, grow)]
        # Auto tables shrink to their used column widths; the table box must
        # match the cells so borders/backgrounds don't extend past the content.
        if sum(self._widths) > 0:
            self.width = min(self.width, sum(self._widths))

        cells = []  # (ri, col, cs, rs, el, content_block, content_h, col_w)
        for ri, row_cells in enumerate(grid):
            for c, cs, rs, el in row_cells:
                col_w = sum(self._widths[c:c + cs])
                # The cell box is the whole column: it insets its own
                # content by its own padding, like any other box.
                content_w = max(1, col_w)
                cb = BlockLayout(el, self, None)
                cb.x = 0
                cb.y = 0
                cb.width = content_w
                if cb.layout_mode() == "block":
                    cb._layout_block()
                else:
                    cb._layout_inline()
                cells.append((ri, c, cs, rs, el, cb, cb.height, col_w))

        # Row heights from non-spanning cells, then let rowspans stretch rows.
        row_h = [0.0] * len(grid)
        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            if rs == 1:
                row_h[ri] = max(row_h[ri], content_h)
        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            if rs <= 1:
                continue
            span_sum = sum(row_h[ri:ri + rs])
            overflow = content_h - span_sum
            if overflow > 0:
                row_h[ri] += overflow

        # Build row / cell boxes.
        y_cursor = self.y
        row_boxes = []
        for ri in range(len(grid)):
            row = RowLayout(rows[ri], self, row_boxes[-1] if row_boxes else None)
            row.x = self.x
            row.y = y_cursor
            row.width = self.width
            row.height = row_h[ri]
            self.children.append(row)
            row_boxes.append(row)
            y_cursor += row_h[ri]

        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            row = row_boxes[ri]
            cell = CellLayout(el, row, None)
            cell.x = self.x + sum(self._widths[:c])
            cell.y = row.y
            cell.width = sum(self._widths[c:c + cs])
            cell.height = sum(row_h[ri:ri + rs]) if rs > 1 else row_h[ri]
            cell.content = self._render_cell(cb, cell, content_h)
            row.children.append(cell)

        self.display_list = [
            DrawOutline(self.x, self.y, self.x + self.width, y_cursor, "#bbbbbb", 1)]
        self.height = y_cursor - self.y

    def _measure_width(self, el):
        """Approximate a cell's min (longest word) and preferred single-line
        content widths so the auto table layout can size its columns."""
        font = _node_font(el)
        cache = self._image_cache()
        total, longest = 0.0, 0.0
        stack = [el]
        while stack:
            n = stack.pop()
            if isinstance(n, Text):
                for word in n.text.split():
                    w = _measure(font, word)
                    total += w + _measure(font, " ")
                    longest = max(longest, w)
            elif isinstance(n, Element):
                if n is not el and n.style.get("display") == "none":
                    # What is not drawn takes up no room. A closed dropdown
                    # holding a whole menu would otherwise demand width for
                    # every item in it.
                    continue
                if n.tag == "img":
                    # Size against the real pixels when the image has been
                    # decoded, not the "[img]" placeholder, or the column is
                    # drawn far too narrow and the image overlaps its
                    # neighbours. Matches _inline_img's advance (w * 1.25).
                    photo = None
                    src = n.attributes.get("src")
                    if src and cache:
                        photo = cache.get(src)
                    if photo is not None:
                        v = float(photo.width()) * 1.25
                    else:
                        v = _measure(font, "[img]") + 8
                    total += v
                    longest = max(longest, v)
                elif n.tag in ("input", "textarea", "button", "select"):
                    v = 110.0
                    total += v
                    longest = max(longest, v)
                for ch in reversed(n.children):
                    stack.append(ch)
        return max(1.0, longest), max(1.0, total)

    @staticmethod
    def _distribute_column_widths(avail, col_min, col_max):
        """Auto table layout: fit columns into a given width, honouring each
        column's content-based min and preferred widths."""
        n = len(col_min)
        if n == 0:
            return []
        total_min = sum(col_min)
        total_max = sum(col_max)
        if avail <= total_min or total_max <= total_min:
            return list(col_min)
        if avail >= total_max:
            return list(col_max)
        grow = [max(0.0, m - n0) for m, n0 in zip(col_max, col_min)]
        gsum = sum(grow) or 1.0
        extra = avail - total_min
        return [mi + extra * (g / gsum) for mi, g in zip(col_min, grow)]

    def _render_cell(self, cb, cell_box, content_h):
        """Flatten a cell's laid-out subtree into absolute paint coordinates,
        applying the cell's vertical alignment. The cell's padding is already
        inside `content_h`; the cell box laid its own content out."""
        cell_node = cb.node
        valign = cell_node.style.get("vertical-align",
                                     "middle" if cell_node.tag in ("td", "th")
                                     else "top") if isinstance(cell_node, Element) \
            else "top"
        cap = max(0, cell_box.height - content_h)
        dy = 0.0
        if valign == "middle":
            dy += cap / 2
        elif valign == "bottom":
            dy += cap
        out = []
        self._flatten_paint(cb, out, cell_box.x, cell_box.y + dy)
        return out

    def _shift_cmd(self, cmd, dx, dy):
        if hasattr(cmd, "left"):
            cmd.left += dx
        if hasattr(cmd, "top"):
            cmd.top += dy
        if hasattr(cmd, "right"):
            cmd.right += dx
        if hasattr(cmd, "bottom"):
            cmd.bottom += dy

    def _flatten_paint(self, box, out, dx, dy):
        for cmd in box.paint():
            self._shift_cmd(cmd, dx, dy)
            out.append(cmd)
        for child in box.children:
            self._flatten_paint(child, out, dx, dy)
        for lx, ty, rx, by, node in getattr(box, "input_boxes", ()):
            self.input_boxes.append((lx + dx, ty + dy, rx + dx, by + dy, node))

    def _translate(self, box, dx, dy):
        """Shift `box` subtree geometry, paint commands, and input hit-boxes
        by (dx, dy). Used when a laid-out subtree (flex/grid item) is
        repositioned after measuring."""
        box.x += dx
        box.y += dy
        for cmd in getattr(box, "display_list", ()):
            self._shift_cmd(cmd, dx, dy)
        # Table cells keep their flattened paint commands in `content`
        # rather than display_list, so those must be translated too, or the
        # cell's text/images stay pinned at the pre-move position and overlap
        # the surrounding content.
        for cmd in getattr(box, "content", ()):
            self._shift_cmd(cmd, dx, dy)
        for child in box.children:
            self._translate(child, dx, dy)
        if getattr(box, "input_boxes", None):
            box.input_boxes = [(lx + dx, ty + dy, rx + dx, by + dy, n)
                               for lx, ty, rx, by, n in box.input_boxes]

    def _gaps(self, node):
        """Resolve gap/row-gap/column-gap shorthand into explicit gaps."""
        gap = parse_px(node.style.get("gap", ""))
        row_gap = parse_px(node.style.get("row-gap", ""))
        column_gap = parse_px(node.style.get("column-gap", ""))
        if gap and not row_gap:
            row_gap = gap
        if gap and not column_gap:
            column_gap = gap
        return row_gap, column_gap

    def _flex_items(self):
        """Child elements (and non-empty text) of a flex/grid container."""
        items = []
        for child in self.node.children:
            if isinstance(child, Text):
                if child.text.strip():
                    items.append(child)
                continue
            if child.style.get("display") == "none":
                continue
            items.append(child)
        return items

    def _layout_item(self, el, w):
        """Lay a flex/grid item in a scratch box at x=0/y=0 of width `w`,
        applying any explicit CSS height; returns (box, box.height)."""
        box = BlockLayout(el, self, None)
        box.x = 0
        box.y = 0
        box.width = w
        _dispatch_layout(box)
        css_h = parse_px(el.style.get("height", "")) if isinstance(el, Element) else 0.0
        box.height = max(box.height, css_h)
        return box, box.height

    def _layout_flex(self):
        """Subset flexbox: `flex-direction: row/column`, `gap`, flex item
        `flex-grow` (and `flex-basis` in px), `justify-content`, `align-items`,
        and `flex-wrap: wrap/wrap-reverse` (rows, plus columns when the
        container has an explicit height)."""
        node = self.node
        direction = (node.style.get("flex-direction", "row")
                     if isinstance(node, Element) else "row")
        if direction not in ("row", "column"):
            direction = "row"
        wrap = (node.style.get("flex-wrap", "nowrap")
                if isinstance(node, Element) else "nowrap")
        if wrap not in ("wrap", "wrap-reverse", "nowrap"):
            wrap = "nowrap"
        flex_flow = (node.style.get("flex-flow", "")
                     if isinstance(node, Element) else "")
        if flex_flow:
            for tok in flex_flow.split():
                if tok in ("row", "column"):
                    direction = tok
                elif tok in ("wrap", "wrap-reverse", "nowrap"):
                    wrap = tok
        row_gap, column_gap = self._gaps(node)
        justify = (node.style.get("justify-content", "flex-start")
                   if isinstance(node, Element) else "flex-start")
        align = (node.style.get("align-items", "stretch")
                 if isinstance(node, Element) else "stretch")

        items = self._flex_items()
        if not items:
            self.height = parse_px(node.style.get("height", ""), 0)
            return

        def margins(el):
            if not isinstance(el, Element):
                return 0.0, 0.0, 0.0, 0.0
            return (parse_px(el.style.get("margin-left", "0")),
                    parse_px(el.style.get("margin-right", "0")),
                    parse_px(el.style.get("margin-top", "0")),
                    parse_px(el.style.get("margin-bottom", "0")))

        def grows(el):
            if isinstance(el, Element):
                v = el.style.get("flex-grow")
                if v is None:
                    fl = el.style.get("flex")
                    if fl:
                        v = fl.split()[0]
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    pass
            return 0.0

        def basis(el):
            if not isinstance(el, Element):
                return None
            b = el.style.get("flex-basis")
            if b and b.endswith("px"):
                return parse_px(b)
            fl = el.style.get("flex")
            if fl:
                for tok in fl.split():
                    if tok.endswith("px"):
                        return parse_px(tok)
            css_w = el.style.get("width")
            if css_w and css_w.endswith("px"):
                return parse_px(css_w)
            return None

        def natural_width(el):
            mi, ma = self._measure_width(el)
            b = basis(el)
            if b is not None:
                return b
            return max(mi, min(ma, self.width))

        def distra_leftover(extra, widths, grow_items=None):
            """Grow flex items proportionally to their `flex-grow`."""
            if grow_items is None:
                grow_items = items
            gs = [grows(el) for el in grow_items]
            gsum = sum(gs)
            if gsum > 0 and extra > 0:
                out = list(widths)
                for i, g in enumerate(gs):
                    out[i] += extra * (g / gsum)
                return out, 0.0
            return widths, extra

        def justify_start(start, leftover, end_alias, count=None):
            """First-item cursor for justify-content plus the per-gap amount
            to add between space-separated items."""
            n = len(items) if count is None else count
            cursor = start
            gap = 0.0
            if justify in ("center", "middle"):
                cursor = start + leftover / 2
            elif justify in ("flex-end", "end", end_alias):
                cursor = start + leftover
            elif justify == "space-between" and n > 1:
                gap = leftover / (n - 1)
            elif justify in ("space-around", "space-evenly"):
                parts = leftover / (2 * n) if justify == "space-around" \
                    else leftover / (n + 1)
                cursor = start + parts
                gap = parts * 2 if justify == "space-around" else parts
            return cursor, gap

        if direction == "row":
            if wrap in ("wrap", "wrap-reverse"):
                nw = [natural_width(el) for el in items]

                # Pack items into lines, honoring margins and column-gap.
                lines = []
                line = []
                used = 0.0
                for i, el in enumerate(items):
                    ml, mr, _, _ = margins(el)
                    item_w = ml + nw[i] + mr
                    if line and used + column_gap + item_w > self.width:
                        lines.append(line)
                        line = []
                        used = 0.0
                    line.append(i)
                    used += (column_gap if used else 0.0) + item_w
                if line:
                    lines.append(line)

                # Lay each line out like the nowrap row: leftover space
                # distributed by flex-grow, justify-content per line.
                line_results = []
                for indices in lines:
                    line_items = [items[i] for i in indices]
                    widths = [nw[i] for i in indices]
                    margin_w = sum(ml + mr for ml, mr, _, _ in
                                   (margins(el) for el in line_items))
                    gap_total = column_gap * (len(indices) - 1)
                    total = sum(widths) + margin_w + gap_total
                    if total > self.width:
                        leftover = 0.0
                        available = max(0.0, self.width - gap_total - margin_w)
                        if sum(widths) > 0:
                            factor = available / sum(widths)
                            widths = [w * factor for w in widths]
                        else:
                            widths = [0.0] * len(widths)
                    else:
                        widths, leftover = distra_leftover(
                            self.width - total, widths, line_items)
                    cursor, extra = justify_start(self.x, leftover, "right",
                                                  len(indices))
                    placement = []
                    for j, idx in enumerate(indices):
                        el = items[idx]
                        ml, mr, mt, mb = margins(el)
                        w = widths[j]
                        if j > 0 and justify in \
                                ("space-between", "space-around", "space-evenly"):
                            cursor += extra
                        x = cursor + ml
                        box, ch = self._layout_item(el, w)
                        placement.append((box, ch, mt, mb, x))
                        cursor += ml + w + mr + column_gap
                    cross = max(ch + mt + mb for _, ch, mt, mb, _ in placement) or 0.0
                    line_results.append((placement, cross))

                css_h = parse_px(node.style.get("height", ""), 0)
                content_h = sum(cross for _, cross in line_results) \
                    + row_gap * (len(lines) - 1)
                self.height = css_h if css_h else content_h

                # align-content distributes leftover vertical space.
                align_content = (node.style.get("align-content", "flex-start")
                                 if isinstance(node, Element) else "flex-start")
                free = max(0.0, self.height - content_h)
                n = len(lines)
                if align_content == "stretch" and free > 0 and n > 0:
                    grow = free / n
                    line_results = [(pl, cross + grow) for pl, cross in line_results]
                    free = 0.0
                if align_content == "center":
                    top, gap = self.y + free / 2, row_gap
                elif align_content in ("flex-end", "end", "bottom"):
                    top, gap = self.y + free, row_gap
                elif align_content == "space-between" and n > 1:
                    top, gap = self.y, row_gap + free / (n - 1)
                elif align_content in ("space-around", "space-evenly"):
                    parts = free / n if align_content == "space-around" \
                        else free / (n + 1)
                    top = self.y + parts
                    gap = row_gap + (parts * 2 if align_content == "space-around"
                                     else parts)
                else:
                    top, gap = self.y, row_gap

                line_tops = []
                y = top
                for _, cross in line_results:
                    line_tops.append(y)
                    y += cross + gap
                if wrap == "wrap-reverse":
                    line_tops = [self.y + self.height - (t - self.y) - cross
                                 for t, (_, cross) in zip(line_tops, line_results)]

                for (placement, cross), line_top in zip(line_results, line_tops):
                    for box, ch, mt, mb, x in placement:
                        if align == "stretch":
                            box.height = cross - mt - mb
                            y = line_top + mt
                        elif align in ("flex-end", "end"):
                            box.height = ch
                            y = line_top + cross - mb - ch
                        elif align in ("center", "middle"):
                            box.height = ch
                            y = line_top + mt + (cross - mt - ch - mb) / 2
                        else:
                            box.height = ch
                            y = line_top + mt
                        self._translate(box, x, y)
                        self.children.append(box)
            else:
                widths = [natural_width(el) for el in items]
                margin_w = sum(ml + mr for ml, mr, _, _ in (margins(el) for el in items))
                gap_total = column_gap * (len(items) - 1)
                avail = self.width
                total = sum(widths) + margin_w + gap_total
                if total > avail:
                    leftover = 0.0
                    available = max(0.0, avail - gap_total - margin_w)
                    if sum(widths) > 0:
                        factor = available / sum(widths)
                        widths = [w * factor for w in widths]
                    else:
                        widths = [0.0] * len(widths)
                else:
                    widths, leftover = distra_leftover(avail - total, widths)
                total = sum(widths) + margin_w + gap_total + leftover

                # justify-content places the leftover space.
                cursor, extra = justify_start(self.x, leftover, "right")

                placement = []
                for i, el in enumerate(items):
                    ml, mr, mt, mb = margins(el)
                    w = widths[i]
                    if i > 0 and justify in \
                            ("space-between", "space-around", "space-evenly"):
                        cursor += extra
                    x = cursor + ml
                    box, ch = self._layout_item(el, w)
                    placement.append((box, ch, mt, mb, x))
                    cursor += ml + w + mr + column_gap

                max_h = max(ch + mt + mb for _, ch, mt, mb, _ in placement) or 0.0
                stretch_h = parse_px(node.style.get("height", ""), 0)
                if stretch_h:
                    self.height = stretch_h
                    max_h = max(max_h, stretch_h)
                else:
                    self.height = max_h
                for box, ch, mt, mb, x in placement:
                    if align == "stretch":
                        box.height = max_h - mt - mb
                        y = self.y + mt
                    elif align in ("flex-end", "end"):
                        box.height = ch
                        y = self.y + self.height - mb - ch
                    elif align in ("center", "middle"):
                        box.height = ch
                        y = self.y + mt + (self.height - mt - ch - mb) / 2
                    else:
                        box.height = ch
                        y = self.y + mt
                    self._translate(box, x, y)
                    self.children.append(box)

        else:  # column
            css_h = parse_px(node.style.get("height", ""), 0)
            if wrap in ("wrap", "wrap-reverse") and css_h:
                # Basic column wrapping: pack items into columns that fit the
                # explicit height, then place the columns side by side.
                nw = [natural_width(el) for el in items]
                boxes = []
                heights = []
                for el, w in zip(items, nw):
                    box, ch = self._layout_item(el, w)
                    boxes.append(box)
                    heights.append(ch)

                cols = []
                col = []
                used = 0.0
                for i, el in enumerate(items):
                    _, _, mt, mb = margins(el)
                    item_h = heights[i] + mt + mb
                    if col and used + row_gap + item_h > css_h:
                        cols.append(col)
                        col = []
                        used = 0.0
                    col.append(i)
                    used += (row_gap if used else 0.0) + item_h
                if col:
                    cols.append(col)
                if wrap == "wrap-reverse":
                    cols.reverse()

                cursor_x = self.x
                for indices in cols:
                    col_w = 0.0
                    for i in indices:
                        ml, mr, _, _ = margins(items[i])
                        col_w = max(col_w, nw[i] + ml + mr)
                    col_w = min(col_w, self.width)
                    y = self.y
                    for i in indices:
                        ml, _, mt, mb = margins(items[i])
                        self._translate(boxes[i], cursor_x + ml, y + mt)
                        self.children.append(boxes[i])
                        y += heights[i] + mt + mb + row_gap
                    cursor_x += col_w + column_gap
                self.height = css_h
            else:
                avails = []
                for el in items:
                    b = basis(el)
                    if b is not None:
                        avails.append(min(self.width, b))
                    else:
                        mi, ma = self._measure_width(el)
                        if align == "stretch":
                            avails.append(self.width)
                        else:
                            avails.append(max(mi, min(ma, self.width)))

                placement = []
                for el, w in zip(items, avails):
                    ml, mr, mt, mb = margins(el)
                    box, ch = self._layout_item(el, w)
                    placement.append((el, box, ch, ml, mr, mt, mb))

                gap_total = row_gap * (len(items) - 1)
                margin_h = sum(mt + mb for _, _, _, _, _, mt, mb in placement)
                content_h = sum(ch for _, _, ch, _, _, _, _ in placement)
                total = content_h + gap_total + margin_h
                extra = max(0.0, css_h - total) if css_h else 0.0

                heights = [ch for _, _, ch, _, _, _, _ in placement]
                if extra > 0:
                    heights, extra = distra_leftover(extra, heights)

                cursor, extra_gap = justify_start(self.y, extra, "bottom")

                for i, (el, box, ch, ml, mr, mt, mb) in enumerate(placement):
                    h = heights[i]
                    if align == "stretch" and not (
                            isinstance(el, Element) and el.style.get("width")):
                        x = self.x
                    elif align in ("flex-end", "end", "right"):
                        x = self.x + (self.width - ml - mr - box.width)
                    elif align in ("center", "middle"):
                        x = self.x + ml + ((self.width - ml - mr - box.width) / 2)
                    else:
                        x = self.x + ml
                    if i > 0 and justify in \
                            ("space-between", "space-around", "space-evenly"):
                        cursor += extra_gap
                    y = cursor + mt
                    self._translate(box, x, y)
                    cursor += mt + h + mb + row_gap
                    self.children.append(box)

                self.height = max(css_h, cursor - self.y) if css_h else cursor - self.y

        self.height += _block_padding(node)

    def _parse_grid_areas(self, value):
        """Parse `grid-template-areas` (whitespace-separated quoted strings,
        one row per string, '.' is an empty cell) into a map of area name ->
        (row, rowspan, col, colspan)."""
        if not value:
            return {}
        rows = []
        for open_q, close_q in re.findall(r"'([^']*)'|\"([^\"]*)\"", value):
            rows.append((open_q if open_q else close_q).split())
        areas = {}
        for r, row in enumerate(rows):
            for c, name in enumerate(row):
                if name == "." or name in areas:
                    continue
                cspan = 1
                while c + cspan < len(row) and row[c + cspan] == name:
                    cspan += 1
                rspan = 1
                while r + rspan < len(rows):
                    nxt = rows[r + rspan]
                    if c + cspan <= len(nxt) and \
                            all(x == name for x in nxt[c:c + cspan]):
                        rspan += 1
                    else:
                        break
                areas[name] = (r, rspan, c, cspan)
        return areas

    def _layout_grid(self):
        """Subset CSS grid: `grid-template-columns` (px/%/fr/auto), row
        auto-placement with `grid-column`/`grid-row` (start, span, or
        start/end), `gap`, and auto row heights from content."""
        node = self.node
        self.children = []

        def parse_tracks(value):
            if not value:
                return []
            out = []
            for tok in value.split():
                tok = tok.strip()
                if not tok:
                    continue
                # minmax(<min>, <max>): a track between two bounds. Use the
                # definite bound (max first, then min), which is right for
                # the common `minmax(0, 1fr)` and `minmax(15.5rem, auto)`.
                if tok.startswith("minmax(") and tok.endswith(")"):
                    parts = [p.strip() for p in tok[len("minmax("):-1].split(",")]
                    chosen = None
                    for p in reversed(parts):
                        if p.lower() not in ("auto", "min-content", "max-content",
                                             "fit-content"):
                            chosen = p
                            break
                    if chosen is None:
                        out.append(("auto", 0.0))
                        continue
                    tok = chosen
                for kind, suffix, cut in (("fr", "fr", -2), ("pct", "%", -1),
                                          ("px", "px", -2), ("rem", "rem", -3)):
                    if tok.endswith(suffix):
                        try:
                            v = float(tok[:cut])
                            if kind == "rem":
                                kind, v = "px", v * 16.0
                            out.append((kind, v))
                        except ValueError:
                            out.append(("auto", 0.0))
                        break
                else:
                    out.append(("auto", 0.0))
            return out

        col_def = parse_tracks(node.style.get("grid-template-columns", ""))
        row_def = parse_tracks(node.style.get("grid-template-rows", ""))
        # The `grid-template` shorthand (`<rows> / <columns>`) is common on
        # real sites (Wikipedia's header) and was silently dropped, leaving
        # the grid at a single auto column that collapses wide content.
        template = node.style.get("grid-template", "")
        if "/" in template:
            t_rows, t_cols = template.split("/", 1)
            if not row_def:
                row_def = parse_tracks(t_rows)
            if not col_def:
                col_def = parse_tracks(t_cols)
        row_gap, col_gap = self._gaps(node)

        # `grid-template-areas` maps named cells to a row/column span:
        # "'siteNotice siteNotice' 'columnStart pageContent' 'footer footer'"
        # -> siteNotice spans both columns of row 0, etc. Items reference a
        # cell by name via `grid-area`, and are placed there (row-major when
        # the name is duplicated, as real browsers do).
        areas = self._parse_grid_areas(node.style.get("grid-template-areas", ""))
        # The number of columns/rows implied by the areas template must be
        # created even if the item tracks weren't declared.
        if areas:
            rows = max(r + rspan for r, rspan, _, _ in areas.values())
            cols = max(c + cspan for _, _, c, cspan in areas.values())
            if not col_def and cols:
                col_def = [("auto", 0.0)] * cols
            if not row_def and rows:
                row_def = [("auto", 0.0)] * rows

        items = self._flex_items()
        if not items:
            self.height = parse_px(node.style.get("height", ""), 0)
            return
        if not col_def:
            col_def = [("auto", 0.0)]

        def parse_num(tok):
            try:
                return int(tok)
            except (TypeError, ValueError):
                return None

        def placement_of(el, areas):
            """Return (col_start, col_span, row_start, row_span), 0-based
            starts; None means auto. Understands 'start/end', 'start/span N',
            'span N', or bare numbers, plus a `grid-area` name."""
            def sides(prop):
                v = el.style.get(prop)
                start = span = None
                if v:
                    parts = [p.strip() for p in v.split("/")]
                    if len(parts) == 2:
                        a, b = parts
                        if a.startswith("span"):
                            span = parse_num(a.split()[1]) or 1
                            end = parse_num(b)
                            if end is not None:
                                start = end - span
                        elif b.startswith("span"):
                            start = parse_num(a)
                            span = parse_num(b.split()[1]) or 1
                        else:
                            start = parse_num(a)
                            end = parse_num(b)
                            if start is not None and end is not None:
                                span = end - start
                    else:
                        v = parts[0]
                        if v.startswith("span"):
                            span = parse_num(v.split()[1]) or 1
                        else:
                            start = parse_num(v)
                return start, span or 1
            cs, cspan = sides("grid-column")
            rs, rspan = sides("grid-row")
            area = el.style.get("grid-area")
            if area and "/" not in area and area in areas:
                ars, arspan, acs, acspan = areas[area]
                if cs is None:
                    cs, cspan = acs, acspan
                if rs is None:
                    rs, rspan = ars, arspan
            return cs, cspan, rs, rspan

        # Auto-place into rows of `col_def` columns, extending tracks when an
        # explicit column goes past the template. Row-major cursor so items
        # fill left-to-right, top-to-bottom by default.
        placements = []  # (row, col, cspan, rspan, el)
        occupied = {}
        cur_r, cur_c = 0, 0
        ncols_so_far = len(col_def)
        for el in items:
            cs, cspan, rs, rspan = placement_of(el, areas)
            ncols_so_far = max(ncols_so_far, cspan)
            if rs is not None:
                row = max(0, rs)
            else:
                row = cur_r
            if cs is not None:
                col = max(0, cs)
            else:
                col = None
            # Wrap the sparse cursor to the next row once it passes the last
            # column known so far (matches row auto-placement).
            while row == cur_r and cur_c + cspan > ncols_so_far:
                cur_r += 1
                cur_c = 0

            if col is None:
                # Scan row-major from the current cursor for a free slot.
                r = max(row, cur_r)
                start_c = cur_c if r == cur_r else 0
                while True:
                    c = start_c if r == row else 0
                    while c < 4096:
                        if all((r + rr, c + cc) not in occupied
                               for rr in range(rspan) for cc in range(cspan)):
                            col = c
                            break
                        c += 1
                    if col is not None:
                        break
                    r += 1
                    if r > 4096:
                        raise RuntimeError("grid auto-placement runaway")
                row = r
            ncols_so_far = max(ncols_so_far, col + cspan)
            for rr in range(rspan):
                for cc in range(cspan):
                    occupied[(row + rr, col + cc)] = True
            placements.append((row, col, cspan, rspan, el))
            cur_r, cur_c = row, col + cspan

        # Determine the number of columns actually used (template may be
        # widened by explicit placement).
        ncols = len(col_def)
        for row, col, cspan, rspan, el in placements:
            ncols = max(ncols, col + cspan)
        col_def += [("auto", 0.0)] * (ncols - len(col_def))
        nrows = max((row + rspan for row, _, _, rspan, _ in placements), default=0)

        # Column widths. Auto tracks size to the widest min-content item.
        col_min = [0.0] * ncols
        for row, col, cspan, rspan, el in placements:
            if cspan == 1:
                mi, _ = self._measure_width(el)
                col_min[col] = max(col_min[col], mi)
        col_w = [0.0] * ncols
        avail = self.width
        fr_sum = sum(v for k, v in col_def if k == "fr")
        used = 0.0
        for i, (k, v) in enumerate(col_def):
            if k == "px":
                col_w[i] = v
            elif k == "pct":
                col_w[i] = avail * v / 100.0
            elif k == "auto":
                col_w[i] = col_min[i]
            used += col_w[i]
        remaining = max(0.0, avail - used - col_gap * (ncols - 1))
        for i, (k, v) in enumerate(col_def):
            if k == "fr" and fr_sum:
                col_w[i] = remaining * v / fr_sum
        # Recompute fr widths from what's left after fixed columns filled the
        # container (rare over-constraint -> shrink fr tracks proportionally).
        total = sum(col_w) + col_gap * (ncols - 1)
        if total > avail:
            scale = max(0.0, (avail - col_gap * (ncols - 1)) / (sum(col_w) or 1))
            col_w = [w * scale for w in col_w]

        # Lay out each item in a scratch box to learn its content height.
        placed = []
        for row, col, cspan, rspan, el in placements:
            w = sum(col_w[col:col + cspan]) + col_gap * (cspan - 1)
            box, _ = self._layout_item(el, w)
            placed.append((row, col, cspan, rspan, el, box, w))

        # Row heights: auto rows grow to their tallest item; explicit rows
        # keep their track size (items overflow rather than stretch).
        row_h = [0.0] * nrows
        for row, col, cspan, rspan, el, box, w in placed:
            if rspan == 1:
                row_h[row] = max(row_h[row], box.height)
        for i, (k, v) in enumerate(row_def):
            if i < nrows and k == "px":
                row_h[i] = v
        # Rowspans: let a spanning item push its top row down.
        for row, col, cspan, rspan, el, box, w in placed:
            if rspan <= 1:
                continue
            span_h = sum(row_h[row:row + rspan]) + row_gap * (rspan - 1)
            if box.height > span_h:
                row_h[row] += box.height - span_h

        # Position via translate so item content moves with its box.
        x_cursor = self.x
        for i in range(ncols):
            if i:
                x_cursor += col_gap
            w = col_w[i]
            for row, col, cspan, rspan, el, box, _w in placed:
                if col == i:
                    y_cursor = self.y
                    for r in range(row):
                        y_cursor += row_h[r] + row_gap
                    self._translate(box, x_cursor, y_cursor)
                    self.children.append(box)
            x_cursor += w

        self.height = sum(row_h) + row_gap * (nrows - 1) + _block_padding(node)

    def _layout_inline(self):
        self.display_list = []
        clear = self.node.style.get("clear") if isinstance(self.node, Element) else ""
        pt, _pr, pb, _pl = _padding_box(getattr(self.node, "style", {}) or {})
        # Text starts below the box's own padding; _line_bounds keeps it
        # inside the left and right padding as each line is placed.
        self.cursor_y = self._cleared(clear, self.y) + pt
        self.cursor_x = self._line_bounds()[0]
        self.line = []  # pending words on the current line
        self._underline_run = None  # (declaring element, DrawLine) being extended
        # Tallest form control painted on the current line. Controls paint
        # straight into the display list instead of queuing a _LineItem, so
        # flush() has to be told how far down they reach.
        self.line_control_h = 0.0

        # List item bullet.
        if isinstance(self.node, Element) and \
                self.node.style.get("display") == "list-item":
            self._draw_bullet(self.cursor_y)

        self.recurse(self.node)
        self.flush()
        self.height = self.cursor_y - self.y + pb

    # -- inline layout ---------------------------------------------------

    def recurse(self, node):
        if isinstance(node, Text):
            return self.text(node)
        if node.style.get("display") == "none":
            return
        if node.tag == "br":
            return self.flush(force=True)
        if node.tag == "img":
            return self._inline_img(node)
        if node.tag == "video":
            return self._inline_video(node)
        if node.tag in ("input", "textarea", "button"):
            return self._inline_button(node) if node.tag == "button" \
                else self._inline_input(node)
        if node.tag == "hr":
            self.flush()
            return self._draw_hr(node)
        # <select> is rendered as a read-only field for now.
        if node.tag == "select":
            return self._inline_select(node)
        # display:inline-block with a background (e.g. Google's "Sign in"
        # pill) paints as one box instead of split words. Padding alone does
        # not trigger this: inline-blocks without a background keep their
        # normal flow so their inline children stay together.
        style = node.style
        if style.get("display") == "inline-block" and isinstance(node, Element):
            bg = resolve_color(style.get("background-color")) or \
                resolve_color(style.get("background"))
            if bg:
                pt, pr, pb, pl = _padding_box(style)
                self._inline_pill(node, bg, pl, pr, pt, pb)
                return
            if _wraps_block_content(node):
                # Blocks inside it, so its insides cannot simply be poured
                # into this line: it gets a box of its own and joins the line
                # as one atom, the way an image does.
                return self._inline_block(node)
        for child in node.children:
            self.recurse(child)

    def text(self, node):
        font = _node_font(node)
        color = resolve_color(node.style.get("color", "black")) or "black"
        white_space = node.style.get("white-space", "normal")
        content = node.text
        if white_space == "pre":
            for k, line in enumerate(content.replace("\t", "    ").split("\n")):
                if k > 0:
                    self.flush(force=True)
                self._place_word(line, font, color, node, measure=False, nowrap=True)
            return
        if white_space == "nowrap":
            # white-space:nowrap collapses spaces but forbids wrapping inside
            # the element: the whole run is one unbreakable token. It still
            # moves to the next line as a unit when the current one runs out
            # of room (e.g. Wikipedia's language cloud of <a> links), instead
            # of one-word-per-line forever overflowing the viewport.
            words = content.split()
            if words:
                self._place_word(" ".join(words), font, color, node,
                                 nowrap=True)
            return
        for word in content.split():
            self._place_word(word, font, color, node)

    def _place_word(self, word, font, color, node, measure=True, nowrap=False):
        if not word:
            return
        w = _measure(font, word)
        x0, x1 = self._line_bounds()
        if x0 >= x1:
            # A float covers the whole line (e.g. a full-width floated table):
            # don't draw the word on top of the float, drop below it first.
            # Flush any words already queued so their baseline isn't dragged
            # down with the cursor.
            if self.line:
                self.flush()
            bottom = self.cursor_y
            for f in self._all_float_regions():
                if f["top"] <= self.cursor_y < f["bottom"]:
                    bottom = max(bottom, f["bottom"])
            if bottom > self.cursor_y:
                self.cursor_y = bottom
            self.cursor_x = self._line_bounds()[0]
            x0, x1 = self._line_bounds()
        if self.cursor_x + w > x1 and self.cursor_x > x0:
            # The token doesn't fit on the remaining space: break before it.
            # A nowrap token breaks here as a whole unit (never inside); a
            # pre line (measure=False) starts a fresh line and simply
            # overflows when wider than the line.
            if self.line:
                self.flush()
            self.cursor_x = self._line_bounds()[0]
        self.line.append(_LineItem("text", self.cursor_x, word, font, color, node, w, 0))
        self.cursor_x += w + (_measure(font, " ") if measure else 0)

    def flush(self, force=False):
        line_top = self.cursor_y
        control_h, self.line_control_h = self.line_control_h, 0.0
        if not self.line:
            if not force and not control_h:
                return
            # A bare <br> (or <br><br>) still has to advance the line; advance
            # by one line box using the current font metrics. A line holding
            # nothing but form controls advances past the controls instead --
            # leaving it at zero height stacks the next line, and every hit
            # box on it, on top of this one.
            advance = 0.0
            if force:
                font = _node_font(self.node)
                advance = _line_height(self.node, font)
            self.cursor_y = line_top + max(advance, control_h)
            self.cursor_x = self._line_bounds()[0]
            return

        # The line box holds every inline box on it, each reaching `above` the
        # baseline and `below` it once its own half-leading is counted. The
        # baseline sits far enough down that the tallest of them fits.
        max_above = max_below = None
        for item in self.line:
            above, below = item.extents()
            if max_above is None or above > max_above:
                max_above = above
            if max_below is None or below > max_below:
                max_below = below
        baseline = self.cursor_y + max_above
        # Underlines join up word by word along a line; a new line starts a
        # new run even if the same link continues onto it.
        self._underline_run = None
        align = self.node.style.get("text-align", "left")
        line_width = (self.line[-1].x + self.line[-1].w) - self.x
        offset = 0
        if align == "center":
            offset = max(0, (self.width - line_width) / 2)
        elif align == "right":
            offset = max(0, self.width - line_width)

        for item in self.line:
            if item.kind == "img":
                y = baseline - item.ascent
                if item.photo:
                    self.display_list.append(DrawImage(
                        item.x + offset, y,
                        item.x + offset + item.w, y + item.h,
                        item.photo, item.node))
                    continue
                self.display_list.append(DrawOutline(
                    item.x + offset, y,
                    item.x + offset + item.w, y + item.h, "#aaaaaa"))
                xoff, ty, color = 4, y + 2, "#888888"
            elif item.kind == "video":
                y = baseline - item.ascent
                if item.photo is not None:
                    left = item.x + offset
                    self.display_list.append(DrawVideo(
                        left, y, left + item.w, y + item.h,
                        item.photo, item.node))
                    bar = _video_controls(item.node, left, y, item.w, item.h)
                    if bar is not None:
                        self.display_list.append(bar)
                    continue
                # No decodable picture: a dark box with the reason in it, at
                # the size the element would have had.
                self.display_list.append(DrawRect(
                    item.x + offset, y,
                    item.x + offset + item.w, y + item.h, "#1a1a1a"))
                xoff, ty, color = 6, y + 6, "#dddddd"
            elif item.kind == "block":
                # Now that the baseline is settled there is a place to put it.
                box = BlockLayout(item.node, self, None)
                box._float_pos = (item.x + offset, baseline - item.ascent,
                                  item.w)
                box.layout()
                self.children.append(box)
                self._underline_run = None
                continue
            elif item.kind == "listbox":
                self._paint_listbox(item.x + offset, baseline - item.ascent,
                                    item)
                self._underline_run = None
                continue
            elif item.kind == "pill":
                y = baseline - item.h
                if item.bg:
                    self.display_list.append(DrawRect(
                        item.x + offset, y,
                        item.x + offset + item.w, y + item.h, item.bg))
                ty = y + item.pt + max(
                    0.0, (item.h - item.pt - item.pb - _linespace(item.font)) / 2)
                xoff, color = item.pl, item.color
            else:
                y = baseline - _metrics(item.font, "ascent")
                xoff, ty, color = 0, y, item.color
            self.display_list.append(DrawText(
                item.x + offset + xoff, ty, item.text, item.font, color, item.node))
            if item.kind == "text":
                self._maybe_underline(
                    item.x + offset, y, item.text, item.font, item.color, item.node)
            else:
                # An image or a form control breaks the run: browsers do not
                # rule a line under a button that happens to sit in a link.
                self._underline_run = None
        self.cursor_y = max(baseline + max_below, line_top + control_h)
        self.cursor_x = self._line_bounds()[0]
        self.line = []

    def _maybe_underline(self, x, y, word, font, color, node):
        """Underline a word when the nearest box that says anything about
        text-decoration asks for one.

        The property does not inherit -- the box that declares it draws the
        line through everything inside -- so the *nearest* declaration wins
        and settles the question. That is what makes `a { text-decoration:
        none }` work: without it the UA sheet's underline on every link
        could never be taken back.

        The box that declared it is also what decides where the line stops:
        consecutive words belonging to the same one are ruled with a single
        line, spaces included, while two links side by side keep the gap
        between them clear.
        """
        owner = None
        n = node
        while n is not None:
            if isinstance(n, Element):
                decoration = n.style.get("text-decoration") \
                    or n.style.get("text-decoration-line")
                if decoration:
                    if "underline" in decoration.lower().split():
                        owner = n
                    break
                if n.tag in ("a", "u"):
                    # No sheet loaded at all (some tests lay out bare DOMs);
                    # links and <u> are underlined by every browser's default.
                    owner = n
                    break
            n = n.parent
        if owner is None:
            self._underline_run = None
            return
        yb = y + _metrics(font, "ascent") + 1
        right = x + _measure(font, word)
        run = self._underline_run
        if run is not None and run[0] is owner and run[1].top == yb \
                and run[1].color == color and run[1].right <= right:
            run[1].right = right
            return
        line = DrawLine(x, yb, right, yb, color, 1)
        self.display_list.append(line)
        self._underline_run = (owner, line)

    def _draw_bullet(self, top):
        """Draw the list item's marker in the margin to its left, level with
        the first line of the item, whose top is `top`.

        Which marker depends on `list-style-type`, which inherits, so a
        `list-style: none` on the <ul> reaches every <li> inside it and an
        <ol> counts in whatever numbering its sheet asked for.
        """
        style = self.node.style
        kind = (style.get("list-style-type") or "disc").strip().lower()
        if kind == "none":
            return
        color = resolve_color(style.get("color", "black")) or "black"
        text = _marker_text(kind, _list_index(self.node))
        if text is not None:
            font = _node_font(self.node)
            self.display_list.append(
                DrawText(self.x - _measure(font, text) - 8, top,
                         text, font, color, self.node))
            return
        size = int(round(parse_px(style.get("font-size", "16px"), 16)))
        top = top + size * 0.5
        left = self.x - 14
        if kind == "square":
            self.display_list.append(
                DrawRect(left, top, left + 6, top + 6, color))
        elif kind == "circle":
            self.display_list.append(
                DrawOval(left, top, left + 6, top + 6, None, color))
        else:
            self.display_list.append(
                DrawOval(left, top, left + 6, top + 6, color))

    def _draw_hr(self, node):
        y = self.cursor_y + 4
        self.display_list.append(
            DrawLine(self.x, y, self.x + self.width, y, "#888888", 1))
        self.cursor_y = y + 6

    def _inline_img(self, node):
        alt = node.attributes.get("alt", "") if isinstance(node, Element) else ""
        src = node.attributes.get("src", "") if isinstance(node, Element) else ""
        photo = None
        cache = self._image_cache()
        if src and cache:
            photo = cache.get(src)
        if photo is None:
            # Placeholder box (image pending / failed to decode).
            label = f"[img: {alt}]" if alt else "[img]"
            font = get_font(12, "normal", "roman")
            w = _measure(font, label) + 8
            h = _linespace(font)
        else:
            label, font = "", None
            w, h = photo.width(), photo.height()
        w = self._fit_control(w, min_w=w)
        self.line.append(_LineItem("img", self.cursor_x, label, font, None,
                                   node, w, h, photo))
        self.cursor_x += w + (_measure(font, " ") if photo is None else w * 0.25)

    # HTML's default `<video>` box, used when the file says nothing useful
    # and the page gave no width or height.
    VIDEO_DEFAULT = (300, 150)

    def _inline_video(self, node):
        """Place a `<video>`.

        Sizing follows the same order a real browser uses, and the order
        matters most when the file is one we cannot decode: `width`/`height`
        attributes first, then the size the *container* declared -- which we
        know even for an MP4, because probing a container is cheap and does
        not need a codec -- and only then the 300x150 default. So a page whose
        video we cannot play still reserves the right hole in the layout
        instead of collapsing, and the text around it lands where it would in
        a browser that could play it.
        """
        if not isinstance(node, Element):
            return
        # The player lives on the element, attached by the tab once the file
        # has been fetched: one `<video>` is one playhead, so two tags on the
        # same URL scrub and pause independently.
        player = getattr(node, "video_player", None)
        info = getattr(player, "info", None)
        w = _video_attr(node, "width")
        h = _video_attr(node, "height")
        if not w or not h:
            intrinsic = (info.width, info.height) if info and info.width \
                else self.VIDEO_DEFAULT
            if not w and not h:
                w, h = intrinsic
            elif not w:
                w = max(1, int(round(h * intrinsic[0] / intrinsic[1])))
            else:
                h = max(1, int(round(w * intrinsic[1] / intrinsic[0])))
        photo = None
        if player is not None and player.track is not None:
            player.set_display_size(w, h)
            photo = player.photo
        label, font = "", None
        if photo is None:
            font = get_font(12, "normal", "roman")
            label = _video_label(node, player)
        w = self._fit_control(w, min_w=min(w, 40))
        self.line.append(_LineItem("video", self.cursor_x, label, font, None,
                                   node, w, h, photo))
        self.cursor_x += w + (_measure(font, " ") if font else w * 0.25)

    def _inline_block(self, node):
        """Place an inline-block that holds blocks: measure it in a box of its
        own, then reserve that much room on the line.

        The box itself is built again in flush(), once the baseline is known
        and there is somewhere to put it. Laying it out twice is the price of
        an inline-level thing whose height decides how tall the line is.
        """
        avail = max(1.0, self.width - (self.cursor_x - self.x))
        mi, ma = self._measure_width(node)
        w = max(1.0, min(max(avail, mi), max(mi, ma)))
        css_w = node.style.get("width", "")
        if css_w.strip().lower() not in ("", "auto", "fit-content",
                                         "min-content", "max-content"):
            w = max(1.0, _resolve_len(css_w, self.width, avail))
        w = self._fit_control(w, min_w=min(w, 20.0))
        probe = BlockLayout(node, self, None)
        probe._float_pos = (self.x, self.y, w)
        probe.layout()
        self.line.append(_LineItem("block", self.cursor_x, "",
                                   _node_font(node), None, node, w,
                                   probe.height))
        self.cursor_x += w + _measure(_node_font(node), " ")

    def _image_cache(self):
        """Walk up the layout tree to find the tab's image cache (a dict of
        absolute URL -> decoded image), if any was attached."""
        box = self
        while box is not None:
            cache = getattr(box, "image_cache", None)
            if cache is not None:
                return cache
            box = box.parent
        return None

    def _fit_control(self, w, min_w=20):
        """Flush if the control would overflow the line and clamp it to the
        space remaining; returns the fitted width."""
        if self.cursor_x + w > self._line_bounds()[1] and self.line:
            self.flush()
        if self.width and w > self.width - (self.cursor_x - self.x):
            w = max(min_w, self.width - (self.cursor_x - self.x))
        return w

    def _box_control(self, x, y, w, h, font, node, rect=None, outline=None,
                     thickness=1, texts=()):
        """Paint a control box (optional fill and border) plus its label
        text(s), record its hit box, and advance the cursor past it."""
        if rect:
            self.display_list.append(DrawRect(x, y, x + w, y + h, rect))
        if outline:
            self.display_list.append(DrawOutline(x, y, x + w, y + h, outline,
                                                 thickness))
        for tx, ty, text, tfont, color in texts:
            self.display_list.append(DrawText(tx, ty, text, tfont, color, node))
        self.input_boxes.append((x, y, x + w, y + h, node))
        self.line_control_h = max(self.line_control_h, (y - self.cursor_y) + h)
        self.cursor_x = x + w + _measure(font, " ")

    def _paint_control(self, node, label, wpad, hpad, rect, outline,
                       dx, dy, tcolor, dropdown=False, thickness=1,
                       glyph="#555555"):
        """Paint a button/select-shaped control from a resolved label."""
        font = get_font(13, "normal", "roman")
        w = self._fit_control(_measure(font, label) + wpad)
        h = _linespace(font) + hpad
        y = self.cursor_y
        texts = [(self.cursor_x + dx, y + dy, label, font, tcolor)]
        if dropdown:
            texts.append((self.cursor_x + w - 14, y + 4, "▾", font, glyph))
        self._box_control(self.cursor_x, y, w, h, font, node,
                          rect=rect, outline=outline, thickness=thickness,
                          texts=texts)

    def _inline_input(self, node):
        itype = node.attributes.get("type", "text").lower()
        if itype == "hidden":
            return
        if itype == "submit" or itype == "image":
            return self._inline_button(node)
        font = get_font(13, "normal", "roman")
        bull = _linespace(font)
        value = field_value(node)
        placeholder = node.attributes.get("placeholder", "")
        label = value
        if node.tag == "textarea":
            label = value.split("\n", 1)[0]
        if not label:
            label = placeholder if placeholder else ("" if value else " ")
        if itype == "password" and value:
            label = "•" * len(value)
        show_placeholder = not value and bool(placeholder)
        if itype in ("checkbox", "radio"):
            w = 18
            y = self.cursor_y
            h = bull + 2
            if self.cursor_x + w > self._line_bounds()[1] and self.line:
                self.flush()
            self.display_list.append(DrawOutline(
                self.cursor_x, y, self.cursor_x + w - 4, y + h, "#666666", 1))
            if field_checked(node):
                self.display_list.append(DrawText(
                    self.cursor_x + 1, y - 1, "✓", get_font(12, "bold", "roman"),
                    "#1a73e8", node))
            self.input_boxes.append(
                (self.cursor_x, y, self.cursor_x + w, y + h, node))
            self.line_control_h = max(self.line_control_h, h)
            self.cursor_x += w
            return
        if itype == "range":
            w = 200
            if "size" in node.attributes:
                try:
                    w = max(60, int(node.attributes["size"]) * 9)
                except ValueError:
                    pass
            w = self._fit_control(w)
            y = self.cursor_y
            h = bull + 2
            try:
                lo = float(node.attributes.get("min", 0))
                hi = float(node.attributes.get("max", 100))
            except ValueError:
                lo, hi = 0.0, 100.0
            try:
                cur = float(field_value(node) or lo)
            except ValueError:
                cur = lo
            span = (hi - lo) or 1.0
            frac = max(0.0, min(1.0, (cur - lo) / span))
            track_h = 6
            track_y = y + h / 2 - track_h / 2
            self.display_list.append(DrawRect(
                self.cursor_x, track_y, self.cursor_x + w, track_y + track_h,
                "#666666"))
            thumb_r = 7
            thumb_x = self.cursor_x + w * frac
            thumb_x = max(self.cursor_x + thumb_r,
                          min(self.cursor_x + w - thumb_r, thumb_x))
            self.display_list.append(DrawRect(
                self.cursor_x, track_y, thumb_x, track_y + track_h,
                "#1a73e8"))
            cy = track_y + track_h / 2
            self.display_list.append(DrawOval(
                thumb_x - thumb_r, cy - thumb_r, thumb_x + thumb_r,
                cy + thumb_r, fill="#1a73e8", outline="#999999"))
            self.input_boxes.append(
                (self.cursor_x, y, self.cursor_x + w, y + h, node))
            self.line_control_h = max(self.line_control_h, h)
            self.cursor_x += w
            return
        w = 160
        if "size" in node.attributes:
            try:
                w = max(24, int(node.attributes["size"]) * 9)
            except ValueError:
                pass
        w = self._fit_control(w)
        h = bull + 8
        y = self.cursor_y
        focused = "data-focused" in node.attributes
        color = "#3b82f6" if focused else "#999999"
        texts = []
        if label.strip():
            lw = _measure(font, label)
            if lw > w - 8:
                ratio = max(1.0, (w - 12) / (_measure(font, "m") or 1))
                label = label[:int(ratio)] + "…"
            texts = [(self.cursor_x + 4, y + 4, label, font,
                      "#8a8a8a" if show_placeholder else "#111111")]
        self._box_control(self.cursor_x, y, w, h, font, node,
                          outline=color, thickness=2 if focused else 1,
                          texts=texts)

    def _inline_button(self, node):
        if node.tag == "input":
            label = node.attributes.get("value", "") or "Submit"
        else:
            label = "".join(c.text for c in node.children
                            if isinstance(c, Text)).strip() or "Button"
        if not isinstance(label, str):
            label = str(label)
        pressed = "data-focused" in node.attributes
        self._paint_control(node, label, 16, 10,
                            "#dcdcdc" if not pressed else "#b9c9e8",
                            "#777777", 8, 5, "#222222")

    def _inline_pill(self, node, bg, pl, pr, pt, pb):
        """Paint a display:inline-block element (background + padding) as a
        single rounded-ish box with its text laid out inside, e.g. a button
        link. Falls back to normal inline flow if there is nothing to draw."""
        parts = []
        for child in node.children:
            if isinstance(child, Text):
                parts.append(child.text)
            elif isinstance(child, Element):
                parts.append("".join(
                    c.text for c in child.children if isinstance(c, Text)))
        label = "".join(parts).strip()
        # An empty inline-block sized by width/height is a colour swatch, a
        # rule, a bar in a chart -- no text to lay out, but very much
        # something to paint. Only a box with neither text nor a size has
        # nothing to say.
        width = parse_px(node.style.get("width", ""))
        height = parse_px(node.style.get("height", ""))
        if not label and not (width or height):
            return
        font = _node_font(node)
        color = resolve_color(node.style.get("color", "black")) or "black"
        w = max(_measure(font, label) if label else 0.0, width)
        total_w = self._fit_control(w + pl + pr, min_w=w)
        lh = parse_px(node.style.get("line-height", "0"))
        h = max(height, _linespace(font) if label else 0.0, lh) + pt + pb
        self.line.append(_LineItem("pill", self.cursor_x, label, font, color,
                                   node, total_w, h, bg=bg, pl=pl, pr=pr,
                                   pt=pt, pb=pb))
        self.cursor_x += total_w + _measure(font, " ")

    def _inline_select(self, node):
        # `size` and `multiple` ask for the options to be on the page rather
        # than behind a drop-down, which is a different shape of box: tall,
        # and taking up room the rest of the line has to make way for.
        if listbox_rows(node):
            return self._inline_listbox(node)
        # The closed control shows the *labels* of the chosen options, not
        # their values: the value is what the form submits, the label is what
        # the page told the reader to look for.
        label = ", ".join(option_label(opt) for opt in selected_options(node))
        disabled = "disabled" in node.attributes
        open_ = "data-focused" in node.attributes
        # A select with nothing to show still needs a box wide enough for the
        # arrow, so pad the empty label out rather than collapsing to a sliver.
        self._paint_control(node, label or "    ", 24, 8,
                            "#e9e9e9" if disabled else "#f2f2f2",
                            "#c8c8c8" if disabled else
                            ("#3b82f6" if open_ else "#999999"),
                            6, 4,
                            "#8a8a8a" if disabled else "#111111",
                            dropdown=True, thickness=2 if open_ else 1,
                            glyph="#bbbbbb" if disabled else "#555555")

    def _inline_listbox(self, node):
        """Reserve room on the line for an expanded <select>.

        Unlike the drop-down, this is page content and not an overlay: it is
        as tall as the rows it shows, and the line it sits on has to grow to
        hold it or the text after it would be drawn straight through it. So
        it joins the pending line as one atom, the way an inline-block does,
        and is painted in flush() once the baseline is settled and there is
        somewhere to put it.
        """
        rows = select_rows(node)
        font = get_font(13, "normal", "roman")
        indent = LISTBOX_INDENT if any(row.heading for row in rows) else 0
        w = 80.0
        for row in rows:
            w = max(w, _measure(font, row.label) + 2 * LISTBOX_PAD_X
                    + (0 if row.heading else indent))
        w = self._fit_control(w, min_w=40)
        h = listbox_rows(node) * LISTBOX_ROW_H + 2 * LISTBOX_PAD
        self.line.append(_LineItem("listbox", self.cursor_x, "", font, None,
                                   node, w, h))
        self.cursor_x += w + _measure(font, " ")

    def _paint_listbox(self, x, y, item):
        """Draw an expanded <select> and record it as one hit box.

        The rows are not separate controls -- which row a click landed on is
        arithmetic on this box, done where the click arrives -- so the whole
        listbox goes into input_boxes once, exactly as the closed control
        does.
        """
        node, font, w, h = item.node, item.font, item.w, item.h
        rows = select_rows(node)
        disabled = "disabled" in node.attributes
        focused = "data-focused" in node.attributes and not disabled
        shown = listbox_rows(node)
        top = listbox_scroll(node, len(rows))
        active = listbox_active(node, rows)
        chosen = {id(opt) for opt in selected_options(node)}
        indent = LISTBOX_INDENT if any(row.heading for row in rows) else 0
        self.display_list.append(DrawRect(
            x, y, x + w, y + h, "#e9e9e9" if disabled else "#ffffff"))
        self.display_list.append(DrawOutline(
            x, y, x + w, y + h,
            "#c8c8c8" if disabled else ("#3b82f6" if focused else "#999999"),
            2 if focused else 1))
        # Row text sits in the middle of its row rather than at the top, so a
        # row reads as a band the reader can aim at.
        inset = max(0.0, (LISTBOX_ROW_H - _linespace(font)) / 2)
        ry = y + LISTBOX_PAD
        for i in range(top, min(len(rows), top + shown)):
            row = rows[i]
            if row.heading:
                self.display_list.append(DrawText(
                    x + LISTBOX_PAD_X, ry + inset, row.label,
                    get_font(12, "bold", "roman"), "#8a8a8a", node))
                ry += LISTBOX_ROW_H
                continue
            picked = id(row.option) in chosen
            if picked:
                self.display_list.append(DrawRect(
                    x + 1, ry, x + w - 1, ry + LISTBOX_ROW_H,
                    "#c9d7ef" if disabled else "#3b82f6"))
            elif focused and i == active:
                # The keyboard is here but this row is not taken. A wash and a
                # ring, both paler than the solid fill a taken row gets, are
                # the only thing saying where the next Space would land.
                self.display_list.append(DrawRect(
                    x + 1, ry, x + w - 1, ry + LISTBOX_ROW_H, "#e5edfc"))
                self.display_list.append(DrawOutline(
                    x + 1, ry, x + w - 1, ry + LISTBOX_ROW_H, "#5b8def", 1))
            if disabled or not row.enabled:
                color = "#8a8a8a"
            elif picked:
                color = "#ffffff"
            else:
                color = "#111111"
            self.display_list.append(DrawText(
                x + LISTBOX_PAD_X + indent, ry + inset, row.label, font,
                color, node))
            ry += LISTBOX_ROW_H
        self.input_boxes.append((x, y, x + w, y + h, node))

    # -- painting --------------------------------------------------------

    def paint(self):
        cmds = []
        _paint_bg(self, cmds)
        if hasattr(self, "display_list"):
            cmds.extend(self.display_list)
        return cmds


class RowLayout(LayoutBox):
    """A single <tr>: a block box that stacks table cells horizontally via
    explicit coordinates assigned by _layout_table."""

    def paint(self):
        cmds = []
        _paint_bg(self, cmds)
        return cmds


class CellLayout(LayoutBox):
    """A <td>/<th>: owns its own pre-flattened display list plus the source
    node used for hit-testing links inside the cell."""

    def __init__(self, node, parent, previous):
        super().__init__(node, parent, previous)
        self.content = []

    def paint(self):
        cmds = []
        # A cell's border comes out of _paint_bg like any other box's now,
        # at the weight and colour the sheet asked for instead of the flat
        # grey outline this used to draw.
        _paint_bg(self, cmds, require_size=False)
        cmds.extend(self.content)
        return cmds


class DocumentLayout(LayoutBox):
    """Root of the layout tree; establishes the viewport width."""

    def __init__(self, node, width):
        super().__init__(node, None, None)
        self.viewport_width = width

    def layout(self):
        self.width = self.viewport_width - 16  # left/right gutter
        self.x = 8
        self.y = 8
        _prewarm(self.node)
        child = BlockLayout(self.node, self, None)
        self.children = [child]
        child.layout()
        self.height = child.height + 16

    def collect_inputs(self, out):
        """Gather hit-test rectangles for every form control in the tree."""
        stack = list(self.children)
        while stack:
            box = stack.pop()
            out.extend(getattr(box, "input_boxes", ()))
            stack.extend(box.children)
        return out

    def paint(self):
        return []


def paint_tree(layout_box, display_list, hidden=False, scroll=0):
    """Flatten a box tree into paint commands, honouring `visibility`: a box
    with `visibility:hidden` (or one nested under a hidden box, unless it
    explicitly opts back in with `visibility:visible`) is not painted.

    Also applies, in tree order:
      * `position: sticky`: offsets the box (and descendants) so it stays in
        view when the page has scrolled past its natural spot;
      * `z-index`: a numeric z-index lifts the box (and its paint) above
        lower stacking content. None/auto keeps document order (stable sort).
    """
    items = []
    _collect_paint(layout_box, items, hidden, scroll, 0, None)
    items.sort(key=lambda pair: pair[0] if pair[0] is not None else 0)
    for _z, cmd in items:
        display_list.append(cmd)


def _sticky_dy(node, natural_top, height, parent, scroll):
    """Extra vertical offset for a `position:sticky` element so it pins to its
    `top` when scrolling would otherwise carry it off-screen, clamped so it
    never leaves its containing block."""
    top = parse_px(node.style.get("top", ""), 0)
    dy = scroll + top - natural_top
    if dy <= 0:
        return 0
    if parent is not None:
        max_y = parent.y + parent.height - height
        max_dy = max(0.0, max_y - natural_top)
        dy = min(dy, max_dy)
    return dy if dy > 0 else 0


def _fixed_dy(node, natural_top, scroll):
    """Extra vertical offset for a `position:fixed` element: it pins to the
    viewport, so its screen position stays constant no matter how far the
    page has scrolled (the offset can be negative to pull it up above its
    natural spot)."""
    top = parse_px(node.style.get("top", ""), 0)
    return scroll + top - natural_top


def _shift_cmd(cmd, dy):
    """Return `cmd` shifted down by `dy`, leaving the original untouched.

    Paint commands are often cached on their box (inline content, table cell
    content) and re-emitted on every repaint; mutating them would accumulate
    the shift across scroll ticks, so a copy is made whenever a shift is
    actually needed."""
    if dy == 0:
        return cmd
    cmd = copy.copy(cmd)
    for attr in ("top", "bottom"):
        value = getattr(cmd, attr, None)
        if isinstance(value, (int, float)):
            setattr(cmd, attr, value + dy)
    return cmd


_CLIPPING_OVERFLOW = ("hidden", "clip")
_EMPTY_CLIP = (0.0, 0.0, -1.0, -1.0)


def _clips(style):
    """Whether this box cuts off what does not fit inside it.

    `scroll` and `auto` clip too, in a real browser -- but only because the
    part you cannot see is a scroll away. We have no scrollable sub-boxes, so
    treating them as clipping would lose the content for good.
    """
    return any(style.get(prop) in _CLIPPING_OVERFLOW
               for prop in ("overflow", "overflow-x", "overflow-y"))


def _clip_rect(box, node, dy):
    """The rectangle this box confines its contents to, or None.

    `overflow: hidden` is the common one, and the two clipping properties are
    here for one specific reason: they are how nearly every site hides the
    "skip to content" links that only screen readers are meant to reach.
    Without them those links pile up at the top of the page. `clip-path:
    inset(50%)` is today's spelling and `clip: rect(1px,1px,1px,1px)` is the
    one it replaced -- both are still in the wild, often in the same rule.
    """
    style = node.style
    left, top = box.x, box.y + dy
    right, bottom = left + box.width, top + box.height
    clip = None
    if _clips(style):
        clip = (left, top, right, bottom)

    path = (style.get("clip-path") or "").strip().lower()
    if path.startswith("inset("):
        insets = _four_sides(path[len("inset("):].split(")")[0])
        if insets:
            def edge(token, extent):
                return parse_px(token, 0.0) if not token.endswith("%") \
                    else extent * float(token[:-1]) / 100.0
            rect = (left + edge(insets["left"], box.width),
                    top + edge(insets["top"], box.height),
                    right - edge(insets["right"], box.width),
                    bottom - edge(insets["bottom"], box.height))
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                return _EMPTY_CLIP
            clip = _intersect_clip(clip, rect)

    legacy = (style.get("clip") or "").strip().lower()
    if legacy.startswith("rect("):
        # The old property measures every side from the box's top-left
        # corner, not inwards from each edge -- so `rect(1px,1px,1px,1px)`
        # is a one-pixel corner, which is the whole point of it.
        sides = _four_sides(legacy[len("rect("):].split(")")[0].replace(",", " "))
        if sides:
            def side(token, base, fallback):
                return fallback if token == "auto" else base + parse_px(token, 0.0)
            rect = (side(sides["left"], left, left),
                    side(sides["top"], top, top),
                    side(sides["right"], left, right),
                    side(sides["bottom"], top, bottom))
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                return _EMPTY_CLIP
            clip = _intersect_clip(clip, rect)
    return clip


def _intersect_clip(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (max(a[0], b[0]), max(a[1], b[1]),
            min(a[2], b[2]), min(a[3], b[3]))


def _clipped(cmd, clip):
    """Crop `cmd` to `clip`, or drop it when nothing of it would show."""
    if clip is None:
        return cmd
    left = getattr(cmd, "left", None)
    if left is None:
        return cmd
    cl, ct, cr, cb = clip
    x0, y0 = max(left, cl), max(cmd.top, ct)
    x1, y1 = min(cmd.right, cr), min(cmd.bottom, cb)
    if x0 > x1 or y0 > y1:
        return None
    if isinstance(cmd, (DrawText, DrawImage)):
        # Glyphs and bitmaps are not cut in half at this layer, so the test is
        # whether most of the command survives: a line of text spilling out of
        # a banner stays whole, and a paragraph stuffed into the 1x1 box of a
        # screen-reader-only link disappears, which is the intent both times.
        area = (cmd.right - left) * (cmd.bottom - cmd.top)
        if area > 0 and (x1 - x0) * (y1 - y0) / area < 0.5:
            return None
        return cmd
    cmd = copy.copy(cmd)
    cmd.left, cmd.top, cmd.right, cmd.bottom = x0, y0, x1, y1
    return cmd


def _collect_paint(box, items, hidden, scroll, dy, z, clip=None):
    node = getattr(box, "node", None)
    if isinstance(node, Element):
        vis = node.style.get("visibility")
        if vis == "hidden":
            hidden = True
        elif vis == "visible":
            hidden = False
    own_dy = dy
    if isinstance(node, Element):
        pos = node.style.get("position")
        if pos == "sticky":
            own_dy += _sticky_dy(node, box.y, box.height, box.parent, scroll)
        elif pos == "fixed":
            own_dy += _fixed_dy(node, box.y, scroll)
    # A numeric z-index establishes a stacking context: the box's own paint
    # AND everything beneath it paint together at that level, so the box's
    # background can never cover its own text.
    if isinstance(node, Element):
        zs = node.style.get("z-index")
        if zs:
            try:
                z = int(zs)
            except ValueError:
                pass
    if isinstance(node, Element):
        clip = _intersect_clip(clip, _clip_rect(box, node, own_dy))
    if not hidden:
        for cmd in box.paint():
            cmd = _clipped(_shift_cmd(cmd, own_dy), clip)
            if cmd is not None:
                items.append((z, cmd))
    for child in box.children:
        _collect_paint(child, items, hidden, scroll, own_dy, z, clip)
