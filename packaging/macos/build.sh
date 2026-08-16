#!/usr/bin/env bash
# Build FeetBrowser.app and FeetBrowser-<version>.dmg.
#
# The output runs on a Mac that has never had Python, Rust or Xcode on it. It
# contains a whole CPython, the feetbrowser package and the compiled
# feetbrowser_engine extension, and nothing it loads lives outside the bundle
# except the system frameworks in /System and /usr/lib. `verify.sh` is what
# checks that claim, and this script runs it before it makes the disk image.
#
# No freezer is involved. A .app is a directory with a known layout and an
# Info.plist, and every tool used here -- pkgutil, install_name_tool, lipo,
# codesign, iconutil, hdiutil, security -- ships with macOS. What the build
# machine needs beyond that is a C compiler (the Command Line Tools, which
# building the Rust engine already requires), a Rust toolchain with both
# Apple targets installed, and a gfortran for the H.264 and AAC decoders
# (brew install gcc). gfortran only ever targets the machine it is on, so the
# other architecture's half of each decoder has to be built on the other
# architecture and handed over -- see step 6.
#
#   packaging/macos/build.sh              build the .app and the .dmg
#   FEETBROWSER_SKIP_DMG=1 ...build.sh    stop after the .app
#   FEETBROWSER_H264_X86_64=/path/to/lib  the x86_64 decoder, built elsewhere
#   FEETBROWSER_H264_ARM64=/path/to/lib   the arm64 one, likewise
#
# Everything lands in packaging/macos/build (working files, including a cache
# of the downloaded CPython) and packaging/macos/dist (the .app and .dmg).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$here/build"
dist="$here/dist"

# The CPython that goes in the bundle: the official python.org macOS
# installer, pinned by version and by hash. See README.md for why this one.
PY_VERSION=3.13.15
PY_XY=3.13
PY_PKG_SHA256=3b7eaf7f29825f796e8267024435540ddf1f17fc9a97ad58095daa7a75bfdcd3
PY_URL="https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-macos11.pkg"

# Where that framework was built to live, and therefore what every install
# name inside it says before this script rewrites them.
PY_PREFIX="/Library/Frameworks/Python.framework/Versions/$PY_XY"

BUNDLE_ID="io.github.67plays.FeetBrowser"
# python.org's macos11 installer supports 10.13 and later; the arm64 half of
# any universal binary is 11.0 and later because that is when the hardware
# arrived.
MIN_MACOS_X86=10.13
MIN_MACOS_ARM=11.0

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/feetbrowser/__init__.py")
[ -n "$version" ] || { echo "cannot read __version__" >&2; exit 1; }

app="$dist/FeetBrowser.app"
contents="$app/Contents"
framework="$contents/Frameworks/Python.framework"
pyroot="$framework/Versions/$PY_XY"
applib="$contents/Resources/lib"

say() { printf '\n== %s\n' "$*"; }

rm -rf "$work" "$app" "$dist/FeetBrowser-$version.dmg"
mkdir -p "$work" "$dist" "$here/cache"

# -- 1. the interpreter ------------------------------------------------------

say "CPython $PY_VERSION"
pkg="$here/cache/python-$PY_VERSION-macos11.pkg"
if [ ! -f "$pkg" ]; then
  curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$pkg.part" "$PY_URL"
  mv "$pkg.part" "$pkg"
fi
# A tampered or truncated download would produce an app that fails in ways
# nobody would trace back to here, so the hash is checked every run and not
# only on the run that fetched it.
echo "$PY_PKG_SHA256  $pkg" | shasum -a 256 -c - >/dev/null

pkgutil --expand-full "$pkg" "$work/pkg" >/dev/null
payload="$work/pkg/Python_Framework.pkg/Payload"
[ -d "$payload" ] || { echo "no Python_Framework payload in $pkg" >&2; exit 1; }

mkdir -p "$contents/Frameworks" "$contents/MacOS" "$contents/Resources"
# ditto rather than cp -R: the framework is held together by symlinks
# (Versions/Current, and the four at its top level) and copying those as
# files would double its size and break the layout.
ditto "$payload" "$framework"

