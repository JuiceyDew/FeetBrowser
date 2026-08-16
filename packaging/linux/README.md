# FeetBrowser for Linux

One file. Download it, `chmod +x` it, run it. No Python on the machine, no
Rust toolchain, no `-dev` packages, no installer and no root.

```sh
chmod +x FeetBrowser-0.6.0-x86_64.AppImage
./FeetBrowser-0.6.0-x86_64.AppImage                 # the welcome page
./FeetBrowser-0.6.0-x86_64.AppImage https://example.com
./FeetBrowser-0.6.0-x86_64.AppImage --install       # add it to the menu
```

x86_64 only; see "Architectures" at the bottom.

## Why an AppImage

The requirement was a single downloadable file that a person makes executable
and runs. That is what an AppImage is: a small ELF runtime with a compressed
filesystem image stapled to it, holding a directory tree the runtime mounts
and executes out of. There is no install step, no root, nothing written
outside the file itself, and deleting the file uninstalls it.

The alternative considered was a tarball with an `install.sh` that writes a
`.desktop` file into `~/.local/share/applications`. It needs no third-party
build tooling at all, which is a real advantage in a project with this
project's rules, but it is two steps and a directory the user now owns
instead of one file they can move to a USB stick, and "download and run" was
the requirement. The desktop-file half of that idea is not lost: it is what
`--install` below does.

`appimagetool` is a third-party binary, and it is the only one anywhere near
this. It is a build tool in exactly the sense `cargo` and `maturin` already
are: it runs in the build container, no user ever downloads it, and nothing it
produces is a library the browser imports. The AppImage it emits is our
directory tree, squashfs, and a ~920 KB statically linked runtime whose entire
job is to mount that tree: no framework, no toolkit, nothing linked into the
browser.

`.deb` and `.rpm` are out of scope for this change. They are a different
distribution model with a different set of promises (dependency resolution,
system-wide install, distro-specific packaging policy), and they are not
needed to answer "one file you download and run".

## What is inside

```
FeetBrowser.AppDir/
  AppRun                              the launcher; see below
  feetbrowser.desktop  .DirIcon
  usr/bin/python3.12                  CPython, compiled from the upstream
                                      tarball inside the build container
  usr/lib/python3.12/                 its standard library
      site-packages/feetbrowser/      this repository's package
      site-packages/feetbrowser_engine/  the Rust extension
      site-packages/feetbrowser/_h264_<digest>.so
                                      the H.264 decoder, compiled
      site-packages/feetbrowser/_aac_<digest>.so
                                      the AAC decoder, the same way
      site-packages/fortran/          the Fortran both were built from
  usr/lib/*.so.*                      the private libraries CPython needs:
                                      libssl, libcrypto, libffi, ...
  usr/lib/feetbrowser/launcher.py     bundled-font wiring, then __main__
  usr/share/feetbrowser/fonts/        DejaVu Serif, Sans and Sans Mono
  usr/share/feetbrowser/ca/cacert.pem a CA bundle, used only as a fallback
  usr/share/icons/hicolor/…           the application icon at four sizes
```

### CPython: compiled here, not downloaded

The interpreter is built from the upstream `Python-3.12.11.tar.xz` inside the
build container, with `--with-ensurepip=no --disable-test-modules`. A
prebuilt relocatable distribution would have been quicker and is a reasonable
choice for most projects, but it is a third-party binary shipped inside a
project whose premise is owning its stack, and it would decide for us which
OpenSSL, which module set and which glibc floor we inherit. Compiling it costs
about four minutes of build time and means every byte of the interpreter came
out of the upstream tarball and this container.

It relocates because CPython finds its prefix by walking up from the
executable looking for `lib/python3.X/os.py`, so the tree can live anywhere as
long as `bin/` and `lib/` keep their relative positions, which is the layout
an AppDir wants anyway. `AppRun` also sets `PYTHONHOME` explicitly, so a
`PYTHONHOME` left in the user's shell profile cannot redirect our interpreter
at their standard library, and clears `PYTHONPATH`/`PYTHONSTARTUP` for the
same reason.

`--enable-optimizations` (PGO) is deliberately off. It roughly triples the
build for a gain the browser cannot see: every hot loop it has is in the Rust
extension.

### Private libraries, and how they are found

