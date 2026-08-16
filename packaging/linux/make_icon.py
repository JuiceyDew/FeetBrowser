"""Make the icons the Linux build installs, from the one artwork file.

The artwork lives once, as `packaging/art/feet.png`, and every platform's icon
is a resample of it: the Windows .ico, the macOS iconset, and these PNGs for
the hicolor theme. Nothing here draws anything -- there is no geometry to keep
in step with the art, because the art is the source of truth.

Pure standard library. The icon step used to double as a smoke test that the
newly built interpreter and rasteriser worked together; it now reads a PNG,
so the build proves itself elsewhere. This script has to run on the plain
CPython that made the rest of the bundle, which is what the standard-library
rule buys.

    python3 packaging/linux/make_icon.py OUTDIR [size ...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "art"))

from feet_icon import encode_png, resample, source  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: make_icon.py OUTDIR [size ...]")
    outdir = sys.argv[1]
    sizes = [int(a) for a in sys.argv[2:]] or [256, 128, 64, 48]
    width, height, rgba = source()
    os.makedirs(outdir, exist_ok=True)
    for size in sizes:
        path = os.path.join(outdir, "feetbrowser-%d.png" % size)
        encode_png(path, size, resample(width, height, rgba, size))
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


if __name__ == "__main__":
    main()
