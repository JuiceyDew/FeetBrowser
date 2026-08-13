"""A from-scratch layout engine.

Produces a layout tree from a styled DOM and a display list of paint
commands. Implements block-and-inline flow: block boxes stack vertically,
inline content flows into lines with word wrapping. Supports font size /
weight / style, colors, backgrounds, list bullets, and horizontal rules.

Coordinates are in CSS px == canvas px. Fonts are Tk fonts, cached.
"""

import re
import tkinter.font

from .htmlparser import Text, Element

# Tags whose default flow is inline.
INLINE_ELEMENTS = {
    "a", "b", "i", "em", "strong", "span", "small", "big", "sub", "sup",
    "code", "tt", "kbd", "samp", "u", "abbr", "cite", "q", "s", "strike",
    "font", "label", "br", "img", "input", "textarea", "button",
    "mark", "time", "var",
}

_FONT_CACHE = {}


def get_font(size, weight, style, family=""):
    key = (size, weight, style, family)
    if key not in _FONT_CACHE:
        fam = family if family else "Times"
        _FONT_CACHE[key] = tkinter.font.Font(
            size=size, weight=weight, slant=style, family=fam)
    return _FONT_CACHE[key]


class DrawText:
    def __init__(self, x1, y1, text, font, color, node=None):
        self.top = y1
        self.left = x1
        self.text = text
        self.font = font
        self.color = color
        self.node = node  # source DOM node, for hit-testing links
        self.right = x1 + font.measure(text)
        self.bottom = y1 + font.metrics("linespace")

    def hit(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def execute(self, scroll, canvas):
        try:
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill=self.color, anchor="nw")
        except tkinter.TclError:
            # Un-drawable color flagged during paint (layout already tried to
            # sanitize); degrade rather than white-screen the browser.
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill="black", anchor="nw")


class DrawRect:
    def __init__(self, x1, y1, x2, y2, color):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.color = color

    def execute(self, scroll, canvas):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill=self.color)
        except tkinter.TclError:
            pass  # un-drawable background color; skip the rect


class DrawLine:
    def __init__(self, x1, y1, x2, y2, color, thickness=1):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.color = color
        self.thickness = thickness

    def execute(self, scroll, canvas):
        try:
            canvas.create_line(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill=self.color, width=self.thickness)
        except tkinter.TclError:
            pass  # un-drawable color; skip the line


class DrawOutline:
    def __init__(self, x1, y1, x2, y2, color, thickness=1, node=None):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.color = color
        self.thickness = thickness
        self.node = node  # source DOM node, for hit-testing form controls

    def hit(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def execute(self, scroll, canvas):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=self.thickness, outline=self.color)
        except tkinter.TclError:
            pass  # un-drawable color; skip the outline


def parse_px(value, default=0.0):
    try:
        if value.endswith("px"):
            return float(value[:-2])
        if value.endswith("%"):
            return default
        return float(value)
    except (ValueError, AttributeError):
        return default


_COLOR_RE = re.compile(r"^#[0-9a-f]{3}$|^#[0-9a-f]{6}$|^[a-z]+$")


def resolve_color(name):
    """Return a Tk-drawable color for `name`, or None if it isn't one.

    The CSS engine stores values verbatim, so we have to handle things the
    paint stage can't draw: CSS custom properties (``var(--x, fallback)``),
    shorthand backgrounds (``url(...) 0 -261px repeat-x``), gradients, and
    4/8-digit hex (Tk only understands #rgb/#rrggbb and color names).
    """
    if not name:
        return None
    name = name.strip().lower()
    if name in ("transparent", "none", "currentcolor", "inherit", "initial"):
        return None
    # var(--name, fallback[, nested var()...]) -> use the last fallback arg.
    if "var(" in name:
        depth = 0
        last = 0
        for i, ch in enumerate(name):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 1:
                last = i + 1
        tail = name[last:].rstrip(")")
        return resolve_color(tail) if tail else None
    # Images, gradients and multi-token shorthand aren't colors.
    if "url(" in name or "gradient" in name or any(ch.isspace() for ch in name):
        return None
    # 4-digit #RGB: drop alpha. 8-digit #RRGGBBAA: drop alpha.
    if re.fullmatch(r"#[0-9a-f]{4}", name):
        name = "#" + name[1:4]
    elif re.fullmatch(r"#[0-9a-f]{8}", name):
        name = "#" + name[1:7]
    if _COLOR_RE.match(name):
        return name
    return None


