"""toe-bar toe: a wide, cluttered, early-2000s toolbar.

The Toe Bar is a chrome band drawn above the tabs, in the grand tradition
of 2003 toolbars: a scrolling marquee, rotating banner ads, a hit counter,
a web ring, and popup windows. It is built entirely on the toe framework —
it declares a chrome band, paints it, handles its own clicks, and serves
its own pages. The browser core only provides the generic band/popup
capabilities.

Pages served (via the handle hook):
    toe://toebar            settings: bar on/off, popup blocker, counter
    toe://ad/<n>            the ad landing pages (spawned as popups)
"""

import html as _html

from feetbrowser import toes
from feetbrowser.layout import get_font

BAND_HEIGHT = 30
BAR_ID = "toe-bar"

# The rotating banner ads. Each is (label, ad page number).
ADS = [
    ("FOOT FETISH.NET — 100% FREE TOE PICKS", 1),
    ("DOWNLOAD MORE RAM NOW", 2),
    ("YOU ARE THE 1,000,000TH VISITOR", 3),
    ("WORLD'S BEST FOOT SITE", 4),
]

MARQUEE = ("WELCOME TO THE TOE BAR ★ YOU'VE GOT TOES ★ "
           "CLICK THE ADS OR THE TOES GET IT ★ BEST VIEWED IN 800x600 ★ "
           "MADE WITH NOTEPAD ★ ")

RING = ["toe://gallery", "toe://hello", "toe://sock", "toe://toebar"]

SETTINGS_STYLE = """
  body { font-family: Courier; margin: 40px; background: #fdf6e3; color: #222; }
  h1 { color: #8b0000; letter-spacing: 2px; }
  .k { color: #8b0000; }
  .v { color: #1a73e8; }
  .box { border: 1px solid #bbb; background: #fff; padding: 6px 10px; margin: 4px 0; }
  a { color: #8b0000; }
"""


def activate(ctx):
    ctx.on("chrome_bands", lambda: [(BAR_ID, BAND_HEIGHT)])
    ctx.on("on_chrome_draw", lambda canvas, bands: _draw_band(ctx, canvas, bands))
    ctx.on("on_chrome_click", lambda x, y, bands: _band_click(ctx, x, y, bands))
    ctx.on("buttons", lambda: [toes.ButtonDef("toebar", "TB", "Toe Bar")])
    ctx.on("on_click", lambda btn_id: _toolbar_click(ctx, btn_id))
    ctx.on("handle", lambda url, tab: _handle(ctx, url, tab))
    ctx.on("on_new_tab", lambda: _maybe_popup(ctx))
    ctx.settings.setdefault("bar_on", True)
    ctx.settings.setdefault("popup_blocker", False)
    ctx.settings.setdefault("visitor", 0)
    ctx.settings.setdefault("ring_pos", 0)
    ctx.settings.setdefault("ad_index", 0)
    ctx.settings.setdefault("marquee_pos", 0)
    ctx.settings.setdefault("navs_since_popup", 0)
    ctx.save_settings()


# -- the band -------------------------------------------------------------


def _draw_band(ctx, canvas, bands):
    if not ctx.settings.get("bar_on", True):
        return
    band = next((b for b in bands if b[0] == BAR_ID), None)
    if band is None:
        return
    _id, height, y = band
    w = canvas.winfo_width()
    # Retro chrome background: beveled gray.
    canvas.create_rectangle(0, y, w, y + height, fill="#c0c0c0", width=0)
    canvas.create_line(0, y, w, y, fill="#ffffff")
    canvas.create_line(0, y + height - 1, w, y + height - 1, fill="#808080")

    x = 4
    # TOE BAR toggle button.
    x = _bevel_button(canvas, x, y, 64, "TOE BAR", _toggle_band, ctx)
    # Marquee.
    x = _marquee(canvas, x, y, ctx)
    # Banner ad.
    x = _banner(canvas, x, y, ctx)
    # Hit counter.
    x = _hit_counter(canvas, x, y, ctx)
    # Web ring.
    _web_ring(canvas, x, y, ctx)


def _bevel_button(canvas, x, y, w, label, action, ctx):
    h = BAND_HEIGHT - 6
    canvas.create_rectangle(x, y + 2, x + w, y + 2 + h,
                            fill="#d4d0c8", outline="#000")
    canvas.create_line(x, y + 2, x + w, y + 2, fill="#ffffff")
    canvas.create_line(x, y + 2, x, y + 2 + h, fill="#ffffff")
    canvas.create_line(x + w, y + 2, x + w, y + 2 + h, fill="#808080")
    canvas.create_line(x, y + 2 + h, x + w, y + 2 + h, fill="#808080")
    canvas.create_text(x + w // 2, y + 2 + h // 2, text=label,
                       font=get_font(9, "bold", "roman", "Helvetica"),
                       fill="#000")
    return x + w + 4