Whatever the interpreter and its extension modules link that is not glibc or
the compiler runtime (OpenSSL, libffi, zlib) is copied into `usr/lib`. They
are reached through an `RPATH` of `$ORIGIN`, computed per file at build time,
**not** through `LD_LIBRARY_PATH`. That distinction matters: `LD_LIBRARY_PATH`
would put our copies in front of the host's libraries for everything the
process ever loads, including whatever gets dlopened behind libX11 on a real
desktop. With `$ORIGIN` the private copies are visible to our binaries and
invisible to everything else.

libc, libm, libpthread, libdl, librt, libgcc_s and libstdc++ are deliberately
*not* bundled. Every glibc system has them, and giving a process an older copy
of its own C library or its own unwinder is worse than any version skew it
could fix.

## X11, and what happens under Wayland

`feetbrowser/x11.py` drives Xlib through `ctypes` (no bindings package, no
compiled shim), so the bundle has to find a `libX11.so.6` at runtime. **It
uses the host's.** That is a deliberate choice, not an oversight:

* Every machine with an X server already has libX11; it is a dependency of
  essentially every graphical client on the system.
* The X protocol is stable across decades, so an old client library and a new
  server (or the reverse) work together.
* Underneath libX11 sits libxcb, and behind that on a real desktop sits the
  GLX and driver stack that gets dlopened at connect time. Shipping our own
  copies of part of that and letting the loader mix them with the host's rest
  is a well-known way to produce protocol errors and driver crashes on
  hardware we cannot test on.

So: the host's X libraries, ours for everything the browser itself is made of.
A machine with no libX11 gets the browser's existing sentence about there
being no window available, and `--screenshot` still works there.

**Wayland.** The browser has no Wayland backend, so on a Wayland session it
runs as an X11 client through **XWayland**. GNOME, KDE Plasma and
wlroots-based compositors all ship XWayland and start it on demand for exactly
this case, and it sets `$DISPLAY` for the clients that need it.

Being exact about what that claim rests on: **no real Wayland session was
available here, so this has not been tested on one.** What was tested is the
X11 side of it (a genuine window on a genuine X server, with the pixels read
back off it), using `Xvfb`, which speaks the X11 protocol XWayland speaks but
is not XWayland. The expectation that XWayland carries this client is
reasoning from what XWayland is for, not a measurement. Treat it as strongly
expected and untested.

If `$DISPLAY` is unset (a session with XWayland disabled), the browser says
there is no window available, and `--screenshot` still works.

## Fonts

`canvas._resolve_face` falls back through three chains (`times`, `helvetica`,
`courier`) and raises `CanvasError: no usable font` when none of them
resolves. On a machine with no fonts installed that is a crash, not a page of
blank boxes, so the bundle carries its own: **DejaVu Serif, Sans and Sans
Mono** (regular and bold, plus Sans Oblique). Those three families are one
entry in each of the three fallback chains, which makes them the smallest set
that resolves every generic family. TrueType outlines only; `fontengine._scan`
skips CFF fonts, which it can measure but cannot rasterise.

`launcher.py` appends the bundled directory to `fontengine.FONT_DIRS_DEFAULT`
before anything draws. Appended rather than prepended, on purpose: a font the
user installed still wins, and the bundled copies are there for the machine
that has nothing else. No file in `feetbrowser/` was changed to make this
work: `FONT_DIRS_DEFAULT` is the same list object `fontengine._dirs()` hands
back on Linux, so mutating it in place is the whole of it.

## HTTPS and certificates

`net.py` calls `ssl.create_default_context()`, which asks OpenSSL where the
trust store is, and the bundled OpenSSL was compiled inside AlmaLinux, so its
built-in answer is `/etc/pki/tls`, a path that exists on one family of distros
and not on most machines this will be run on. The failure that produces is the
nasty kind: every `http://` page loads, the browser looks perfectly healthy,
and the first `https://` page fails with a certificate error.

`AppRun` sets `SSL_CERT_FILE` by probing, in order, the places the major
distributions keep their bundle:

```
/etc/ssl/certs/ca-certificates.crt      Debian, Ubuntu, Arch, Alpine
/etc/pki/tls/certs/ca-bundle.crt        Fedora, RHEL, AlmaLinux
/etc/ssl/ca-bundle.pem                  openSUSE
/etc/pki/tls/cacert.pem
/etc/ssl/cert.pem
/usr/share/ssl/certs/ca-bundle.crt
/usr/local/share/certs/ca-root-nss.crt
```

