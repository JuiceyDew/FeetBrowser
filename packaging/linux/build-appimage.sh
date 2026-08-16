#!/usr/bin/env bash
# Build the FeetBrowser AppImage. Runs *inside* the manylinux container --
# see build.sh, which is what puts it there.
#
# The container is not incidental. A binary linked on a current Ubuntu quietly
# requires that Ubuntu's glibc and refuses to start on anything older, which is
# the standard way a Linux bundle works on the machine that built it and
# nowhere else. manylinux_2_28 is AlmaLinux 8, glibc 2.28, which is older than
# every distribution still receiving updates; build here and the floor is the
# oldest thing anyone is running rather than the newest thing CI happens to
# have. The last step measures that floor off the shipped binaries rather than
# taking this paragraph's word for it.
#
# What goes in the bundle:
#
#   a CPython, compiled here from the upstream tarball
#   the feetbrowser package, from this checkout
#   feetbrowser_engine, compiled here against that CPython
#   the private shared libraries CPython needs (libssl, libffi, ...)
#   DejaVu, so a machine with no fonts still renders text
#   a CA bundle, as a fallback for a machine with no trust store
#
# What does not, and why:
#
#   libX11 and everything under it. x11.py dlopens libX11.so.6 through
#   ctypes, so the bundle has to find one at runtime -- but it should be the
#   host's. Every machine with an X server has libX11 (it is a dependency of
#   the server's own clients), the X protocol is stable across decades, and
#   the libraries beneath it -- libxcb, and on a real desktop the GLX and
#   driver stack that gets dlopened behind them -- are matched to the running
#   server. Shipping our own copies of half of that and letting the loader
#   mix them with the host's other half is a well-known way to produce
#   BadRequest errors and driver crashes on hardware we cannot test on. So:
#   host's X libraries, ours for everything the browser itself is made of.
set -euo pipefail

PY_VERSION="${PY_VERSION:-3.12.11}"
PY_SHORT="${PY_VERSION%.*}"          # 3.12
SRC="${SRC:-/io}"                    # the checkout, read-write mounted
WORK="${WORK:-/build}"               # everything we make, never under $HOME
OUT="${OUT:-$SRC/dist}"
PREFIX="$WORK/python"
APPDIR="$WORK/FeetBrowser.AppDir"
SITE="$APPDIR/usr/lib/python$PY_SHORT/site-packages"
ARCH="${ARCH:-x86_64}"
export ARCH

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SRC/feetbrowser/__init__.py")
[ -n "$VERSION" ] || { echo "cannot read __version__" >&2; exit 1; }

step() { printf '\n=== %s ===\n' "$1"; }

# -- 1. build-time packages -------------------------------------------------
#
# All of these are development packages and compilers. None of them is in the
# bundle: what ships is the .so files they produced, listed one by one below.
step "build dependencies"
dnf install -y -q \
  openssl-devel libffi-devel bzip2-devel xz-devel zlib-devel \
  gcc-gfortran \
  dejavu-sans-fonts dejavu-serif-fonts dejavu-sans-mono-fonts \
  ca-certificates patchelf file findutils >/dev/null

# -- 2. CPython, from source ------------------------------------------------
#
# From source rather than from a prebuilt relocatable distribution. A prebuilt
# one would be quicker and is a perfectly respectable choice, but it is a
# third-party binary shipped inside a project whose entire premise is owning
# its stack, and it would decide for us which OpenSSL, which module set and
# which glibc floor we get. Compiling it here costs about four minutes and
# means every byte of the interpreter came out of the upstream tarball and
# this container.
#
# Relocation works because CPython finds its prefix by walking up from the
# executable looking for lib/pythonX.Y/os.py, so the tree can be moved
# anywhere as long as bin/ and lib/ keep their relative positions -- which is
# exactly the layout an AppDir wants. AppRun sets PYTHONHOME as well, so a
# stray PYTHONHOME in the user's shell cannot redirect us at their stdlib.
step "CPython $PY_VERSION"
mkdir -p "$WORK"
cd "$WORK"
curl -fsSLO "https://www.python.org/ftp/python/$PY_VERSION/Python-$PY_VERSION.tar.xz"
tar xf "Python-$PY_VERSION.tar.xz"
cd "Python-$PY_VERSION"
# No --enable-optimizations: PGO triples the build for a gain the browser
# cannot see, because every hot loop it has is in the Rust extension.
./configure --prefix="$PREFIX" \
            --with-system-ffi \
            --with-ensurepip=no \
            --disable-test-modules \
            --without-static-libpython >/dev/null