def _marquee(canvas, x, y, ctx):
    w = 300
    canvas.create_rectangle(x, y + 2, x + w, y + BAND_HEIGHT - 2,
                            fill="#000000", outline="#000")
    pos = ctx.settings.get("marquee_pos", 0)
    text = MARQUEE * 3
    canvas.create_text(x + 4 - pos, y + BAND_HEIGHT // 2, text=text,
                       anchor="w", font=get_font(9, "normal", "roman",
                                                 "Helvetica"), fill="#00ff00")
    ctx.settings["marquee_pos"] = (pos + 1) % (len(MARQUEE) * 2)
    _schedule_redraw(ctx)
    return x + w + 4


def _banner(canvas, x, y, ctx):
    w = 240
    label, _n = ADS[ctx.settings.get("ad_index", 0) % len(ADS)]
    canvas.create_rectangle(x, y + 2, x + w, y + BAND_HEIGHT - 2,
                            fill="#ffff00", outline="#000")
    canvas.create_text(x + w // 2, y + BAND_HEIGHT // 2, text=label,
                       font=get_font(9, "bold", "roman", "Helvetica"),
                       fill="#000")
    return x + w + 4


def _hit_counter(canvas, x, y, ctx):
    n = ctx.settings.get("visitor", 0)
    label = f"YOU ARE VISITOR #{n:06d}"
    canvas.create_rectangle(x, y + 2, x + 150, y + BAND_HEIGHT - 2,
                            fill="#000000", outline="#000")
    canvas.create_text(x + 75, y + BAND_HEIGHT // 2, text=label,
                       font=get_font(9, "normal", "roman", "Helvetica"),
                       fill="#00ff00")
    return x + 154


def _web_ring(canvas, x, y, ctx):
    label = "← PREV | TOE RING | NEXT →"
    canvas.create_rectangle(x, y + 2, x + 180, y + BAND_HEIGHT - 2,
                            fill="#d4d0c8", outline="#000")
    canvas.create_text(x + 90, y + BAND_HEIGHT // 2, text=label,
                       font=get_font(9, "bold", "roman", "Helvetica"),
                       fill="#000")


def _schedule_redraw(ctx):
    browser = ctx.browser
    if getattr(ctx, "_redraw_scheduled", False):
        return
    ctx._redraw_scheduled = True
    try:
        browser.window.after(120, lambda: _redraw(ctx))
    except Exception:
        pass


def _redraw(ctx):
    ctx._redraw_scheduled = False
    try:
        ctx.browser.draw()
    except Exception:
        pass


# -- band clicks ----------------------------------------------------------


def _band_click(ctx, x, y, bands):
    if not ctx.settings.get("bar_on", True):
        return False
    band = next((b for b in bands if b[0] == BAR_ID), None)
    if band is None:
        return False
    _id, height, by = band
    if not (by <= y < by + height):
        return False
    # TOE BAR button.
    if 4 <= x < 68:
        _toggle_band(ctx)
        return True
    # Banner ad.
    if 376 <= x < 616:
        _spawn_ad(ctx)
        return True
    # Web ring.
    if 774 <= x < 954:
        _ring_hop(ctx)
        return True
    return False


def _toggle_band(ctx):
    ctx.settings["bar_on"] = not ctx.settings.get("bar_on", True)
    ctx.save_settings()
    ctx.set_status("The Toe Bar has been toggled. "
                   "The 2000s called; they want their toolbar back."
                   if ctx.settings["bar_on"] else
                   "The Toe Bar is gone. The 2000s are disappointed.")
    ctx.browser.draw()


def _spawn_ad(ctx):
    if ctx.settings.get("popup_blocker", False):
        ctx.set_status("Popup blocked. That's not very 2003 of you.")
        return
    n = ctx.settings.get("ad_index", 0) % len(ADS)
    ctx.settings["ad_index"] = n + 1
    ctx.save_settings()
    ctx.popup(f"toe://ad/{n + 1}", 320, 240)


def _ring_hop(ctx):
    pos = ctx.settings.get("ring_pos", 0)
    ctx.settings["ring_pos"] = (pos + 1) % len(RING)
    ctx.save_settings()
    ctx.open(RING[ctx.settings["ring_pos"]])


def _toolbar_click(ctx, btn_id):
    if btn_id == "toebar":
        ctx.open("toe://toebar")