and only if none of those exists does it fall back to the copy in
`usr/share/feetbrowser/ca/cacert.pem`. The host's store is preferred because
it is the set of authorities the user's administrator chose, it receives
security updates, and it carries any internal CA their network needs. Ours
exists so that a machine with no trust store at all (a container, a stripped
install), fails soft instead of losing the encrypted half of the web. An
`SSL_CERT_FILE` the user set themselves is left alone.

The acceptance test proves the fallback works by fetching a live `https://`
page in a container with no `ca-certificates` package installed.

## Optional imports, and writing to a read-only bundle

`net.py` imports `curl_cffi` lazily inside `request_impersonated()` and falls
back to `request()` on `ImportError`. It is not in the bundle; it is a
compiled third-party package and this project does not ship those, so that
path degrades to the ordinary socket/TLS client, which is what happens on any
machine without it today. Verified rather than assumed: the acceptance test
navigates and renders with it absent.

`asmlib.py` compiles `asm/x11pack.S` at import time if a C compiler is on
`PATH`, and returns pure-Python kernels when there is none. A user's machine
has no compiler, so the fallback is what runs; the kernels only matter on an
X server whose TrueColor visual is depth 15 or 16, which almost nobody meets.
It writes only to `TMPDIR`, never inside the bundle.

The mounted image is read-only. `PYTHONDONTWRITEBYTECODE=1` is set and every
`.pyc` is compiled in at build time, so imports are a mapped read rather than
a re-parse. The two files the browser persists (`
~/.feetbrowser_bookmarks.json` and `~/.feetbrowser_shoes.json`) are already
in `$HOME`. `toes.repo_root()` resolves to the bundle's `site-packages`, and
its config writer already swallows `OSError`, so a toe enable/disable is a
no-op rather than a crash.

## The applications menu

The bundle carries a `feetbrowser.desktop` and the icon at 256, 128, 64 and
48 pixels. Desktops running AppImageLauncher or `appimaged` pick those up on
their own. For everyone else:

```sh
./FeetBrowser-0.6.0-x86_64.AppImage --install
```

writes `~/.local/share/applications/feetbrowser.desktop` with `Exec` pointing
at wherever the AppImage currently sits, copies the icons into
`~/.local/share/icons/hicolor`, refreshes the desktop and icon caches, and
runs `xdg-mime default` so the browser is offered for `http://` and `https://`
links. Nothing outside `$HOME`, no root. `--uninstall` reverses it.

The `.desktop` entry declares
`MimeType=x-scheme-handler/http;x-scheme-handler/https;text/html;` and
`Categories=Network;WebBrowser;`, which is what makes it show up under
"Internet" and appear in the "open with" list for a link.

There is one more flag, `--python`, which runs the bundled interpreter on a
script of your choosing. It exists so the acceptance test can drive the
bundle's own Python; it is what runs `tests/x11_shot.py` inside a container
that has no Python, and it is useful for debugging a bundle in the field.

## The icon

The artwork lives once, as `packaging/art/feet.png`, and every platform's icon
is a resample of it: the Windows `.ico`, the macOS iconset, and these hicolor
PNGs. `packaging/linux/make_icon.py` decodes that one PNG and area-averages it
down to each of the four sizes, pure standard library, so there is no second
copy of the art anywhere and no image library to import. Shrinking is a fair
sample of the whole mark rather than one pixel in every N, so the 48-pixel
menu icon stays legible.

## Building it

Needs Docker and nothing else. Roughly ten minutes, most of it CPython and
the Rust extension.

```sh
packaging/linux/build.sh          # -> dist/FeetBrowser-<version>-x86_64.AppImage
packaging/linux/verify.sh         # the acceptance test, in a clean container
```

`build.sh` is a wrapper that puts `build-appimage.sh` inside
`quay.io/pypa/manylinux_2_28_x86_64` and hands it the checkout. Driving the
container with `docker run` rather than a workflow-level `container:` keeps
checkout and artifact upload on the runner and puts only the compilers
inside, the shape `wheels.yml` already uses.

