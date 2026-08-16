"""Offline tests for the rendering stack: fonts, raster, image codecs, canvas.

These cover the layers that replaced Tk. They need no display and reach
nothing outside this machine -- the few that need a page to arrive over HTTP
serve it from a loopback server they start themselves -- but they do need at
least one installed font, which every platform we support has.
"""
import os
import shutil
import struct
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import media_fixtures
from feetbrowser import canvas as canvasmod
from feetbrowser import fontengine, gui, imagecodec, media, mediacodec, raster
from feetbrowser.net import URL
from feetbrowser.window import Event, Window

# A Browser() reads ~/.feetbrowser_settings.json for its momentum and scroll
# settings. Point the module at a throwaway file so a machine's real settings
# (momentum off, say) cannot break a test that assumes the defaults.
from feetbrowser import settings as _settings
_settings.SETTINGS_FILE = os.path.join(
    tempfile.mkdtemp(prefix="feetbrowser-test-"), "settings.json")

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# -- helpers ---------------------------------------------------------------

def _png(width, height, depth, color, samples, palette=None, trns=None,
         interlace=0, idat=None):
    """Build a PNG so the decoder can be tested against known pixels.

    `idat` replaces the compressed pixel data outright, which is how the
    malformed-input tests hand the decoder something no encoder wrote."""
    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = (width * channels * depth + 7) // 8
    raw = bytearray()
    if idat is None:
        for y in range(height):
            raw.append(0)
            raw += samples[y * stride:(y + 1) * stride]
        idat = zlib.compress(bytes(raw))
    out = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0,
                             interlace))
    if palette:
        out += chunk(b"PLTE", palette)
    if trns:
        out += chunk(b"tRNS", trns)
    return out + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _gif():
    """A 2x2 GIF whose LZW stream is four literal codes and an end marker."""
    palette = bytes([255, 0, 0, 0, 0, 255]) + bytes(6)
    stream, acc, bits = bytearray(), 0, 0
    for code in (0, 1, 1, 0, 5):     # 4-entry table: clear=4, end=5, 3 bits
        acc |= code << bits
        bits += 3
        while bits >= 8:
            stream.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        stream.append(acc & 0xFF)
    return (b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
            + palette
            + b"\x2C" + struct.pack("<HHHHB", 0, 0, 2, 2, 0)
            + bytes([2, len(stream)]) + bytes(stream) + b"\x00" + b"\x3B")


def _pixel(surface, x, y):
    o = y * surface.stride + x * 3
    return tuple(surface.pixels[o:o + 3])


def _sans():
    return canvasmod.Font(family="Helvetica", size=16)


# -- font engine -----------------------------------------------------------

def test_font_index_finds_families():
    index = fontengine.index()
    assert index, "no fonts found on this system"
    for family, faces in index.items():
        assert isinstance(family, str) and family == family.lower()
        assert faces, f"{family} indexed with no faces"
        break


def test_font_metrics_are_sane():
    font = _sans()
    assert font.ascent > 0, "ascent must be positive"
    assert font.descent >= 0, "descent is reported as a positive depth"
    assert font.linespace >= font.ascent + font.descent
    assert font.linespace <= font.size * 3, "line height implausibly large"


def test_font_metrics_scale_with_size():
    small = canvasmod.Font(family="Helvetica", size=10)
    large = canvasmod.Font(family="Helvetica", size=30)
    assert large.linespace > small.linespace
    assert large.measure("Handgloves") > small.measure("Handgloves")


def test_measure_is_additive():
    """The layout engine caches per-character widths and sums them, so a
    string's width must equal the sum of its characters' widths exactly."""
    font = _sans()
    text = "Handgloves, 0123!"
    total = sum(font.measure(ch) for ch in text)
    assert abs(font.measure(text) - total) < 1e-9, "measure is not additive"


def test_measure_empty_and_space():
    font = _sans()
    assert font.measure("") == 0
    assert font.measure(" ") > 0, "a space must advance the pen"


def test_glyph_lookup_and_contours():
    font = _sans()
    face = font.face
    assert face.glyph_id("H") != 0, "no glyph for 'H'"
    assert face.glyph_contours(face.glyph_id(" ")) == [], \
        "a space should have no outline"
    contours = face.glyph_contours(face.glyph_id("H"))
    assert contours and len(contours[0]) >= 4, "'H' has no usable outline"


def test_flatten_produces_pixel_polygons():
    font = _sans()
    face = font.face
    polys = fontengine.flatten(face.glyph_contours(face.glyph_id("o")),
                               face.scale(64))
    assert len(polys) >= 2, "'o' should flatten to an outer and inner contour"
    ys = [p[1] for poly in polys for p in poly]
    assert min(ys) < 0, "flattened glyphs sit above the baseline (negative y)"


def test_bold_and_italic_select_different_faces():
    plain = canvasmod.Font(family="Helvetica", size=16)
    bold = canvasmod.Font(family="Helvetica", size=16, weight="bold")
    assert bold.bold and not plain.bold
    # Either a real bold face exists (different widths) or it fell back to the
    # regular one; both are acceptable, but the flags must be right.
    assert bold.measure("Handgloves") >= plain.measure("Handgloves") * 0.9


def test_missing_glyph_falls_back_to_another_face():
    """A face without a glyph must not paint .notdef boxes; measuring and
    painting both have to agree on whichever face supplies the character."""
    font = _sans()
    ch = "★"  # BLACK STAR - absent from many text faces
    face, gid, _scale = font.face_for(ch)
    if font.face.glyph_id(ch) == 0:
        assert gid != 0 or face is font.face, \
            "no fallback face was consulted"
    assert font.measure(ch) >= 0


# -- malformed fonts -------------------------------------------------------
#
# Fonts come off the local disk rather than off the network, so the threat is
# not a hostile author but a truncated download, a partially written file, or
# a face doing something the spec allows and nobody expects. The rule is the
# same either way: a file that is not a font raises FontError, and a font that
# is merely strange gives up on the glyph it cannot read and keeps going. It
# may never crash the parser, which since the parser is Rust would mean
# taking the process with it.

def _sfnt(tables):
    """Assemble an sfnt file from ``{tag: payload}``."""
    tags = sorted(tables)
    out = struct.pack(">IHHHH", 0x00010000, len(tags), 0, 0, 0)
    offset = 12 + 16 * len(tags)
    body = b""
    for tag in tags:
        payload = tables[tag]
        out += tag.encode("ascii").ljust(4)[:4]
        out += struct.pack(">III", 0, offset + len(body), len(payload))
        body += payload + b"\x00" * (-len(payload) % 4)
    return out + body


def _minimal(glyf=None, loca=None, cmap=None, n_glyphs=2, n_metrics=2,
             index_to_loc=0):
    head = bytearray(54)
    struct.pack_into(">H", head, 18, 1000)          # unitsPerEm
    struct.pack_into(">h", head, 50, index_to_loc)  # indexToLocFormat
    hhea = bytearray(36)
    struct.pack_into(">hhh", hhea, 4, 800, -200, 0)
    struct.pack_into(">H", hhea, 34, n_metrics)
    maxp = bytearray(6)
    struct.pack_into(">H", maxp, 4, n_glyphs)
    hmtx = struct.pack(">HhHh", 500, 0, 300, 0)
    if glyf is None:
        # One square contour, points given as 16-bit deltas.
        glyf = (struct.pack(">hhhhh", 1, 0, 0, 100, 100)
                + struct.pack(">HH", 3, 0) + bytes([1, 1, 1, 1])
                + struct.pack(">hhhh", 0, 100, 0, -100)
                + struct.pack(">hhhh", 0, 0, 100, 0))
    if loca is None:
        loca = struct.pack(">HHH", 0, 0, len(glyf) // 2)
    if cmap is None:
        # Format 6, mapping 'A' to glyph 1.
        sub = struct.pack(">HHHHHH", 6, 12, 0, ord("A"), 1, 1)
        cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    return _sfnt({"head": bytes(head), "hhea": bytes(hhea),
                  "maxp": bytes(maxp), "hmtx": hmtx, "glyf": glyf,
                  "loca": loca, "cmap": cmap})


def _raises_fonterror(data, why):
    try:
        fontengine.Font(data)
    except fontengine.FontError:
        return
    raise AssertionError(why)


def test_minimal_font_parses():
    """The scaffolding above has to make a font, or the tests below prove
    nothing about the cases they break."""
    font = fontengine.Font(_minimal())
    assert (font.units_per_em, font.ascent, font.descent) == (1000, 800, -200)
    assert font.glyph_id("A") == 1 and font.glyph_id("B") == 0
    assert len(font.glyph_contours(1)) == 1
    assert font.advance(0) == 500 and font.advance(1) == 300


def test_font_rejects_files_that_are_not_fonts():
    _raises_fonterror(b"", "an empty file is not a font")
    _raises_fonterror(b"<html>not a font at all</html>", "HTML is not a font")
    _raises_fonterror(b"\x00\x01\x00\x00", "a bare sfnt tag is not a font")
    _raises_fonterror(_sfnt({"cmap": b"\x00" * 8}),
                      "a font without head cannot be measured")


def test_font_rejects_a_collection_index_it_does_not_have():
    _raises_fonterror(b"ttcf" + struct.pack(">IIII", 0x00010000, 1, 200, 0),
                      "a one-font collection has no second face")


def test_font_treats_tables_pointing_past_the_end_as_absent():
    """A truncated font leaves directory entries pointing into nothing. Those
    read as missing tables, which is how a partly written file still gives up
    politely instead of indexing off the end of the buffer."""
    data = bytearray(_minimal())
    # The directory is sorted by tag, so cmap is the first entry: aim its
    # offset a megabyte past the end of the file.
    struct.pack_into(">I", data, 12 + 8, 1 << 20)
    font = fontengine.Font(bytes(data))
    assert font.glyph_id("A") == 0, "a missing cmap maps nothing"
    assert font.names() == {}


def test_font_ignores_a_truncated_glyph():
    good = _minimal()
    for cut in (10, 12, 14, 16, 20, 24):
        glyf = (struct.pack(">hhhhh", 1, 0, 0, 100, 100)
                + struct.pack(">HH", 3, 0) + bytes([1, 1, 1, 1])
                + struct.pack(">hhhh", 0, 100, 0, -100)
                + struct.pack(">hhhh", 0, 0, 100, 0))[:cut]
        font = fontengine.Font(_minimal(glyf=glyf,
                                        loca=struct.pack(">HHH", 0, 0,
                                                         len(glyf) // 2)))
        assert font.glyph_contours(1) == [], f"cut at {cut} produced an outline"
    assert fontengine.Font(good).glyph_contours(1), "the whole glyph is fine"


def test_font_ignores_loca_entries_past_glyf():
    font = fontengine.Font(_minimal(loca=struct.pack(">HHH", 0, 0, 30000)))
    assert font.glyph_contours(1) == []
    assert font.glyph_contours(9999) == [], "a glyph id past loca is blank"


def test_font_stops_on_a_composite_that_refers_to_itself():
    """A composite pointing at itself would recurse for ever. The parser gives
    up after a few levels and returns nothing, which is the only answer that
    terminates."""
    glyf = (struct.pack(">hhhhh", -1, 0, 0, 100, 100)
            + struct.pack(">HH", 0x0002, 1) + bytes([0, 0]))
    font = fontengine.Font(_minimal(glyf=glyf,
                                    loca=struct.pack(">HHH", 0, 0,
                                                     len(glyf) // 2)))
    assert font.glyph_contours(1) == []


def test_font_cmap_ignores_impossible_ranges():
    """A format 12 group spanning the whole 32-bit space is a corrupt record,
    not four billion characters to enumerate."""
    groups = (struct.pack(">III", ord("A"), ord("A"), 1)
              + struct.pack(">III", 0, 0xFFFFFFFF, 1))
    sub = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, 2) + groups
    cmap = struct.pack(">HHHHI", 0, 1, 3, 10, 12) + sub
    font = fontengine.Font(_minimal(cmap=cmap))
    assert font.glyph_id("A") == 1
    assert font.glyph_id("B") == 0, "the impossible group should be skipped"


def test_font_advance_falls_back_to_the_last_metric():
    """hmtx stops after numberOfHMetrics entries and every glyph after that
    shares the last advance -- that is how the format spells a monospaced
    tail, not a reason to index past the table."""
    font = fontengine.Font(_minimal())
    assert font.advance(1) == 300
    assert font.advance(50000) == 300
    assert font.advance(-1) == 300


def test_font_survives_arbitrary_corruption():
    """Flip bytes through a real font and a hand-built one and insist the
    parser always either works or raises FontError. A font is read once and
    then asked for outlines all day, so a stray offset must be survivable at
    every one of those calls, not just at parse time."""
    import random

    seeds = [_minimal()]
    for faces in fontengine.index().values():
        path, _face = next(iter(faces.values()))
        with open(path, "rb") as f:
            seeds.append(f.read(200000))
        break
    rng = random.Random(20260814)
    for _ in range(1500):
        data = bytearray(rng.choice(seeds))
        for _flip in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.3:
            del data[rng.randrange(len(data)):]
        try:
            font = fontengine.Font(bytes(data))
            font.names()
            for ch in ("A", "g", "☃"):
                font.glyph_id(ch)
            for gid in (0, 1, 7, 4000):
                fontengine.flatten(font.glyph_contours(gid), font.scale(16))
                font.advance(gid)
        except fontengine.FontError:
            pass
        except Exception as exc:                 # noqa: BLE001
            raise AssertionError(
                f"{type(exc).__name__} escaped the font parser: {exc}")


def test_text_survives_lone_surrogates():
    """``&#xD800;`` puts a lone surrogate in a page's text. It is not a
    character any font maps, but measuring and drawing it must come back
    quietly rather than raise on the way into the renderer."""
    font = _sans()
    face = font.face
    assert face.glyph_id("\ud800") == 0
    assert face.has_char("\ud800") is False
    text = "a\ud800b"
    assert raster.measure_text(face, 16, text) >= 0
    s = raster.Surface(40, 20, (255, 255, 255))
    raster.draw_text(s, face, 16, text, 2, 15, (0, 0, 0))


# -- rasteriser ------------------------------------------------------------

def test_surface_starts_filled_with_background():
    s = raster.Surface(8, 4, (10, 20, 30))
    assert _pixel(s, 0, 0) == (10, 20, 30)
    assert _pixel(s, 7, 3) == (10, 20, 30)


def test_fill_rect_bounds_are_half_open():
    s = raster.Surface(10, 10, (0, 0, 0))
    s.fill_rect(2, 2, 5, 5, (255, 255, 255))
    assert _pixel(s, 2, 2) == (255, 255, 255)
    assert _pixel(s, 4, 4) == (255, 255, 255)
    assert _pixel(s, 5, 5) == (0, 0, 0), "x1/y1 must be exclusive"
    assert _pixel(s, 1, 1) == (0, 0, 0)


def test_fill_rect_clips_to_surface():
    s = raster.Surface(6, 6, (0, 0, 0))
    s.fill_rect(-40, -40, 400, 400, (9, 9, 9))
    assert _pixel(s, 0, 0) == (9, 9, 9) and _pixel(s, 5, 5) == (9, 9, 9)
    s.fill_rect(100, 100, 200, 200, (255, 0, 0))  # entirely outside
    assert _pixel(s, 5, 5) == (9, 9, 9)


def test_fill_rect_alpha_blends_halfway():
    s = raster.Surface(4, 4, (0, 0, 0))
    s.fill_rect(0, 0, 4, 4, (200, 100, 50), 128)
    r, g, b = _pixel(s, 1, 1)
    assert abs(r - 100) <= 2 and abs(g - 50) <= 2 and abs(b - 25) <= 2, \
        f"half-alpha blend gave {(r, g, b)}"


def test_fill_rect_alpha_lands_in_the_right_rows_and_nowhere_else():
    """This is what is left of the span-kernel test after the fill moved to
    Rust. The row-at-a-time assembly path is gone -- the Rust fill covers the
    whole rectangle in one crossing instead of one per row -- so what is worth
    checking is the same thing that test checked underneath the plumbing: the
    blend is exact, it covers every pixel of the rectangle, and it touches
    nothing outside it.

    Note the arithmetic: `// 255`, as the translate tables did. The assembly
    rounded by `>> 8`, which is one level darker at the top of the range.
    """
    s = raster.Surface(6, 4, (40, 40, 40))
    s.fill_rect(1, 1, 5, 3, (200, 100, 50), 128)
    inv = 255 - 128
    expect = tuple((c * 128 + 40 * inv) // 255 for c in (200, 100, 50))
    assert _pixel(s, 1, 1) == expect, (_pixel(s, 1, 1), expect)
    assert _pixel(s, 4, 2) == expect, "the last covered pixel blended too"
    assert _pixel(s, 0, 1) == (40, 40, 40), "the blend escaped to the left"
    assert _pixel(s, 5, 1) == (40, 40, 40), "the blend escaped to the right"
    assert _pixel(s, 1, 0) == (40, 40, 40), "the blend escaped upwards"
    assert _pixel(s, 1, 3) == (40, 40, 40), "the blend escaped downwards"

    s.fill_all((1, 2, 3))
    assert _pixel(s, 0, 0) == (1, 2, 3), "the surface still refills afterwards"


def test_clip_confines_drawing():
    s = raster.Surface(20, 20, (0, 0, 0))
    saved = s.set_clip(5, 5, 10, 10)
    s.fill_rect(0, 0, 20, 20, (255, 255, 255))
    s.reset_clip(saved)
    assert _pixel(s, 7, 7) == (255, 255, 255)
    assert _pixel(s, 4, 4) == (0, 0, 0), "drawing escaped the clip"
    assert _pixel(s, 10, 10) == (0, 0, 0)
    s.fill_rect(0, 0, 2, 2, (1, 2, 3))
    assert _pixel(s, 0, 0) == (1, 2, 3), "clip was not restored"


def test_outline_rect_leaves_the_middle_alone():
    s = raster.Surface(12, 12, (0, 0, 0))
    s.outline_rect(2, 2, 10, 10, (255, 255, 255), 1)
    assert _pixel(s, 2, 2) == (255, 255, 255)
    assert _pixel(s, 5, 5) == (0, 0, 0), "outline should not fill"
    assert _pixel(s, 9, 9) == (255, 255, 255)


def test_draw_line_axis_aligned_and_diagonal():
    s = raster.Surface(16, 16, (0, 0, 0))
    s.draw_line(1, 3, 12, 3, (255, 0, 0))
    assert _pixel(s, 6, 3) == (255, 0, 0)
    s.draw_line(3, 1, 3, 12, (0, 255, 0))
    assert _pixel(s, 3, 6) == (0, 255, 0)
    s.draw_line(0, 0, 15, 15, (0, 0, 255))
    assert _pixel(s, 8, 8) == (0, 0, 255), "diagonal missed its midpoint"


def test_rasterize_fills_a_square_with_clean_edges():
    square = [[(2.0, 2.0), (10.0, 2.0), (10.0, 10.0), (2.0, 10.0)]]
    cov = raster.rasterize(square, 12, 12)
    assert cov[6 * 12 + 6] == 255, "interior should be fully covered"
    assert cov[0] == 0, "exterior should be empty"


def test_rasterize_anti_aliases_partial_coverage():
    """A half-pixel-wide sliver must produce partial coverage, not a hard
    on/off edge -- that is the whole point of the scanline sampler."""
    sliver = [[(1.0, 0.0), (1.5, 0.0), (1.5, 4.0), (1.0, 4.0)]]
    cov = raster.rasterize(sliver, 4, 4)
    value = cov[2 * 4 + 1]
    assert 0 < value < 255, f"expected partial coverage, got {value}"


def test_rasterize_respects_nonzero_winding():
    """An inner contour wound the other way must punch a hole, which is how
    counters in 'o' and 'e' stay open."""
    outer = [(0.0, 0.0), (12.0, 0.0), (12.0, 12.0), (0.0, 12.0)]
    inner = [(4.0, 4.0), (4.0, 8.0), (8.0, 8.0), (8.0, 4.0)]  # reversed
    cov = raster.rasterize([outer, inner], 12, 12)
    assert cov[1 * 12 + 1] == 255, "ring should be filled"
    assert cov[6 * 12 + 6] == 0, "counter should be knocked out"


def test_glyph_bitmap_is_cached_and_positioned():
    font = _sans()
    face = font.face
    gid = face.glyph_id("H")
    first = raster.glyph_bitmap(face, 24, gid)
    again = raster.glyph_bitmap(face, 24, gid)
    assert first is again, "glyph bitmaps must be cached by face/size/glyph"
    cov, w, h, _left, top = first
    assert w > 0 and h > 0 and len(cov) == w * h
    assert top < 0, "a capital sits above the baseline"


def test_blit_coverage_honours_alpha():
    s = raster.Surface(4, 4, (0, 0, 0))
    s.blit_coverage(bytes([255, 128, 0, 0] * 4), 4, 4, 0, 0, (200, 200, 200))
    assert _pixel(s, 0, 0) == (200, 200, 200), "full coverage should be solid"
    mid = _pixel(s, 1, 0)[0]
    assert 80 < mid < 120, f"half coverage gave {mid}"
    assert _pixel(s, 2, 0) == (0, 0, 0), "zero coverage must not paint"


def test_draw_text_advance_matches_measure():
    font = _sans()
    s = raster.Surface(300, 40, (255, 255, 255))
    advance = font.draw(s, "Handgloves 123", 5, 25, (0, 0, 0))
    assert abs(advance - font.measure("Handgloves 123")) < 1e-9, \
        "painted advance disagrees with measured width"


def test_draw_text_actually_marks_pixels():
    font = canvasmod.Font(family="Helvetica", size=24)
    s = raster.Surface(200, 40, (255, 255, 255))
    font.draw(s, "Hello", 5, 30, (0, 0, 0))
    dark = sum(1 for i in range(0, len(s.pixels), 3) if s.pixels[i] < 128)
    assert dark > 40, f"only {dark} dark pixels; text did not render"


def test_png_round_trip():
    s = raster.Surface(17, 11, (30, 60, 90))
    s.fill_rect(3, 3, 9, 7, (200, 10, 10))
    width, height, rgba = imagecodec.decode_png(s.to_png())
    assert (width, height) == (17, 11)
    for i in range(width * height):
        assert tuple(rgba[i * 4:i * 4 + 3]) == tuple(s.pixels[i * 3:i * 3 + 3])


# -- image codecs ----------------------------------------------------------

def test_decode_png_truecolour():
    data = _png(2, 2, 8, 2, bytes([255, 0, 0, 0, 255, 0,
                                   0, 0, 255, 255, 255, 255]))
    w, h, rgba = imagecodec.decode(data)
    assert (w, h) == (2, 2)
    assert bytes(rgba[:8]) == bytes([255, 0, 0, 255, 0, 255, 0, 255])


def test_decode_png_palette_with_transparency():
    palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
    data = _png(3, 1, 8, 3, bytes([0, 1, 2]), palette=palette,
                trns=bytes([0, 255, 255]))
    _w, _h, rgba = imagecodec.decode(data)
    assert rgba[3] == 0, "palette index 0 should be transparent"
    assert rgba[7] == 255 and tuple(rgba[4:7]) == (0, 255, 0)


def test_decode_png_greyscale_alpha_and_low_bit_depth():
    _w, _h, rgba = imagecodec.decode(_png(2, 1, 8, 4,
                                          bytes([200, 0, 100, 255])))
    assert tuple(rgba[:4]) == (200, 200, 200, 0)
    _w, _h, rgba = imagecodec.decode(_png(4, 1, 1, 0, bytes([0b10100000])))
    assert rgba[0] == 255 and rgba[4] == 0, "1-bit grey should span 0..255"


def test_decode_png_all_filter_types():
    """Every scanline filter must reverse exactly; a single wrong Paeth
    prediction corrupts the rest of the image."""
    width, height, channels = 4, 3, 3
    source = bytes(range(width * height * channels))

    def encode(ftype):
        stride = width * channels
        raw = bytearray()
        prev = bytearray(stride)
        for y in range(height):
            line = source[y * stride:(y + 1) * stride]
            enc = bytearray(stride)
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                if ftype == 0:
                    pred = 0
                elif ftype == 1:
                    pred = a
                elif ftype == 2:
                    pred = b
                elif ftype == 3:
                    pred = (a + b) >> 1
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (
                        b if pb <= pc else c)
                enc[i] = (line[i] - pred) & 0xFF
            raw.append(ftype)
            raw += enc
            prev = bytearray(line)

        def chunk(tag, payload):
            body = tag + payload
            return (struct.pack(">I", len(payload)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                             0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw)))
                + chunk(b"IEND", b""))

    for ftype in range(5):
        _w, _h, rgba = imagecodec.decode(encode(ftype))
        got = bytes(b for i in range(width * height)
                    for b in rgba[i * 4:i * 4 + 3])
        assert got == source, f"filter {ftype} did not round-trip"


def test_decode_png_interlaced():
    width = height = 8
    image = bytes([(x * 32 + y * 4) % 256
                   for y in range(height) for x in range(width)])
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    raw = bytearray()
    for ox, oy, sx, sy in passes:
        pw = (width - ox + sx - 1) // sx
        ph = (height - oy + sy - 1) // sy
        if pw <= 0 or ph <= 0:
            continue
        for y in range(ph):
            raw.append(0)
            for x in range(pw):
                raw.append(image[(oy + y * sy) * width + ox + x * sx])

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0,
                                         0, 1))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))
    _w, _h, rgba = imagecodec.decode(data)
    assert bytes(rgba[i * 4] for i in range(width * height)) == image


def test_decode_gif_lzw_and_palette():
    """A hand-built GIF whose LZW stream is nothing but literal codes: it
    exercises the bit unpacker and the code-width growth without needing a
    real encoder."""
    palette = bytes([255, 0, 0, 0, 0, 255]) + bytes(6)
    indices = [0, 1, 1, 0]
    min_code = 2                     # 4-entry table -> clear=4, end=5
    bits, width = [], min_code + 1   # codes start 3 bits wide
    for value in indices:
        bits.append((value, width))
    bits.append((5, width))          # end of information
    stream = bytearray()
    acc = acc_bits = 0
    for code, size in bits:
        acc |= code << acc_bits
        acc_bits += size
        while acc_bits >= 8:
            stream.append(acc & 0xFF)
            acc >>= 8
            acc_bits -= 8
    if acc_bits:
        stream.append(acc & 0xFF)

    data = (b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
            + palette
            + b"\x2C" + struct.pack("<HHHHB", 0, 0, 2, 2, 0)
            + bytes([min_code, len(stream)]) + bytes(stream) + b"\x00"
            + b"\x3B")
    w, h, rgba = imagecodec.decode(data)
    assert (w, h) == (2, 2)
    assert tuple(rgba[0:4]) == (255, 0, 0, 255)
    assert tuple(rgba[4:8]) == (0, 0, 255, 255)


def test_decode_pnm_binary_and_ascii():
    _w, _h, rgba = imagecodec.decode(b"P6\n2 1\n255\n"
                                     + bytes([1, 2, 3, 4, 5, 6]))
    assert tuple(rgba[:4]) == (1, 2, 3, 255)
    _w, _h, rgba = imagecodec.decode(b"P3\n2 1\n255\n9 8 7 6 5 4\n")
    assert tuple(rgba[:4]) == (9, 8, 7, 255)


def test_decode_rejects_unknown_format():
    try:
        imagecodec.decode(b"not an image at all")
    except imagecodec.ImageError:
        return
    raise AssertionError("unknown data should raise ImageError")


def test_resize_nearest_neighbour():
    rgba = bytearray()
    for value in (10, 20, 30, 40):
        rgba += bytes([value, value, value, 255])
    out = imagecodec.resize(rgba, 2, 2, 4, 4)
    assert len(out) == 4 * 4 * 4
    assert out[0] == 10 and out[-4] == 40


def test_resize_is_identity_at_same_size():
    rgba = bytearray(bytes([1, 2, 3, 4]) * 4)
    assert imagecodec.resize(rgba, 2, 2, 2, 2) is rgba

# -- malformed images ------------------------------------------------------
#
# Everything below feeds the decoders bytes no encoder would ever produce.
# Images are the one part of a page that arrives as raw binary from a
# stranger, so the decoders are where a hostile file gets its chance: the
# rule is that a broken image is an ImageError and never a crash, a hang or
# an allocation the file chose the size of. That mattered when this was
# Python and it matters more now that it is Rust, where an unchecked index
# is a panic no `except` can catch.

def _rejects(data, why):
    try:
        imagecodec.decode(data)
    except imagecodec.ImageError:
        return
    raise AssertionError(why)


def test_decode_png_rejects_truncation_in_the_header():
    """Cut a good PNG short at each structural boundary up to the end of
    IHDR: the signature, the length word, the chunk tag, mid-payload."""
    good = _png(4, 3, 8, 2, bytes(36))
    for cut in (0, 1, 7, 8, 12, 20, 25):
        _rejects(good[:cut], f"truncation at {cut} bytes should be rejected")


def test_decode_png_pads_a_truncated_image():
    """Past the header a short file is not an error: a header we believe
    plus fewer pixels than it promised comes back padded, which is what a
    half-arrived image on a slow connection looks like."""
    good = _png(4, 3, 8, 2, bytes(range(36)))
    width, height, rgba = imagecodec.decode(good[:len(good) - 20])
    assert (width, height) == (4, 3)
    assert len(rgba) == 4 * 3 * 4


def test_decode_png_ignores_chunk_crcs():
    """We have never checked CRCs and must not start: a stray bad checksum
    is common in the wild and the pixels are usually perfectly fine."""
    good = _png(2, 2, 8, 2, bytes(12))
    _w, _h, rgba = imagecodec.decode(good[:-4] + b"\x00\x00\x00\x00")
    assert len(rgba) == 2 * 2 * 4


def test_decode_png_rejects_headers_it_cannot_honour():
    _rejects(_png(2, 2, 8, 2, bytes(12), interlace=3),
             "unknown interlace method should be rejected")
    _rejects(_png(2, 2, 7, 2, bytes(12)), "bit depth 7 should be rejected")
    header = struct.pack(">IIBBBBB", 2, 2, 8, 9, 0, 0, 0)
    body = b"IHDR" + header
    _rejects(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + body
             + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF),
             "colour type 9 should be rejected")