# -- 2. throw away what a browser does not need ------------------------------
#
# Two of these are not about size. Tcl/Tk and _tkinter are the toolkit this
# project deleted; shipping them back inside the app would be absurd, and
# they are also the one part of the framework that links to a second
# framework outside itself. The rest is the test suite, IDLE, the build
# headers and the developer tooling.

say "pruning the framework"
prune=(
  Frameworks
  share
  bin
  lib/pkgconfig
  lib/python$PY_XY/test
  lib/python$PY_XY/idlelib
  lib/python$PY_XY/tkinter
  lib/python$PY_XY/turtledemo
  lib/python$PY_XY/turtle.py
  lib/python$PY_XY/ensurepip
  lib/python$PY_XY/pydoc_data
  lib/python$PY_XY/config-$PY_XY-darwin
)
# bin/ goes as a whole and nothing comes back. Everything in it is either a
# console script whose shebang points at /Library, or -- python3.13 and
# pythonw3.13 -- a stub compiled from Mac/Tools/pythonw.c that does nothing
# but posix_spawn the real interpreter, which in a framework build lives at
# Resources/Python.app/Contents/MacOS/Python. That one stays for now: the
# steps below run it to build the engine, draw the icon and byte-compile the
# stdlib. Step 10 deletes it once it has done its work, because the app's
# only executable should be the launcher, and a second one would be a second
# Info.plist and a second thing to sign, relocate and answer for.
for item in "${prune[@]}"; do
  rm -rf "${pyroot:?}/$item"
