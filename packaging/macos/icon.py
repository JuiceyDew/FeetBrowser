"""Make FeetBrowser.app's icon from the one artwork file.

There is no second, hand-kept copy of the icon anywhere: the artwork lives in
`packaging/art/feet.png` and every platform's icon is a resample of it, this
one being the PNGs that `iconutil` turns into the app's `.icns`. The 16pt
icon is a four-pixel-wide toe, and area-average resampling -- the coverage of
each destination pixel across the source -- is exactly what keeps it legible
instead of the one-in-every-N pixels a nearest pick would give it.

Pure standard library. macOS packaging runs on the system Python, which has
no way to import this project's engine, so this script may not either.

    python3 packaging/macos/icon.py out.iconset
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "art"))

from feet_icon import encode_png, resample, source  # noqa: E402

# The sizes `iconutil` wants, as (pixels, filename).
ICONSET = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "FeetBrowser.iconset"
    width, height, rgba = source()
    os.makedirs(out, exist_ok=True)
    drawn = {}
    for size, name in ICONSET:
        if size not in drawn:
            drawn[size] = resample(width, height, rgba, size)
        encode_png(os.path.join(out, name), size, drawn[size])
    print("wrote %d PNGs to %s" % (len(ICONSET), out))


if __name__ == "__main__":
    main()