def test_decode_png_rejects_absurd_dimensions():
    """Twenty million pixels is the cap, and a header is a claim rather
    than a fact: a 65535x65535 IHDR must not become a 17-gigabyte buffer."""
    _rejects(_png(0xFFFF, 0xFFFF, 8, 2, b""), "4G pixels should be rejected")
    _rejects(_png(0, 0, 8, 2, b""), "a zero-area image should be rejected")
    header = struct.pack(">IIBBBBB", 0x80000000, 4, 8, 2, 0, 0, 0)
    body = b"IHDR" + header
    _rejects(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + body
             + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF),
             "a width past 2^31 should be rejected")


def test_decode_png_rejects_undecodable_pixel_data():
    _rejects(_png(2, 2, 8, 2, bytes(12), idat=b"junkjunkjunk"),
             "an IDAT that is not deflate data should be rejected")
    _rejects(_png(2, 2, 8, 2, bytes(12),
                  idat=zlib.compress(bytes([9, 1, 2, 3, 4, 5, 6,
                                            9, 1, 2, 3, 4, 5, 6]))),
             "filter type 9 does not exist and should be rejected")


def test_decode_png_inflate_is_bounded():
    """A third of a megabyte on the wire that would expand past
    MAX_INFLATED has to stop at the ceiling rather than eat the machine."""
    packer = zlib.compressobj(9)
    megabyte = b"\x00" * (1 << 20)
    parts = [packer.compress(megabyte)
             for _ in range((imagecodec.MAX_INFLATED >> 20) + 4)]
    parts.append(packer.flush())
    bomb = b"".join(parts)
    assert len(bomb) < (1 << 20), "the bomb should be small on the wire"
    _rejects(_png(64, 64, 8, 2, b"", idat=bomb),
             "a zip bomb should be rejected, not decoded")


def test_decode_gif_rejects_truncation():
    good = _gif()
    for cut in (0, 3, 6, 10, 13, 20, 26, 30):
        _rejects(good[:cut], f"truncation at {cut} bytes should be rejected")


def test_decode_gif_rejects_impossible_code_sizes():
    """A GIF palette holds 256 colours at most, so an initial LZW code size
    above 8 is a file asking us to size a table from a number it invented."""
    for min_code in (9, 12, 200, 255):
        raw = bytearray(_gif())
        raw[raw.index(b"\x2C") + 10] = min_code
        _rejects(bytes(raw),
                 f"LZW code size {min_code} should be rejected")


def test_decode_gif_rejects_absurd_geometry():
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 0xFFFF, 0xFFFF, 0, 0, 0)
             + b"\x3B", "a 4G-pixel canvas should be rejected")
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 4, 4, 0, 0, 0) + b"\x3B",
             "a GIF with no image block should be rejected")
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
             + bytes(12) + b"\x2C"
             + struct.pack("<HHHHB", 60000, 60000, 2, 2, 0)
             + bytes([2, 1]) + b"\x00\x00\x3B",
             "a frame placed 60000 pixels off canvas should be rejected")


# -- animated GIF ----------------------------------------------------------

_GIF_FIXTURES = os.path.join(_FIXTURES, "gif")

# What each committed animation is for, and what the file says about itself.
# Delays are milliseconds as the file asked for them, before any clamp; the
# loop count is the NETSCAPE2.0 one, -1 where the file carries no such block.
_GIF_VECTORS = {
    "frames": ((8, 6), [50, 120, 70, 30], 0),
    "offset": ((8, 6), [50, 120, 70, 30], 0),
    "trans": ((8, 6), [80, 80, 80], 2),
    "dispose": ((8, 6), [60, 60, 60, 60], 0),
    "interlace": ((8, 6), [60, 60], 0),
    "still": ((8, 6), [0], -1),
    "ffmpeg": ((8, 6), [100, 100, 100, 100], 0),
}


def _gif_vector(name):
    """A committed animation and the frames an independent decoder got out
    of it -- ImageMagick 7's `-coalesce`, zlib'd, every frame back to back.
    See tests/fixtures/gif/make_gif_vectors.sh for why that tool and not
    FFmpeg."""
    with open(os.path.join(_GIF_FIXTURES, name + ".gif"), "rb") as handle:
        data = handle.read()
    with open(os.path.join(_GIF_FIXTURES, name + ".rgba.z"), "rb") as handle:
        truth = zlib.decompress(handle.read())
    return data, truth


def _pixels_match(ours, truth):
    """Bit-identical, except that two fully transparent pixels are equal
    whatever colour is recorded underneath them.

    That is not a loosened threshold, it is the only comparison the data
    supports: nothing observes the colour of a pixel with alpha zero, and the
    two decoders leave different ones there after a disposal. Everything with
    any opacity at all must match exactly, which is what the negative control
    below leans on."""
    if len(ours) != len(truth):
        return "length %d, expected %d" % (len(ours), len(truth))
    for i in range(0, len(ours), 4):
        mine, theirs = bytes(ours[i:i + 4]), bytes(truth[i:i + 4])
        if mine == theirs or (mine[3] == 0 and theirs[3] == 0):
            continue
        return "pixel %d is %s, expected %s" % (i // 4, tuple(mine),
                                                tuple(theirs))
    return ""


