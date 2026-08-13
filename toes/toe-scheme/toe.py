"""toe-scheme toe: prove the handle hook by registering a custom scheme.

Intercepts any toe:// navigation and renders a real page from its own
feet. toe://hello says hi, toe://gallery lists every installed toe, and
anything else gets a friendly 404. Pages come back as ordinary HTML and
flow through the normal pipeline, so toe:// links are clickable, view-source
works, and history behaves like any other page.
"""


def activate(ctx):
    ctx.on("handle", handle)


def handle(url, tab):
    if url.scheme != "toe":
        return None
    if url.host == "hello":
        return {}, _hello(), "text/html"
    if url.host == "gallery":
        toes = tab.browser.toes
        rows = "".join(
            f"<li><b>{toe.name}</b> <span class=v>{toe.version}</span> "
            f"— {toe.description or 'no description'}</li>"
            for toe in toes)
        return {}, _gallery(rows), "text/html"
    # Only claim hosts this toe owns; other toes may handle toe:// hosts.
    return None


def _hello():
    return """
<!doctype html>
<html><head><title>toe://hello</title>
<style>
  body { font-family: Helvetica; margin: 60px; color: #222; }
  h1 { color: #1a73e8; font-size: 40px; }
  .sub { color: #666; }
  a { color: #1a73e8; }
</style></head>
<body>
  <h1>toe://hello</h1>
  <p class="sub">This page was rendered by a toe. Type toe://gallery to see
  every toe currently gripping this browser.</p>
  <p><a href="toe://gallery">toe://gallery</a></p>
</body></html>
"""


def _gallery(rows):
    return f"""
<!doctype html>
<html><head><title>toe://gallery</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: #222; }}
  h1 {{ color: #1a73e8; }}
  .v {{ color: #888; font-size: 13px; }}
  a {{ color: #1a73e8; }}
</style></head>
<body>
  <h1>The toe gallery</h1>
  <ul>
    {rows}
  </ul>
  <p><a href="toe://hello">toe://hello</a> ·
     <a href="toe://toebar">the Toe Bar</a> ·
     <a href="toe://sock">the Sock Detective</a></p>
</body></html>
"""