make -j"$(nproc)" >/dev/null
make install >/dev/null
cd "$WORK"

PY="$PREFIX/bin/python$PY_SHORT"
"$PY" -c 'import ssl, ctypes, zlib, socket; print("stdlib ok:", ssl.OPENSSL_VERSION)'

# -- 3. the engine ----------------------------------------------------------
step "feetbrowser_engine"
if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable >/dev/null
fi
export PATH="$HOME/.cargo/bin:/opt/python/cp312-cp312/bin:$PATH"
pip install --quiet maturin
# Built for the interpreter we just made, not for the container's.
maturin build --release --manifest-path "$SRC/rust/Cargo.toml" \
        --out "$WORK/wheel" -i "$PY" >/dev/null
WHEEL=$(ls "$WORK"/wheel/*.whl | head -1)
echo "wheel: $(basename "$WHEEL")"

# -- 4. assemble the AppDir -------------------------------------------------
step "AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr" "$SITE" "$APPDIR/usr/lib/feetbrowser" \
         "$APPDIR/usr/share/feetbrowser/fonts" \
         "$APPDIR/usr/share/feetbrowser/ca" \
         "$APPDIR/usr/share/applications"
cp -a "$PREFIX/." "$APPDIR/usr/"

# Everything the browser will never open. The test suite alone is a third of
# the stdlib on disk, and tkinter is not merely unused -- this project removed
# it on purpose.
STDLIB="$APPDIR/usr/lib/python$PY_SHORT"
rm -rf "$STDLIB/test" "$STDLIB/idlelib" "$STDLIB/tkinter" "$STDLIB/turtledemo" \
       "$STDLIB/lib2to3" "$STDLIB/ensurepip" "$STDLIB/pydoc_data" \
       "$STDLIB/config-$PY_SHORT"*
rm -f "$STDLIB/turtle.py" "$STDLIB/lib-dynload/_tkinter"*.so
rm -rf "$APPDIR/usr/include" "$APPDIR/usr/share/man" "$APPDIR/usr/lib/pkgconfig"
rm -f "$APPDIR/usr/lib/libpython"*.a
rm -f "$APPDIR/usr/bin/idle"* "$APPDIR/usr/bin/2to3"* \
      "$APPDIR/usr/bin/pydoc"* "$APPDIR/usr/bin/python"*-config
# python3-config is a shell script with the build prefix written into it.
rm -f "$APPDIR/usr/bin"/*-config

# The browser itself.
(cd "$SRC" && tar cf - --exclude='__pycache__' --exclude='*.pyc' feetbrowser) \
  | (cd "$SITE" && tar xf -)
# toes.repo_root() is the directory above the package, which inside the bundle
# is site-packages. discover_toes() returns nothing when toes/ is missing, so
# this only exists so the shipped copy looks like the checkout does.
mkdir -p "$SITE/toes"
cp "$SRC/toes/README.md" "$SITE/toes/README.md"

# The engine. A wheel is a zip; unzipping it is what pip would do here, and
# there is no pip in this interpreter on purpose.
"$PY" -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
      "$WHEEL" "$SITE"
rm -rf "$SITE"/*.dist-info

cp "$SRC/packaging/linux/launcher.py" "$APPDIR/usr/lib/feetbrowser/launcher.py"

# The two Fortran decoders. feetbrowser/h264.py and feetbrowser/aac.py compile
# fortran/ with gfortran the first time a video or a soundtrack plays, which
# is fine in a checkout and impossible in an AppImage: the image is read-only
# and the user has no compiler. Left alone the browser starts, renders, and
# says "[video: H.264: no gfortran on PATH]" to anyone who opens a video -- a
# failure no developer ever sees, because developers have gfortran.
#
# Both of them, because shipping only the video half produces an AppImage
# that plays pictures in silence, which reads as a broken player rather than
# as a missing decoder.
#
# So they are compiled here and shipped inside the package, each under a name
# that is a hash of the sources it was built from; the sources ship beside the
# package so the loader can recompute those hashes and refuse a mismatch. This
# runs before the private-library step on purpose: if gfortran's runtime could
# not be linked in statically -- manylinux_2_28's libgfortran.a is not built
# -fPIC and cannot go into a shared object at all -- what is left is an
# ordinary NEEDED entry, which collect() will bundle and the rpath pass will
# point at $ORIGIN, and either way nothing outside the image is required.
step "the Fortran decoders"
(cd "$SRC" && tar cf - fortran) | (cd "$SITE" && tar xf -)
H264=$(PYTHONPATH="$SITE" "$PY" -m feetbrowser.h264 --name)
PYTHONPATH="$SITE" "$PY" -m feetbrowser.h264 --build "$SITE/feetbrowser/$H264"
AAC=$(PYTHONPATH="$SITE" "$PY" -m feetbrowser.aac --name)
PYTHONPATH="$SITE" "$PY" -m feetbrowser.aac --build "$SITE/feetbrowser/$AAC"
for lib in "$H264" "$AAC"; do
  file "$SITE/feetbrowser/$lib"
  # What it still needs, printed here and dealt with by collect() below. A
  # NEEDED libgfortran.so.5 in this list is expected on manylinux and is not
  # a failure; anything left unresolved after the rpath pass would be, and
  # the self-test at step 9 is what would catch it.
  ldd "$SITE/feetbrowser/$lib"
done

# -- 5. fonts ---------------------------------------------------------------
#
# canvas._resolve_face falls back through three chains (times, helvetica,
# courier) and raises CanvasError if none of them resolves, so a machine with
# no fonts does not render blank boxes -- it crashes. DejaVu Serif, Sans and
# Sans Mono are one entry in each of the three chains, so these six files are
# the smallest set that makes every generic family resolve. TrueType outlines
# only: fontengine._scan skips CFF fonts, which it can measure but not draw.
step "fonts"
for f in DejaVuSerif.ttf DejaVuSerif-Bold.ttf \
         DejaVuSans.ttf DejaVuSans-Bold.ttf DejaVuSans-Oblique.ttf \
         DejaVuSansMono.ttf DejaVuSansMono-Bold.ttf; do
  src=$(find /usr/share/fonts -name "$f" -print -quit)
  [ -n "$src" ] || { echo "missing font $f" >&2; exit 1; }
  cp "$src" "$APPDIR/usr/share/feetbrowser/fonts/"
done
cp /usr/share/doc/dejavu-fonts-common/LICENSE \
   "$APPDIR/usr/share/feetbrowser/fonts/LICENSE" 2>/dev/null || \
  find /usr/share/licenses /usr/share/doc -ipath '*dejavu*' -name 'LICENSE*' \
       -exec cp {} "$APPDIR/usr/share/feetbrowser/fonts/LICENSE" \; -quit

# -- 6. certificates --------------------------------------------------------
#
# AppRun prefers the host's trust store and only reaches for this when the
# machine has none, which is the case on a minimal container or a stripped
# install. Without a fallback the symptom is one https:// page failing while
# everything else about the browser looks perfect.
step "certificates"
cp /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
   "$APPDIR/usr/share/feetbrowser/ca/cacert.pem"

# -- 7. private shared libraries --------------------------------------------
#
# Whatever the interpreter and its extension modules link that is not part of
# glibc or the compiler runtime: OpenSSL, libffi, zlib and friends. They go
# next to the interpreter and are reached through an RPATH of $ORIGIN, so
# nothing else on the system can see them and they cannot see anything else.
#
# The exclusion list is the set every glibc machine has and that must not be
# doubled: swapping a process's libc or its unwinder for an older copy is
# worse than any version skew it could fix.
step "private libraries"
LIBDIR="$APPDIR/usr/lib"
EXCLUDE='^(linux-vdso|ld-linux|libc|libm|libdl|libpthread|librt|libutil|libnsl|libresolv|libgcc_s|libstdc\+\+|libcrypt)\.'
collect() {
  ldd "$1" 2>/dev/null | awk '/=> \//{print $1, $3}' | while read -r soname path; do
    if echo "$soname" | grep -Eq "$EXCLUDE"; then continue; fi
    case "$path" in "$APPDIR"/*) continue ;; esac
    if [ ! -e "$LIBDIR/$soname" ]; then
      cp -L "$path" "$LIBDIR/$soname"
      echo "  + $soname"
      collect "$LIBDIR/$soname"
    fi
  done
}
collect "$APPDIR/usr/bin/python$PY_SHORT"
find "$STDLIB/lib-dynload" "$SITE" -name '*.so' -print0 \
  | while IFS= read -r -d '' so; do collect "$so"; done

step "rpaths"
# Every ELF file in the bundle points at usr/lib through $ORIGIN, computed
# from where the file actually sits. An absolute RPATH would be a build path
# baked into a shipped binary, which is both wrong on the user's machine and
# a place a build directory's name can leak out of.
"$PY" - "$APPDIR" <<'RPATH'
import os, subprocess, sys
appdir = sys.argv[1]
libdir = os.path.join(appdir, "usr", "lib")
targets = []
for root, _dirs, files in os.walk(appdir):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as fh:
            if fh.read(4) != b"\x7fELF":
                continue
        targets.append(path)
for path in targets:
    rel = os.path.relpath(libdir, os.path.dirname(path))
    origin = "$ORIGIN" if rel == "." else os.path.join("$ORIGIN", rel)
    subprocess.run(["patchelf", "--set-rpath", origin, path], check=True)
print("set $ORIGIN rpaths on %d ELF files" % len(targets))
RPATH

# -- 8. icon, desktop entry, AppRun ----------------------------------------
step "icon"
ICONS="$APPDIR/usr/share/icons/hicolor"
# Resampled from the one artwork file, packaging/art/feet.png, which is the
# same source the Windows .ico and the macOS iconset come from. The script is
# pure standard library, so it runs on the plain CPython building the bundle.
PYTHONPATH="$SITE" "$PY" "$SRC/packaging/linux/make_icon.py" "$WORK/icons"
for size in 256 128 64 48; do
  mkdir -p "$ICONS/${size}x${size}/apps"
  cp "$WORK/icons/feetbrowser-$size.png" "$ICONS/${size}x${size}/apps/feetbrowser.png"
done
cp "$ICONS/256x256/apps/feetbrowser.png" "$APPDIR/feetbrowser.png"
cp "$APPDIR/feetbrowser.png" "$APPDIR/.DirIcon"

cp "$SRC/packaging/linux/feetbrowser.desktop" "$APPDIR/feetbrowser.desktop"
cp "$APPDIR/feetbrowser.desktop" "$APPDIR/usr/share/applications/"
cp "$SRC/packaging/linux/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# -- 9. bytecode and a self-test --------------------------------------------
#
# The mounted filesystem is read-only, so nothing can write a .pyc at runtime
# and every import would re-parse its source on every launch. Compiling them
# in now turns that into a mapped read. -o0 keeps the plain .pyc that an
# unoptimised interpreter looks for.
step "bytecode"
"$PY" -m compileall -q -j0 "$STDLIB" "$SITE" "$APPDIR/usr/lib/feetbrowser" \
  >/dev/null || true

step "self-test inside the AppDir"
APPDIR="$APPDIR" "$APPDIR/AppRun" --version
APPDIR="$APPDIR" "$APPDIR/AppRun" --screenshot \
  "file://$SRC/tests/fixtures/pixels.html" "$WORK/appdir-shot.png"
# The decoders, asked of the bundle rather than of the container they were
# built in: a stripped PATH so no gfortran can be found, one frame and one
# soundtrack decoded, and both results compared with what a reference decoder
# produced. verify-in-container.sh asks the finished AppImage the same
# questions on a machine with no Python at all.
APPDIR="$APPDIR" PATH=/usr/bin:/bin "$APPDIR/AppRun" --check-video \
  "$SRC/tests/fixtures/h264/mb1.264" "$SRC/tests/fixtures/h264/mb1.i420.z"
APPDIR="$APPDIR" PATH=/usr/bin:/bin "$APPDIR/AppRun" --check-audio \
  "$SRC/tests/fixtures/aac/lowrate.aac" "$SRC/tests/fixtures/aac/lowrate.f32.z"

# -- 10. the AppImage -------------------------------------------------------
#
# appimagetool is a third-party binary and this is the one place one appears.
# It is a build tool in the same sense cargo and maturin are: it runs here,
# it is not downloaded by a user, and nothing it produces is a library the
# browser imports -- the AppImage is our AppDir, a squashfs image and a small
# ELF runtime that mounts it.
step "appimagetool"
TOOL="$WORK/appimagetool.AppImage"
curl -fsSL -o "$TOOL" \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
chmod +x "$TOOL"
# No FUSE in a container, so unpack the tool rather than mounting it.
(cd "$WORK" && "$TOOL" --appimage-extract >/dev/null)
mkdir -p "$OUT"
IMAGE="$OUT/FeetBrowser-$VERSION-$ARCH.AppImage"
rm -f "$IMAGE"
"$WORK/squashfs-root/AppRun" --no-appstream "$APPDIR" "$IMAGE"
chmod +x "$IMAGE"

# -- 11. what we actually shipped -------------------------------------------
step "glibc floor"
# Measured, not assumed. Every versioned glibc symbol any shipped ELF file
# asks the dynamic loader for carries the glibc release that introduced it;
# the highest of them is the oldest glibc that can start this bundle. The
# AppImage's own runtime is checked separately, because it is the first thing
# that executes and a floor it raised would be invisible in the AppDir.
"$PY" - "$APPDIR" "$IMAGE" <<'FLOOR' | tee "$OUT/glibc-floor.txt"
import os, re, subprocess, sys
def floor(paths):
    seen = set()
    for path in paths:
        out = subprocess.run(["objdump", "-T", path], capture_output=True,
                             text=True).stdout
        seen.update(re.findall(r"GLIBC_(\d+(?:\.\d+)+)", out))
    return max(seen, key=lambda v: tuple(int(n) for n in v.split("."))) \
        if seen else "none"
appdir, image = sys.argv[1], sys.argv[2]
elves = []
for root, _dirs, files in os.walk(appdir):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as fh:
            if fh.read(4) == b"\x7fELF":
                elves.append(path)
print("payload (%d ELF files): glibc %s" % (len(elves), floor(elves)))
print("AppImage runtime: glibc %s" % floor([image]))
FLOOR

step "no build paths in the payload"
# An absolute path from the build machine baked into a shipped binary is
# wrong on the user's machine and is also how a build directory's name gets
# published. Everything here was built under /build and /io, so any /home or
# /Users path would have come from somewhere it should not have. The AppDir
# is checked rather than the AppImage because the AppImage is compressed and
# `strings` cannot see through squashfs.
if grep -rla -E '/(home|Users)/[A-Za-z0-9_.-]+/' "$APPDIR" 2>/dev/null \
   | grep -v '/share/feetbrowser/ca/' | head -5 | grep .; then
  echo "a build path leaked into the bundle" >&2
  exit 1
fi
echo "clean"

step "result"
ls -lh "$IMAGE"
echo "uncompressed AppDir: $(du -sh "$APPDIR" | cut -f1)"