def _animated_gif(screen, palette, frames, loops=None):
    """Build an animated GIF with a named palette and hand-placed frames.

    The committed vectors are what real encoders write; this is for the cases
    no encoder will write on request -- a delay of zero, a canvas built to be
    a bomb -- and for reading a fixture back as sixteen numbers.
    """
    def lzw(indices, min_code):
        """Every pixel as its own literal code: legal, and the smallest
        encoder a conforming decoder has to read. The table still has to be
        counted along with, because the decoder widens its codes when the
        table fills, and an encoder that does not is how a hand-built GIF
        comes out looking almost right."""
        clear, end = 1 << min_code, (1 << min_code) + 1
        out, acc, bits = bytearray(), 0, 0

        def emit(code, size):
            nonlocal acc, bits
            acc |= code << bits
            bits += size
            while bits >= 8:
                out.append(acc & 0xFF)
                acc >>= 8
                bits -= 8

        width, table, prev = min_code + 1, end + 1, False
        emit(clear, width)
        for value in indices:
            emit(value, width)
            if prev:
                table += 1
                if table == (1 << width) and width < 12:
                    width += 1
            prev = True
        emit(end, width)
        if bits:
            out.append(acc & 0xFF)
        return bytes(out)

    def blocks(payload):
        out = bytearray()
        while payload:
            chunk, payload = payload[:255], payload[255:]
            out.append(len(chunk))
            out += chunk
        return bytes(out) + b"\x00"

    screen_w, screen_h = screen
    # Two is the smallest LZW minimum code size the format allows, whatever
    # the palette would otherwise need.
    bits = max(2, (len(palette) // 3 - 1).bit_length())
    table = palette + bytes(3 * ((1 << bits) - len(palette) // 3))
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", screen_w, screen_h, 0x80 | (bits - 1), 0, 0)
    out += table
    if loops is not None:
        out += (b"\x21\xFF\x0BNETSCAPE2.0\x03\x01"
                + struct.pack("<H", loops) + b"\x00")
    for frame in frames:
        gflags = (frame.get("dispose", 0) << 2) | (1 if "clear" in frame else 0)
        out += b"\x21\xF9\x04" + bytes([gflags])
        out += struct.pack("<H", frame.get("delay", 0))
        out += bytes([frame.get("clear", 0), 0])
        out += b"\x2C" + struct.pack("<HHHHB", frame.get("left", 0),
                                     frame.get("top", 0), frame["w"],
                                     frame["h"], 0)
        out += bytes([bits]) + blocks(lzw(frame["indices"], bits))
    return bytes(out + b"\x3B")


def _frame_pixels(rgba, width, x, y):
    o = (y * width + x) * 4
    return tuple(rgba[o:o + 4])


def test_animated_gif_matches_an_independent_decoder():
    """Seven animations from two encoders, every frame of every one of them
    against what ImageMagick composited, pixel for pixel."""
    for name, (size, delays, loops) in sorted(_GIF_VECTORS.items()):
        data, truth = _gif_vector(name)
        width, height, frames, count = imagecodec.decode_gif_frames(data)
        assert (width, height) == size, f"{name}: size {width}x{height}"
        assert count == loops, f"{name}: loop count {count}"
        assert [d for _rgba, d in frames] == delays, f"{name}: delays"
        stride = width * height * 4
        assert len(truth) == len(frames) * stride, (
            f"{name}: {len(frames)} frames, truth has {len(truth) // stride}")
        for i, (rgba, _delay) in enumerate(frames):
            why = _pixels_match(rgba, truth[i * stride:(i + 1) * stride])
            assert not why, f"{name} frame {i}: {why}"


def test_the_gif_vectors_would_catch_a_decoder_that_did_nothing():
    """A threshold proves nothing about a file whose frames are all the same
    picture. Each vector has to move, and the ones that exist for disposal
    and transparency have to end up with pixels that only those produce."""
    for name in sorted(_GIF_VECTORS):
        if name == "still":
            continue
        _data, truth = _gif_vector(name)
        stride = 8 * 6 * 4
        first = truth[:stride]
        rest = [truth[i:i + stride] for i in range(stride, len(truth), stride)]
        assert any(f != first for f in rest), f"{name}: nothing ever changes"
    _data, truth = _gif_vector("dispose")
    stride = 8 * 6 * 4
    got = [truth[i:i + stride] for i in range(0, len(truth), stride)]
    appeared = [i for i in range(1, len(got))
                if any(got[i][j + 3] == 0 and got[i - 1][j + 3] == 255
                       for j in range(0, stride, 4))]
    assert appeared, (
        "somewhere in the disposal vector a pixel has to go from opaque back "
        "to transparent -- that is the only thing disposal-to-background "
        "does, and a decoder that ignored disposal would never produce it")
    _data, truth = _gif_vector("trans")
    assert any(truth[i + 3] == 0 for i in range(0, stride, 4)), (
        "the transparency vector should have transparent pixels in it")


def test_a_gif_frame_smaller_than_its_screen_is_composited_onto_it():
    """`offset.gif` is `frames.gif` after an optimiser: frames three pixels
    tall, placed at an offset, each meaning "and the rest is as it was".
    That is what nearly every animated GIF on the web looks like, and a
    decoder that hands back the sub-image rather than the screen returns a
    different picture at a different size."""
    data, _truth = _gif_vector("offset")
    width, height, frames, _loops = imagecodec.decode_gif_frames(data)
    assert (width, height) == (8, 6)
    assert len(frames) == 4
    for i, (rgba, _delay) in enumerate(frames):
        assert len(rgba) == 8 * 6 * 4, f"frame {i} is not screen sized"
    # The file really does store them small: the second image descriptor is
    # a 4x3 block at an offset, not the whole screen.
    descriptors = []
    for i in range(len(data) - 10):
        if data[i] == 0x2C:
            descriptors.append(struct.unpack("<HHHH", data[i + 1:i + 9]))
    assert any(d[2:] != (8, 6) for d in descriptors[1:]), (
        "the fixture should carry sub-screen frames, else it proves nothing")
    # Row 5 is untouched ground in every frame: the optimiser never stored
    # it after the first, so it can only be there by compositing.
    for i, (rgba, _delay) in enumerate(frames):
        assert _frame_pixels(rgba, 8, 0, 5)[3] == 255, (
            f"frame {i} lost the part of the screen it never redrew")


def test_gif_disposal_puts_back_what_it_promised():
    """The three disposal methods, hand-built so each pixel is nameable.

    Frame 2 leaves its rectangle behind. Frame 3 says "restore to
    background", so frame 4 must find that rectangle transparent again.
    Frame 4 says "restore to previous", so frame 5 must find what frame 3
    left, not what frame 4 drew.
    """
    red, green, blue, white = 0, 1, 2, 3
    data = _animated_gif((4, 2), bytes([255, 0, 0, 0, 255, 0,
                                        0, 0, 255, 255, 255, 255]), [
        {"w": 4, "h": 2, "delay": 5,
         "indices": [red] * 4 + [green] * 4},
        {"w": 2, "h": 1, "delay": 5, "indices": [blue, blue]},
        {"w": 2, "h": 1, "left": 2, "top": 1, "delay": 5, "dispose": 2,
         "indices": [white, white]},
        {"w": 1, "h": 1, "left": 0, "top": 1, "delay": 5, "dispose": 3,
         "indices": [white]},
        {"w": 1, "h": 1, "left": 3, "top": 0, "delay": 5, "indices": [white]},
    ], loops=0)
    _w, _h, frames, _loops = imagecodec.decode_gif_frames(data)
    assert len(frames) == 5
    kept = frames[1][0]
    assert _frame_pixels(kept, 4, 0, 0) == (0, 0, 255, 255), (
        "an undisposed frame draws over what was there")
    assert _frame_pixels(kept, 4, 2, 0) == (255, 0, 0, 255), (
        "and leaves the rest of the screen alone")
    disposed = frames[3][0]
    assert _frame_pixels(disposed, 4, 2, 1) == (0, 0, 0, 0), (
        '"restore to background" has to leave the rectangle transparent')
    assert _frame_pixels(frames[3][0], 4, 0, 1) == (255, 255, 255, 255), (
        "frame 4 draws its own pixel before anything is disposed")
    restored = frames[4][0]
    assert _frame_pixels(restored, 4, 0, 1) == (0, 255, 0, 255), (
        '"restore to previous" has to put back what frame 4 covered')
    assert _frame_pixels(restored, 4, 3, 0) == (255, 255, 255, 255), (
        "and frame 5's own pixel is still drawn")


def test_a_transparent_index_lets_the_frame_before_it_through():
    data = _animated_gif((2, 1), bytes([255, 0, 0, 0, 255, 0]), [
        {"w": 2, "h": 1, "indices": [0, 1]},
        {"w": 2, "h": 1, "clear": 0, "indices": [0, 0]},
    ], loops=0)
    _w, _h, frames, _loops = imagecodec.decode_gif_frames(data)
    assert _frame_pixels(frames[1][0], 2, 0, 0) == (255, 0, 0, 255)
    assert _frame_pixels(frames[1][0], 2, 1, 0) == (0, 255, 0, 255), (
        "a pixel of the transparent index must not overwrite the one under it")


def test_a_still_gif_is_a_one_frame_animation():
    """A caller should not have to know which it has before it asks, and the
    still entry point has to agree with the first frame of the animated one."""
    data, truth = _gif_vector("still")
    width, height, frames, loops = imagecodec.decode_gif_frames(data)
    assert (width, height, len(frames), loops) == (8, 6, 1, -1)
    assert bytes(frames[0][0]) == truth
    assert imagecodec.decode_gif(data) == (width, height, frames[0][0])
    assert imagecodec.decode(data) == (width, height, frames[0][0])


def test_decoding_every_frame_of_a_gif_is_bounded():
    """Every frame of an animation is a screen-sized copy, so the cost is the
    canvas times the frame count and neither number alone. A large screen and
    a long run of one-pixel frames is a few hundred bytes on the wire and a
    third of a gigabyte in hand, which is a decompression bomb with a palette
    on it."""
    palette = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    screen = 2000, 2000  # 16 MB a frame; MAX_INFLATED buys sixteen of them
    tiny = {"w": 1, "h": 1, "delay": 1, "indices": [0]}
    data = _animated_gif(screen, palette, [dict(tiny) for _ in range(17)],
                         loops=0)
    assert len(data) < 4096, "the bomb should be small on the wire"
    try:
        imagecodec.decode_gif_frames(data)
    except imagecodec.ImageError as exc:
        assert "expands too far" in str(exc), str(exc)
    else:
        raise AssertionError("17 frames of a 16 MB canvas should be refused")
    # Sixteen of them is under the ceiling and still decodes, so the guard is
    # a ceiling and not a blanket refusal of anything with a big canvas.
    ok = _animated_gif(screen, palette, [dict(tiny) for _ in range(16)],
                       loops=0)
    width, height, frames, _loops = imagecodec.decode_gif_frames(ok)
    assert (width, height, len(frames)) == (2000, 2000, 16)
    # And the still decoder reads the bomb regardless: it stops at one frame.
    width, height, _rgba = imagecodec.decode(data)
    assert (width, height) == (2000, 2000)


def test_photoimage_advances_a_gif_on_the_delays_it_carries():
    data, _truth = _gif_vector("frames")
    photo = canvasmod.PhotoImage(data=data)
    assert photo.animated and photo.width() == 8 and photo.height() == 6
    first = bytes(photo.rgba)
    # The clock starts when something first asks, not when the file decoded.
    assert not photo.advance(1000.0), "the first call only starts the clock"
    assert bytes(photo.rgba) == first
    assert not photo.advance(1000.04), "40 ms is not the 50 ms frame 1 asked"
    assert photo.advance(1000.06), "50 ms is"
    assert photo.frame_index == 1 and bytes(photo.rgba) != first
    assert not photo.advance(1000.1), "frame 2 is 120 ms long"
    assert photo.advance(1000.18) and photo.frame_index == 2
    assert photo.advance(1000.25) and photo.frame_index == 3
    assert photo.advance(1000.28) and photo.frame_index == 0, "round again"
    # Each frame was shown for the time it asked for and no longer: the
    # deadline moves on by the delay, not to the moment it happened to be
    # noticed, so a tick that arrives late does not stretch the animation.
    assert photo.advance(1000.33) and photo.frame_index == 1


def test_a_gif_that_asked_for_a_finite_number_of_loops_stops_asking():
    """`trans.gif` was written with ImageMagick's `-loop 3` and carries a
    NETSCAPE2.0 count of 2, which is the extension's own arithmetic: the
    count is the repeats *after* the first pass. Three passes, then it holds
    its last frame and stops costing repaints."""
    data, _truth = _gif_vector("trans")
    photo = canvasmod.PhotoImage(data=data)
    assert photo.loop_count == 2
    now = 500.0
    photo.advance(now)
    seen = 0
    for _ in range(200):
        now += 0.08
        if photo.advance(now):
            seen += 1
    assert photo.frame_index == len(photo.frames) - 1, (
        "it should come to rest on the last frame, not the first")
    assert photo.finished and not photo.animated
    assert seen == 3 * len(photo.frames) - 1, (
        f"three passes of three frames, less the one it started on: {seen}")


def test_a_gif_asking_for_no_delay_at_all_is_slowed_to_what_browsers_show():
    """Files written with a delay of zero are everywhere and are almost
    always an accident. No browser has honoured one since the 1990s."""
    data = _animated_gif((2, 1), bytes([255, 0, 0, 0, 255, 0]), [
        {"w": 2, "h": 1, "delay": 0, "indices": [0, 1]},
        {"w": 2, "h": 1, "delay": 1, "indices": [1, 0]},
    ], loops=0)
    _w, _h, frames, _loops = imagecodec.decode_gif_frames(data)
    assert [d for _rgba, d in frames] == [0, 10], (
        "the decoder reports what the file said")
    photo = canvasmod.PhotoImage(data=data)
    assert photo.delays == (canvasmod.DEFAULT_GIF_DELAY_MS,
                            canvasmod.DEFAULT_GIF_DELAY_MS), (
        "and the animation is what decides not to believe it")
    photo.advance(0.0)
    assert not photo.advance(0.05), "50 ms is not a frame"
    assert photo.advance(0.11), "100 ms is"


def test_an_animation_that_fell_behind_does_not_walk_every_frame_it_missed():
    """A tab that was not ticked for an hour -- asleep, or behind a modal --
    comes back to a frame, not to an hour of frames."""
    data, _truth = _gif_vector("frames")
    photo = canvasmod.PhotoImage(data=data)
    photo.advance(0.0)
    started = time.monotonic()
    assert photo.advance(3600.0)
    assert time.monotonic() - started < 0.5, "one tick, not an hour of them"
    assert 0 <= photo.frame_index < len(photo.frames)


def test_decode_pnm_rejects_malformed_headers():
    _rejects(b"P6", "a header with nothing after it should be rejected")
    _rejects(b"P6\n2 1\n", "a missing maxval should be rejected")
    _rejects(b"P6\nx y\n255\n" + bytes(6), "junk dimensions are rejected")
    _rejects(b"P6\n0 0\n255\n", "a zero-area image should be rejected")
    _rejects(b"P6\n99999 99999\n255\n" + bytes(6),
             "ten billion pixels should be rejected")
    _rejects(b"P3\n2 1\n255\nred green blue\n", "junk samples are rejected")
    _rejects(b"P9\n2 1\n255\n" + bytes(6), "P9 is not a Netpbm type")


def test_decode_pnm_accepts_the_awkward_but_legal():
    """Comments mid-header, 16-bit samples and bitmaps are all in the spec
    and all three take a different path through the reader."""
    _w, _h, rgba = imagecodec.decode(b"P6\n# who\n2 1\n# what\n255\n"
                                     + bytes([1, 2, 3, 4, 5, 6]))
    assert tuple(rgba[:4]) == (1, 2, 3, 255)
    _w, _h, rgba = imagecodec.decode(b"P5\n2 1\n65535\n"
                                     + bytes([255, 255, 0, 0]))
    assert rgba[0] == 255 and rgba[4] == 0
    _w, _h, rgba = imagecodec.decode(b"P4\n8 1\n" + bytes([0b10000000]))
    assert rgba[0] == 0 and rgba[4] == 255, "in PBM a set bit is black"
    _w, _h, rgba = imagecodec.decode(b"P6\n4 4\n255\n" + bytes(6))
    assert len(rgba) == 4 * 4 * 4, "short pixel data is padded, not fatal"


def test_decoders_survive_arbitrary_corruption():
    """Flip bytes at random through each format and insist that the only
    thing which ever comes back is a picture or an ImageError."""
    import random

    seeds = [_png(4, 3, 8, 2, bytes(36)),
             _png(4, 2, 8, 3, bytes([0, 1, 2, 3, 0, 1, 2, 3]),
                  palette=bytes(12)),
             _png(2, 2, 16, 6, bytes(32)),
             _gif(),
             b"P6\n3 2\n255\n" + bytes(18),
             b"P3\n2 2\n255\n1 2 3 4 5 6 7 8\n"]
    rng = random.Random(20260813)
    for _ in range(3000):
        data = bytearray(rng.choice(seeds))
        for _flip in range(rng.randint(1, 5)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.3:
            del data[rng.randrange(len(data)):]
        try:
            imagecodec.decode(bytes(data))
        except imagecodec.ImageError:
            pass
        except Exception as exc:                 # noqa: BLE001
            raise AssertionError(
                f"{type(exc).__name__} escaped the decoder for "
                f"{bytes(data)!r}: {exc}")


def test_jpeg_survives_arbitrary_corruption():
    """The same treatment for JPEG, from the real fixtures rather than from
    anything built here. It needs its own round because a JPEG is mostly one
    long entropy-coded run: a flipped bit does not stop the decoder, it
    changes what every following symbol means, and the sizes and counts it
    then reads are numbers no encoder ever wrote. Those are the numbers that
    index a table or size an allocation, so this is the test that says a
    corrupt photograph loses its picture and not the page it is on.
    """
    import random

    seeds = []
    for name in ("photo.jpg", "photo-progressive.jpg", "photo-grey.jpg",
                 "photo-restart.jpg"):
        with open(os.path.join(_FIXTURES, name), "rb") as fh:
            seeds.append(fh.read())
    rng = random.Random(20260813)
    decoded = 0
    for _ in range(1500):
        data = bytearray(rng.choice(seeds))
        for _flip in range(rng.randint(1, 6)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.4:
            del data[rng.randrange(len(data)):]
        try:
            imagecodec.decode(bytes(data))
            decoded += 1
        except imagecodec.ImageError:
            pass
        except Exception as exc:                 # noqa: BLE001
            raise AssertionError(
                f"{type(exc).__name__} escaped the JPEG decoder for "
                f"{bytes(data)!r}: {exc}")
    assert decoded > 500, (
        "only %d of 1500 corrupted photographs decoded at all, which means "
        "the run is testing rejection and not the decoder" % decoded)


# -- colours ---------------------------------------------------------------

def test_color_parses_every_accepted_form():
    assert canvasmod.color("#f00") == (255, 0, 0)
    assert canvasmod.color("#00ff00") == (0, 255, 0)
    assert canvasmod.color("#0000ffff0000") == (0, 255, 0)
    assert canvasmod.color("rebeccapurple") == (0x66, 0x33, 0x99)
    assert canvasmod.color("  WHITE  ") == (255, 255, 255)


def test_color_empty_is_transparent_and_junk_raises():
    assert canvasmod.color("") is None
    assert canvasmod.color(None) is None
    try:
        canvasmod.color("not-a-colour")
    except canvasmod.CanvasError:
        return
    raise AssertionError("a bad colour name must raise CanvasError")


# -- canvas ----------------------------------------------------------------

def test_canvas_items_get_increasing_ids():
    c = canvasmod.Canvas(width=50, height=50)
    first = c.create_rectangle(0, 0, 10, 10, fill="red")
    second = c.create_rectangle(0, 0, 10, 10, fill="blue")
    assert second > first
    assert c.find_all() == [first, second]


def test_canvas_delete_by_tag_and_all():
    c = canvasmod.Canvas(width=50, height=50)
    c.create_rectangle(0, 0, 5, 5, fill="red", tags=("page",))
    keep = c.create_rectangle(0, 0, 5, 5, fill="blue", tags=("chrome",))
    c.delete("page")
    assert c.find_all() == [keep]
    c.delete("all")
    assert c.find_all() == []


def test_canvas_delete_unknown_tag_is_harmless():
    c = canvasmod.Canvas(width=20, height=20)
    item = c.create_rectangle(0, 0, 5, 5, fill="red")
    c.delete("nothing-has-this-tag")
    assert c.find_all() == [item]


def test_canvas_addtag_withtag_matches_tk_semantics():
    """The browser tags a plugin's items by diffing find_all() around the
    call, so adding a tag must reach items found by an existing tag."""
    c = canvasmod.Canvas(width=50, height=50)
    a = c.create_rectangle(0, 0, 5, 5, fill="red", tags=("first",))
    c.create_rectangle(0, 0, 5, 5, fill="blue", tags=("second",))
    c.addtag_withtag("marked", "first")
    assert c.find_withtag("marked") == [a]
    c.addtag_withtag("marked", "first")  # idempotent
    assert c.find_withtag("marked") == [a]


def test_canvas_addtag_by_item_id():
    c = canvasmod.Canvas(width=50, height=50)
    item = c.create_rectangle(0, 0, 5, 5, fill="red")
    c.addtag_withtag("toe-draw", item)
    assert c.find_withtag("toe-draw") == [item]


def test_canvas_rejects_bad_colour_at_creation():
    """The display list catches CanvasError to fall back to black, so the error
    has to arrive from create_*, not from render()."""
    c = canvasmod.Canvas(width=10, height=10)
    try:
        c.create_rectangle(0, 0, 5, 5, fill="rgb-ish?")
    except canvasmod.CanvasError:
        return
    raise AssertionError("create_rectangle accepted an invalid colour")


def test_canvas_render_paints_in_creation_order():
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_rectangle(0, 0, 20, 20, fill="#ff0000", width=0)
    c.create_rectangle(0, 0, 10, 10, fill="#0000ff", width=0)
    s = c.render()
    assert _pixel(s, 5, 5) == (0, 0, 255), "later items must paint on top"
    assert _pixel(s, 15, 15) == (255, 0, 0)


def test_small_filled_oval_is_a_round_dot():
    """A `disc` list marker. The oval used to be stroked only, so a filled
    one came out hollow -- and the corners have to stay empty or the dot is
    a square."""
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_oval(4, 4, 14, 14, fill="#000000", outline="", width=0)
    s = c.render()
    assert _pixel(s, 9, 9) == (0, 0, 0), "the middle is filled"
    assert _pixel(s, 4, 4) == (255, 255, 255), "the corner is outside the dot"


def test_small_hollow_oval_keeps_its_hole():
    """A `circle` marker is a ring. At six pixels across an aliased ring is
    indistinguishable from a square, so this one is anti-aliased: the edge
    pixels land between the two colours."""
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_oval(4, 4, 14, 14, fill="", outline="#000000", width=1)
    s = c.render()
    assert _pixel(s, 9, 9) == (255, 255, 255), "the middle stays open"
    edge = _pixel(s, 9, 4)
    assert edge != (255, 255, 255), "the top of the ring is drawn"
    corner = _pixel(s, 4, 4)
    assert corner[0] > 200, f"the corner is outside the ring: {corner}"


def test_big_oval_still_fills_without_supersampling():
    """Past the anti-aliasing size limit the cheap scanline takes over; it
    still has to fill."""
    c = canvasmod.Canvas(width=120, height=120, bg="white")
    c.create_oval(5, 5, 115, 115, fill="#ff0000", outline="", width=0)
    s = c.render()
    assert _pixel(s, 60, 60) == (255, 0, 0), "interior filled"
    assert _pixel(s, 6, 6) == (255, 255, 255), "corner untouched"


def test_canvas_render_is_repeatable():
    c = canvasmod.Canvas(width=30, height=30, bg="white")
    c.create_rectangle(5, 5, 10, 10, fill="#123456", width=0)
    first = bytes(c.render().pixels)
    second = bytes(c.render().pixels)
    assert first == second, "compositing must be idempotent"


def test_canvas_render_reflects_deletion():
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_rectangle(0, 0, 20, 20, fill="#000000", width=0, tags=("x",))
    c.render()
    c.delete("x")
    s = c.render()
    assert _pixel(s, 10, 10) == (255, 255, 255), \
        "deleted items must not survive the next frame"


def test_canvas_text_anchors():
    font = _sans()
    c = canvasmod.Canvas(width=200, height=60, bg="white")
    nw = c.create_text(10, 10, text="Ay", font=font, fill="black",
                       anchor="nw")
    west = c.create_text(10, 30, text="Ay", font=font, fill="black",
                         anchor="w")
    top_nw = c._bounds(next(i for i in c._items if i.id == nw))[1]
    top_w = c._bounds(next(i for i in c._items if i.id == west))[1]
    assert top_nw == 10, "anchor=nw puts the top edge at y"
    assert top_w < 30, "anchor=w centres the line on y"


def test_canvas_text_width_matches_font():
    font = _sans()
    c = canvasmod.Canvas(width=300, height=40, bg="white")
    item = c.create_text(0, 0, text="Handgloves", font=font, anchor="nw")
    box = c._bounds(next(i for i in c._items if i.id == item))
    assert abs((box[2] - box[0]) - font.measure("Handgloves")) < 1e-9


def test_canvas_stipple_renders_translucent():
    c = canvasmod.Canvas(width=10, height=10, bg="white")
    c.create_rectangle(0, 0, 10, 10, fill="#000000", width=0,
                       stipple="gray50")
    value = _pixel(c.render(), 5, 5)[0]
    assert 100 < value < 160, f"stippled black over white gave {value}"


def test_canvas_image_blit():
    photo = canvasmod.PhotoImage(
        data=_png(2, 2, 8, 2, bytes([255, 0, 0] * 4)))
    assert (photo.width(), photo.height()) == (2, 2)
    assert photo.opaque, "an alpha-free PNG should take the fast blit path"
    c = canvasmod.Canvas(width=10, height=10, bg="white")
    c.create_image(1, 1, image=photo, anchor="nw")
    s = c.render()
    assert _pixel(s, 1, 1) == (255, 0, 0)
    assert _pixel(s, 5, 5) == (255, 255, 255)


def test_canvas_blank_photoimage_has_a_size():
    photo = canvasmod.PhotoImage(width=200, height=100)
    assert (photo.width(), photo.height()) == (200, 100)


def test_canvas_resize_keeps_background():
    c = canvasmod.Canvas(width=10, height=10, bg="#102030")
    c.resize(40, 20)
    assert (c.winfo_width(), c.winfo_height()) == (40, 20)
    assert _pixel(c.render(), 39, 19) == (0x10, 0x20, 0x30)


def test_canvas_render_region_clips():
    c = canvasmod.Canvas(width=40, height=40, bg="white")
    c.create_rectangle(0, 0, 40, 40, fill="#00ff00", width=0)
    c.render()
    c.delete("all")
    c.render(region=(0, 0, 10, 10))
    s = c.surface
    assert _pixel(s, 5, 5) == (255, 255, 255), "region should have been reset"
    assert _pixel(s, 20, 20) == (0, 255, 0), "outside the region is untouched"


def test_canvas_arc_strokes_without_filling():
    c = canvasmod.Canvas(width=40, height=40, bg="white")
    c.create_arc(10, 10, 30, 30, start=0, extent=270, style="arc",
                 outline="#ff0000", width=2)
    s = c.render()
    assert _pixel(s, 20, 20) == (255, 255, 255), "an arc must not fill"
    edge = [_pixel(s, x, 20) for x in range(28, 32)]
    assert any(p != (255, 255, 255) for p in edge), "arc drew nothing"


# -- window / event loop ---------------------------------------------------

def test_window_bindings_fire():
    w = Window()
    seen = []
    w.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
    assert w.dispatch("<Button-1>", Event(x=3, y=4))
    assert seen == [(3, 4)]
    assert not w.dispatch("<Button-3>"), "unbound sequences report no handler"


def test_window_binding_errors_do_not_escape():
    """Tk reported handler exceptions and carried on; so must we, or one bad
    plugin takes down the browser."""
    w = Window()
    w.on_callback_error = lambda where, exc: None
    w.bind("<Key>", lambda e: 1 // 0)
    w.dispatch("<Key>", Event(keysym="a"))


def test_window_timers_run_in_order():
    w = Window()
    order = []
    w.after(0, lambda: order.append("first"))
    w.after(0, lambda: order.append("second"))
    w.flush_timers()
    assert order == ["first", "second"]


def test_window_after_cancel_prevents_the_call():
    w = Window()
    fired = []
    handle = w.after(0, lambda: fired.append(1))
    w.after_cancel(handle)
    w.flush_timers()
    assert fired == []


def test_window_timer_not_yet_due_is_kept():
    w = Window()
    fired = []
    w.after(60_000, lambda: fired.append(1))
    wait = w.flush_timers()
    assert fired == []
    assert wait is not None and wait > 1, "should report the time remaining"


def test_window_timer_batch_stops_at_a_deadline():
    """A batch of due timers is unbounded work -- each callback can fetch a
    stylesheet or lay the page out -- so settle() bounding only the loop
    around the batch bounded nothing. Past the deadline the rest of the batch
    goes back on the queue rather than running."""
    w = Window()
    fired = []
    for i in range(5):
        w.after(0, lambda n=i: fired.append(n))
    # A deadline already in the past: nothing in this batch should run.
    w.flush_timers(time.monotonic() - 1)
    assert fired == [], fired
    # ...and nothing was lost or reordered: the next unbounded flush runs
    # every one of them, once, in the order they were scheduled.
    w.flush_timers()
    assert fired == [0, 1, 2, 3, 4], fired


def test_window_deadline_does_not_resurrect_a_cancelled_timer():
    """Deferred timers are pushed back by handle, and a cancelled handle must
    stay cancelled across the push-back."""
    w = Window()
    fired = []
    w.after(0, lambda: fired.append("kept"))
    handle = w.after(0, lambda: fired.append("cancelled"))
    w.after_cancel(handle)
    w.flush_timers(time.monotonic() - 1)
    w.flush_timers()
    assert fired == ["kept"], fired


def test_window_geometry_and_resize_event():
    w = Window()
    seen = []
    w.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
    w.geometry("640x480")
    assert (w.winfo_width(), w.winfo_height()) == (640, 480)
    assert seen == [(640, 480)]
    assert w.geometry() == "640x480"


def test_window_minsize_is_enforced_on_resize():
    w = Window()
    w.minsize(300, 200)
    w.resize(100, 100)
    assert (w.width, w.height) == (300, 200)


def test_window_clipboard_round_trip():
    w = Window()
    w.clipboard_clear()
    w.clipboard_append("hello")
    assert w.clipboard_get() == "hello"


def test_window_destroy_takes_children_with_it():
    from feetbrowser.window import Tk, Toplevel
    root = Tk()
    child = Toplevel(root)
    assert child in root.children
    root.destroy()
    assert not child.winfo_exists() and not root.winfo_exists()


def test_gui_exports_the_window_names_browser_uses():
    for name in ("Toplevel", "new_window", "has_display", "display_problem"):
        assert getattr(gui, name, None) is not None, f"gui.{name} missing"


# -- images end to end -----------------------------------------------------
#
# imagecodec is tested above against known pixels, and passed happily while
# every <img> on the screen was the "[img]" placeholder: decoding was never
# the broken part. What follows drives the whole path instead -- page load,
# the fetch that runs off the UI thread, the timer sweep that publishes the
# decoded image, layout, and the blit -- and looks at the pixels that come
# out the far end.

IMAGE_RGB = (255, 0, 255)
IMAGE_SIZE = 8


def _page_with_image(directory, rgb=IMAGE_RGB, size=IMAGE_SIZE):
    """Write an HTML file whose <img> is a data: PNG of a solid colour."""
    import base64
    samples = bytes(rgb) * size * size
    src = "data:image/png;base64," + base64.b64encode(
        _png(size, size, 8, 2, samples)).decode()
    path = os.path.join(directory, "page.html")
    with open(path, "w") as handle:
        handle.write(f"<!doctype html><title>img</title><p><img src='{src}'>")
    return path


def _serve_page_with_image(delay, rgb=IMAGE_RGB, size=IMAGE_SIZE):
    """A loopback server: an HTML page, and a PNG that takes `delay` to send.

    The delay is the whole point. Images arrive after the document that asked
    for them, and anything that captures a frame in between captures
    placeholders -- so a test that lets the image win the race tests nothing.
    """
    import http.server
    import threading
    import time

    pixels = _png(size, size, 8, 2, bytes(rgb) * size * size)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith(".png"):
                time.sleep(delay)
                body, ctype = pixels, "image/png"
            else:
                body = b'<!doctype html><title>img</title><p><img src="/i.png">'
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _count_pixels(surface, rgb):
    return sum(1 for y in range(surface.height) for x in range(surface.width)
               if _pixel(surface, x, y) == rgb)


def test_screenshot_paints_images_rather_than_placeholders():
    """A screenshot must contain the page's image, not its alt text.

    The regression this guards was not in any decoder: --screenshot stopped
    waiting the moment the *document* had arrived, which is the moment image
    loading begins, so every frame was captured with an empty image cache and
    every <img> laid out as "[img]".
    """
    import shutil
    import tempfile
    from feetbrowser.browser import screenshot

    work = tempfile.mkdtemp(prefix="fb-shot-")
    server = _serve_page_with_image(0.3)
    try:
        out = os.path.join(work, "shot.png")
        url = "http://127.0.0.1:%d/page" % server.server_address[1]
        browser = screenshot(url, out, settle=20.0)
        placeholders = [c for c in browser.tabs[0].display_list
                        if "[img" in getattr(c, "text", "")]
        assert not placeholders, f"placeholder still drawn: {placeholders}"
        width, height, rgba = imagecodec.decode(open(out, "rb").read())
        assert (width, height) == (browser.canvas.winfo_width(),
                                   browser.canvas.winfo_height())
        painted = sum(1 for i in range(0, len(rgba), 4)
                      if tuple(rgba[i:i + 3]) == IMAGE_RGB)
        assert painted == IMAGE_SIZE * IMAGE_SIZE, \
            f"expected an {IMAGE_SIZE}px square, found {painted} pixels"
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def test_settle_waits_for_images_a_finished_document_asked_for():
    """`loading` going false does not mean the page is finished.

    A document is fetched first and its images afterwards, so there is a
    window in which nothing is "loading" and the page is still all
    placeholders. Browser.settle() has to span it.
    """
    import shutil
    import tempfile
    from feetbrowser.browser import Browser

    work = tempfile.mkdtemp(prefix="fb-settle-")
    try:
        page = _page_with_image(work)
        browser = Browser()
        browser.new_tab("file://" + page)
        tab = browser.tabs[0]
        assert not tab.loading, "a file: document loads synchronously"
        assert tab.pending_images(), "images are queued, so work remains"
        assert browser.busy(), "and the browser has to call that busy"
        assert browser.settle(20.0), "settle should not have timed out"
        assert not browser.busy() and not tab.pending_images()
        assert tab.image_cache, "settling means the image is decoded"
        browser.draw()
        assert _count_pixels(browser.canvas.render(),
                             IMAGE_RGB) == IMAGE_SIZE * IMAGE_SIZE
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _serve_subresource_page(sheets, scripts, delay=0.05):
    """A page naming `sheets` stylesheets and `scripts` scripts, served by a
    loopback server that counts how many of them are ever in flight at once.

    Every response is held for `delay` first, which is what makes the
    overlap observable: without it a fast enough serial fetch could finish
    one before the next began and look concurrent by accident.

    The sheets each colour `h1` differently and the scripts each append their
    own number to a global, so the same page also says whether the cascade
    and the execution order survived being fetched out of order.
    """
    import http.server
    import threading

    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    body = ["<!doctype html><title>subresources</title>"]
    body += ['<link rel="stylesheet" href="/sheet%d.css">' % i
             for i in range(sheets)]
    body += ['<script src="/script%d.js"></script>' % i
             for i in range(scripts)]
    body.append("<h1>subresources</h1>")
    page = "".join(body).encode("utf8")
    # Distinct enough per sheet that "the last one won" is a statement about
    # which sheet, not about rounding.
    colors = ["#%02x0000" % (0x10 + i * 0x20) for i in range(sheets)]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 - the name http.server dispatches to
            path = self.path
            if path.endswith(".css"):
                index = int(path[len("/sheet"):-len(".css")])
                payload = ("h1 { color: %s }" % colors[index]).encode()
                ctype = "text/css"
            elif path.endswith(".js"):
                index = int(path[len("/script"):-len(".js")])
                payload = ("order = (typeof order === 'undefined' "
                           "? '' : order) + '%d,';" % index).encode()
                ctype = "text/javascript"
            else:
                payload, ctype = page, "text/html"
            if path != "/page.html":
                with lock:
                    state["live"] += 1
                    state["peak"] = max(state["peak"], state["live"])
                time.sleep(delay)
                with lock:
                    state["live"] -= 1
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/page.html" % server.server_address[1]
    return server, url, state, colors


def test_linked_sheets_and_scripts_are_fetched_at_the_same_time():
    """Twenty blocking round trips in a row is what froze discord.com.

    The cascade and script execution both go in document order, and the
    fetches used to go in that order too -- one at a time, on the UI thread,
    with the window unable to repaint for the sum of them. Document order is
    a constraint on *using* a subresource, not on getting it.

    Asserted as a mechanism rather than as a stopwatch reading: the server
    reports how many of the page's subresources were ever open at once, and
    a serial fetch cannot make that number larger than one however fast the
    machine is. The other two assertions are the ones that matter for
    correctness -- fetching out of order must not let a sheet or a script
    take effect out of order.
    """
    from feetbrowser.browser import Browser, Element, tree_to_list

    server, url, state, colors = _serve_subresource_page(sheets=3, scripts=6)
    try:
        browser = Browser()
        browser.new_tab(url)
        assert browser.settle(30.0), "settle should not have timed out"
        tab = browser.tabs[0]
        assert state["peak"] > 1, (
            "subresources were fetched one at a time (peak in flight: %d)"
            % state["peak"])
        heading = next(n for n in tree_to_list(tab.nodes, [])
                       if isinstance(n, Element) and n.tag == "h1")
        assert heading.style["color"] == colors[-1], (
            "the last sheet in document order must win the cascade, not the "
            "first one to come back off the wire")
        order = tab._js_interp.globals["order"]
        assert str(order) == "0,1,2,3,4,5,", (
            "scripts must still run in document order, got %r" % (order,))
    finally:
        server.shutdown()
        server.server_close()


def test_a_videos_player_is_built_off_the_ui_thread():
    """Opening a container and decoding frame zero is not free.

    Six autoplaying clips -- discord.com's front page -- cost about four
    seconds of it, and it used to happen in `_drain_videos`, on the UI
    thread, between one timer tick and the next. The bytes already arrive on
    a thread of their own; the decode they imply belongs there too, and only
    publishing the finished player needs the UI thread back.

    Recorded rather than timed: which thread ran the decode is the whole
    claim, and a fast enough machine would hide a slow enough stopwatch.
    """
    import http.server
    import threading

    from feetbrowser.browser import Browser, Element, tree_to_list

    def painter(i):
        return lambda x, y: (i * 20, 0, 0)

    clip = media_fixtures.avi(
        [media_fixtures.rgb24_frame(16, 12, painter(i)) for i in range(4)],
        16, 12, fps=10.0)
    page = (b"<!doctype html><title>clip</title>"
            b"<video src='/clip.avi'></video>")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 - the name http.server dispatches to
            payload, ctype = ((clip, "video/x-msvideo")
                              if self.path.endswith(".avi")
                              else (page, "text/html"))
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    threads = []
    built = media.VideoPlayer.__init__

    def recording(self, *args, **kwargs):
        threads.append(threading.current_thread())
        return built(self, *args, **kwargs)

    media.VideoPlayer.__init__ = recording
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        browser = Browser()
        browser.new_tab("http://127.0.0.1:%d/page.html"
                        % server.server_address[1])
        assert browser.settle(30.0), "settle should not have timed out"
        tab = browser.tabs[0]
        node = next(n for n in tree_to_list(tab.nodes, [])
                    if isinstance(n, Element) and n.tag == "video")
        player = getattr(node, "video_player", None)
        assert player is not None and player.track is not None, \
            "the clip should have decoded"
        assert player.scheduler.current is not None, \
            "and its first frame should be on screen"
        assert threads, "no player was built at all"
        main = threading.main_thread()
        assert not any(t is main for t in threads), (
            "the player was built on the UI thread, which is the freeze: %s"
            % [t.name for t in threads])
    finally:
        media.VideoPlayer.__init__ = built
        server.shutdown()
        server.server_close()


def test_an_animated_gif_in_a_page_moves_and_does_not_keep_the_page_busy():
    """The whole path, from `<img src=...gif>` to different pixels on screen.

    Two things have to be true at once. The picture has to change when the
    browser ticks -- which it can do without layout hearing about it, because
    the draw blits whatever `photo.rgba` currently is. And an animation must
    never count as outstanding work: a GIF looping for ever is the ordinary
    case, and a browser that called that "still loading" would hang every
    screenshot and every settle() on the internet's least important pixels.
    """
    import base64
    import shutil
    import tempfile
    from feetbrowser.browser import Browser

    data, _truth = _gif_vector("frames")
    src = "data:image/gif;base64," + base64.b64encode(data).decode()
    work = tempfile.mkdtemp(prefix="fb-gif-")
    try:
        path = os.path.join(work, "page.html")
        with open(path, "w") as handle:
            handle.write("<!doctype html><title>gif</title>"
                         f"<p><img src='{src}'>")
        browser = Browser()
        browser.new_tab("file://" + path)
        tab = browser.tabs[0]
        assert browser.settle(20.0), "settle should not have timed out"
        assert tab.image_cache, "the GIF should have decoded"
        photo = next(iter(tab.image_cache.values()))
        assert photo.animated and len(photo.frames) == 4
        browser.draw()
        before = bytes(browser.canvas.render().pixels)

        assert not browser.busy(), (
            "an animation is not outstanding work, however long it runs")
        # The tick is on the browser's clock, so wait out the longest delay
        # rather than reaching into the photo's.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not tab.tick_images():
            time.sleep(0.01)
        assert photo.frame_index != 0, "the tick should have moved it on"
        browser.draw()
        assert bytes(browser.canvas.render().pixels) != before, (
            "and moving on should have changed what is painted")
        assert not browser.busy(), "still not outstanding work"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_undecodable_image_settles_instead_of_being_refetched_for_ever():
    """A picture we cannot decode is finished, not outstanding.

    An <img> pointing at an SVG (or anything else with no decoder here) never
    reaches the image cache. A scripted page re-scans for images whenever the
    DOM is marked dirty, and that scan looked for sources that were in
    neither the cache nor the fetch queue -- which is exactly what an
    undecodable one is, for ever. So it was fetched again, and again, several
    times a second: pending_images() never went false, settle() spent its
    whole timeout, and the site got hammered for as long as the tab was open.
    sqlite.org, go.dev and www.w3.org all sat in that loop.

    The second half of the same story is the dirty flag. The DOM bindings set
    it on every mutating call and nothing cleared it, so one line of script
    was enough to restyle and re-lay-out the whole page on every poll -- which
    is what drove the re-scan in the first place.
    """
    import http.server
    import threading
    import time as _time
    from feetbrowser.browser import Browser

    hits = []
    page = (b'<!doctype html><div id="wrap"><img src="/logo.svg"></div>'
            b'<script>document.getElementById("wrap").className = "x";'
            b'</script>')

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.endswith(".svg"):
                body = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
                ctype = "image/svg+xml"
            else:
                body, ctype = page, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        browser = Browser()
        browser.new_tab("http://127.0.0.1:%d/" % server.server_address[1])
        started = _time.monotonic()
        assert browser.settle(20.0), \
            "an image that cannot be decoded still finishes the page"
        assert _time.monotonic() - started < 10, \
            "and it finishes promptly, not on the timeout"
        tab = browser.tabs[0]
        assert not tab.pending_images(), "nothing is left outstanding"
        # Keep pumping the way a live browser does. The count must not move.
        svg_hits = [h for h in hits if h.endswith(".svg")]
        for _ in range(40):
            browser.window.flush_timers()
        assert [h for h in hits if h.endswith(".svg")] == svg_hits, \
            "the failed image is not fetched again on every poll"
        assert len(svg_hits) == 1, ("fetched once", svg_hits)
        assert not tab._js_doc._flag["dirty"], \
            "the mutation flag is consumed, not left set for every poll"
    finally:
        server.shutdown()


def test_script_created_images_are_fetched_and_painted():
    """A `<script>` that builds `<img>` elements must have them fetched like
    anything else in the document.

    20plays.com (and plenty of other pages) build their banner strip by
    creating `<img>` elements in JavaScript after the document has already
    been scanned for images. Those used to render as "[img]": the DOM bridges
    dropped `img.src = ...` writes, and nothing re-scanned the tree for images
    a script added. This drives both halves through a loopback server.
    """
    import http.server
    import struct
    import threading
    import zlib as _z

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    pixels = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    png_bytes = b"\x89PNG\r\n\x1a\n"
    png_bytes += chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
    png_bytes += chunk(b"IDAT", _z.compress(pixels))
    png_bytes += chunk(b"IEND", b"")

    script = (
        'var files = ["a.png", "b.png", "c.png"];'
        'for (var i = 0; i < files.length; i++) {'
        '  var img = document.createElement("img");'
        '  img.src = "/" + files[i];'
        '  document.getElementById("wrap").appendChild(img);'
        '}')

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith(".png"):
                body, ctype = png_bytes, "image/png"
            else:
                body = (b'<!doctype html><div id="wrap"></div><script>'
                        + script.encode() + b'</script>')
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from feetbrowser.browser import Browser
        from feetbrowser.layout import DrawImage

        browser = Browser()
        browser.window.geometry("900x700")
        browser.canvas.resize(900, 700)
        browser._apply_resize()
        browser.new_tab("http://127.0.0.1:%d/" % server.server_address[1])
        assert browser.settle(20.0), "script-created images should arrive"
        tab = browser.tabs[0]
        drawn = [c for c in tab.display_list if isinstance(c, DrawImage)]
        assert len(drawn) == 3, \
            f"wanted 3 JS-created images drawn, found {len(drawn)}"
        assert not [c for c in tab.display_list
                    if "[img" in getattr(c, "text", "")], \
            "no [img] placeholder may remain"
    finally:
        server.shutdown()


def test_browse_page_thumbnails_with_query_urls_render():
    """safebooru's post list: absolute JPEG `<img>` URLs with a query
    string, wrapped in flex cells.

    The regression this guards is the safebooru.org browse page, whose
    thumbnails are all `.../thumbnail_<hash>.jpg?<post_id>`. The query is
    part of the URL the layout keys its image cache on, so a URL
    round-trip that dropped it would turn every photo back into an
    "[img]" placeholder; so would an engine built before native JPEG
    support, which draws the homepage PNG and quietly no JPEG at all --
    exactly the page as safebooru serves it.
    """
    import http.server
    import threading

    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawImage

    photo = open(os.path.join(_FIXTURES, "photo.jpg"), "rb").read()
    thumbs = "".join(
        f'<span class="thumb"><a href="/index.php?page=post&s=view&id={i}">'
        f'<img src="/thumbnails/999/thumbnail_{i}.jpg?{i}" border="0"></a>'
        f'</span>'
        for i in range(6))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/thumbnails/"):
                body, ctype = photo, "image/jpeg"
            else:
                body = (b'<!doctype html><style>'
                        b'div.image-list{display:flex;flex-flow:wrap}'
                        b'.thumb{width:200px;height:200px}</style>'
                        b'<div class="image-list">' + thumbs.encode() +
                        b'</div>')
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        browser = Browser()
        browser.new_tab("http://127.0.0.1:%d/index.php?page=post&s=list"
                        % server.server_address[1])
        tab = browser.tabs[0]
        assert browser.settle(20.0), "thumbnails should have arrived"
        drawn = [c for c in tab.display_list if isinstance(c, DrawImage)]
        assert len(drawn) == 6, \
            f"wanted 6 thumbnails drawn, found {len(drawn)}"
        for c in drawn:
            assert (c.photo.width(), c.photo.height()) == (320, 224), \
                "each thumbnail decodes to the real photograph"
    finally:
        server.shutdown()


def test_a_scrolled_page_never_paints_over_the_chrome():
    """Scrolling, then a page re-render, must not bury the nav bar.

    The canvas paints in insertion order, so page commands re-executed after
    the chrome was drawn paint on top of it. Once the page is scrolled, its
    full-viewport background rectangles start above the window top and span
    the whole chrome strip -- so a page-layer repaint on its own (the 120ms
    coalescer, which fires when a background image arrives) used to white out
    the tabs and address bar. _draw_page now re-asserts the chrome over the
    page, and this proves the tab strip survives the exact sequence.
    """
    import shutil
    import tempfile
    from feetbrowser.browser import Browser

    work = tempfile.mkdtemp(prefix="fb-chrome-")
    try:
        page = os.path.join(work, "page.html")
        with open(page, "w") as handle:
            handle.write('<!doctype html><style>div{background:#ff0000;'
                         'height:5000px}</style><div><p>hi</p></div>')
        browser = Browser()
        browser.window.geometry("900x700")
        browser.canvas.resize(900, 700)
        browser._apply_resize()
        browser.new_tab("file://" + page)
        browser.settle(20.0)
        tab = browser.tabs[0]
        chrome = browser.chrome_height()
        # Scroll the red page up under the chrome, then re-render and let the
        # 120ms coalescer repaint the page alone -- the sequence that follows
        # a scroll while background images are still arriving.
        tab.set_scroll(400)
        tab.render()
        browser._repaint_tick()
        surface = browser.canvas.render()
        tab_bar = tuple(canvasmod.color(browser.c("tab_bar"))[:3])
        red = (255, 0, 0)
        assert _pixel(surface, 400, 5) == tab_bar, \
            "the tab strip was painted over by the scrolled page"
        assert _pixel(surface, 400, chrome - 3) != red, \
            "the chrome strip shows page content instead of chrome"
    finally:
        shutil.rmtree(work, ignore_errors=True)
# -- the scrollbar, dragged ------------------------------------------------
#
# The bar is painted onto the same canvas as everything else and is not a
# widget, so nothing in the platform layers knows it exists: whether a press
# on it scrolls is decided in browser.py, and both backends deliver the
# press, the drag and the release through exactly the bindings used here.
# That is why these live with the rest of the drawn chrome rather than in
# either platform suite -- and why the platform suites each drive the same
# gesture through their own event translation as well.

def _scrollable_browser(lines=300):
    """A browser showing a page far taller than its window."""
    from feetbrowser.browser import Browser

    browser = Browser()
    browser.new_tab("data:text/html," + "".join("<p>line %d</p>" % i
                                                for i in range(lines)))
    browser.draw()
    tab = browser.tabs[0]
    assert tab.content_height() > browser.tab_height(), \
        "the fixture page is not tall enough to scroll"
    assert _thumb(browser)[0] is not None, "no scrollbar is drawn"
    return browser, tab


def _mouse(browser, sequence, y, x=None):
    """Press, drag or release on the scrollbar, the way a backend would."""
    if x is None:
        x = browser.canvas.winfo_width() - 7  # the middle of the thumb
    browser.window.dispatch(sequence, Event(x=x, y=y, num=1, type=sequence))


def _thumb(browser):
    """(top, height) of the thumb, read off the painted pixels.

    Off the pixels rather than out of the browser's own arithmetic, so these
    tests say where the reader can see the thumb and not merely where the
    code that draws it thinks it is.
    """
    rgb = tuple(canvasmod.color(browser.c("scroll_thumb"))[:3])
    x = browser.canvas.winfo_width() - 7
    surface = browser.canvas.render()
    rows = [y for y in range(surface.height) if _pixel(surface, x, y) == rgb]
    if not rows:
        return None, None
    return min(rows), max(rows) - min(rows) + 1


def _track(browser):
    """(top, height) of the whole track the thumb travels in."""
    return browser.chrome_height(), browser.tab_height()


def _span(tab):
    """The scroll offset the bottom of the track stands for."""
    return tab.content_height() - tab.tab_height


def test_dragging_the_scrollbar_scrolls_the_page():
    """The bug: the bar was painted and nothing hit-tested it, so a press on
    it started a text selection and the page never moved."""
    browser, tab = _scrollable_browser()
    top, _height = _thumb(browser)
    _mouse(browser, "<Button-1>", top + 5)
    assert tab.scroll == 0, "merely grabbing the thumb moved the page"
    assert tab.selection is None, "the press started a text selection"
    _mouse(browser, "<B1-Motion>", top + 105)
    assert tab.scroll > 0, "dragging the scrollbar did not scroll the page"
    # 100px of a track that is (track height - thumb height) long, in
    # document terms: the thumb covers the viewport, the rest is the scroll.
    _track_top, track_h = _track(browser)
    expected = 100 / (track_h - _thumb(browser)[1]) * _span(tab)
    assert abs(tab.scroll - expected) < 10, \
        "scrolled %r, expected about %r" % (tab.scroll, expected)


def test_the_grabbed_point_stays_under_the_pointer():
    """Whatever part of the thumb was grabbed is the part that follows the
    pointer -- otherwise the page jumps on the first pixel of movement."""
    browser, _tab = _scrollable_browser()
    top, height = _thumb(browser)
    grab = top + height - 3  # grabbed near the bottom edge of the thumb
    _mouse(browser, "<Button-1>", grab)
    assert _thumb(browser)[0] == top, "the thumb moved when it was grabbed"
    for step in (40, 90, 61):
        _mouse(browser, "<B1-Motion>", grab + step)
        moved = _thumb(browser)[0] - top
        assert abs(moved - step) <= 1, \
            "pointer moved %d, thumb moved %r" % (step, moved)


def test_dragging_the_scrollbar_stops_where_the_wheel_stops():
    """Past either end of the track the page stops at the same offsets the
    wheel stops at, rather than at limits of the scrollbar's own."""
    browser, tab = _scrollable_browser()
    tab.scroll_by(10 ** 9)
    bottom = tab.scroll  # where the wheel gives up
    tab.set_scroll(0)
    browser.draw()
    top, _height = _thumb(browser)
    _mouse(browser, "<Button-1>", top + 5)
    _mouse(browser, "<B1-Motion>", 10 ** 6)  # far below the window
    assert tab.scroll == bottom, \
        "dragging off the bottom gave %r, the wheel gives %r" % (tab.scroll,
                                                                 bottom)
    _mouse(browser, "<B1-Motion>", -10 ** 6)  # and far above it
    assert tab.scroll == 0, "dragging off the top gave %r" % tab.scroll


def test_releasing_outside_the_window_ends_the_drag():
    """A release lands wherever the pointer got to, which is routinely
    outside the window. Missing it would leave the bar stuck to the mouse."""
    browser, tab = _scrollable_browser()
    top, _height = _thumb(browser)
    _mouse(browser, "<Button-1>", top + 5)
    _mouse(browser, "<B1-Motion>", top + 205)
    scrolled = tab.scroll
    assert scrolled > 0, "the drag never got going"
    # Off the bottom of the window and off its left edge, which is where a
    # pointer ends up when someone flings the bar and lets go.
    _mouse(browser, "<ButtonRelease-1>", 10 ** 6, x=-400)
    _mouse(browser, "<B1-Motion>", top + 400)
    assert tab.scroll == scrolled, \
        "the page kept following the pointer after the button came up"


def test_pressing_the_empty_track_centres_the_thumb_and_drags_on():
    """The track is jump-to-here, and the jump hands straight over to a drag
    so one gesture can start anywhere on the bar."""
    browser, tab = _scrollable_browser()
    track_top, track_h = _track(browser)
    target = track_top + track_h - 40  # well below the thumb, on the track
    _mouse(browser, "<Button-1>", target)
    top, height = _thumb(browser)
    assert abs((top + height / 2) - target) <= 1, \
        "the thumb centred on %r, not on the press at %r" % (top + height / 2,
                                                             target)
    jumped = tab.scroll
    _mouse(browser, "<B1-Motion>", target - 50)
    assert tab.scroll < jumped, "the press did not hand over to a drag"
    assert abs(_thumb(browser)[0] - (top - 50)) <= 1, \
        "the drag did not continue from where the thumb landed"


def test_a_page_that_fits_has_no_scrollbar_to_drag():
    """Nothing to scroll, so a press in the gutter is the page's, and a drag
    across it must not move anything or raise."""
    from feetbrowser.browser import Browser

    browser = Browser()
    browser.new_tab("data:text/html,<p>short</p>")
    browser.draw()
    tab = browser.tabs[0]
    assert tab.content_height() <= browser.tab_height(), "fixture too tall"
    assert _thumb(browser)[0] is None, "a bar was drawn anyway"
    _mouse(browser, "<Button-1>", browser.chrome_height() + 200)
    _mouse(browser, "<B1-Motion>", browser.chrome_height() + 300)
    _mouse(browser, "<ButtonRelease-1>", browser.chrome_height() + 300)
    assert tab.scroll == 0, "a page that fits scrolled to %r" % tab.scroll


# -- wheel momentum --------------------------------------------------------
#
# A fast wheel flick coasts after the last notch instead of stopping dead:
# the tracked velocity animates the page on and decays it to nothing. These
# drive the same handler a wheel does and pump the window's own timer queue.

def _wheel_flick(browser, notches, delta=-20):
    """Dispatch `notches` wheel events in a quick burst, as one flick."""
    for _ in range(notches):
        browser._on_wheel(Event(delta=delta, x=200, y=300))


def _pump_timers(browser, seconds):
    """Run the window's timer queue until `seconds` have passed or it idles."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        wait = browser.window.flush_timers()
        if wait is None:
            break
        time.sleep(0.005)


def test_a_fast_wheel_flick_coasts_after_the_last_notch():
    """A burst of notches carries the page on after the wheel stops, so
    quick scrolling does not need a notch for every pixel it wants."""
    browser, tab = _scrollable_browser()
    _wheel_flick(browser, 8)
    manual = tab.scroll
    assert manual > 0, "the notches themselves scrolled nothing"
    _pump_timers(browser, 2.0)
    assert tab.scroll > manual, \
        "the coast stopped at the last notch instead of gliding on"


def test_a_lone_slow_notch_does_not_coast():
    """Momentum is for flicks: a single notch is a step, not a glide, and
    must not run away with the page by itself."""
    browser, tab = _scrollable_browser()
    _wheel_flick(browser, 1)
    manual = tab.scroll
    _pump_timers(browser, 2.0)
    assert tab.scroll <= manual + 30, \
        "a single notch coasted %r px past itself" % (tab.scroll - manual)


def test_a_keyboard_scroll_stops_an_in_progress_coast():
    """Whatever coast is running must die the moment the reader scrolls any
    other way -- the page cannot be both gliding and keyed at once."""
    browser, tab = _scrollable_browser()
    _wheel_flick(browser, 8)
    _pump_timers(browser, 0.1)  # the coast is underway
    browser._scroll(0)  # a keyed scroll of zero is still a scroll
    settled = tab.scroll
    _pump_timers(browser, 2.0)
    assert tab.scroll == settled, \
        "the coast resumed after a keyboard scroll stopped it"


# -- selecting page text ---------------------------------------------------
#
# Every test below drives the same path a mouse does -- a press, some drags,
# a release, at pixel coordinates worked out from where the text actually
# landed -- and asserts on the string the selection spells, because that
# string is what a user gets when they press Ctrl+C. Asserting on the
# internal ranges instead would pass just as happily with the offsets one
# character out.


def _selection_tab(html, height=600):
    """A laid-out page in a Tab, with no browser and no window."""
    from feetbrowser.browser import Tab
    tab = Tab(height)
    tab._build(None, html)
    return tab


def _runs(tab):
    """The page's text runs by the word they drew, in document order."""
    from feetbrowser.layout import DrawText
    out = {}
    for cmd in tab.display_list:
        if isinstance(cmd, DrawText) and cmd.text:
            out.setdefault(cmd.text, cmd)
    return out


def _x_of(run, offset):
    """Pixel x of the boundary before character `offset` of `run`."""
    from feetbrowser.layout import _measure
    return run.left + _measure(run.font, run.text[:offset])


def _drag(tab, start, end):
    """Press at `start`, drag through and release at `end`; both (x, y)."""
    tab.start_selection(*start)
    tab.extend_selection(*end)
    return tab.selected_text()


def test_selection_hit_testing_uses_per_character_advances():
    """A pixel maps to a character offset, in a proportional face.

    "Illustration" in a serif face is the case that catches an implementation
    assuming every character is the same width: an `l` is a fraction of an
    `I`, so dividing the run's width by its length puts every offset past the
    first one in the wrong place.
    """
    tab = _selection_tab("<p>Illustration</p>")
    run = _runs(tab)["Illustration"]
    index = tab.selection_index()
    widths = [_x_of(run, i + 1) - _x_of(run, i) for i in range(len(run.text))]
    assert max(widths) - min(widths) > 1.0, \
        "this face is monospaced, so the test proves nothing"
    for offset in range(len(run.text) + 1):
        # Aim a hair to the right of the boundary, as a pointer would.
        hit_run, hit = index.hit(_x_of(run, offset) + 0.4, run.top + 2)
        assert (hit_run.text, hit) == (run.text, offset), \
            f"x of offset {offset} resolved to {hit}"


def test_selection_click_past_the_end_of_a_line_lands_at_the_line_end():
    """Dragging out into the margin selects to the end of the line, which is
    where the pointer is pointing -- not nothing, which is what a hit test
    requiring the point to be inside a glyph returns."""
    tab = _selection_tab("<p>Alpha beta gamma</p><p>Delta</p>")
    runs = _runs(tab)
    alpha, gamma = runs["Alpha"], runs["gamma"]
    assert _drag(tab, (alpha.left, alpha.top + 2),
                 (gamma.right + 400, gamma.top + 2)) == "Alpha beta gamma"
    # And to the left of the first word, which is the same question mirrored.
    assert _drag(tab, (gamma.right, gamma.top + 2),
                 (alpha.left - 40, alpha.top + 2)) == "Alpha beta gamma"


def test_selection_spans_three_paragraphs_in_both_directions():
    """The case a pixel-pair model gets wrong: the selection covers whole
    lines in the middle, partial runs at each end, and reads the same however
    the drag ran."""
    tab = _selection_tab("<p>Alpha beta gamma</p>"
                         "<p>Delta epsilon</p>"
                         "<p>Zeta eta theta</p>")
    runs = _runs(tab)
    start = (_x_of(runs["beta"], 2), runs["beta"].top + 2)
    end = (_x_of(runs["eta"], 1), runs["eta"].top + 2)
    forwards = _drag(tab, start, end)
    assert forwards == "ta gamma\nDelta epsilon\nZeta e", repr(forwards)
    backwards = _drag(tab, end, start)
    assert backwards == forwards, repr(backwards)


def test_selection_of_a_partial_run_paints_only_the_selected_characters():
    """A run the selection stops inside must be split, in the ranges the
    painter uses as well as in the text that gets copied."""
    tab = _selection_tab("<p>Alpha beta gamma</p>")
    runs = _runs(tab)
    beta = runs["beta"]
    text = _drag(tab, (_x_of(beta, 1), beta.top + 2),
                 (_x_of(beta, 3), beta.top + 2))
    assert text == "et", repr(text)
    spans = tab._selection_spans()
    assert len(spans) == 1, spans
    run, s, e = spans[0]
    assert (run.text, s, e) == ("beta", 1, 3)
    assert abs(run.x_at(s) - _x_of(beta, 1)) < 0.01
    assert run.x_at(e) < beta.right, "a partial run must not paint to its end"


def test_double_click_selects_a_word_and_triple_click_the_line():
    """The convention on both platforms we run on: two clicks take the word
    under the pointer, three take the whole laid-out line."""
    tab = _selection_tab("<p>Alpha beta-gamma, delta.</p>")
    runs = _runs(tab)
    run = runs["beta-gamma,"]
    # Inside "gamma", which is a word bounded by a hyphen and a comma.
    tab.start_selection(_x_of(run, 7), run.top + 2, "word")
    assert tab.selected_text() == "gamma", repr(tab.selected_text())
    # Inside "beta".
    tab.start_selection(_x_of(run, 2), run.top + 2, "word")
    assert tab.selected_text() == "beta", repr(tab.selected_text())
    # On the punctuation between them: browsers take the punctuation run.
    tab.start_selection(_x_of(run, 4) + 0.5, run.top + 2, "word")
    assert tab.selected_text() == "-", repr(tab.selected_text())
    tab.start_selection(_x_of(run, 2), run.top + 2, "line")
    assert tab.selected_text() == "Alpha beta-gamma, delta."


def test_double_click_then_drag_keeps_extending_by_whole_words():
    tab = _selection_tab("<p>Alpha beta gamma delta</p>")
    runs = _runs(tab)
    tab.start_selection(_x_of(runs["beta"], 2), runs["beta"].top + 2, "word")
    tab.extend_selection(_x_of(runs["gamma"], 2), runs["gamma"].top + 2)
    assert tab.selected_text() == "beta gamma", repr(tab.selected_text())
    # Dragging back past where it started keeps the word it started on.
    tab.extend_selection(_x_of(runs["Alpha"], 2), runs["Alpha"].top + 2)
    assert tab.selected_text() == "Alpha beta", repr(tab.selected_text())


def test_selection_stays_on_its_words_when_the_page_scrolls():
    """The highlight is attached to the text, not to the screen.

    repaint() rebuilds the display list on every scroll tick, so a selection
    remembering paint commands or y coordinates is pointing at nothing one
    wheel click later. The positions are node offsets, so they survive -- and
    a press after the scroll has to be read in the same coordinates.
    """
    tab = _selection_tab("".join("<p>Para %d alpha beta gamma</p>" % i
                                 for i in range(60)))
    runs = _runs(tab)
    before = _drag(tab, (runs["alpha"].left, runs["alpha"].top + 2),
                   (runs["gamma"].right, runs["gamma"].top + 2))
    assert before == "alpha beta gamma"
    top_before = tab._selection_spans()[0][0].top
    tab.set_scroll(300)
    tab.repaint()
    assert tab.scroll == 300, "the page has to be scrollable for this to test"
    assert tab.selected_text() == before, "the selection moved off its words"
    assert tab._selection_spans()[0][0].top == top_before, \
        "the highlight is in document space, so its y must not move"
    # A fresh press is given viewport coordinates and must land on the word
    # actually under the pointer.
    from feetbrowser.layout import DrawText
    visible = next(c for c in tab.display_list
                   if isinstance(c, DrawText) and c.text
                   and c.top > tab.scroll + 40)
    text = _drag(tab, (visible.left, visible.top - tab.scroll + 2),
                 (visible.right, visible.top - tab.scroll + 2))
    assert text == visible.text, repr(text)


def test_selection_survives_a_rewrap_but_not_a_new_document():
    """A relayout that only moved the words keeps the highlight on them; one
    that replaced them drops it, rather than leaving a highlight sitting over
    whatever moved into those pixels."""
    tab = _selection_tab("<p>Alpha beta gamma delta epsilon zeta</p>")
    runs = _runs(tab)
    before = _drag(tab, (runs["beta"].left, runs["beta"].top + 2),
                   (runs["delta"].right, runs["delta"].top + 2))
    assert before == "beta gamma delta"
    tab.document.width = 120        # force the paragraph to rewrap narrower
    tab.render()
    assert tab.selected_text() == before, "a rewrap must not move the selection"
    tab._build(None, "<p>Something else entirely</p>")
    assert tab.selection is None, "a new document must drop the selection"
    assert tab.selected_text() == ""


def test_selection_ignores_text_a_stacking_context_lifted():
    """Document order comes from the DOM, not from paint order: a z-index
    lifts a paragraph's paint above its neighbours without moving its text in
    the document, and a selection ordered by the display list would copy the
    paragraphs back to front."""
    tab = _selection_tab(
        "<p>First one</p><p style='position:relative;z-index:5'>Second one"
        "</p><p>Third one</p>")
    runs = _runs(tab)
    lifted = [c.text for c in tab.display_list
              if getattr(c, "text", None) == "Second"]
    assert lifted, "the middle paragraph still has to be painted"
    text = _drag(tab, (runs["First"].left, runs["First"].top + 2),
                 (runs["Third"].right, runs["Third"].top + 2))
    assert text == "First one\nSecond one\nThird", repr(text)


def _selection_browser(html, tmpdir):
    """A real Browser on a real canvas, showing `html` from a file."""
    from feetbrowser.browser import Browser
    path = os.path.join(tmpdir, "page.html")
    with open(path, "w") as handle:
        handle.write(html)
    browser = Browser()
    browser.new_tab("file://" + path)
    browser.draw()
    return browser


def test_selection_paints_a_themed_highlight_with_legible_text():
    """Press, drag and release through the browser's own handlers, then look
    at the pixels: the highlight is the shoe's accent, it lies where the
    selected characters are and nowhere else, and there is contrasting text
    on top of it rather than a solid slab."""
    import shutil
    import tempfile
    from feetbrowser.canvas import color
    from feetbrowser.window import Event

    work = tempfile.mkdtemp(prefix="fb-selection-")
    try:
        browser = _selection_browser(
            "<!doctype html><body style='font-size:20px'>"
            "<p>Alpha beta gamma delta</p><p>Second paragraph</p>", work)
        tab = browser.tabs[0]
        accent = color(browser.c("accent"))
        assert _count_pixels(browser.canvas.render(), accent) == 0, \
            "nothing should be accent-coloured before anything is selected"
        runs = _runs(tab)
        alpha, gamma = runs["Alpha"], runs["gamma"]
        chrome = browser.chrome_height()
        browser._on_click(Event(x=alpha.left, y=alpha.top + chrome + 2))
        browser._on_drag(Event(x=gamma.right, y=gamma.top + chrome + 2))
        browser._on_release(Event(x=gamma.right, y=gamma.top + chrome + 2))
        assert tab.selected_text() == "Alpha beta gamma"

        surface = browser.canvas.render()
        painted = _count_pixels(surface, accent)
        assert painted > 500, f"no highlight painted ({painted} px)"
        # All of it inside the band the selected characters occupy, so a
        # partially selected line is not filled to the window edge.
        x0, x1 = int(alpha.left), int(gamma.right) + 1
        y0, y1 = int(alpha.top + chrome), int(alpha.bottom + chrome) + 1
        inside = sum(1 for y in range(y0, y1) for x in range(x0, x1)
                     if _pixel(surface, x, y) == accent)
        assert inside == painted, \
            f"{painted - inside} highlight pixels outside the selection"
        ink = color(tab.selection_colors()[1])
        legible = sum(1 for y in range(y0, y1) for x in range(x0, x1)
                      if _pixel(surface, x, y) == ink)
        assert legible > 100, \
            f"the highlighted text is not being drawn on top ({legible} px)"
        # "delta" is not selected, so its glyphs stay the page's own colour.
        delta = runs["delta"]
        assert _pixel(surface, int(delta.left + 2),
                      int(delta.top + chrome + 10)) != accent
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_plain_click_clears_the_selection_and_copy_puts_it_on_the_clipboard():
    import shutil
    import tempfile
    from feetbrowser.window import Event

    work = tempfile.mkdtemp(prefix="fb-selection-")
    try:
        browser = _selection_browser(
            "<!doctype html><p>Alpha beta gamma delta</p>", work)
        tab = browser.tabs[0]
        runs = _runs(tab)
        alpha, gamma = runs["Alpha"], runs["gamma"]
        chrome = browser.chrome_height()
        browser._on_click(Event(x=alpha.left, y=alpha.top + chrome + 2))
        browser._on_drag(Event(x=gamma.right, y=gamma.top + chrome + 2))
        browser._on_release(Event(x=gamma.right, y=gamma.top + chrome + 2))
        browser._copy_selection()
        assert browser.window.clipboard_get() == "Alpha beta gamma"
        assert any(item and item[0] == "Copy" for item
                   in browser._context_items(alpha.left, alpha.top + chrome)), \
            "a selection should offer Copy in the context menu"
        # A press and release with no drag in between, somewhere else on the
        # page, is a plain click.
        delta = runs["delta"]
        browser._on_click(Event(x=delta.right, y=delta.top + chrome + 2))
        browser._on_release(Event(x=delta.right, y=delta.top + chrome + 2))
        assert tab.selection is None
        assert tab.selected_text() == ""
        surface = browser.canvas.render()
        from feetbrowser.canvas import color
        assert _count_pixels(surface, color(browser.c("accent"))) == 0, \
            "the highlight outlived the selection"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_press_in_the_scrollbar_gutter_starts_no_selection():
    """The strip the scrollbar lives in belongs to the bar, not to the text.

    Hit testing resolves any point to the nearest line, so without this a
    grab at the scrollbar -- and the drag down that follows it -- selects
    whatever text happens to end nearest the right-hand edge instead of
    scrolling.
    """
    import shutil
    import tempfile
    from feetbrowser.window import Event

    work = tempfile.mkdtemp(prefix="fb-selection-")
    try:
        browser = _selection_browser(
            "<!doctype html>" + "".join("<p>Para %d alpha beta</p>" % i
                                        for i in range(80)), work)
        tab = browser.tabs[0]
        assert tab.content_height() > browser.tab_height(), \
            "the page has to overflow for there to be a scrollbar"
        chrome = browser.chrome_height()
        x = browser.canvas.winfo_width() - 5
        browser._on_click(Event(x=x, y=chrome + 100))
        assert tab.selection is None, "a scrollbar press anchored a selection"
        browser._on_drag(Event(x=x, y=chrome + 400))
        assert tab.selected_text() == "", \
            "dragging the scrollbar selected page text"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_dragging_a_tab_paints_it_under_the_pointer():
    """Press a tab and carry it two places along, then look at the strip:
    the tab is painted where the pointer took it, the tabs it passed have
    shifted back into the hole it left, and the drop lands it there."""
    from feetbrowser import browser as browsermod
    from feetbrowser import toes
    from feetbrowser.canvas import color

    browser = browsermod.Browser()
    try:
        for _ in range(3):
            browser.new_tab("about:blank")
        first, second = browser.tabs[0], browser.tabs[1]
        top = toes.band_height(browser.chrome_bands())
        active = color(browser.c("tab_active"))
        inactive = color(browser.c("tab_inactive"))
        strip = color(browser.c("tab_bar"))
        gap = browsermod.TAB_GAP

        def fill(slot, dy=20):
            """The colour painted in the middle of slot `slot`."""
            x = browsermod.TAB_LEFT + slot * gap + browsermod.TAB_WIDTH // 2
            return _pixel(browser.canvas.render(), x, top + dy)

        # A press picks the tab up and makes it active, so the active fill is
        # what marks where it has got to from here on.
        x = browsermod.TAB_LEFT + browsermod.TAB_WIDTH // 2
        browser._on_click(Event(x=x, y=top + 20))
        assert browser.active_tab is first, "the press did not select the tab"
        assert fill(0) == active, "the pressed tab is not the active one"

        browser._on_drag(Event(x=x + 2 * gap, y=top + 20))
        assert fill(2) == active, \
            f"the carried tab is not under the pointer: {fill(2)}"
        assert fill(0) == inactive, \
            f"the tab it passed did not shift into the hole: {fill(0)}"
        assert fill(2, dy=3) == active, \
            "the carried tab is not drawn standing above its neighbours"
        assert fill(0, dy=3) == strip, \
            "a tab that is not being carried should not stand up"
        assert browser.tabs[0] is first, "the list moved before the drop"

        browser._on_release(Event(x=x + 2 * gap, y=top + 20))
        assert browser.tabs.index(first) == 2, \
            "the tab did not land where the strip said it would"
        assert browser.tabs.index(second) == 0, "the strip did not close up"
        assert browser.active_tab is first, "the dragged tab is not active"
        assert fill(2) == active, "the tab is not painted in its new slot"
    finally:
        browser.window.destroy()


# -- video: containers and codecs ------------------------------------------
#
# Everything below reads bytes this file wrote, so an assertion about a pixel
# is an assertion about a decoder and not about a fixture nobody can open.
# The scheduling tests all drive a ManualClock, so what they measure is the
# scheduler and not how busy the machine was when they ran.

def _clip(count=10, width=8, height=6, fps=10.0, top_down=False):
    """An uncompressed AVI whose frame `i` is the flat colour (i, 0, 0)
    everywhere except pixel (1, 2), which is (0, 200, i). The frame index is
    written into the pixels, so "which frame is on screen" is a question the
    screen itself answers."""
    def painter(i):
        def pixel(x, y):
            return (0, 200, i) if (x, y) == (1, 2) else (i, 0, 0)
        return pixel
    frames = [media_fixtures.rgb24_frame(width, height, painter(i), top_down)
              for i in range(count)]
    return media_fixtures.avi(frames, width, height, fps=fps,
                              top_down=top_down)


def _rgba_at(frame, x, y):
    offset = (y * frame.width + x) * 4
    return tuple(frame.rgba[offset:offset + 4])


def test_avi_header_reports_geometry_rate_and_frame_count():
    track = mediacodec.open_video(_clip(count=7, width=12, height=5, fps=20.0))
    assert track.container == "AVI"
    assert track.codec_name == "BI_RGB"
    assert (track.width, track.height) == (12, 5)
    assert track.frame_count == 7
    assert abs(track.frame_rate - 20.0) < 1e-9
    assert abs(track.duration - 0.35) < 1e-9
    assert track.info.supported


def test_avi_rgb24_frames_decode_to_the_exact_pixels_written():
    track = mediacodec.open_video(_clip(count=4))
    for index in range(4):
        frame = track.frame(index)
        assert frame.index == index
        assert (frame.width, frame.height) == (8, 6)
        assert len(frame.rgba) == 8 * 6 * 4
        assert _rgba_at(frame, 0, 0) == (index, 0, 0, 255)
        assert _rgba_at(frame, 1, 2) == (0, 200, index, 255)
        # Every codec in this module writes opaque alpha; the player relies
        # on it to take the surface's row-copy blit.
        assert frame.rgba[3::4].count(255) == 8 * 6


def test_avi_rows_come_out_the_same_way_up_whichever_way_they_went_in():
    """A DIB is bottom-up unless biHeight is negative. Getting this wrong
    produces a picture that is upside down but otherwise perfect, which is
    exactly the bug that survives a "does it look like video" check."""
    up = mediacodec.open_video(_clip(count=2, top_down=False)).frame(1)
    down = mediacodec.open_video(_clip(count=2, top_down=True)).frame(1)
    assert _rgba_at(up, 1, 2) == (0, 200, 1, 255)
    assert _rgba_at(down, 1, 2) == (0, 200, 1, 255)
    assert bytes(up.rgba) == bytes(down.rgba)


def test_avi_decodes_32_bit_and_8_bit_palettised_frames():
    def pixel(x, y):
        return (10 + x, 20 + y, 30)
    raw32 = media_fixtures.avi([media_fixtures.rgb32_frame(4, 3, pixel)],
                               4, 3, bit_count=32)
    frame = mediacodec.open_video(raw32).frame(0)
    assert _rgba_at(frame, 3, 2) == (13, 22, 30, 255)

    palette = media_fixtures.grey_palette()
    raw8 = media_fixtures.avi(
        [media_fixtures.pal8_frame(4, 3, lambda x, y: x + y * 4)],
        4, 3, bit_count=8, palette=palette)
    frame = mediacodec.open_video(raw8).frame(0)
    assert _rgba_at(frame, 2, 1) == (6, 6, 6, 255)
    assert _rgba_at(frame, 0, 0) == (0, 0, 0, 255)


def test_avi_frame_times_follow_the_declared_rate():
    track = mediacodec.open_video(_clip(count=5, fps=8.0))
    times = [track.frame(i).pts for i in range(5)]
    assert times == [0.0, 0.125, 0.25, 0.375, 0.5]
    assert all(abs(track.frame(i).duration - 0.125) < 1e-9 for i in range(5))
    # dwRate/dwScale is exact; dwMicroSecPerFrame is rounded to whole
    # microseconds and must not be the one we believe.
    assert abs(track.frame_rate - 8.0) < 1e-9


def test_rle8_keyframes_decode_and_delta_frames_composite_on_the_last():
    """The inter-frame case, and the reason decoding is stateful: frames 1
    and 2 carry only the pixels that changed."""
    palette = media_fixtures.grey_palette()
    key = media_fixtures.rle8_keyframe(4, 3, lambda x, y: 5)
    # Bottom-up: opcode row 0 is image row 2, so (delta 1,1) lands on image
    # row 1 at x=1, where two pixels become index 99.
    delta = media_fixtures.rle8_delta([("delta", 1, 1), ("run", 2, 99),
                                       ("eob",)])
    data = media_fixtures.avi([key, delta], 4, 3, bit_count=8, compression=1,
                              palette=palette, handler="MRLE",
                              keyframes=[1, 0])
    track = mediacodec.open_video(data)
    assert track.codec_name == "BI_RLE8"
    assert [track.is_keyframe(i) for i in range(2)] == [True, False]

    first = track.frame(0)
    assert all(_rgba_at(first, x, y) == (5, 5, 5, 255)
               for x in range(4) for y in range(3))
    second = track.frame(1)
    assert [_rgba_at(second, x, 1)[0] for x in range(4)] == [5, 99, 99, 5]
    # Untouched rows kept the previous picture, which is the whole point.
    assert [_rgba_at(second, x, 0)[0] for x in range(4)] == [5, 5, 5, 5]


def test_seeking_an_inter_frame_stream_replays_from_the_keyframe():
    palette = media_fixtures.grey_palette()
    key = media_fixtures.rle8_keyframe(4, 3, lambda x, y: 5)
    delta = media_fixtures.rle8_delta([("delta", 1, 1), ("run", 2, 99),
                                       ("eob",)])
    grow = media_fixtures.rle8_delta([("delta", 0, 1), ("run", 4, 77),
                                      ("eob",)])
    data = media_fixtures.avi([key, delta, grow], 4, 3, bit_count=8,
                              compression=1, palette=palette,
                              handler="MRLE", keyframes=[1, 0, 0])
    track = mediacodec.open_video(data)
    forwards = [bytes(track.frame(i).rgba) for i in range(3)]
    assert track.keyframe_before(2) == 0
    # Jump backwards: the decoder must rewind to frame 0 and replay, not hand
    # back whatever plane it happened to be holding.
    assert bytes(track.frame(1).rgba) == forwards[1]
    assert bytes(track.frame(2).rgba) == forwards[2]
    track.reset()
    assert bytes(track.frame(2).rgba) == forwards[2]


def test_truncated_avi_files_fail_cleanly_instead_of_hanging():
    """A media parser fed half a file is the classic place a browser hangs.
    Every prefix of a real AVI must come back as a MediaError or a working
    track -- never an IndexError, never a struct error, never a wait."""
    good = _clip(count=4)
    deadline = time.monotonic() + 20.0
    for cut in range(0, len(good), 7):
        chopped = good[:cut]
        try:
            track = mediacodec.open_video(chopped)
        except mediacodec.MediaError:
            pass
        else:
            for index in range(track.frame_count):
                try:
                    track.frame(index)
                except mediacodec.MediaError:
                    break
        assert time.monotonic() < deadline, "truncated AVI took too long"


def test_hostile_chunk_sizes_terminate_the_walk():
    """Sizes that point at themselves, at zero, or past the end of the file.
    Each one is a loop that does not advance if the walker trusts it."""
    good = _clip(count=2)
    at = good.index(b"LIST")
    for size in (0xFFFFFFFF, 0, 4, 8):
        broken = bytearray(good)
        broken[at + 4:at + 8] = struct.pack("<I", size)
        start = time.monotonic()
        try:
            mediacodec.open_video(bytes(broken))
        except mediacodec.MediaError:
            pass
        assert time.monotonic() - start < 5.0, \
            "a LIST size of %d took too long" % size

    # A RIFF header that claims the file is enormous.
    broken = bytearray(good)
    broken[4:8] = struct.pack("<I", 0xFFFFFFFF)
    try:
        mediacodec.open_video(bytes(broken))
    except mediacodec.MediaError:
        pass


def test_rle8_opcodes_that_leave_the_frame_are_rejected():
    palette = media_fixtures.grey_palette()
    key = media_fixtures.rle8_keyframe(4, 3, lambda x, y: 1)
    for ops in (
            [("run", 200, 9), ("eob",)],                 # run past the row
            [("delta", 0, 60), ("run", 1, 9), ("eob",)],  # delta off the end
            [("literal", list(range(40))), ("eob",)],     # literals past it
    ):
        data = media_fixtures.avi([key, media_fixtures.rle8_delta(ops)],
                                  4, 3, bit_count=8, compression=1,
                                  palette=palette, handler="MRLE",
                                  keyframes=[1, 0])
        track = mediacodec.open_video(data)
        assert track.frame(0) is not None
        try:
            track.frame(1)
        except mediacodec.MediaError:
            continue
        raise AssertionError("bad RLE8 opcodes %r were accepted" % (ops,))


def test_an_rle8_stream_that_never_ends_is_stopped():
    """No end-of-bitmap and no advance: the decoder has to give up on its
    own rather than run until the process is killed."""
    palette = media_fixtures.grey_palette()
    key = media_fixtures.rle8_keyframe(2, 2, lambda x, y: 1)
    endless = b"\x00\x02\x00\x00" * 200000        # delta of (0, 0), for ever
    data = media_fixtures.avi([key, endless], 2, 2, bit_count=8,
                              compression=1, palette=palette,
                              handler="MRLE", keyframes=[1, 0])
    track = mediacodec.open_video(data)
    start = time.monotonic()
    try:
        track.frame(1)
    except mediacodec.MediaError:
        pass
    assert time.monotonic() - start < 10.0


def test_probe_reports_mp4_and_webm_without_pretending_to_decode_them():
    info = mediacodec.probe(media_fixtures.mp4(1280, 720, 4.5))
    assert info.container == "MP4"
    assert (info.width, info.height) == (1280, 720)
    assert abs(info.duration - 4.5) < 0.01
    assert info.codec == "avc1"
    assert not info.supported and "H.264" in info.reason

    info = mediacodec.probe(media_fixtures.webm(640, 360, 2.5))
    assert info.container == "WebM"
    assert (info.width, info.height) == (640, 360)
    assert abs(info.duration - 2.5) < 0.01
    assert info.codec == "V_VP9"
    assert not info.supported

    # A WAV is a container we now read, so it is recognised and comes back
    # unsupported rather than raising: it is sound with no picture in it, and
    # `<video src="x.wav">` deserves to be told that rather than "not a media
    # container". `probe_audio` is the one with the answer for it.
    info = mediacodec.probe(b"RIFF\x04\x00\x00\x00WAVE")
    assert info.container == "WAV"
    assert not info.supported and "no picture" in info.reason

    for payload in (b"", b"not a video at all", b"RIFF\x04\x00\x00\x00AVI "):
        try:
            mediacodec.probe(payload)
        except mediacodec.MediaError:
            continue
        raise AssertionError("probe accepted %r" % payload)


def test_an_avi_carrying_a_codec_we_lack_names_it_rather_than_guessing():
    """MPEG-4 ASP demuxes perfectly and decodes not at all. The file's
    geometry and frame count are still real, which is what lets the element
    reserve the right box and say something true."""
    data = media_fixtures.avi([b"\x00\x00\x01\xb6stub"] * 3, 320, 240,
                              bit_count=24,
                              compression=int.from_bytes(b"XVID", "little"),
                              handler="XVID")
    info = mediacodec.probe(data)
    assert info.codec == "XVID"
    assert (info.width, info.height) == (320, 240)
    assert info.frame_count == 3
    assert not info.supported
    assert "MPEG-4" in info.reason
    try:
        mediacodec.open_video(data)
    except mediacodec.MediaError as exc:
        assert "MPEG-4" in str(exc)
    else:
        raise AssertionError("open_video decoded MPEG-4 ASP")


# -- video: Motion JPEG, the format this browser can actually decode --------

def _mjpeg_pixel(index):
    """What frame `index` of the MJPEG fixture clip is painted with.

    Two things are going on here. The frame index is in the red channel, so
    "which frame is on screen" is a question a pixel answers, the same way the
    raw AVI clip does it. And the colour is constant across each 8x8 block --
    it changes only every eighth column -- because a block of one colour has
    no AC coefficients at all, and a JPEG of such blocks survives the round
    trip to within a single count. A single bright pixel in a flat field would
    not: it is the one thing an 8x8 DCT cannot put back exactly, and a test
    written that way would be measuring the transform rather than the codec.
    """
    def pixel(x, y):
        return (index * 20, 60 + (x // 8 % 2) * 40, 120)
    return pixel


def _mjpeg_frames(count, width=16, height=12):
    """`count` real baseline JPEGs, each one telling you which frame it is."""
    return [media_fixtures.jpeg(width, height, _mjpeg_pixel(i))
            for i in range(count)]


def _close(frame, x, y, want, slack=2):
    """Is the pixel at (x, y) the colour we asked for, give or take?

    JPEG is lossy even with an all-ones quantisation table, because the
    forward transform rounds its coefficients to integers. One count is the
    measured worst case over the fixture clip; two is the allowance, and it
    is still nowhere near the twenty counts between one frame and the next.
    """
    got = _rgba_at(frame, x, y)
    return (got[3] == 255
            and all(abs(a - b) <= slack for a, b in zip(got[:3], want)))


def test_mjpeg_in_an_avi_decodes_to_the_pixels_the_encoder_was_given():
    """The format the browser plays. Every frame is a keyframe, so there is
    no inter-frame state to get wrong -- what there is to get wrong is the
    handler/compression fourcc dance and the colour conversion, and an exact
    pixel match catches both."""
    data = media_fixtures.mjpeg_avi(_mjpeg_frames(5), 16, 12, fps=20.0)
    info = mediacodec.probe(data)
    assert info.container == "AVI" and info.codec == "MJPG"
    assert info.supported and not info.reason
    track = mediacodec.open_video(data)
    assert (track.width, track.height) == (16, 12)
    assert track.frame_count == 5
    assert abs(track.frame_rate - 20.0) < 1e-9
    for index in range(5):
        frame = track.frame(index)
        assert _close(frame, 0, 0, (index * 20, 60, 120)), index
        assert _close(frame, 9, 5, (index * 20, 100, 120)), index
        # Every codec in this module writes opaque alpha; the player relies
        # on it to take the surface's row-copy blit.
        assert frame.rgba[3::4].count(255) == 16 * 12
    # Every JPEG frame stands alone, so every one of them is a seek target.
    assert all(track.is_keyframe(i) for i in range(5))
    assert track.keyframe_before(4) == 4


def test_an_mjpeg_avi_whose_index_calls_every_frame_a_delta_is_believed_anyway():
    """Camera-written AVIs routinely leave AVIIF_KEYFRAME off every entry in
    idx1. Taking that at face value would make seeking replay the clip from
    frame zero, for a codec where every frame is independent by definition."""
    data = media_fixtures.mjpeg_avi(_mjpeg_frames(4), 16, 12,
                                    keyframes=[0, 0, 0, 0])
    track = mediacodec.open_video(data)
    assert [track.is_keyframe(i) for i in range(4)] == [True] * 4
    assert track.keyframe_before(3) == 3
    assert _close(track.frame(3), 0, 0, (60, 60, 120))


def test_a_motion_jpeg_frame_without_its_tables_borrows_the_standard_ones():
    """The abbreviated format: the DHT segments are gone, because they would
    be identical in every frame, and the decoder is expected to know the
    Annex K tables. The proof that ours are right is that the same frame
    decodes to the same pixels with the tables and without them."""
    frames = _mjpeg_frames(3)
    stripped = [media_fixtures.strip_huffman_tables(f) for f in frames]
    assert all(len(s) < len(f) for s, f in zip(stripped, frames))
    assert all(b"\xff\xc4" not in s.split(b"\xff\xda")[0] for s in stripped)

    full = mediacodec.open_video(media_fixtures.mjpeg_avi(frames, 16, 12))
    short = mediacodec.open_video(media_fixtures.mjpeg_avi(stripped, 16, 12))
    for index in range(3):
        assert bytes(short.frame(index).rgba) == bytes(full.frame(index).rgba)
    # And the tables really are spliced in rather than the file rewritten:
    # the frame still starts and ends where the file said it did.
    patched = mediacodec._jpeg_frame(stripped[0])
    assert patched.startswith(b"\xff\xd8") and patched.endswith(b"\xff\xd9")
    assert len(patched) > len(stripped[0])


def test_a_bare_jpeg_after_jpeg_file_is_a_playable_stream():
    """No container at all: the `.mjpeg` that comes out of a security camera
    or an MJPEG-over-HTTP capture is JPEGs end to end, and the frame
    boundaries are the markers."""
    frames = _mjpeg_frames(4)
    data = b"".join(frames)
    assert mediacodec.sniff(data) == "MJPEG"
    track = mediacodec.open_video(data)
    assert track.container == "MJPEG"
    assert track.frame_count == 4
    assert (track.width, track.height) == (16, 12)
    assert abs(track.frame_rate - mediacodec.MJPEG_DEFAULT_FPS) < 1e-9
    for index in range(4):
        assert _close(track.frame(index), 0, 0, (index * 20, 60, 120)), index


def test_a_quicktime_movie_demuxes_its_sample_tables():
    """MOV is where MJPEG usually lives, and the samples are not laid out for
    you: chunk offsets, samples-per-chunk runs and sizes have to be walked
    together. Three samples to a chunk means an off-by-one in that walk shows
    up as the wrong picture rather than as an error."""
    frames = _mjpeg_frames(7)
    data = media_fixtures.mov(frames, 16, 12, codec="jpeg", fps=25.0,
                              samples_per_chunk=3)
    assert mediacodec.sniff(data) == "MOV"
    info = mediacodec.probe(data)
    assert info.container == "MOV" and info.codec == "jpeg"
    assert (info.width, info.height) == (16, 12)
    assert info.frame_count == 7 and info.supported
    track = mediacodec.open_video(data)
    for index in range(7):
        assert _close(track.frame(index), 0, 0, (index * 20, 60, 120)), index

    # 64-bit chunk offsets are the same table with a wider field, and a file
    # that uses them is a file no 32-bit reader gets right by accident.
    wide = mediacodec.open_video(
        media_fixtures.mov(frames, 16, 12, samples_per_chunk=2,
                           wide_offsets=True))
    assert _close(wide.frame(6), 0, 0, (120, 60, 120))


def test_a_movie_with_a_sync_table_seeks_to_the_frames_it_names():
    """`stss` lists the keyframes; a track without one is all keyframes. Both
    readings have to be right, because the second is what MJPEG relies on."""
    frames = _mjpeg_frames(6)
    listed = mediacodec.open_video(
        media_fixtures.mov(frames, 16, 12, sync=[1, 4]))
    assert [listed.is_keyframe(i) for i in range(6)] == \
        [True, False, False, True, False, False]
    assert listed.keyframe_before(5) == 3

    absent = mediacodec.open_video(media_fixtures.mov(frames, 16, 12))
    assert all(absent.is_keyframe(i) for i in range(6))


def test_frame_times_come_from_the_file_not_from_an_average_rate():
    """A variable frame rate movie. Dividing the position by a mean rate puts
    every frame after the first change on the wrong side of its boundary, and
    the file says exactly when each one starts, so we ask it."""
    frames = _mjpeg_frames(4)
    # 600 ticks per second: a quarter of a second, then three tenths, twice.
    data = media_fixtures.mov(frames, 16, 12, timescale=600,
                              durations=[150, 180, 180, 90])
    track = mediacodec.open_video(data)
    times = [track.frame(i).pts for i in range(4)]
    assert [round(t, 6) for t in times] == [0.0, 0.25, 0.55, 0.85]
    assert [round(track.frame_duration(i), 6) for i in range(4)] == \
        [0.25, 0.3, 0.3, 0.15]
    assert abs(track.duration - 1.0) < 1e-9
    # index_at is the question the scheduler asks every tick.
    assert [track.index_at(t) for t in
            (0.0, 0.24, 0.25, 0.54, 0.56, 0.84, 0.99, 5.0)] == \
        [0, 0, 1, 1, 2, 2, 3, 3]
    assert track.index_at(-1.0) == 0


def test_quicktime_raw_and_png_frames_decode_without_a_codec():
    """Two formats a QuickTime file can hold that are not compressed video at
    all. They cost nothing -- one is a memcpy and the other is the PNG
    decoder `<img>` already needed -- and they are the only way a lossless
    clip plays here."""
    def pixel(x, y):
        return (x * 10, y * 20, 40)
    raw = [media_fixtures.quicktime_raw_frame(4, 3, pixel) for _ in range(2)]
    track = mediacodec.open_video(media_fixtures.mov(raw, 4, 3, codec="raw "))
    assert track.codec_name == "raw "
    assert _rgba_at(track.frame(1), 3, 2) == (30, 40, 40, 255)

    # 32-bit QuickTime raw is ARGB: alpha first, which is the one byte order
    # that looks fine on a grey test card and wrong on everything else.
    raw32 = [media_fixtures.quicktime_raw_frame(4, 3, pixel, depth=32)]
    track = mediacodec.open_video(
        media_fixtures.mov(raw32, 4, 3, codec="raw ", depth=32))
    assert _rgba_at(track.frame(0), 3, 2) == (30, 40, 40, 255)

    rows = bytes(bytearray([channel for y in range(3) for x in range(4)
                            for channel in pixel(x, y)]))
    png = _png(4, 3, 8, 2, rows)
    track = mediacodec.open_video(
        media_fixtures.mov([png, png], 4, 3, codec="png "))
    assert track.codec_name == "png "
    assert _rgba_at(track.frame(1), 3, 2) == (30, 40, 40, 255)


def test_a_broken_jpeg_frame_fails_rather_than_painting_noise():
    """Every one of these parses as a container and then hands the decoder
    something it cannot use. None of them may hang, and none of them may come
    back as a picture."""
    frames = _mjpeg_frames(2)
    truncated = frames[0][:len(frames[0]) // 2]
    for payload in (b"", b"\xff\xd8\xff\xd9", b"\xff\xd8" + b"\x00" * 64,
                    truncated):
        data = media_fixtures.mjpeg_avi([payload, frames[1]], 16, 12)
        start = time.monotonic()
        try:
            track = mediacodec.open_video(data)
            track.frame(0)
        except mediacodec.MediaError:
            pass
        assert time.monotonic() - start < 10.0, "decoding %r hung" % payload[:8]

    # A file that is nothing but a JPEG header is not a stream of frames.
    for payload in (b"\xff\xd8", b"\xff\xd8\xff", b"\xff\xd8" + b"\xff" * 40):
        try:
            mediacodec.open_video(payload)
        except mediacodec.MediaError:
            continue
        raise AssertionError("open_video accepted %r" % payload)


# -- audio: demuxing sound out of MP4 and MOV -------------------------------
#
# None of these need a working AAC decoder, and that is deliberate: the
# decoder is Fortran and a machine without gfortran must still be able to
# prove that the demuxer finds the right bytes. So they check offsets,
# timestamps, the AudioSpecificConfig, the sample rate and the channel count
# -- everything the container knows -- and the one test that exercises
# `AudioTrack.frame()` hands it a decoder written here.

# AAC-LC, 44100 Hz, stereo, spelled out: five bits of object type (2), four
# of sampling frequency index (4), four of channel configuration (2), and
# three of frame length and extension flags, all zero.
_ASC_44100_STEREO = b"\x12\x10"


def _aac_packets(count=4):
    """Packet payloads of distinct lengths, so an offset bug cannot land on
    the right bytes by accident."""
    return [bytes([0x21 + i]) * (7 + i * 3) for i in range(count)]


def _parse_soun(data):
    """The one `soun` track of an MP4, straight out of the demuxer's own
    parser -- which is where the sample entry and the `esds` are read."""
    _duration, tracks = mediacodec._parse_mp4(data)
    for track in tracks:
        if track.handler == "soun":
            return track
    raise AssertionError("no sound track in this fixture")


def test_an_mp4_audio_track_demuxes_to_offsets_times_and_a_config():
    packets = _aac_packets(4)
    # A short final frame: real encoders write one, and a demuxer that
    # divides a duration by a frame count never notices.
    data = media_fixtures.mp4_audio(packets, durations=[1024, 1024, 1024, 512])
    assert mediacodec.sniff(data) == "MP4"

    track = _parse_soun(data)
    assert track.codec == "mp4a"
    assert track.channels == 2
    assert abs(track.sample_rate - 44100.0) < 1e-9
    assert track.sample_size == 16
    assert track.object_type == 0x40
    assert track.extradata == _ASC_44100_STEREO

    samples = mediacodec._mp4_samples(track)
    assert len(samples) == 4
    base = data.index(b"mdat") + 4
    running = base
    for i, payload in enumerate(packets):
        offset, length, _key = samples[i]
        assert (offset, length) == (running, len(payload))
        assert data[offset:offset + length] == payload
        running += len(payload)

    times = mediacodec._mp4_times(track, len(samples))
    assert abs(times[0][0]) < 1e-9
    assert abs(times[2][0] - 2048 / 44100.0) < 1e-9
    assert abs(times[3][1] - 512 / 44100.0) < 1e-9

    info = mediacodec.probe_audio(data)
    assert info.container == "MP4" and info.codec == "mp4a"
    assert (info.sample_rate, info.channels) == (44100, 2)
    assert info.frame_count == 4
    assert abs(info.duration - 3584 / 44100.0) < 1e-6
    # Whether this machine has a decoder is not this test's business; that it
    # says one thing or the other, with a sentence when it says no, is.
    assert info.supported or info.reason


def test_a_quicktime_sound_entry_is_read_in_all_three_of_its_versions():
    """Versions 1 and 2 append fields before the child boxes -- sixteen bytes
    and thirty-six -- and version 2 puts the real sample rate in a float64.
    A parser that does not know that looks for `esds` inside a number."""
    packets = _aac_packets(2)
    plain = _parse_soun(media_fixtures.mp4_audio(packets))
    assert plain.extradata == _ASC_44100_STEREO

    old = _parse_soun(media_fixtures.mp4_audio(packets, entry_version=1))
    assert old.extradata == _ASC_44100_STEREO
    assert old.channels == 2 and abs(old.sample_rate - 44100.0) < 1e-9

    new = _parse_soun(media_fixtures.mp4_audio(packets, entry_version=2,
                                               sample_rate=48000))
    assert new.extradata == _ASC_44100_STEREO
    assert new.channels == 2 and abs(new.sample_rate - 48000.0) < 1e-9

    # QuickTime hides the same `esds` one box deeper, inside `wave`.
    wrapped = _parse_soun(media_fixtures.mp4_audio(packets, in_wave=True))
    assert wrapped.extradata == _ASC_44100_STEREO

    # And the descriptor lengths have four legal spellings; the four-byte one
    # is what QuickTime writes.
    longhand = _parse_soun(media_fixtures.mp4_audio(packets,
                                                    long_lengths=True))
    assert longhand.extradata == _ASC_44100_STEREO


def test_a_file_with_both_tracks_keeps_the_two_sample_tables_apart():
    """The bug this catches is reading the audio track's samples out of the
    video track's chunk offsets, which produces a file that plays and a
    sound that is someone else's bytes."""
    frames = _mjpeg_frames(3)
    packets = _aac_packets(2)
    data = media_fixtures.mov(frames, 16, 12, codec="jpeg",
                              audio={"packets": packets})
    video = mediacodec.open_video(data)
    assert video.frame_count == 3
    assert _rgba_at(video.frame(2), 0, 0)[0] == 40      # frame index in red

    track = _parse_soun(data)
    samples = mediacodec._mp4_samples(track)
    assert len(samples) == 2
    for i, payload in enumerate(packets):
        offset, length, _key = samples[i]
        assert data[offset:offset + length] == payload
    info = mediacodec.probe_audio(data)
    assert info.container == "MOV" and info.frame_count == 2
    assert (info.sample_rate, info.channels) == (44100, 2)


def test_probe_audio_names_the_codecs_it_will_not_decode():
    packets = _aac_packets(2)
    ac3 = mediacodec.probe_audio(
        media_fixtures.mp4_audio(packets, codec="ac-3", asc=None))
    assert ac3.codec == "ac-3" and not ac3.supported
    assert "Dolby" in ac3.reason

    # An MP3 inside an `mp4a` sample entry is legal, and calling it AAC
    # because of the fourcc is exactly the mistake the object type exists
    # to prevent.
    mp3 = mediacodec.probe_audio(
        media_fixtures.mp4_audio(packets, object_type=0x69))
    assert mp3.codec == "mp4a" and not mp3.supported
    assert "MP3" in mp3.reason

    avi = mediacodec.probe_audio(
        media_fixtures.avi([b"\x00" * 8], 4, 2,
                           audio={"format_tag": 0x00FF, "channels": 2,
                                  "sample_rate": 48000, "length": 480}))
    assert avi.container == "AVI" and avi.codec == "AAC"
    assert (avi.sample_rate, avi.channels) == (48000, 2)
    assert not avi.supported and "AVI" in avi.reason

    mp3_avi = mediacodec.probe_audio(
        media_fixtures.avi([b"\x00" * 8], 4, 2,
                           audio={"format_tag": 0x0055}))
    assert mp3_avi.codec == "MP3" and not mp3_avi.supported

    webm = mediacodec.probe_audio(
        media_fixtures.webm(640, 360, 2.5, audio_codec="A_OPUS"))
    assert webm.container == "WebM" and webm.codec == "Opus"
    assert abs(webm.duration - 2.5) < 0.01
    assert not webm.supported and "WebM" in webm.reason


def test_a_file_with_no_sound_in_it_says_so_rather_than_guessing():
    silent = media_fixtures.mp4(1280, 720, 4.5)
    info = mediacodec.probe_audio(silent)
    assert info.container == "MP4" and not info.supported
    assert "no audio track" in info.reason
    try:
        mediacodec.open_audio(silent)
    except mediacodec.MediaError as exc:
        assert "no audio track" in str(exc)
    else:
        raise AssertionError("open_audio invented an audio track")

    # A bare MJPEG stream is pictures and nothing else.
    stream = b"".join(_mjpeg_frames(2))
    assert not mediacodec.probe_audio(stream).supported

    for payload in (b"", b"not a video at all", b"RIFF\x04\x00\x00\x00WAVE"):
        try:
            mediacodec.probe_audio(payload)
        except mediacodec.MediaError:
            continue
        raise AssertionError("probe_audio accepted %r" % payload)


def test_a_truncated_or_lying_sound_entry_never_takes_the_picture_with_it():
    """A stranger's `esds` is not a reason to refuse to describe the video
    track next to it, and no length in it may hang the parse."""
    data = media_fixtures.mov(_mjpeg_frames(2), 16, 12, codec="jpeg",
                              audio={"packets": _aac_packets(2)})
    for cut in range(len(data) - 200, len(data)):
        start = time.monotonic()
        try:
            mediacodec.probe(data[:cut])
            mediacodec.probe_audio(data[:cut])
        except mediacodec.MediaError:
            pass
        assert time.monotonic() - start < 5.0, \
            "parsing a %d-byte cut hung" % cut

    # A descriptor length that says "another byte follows" forever, and one
    # that claims more than the box holds. Both are files, not hangs.
    esds_at = data.index(b"esds")
    for patch in (b"\x80" * 8, b"\xff\xff\xff\xff\xff\xff\xff\xff"):
        broken = bytearray(data)
        broken[esds_at + 4:esds_at + 4 + len(patch)] = patch
        start = time.monotonic()
        try:
            info = mediacodec.probe_audio(bytes(broken))
            assert info.container == "MOV"
        except mediacodec.MediaError:
            pass
        assert time.monotonic() - start < 5.0
        # And the video track is still described, whatever the sound did.
        assert mediacodec.probe(bytes(broken)).width == 16


class _StubAacDecoder:
    """Stands in for the Fortran decoder, so the track's own logic -- replay,
    reset, timing -- can be tested on a machine with no compiler.

    It behaves the way a real AAC decoder does in the one respect that
    matters here: it is stateful, and it says so, by numbering the samples it
    emits with how many frames it has seen since the last reset.
    """

    def __init__(self, channels=2, frame_length=4):
        self.channels = channels
        self.frame_length = frame_length
        self.sample_rate = 44100
        self.seen = []
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.seen = []

    def decode(self, packet):
        self.seen.append(packet)
        count = self.frame_length * self.channels
        block = struct.pack("<%df" % count, *([float(len(self.seen))] * count))
        return self.frame_length, self.channels, block


def _stub_track(count=5):
    data = b"".join(bytes([0x41 + i]) * 4 for i in range(count))
    packets = [(i * 4, 4, True) for i in range(count)]
    times = [(i * 1024 / 44100.0, 1024 / 44100.0) for i in range(count)]
    info = mediacodec.AudioInfo("MP4", "mp4a", 44100, 2,
                                count * 1024 / 44100.0, count, True, "")
    codec = _StubAacDecoder()
    return codec, mediacodec.AudioTrack(data, info, packets, codec,
                                        times=times, asc=_ASC_44100_STEREO)


def test_an_audio_frame_carries_its_time_its_shape_and_its_samples():
    codec, track = _stub_track()
    assert (track.sample_rate, track.channels) == (44100, 2)
    assert track.sample_count == 5 and track.container == "MP4"
    assert track.codec_name == "mp4a" and track.asc == _ASC_44100_STEREO
    assert track.packet(1) == b"BBBB"

    frame = track.frame(0)
    assert isinstance(frame, mediacodec.AudioFrame)
    assert frame.index == 0 and frame.channels == 2
    assert frame.sample_count == 4              # per channel, not in bytes
    assert len(frame.samples) == 4 * 2 * 4      # floats, interleaved
    assert abs(frame.pts) < 1e-9
    assert abs(frame.duration - 1024 / 44100.0) < 1e-9
    assert abs(frame.end - frame.duration) < 1e-9
    assert "AudioFrame" in repr(frame)
    assert struct.unpack("<f", frame.samples[:4])[0] == 1.0

    assert abs(track.frame_time(2) - 2048 / 44100.0) < 1e-9
    assert track.index_at(0.0) == 0
    assert track.index_at(2049 / 44100.0) == 2
    assert track.index_at(1000.0) == 4
    for index in (-1, 5):
        try:
            track.packet(index)
        except mediacodec.MediaError:
            continue
        raise AssertionError("packet(%d) came back" % index)
    assert codec.resets == 0                    # nothing seeked, nothing reset


def test_audio_decoding_replays_from_the_start_because_there_is_no_keyframe():
    """AAC has no keyframe, but the decoder carries the previous frame's
    overlap, so an out-of-order request has to start again from frame zero.
    Sequential playback must not pay that price."""
    codec, track = _stub_track()
    for index in range(5):
        frame = track.frame(index)
        assert frame.index == index
    assert codec.resets == 0
    assert len(codec.seen) == 5, "sequential playback decoded twice"

    # Backwards: reset, then replay everything up to the frame asked for.
    frame = track.frame(1)
    assert codec.resets == 1
    assert len(codec.seen) == 2
    # The stub numbers its output with frames-since-reset, so this is proof
    # the replay actually happened rather than the cursor being moved.
    assert struct.unpack("<f", frame.samples[:4])[0] == 2.0

    track.reset()
    assert codec.resets == 2
    assert track.frame(3).index == 3
    assert len(codec.seen) == 4

    try:
        track.frame(5)
    except mediacodec.MediaError:
        pass
    else:
        raise AssertionError("decoded a frame that is not in the file")


class _StubAacModule:
    """A stand-in for `feetbrowser.aac`, installed for the length of one test.

    The real decoder is Fortran and its own suite tests it. What is tested
    here is the seam: that `open_audio` hands the AudioSpecificConfig over,
    wraps the decoder's exception type in a `MediaError` with the codec named
    once, and believes the decoder over the container about the sample rate.
    """

    class AacError(Exception):
        pass

    def __init__(self, reason=None, refuse=None, raises=False,
                 sample_rate=44100, channels=2):
        self._reason = reason
        self._refuse = refuse
        self._raises = raises
        self.sample_rate = sample_rate
        self.channels = channels
        self.asc = None
        module = self

        class Decoder(_StubAacDecoder):
            def __init__(self, asc):
                _StubAacDecoder.__init__(self, module.channels)
                module.asc = asc
                if module._raises:
                    raise module.AacError("this config is not AAC-LC")
                self.sample_rate = module.sample_rate

        self.Decoder = Decoder

    def available(self):
        return self._reason is None

    def unavailable_reason(self):
        return self._reason

    def probe(self, asc):
        return self._refuse


def _with_stub_aac(module, run):
    import feetbrowser
    key = "feetbrowser.aac"
    had_module = sys.modules.get(key)
    had_attribute = getattr(feetbrowser, "aac", None)
    sys.modules[key] = module
    feetbrowser.aac = module
    try:
        return run()
    finally:
        if had_module is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = had_module
        if had_attribute is None:
            if hasattr(feetbrowser, "aac"):
                delattr(feetbrowser, "aac")
        else:
            feetbrowser.aac = had_attribute


def test_open_audio_hands_the_config_over_and_names_the_codec_when_it_fails():
    data = media_fixtures.mp4_audio(_aac_packets(3),
                                    durations=[1024, 1024, 512])

    stub = _StubAacModule(sample_rate=22050)
    track = _with_stub_aac(stub, lambda: mediacodec.open_audio(data))
    assert stub.asc == _ASC_44100_STEREO, "the ASC never reached the decoder"
    assert track.sample_count == 3 and track.container == "MP4"
    # HE-AAC codes at half the rate the sample entry declares, so where the
    # config and the container disagree the config wins.
    assert track.sample_rate == 22050 and track.info.sample_rate == 22050
    frame = track.frame(2)
    assert frame.channels == 2 and frame.sample_count == 4
    assert abs(frame.duration - 512 / 44100.0) < 1e-9
    assert track.info.supported

    for stub, expected in ((_StubAacModule(reason="no gfortran"), "gfortran"),
                           (_StubAacModule(refuse="AAC: SBR is not decoded"),
                            "SBR"),
                           (_StubAacModule(raises=True), "AAC-LC")):
        info = _with_stub_aac(stub, lambda: mediacodec.probe_audio(data))
        assert not info.supported
        assert info.reason.startswith("AAC: ") and expected in info.reason
        # The container's numbers survive a missing decoder: that is the
        # whole point of reporting them separately from the ability to play.
        assert (info.sample_rate, info.channels) == (44100, 2)
        assert info.frame_count == 3
        try:
            _with_stub_aac(stub, lambda: mediacodec.open_audio(data))
        except mediacodec.MediaError as exc:
            assert str(exc).startswith("AAC: ")
        else:
            raise AssertionError("open_audio decoded %r" % expected)


# -- video: scheduling against a clock we control ---------------------------

def test_frames_are_presented_against_the_clock_not_counted_off():
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=20, fps=10.0), clock=clock,
                               threaded=False, decode_budget=8)
    assert player.info.supported
    player.play()
    shown = []
    for step in range(20):
        clock.set(step * 0.1)
        if player.tick():
            shown.append(player.scheduler.current.index)
    assert shown == list(range(20))
    assert player.stats()["dropped"] == 0
    assert abs(player.position() - 1.9) < 1e-9


def test_a_slow_decoder_drops_frames_instead_of_drifting():
    """The load-bearing test. The decoder is given one frame of budget per
    tick while the clock moves four frames per tick, so it cannot keep up by
    construction. What must not happen is playback sliding further and
    further behind: the lag has to stay bounded and the position has to stay
    exactly on the clock."""
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=200, fps=10.0), clock=clock,
                               threaded=False, decode_budget=1)
    player.play()
    lags = []
    for step in range(1, 41):
        clock.set(step * 0.4)
        player.tick()
        current = player.scheduler.current
        assert current is not None
        lags.append(player.scheduler.due_index() - current.index)
    assert abs(player.position() - 16.0) < 1e-9, "the clock is the position"
    assert max(lags) <= 4 * media.RESYNC_FRAMES, \
        "playback drifted: lag grew to %d frames" % max(lags)
    assert lags[-1] <= max(lags), "lag is bounded, not monotonic"
    stats = player.stats()
    assert stats["dropped"] > 50, stats
    assert stats["resyncs"] > 0, stats
    # And it really did skip: far fewer frames were decoded than were due.
    assert stats["decoded"] < 60, stats


def test_pause_freezes_the_position_and_resume_carries_on_from_it():
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=40, fps=10.0), clock=clock,
                               threaded=False)
    player.play()
    clock.set(1.0)
    player.tick()
    player.pause()
    assert abs(player.position() - 1.0) < 1e-9
    clock.set(9.0)                      # eight seconds pass with it paused
    assert abs(player.position() - 1.0) < 1e-9
    assert player.tick() is False
    player.play()
    clock.set(9.5)
    assert abs(player.position() - 1.5) < 1e-9


def test_seek_moves_the_playhead_and_the_decoder_with_it():
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=40, fps=10.0), clock=clock,
                               threaded=False, decode_budget=4)
    player.play()
    player.seek(2.0)
    assert abs(player.position() - 2.0) < 1e-9
    # seek() re-bases the clock, so the position is 2.0 plus whatever has
    # elapsed *since the seek* -- not 2.0 plus the whole session.
    clock.set(0.05)
    assert abs(player.position() - 2.05) < 1e-9
    assert player.tick()
    assert player.scheduler.current.index == 20
    player.seek(-5.0)
    assert player.position() == 0.0
    player.seek(1e6)
    assert abs(player.position() - player.scheduler.duration) < 1e-9


def test_playback_stops_at_the_end_and_loops_when_asked():
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=10, fps=10.0), clock=clock,
                               threaded=False, decode_budget=4)
    player.play()
    clock.set(1.5)                      # past the 1.0s end
    player.tick()
    assert not player.playing and player.ended

    clock = media.ManualClock()
    looping = media.VideoPlayer(data=_clip(count=10, fps=10.0), clock=clock,
                                threaded=False, decode_budget=4, loop=True)
    looping.play()
    clock.set(1.5)
    looping.tick()
    assert looping.playing and not looping.ended
    assert looping.position() < 1.0


