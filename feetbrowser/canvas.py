"""The drawing surface: fonts, images, and a canvas of retained items.

Four things sit behind every pixel the browser puts on screen -- a font that
can measure text, an image that can report its size and hand back pixels, a
canvas that keeps drawing items under tags, and an error to raise when a
value is not usable. This module is all four, on top of our own font engine
and rasteriser.

The canvas is retained rather than immediate: items keep their identity and
their tags, `delete(tag)` removes a whole layer, and stacking order is
creation order, so a later rectangle covers an earlier one. Compositing to
pixels happens in `render()`, when a frame is actually wanted, rather than
inside every `create_*` call.

The shape of that API is inherited. It was grown from the widget set the
browser started on, and the toe plugins in the wild are written against it,
so the method names and keyword arguments are a compatibility surface now
even though nothing underneath them is shared. See docs/toes.md.
"""
import base64
import math

from . import fontengine, imagecodec, raster


class CanvasError(Exception):
    """Raised when the canvas is handed something it cannot draw with.

    A colour name that parses to nothing, a family with no face behind it, an
    image whose bytes are not a picture: all of them are the caller's mistake
    about a value, and all of them are recoverable by substituting a default.
    The display list depends on that -- every `execute` catches this and
    paints in black rather than dropping the box.
    """


# -- colours ---------------------------------------------------------------

# CSS named colours. Pages reach for these constantly; the browser's own
# chrome uses hex, so this table exists purely for author stylesheets.
_NAMED = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "darkgreen": "#006400",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f", "darkorange": "#ff8c00",
    "darkorchid": "#9932cc", "darkred": "#8b0000", "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1", "darkviolet": "#9400d3", "deeppink": "#ff1493",
    "deepskyblue": "#00bfff", "dimgray": "#696969", "dimgrey": "#696969",
    "dodgerblue": "#1e90ff", "firebrick": "#b22222", "floralwhite": "#fffaf0",
    "forestgreen": "#228b22", "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc",
    "ghostwhite": "#f8f8ff", "gold": "#ffd700", "goldenrod": "#daa520",
    "gray": "#808080", "grey": "#808080", "green": "#008000",
    "greenyellow": "#adff2f", "honeydew": "#f0fff0", "hotpink": "#ff69b4",
    "indianred": "#cd5c5c", "indigo": "#4b0082", "ivory": "#fffff0",
    "khaki": "#f0e68c", "lavender": "#e6e6fa", "lavenderblush": "#fff0f5",
    "lawngreen": "#7cfc00", "lemonchiffon": "#fffacd", "lightblue": "#add8e6",
    "lightcoral": "#f08080", "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3", "lightgreen": "#90ee90", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa", "lightslategray": "#778899",
    "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3", "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970",
    "mintcream": "#f5fffa", "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead", "navy": "#000080", "oldlace": "#fdf5e6",
    "olive": "#808000", "olivedrab": "#6b8e23", "orange": "#ffa500",
    "orangered": "#ff4500", "orchid": "#da70d6", "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98", "paleturquoise": "#afeeee",
    "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}

_COLOR_MEMO = {}


