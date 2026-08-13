"""word-count toe: prove the on_load and extra_css hooks.

After every navigation this toe parses the raw HTML, counts the words, and
injects a small status line into the body before the browser ever parses it.
A bit of injected CSS keeps the status line looking like it belongs.
"""

import re


def activate(ctx):
    ctx.on("on_load", on_load)
    ctx.on("extra_css", extra_css)


def on_load(url, body):
    words = re.findall(r"[A-Za-z0-9]+", body)
    if not words:
        return None
    line = (f'<div class="toe-word-count">'
            f'Toes counted {len(words)} words on this page.</div>')
    return body.replace("</body>", line + "</body>", 1)


def extra_css(url):
    return """
.toe-word-count {
  margin: 14px 8px;
  padding: 6px 10px;
  border: 1px dashed #888;
  color: #666;
  font-size: 13px;
  font-style: italic;
}
"""