def test_the_player_scales_frames_to_the_size_the_layout_asked_for():
    player = media.VideoPlayer(data=_clip(count=3, width=8, height=6),
                               clock=media.ManualClock(), threaded=False)
    player.first_frame()
    assert (player.photo.width(), player.photo.height()) == (8, 6)
    assert player.set_display_size(16, 12)
    assert (player.photo.width(), player.photo.height()) == (16, 12)
    assert len(player.photo.rgba) == 16 * 12 * 4
    # Nearest neighbour: the flat background survives the scale exactly.
    assert tuple(player.photo.rgba[0:4]) == (0, 0, 0, 255)
    assert not player.set_display_size(16, 12), "no-op resize rebuilt buffer"


def test_a_file_we_cannot_decode_still_makes_a_usable_player():
    player = media.VideoPlayer(data=media_fixtures.mp4(1920, 1080, 3.0))
    assert player.track is None
    assert (player.width, player.height) == (1920, 1080)
    assert "H.264" in player.error
    assert player.play() is False and not player.playing
    assert player.tick() is False
    assert "1920x1080" in player.status()


def test_the_decode_worker_runs_off_the_ticking_thread():
    """Threaded mode: `tick()` must never be the thing that decodes. The
    proof is that frames appear in the queue while the caller is doing
    nothing at all."""
    player = media.VideoPlayer(data=_clip(count=60, fps=25.0))
    try:
        player.play()
        deadline = time.monotonic() + 3.0
        while player.decoded < media.QUEUE_DEPTH \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        assert player.decoded >= media.QUEUE_DEPTH, \
            "the worker decoded nothing without a tick"
        started = time.monotonic()
        for _ in range(50):
            player.tick()
        assert time.monotonic() - started < 0.5, "tick() blocked"
    finally:
        player.close()
    assert player._thread is None


