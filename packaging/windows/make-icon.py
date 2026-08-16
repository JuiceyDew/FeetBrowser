#!/usr/bin/env python3
"""Draw launcher/resources/FeetBrowser.ico.

The icon is a binary file that has to be in the repository -- rc.exe wants a
real .ico at link time, and a build that fetched its own icon from somewhere
would be a build that could not run offline. This is the thing that made it,
so the blob is reviewable: change the artwork in `packaging/art/feet.png`,
re-run, and the diff on the .ico is explained.

Pure standard library on purpose. It is the same rule the browser lives by,
and it is why the artwork is a PNG this script decodes itself rather than a
format only a drawing program could read: nothing outside CPython is needed
to reproduce the .ico.

    python3 packaging/windows/make-icon.py

Windows Vista and later read PNG-compressed icon entries at every size, and
the bundle needs Windows 10 anyway, so every entry here is a PNG.
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "art"))
import feet_icon  # noqa: E402

# One size per entry, covering what Explorer, the taskbar, the Alt-Tab
# switcher and the file properties dialog each ask for.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def ico(images):
    """ICONDIR + one ICONDIRENTRY per image, then the PNG payloads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payloads = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,   # 0 means 256 in an .ico
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset)
        payloads += data
        offset += len(data)
    return header + entries + payloads


def main():
    out = os.path.join(HERE, "launcher", "resources", "FeetBrowser.ico")
    width, height, rgba = feet_icon.source()
    images = []
    for size in SIZES:
        sys.stderr.write("  %dx%d\n" % (size, size))
        data = feet_icon.png_bytes(size,
                                   feet_icon.resample(width, height, rgba, size))
        images.append((size, data))
    blob = ico(images)
    with open(out, "wb") as f:
        f.write(blob)
    sys.stderr.write("wrote %s (%d bytes, %d sizes)\n"
                     % (out, len(blob), len(images)))


if __name__ == "__main__":
    main()