class LayoutBox:
    """Base class carrying geometry."""

    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.x = self.y = self.width = self.height = 0


class BlockLayout(LayoutBox):
    def layout(self):
        node = self.node
        self.x = self.parent.x + parse_px(node.style.get("margin-left", "0"))
        self.width = self.parent.width - parse_px(node.style.get("margin-left", "0")) \
            - parse_px(node.style.get("margin-right", "0"))
        if self.previous:
            # margin-bottom is "0" for text nodes, so this is safe for them too.
            self.y = self.previous.y + self.previous.height \
                + parse_px(self.previous.node.style.get("margin-bottom", "0"))
        else:
            self.y = self.parent.y + parse_px(
                self.parent.node.style.get("padding-top", "0"))
        self.y += parse_px(node.style.get("margin-top", "0"))

        mode = self.layout_mode()
        if mode == "block":
            self._layout_block()
        else:
            self._layout_inline()

    def layout_mode(self):
        node = self.node
        if isinstance(node, Text):
            return "inline"
        for child in node.children:
            if isinstance(child, Text):
                if child.text.strip():
                    return "inline"
            elif child.style.get("display") not in ("none",) and \
                    (child.tag in INLINE_ELEMENTS and child.style.get("display") is None
                     or child.style.get("display") in ("inline", "inline-block")):
                continue
            elif isinstance(child, Element) and child.style.get("display") == "none":
                continue
            else:
                return "block"
        # No block children -> inline (even if empty).
        return "inline"

    def _layout_block(self):
        previous = None
        for child in self.node.children:
            if isinstance(child, Element) and child.style.get("display") == "none":
                continue
            if isinstance(child, Text) and not child.text.strip():
                continue
            box = BlockLayout(child, self, previous)
            self.children.append(box)
            previous = box
        content_h = 0
        for box in self.children:
            box.layout()
        if self.children:
            last = self.children[-1]
            content_h = (last.y + last.height
                         + parse_px(last.node.style.get("margin-bottom", "0"))
                         - self.y)
        self.height = content_h + parse_px(self.node.style.get("padding-top", "0")) \
            + parse_px(self.node.style.get("padding-bottom", "0"))

    def _layout_inline(self):
        self.display_list = []
        self.cursor_x = self.x
        self.cursor_y = self.y
        self.line = []  # pending words on the current line
        self.line_max_ascent = 0  # tallest control box on the current line
        self.line_max_descent = 0

        # List item bullet.
        if isinstance(self.node, Element) and \
                self.node.style.get("display") == "list-item":
            self._draw_bullet()

        self.recurse(self.node)
        self.flush()
        self.height = self.cursor_y - self.y \
            + parse_px(self.node.style.get("padding-bottom", "0"))

    # -- inline layout ---------------------------------------------------

    def _font_for(self, node):
        style = node.style
        size = int(round(parse_px(style.get("font-size", "16px"), 16)))
        size = max(6, min(size, 80))
        weight = "bold" if style.get("font-weight") in ("bold", "bolder", "600",
                                                         "700", "800", "900") else "normal"
        slant = "italic" if style.get("font-style") in ("italic", "oblique") else "roman"
        fam = style.get("font-family", "")
        if fam:
            fam = fam.split(",")[0].strip().strip("'\"")
            if fam.lower() in ("monospace", "courier"):
                fam = "Courier"
            elif fam.lower() in ("serif", "times"):
                fam = "Times"
            elif fam.lower() in ("sans-serif", "arial", "helvetica"):
                fam = "Helvetica"
        return get_font(size, weight, slant, fam)

    def recurse(self, node):
        if isinstance(node, Text):
            self.text(node)
            return
        if node.style.get("display") == "none":
            return
        if node.tag == "br":
            self.flush()
            return
        if node.tag == "img":
            self._inline_img(node)
            return
        if node.tag == "hr":
            self.flush()
            self._draw_hr(node)
            return
        if node.tag == "input":
            self._inline_input(node)
            return
        if node.tag == "textarea":
            self._inline_textarea(node)
            return
        for child in node.children:
            self.recurse(child)

    def text(self, node):
        font = self._font_for(node)
        color = resolve_color(node.style.get("color", "black")) or "black"
        white_space = node.style.get("white-space", "normal")
        content = node.text
        if white_space != "pre":
            words = content.split()
        else:
            words = content.replace("\t", "    ").split("\n")
            for k, line in enumerate(words):
                if k > 0:
                    self.flush()
                self._place_word(line, font, color, node, measure=False)
            return
        for word in words:
            self._place_word(word, font, color, node)

    def _place_word(self, word, font, color, node, measure=True):
        w = font.measure(word)
        if measure and self.cursor_x + w > self.x + self.width and self.line:
            self.flush()
        self.line.append((self.cursor_x, word, font, color, node))
        self.cursor_x += w + (font.measure(" ") if measure else 0)

    def flush(self):
        if not self.line and not (self.line_max_ascent or self.line_max_descent):
            # Still advance for empty <br> lines.
            return
        metrics = [font.metrics() for _, _, font, _, _ in self.line]
        max_ascent = max((m["ascent"] for m in metrics), default=0)
        max_descent = max((m["descent"] for m in metrics), default=0)
        max_ascent = max(max_ascent, self.line_max_ascent)
        max_descent = max(max_descent, self.line_max_descent)
        baseline = self.cursor_y + 1.25 * max_ascent
        align = self.node.style.get("text-align", "left")
        line_width = 0
        if self.line:
            line_width = (self.line[-1][0]
                          + self.line[-1][2].measure(self.line[-1][1])) - self.x
        offset = 0
        if align == "center":
            offset = max(0, (self.width - line_width) / 2)
        elif align == "right":
            offset = max(0, self.width - line_width)

        for x, word, font, color, node in self.line:
            y = baseline - font.metrics("ascent")
            self.display_list.append(DrawText(x + offset, y, word, font, color, node))
            self._maybe_underline(x + offset, y, word, font, color, node)
        self.cursor_y = baseline + 1.25 * max_descent
        self.cursor_x = self.x
        self.line = []
        self.line_max_ascent = 0
        self.line_max_descent = 0

    def _maybe_underline(self, x, y, word, font, color, node):
        # Walk up to see if any ancestor requests underline (links, <u>).
        n = node
        underline = False
        while n is not None:
            if isinstance(n, Element):
                if n.tag in ("a", "u") or n.style.get("text-decoration") == "underline":
                    underline = True
                    break
            n = n.parent
        if underline:
            yb = y + font.metrics("ascent") + 1
            self.display_list.append(
                DrawLine(x, yb, x + font.measure(word), yb, color, 1))

    def _draw_bullet(self):
        size = int(round(parse_px(self.node.style.get("font-size", "16px"), 16)))
        color = resolve_color(self.node.style.get("color", "black")) or "black"
        by = self.cursor_y + size * 0.5
        bx = self.x - 14
        self.display_list.append(DrawRect(bx, by, bx + 5, by + 5, color))

    def _draw_hr(self, node):
        y = self.cursor_y + 4
        self.display_list.append(
            DrawLine(self.x, y, self.x + self.width, y, "#888888", 1))
        self.cursor_y = y + 6

    def _inline_img(self, node):
        # We don't decode images; draw a labelled placeholder box. The node is
        # attached so an <img> inside an <a> is still clickable.
        alt = node.attributes.get("alt", "") if isinstance(node, Element) else ""
        label = f"[img: {alt}]" if alt else "[img]"
        font = get_font(12, "normal", "roman")
        w = font.measure(label) + 8
        if self.cursor_x + w > self.x + self.width and self.line:
            self.flush()
        h = font.metrics("linespace")
        self.display_list.append(
            DrawOutline(self.cursor_x, self.cursor_y + 2,
                        self.cursor_x + w, self.cursor_y + h, "#aaaaaa",
                        node=node))
        self.display_list.append(
            DrawText(self.cursor_x + 4, self.cursor_y + 2, label, font,
                     "#888888", node))
        self.line_max_ascent = max(self.line_max_ascent, 0.8 * h)
        self.line_max_descent = max(self.line_max_descent, 0.2 * h)
        self.cursor_x += w + font.measure(" ")

    def _inline_textarea(self, node):
        # Draw a multi-line box for <textarea>. Forms aren't wired up yet, but
        # the control should at least be visible.
        font = get_font(14, "normal", "roman")
        value = "".join(c.text for c in node.children if isinstance(c, Text))
        first = value.strip().split("\n")[0] if value.strip() else ""
        w = max(80, len(first) * font.measure("0") + 16)
        h = font.metrics("linespace") * 2 + 8
        self._place_control(node, w, h, first, font, "#111111", "#8a8a8a")

    def _inline_input(self, node):
        # Form fields aren't interactive yet, but render them as visible boxes
        # (text bar / submit buttons) instead of vanishing into whitespace.
        ftype = (node.attributes.get("type", "text") or "text").lower()
        font = get_font(14, "normal", "roman")
        height = font.metrics("linespace") + 6
        if ftype == "hidden":
            return
        if ftype == "submit":
            label = (node.attributes.get("value", "")
                     or node.attributes.get("title", "") or "Submit")
            width = font.measure(label) + 18
            self._place_control(node, width, height, label, font,
                                "#333333", "#8a8a8a")
        elif ftype in ("text", "search", "email", "url"):
            try:
                size = max(1, int(node.attributes.get("size", "20")))
            except ValueError:
                size = 20
            width = size * font.measure("0") + 10
            value = node.attributes.get("value", "") or \
                node.attributes.get("placeholder", "")
            self._place_control(node, width, height, value, font,
                                "#111111", "#8a8a8a")
        else:
            # checkbox / radio / button / password: a small box.
            self._place_control(node, 20, height - 4, "", font,
                                "#111111", "#8a8a8a")

    def _place_control(self, node, w, h, text, font, color, border):
        if self.cursor_x + w > self.x + self.width and self.line:
            self.flush()
        y = self.cursor_y
        self.display_list.append(
            DrawOutline(self.cursor_x, y, self.cursor_x + w, y + h, border,
                        node=node))
        if text:
            self.display_list.append(
                DrawText(self.cursor_x + 6, y + 3, text, font, color, node))
        # Let the control stretch the line it sits on so following blocks
        # stack below it instead of overlapping it.
        self.line_max_ascent = max(self.line_max_ascent, 0.8 * h)
        self.line_max_descent = max(self.line_max_descent, 0.2 * h)
        self.cursor_x += w + font.measure(" ")

    # -- painting --------------------------------------------------------

    def paint(self):
        cmds = []
        node = self.node
        if isinstance(node, Element):
            bg = resolve_color(node.style.get("background-color")) or \
                resolve_color(node.style.get("background"))
            if bg and self.width > 0 and self.height > 0:
                cmds.append(DrawRect(self.x, self.y, self.x + self.width,
                                     self.y + self.height, bg))
        if hasattr(self, "display_list"):
            cmds.extend(self.display_list)
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
        child = BlockLayout(self.node, self, None)
        self.children = [child]
        child.layout()
        self.height = child.height + 16

    def paint(self):
        return []


def paint_tree(layout_box, display_list):
    display_list.extend(layout_box.paint())
    for child in layout_box.children:
        paint_tree(child, display_list)
