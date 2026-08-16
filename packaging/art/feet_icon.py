"""The one true icon, and the tooling that turns it into every platform's file.

The repository used to draw its icons from polygons, a footprint assembled out
of ellipses, so that the blob in the tree was reproducible with nothing but
the browser's own rasteriser. It still needs each platform's icon to be
reviewable -- the Windows .ico is a binary that rc.exe requires to be real --
so the artwork now lives here as `feet.png`, an ordinary RGBA PNG, and every
packaging script in this directory tree decodes it through this module and
resamples it to the sizes the platform asks for.

Pure standard library on purpose, and it has to be: `make-icon.py` runs on a
plain CPython where the browser's engine does not exist, so nothing here may
import the project. That also means the artwork is one file, `feet.png`, and
there is no second, differently-scaled copy of it anywhere.

    python3 -c "import feet_icon; print(feet_icon.source())"
"""

import os
import struct
import sys
import zlib

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feet.png")

# Boxes the platform targets draw from: Windows Explorer, the taskbar and the
# Alt-Tab switcher; the Linux hicolor theme; the macOS iconset. The artwork is
# 256 square, so anything larger is nearest-resampled and stays blocky, which
# is what a real 256px asset deserves.
_DEPTHS = {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}  # PNG colour type -> channels


class IconError(Exception):
    """A bad source file, or a size this module will not produce."""


def _inflate_idat(width, height, channels, raw):
    """Unfilter a deflate stream back into scanline rows."""
    bpp = channels
    stride = width * channels
    data = bytearray(zlib.decompress(raw))
    rows = []
    at = 0
    prev = bytearray(stride)
    for _ in range(height):
        if at >= len(data):
            raise IconError("short PNG scanline data")
        f, line = data[at], data[at + 1:at + 1 + stride]
        at += 1 + stride
        if len(line) != stride:
            raise IconError("ragged PNG scanline")
        out = bytearray(line)
        if f == 1:      # Sub: each byte adds the one `bpp` to its left
            for i in range(bpp, stride):
                out[i] = (out[i] + out[i - bpp]) & 0xFF
        elif f == 2:    # Up: each byte adds the one above it
            for i in range(stride):
                out[i] = (out[i] + prev[i]) & 0xFF
        elif f == 3:    # Average: add the mean of left and above
            for i in range(stride):
                left = out[i - bpp] if i >= bpp else 0
                out[i] = (out[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:    # Paeth: add the least-error of the three neighbours
            for i in range(stride):
                a = out[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                guess = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out[i] = (out[i] + guess) & 0xFF
        elif f != 0:
            raise IconError("unknown PNG filter %d" % f)
        rows.append(out)
        prev = out
    return rows


def decode_png(path):
    """`(width, height, rgba)` for an 8-bit, non-interlaced RGBA PNG."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise IconError("%s is not a PNG" % path)
    at = 8
    width = height = depth = colour = None
    idat = bytearray()
    while at < len(data):
        if at + 8 > len(data):
            raise IconError("truncated PNG chunk")
        size, = struct.unpack(">I", data[at:at + 4])
        kind = data[at + 4:at + 8]
        body = data[at + 8:at + 8 + size]
        at += 12 + size
        if kind == b"IHDR":
            width, height, depth, colour, comp, filt, inter = struct.unpack(
                ">IIBBBBB", body)
            if depth != 8 or comp or filt or inter:
                raise IconError("unsupported PNG: 8-bit, non-interlaced only")
            if colour not in _DEPTHS:
                raise IconError("unsupported PNG colour type %d" % colour)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if width is None or height is None:
        raise IconError("PNG with no IHDR")
    channels = _DEPTHS[colour]
    rows = _inflate_idat(width, height, channels, bytes(idat))
    rgba = bytearray()
    for row in rows:
        for i in range(width):
            base = i * channels
            if colour == 6:                 # RGBA
                rgba += bytes(row[base:base + 4])
            elif colour == 4:               # greyscale + alpha
                rgba += bytes((row[base],) * 3) + bytes((row[base + 1],))
            elif colour == 0:               # greyscale
                rgba += bytes((row[base],) * 3) + b"\xff"
            else:                           # RGB
                rgba += bytes(row[base:base + 3]) + b"\xff"
    return width, height, bytes(rgba)


def resample(width, height, rgba, size):
    """`rgba` scaled to `size` x `size`.

    Shrinking is an area average -- the coverage of each destination pixel
    across the source -- so a 16px icon is a fair sample of the whole mark,
    not one in every N pixels. Growing is nearest-neighbour: there is no new
    information to invent, so the extra pixels repeat the ones that exist.
    """
    rgba = memoryview(rgba)
    if size < 1:
        raise IconError("icon size must be >= 1")
    if (size, size) == (width, height):
        return bytes(rgba)
    out = bytearray(size * size * 4)
    if size < width and size < height:
        for dy in range(size):
            y0, y1 = dy * height / size, (dy + 1) * height / size
            ys = range(int(y0), int(y1))
            for dx in range(size):
                x0, x1 = dx * width / size, (dx + 1) * width / size
                xs = range(int(x0), int(x1))
                if not xs or not ys:
                    continue
                r = g = b = a = 0.0
                for sy in ys:
                    row = sy * width
                    for sx in xs:
                        i = (row + sx) * 4
                        alpha = rgba[i + 3]
                        r += rgba[i] * alpha
                        g += rgba[i + 1] * alpha
                        b += rgba[i + 2] * alpha
                        a += alpha
                n = len(xs) * len(ys)
                if a:
                    d = (dy * size + dx) * 4
                    out[d] = int(r / a)
                    out[d + 1] = int(g / a)
                    out[d + 2] = int(b / a)
                    out[d + 3] = int(a / n)
    else:
        for dy in range(size):
            sy = min(height - 1, dy * height // size)
            for dx in range(size):
                sx = min(width - 1, dx * width // size)
                s = (sy * width + sx) * 4
                d = (dy * size + dx) * 4
                out[d:d + 4] = rgba[s:s + 4]
    return bytes(out)


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + \
        struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(size, rgba):
    """`rgba` as an 8-bit RGBA PNG, returned as bytes."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        rows += rgba[y * size * 4:(y + 1) * size * 4]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))


def encode_png(path, size, rgba):
    """Write `rgba` as an 8-bit RGBA PNG at `path`."""
    with open(path, "wb") as f:
        f.write(png_bytes(size, rgba))


def source():
    """The artwork as a `(width, height, rgba)` triple, cached."""
    if not hasattr(source, "_cached"):
        source._cached = decode_png(ART)
    return source._cached


if __name__ == "__main__":
    w, h, _ = source()
    sys.stdout.write("artwork %s: %dx%d RGBA\n" % (ART, w, h))
