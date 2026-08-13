"""A from-scratch CSS parser + cascade.

Supports: tag / class / id / universal selectors, descendant combinators,
grouped selectors (a, b), a property/value declaration parser, specificity,
inheritance of inherited properties, and inline style="" attributes.
"""

from .htmlparser import Element

INHERITED_PROPERTIES = {
    "font-size": "16px",
    "font-style": "normal",
    "font-weight": "normal",
    "font-family": "",
    "color": "black",
    "line-height": "normal",
    "text-align": "left",
    "white-space": "normal",
    "list-style-type": "disc",
}


class TagSelector:
    def __init__(self, tag):
        self.tag = tag
        self.priority = (0, 0, 1) if tag != "*" else (0, 0, 0)

    def matches(self, node):
        return isinstance(node, Element) and (self.tag == "*" or node.tag == self.tag)


class ClassSelector:
    def __init__(self, cls):
        self.cls = cls
        self.priority = (0, 1, 0)

    def matches(self, node):
        if not isinstance(node, Element):
            return False
        return self.cls in node.attributes.get("class", "").split()


class IdSelector:
    def __init__(self, id_):
        self.id = id_
        self.priority = (1, 0, 0)

    def matches(self, node):
        return isinstance(node, Element) and node.attributes.get("id") == self.id


class CompoundSelector:
    """One or more simple selectors on the same element, e.g. div.note#x"""

    def __init__(self, parts):
        self.parts = parts
        self.priority = tuple(sum(p) for p in zip(*[s.priority for s in parts]))

    def matches(self, node):
        return all(p.matches(node) for p in self.parts)


class DescendantSelector:
    def __init__(self, ancestor, descendant):
        self.ancestor = ancestor
        self.descendant = descendant
        self.priority = tuple(
            a + b for a, b in zip(ancestor.priority, descendant.priority))

    def matches(self, node):
        if not self.descendant.matches(node):
            return False
        parent = node.parent
        while parent:
            if self.ancestor.matches(parent):
                return True
            parent = parent.parent
        return False