def color(value):
    """Parse a colour into an ``(r, g, b)`` triple.

    Returns None for "no colour" -- an empty string means transparent for a
    fill or an outline, which is how a stylesheet spells "leave this alone".
    Genuine nonsense raises CanvasError instead of quietly becoming
    transparent, because the display list relies on the error to fall back to
    black rather than paint an invisible box.
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        return value
    try:
        return _COLOR_MEMO[value]
    except KeyError:
        pass
    text = value.strip().lower()
    if not text:
        return None
    hexed = _NAMED.get(text, text)
    result = None
    if hexed.startswith("#"):
        digits = hexed[1:]
        if len(digits) == 3:
            result = tuple(int(c * 2, 16) for c in digits)
        elif len(digits) == 6:
            result = tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        elif len(digits) == 12:  # the inherited 16-bit-per-channel form
            result = tuple(int(digits[i:i + 4], 16) >> 8 for i in (0, 4, 8))
    if result is None:
        raise CanvasError("unknown color name %r" % value)
    if len(_COLOR_MEMO) < 20000:
        _COLOR_MEMO[value] = result
    return result


# -- fonts -----------------------------------------------------------------

_FONT_SEQ = [0]

# What a family name should resolve to when the requested face is missing.
# Ordered by preference; the first family present on the system wins.
_FALLBACKS = {
    "times": ("times new roman", "times", "georgia", "dejavu serif",
              "liberation serif", "noto serif"),
    "helvetica": ("helvetica", "helvetica neue", "arial", "dejavu sans",
                  "liberation sans", "noto sans"),
    "courier": ("courier new", "courier", "menlo", "dejavu sans mono",
                "liberation mono", "noto sans mono"),
}


class Font:
    """A measurable font: a face, a size, and the metrics layout asks for.

    ``measure`` is exact and additive: a string's width is the sum of its
    characters' advances, with no kerning. That is not a shortcut, it is the
    invariant the layout engine's per-character width cache is built on, so
    the rasteriser advances the pen by the same per-character amounts and
    painted text lands exactly where layout measured it would.
    """

    def __init__(self, family="", size=12, weight="normal", slant="roman",
                 **_ignored):
        self.family = family or "Times"
        self.size = abs(int(size)) if size else 12
        self.weight = weight
        self.slant = slant
        self.bold = weight in ("bold", "heavy")
        self.italic = slant in ("italic", "oblique")
        _FONT_SEQ[0] += 1
        self.name = "ftbsfont%d" % _FONT_SEQ[0]
        self.face = _resolve_face(self.family, self.bold, self.italic)
        if self.face is None:
            raise CanvasError("no usable font for %r" % self.family)
        self._scale = self.face.scale(self.size)
        self.ascent = int(round(self.face.ascent * self._scale))
        self.descent = int(round(-self.face.descent * self._scale))
        self.linespace = int(round(self.face.linespace() * self._scale))
        self._widths = {}
        self._faces = {}

    def face_for(self, ch):
        """The face that will actually draw `ch`, and its scale.

        A single face never covers everything a page throws at it -- arrows,
        stars, box-drawing, CJK -- and drawing .notdef boxes for those is a
        visible bug, so a character the primary face lacks is looked up in a
        fallback chain. Measuring and painting both come through here, which
        is what keeps them in agreement.
        """
        try:
            return self._faces[ch]
        except KeyError:
            pass
        face, gid = self.face, self.face.glyph_id(ch)
        if gid == 0 and not ch.isspace() and ch != "\ufffd":
            alt = _fallback_face(ch, self.bold, self.italic)
            if alt is not None:
                face, gid = alt, alt.glyph_id(ch)
        entry = (face, gid, face.scale(self.size))
        self._faces[ch] = entry
        return entry

    def measure(self, text):
        widths = self._widths
        total = 0.0
        for ch in text:
            try:
                total += widths[ch]
            except KeyError:
                face, gid, scale = self.face_for(ch)
                w = face.advance(gid) * scale
                widths[ch] = w
                total += w
        return total

    def draw(self, surface, text, x, baseline, fill, scale=1.0):
        """Paint `text` at device coordinates, advancing exactly as `measure`
        says it will.

        `scale` is device pixels per CSS pixel, and `x`/`baseline` are already
        in device pixels -- the caller has multiplied them. Everything else in
        here is a CSS-pixel quantity being converted on the way out: the pen
        advances by `advance * scale`, so the run occupies exactly the width
        layout measured, and the glyph is rasterised at `self.size * scale`
        rather than magnified from a 1x bitmap. That last part is the whole
        point of the parameter. A 12px glyph blown up to 24 device pixels is
        the blur we are trying to remove; a glyph outline hinted and filled at
        24 is a different, sharper bitmap with the curves resolved at the size
        it is actually shown. Coverage caching still works, because the cache
        key includes the size, so a page at one scale rasterises each glyph
        once and reuses it exactly as before.
        """
        pen = float(x)
        widths = self._widths
        size = self.size * scale
        for ch in text:
            face, gid, face_scale = self.face_for(ch)
            try:
                advance = widths[ch]
            except KeyError:
                advance = face.advance(gid) * face_scale
                widths[ch] = advance
            if not ch.isspace():
                cov, w, h, left, top = raster.glyph_bitmap(face, size, gid)
                if w:
                    surface.blit_coverage(cov, w, h, int(pen) + left,
                                          int(baseline) + top, fill)
            pen += advance * scale
        return pen - x

    def metrics(self, name=None):
        table = {"ascent": self.ascent, "descent": self.descent,
                 "linespace": self.linespace, "fixed": 0}
        return table[name] if name else table

    def cget(self, option):
        return getattr(self, option, "")

    def actual(self, option=None):
        table = {"family": self.family, "size": self.size,
                 "weight": self.weight, "slant": self.slant}
        return table[option] if option else table

    def __repr__(self):
        return "<Font %s %dpx%s%s>" % (self.family, self.size,
                                       " bold" if self.bold else "",
                                       " italic" if self.italic else "")


_FACE_MEMO = {}
_CHAR_FACE_MEMO = {}

# Tried in order when the requested face has no glyph for a character. These
# are the families that actually carry the symbol, arrow and CJK ranges on
# the platforms we run on; anything still missing falls through to a scan of
# every installed family.
_FALLBACK_FAMILIES = (
    "apple symbols", "arial unicode ms", "segoe ui symbol",
    "noto sans symbols 2", "noto sans symbols", "symbola", "dejavu sans",
    "menlo", "consolas", "arial", "helvetica", "times new roman",
    "hiragino sans", "noto sans cjk jp", "microsoft yahei", "pingfang sc",
    "noto color emoji", "apple color emoji",
)


def _fallback_face(ch, bold, italic):
    """Find any installed face containing `ch`.

    The full scan is the slow path, but it runs at most once per distinct
    missing character for the life of the process, and only for characters
    the preferred families already failed to supply.
    """
    key = (ch, bold, italic)
    if key in _CHAR_FACE_MEMO:
        return _CHAR_FACE_MEMO[key]
    found = None
    for family in _FALLBACK_FAMILIES:
        face = fontengine.find(family, bold, italic)
        if face is not None and face.has_char(ch):
            found = face
            break
    if found is None:
        for family in sorted(fontengine.index()):
            face = fontengine.find(family, bold, italic)
            if face is not None and face.has_char(ch):
                found = face
                break
    _CHAR_FACE_MEMO[key] = found
    return found


def _resolve_face(family, bold, italic):
    """Find the best installed face, walking the fallback chain for the
    three generics the layout engine normalises font stacks down to."""
    key = (family.lower(), bold, italic)
    if key in _FACE_MEMO:
        return _FACE_MEMO[key]
    candidates = _FALLBACKS.get(family.lower(), (family.lower(),))
    face = None
    for name in candidates:
        face = fontengine.find(name, bold, italic)
        if face is not None:
            break
    if face is None:
        # Last resort: anything at all, so a page never fails to paint.
        for chain in _FALLBACKS.values():
            for name in chain:
                face = fontengine.find(name, bold, italic)
                if face is not None:
                    break
            if face is not None:
                break
    if face is None:
        available = fontengine.index()
        if available:
            first = sorted(available)[0]
            face = fontengine.find(first, bold, italic)
    _FACE_MEMO[key] = face
    return face


# -- images ----------------------------------------------------------------

# A GIF's delays are a request, not an instruction. Files written with 0 --
# "as fast as the machine can" -- are everywhere, mostly by accident, and no
# browser has honoured them since the 1990s: Chrome, Firefox and Safari all
# round anything under 20 ms up to 100 ms, so that is what we do. The decoder
# reports what the file said; the clamp lives here, with the animation.
MIN_GIF_DELAY_MS = 20
DEFAULT_GIF_DELAY_MS = 100


class PhotoImage:
    """Holds decoded RGBA pixels; the API surface is width()/height().

    A GIF may hold more than one frame, and then this object is the animation
    rather than a picture: `frames` is every frame composited onto the logical
    screen, `rgba` is whichever of them is showing, and `advance(now)` moves
    that on when the frame's time is up. Nothing that draws needs to know --
    a repaint blits `rgba` either way, which is the whole reason the animation
    lives in the image and not in the display list.
    """

    def __init__(self, data=None, file=None, width=None, height=None,
                 **_ignored):
        self.frames = ()
        self.delays = ()
        self.frame_index = 0
        # The count from the file's NETSCAPE2.0 extension: 0 for "for ever",
        # -1 when the file never said, and otherwise the number of times to
        # repeat *after* the first pass -- which is the extension's own
        # wording, and is what ImageMagick writes (`-loop 3` comes back as 2).
        self.loop_count = -1
        self.loops_done = 0
        self._frame_started = None
        if file is not None:
            with open(file, "rb") as handle:
                data = handle.read()
        if data is None:
            if width is None or height is None:
                raise CanvasError("PhotoImage needs data, a file, or a size")
            self._width, self._height = max(1, int(width)), max(1, int(height))
            self.rgba = bytearray(self._width * self._height * 4)
            self.opaque = False
            return
        if isinstance(data, str):
            # Image bytes may arrive as base64 text rather than raw.
            try:
                data = base64.b64decode(data)
            except Exception:
                data = data.encode("latin-1", "replace")
        try:
            if data[:6] in (b"GIF87a", b"GIF89a"):
                self._decode_gif(data)
            else:
                self._width, self._height, self.rgba = imagecodec.decode(data)
        except imagecodec.ImageError as exc:
            raise CanvasError(str(exc))
        # Checked once here so every blit can take the fast row-copy path;
        # most photos on the web have no transparency at all.
        self.opaque = self.rgba[3::4].count(255) == self._width * self._height

    def _decode_gif(self, data):
        width, height, frames, loops = imagecodec.decode_gif_frames(data)
        self._width, self._height = width, height
        self.frames = tuple(rgba for rgba, _delay in frames)
        self.delays = tuple(
            (delay if delay >= MIN_GIF_DELAY_MS else DEFAULT_GIF_DELAY_MS)
            for _rgba, delay in frames)
        self.loop_count = loops
        self.rgba = self.frames[0]

    @property
    def animated(self):
        """True when there is a second frame to move on to. A still GIF
        decodes to a one-frame animation, and is not one."""
        return len(self.frames) > 1 and not self.finished

    @property
    def finished(self):
        """True once a file with a finite loop count has run out of them and
        is holding its last frame. A file that never said loops once."""
        if self.loop_count == 0:
            return False
        passes = 1 if self.loop_count < 0 else 1 + self.loop_count
        return (self.loops_done >= passes
                and self.frame_index == len(self.frames) - 1)

    def advance(self, now):
        """Show the frame due at `now` (a monotonic clock, in seconds).

        Returns True when the visible frame changed, which is what tells the
        caller to repaint. The first call only starts the clock: an animation
        begins when something first asks it to, not when it was decoded, so a
        GIF that was fetched while the tab was in the background does not
        arrive already several seconds in.
        """
        if not self.animated:
            return False
        if self._frame_started is None:
            self._frame_started = now
            return False
        changed = False
        # Bounded by the frame count: a tab that was asleep for an hour comes
        # back to the right frame without walking an hour of them, and a file
        # of 10 ms frames cannot make one tick do unbounded work.
        for _ in range(len(self.frames)):
            due = self.delays[self.frame_index] / 1000.0
            if now - self._frame_started < due:
                break
            nxt = self.frame_index + 1
            if nxt >= len(self.frames):
                if self.finished:
                    break
                self.loops_done += 1
                nxt = 0
                if self.finished:
                    # The last pass ends on the last frame, not back at the
                    # first: a three-loop animation stops where it stopped.
                    break
            self.frame_index = nxt
            self._frame_started += due
            changed = True
        else:
            # Fell behind by more than one pass; re-anchor rather than keep
            # a start time that is now history.
            self._frame_started = now
        if changed:
            self.rgba = self.frames[self.frame_index]
        return changed

    def width(self):
        return self._width

    def height(self):
        return self._height

    def subsample(self, x, y=None):
        return self._scaled(self._width // max(1, x),
                            self._height // max(1, y or x))

    def zoom(self, x, y=None):
        return self._scaled(self._width * max(1, x),
                            self._height * max(1, y or x))

    def _scaled(self, width, height):
        """A resized copy of the frame showing now.

        A still, even when the original animates: resizing every frame of an
        animation is work for a picture nobody asked to keep moving, and the
        one caller of this is the compatibility API that toe plugins use to
        put a thumbnail somewhere.
        """
        clone = PhotoImage.__new__(PhotoImage)
        clone.rgba = imagecodec.resize(self.rgba, self._width, self._height,
                                       width, height)
        clone._width, clone._height = max(1, width), max(1, height)
        clone.opaque = self.opaque
        clone.frames = clone.delays = ()
        clone.frame_index = clone.loops_done = 0
        clone.loop_count = -1
        clone._frame_started = None
        return clone


# -- canvas ----------------------------------------------------------------

class _Item:
    __slots__ = ("id", "kind", "coords", "opts", "tags")

    def __init__(self, item_id, kind, coords, opts, tags):
        self.id = item_id
        self.kind = kind
        self.coords = coords
        self.opts = opts
        self.tags = tags


class Canvas:
    """Retained display list, composited on demand.

    The browser leans on both halves of the item identity: it diffs
    `find_all()` before and after a plugin draws in order to tag that
    plugin's items, and it deletes whole layers by tag on every frame.
    """

    def __init__(self, master=None, width=800, height=600, bg="white",
                 background=None, scale=None, **_ignored):
        self.master = master
        # Every coordinate handed to a create_* method is a CSS pixel, and the
        # framebuffer is in device pixels: `scale` is the ratio between them
        # and `render` is where it is applied. A canvas with no window over it
        # -- a test, a toe drawing a thumbnail -- has nobody to ask and draws
        # at 1:1.
        if scale is None:
            scale = getattr(master, "scale", 1.0)
        self.scale = float(scale) if scale and float(scale) > 0 else 1.0
        self._width = int(width)
        self._height = int(height)
        self.background = color(background or bg) or (255, 255, 255)
        self.cursor = ""
        self._items = []
        self._by_tag = {}
        self._next_id = 1
        self._device = ()
        self._reallocate(self._device_for(self._width, self._height))
        # Set by every mutation, cleared by render(). A platform window reads
        # it to skip presenting a frame nothing changed in.
        self.dirty = True

    # -- geometry ----------------------------------------------------------

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return self._height

    def winfo_reqwidth(self):
        return self._width

    def winfo_reqheight(self):
        return self._height

    def device_size(self):
        """The framebuffer's size in device pixels.

        Not the same number as winfo_width()/winfo_height() on a HiDPI
        display, and deliberately a separate call: everything that positions
        anything wants the CSS-pixel size, and the two places that want this
        one are the code that hands the buffer to the operating system and
        the tests that check it was the right size.
        """
        return self._device

    def _device_for(self, width, height):
        return (max(1, int(round(width * self.scale))),
                max(1, int(round(height * self.scale))))

    def _reallocate(self, device):
        if device == self._device:
            return
        self._device = device
        self.surface = raster.Surface(device[0], device[1], self.background)
        self.dirty = True

    def resize(self, width, height, device=None):
        """Resize to `width` x `height` CSS pixels.

        `device` is the framebuffer size the platform wants, when it knows it
        exactly -- see Window.resize. Without it the size is this many CSS
        pixels scaled and rounded, which is right everywhere the window
        system is not itself the authority on the answer.
        """
        width, height = max(1, int(width)), max(1, int(height))
        device = (tuple(max(1, int(v)) for v in device) if device
                  else self._device_for(width, height))
        if (width, height) == (self._width, self._height) and \
                device == self._device:
            return
        self._width, self._height = width, height
        self._reallocate(device)
        self.dirty = True

    def set_scale(self, scale, device=None):
        """Draw at a new device-pixel ratio from the next frame on.

        The retained items are untouched, because they were never in device
        pixels: they say where things are in CSS pixels and this only changes
        what that is worth. Returns True when the ratio actually moved.
        """
        scale = float(scale) if scale and float(scale) > 0 else 1.0
        if scale == self.scale:
            return False
        self.scale = scale
        self._reallocate(tuple(max(1, int(v)) for v in device) if device
                         else self._device_for(self._width, self._height))
        self.dirty = True
        return True

    def pack(self, **_ignored):
        # Geometry management is a single canvas filling its window, so
        # "packing" just means becoming the window's presented surface.
        if self.master is not None and hasattr(self.master, "canvas"):
            self.master.canvas = self
            self.resize(self.master.width, self.master.height)
        return self

    def place(self, **_ignored):
        return self

    def grid(self, **_ignored):
        return self

    def config(self, **kwargs):
        if "cursor" in kwargs:
            self.cursor = kwargs["cursor"]
        if "bg" in kwargs or "background" in kwargs:
            self.background = color(kwargs.get("bg")
                                    or kwargs.get("background")) or self.background
        if "width" in kwargs or "height" in kwargs:
            self.resize(kwargs.get("width", self._width),
                        kwargs.get("height", self._height))
        return self

    configure = config

    # -- item creation -----------------------------------------------------

    def _add(self, kind, coords, opts):
        tags = opts.pop("tags", ())
        if isinstance(tags, str):
            tags = (tags,)
        tags = tuple(tags)
        item = _Item(self._next_id, kind, coords, opts, tags)
        self._next_id += 1
        self._items.append(item)
        self.dirty = True
        for tag in tags:
            self._by_tag.setdefault(tag, []).append(item)
        return item.id

    def create_rectangle(self, x1, y1, x2, y2, **opts):
        # Validate colours now rather than at paint time, so a caller's
        # except-CanvasError fallback runs while it can still substitute one.
        color(opts.get("fill"))
        color(opts.get("outline"))
        return self._add("rectangle", (x1, y1, x2, y2), opts)

    def create_line(self, *coords, **opts):
        color(opts.get("fill"))
        return self._add("line", tuple(coords), opts)

    def create_text(self, x, y, **opts):
        color(opts.get("fill"))
        return self._add("text", (x, y), opts)

    def create_image(self, x, y, **opts):
        return self._add("image", (x, y), opts)

    def create_oval(self, x1, y1, x2, y2, **opts):
        color(opts.get("fill"))
        color(opts.get("outline"))
        return self._add("oval", (x1, y1, x2, y2), opts)

    def create_arc(self, x1, y1, x2, y2, **opts):
        color(opts.get("outline"))
        return self._add("arc", (x1, y1, x2, y2), opts)

    def create_polygon(self, *coords, **opts):
        color(opts.get("fill"))
        return self._add("polygon", tuple(coords), opts)

    # -- item management ---------------------------------------------------

    def find_all(self):
        return [item.id for item in self._items]

    def find_withtag(self, tag):
        return [item.id for item in self._resolve(tag)]

    def delete(self, *tags):
        self.dirty = True
        for tag in tags:
            if tag == "all":
                self._items = []
                self._by_tag = {}
                continue
            doomed = set(id(item) for item in self._resolve(tag))
            if not doomed:
                continue
            self._items = [i for i in self._items if id(i) not in doomed]
            for key, items in list(self._by_tag.items()):
                kept = [i for i in items if id(i) not in doomed]
                if kept:
                    self._by_tag[key] = kept
                else:
                    del self._by_tag[key]

    def addtag_withtag(self, newtag, tag):
        for item in self._resolve(tag):
            if newtag not in item.tags:
                item.tags += (newtag,)
                self._by_tag.setdefault(newtag, []).append(item)

    def itemconfig(self, tag, **opts):
        for item in self._resolve(tag):
            item.opts.update(opts)
            self.dirty = True

    itemconfigure = itemconfig

    def coords(self, tag, *values):
        items = self._resolve(tag)
        if not values:
            return list(items[0].coords) if items else []
        for item in items:
            item.coords = tuple(values)
            self.dirty = True

    def bbox(self, tag=None):
        items = self._items if tag in (None, "all") else self._resolve(tag)
        boxes = [self._bounds(i) for i in items]
        boxes = [b for b in boxes if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def _resolve(self, tag):
        if tag == "all":
            return list(self._items)
        if isinstance(tag, int):
            return [i for i in self._items if i.id == tag]
        if isinstance(tag, str) and tag.isdigit():
            wanted = int(tag)
            return [i for i in self._items if i.id == wanted]
        return list(self._by_tag.get(tag, ()))

    def _bounds(self, item):
        c = item.coords
        if item.kind == "text":
            font = item.opts.get("font")
            text = str(item.opts.get("text", ""))
            if font is None:
                return None
            w = font.measure(text)
            h = font.linespace
            x, y = c[0], c[1]
            if item.opts.get("anchor", "nw") == "w":
                y -= h / 2
            return (x, y, x + w, y + h)
        if item.kind == "image":
            photo = item.opts.get("image")
            if photo is None:
                return None
            return (c[0], c[1], c[0] + photo.width(), c[1] + photo.height())
        if len(c) < 4:
            return None
        xs, ys = c[0::2], c[1::2]
        return (min(xs), min(ys), max(xs), max(ys))

    # -- painting ----------------------------------------------------------

    def render(self, region=None):
        """Composite every item into the surface and return it.

        Painting the whole retained list each frame is what keeps the model
        honest: overlapping items, deletions and re-inserts all resolve
        correctly without tracking damaged pixels. `region` clips the work
        to a rectangle when only part of the frame changed; like every other
        coordinate here it is in CSS pixels.
        """
        surface = self.surface
        if region:
            x0, y0, x1, y1 = (v * self.scale for v in region)
            saved = surface.set_clip(x0, y0, x1, y1)
            surface.fill_rect(x0, y0, x1, y1, self.background)
        else:
            saved = None
            surface.fill_all(self.background)
        for item in self._items:
            self._paint(surface, item)
        if saved is not None:
            surface.reset_clip(saved)
        self.dirty = False
        return surface

    def _paint(self, surface, item):
        kind = item.kind
        opts = item.opts
        if kind == "text":
            # The one item that keeps its CSS-pixel coordinates this far:
            # anchoring is arithmetic on font metrics, which are measured in
            # CSS pixels, so it is done there and scaled at the last moment.
            self._paint_text(surface, item.coords, opts)
            return
        scale = self.scale
        c = item.coords if scale == 1.0 else tuple(v * scale
                                                   for v in item.coords)
        if kind == "rectangle":
            x0, y0, x1, y1 = _ordered(c)
            alpha = 128 if opts.get("stipple") else 255
            fill = color(opts.get("fill"))
            if fill:
                surface.fill_rect(x0, y0, x1, y1, fill, alpha)
            # A rectangle gets a black 1px border unless told otherwise,
            # so a caller that says nothing is asking for one. Saying
            # `outline=""` or `width=0` is how you decline it -- which is why
            # the absent case has to be told apart from the empty one.
            outline = color(opts["outline"]) if "outline" in opts else (0, 0, 0)
            width = self._stroke(opts.get("width", 1))
            if outline and width:
                surface.outline_rect(x0, y0, x1, y1, outline, width, alpha)
        elif kind == "line":
            stroke = color(opts.get("fill")) or (0, 0, 0)
            width = self._stroke(opts.get("width", 1))
            for i in range(0, len(c) - 3, 2):
                surface.draw_line(c[i], c[i + 1], c[i + 2], c[i + 3],
                                  stroke, width)
        elif kind == "image":
            photo = opts.get("image")
            if photo is not None:
                self._paint_image(surface, c, opts, photo)
        elif kind == "oval":
            self._paint_oval(surface, c, opts)
        elif kind == "arc":
            self._paint_arc(surface, c, opts)
        elif kind == "polygon":
            fill = color(opts.get("fill"))
            if fill and len(c) >= 6:
                points = [(c[i], c[i + 1]) for i in range(0, len(c) - 1, 2)]
                _fill_polygon(surface, points, fill)

    def _stroke(self, width):
        """A line width in device pixels: scaled, but never rounded away.

        A hairline is the case that matters. One CSS pixel of border is two
        device pixels at 2x and one at 1x, and at a fractional scale it is
        whatever that rounds to -- except zero, which would delete a border
        the page asked for rather than draw it thin. Declining a border by
        asking for a width of 0 still works, because 0 comes back as 0.
        """
        width = int(width)
        if width <= 0 or self.scale == 1.0:
            return width
        return max(1, int(round(width * self.scale)))

    def _paint_image(self, surface, coords, opts, photo):
        """Blit a picture, resampled to the device pixels it now covers.

        An image is the one thing on the canvas whose pixels are its own
        rather than ours: a 100x100 photo is 100x100 CSS pixels, which is
        200x200 device pixels on a 2x display, so the blit scales it. That is
        a magnification and it looks like one -- the real fix for a photo is
        to decode it at the size it will be drawn -- but it puts the picture
        in the right place at the right size, which is what the rest of the
        frame is relying on.
        """
        width = photo.width() * self.scale
        height = photo.height() * self.scale
        x, y = coords[0], coords[1]
        if opts.get("anchor", "nw") == "center":
            x -= width / 2
            y -= height / 2
        surface.blit_rgba(photo.rgba, photo.width(), photo.height(), x, y,
                          getattr(photo, "opaque", False),
                          int(round(width)), int(round(height)))

    def _paint_text(self, surface, coords, opts):
        font = opts.get("font")
        text = opts.get("text", "")
        if font is None or not text:
            return
        fill = color(opts.get("fill")) or (0, 0, 0)
        x, y = coords[0], coords[1]
        anchor = opts.get("anchor", "center")
        if anchor in ("nw", "n", "ne"):
            top = y
        else:
            top = y - font.linespace / 2
        if anchor in ("center", "n", "s"):
            x -= font.measure(text) / 2
        elif anchor in ("e", "ne", "se"):
            x -= font.measure(text)
        scale = self.scale
        baseline = top + font.ascent
        for line in str(text).split("\n"):
            font.draw(surface, line, x * scale, baseline * scale, fill, scale)
            baseline += font.linespace

    def _paint_oval(self, surface, coords, opts):
        """Fill an ellipse, then stroke its edge.

        List markers are what this is for: `list-style-type: disc` is a
        small filled dot and `circle` is the same dot hollow, and stroking
        an arc alone can only ever give you the hollow one.

        A marker is about six pixels across, which is small enough that
        aliasing decides what shape the reader thinks they are looking at --
        a hard-edged six-pixel ring reads as a square. Small ovals therefore
        go through the supersampled path, which costs a few hundred samples;
        big ones keep the cheap scanline, where a stair-step of one pixel in
        two hundred is invisible anyway.

        `coords` arrives in device pixels, so the limit that picks between
        the two paths is scaled with them: which shapes are small enough to
        need the sampled edge is a fact about how big they look, not about
        how many pixels the display happens to give them.
        """
        x0, y0, x1, y1 = _ordered(coords)
        fill = color(opts.get("fill"))
        outline = color(opts.get("outline"))
        rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        if rx <= 0 or ry <= 0 or not (fill or outline):
            return
        width = (float(opts.get("width", 1) or 0) * self.scale
                 if outline else 0.0)
        if max(x1 - x0, y1 - y0) <= _OVAL_AA_LIMIT * self.scale:
            self._paint_oval_aa(surface, x0, y0, x1, y1, fill, outline, width)
            return
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if fill:
            for y in range(int(math.floor(y0)), int(math.ceil(y1))):
                dy = (y + 0.5 - cy) / ry
                if abs(dy) > 1.0:
                    continue
                half = rx * math.sqrt(1.0 - dy * dy)
                surface.fill_rect(cx - half, y, cx + half, y + 1, fill, 255)
        if outline:
            self._paint_arc(surface, coords, opts)

    def _paint_oval_aa(self, surface, x0, y0, x1, y1, fill, outline, width):
        """Draw a small ellipse pixel by pixel, each one blended by how much
        of it the shape actually covers."""
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        # The outline is drawn inside the edge, so the hole is the ellipse
        # inset by its width. Without an outline the two coincide and every
        # pixel is simply interior.
        inner_rx, inner_ry = max(0.0, rx - width), max(0.0, ry - width)
        for y in range(int(math.floor(y0)), int(math.ceil(y1))):
            for x in range(int(math.floor(x0)), int(math.ceil(x1))):
                outer = _ellipse_coverage(x, y, cx, cy, rx, ry)
                if outer <= 0.0:
                    continue
                inner = _ellipse_coverage(x, y, cx, cy, inner_rx, inner_ry)
                if fill and inner > 0.0:
                    surface.fill_rect(x, y, x + 1, y + 1, fill,
                                      int(round(inner * 255)))
                if outline and outer > inner:
                    surface.fill_rect(x, y, x + 1, y + 1, outline,
                                      int(round((outer - inner) * 255)))

    def _paint_arc(self, surface, coords, opts):
        """Stroke an elliptical arc. Angles are degrees measured
        counter-clockwise from 3 o'clock, and the browser uses this for one
        thing: the loading spinner."""
        x0, y0, x1, y1 = _ordered(coords)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        stroke = color(opts.get("outline")) or color(opts.get("fill"))
        if not stroke or rx <= 0 or ry <= 0:
            return
        width = self._stroke(opts.get("width", 1))
        start = float(opts.get("start", 0.0))
        extent = float(opts.get("extent", 360.0))
        steps = max(8, int(abs(extent) / 4))
        prev = None
        for i in range(steps + 1):
            angle = math.radians(start + extent * i / steps)
            px = cx + rx * math.cos(angle)
            py = cy - ry * math.sin(angle)  # screen y grows downward
            if prev:
                surface.draw_line(prev[0], prev[1], px, py, stroke, width)
            prev = (px, py)

    # -- output ------------------------------------------------------------

    def to_png(self):
        return self.render().to_png()

    def save_png(self, path):
        self.render().save_png(path)


_OVAL_AA_LIMIT = 48   # px across; above this the scanline path is good enough
_OVAL_AA_SAMPLES = 4  # per axis, so 16 samples a pixel


def _ellipse_coverage(x, y, cx, cy, rx, ry, samples=_OVAL_AA_SAMPLES):
    """How much of pixel (x, y) falls inside the ellipse, from 0 to 1."""
    if rx <= 0 or ry <= 0:
        return 0.0
    step = 1.0 / samples
    inside = 0
    for row in range(samples):
        dy = (y + (row + 0.5) * step - cy) / ry
        dy2 = dy * dy
        if dy2 > 1.0:
            continue
        span = math.sqrt(1.0 - dy2) * rx
        for col in range(samples):
            dx = x + (col + 0.5) * step - cx
            if -span <= dx <= span:
                inside += 1
    return inside / float(samples * samples)


def _ordered(coords):
    x0, y0, x1, y1 = coords[:4]
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def _fill_polygon(surface, points, fill):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    left, top = int(min(xs)) - 1, int(min(ys)) - 1
    width = int(max(xs)) - left + 2
    height = int(max(ys)) - top + 2
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        return
    cov = raster.rasterize([points], width, height, -left, -top)
    surface.blit_coverage(cov, width, height, left, top, fill)