done
rm -f "$pyroot"/lib/python$PY_XY/lib-dynload/_tkinter*.so
rm -f "$pyroot"/lib/python$PY_XY/lib-dynload/*test*.so
rm -f "$pyroot"/lib/python$PY_XY/lib-dynload/xx*.so
# install_name_tool invalidates any signature it touches, so the framework's
# own seal is stale the moment step 3 runs. It is replaced by an ad-hoc one
# in step 4 rather than left to be found broken.
rm -rf "$pyroot/_CodeSignature" "$framework/_CodeSignature"
find "$pyroot/lib/python$PY_XY" -name __pycache__ -type d -exec rm -rf {} +

# -- 3. make it relocatable --------------------------------------------------
#
# Every Mach-O in the framework says /Library/Frameworks/... about its
# neighbours, because that is where the installer would have put it. Left
# alone, the app loads the *machine's* Python if it has one and fails outright
# if it does not -- which is the failure that only ever shows up on somebody
# else's Mac. Each reference is rewritten to a path relative to the file that
# holds it, so the whole tree can be moved anywhere.

# Relative path from directory $1 to path $2, both absolute.
relpath() {
  local from="$1" to="$2" up=""
  while [ "${to#"$from"/}" = "$to" ]; do
    from="$(dirname "$from")"
    up="../$up"
  done
  printf '%s%s' "$up" "${to#"$from"/}"
}

# Every Mach-O under $1, one per line.
machos() {
  find "$1" -type f -print0 | while IFS= read -r -d '' f; do
    case "$(file -b "$f")" in *Mach-O*) printf '%s\n' "$f" ;; esac
  done
}

# Ad-hoc sign every Mach-O under $1. Not a substitute for a Developer ID --
# see README.md -- but not optional either: on Apple Silicon every executable
# page must carry a signature, and install_name_tool invalidates the one a
# binary arrived with, so anything rewritten above is killed on sight by the
# kernel until this has run over it.
sign_all() {
  while IFS= read -r file; do
    codesign --force --sign - --timestamp=none "$file" >/dev/null 2>&1
  done < <(machos "$1")
}

say "rewriting install names"
while IFS= read -r file; do
  dir="$(dirname "$file")"
  # An install name that is already relative, or points into /System or
  # /usr/lib, is left alone; those are the only two absolute prefixes a
  # self-contained bundle is allowed to keep.
  otool -L "$file" | grep '^	' | awk '{print $1}' | sort -u |
  while read -r dep; do
    case "$dep" in
      "$PY_PREFIX"/*)
        target="$pyroot/${dep#"$PY_PREFIX"/}"
        install_name_tool -change "$dep" \
          "@loader_path/$(relpath "$dir" "$target")" "$file" 2>/dev/null
        ;;
    esac
  done
  # The library's own name, which otool -L reports as its first entry and
  # which a linker would copy into anything built against it later.
  id=$(otool -D "$file" | sed -n '2p')
  case "$id" in
    "$PY_PREFIX/Python")
      # @rpath, not @loader_path: this one is loaded by the launcher in
      # Contents/MacOS, and @loader_path in an *id* resolves against the
      # client's directory, which is the wrong end of the bundle.
      install_name_tool -id "@rpath/Python.framework/Versions/$PY_XY/Python" \
        "$file" ;;
    "$PY_PREFIX"/*)
      install_name_tool -id "@loader_path/$(basename "$id")" "$file" 2>/dev/null ;;
  esac
done < <(machos "$framework")

# Immediately, not at the end: the next step runs the bundled interpreter to
# build the engine against it, and an interpreter whose framework has just
# been rewritten is SIGKILLed the moment dyld looks at it.
sign_all "$framework"

# -- 4. the engine, both architectures ---------------------------------------
#
# One wheel carrying arm64 and x86_64, built against the interpreter that is
# going in the bundle so the extension's ABI tag matches it exactly. This is
# the technique wheels.yml uses: maturin builds the crate once per
# architecture and lipos the halves together, which cannot end up shipping
# only one of them.

say "building feetbrowser_engine (universal2)"
pybin="$pyroot/Resources/Python.app/Contents/MacOS/Python"
"$pybin" -V

if ! command -v maturin >/dev/null 2>&1 && [ -z "${MATURIN:-}" ]; then
  if [ -x "$root/.venv/bin/maturin" ]; then
    MATURIN="$root/.venv/bin/maturin"
  else
    echo "maturin is not on PATH; set MATURIN=/path/to/maturin" >&2
    exit 1
  fi
fi
# Cross-compiling to the other architecture needs that architecture's
# prebuilt standard library, which is a rustup component. A cargo that is not
# rustup's -- Homebrew ships one, and it comes first on PATH if it is
# installed -- has only the host target and fails halfway through with
# "can't find crate for `core`" after the native half has already succeeded.
# So the toolchain rustup manages is put in front for the duration.
if command -v rustup >/dev/null 2>&1; then
  rustup target add aarch64-apple-darwin x86_64-apple-darwin >/dev/null
  PATH="$(dirname "$(rustup which cargo)"):$PATH"
  export PATH
fi
for target in aarch64-apple-darwin x86_64-apple-darwin; do
  rustc --print sysroot >/dev/null 2>&1 || break
  if [ ! -d "$(rustc --print sysroot)/lib/rustlib/$target" ]; then
    echo "the standard library for $target is not installed" >&2
    echo "run: rustup target add $target" >&2
    exit 1
  fi
done

# Rust puts the source path of every panic site into the binary, because
# that is what a panic message prints. Those paths are the build machine's:
# the crate registry under $CARGO_HOME and the checkout this ran from, so a
# release build ships a few thousand strings naming whoever made it. `strip`
# cannot touch them -- they are ordinary constants, not debug info -- and
# --remap-path-prefix is the switch that stops them being written in the
# first place. The panic messages stay just as useful with the prefix
# replaced.
# The whole home directory rather than just the crate registry: the compiler
# also inlines panic sites from the standard library, whose sources are under
# .rustup, and next year it will be somewhere else again. First match wins,
# so the checkout is named before the directory that contains it.
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$root=/feetbrowser --remap-path-prefix=$HOME=/build"

# -i: build against the interpreter that is going in the bundle. pyo3 is not
# used in abi3 mode here, so the extension is tied to one CPython minor
# version and its ABI tag has to be that one -- left to itself maturin picks
# whatever python is on PATH, and a wheel tagged cp314 lands in a bundle that
# runs 3.13 and is simply not importable.
"${MATURIN:-maturin}" build --release \
  --manifest-path "$root/rust/Cargo.toml" -i "$pybin" \
  --target universal2-apple-darwin --out "$work/wheel" >/dev/null

wheel=$(echo "$work/wheel"/*.whl)
mkdir -p "$applib"
unzip -o -q "$wheel" -d "$applib"
rm -rf "$applib"/*.dist-info
engine=$(find "$applib" -name 'feetbrowser_engine*.so' | head -1)
[ -n "$engine" ] || { echo "the wheel contained no extension module" >&2; exit 1; }
# Release builds keep the paths of the machine that made them in their debug
# symbols. Nothing in a shipped app should carry a stranger's home directory
# around, and `strip -x` takes the local symbols out without touching the
# dynamic ones the loader needs.
strip -x "$engine"

# -- 5. the browser ----------------------------------------------------------

say "copying the package"
ditto "$root/feetbrowser" "$applib/feetbrowser"
find "$applib/feetbrowser" -name __pycache__ -type d -exec rm -rf {} +
# toes/ is where discover_toes() looks, relative to the package's parent. It
# is read-only in an installed app; see README.md.
mkdir -p "$applib/toes"
cp "$root/toes/README.md" "$applib/toes/"

# -- 6. the Fortran decoders -------------------------------------------------
#
# fortran/ is FORTRAN 77: eleven sources and an include file that
# feetbrowser/h264.py compiles the first time a video plays, and five and an
# include file that feetbrowser/aac.py compiles the first time one has sound.
# That works from a checkout, where there is a compiler; it cannot work in a
# shipped app, where there is not. Left alone the app starts, renders, and
# says "[video: H.264: no gfortran on PATH]" the first time anyone opens a
# video -- which no developer ever sees, because developers run from a
# checkout.
#
# Both decoders, and for the same reason. Shipping only the video half is
# not half a fix: it produces an app that plays pictures in silence, which
# looks like a bug in the player rather than like a missing decoder, and it
# is the state this section was in until the sound half was added.
#
# So the libraries are built here, by a gfortran only this machine needs, and
# ship inside the package under the names h264.py and aac.py look for. The
# sources ship as well, beside the package like toes/ -- 250K, and each name
# is a hash of them, so the two cannot come apart. See prebuilt_name() for
# why that is the whole guarantee.
#
# Both architectures, like everything else in here. The build machine's
# gfortran only targets its own -- gfortran does not cross-compile on Darwin
# and Homebrew's does not pretend to -- so the other half comes from
# FEETBROWSER_H264_<ARCH> and FEETBROWSER_AAC_<ARCH> when they are set, which
# is how the workflow hands the x86_64 slices over from the Intel runner, and
# otherwise from any gfortran on PATH that targets it.

say "the Fortran decoders"
ditto "$root/fortran" "$applib/fortran"
h264name=$(PYTHONPATH="$applib" "$pybin" -m feetbrowser.h264 --name)
aacname=$(PYTHONPATH="$applib" "$pybin" -m feetbrowser.aac --name)

# The first gfortran on PATH that targets $1, or nothing. gfortran calls
# arm64 aarch64, hence the second pattern.
gfortran_for() {
  local arch="$1" candidate machine
  for candidate in "${2:-}" gfortran gfortran-15 gfortran-14 gfortran-13 \
                   gfortran-12 gfortran-11; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    machine=$("$candidate" -dumpmachine 2>/dev/null || true)
    case "$machine" in
      "$arch"-*)  printf '%s\n' "$candidate"; return 0 ;;
      aarch64-*)  [ "$arch" = arm64 ] && { printf '%s\n' "$candidate"; return 0; } ;;
    esac
  done
  return 1
}

# One decoder, one architecture, into $work/<module>/<arch>/<name>. Written
# once and called twice rather than copied: the rpath surgery and the
# dependency check below are the parts that decide whether a bundle works on
# somebody else's machine, and two copies of them are one copy that is out of
# date.
#
#   decoder_slice <module> <library name> <arch> <deployment target> \
#                 <gfortran hint> <prebuilt slice or empty>
decoder_slice() {
  local module="$1" name="$2" arch="$3" min="$4" hint="$5" prebuilt="$6"
  local slice fc rp bad upper
  upper=$(printf '%s' "$module" | tr a-z A-Z)
  mkdir -p "$work/$module/$arch"
  slice="$work/$module/$arch/$name"
  if [ -n "$prebuilt" ]; then
    [ -f "$prebuilt" ] || {
      echo "FEETBROWSER_${upper}_* names $prebuilt, which does not exist" >&2
      exit 1
    }
    # Named after the digest, so a slice built from other sources cannot be
    # lipo'd in silently: it would not be called this.
    [ "$(basename "$prebuilt")" = "$name" ] || {
      echo "$prebuilt is not $name -- it was built from different sources" >&2
      exit 1
    }
    cp "$prebuilt" "$slice"
  else
    fc=$(gfortran_for "$arch" "$hint") || {
      echo "no gfortran on PATH targets $arch" >&2
      echo "install one, or point FEETBROWSER_${upper}_$(printf '%s' "$arch" | tr a-z A-Z) at a slice built elsewhere" >&2
      exit 1
    }
    echo "$module $arch: $("$fc" -dumpmachine) ($fc)"
    MACOSX_DEPLOYMENT_TARGET=$min PYTHONPATH="$applib" \
      "$pybin" -m "feetbrowser.$module" --build "$slice" --fc "$fc" >/dev/null
  fi
  # gfortran writes the path to its own runtime into the library as an
  # LC_RPATH -- on this machine, a directory under whoever's Homebrew Cellar
  # built it -- and it does so even when that runtime was linked in
  # statically and there is nothing left to go looking for. Nothing needs
  # them and they name the build machine, which verify.sh fails a bundle for,
  # correctly. So they go. (-nodefaultrpaths would prevent them at the link
  # step and is GCC 12 and later on Darwin only; deleting them afterwards
  # works with any compiler that produced the slice, including one that
  # arrived from the other runner.)
  while IFS= read -r rp; do
    [ -n "$rp" ] || continue
    install_name_tool -delete_rpath "$rp" "$slice" 2>/dev/null || true
    echo "  dropped rpath $rp"
  done < <(otool -l "$slice" | awk '/LC_RPATH/{want=1; next} want && $1=="path"{print $2; want=0}')

  # The one failure that would otherwise be found by a user: gfortran's
  # runtime left as a dependency on a compiler installation nobody but the
  # build machine has. -static-libgfortran and friends are meant to prevent
  # it; this is where that claim is checked rather than assumed. verify.sh
  # applies the same rule to the lipo'd result, and to every other Mach-O.
  bad=$(otool -L "$slice" | tail -n +2 | awk '{print $1}' \
        | grep -v -e '^/usr/lib/' -e '^/System/' -e '^@loader_path/' || true)
  [ -z "$bad" ] || {
    echo "the $arch $module decoder depends on something outside the bundle:" >&2
    echo "$bad" >&2
    exit 1
  }
}

for arch in arm64 x86_64; do
  case $arch in
    arm64)  min=$MIN_MACOS_ARM
            h264slice="${FEETBROWSER_H264_ARM64:-}"
            aacslice="${FEETBROWSER_AAC_ARM64:-}"
            hint="${FEETBROWSER_GFORTRAN_ARM64:-}" ;;
    x86_64) min=$MIN_MACOS_X86
            h264slice="${FEETBROWSER_H264_X86_64:-}"
            aacslice="${FEETBROWSER_AAC_X86_64:-}"
            hint="${FEETBROWSER_GFORTRAN_X86_64:-}" ;;
  esac
  decoder_slice h264 "$h264name" "$arch" "$min" "$hint" "$h264slice"
  decoder_slice aac  "$aacname"  "$arch" "$min" "$hint" "$aacslice"
done

for module in h264 aac; do
  case $module in
    h264) name=$h264name ;;
    aac)  name=$aacname ;;
  esac
  lipo -create "$work/$module/arm64/$name" "$work/$module/x86_64/$name" \
    -output "$applib/feetbrowser/$name"
  lipo -info "$applib/feetbrowser/$name"
done

# -- 7. certificates ---------------------------------------------------------
#
# A bundled CPython has no trust store. python.org's OpenSSL looks under the
# framework's own etc/openssl, which ships empty -- that is what the
# installer's "Install Certificates.command" is for -- so the first https://
# fetch fails with a certificate error while every other part of the browser
# looks fine. The roots come out of macOS's own system keychain rather than
# off the internet, and launcher.c points SSL_CERT_FILE at them.

say "trust store"
mkdir -p "$contents/Resources/certs"
security find-certificate -a -p \
  /System/Library/Keychains/SystemRootCertificates.keychain \
  > "$contents/Resources/certs/cacert.pem"
roots=$(grep -c 'BEGIN CERTIFICATE' "$contents/Resources/certs/cacert.pem")
[ "$roots" -gt 50 ] || { echo "only $roots roots in the trust store" >&2; exit 1; }
echo "$roots root certificates"

# -- 8. the icon -------------------------------------------------------------
#
# The artwork lives once, as the shipped feetbrowser/icon.png, and the iconset
# PNGs are committed at the sizes iconutil wants -- nothing generates them at
# build time any more, so an offline build cannot fall over on the icon.

say "icon"
cp -R "$here/FeetBrowser.iconset" "$work/FeetBrowser.iconset"
iconutil -c icns "$work/FeetBrowser.iconset" \
  -o "$contents/Resources/FeetBrowser.icns"

# -- 9. the launcher ---------------------------------------------------------
#
# Compiled once per architecture, because the two have different oldest
# supported systems, and lipo'd together afterwards.

say "launcher"
for arch in x86_64 arm64; do
  case $arch in
    x86_64) min=$MIN_MACOS_X86 ;;
    arm64)  min=$MIN_MACOS_ARM ;;
  esac
  cc -arch "$arch" -mmacosx-version-min="$min" -O2 -Wall -Wextra \
    -I"$pyroot/include/python$PY_XY" \
    -o "$work/launcher-$arch" "$here/launcher.c" \
    -F"$contents/Frameworks" -framework Python \
    -Wl,-rpath,@executable_path/../Frameworks
done
lipo -create "$work/launcher-x86_64" "$work/launcher-arm64" \
  -output "$contents/MacOS/FeetBrowser"
chmod 755 "$contents/MacOS/FeetBrowser"
# The linker records the framework's id, which step 3 already made @rpath;
# the -rpath above resolves it. Stated here rather than assumed, because an
# absolute path surviving this step is exactly what verify.sh fails on.
install_name_tool -change \
  "@rpath/Python.framework/Versions/$PY_XY/Python" \
  "@rpath/Python.framework/Versions/$PY_XY/Python" \
  "$contents/MacOS/FeetBrowser" 2>/dev/null || true

# The headers were only needed to compile the launcher.
rm -rf "$pyroot/include" "$pyroot/Headers" "$framework/Headers"

# -- 10. the plist ------------------------------------------------------------

say "Info.plist"
cat > "$contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>FeetBrowser</string>
  <key>CFBundleDisplayName</key>       <string>FeetBrowser</string>
  <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>        <string>FeetBrowser</string>
  <key>CFBundleIconFile</key>          <string>FeetBrowser</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleSignature</key>         <string>????</string>
  <key>CFBundleInfoDictionaryVersion</key> <string>6.0</string>
  <key>CFBundleShortVersionString</key> <string>$version</string>
  <key>CFBundleVersion</key>           <string>$version</string>
  <key>LSMinimumSystemVersion</key>    <string>$MIN_MACOS_X86</string>
  <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
  <!-- The renderer draws its own pixels at the backing scale AppKit gives
       it. Without this the window server would magnify a 1x frame on a
       Retina display and every glyph the font engine anti-aliased would
       arrive blurred. -->
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSHumanReadableCopyright</key>
  <string>Copyright (c) 2026 JuiceyDew. MIT licensed.</string>
</dict>
</plist>
PLIST
printf 'APPL????' > "$contents/PkgInfo"

# -- 11. compile, sign, check ------------------------------------------------

say "byte-compiling"
# The bundle is read-only on a mounted disk image and must not be written to
# in /Applications either, so launcher.c turns .pyc writing off. Compiling
# here is what keeps that from costing a slow parse on every start.
#
# -s/-p rewrite the filename each .pyc records for tracebacks. Left alone
# that is the absolute path of the file on this machine, so every one of the
# few thousand .pyc files in the bundle would ship a stranger somebody's home
# directory -- which is both a privacy leak and a nonsense path in any
# traceback a user ever sees. Replacing the build directory with the place
# the app is meant to live makes those tracebacks true for the reader.
#
# The steps above ran the bundled interpreter, and every module it imported
# to do so cached itself with the real path. compileall would leave those
# alone as already up to date, so they go first and are written again below
# with the rewritten filename.
find "$pyroot/lib/python$PY_XY" "$applib" -name __pycache__ -type d \
  -exec rm -rf {} + 2>/dev/null || true
#
# -B, and -f: compileall is itself Python, so the modules it imports to do
# its job cache themselves the ordinary way -- with the real path, and before
# the walk reaches them, after which the walk finds them up to date and
# leaves them. -B stops the import machinery writing anything (py_compile
# writes its files directly and is unaffected) and -f recompiles regardless
# of what is already there, so no .pyc can survive from before this line.
"$pybin" -B -m compileall -q -f -j 0 -s "$dist" -p /Applications \
  "$pyroot/lib/python$PY_XY" "$applib" >/dev/null || true

# The build-time interpreter has now done everything it was kept for. What
# ships is one executable, Contents/MacOS/FeetBrowser, and a framework that
# is only ever loaded as a library.
rm -rf "$pyroot/Resources/Python.app"

say "ad-hoc signing"
# Again, and this time over the launcher and the engine as well. The seals go
# on from the inside out: signing a bundle records what its nested code was
# sealed as, so the framework has to be finished before the app is.
sign_all "$contents"
codesign --force --sign - --timestamp=none "$framework/Versions/$PY_XY" >/dev/null
codesign --force --sign - --timestamp=none "$app" >/dev/null
codesign --verify --deep --strict "$app"

"$here/verify.sh" "$app"

if [ -n "${FEETBROWSER_SKIP_DMG:-}" ]; then
  say "done: $app"
  exit 0
fi

# -- 12. the disk image ------------------------------------------------------

say "disk image"
dmgroot="$work/dmg"
mkdir -p "$dmgroot"
ditto "$app" "$dmgroot/FeetBrowser.app"
# The customary drag-to-install gesture: the app on one side, a link to
# /Applications on the other.
ln -s /Applications "$dmgroot/Applications"
# The app is unsigned on purpose, so the first launch is refused and the way
# past it is buried in System Settings. Nothing else on this image says so,
# and a user who drags the app across and double-clicks it has been told by
# macOS that it may be malicious and by us nothing at all. The Windows
# bundle ships README-FIRST.txt for the same reason; this is its counterpart,
# and the name is what makes it get read while the warning is on screen.
cp "$here/OPEN-ME-FIRST.txt" "$dmgroot/OPEN ME FIRST.txt"
dmg="$dist/FeetBrowser-$version.dmg"
hdiutil create -volname "FeetBrowser $version" -srcfolder "$dmgroot" \
  -fs HFS+ -format UDZO -ov "$dmg" >/dev/null

# Mount what was just written and look at it, because every check up to here
# examined the staging directory rather than the file people download. The
# instructions especially: an unsigned app whose disk image forgot to say how
# to open it is an app most people cannot open at all, and that failure is
# invisible to anyone who has already allowed it on their own machine.
check="$work/dmgcheck"
mkdir -p "$check"
hdiutil attach "$dmg" -mountpoint "$check" -nobrowse -readonly -quiet
missing=""
for item in "FeetBrowser.app" "Applications" "OPEN ME FIRST.txt"; do
  [ -e "$check/$item" ] || missing="$missing $item"
done
hdiutil detach "$check" -quiet
rmdir "$check" 2>/dev/null || true
[ -z "$missing" ] || { echo "the disk image is missing:$missing" >&2; exit 1; }
echo "  image carries the app, the Applications link and the instructions"

say "done"
echo "$app"
echo "$dmg  ($(du -h "$dmg" | cut -f1))"