class CSSParser:
    def __init__(self, s):
        self.s = s
        self.i = 0

    def whitespace(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n\f":
            self.i += 1

    def comments(self):
        while self.s[self.i:self.i + 2] == "/*":
            end = self.s.find("*/", self.i)
            self.i = len(self.s) if end == -1 else end + 2
            self.whitespace()

    def skip_ws(self):
        self.whitespace()
        self.comments()
        self.whitespace()

    def literal(self, ch):
        if self.i < len(self.s) and self.s[self.i] == ch:
            self.i += 1
            return True
        return False

    def pair(self):
        # property : value
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ":;}":
            self.i += 1
        prop = self.s[start:self.i].strip().lower()
        if not self.literal(":"):
            return None
        vstart = self.i
        while self.i < len(self.s) and self.s[self.i] not in ";}":
            self.i += 1
        value = self.s[vstart:self.i].strip()
        return (prop, value) if prop and value else None

    def body(self):
        """Parse a declaration block { ... } already positioned after '{'."""
        pairs = {}
        while self.i < len(self.s) and self.s[self.i] != "}":
            self.skip_ws()
            if self.i >= len(self.s) or self.s[self.i] == "}":
                break
            p = self.pair()
            if p:
                pairs[p[0]] = p[1]
            self.skip_ws()
            self.literal(";")
            self.skip_ws()
        return pairs

    def simple_selector(self, text):
        # e.g. div.note#id  or  .cls  or  #id  or  *
        parts = []
        token = ""
        i = 0
        while i < len(text):
            c = text[i]
            if c in ".#" and token:
                parts.append(token)
                token = c
            elif c in ".#":
                token = c
            else:
                token += c
            i += 1
        if token:
            parts.append(token)

        simples = []
        for part in parts:
            if part.startswith("#"):
                simples.append(IdSelector(part[1:]))
            elif part.startswith("."):
                simples.append(ClassSelector(part[1:]))
            else:
                simples.append(TagSelector(part.lower()))
        if len(simples) == 1:
            return simples[0]
        return CompoundSelector(simples)

    def selector(self, text):
        # Handle descendant combinators (whitespace between simple selectors).
        tokens = text.split()
        tokens = [t for t in tokens if t and t != ">"]  # treat > as descendant
        if not tokens:
            return None
        result = self.simple_selector(tokens[0])
        for tok in tokens[1:]:
            result = DescendantSelector(result, self.simple_selector(tok))
        return result

    def parse(self):
        """Return a list of (selector, declarations) rules."""
        rules = []
        while self.i < len(self.s):
            self.skip_ws()
            if self.i >= len(self.s):
                break
            # @-rules: skip @media { ... } but keep inner rules for @media all/screen.
            if self.s[self.i] == "@":
                self._handle_at_rule(rules)
                continue
            # Read selector text up to '{'.
            start = self.i
            while self.i < len(self.s) and self.s[self.i] not in "{}":
                self.i += 1
            if self.i >= len(self.s) or self.s[self.i] == "}":
                self.i += 1
                continue
            sel_text = self.s[start:self.i].strip()
            self.literal("{")
            decls = self.body()
            self.literal("}")
            for one in sel_text.split(","):
                sel = self.selector(one.strip())
                if sel is not None:
                    rules.append((sel, decls))
        return rules

    def _handle_at_rule(self, rules):
        # Find the at-rule keyword.
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in "{;":
            self.i += 1
        prelude = self.s[start:self.i]
        keyword = prelude.split()[0].lower() if prelude.split() else ""
        if self.i < len(self.s) and self.s[self.i] == ";":
            self.i += 1  # @import/@charset etc.
            return
        # It's a block at-rule.
        self.literal("{")
        if keyword in ("@media", "@supports"):
            # Naively include the inner rules regardless of the query.
            inner = CSSParser(self._read_block())
            rules.extend(inner.parse())
        else:
            self._read_block()  # skip @font-face, @keyframes, etc.

    def _read_block(self):
        depth = 1
        start = self.i
        while self.i < len(self.s) and depth > 0:
            if self.s[self.i] == "{":
                depth += 1
            elif self.s[self.i] == "}":
                depth -= 1
                if depth == 0:
                    break
            self.i += 1
        block = self.s[start:self.i]
        self.literal("}")
        return block


def parse_inline(style_text):
    parser = CSSParser("{" + style_text + "}")
    parser.literal("{")
    return parser.body()


def cascade_priority(rule):
    selector, _body = rule
    return selector.priority


def style(node, rules, _sorted_rules=None):
    """Compute the `.style` dict for `node` and its subtree.

    Rules are sorted by cascade priority exactly once (at the root call) and
    reused for every node, rather than re-sorting per node.
    """
    if _sorted_rules is None:
        _sorted_rules = sorted(rules, key=cascade_priority)

    node.style = {}

    # 1. Inherited properties from parent (or defaults at root).
    for prop, default in INHERITED_PROPERTIES.items():
        if node.parent and prop in node.parent.style:
            node.style[prop] = node.parent.style[prop]
        else:
            node.style[prop] = default

    # 2. Author + UA rules, in cascade order.
    for selector, body in _sorted_rules:
        if not selector.matches(node):
            continue
        for prop, value in body.items():
            node.style[prop] = value

    # 3. Inline style attribute (highest, aside from !important which we ignore).
    if isinstance(node, Element) and "style" in node.attributes:
        for prop, value in parse_inline(node.attributes["style"]).items():
            node.style[prop] = value

    # 4. Resolve relative font sizes (percent / em) against the parent.
    _resolve_font_size(node)

    for child in node.children:
        style(child, rules, _sorted_rules)


def _resolve_font_size(node):
    if "font-size" not in node.style:
        return
    value = node.style["font-size"]
    parent_size = 16.0
    if node.parent and "font-size" in node.parent.style:
        ps = node.parent.style["font-size"]
        if ps.endswith("px"):
            try:
                parent_size = float(ps[:-2])
            except ValueError:
                pass
    if value.endswith("%"):
        try:
            node.style["font-size"] = f"{parent_size * float(value[:-1]) / 100:.1f}px"
        except ValueError:
            node.style["font-size"] = f"{parent_size}px"
    elif value.endswith("em"):
        try:
            node.style["font-size"] = f"{parent_size * float(value[:-2]):.1f}px"
        except ValueError:
            node.style["font-size"] = f"{parent_size}px"
    elif value in ("smaller",):
        node.style["font-size"] = f"{parent_size * 0.8:.1f}px"
    elif value in ("larger",):
        node.style["font-size"] = f"{parent_size * 1.2:.1f}px"