# -- popups ---------------------------------------------------------------


def _maybe_popup(ctx):
    if not ctx.settings.get("bar_on", True):
        return
    if ctx.settings.get("popup_blocker", False):
        return
    ctx.settings["navs_since_popup"] = ctx.settings.get("navs_since_popup", 0) + 1
    if ctx.settings["navs_since_popup"] >= 10:
        ctx.settings["navs_since_popup"] = 0
        ctx.save_settings()
        ctx.popup("toe://ad/youve-got-toes", 300, 200)


# -- pages ----------------------------------------------------------------


def _handle(ctx, url, tab):
    if url.scheme != "toe":
        return None
    if url.host == "ad":
        n = (url.path or "/").lstrip("/") or "1"
        return {}, _ad_page(ctx, n), "text/html"
    if url.host != "toebar":
        return None
    path = (url.path or "/").rstrip("/") or "/"
    if path == "/":
        return {}, _settings_page(ctx), "text/html"
    if path.startswith("/toggle/"):
        key = path[len("/toggle/"):]
        if key == "bar":
            ctx.settings["bar_on"] = not ctx.settings.get("bar_on", True)
            ctx.save_settings()
        if key == "popups":
            ctx.settings["popup_blocker"] = not ctx.settings.get(
                "popup_blocker", False)
            ctx.save_settings()
        if key == "reset-counter":
            ctx.settings["visitor"] = 0
            ctx.save_settings()
        return {}, _settings_page(ctx), "text/html"
    return {}, _settings_page(ctx), "text/html"


def _settings_page(ctx):
    bar = "ON" if ctx.settings.get("bar_on", True) else "OFF"
    blocker = "ON" if ctx.settings.get("popup_blocker", False) else "OFF"
    visitor = ctx.settings.get("visitor", 0)
    return f"""<!doctype html>
<html><head><title>Toe Bar</title><style>{SETTINGS_STYLE}</style></head>
<body>
<h1>THE TOE BAR</h1>
<p class="k">A 2003 toolbar for a 2026 browser. Best viewed in 800x600.</p>
<div class="box"><span class="k">TOE BAR</span>: <span class="v">{bar}</span>
 — <a href="toe://toebar/toggle/bar">toggle</a></div>
<div class="box"><span class="k">POPUP BLOCKER</span>: <span class="v">{blocker}</span>
 — <a href="toe://toebar/toggle/popups">toggle</a>
 <span class="k">(blocking popups is not very 2003 of you)</span></div>
<div class="box"><span class="k">VISITOR NUMBER</span>: <span class="v">#{visitor:06d}</span>
 — <a href="toe://toebar/toggle/reset-counter">reset</a></div>
<div class="box"><span class="k">THE ADS</span>: <a href="toe://ad/1">ad 1</a> ·
<a href="toe://ad/2">ad 2</a> · <a href="toe://ad/3">ad 3</a> ·
<a href="toe://ad/4">ad 4</a></div>
<div class="box"><span class="k">THE RING</span>: <a href="toe://gallery">gallery</a> ·
<a href="toe://hello">hello</a> · <a href="toe://sock">sock</a></div>
</body></html>
"""


def _ad_page(ctx, n):
    if n == "youve-got-toes":
        return _youve_got_toes()
    try:
        idx = int(n) - 1
    except ValueError:
        idx = 0
    label, _ = ADS[idx % len(ADS)]
    return f"""<!doctype html>
<html><head><title>AD {n}</title><style>{SETTINGS_STYLE}</style></head>
<body>
<h1>★ {_html.escape(label)} ★</h1>
<p class="k">This ad was brought to you by the Toe Bar, a toe that
clutters your screen like it's 2003.</p>
<div class="box"><a href="popup:close">CLOSE</a> ·
<a href="popup:spawn:toe://ad/{idx + 2}">MORE FREE TOES</a></div>
<div class="box">Visit the sponsor: <a href="toe://gallery">the toe gallery</a>
 · <a href="toe://sock">the Sock Detective</a> ·
<a href="toe://hello">toe://hello</a></div>
</body></html>
"""


def _youve_got_toes():
    return """<!doctype html>
<html><head><title>YOU'VE GOT TOES!</title><style>%s</style></head>
<body>
<h1>YOU'VE GOT TOES!</h1>
<p class="k">Congratulations. Your browser is now wearing a full set of
extensions, and they are all gripping the page.</p>
<div class="box"><a href="popup:close">OK</a> ·
<a href="popup:spawn:toe://ad/1">CLICK HERE FOR FREE TOES</a></div>
</body></html>
""" % SETTINGS_STYLE