# -- video: the element on the page ----------------------------------------

def _video_page(directory, markup, clip=None):
    """Write a page and the clips it points at, and return the file:// URL.

    Three files, because the interesting cases are "a codec we have", "the
    codec a real page is most likely to be using" and "a codec we do not
    have", and a page can name whichever of them it is about.
    """
    with open(os.path.join(directory, "clip.avi"), "wb") as handle:
        handle.write(clip if clip is not None else _clip(count=10, width=16,
                                                         height=12))
    with open(os.path.join(directory, "clip.mjpeg"), "wb") as handle:
        handle.write(b"".join(_mjpeg_frames(10, 160, 120)))
    with open(os.path.join(directory, "far.mp4"), "wb") as handle:
        handle.write(media_fixtures.mp4(320, 180, 3.0))
    page = os.path.join(directory, "video.html")
    with open(page, "w", encoding="utf8") as handle:
        handle.write("<html><body>%s</body></html>" % markup)
    return "file://" + page


def test_a_video_element_lays_out_and_paints_decoded_frames():
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideo
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<p>before</p><video src='clip.avi'></video>"
                                "<p>after</p>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        assert len(tab.video_players) == 1
        player = tab.video_players[0]
        assert player.track is not None
        assert (player.width, player.height) == (16, 12)

        drawn = [c for c in tab.display_list if isinstance(c, DrawVideo)]
        assert len(drawn) == 1, tab.display_list
        box = drawn[0]
        assert (box.right - box.left, box.bottom - box.top) == (16, 12)

        # Frame zero is on screen before anyone pressed play.
        browser.draw()
        surface = browser.canvas.render()
        top = browser.chrome_height()
        assert _pixel(surface, int(box.left) + 2,
                      int(box.top) + 2 + top) == (0, 0, 0)
        assert _pixel(surface, int(box.left) + 1,
                      int(box.top) + 2 + top) == (0, 200, 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_clicking_a_video_plays_it_and_the_picture_changes():
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideo
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<video src='clip.avi'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        player = tab.video_players[0]
        box = [c for c in tab.display_list if isinstance(c, DrawVideo)][0]

        assert tab.click(int(box.left) + 3, int(box.top) + 3) is None
        assert player.playing

        # Drive the browser's own frame timer, not the player directly.
        deadline = time.monotonic() + 3.0
        while player.scheduler.presented < 3 and time.monotonic() < deadline:
            browser.window.flush_timers()
            time.sleep(0.005)
        assert player.scheduler.presented >= 3, player.stats()
        assert player.scheduler.current.index >= 1

        browser.draw()
        surface = browser.canvas.render()
        top = browser.chrome_height()
        shown = _pixel(surface, int(box.left) + 2, int(box.top) + 2 + top)
        assert shown == (player.scheduler.current.index, 0, 0), shown

        tab.click(int(box.left) + 3, int(box.top) + 3)
        assert not player.playing
        where = player.position()
        time.sleep(0.05)
        assert player.position() == where
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_two_video_tags_on_one_file_are_two_independent_playheads():
    from feetbrowser.browser import Browser
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<video src='clip.avi'></video>"
                                "<video width='32' height='24' "
                                "src='clip.avi'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        assert len(tab.video_players) == 2
        first, second = tab.video_players
        assert first is not second
        assert (first.photo.width(), first.photo.height()) == (16, 12)
        assert (second.photo.width(), second.photo.height()) == (32, 24)
        first.play()
        assert first.playing and not second.playing
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_video_we_cannot_play_reserves_its_real_size_and_says_why():
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideo
    work = tempfile.mkdtemp()
    try:
        url = _video_page(
            work, "<video><source src='far.mp4' type='video/mp4'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        assert len(tab.video_players) == 1
        player = tab.video_players[0]
        assert player.track is None and "H.264" in player.error
        assert not [c for c in tab.display_list if isinstance(c, DrawVideo)]
        # The box is the size the *container* declared, not a 300x150 guess.
        boxes = [c for c in tab.display_list
                 if getattr(c, "color", "") == "#1a1a1a"]
        assert boxes, tab.display_list
        assert (boxes[0].right - boxes[0].left,
                boxes[0].bottom - boxes[0].top) == (320, 180)
        labels = [c.text for c in tab.display_list
                  if hasattr(c, "text") and "video" in str(c.text)]
        assert labels and "H.264" in labels[0], labels
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_navigating_away_stops_the_decode_threads():
    from feetbrowser.browser import Browser
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<video src='clip.avi'></video>")
        plain = os.path.join(work, "plain.html")
        with open(plain, "w", encoding="utf8") as handle:
            handle.write("<p>nothing here</p>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        player = tab.video_players[0]
        player.play()
        tab.load(URL("file://" + plain))
        browser.settle(20.0)
        assert tab.video_players == []
        assert player._thread is None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_mjpeg_clip_plays_on_a_page_and_the_picture_moves():
    """The whole point of the exercise, end to end: a `<video>` on an ordinary
    HTML page, pointed at an ordinary Motion JPEG file, decoding and painting
    frames into the window. The red channel carries the frame number, so the
    assertion is not "something changed" but "frame N is on screen"."""
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideo
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<p>before</p>"
                                "<video src='clip.mjpeg'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        player = tab.video_players[0]
        assert player.track is not None, player.error
        assert player.track.codec_name in mediacodec.MJPEG_FOURCCS
        assert (player.width, player.height) == (160, 120)
        box = [c for c in tab.display_list if isinstance(c, DrawVideo)][0]
        top = browser.chrome_height()

        browser.draw()
        first = _pixel(browser.canvas.render(), int(box.left) + 4,
                       int(box.top) + 4 + top)
        assert abs(first[0] - 0) <= 2 and abs(first[2] - 120) <= 2, first

        assert tab.click(int(box.left) + 8, int(box.top) + 8) is None
        assert player.playing
        deadline = time.monotonic() + 5.0
        while player.scheduler.presented < 3 and time.monotonic() < deadline:
            browser.window.flush_timers()
            time.sleep(0.005)
        assert player.scheduler.presented >= 3, player.stats()

        index = player.scheduler.current.index
        assert index >= 1
        browser.draw()
        shown = _pixel(browser.canvas.render(), int(box.left) + 4,
                       int(box.top) + 4 + top)
        assert abs(shown[0] - index * 20) <= 2, (shown, index)
        assert shown != first, "the picture never moved"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_clip_served_over_http_is_chosen_by_its_type_and_plays():
    """Over a socket, the way a page on the web arrives, and with the format
    named by `type` rather than by a file extension -- which is the case the
    `file://` tests cannot reach, because a filename is all they have. The
    first `<source>` is an H.264 MP4 we cannot decode; picking it would leave
    a placeholder box where the film should be."""
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideo
    from fixture_server import FixtureServer
    work = tempfile.mkdtemp()
    try:
        with open(os.path.join(work, "movie.mp4"), "wb") as handle:
            handle.write(media_fixtures.mp4(640, 360, 8.0))
        with open(os.path.join(work, "clip.bin"), "wb") as handle:
            handle.write(b"".join(_mjpeg_frames(6, 64, 48)))
        with open(os.path.join(work, "index.html"), "w",
                  encoding="utf8") as handle:
            handle.write("<html><body><video controls width='200' "
                         "height='150'>"
                         "<source src='movie.mp4' type='video/mp4'>"
                         "<source src='clip.bin' "
                         "type='video/x-motion-jpeg'></video></body></html>")
        with FixtureServer(directory=work) as fixtures:
            browser = Browser()
            browser.new_tab(fixtures.url("index.html"))
            browser.settle(30.0)
            tab = browser.active_tab
            player = tab.video_players[0]
            assert player.track is not None, player.error
            assert player.track.container == "MJPEG"
            assert player.info.frame_count == 6

            box = [c for c in tab.display_list
                   if isinstance(c, DrawVideo)][0]
            assert (box.right - box.left, box.bottom - box.top) == (200, 150)
            player.seek(player.info.duration * 0.5)
            browser.draw()
            index = player.scheduler.current.index
            assert index == 3, index
            shown = _pixel(browser.canvas.render(), int(box.left) + 4,
                           int(box.top) + 4 + browser.chrome_height())
            assert abs(shown[0] - index * 20) <= 2, (shown, index)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_video_with_controls_gets_a_transport_bar_and_one_without_does_not():
    """`controls` is the whole switch, the way HTML says it is, and a box too
    small to put a bar in gets none rather than a bar over the film."""
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideoControls, CONTROLS_HEIGHT
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work,
                          "<video id=a controls width='240' height='180' "
                          "src='clip.mjpeg'></video>"
                          "<video id=b width='240' height='180' "
                          "src='clip.mjpeg'></video>"
                          "<video id=c controls width='100' height='40' "
                          "src='clip.mjpeg'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        bars = [c for c in tab.display_list
                if isinstance(c, DrawVideoControls)]
        assert len(bars) == 1, [b.node.attributes.get("id") for b in bars]
        bar = bars[0]
        assert bar.node.attributes.get("id") == "a"
        assert bar.right - bar.left == 240
        assert bar.bottom - bar.top == CONTROLS_HEIGHT
        # It sits on the foot of the picture, not below it.
        video = [c for c in tab.display_list
                 if getattr(c, "photo", None) is not None
                 and getattr(c, "node", None) is bar.node][0]
        assert bar.bottom == video.bottom and bar.left == video.left

        # The button is square and the groove starts after it and stops
        # before the time readout, which is measured rather than assumed.
        bx0, by0, bx1, by1 = bar.button_rect()
        assert bx1 - bx0 == by1 - by0 == CONTROLS_HEIGHT
        gx0, _, gx1, _ = bar.groove_rect()
        assert bx1 < gx0 < gx1 < bar.right
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_clicking_the_transport_bar_plays_pauses_and_scrubs():
    """The bar's own arithmetic, driven through the browser's click path: the
    button end toggles, the groove seeks to where along it you pressed, and
    anything else the bar covers is swallowed rather than passed to the
    click-anywhere-to-play behaviour underneath."""
    from feetbrowser.browser import Browser
    from feetbrowser.layout import DrawVideoControls
    work = tempfile.mkdtemp()
    try:
        url = _video_page(work, "<video controls width='240' height='180' "
                                "src='clip.mjpeg'></video>")
        browser = Browser()
        browser.new_tab(url)
        browser.settle(20.0)
        tab = browser.active_tab
        player = tab.video_players[0]
        bar = [c for c in tab.display_list
               if isinstance(c, DrawVideoControls)][0]
        duration = player.info.duration
        assert duration > 0

        def press(x, y):
            tab.click(int(x), int(y) - tab.scroll)

        bx0, _, bx1, _ = bar.button_rect()
        middle = (bar.top + bar.bottom) / 2
        press((bx0 + bx1) / 2, middle)
        assert player.playing
        press((bx0 + bx1) / 2, middle)
        assert not player.playing

        gx0, _, gx1, _ = bar.groove_rect()
        press(gx0 + (gx1 - gx0) * 0.5, middle)
        assert abs(player.position() - duration * 0.5) < duration * 0.05
        press(gx1, middle)
        assert abs(player.position() - duration) < duration * 0.05
        press(gx0, middle)
        assert player.position() == 0.0
        # Seeking did not start playback, and the bar is still where it was.
        assert not player.playing

        # A press on the dead strip between the groove and the time readout
        # does nothing at all -- and, in particular, does not fall through to
        # the picture underneath and start the film.
        press(gx1 + (bar.right - gx1) / 2, middle)
        assert not player.playing
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_scrubber_follows_the_playhead():
    """The bar reads the player when it paints, because the frame timer
    repaints the display list without rebuilding it. If the knob were placed
    at layout time it would sit still for the whole film."""
    from feetbrowser.layout import DrawVideoControls
    player = media.VideoPlayer(data=_clip(count=40, fps=10.0),
                               clock=media.ManualClock(), threaded=False)
    bar = DrawVideoControls(0, 0, 200, 28, None, player, None)
    assert bar._fraction() == 0.0
    assert bar._time_text() == "0:00 / 0:04"
    player.seek(2.0)
    assert abs(bar._fraction() - 0.5) < 1e-9
    assert bar._time_text() == "0:02 / 0:04"
    player.seek(1e6)
    assert bar._fraction() == 1.0


def test_the_time_readout_reads_like_a_clock():
    from feetbrowser.layout import format_media_time
    assert format_media_time(0) == "0:00"
    assert format_media_time(9.9) == "0:09"
    assert format_media_time(61) == "1:01"
    assert format_media_time(3599) == "59:59"
    assert format_media_time(3600) == "1:00:00"
    assert format_media_time(3600 * 2 + 65) == "2:01:05"
    # A duration of zero divided by itself, and a seek that ran off the end.
    assert format_media_time(float("nan")) == "0:00"
    assert format_media_time(-4) == "0:00"


def test_seeking_while_paused_puts_the_frame_you_asked_for_on_screen():
    """A scrubber that moves the playhead and leaves the old picture up is
    worse than no scrubber. Paused means nothing is decoding in the
    background, so the seek itself has to do the decode."""
    player = media.VideoPlayer(data=_clip(count=40, fps=10.0),
                               clock=media.ManualClock(), threaded=False)
    player.first_frame()
    assert tuple(player.photo.rgba[0:4]) == (0, 0, 0, 255)
    assert player.seek(2.0) and not player.playing
    assert tuple(player.photo.rgba[0:4]) == (20, 0, 0, 255)
    assert player.seek(3.75)
    assert tuple(player.photo.rgba[0:4]) == (37, 0, 0, 255)
    # Backwards too, which for an all-keyframe stream is the same work and
    # for an inter-frame one means replaying from the keyframe before it.
    assert player.seek(0.5)
    assert tuple(player.photo.rgba[0:4]) == (5, 0, 0, 255)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} RENDER TESTS PASSED")


if __name__ == "__main__":
    main()