`.github/workflows/package-linux.yml` runs both on `workflow_dispatch`, on
`v*` tags, and on pull requests that touch `packaging/linux/**`. It uploads
the AppImage, the measured glibc floor, and the verification screenshots, and
attaches the AppImage to the release on a tag.

## Minimum glibc

**glibc 2.28**: measured, not assumed.

Every versioned glibc symbol an ELF file asks the dynamic loader for carries
the release that introduced it, and the highest one across the shipped
binaries is the oldest glibc that can start the bundle. The build prints it
and writes it to `dist/glibc-floor.txt`:

```
payload (70 ELF files): glibc 2.28
AppImage runtime: glibc none
```

`none` is not a gap in the measurement: the AppImage runtime is statically
linked, so it asks the dynamic loader for nothing at all and the floor is
entirely the payload's.

That is why the build happens in manylinux_2_28 and not on the CI runner: the
same tree built on `ubuntu-latest` would demand that runner's glibc and refuse
to start on RHEL 8, Debian 10 or anything else older, which is exactly the
failure this whole arrangement exists to avoid. glibc 2.28 is Debian 10,
RHEL/CentOS 8, Ubuntu 18.10 and every release of anything since.

## Size

**32 MB** downloaded; 111 MB once mounted. Roughly two thirds of that is
CPython and its standard library, and most of the rest is the Rust extension
and the seven font faces. The squashfs is zstd-compressed and the runtime
mounts it, so the uncompressed figure is what the filesystem reports rather
than disk a user has to find, except under `APPIMAGE_EXTRACT_AND_RUN`, which
really does unpack it to a temporary directory.

## Architectures

x86_64 only. An aarch64 AppImage is the same script with a different base
image and a different `appimagetool`, but building it on an x86_64 runner
means emulating a CPython build and an LTO Rust build under qemu, and there is
no aarch64 Linux runner in this project's CI to test the result on. Shipping
one unverified would be worse than not shipping one. It wants its own job and
its own hardware.

## FUSE

An AppImage mounts itself with FUSE. Desktop distributions ship it (`fuse3` on
anything current), and the runtime used here is the statically-linked one that
works with fuse3 rather than requiring the older `libfuse2`. Where FUSE is
genuinely unavailable (inside a container, typically), the runtime's own
fallback unpacks the image to a temporary directory instead:

```sh
APPIMAGE_EXTRACT_AND_RUN=1 ./FeetBrowser-0.6.0-x86_64.AppImage
```

The acceptance test uses whichever of the two the test container can offer,
and says which.

## The acceptance test

`verify.sh` runs the finished file in `debian:stable-slim`, a different
distribution family from the one it was built in, a newer glibc, and
deliberately bare. It refuses to run if a `python3` is on `PATH`, and it
installs exactly two packages:

| package | why a real user's machine has it |
| --- | --- |
| `libx11-6` | the X client library `x11.py` dlopens; every machine with an X server has it |
| `xvfb` | an X server. A user's desktop *is* the X server; a container has to be given one |

Not installed, on purpose: `ca-certificates` (so the bundled CA fallback is
what is being tested) and any font package (so the bundled DejaVu is what is
being tested).

It then checks, in order: the file runs and prints its version; a live
`https://` page fetches and renders; a local fixture page renders with no font
package installed; a page with a PNG and a GIF on it renders, which is the
compiled engine decoding them; `--check-video` decodes an H.264 frame and
compares it with the reference picture, and `--check-audio` decodes an AAC
frame and compares it with the reference samples, both on a machine with no
gfortran on it (the test refuses to run if there is one, because it would
then prove nothing); and `tests/x11_shot.py`, the repository's own
end-to-end window check, opens a real window on the Xvfb server, paints a
page into it, reads the pixels back with `XGetImage` and fails unless the red,
green and blue swatches are all present and land in that order across the
window. That last one runs through the bundle's own interpreter, with
`sys.path[0]` pointing at a directory containing no `feetbrowser`, so the
package, the engine, the fonts and the interpreter all come out of the
AppImage. Then `--install` runs and the `.desktop` file and icon are checked
into place, and the image is unpacked and searched for absolute build paths;
an artifact carrying `/home/somebody/...` is both wrong on the user's machine
and a leak of the build machine's directory names.

The PNGs are uploaded by CI and are meant to be looked at.
