# FeetBrowser.app

A macOS application bundle that runs on a Mac with no Python, no Rust and no
Xcode on it: drag `FeetBrowser.app` out of the disk image into
`/Applications` and double-click it.

```
packaging/macos/build.sh          # -> dist/FeetBrowser.app, dist/FeetBrowser-<version>.dmg
packaging/macos/verify.sh <app>   # read the bundle: is it really self-contained?
packaging/macos/appshot.py <app>  # run the bundle: does it start, render and fetch?
```

`FEETBROWSER_SKIP_DMG=1 packaging/macos/build.sh` stops after the `.app`.

Nothing is frozen, bytecode-compiled into an executable, or produced by
py2app, PyInstaller or Nuitka. A `.app` is a directory with a known layout
and an `Info.plist`; this builds that directory. Every tool involved --
`pkgutil`, `install_name_tool`, `lipo`, `codesign`, `iconutil`, `hdiutil`,
`security` -- ships with macOS. The build machine additionally needs the
Command Line Tools (a C compiler, which building the Rust engine already
requires), a Rust toolchain with `aarch64-apple-darwin` and
`x86_64-apple-darwin` installed, and `maturin`.

## What is inside

About 69 MB unpacked, 27 MB as a compressed image.

```
FeetBrowser.app/Contents/
  Info.plist                     name, version, identifier, icon, 10.13+
  PkgInfo                        APPL????
  MacOS/FeetBrowser              the launcher: a real Mach-O, both arches
  Resources/FeetBrowser.icns     ten sizes, 16 to 1024
  Resources/certs/cacert.pem     the system's root certificates
  Resources/lib/feetbrowser/     the browser
  Resources/lib/feetbrowser_engine/
                                 the Rust extension, universal2
  Resources/lib/feetbrowser/_h264_<digest>.dylib
                                 the H.264 decoder, compiled, universal2
  Resources/lib/feetbrowser/_aac_<digest>.dylib
                                 the AAC decoder, the same way
  Resources/lib/fortran/         the Fortran both were built from
  Resources/lib/toes/            where discover_toes() looks
  Frameworks/Python.framework/   CPython 3.13, pruned
```

### Video and sound

`feetbrowser/h264.py` and `feetbrowser/aac.py` compile `fortran/` with
gfortran the first time a video plays. That is right for a checkout, where
there is a compiler, and impossible here. Until this was fixed every shipped
copy of the browser answered `[video: H.264: no gfortran on PATH]` to anyone
who opened a video, which no developer ever saw, because developers run from
a checkout. Fixing that for the video decoder alone left the quieter half of
the same bug: a bundle that played pictures in silence.

So both libraries are built at packaging time -- step 6 -- and ship inside
the package. Both architectures each: the arm64 halves from the build
machine's own gfortran, the x86_64 halves from `FEETBROWSER_H264_X86_64` and
`FEETBROWSER_AAC_X86_64`, which the workflow fills in from a job on
`macos-15-intel`. gfortran is not a cross-compiler in any form Homebrew
ships, so the alternative was shipping one architecture's media and not the
other's.

Each is named after a hash of the sources it was built from, and those
sources ship beside the package. The loader recomputes the hash: a library
built from a different `fortran/`, or for a different ABI, is not preferred
over the sources -- it is not found. `-static-libgfortran -static-libgcc
-static-libquadmath` mean neither needs anything from the compiler
installation, and build.sh checks that with `otool -L` rather than assuming
it. It also deletes the `LC_RPATH` entries gfortran writes pointing at its
own Cellar.

To build them by hand:

```
python3 -m feetbrowser.h264 --name              # what it has to be called
python3 -m feetbrowser.h264 --build path/to/it  # make one
python3 -m feetbrowser.aac --name               # and the same two for sound
python3 -m feetbrowser.aac --build path/to/it
```

### The launcher is a real executable, not a script

`Contents/MacOS/FeetBrowser` is compiled from `launcher.c`, which embeds the
bundled interpreter through the `PyConfig` API and runs `-m feetbrowser` in
it. The obvious alternative -- a shell script that `exec`s the interpreter --
does not work here, and the reason is worth writing down: AppKit answers
"which application am I?" with `[NSBundle mainBundle]`, which walks up from
the path of the *currently executing image*. After an `exec` that path is the
interpreter's, so the running application would be the `Python.app` nested
inside Python.framework, and it would take its name, its icon and its bundle
identifier from that plist instead of ours. `cocoa.py` calls
`setActivationPolicy:` and `activateIgnoringOtherApps:`, so it gets a Dock
tile and the keyboard either way; what it cannot get from a script is that
the tile says FeetBrowser. `appshot.py`'s `gui` stage is the check: it asks
LaunchServices who the running process is and insists on the answer.

The launcher computes every path from its own location with
`_NSGetExecutablePath`, so the bundle works from `/Applications`, from a
mounted disk image, from a folder whose name has spaces in it, and from
wherever it has been dragged and renamed.

### Which CPython, and why

python.org's official macOS installer, `python-3.13.15-macos11.pkg`, pinned
by version and by SHA-256 in `build.sh`, unpacked with `pkgutil
--expand-full` and relocated. It is already universal2, so one download
covers both kinds of Mac; it is the reference build the language's own
maintainers publish; it ships the standard library and a working OpenSSL;
and it is a framework, which is the shape macOS wants a bundled interpreter
to be in. The two alternatives were building CPython from source with
`--enable-framework` -- correct, but half an hour of runner time per
architecture to arrive at something very close to what python.org already
publishes -- and `python-build-standalone`, which is a genuinely useful
project but a third-party artifact for a security-relevant component, and is
distributed per-architecture, so it would mean two full distributions
`lipo`'d together by hand. The cost of the choice is one step: every Mach-O
in the framework records `/Library/Frameworks/Python.framework/...` as the
place its neighbours live, and `build.sh` rewrites all of those to
`@loader_path`-relative paths so the tree can be moved.

### Signing

Ad-hoc (`codesign --sign -`), inside out, after the install names are
rewritten. This is not optional: on Apple Silicon every executable page must
carry a signature, and `install_name_tool` invalidates the one a binary
arrived with, so an unsigned rewrite is killed on sight by the kernel. It is
also not a substitute for a Developer ID -- see Gatekeeper below.

### Certificates

A bundled CPython has no trust store. python.org's OpenSSL is compiled to
look under the framework's own `etc/openssl`, that directory ships empty --
which is what the installer's "Install Certificates.command" exists to fix
-- and the compiled-in path would point outside the bundle in any case. So
`build.sh` exports the system's roots at build time with

```
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain
```

into `Contents/Resources/certs/cacert.pem`, and `launcher.c` points
`SSL_CERT_FILE` and `SSL_CERT_DIR` at it before the interpreter starts.
`ssl` reads both variables every time it builds a default context, and the
launcher sets them with `setenv`'s overwrite flag at 0, so anyone who has
already chosen a trust store keeps it.

The roots are a snapshot taken on the build machine. They are exactly as
current as the macOS that built the image, and they do not update
themselves; a build a year from now picks up a year of changes.

### Nothing is written inside the bundle

A disk image is mounted read-only, and an app in `/Applications` should not
scribble on itself in any case. `launcher.c` sets `config.write_bytecode =
0` and `build.sh` compiles the whole standard library and the package ahead
of time, so there is no `.pyc` to write at startup. Bookmarks
(`~/.feetbrowser_bookmarks.json`) and shoes (`~/.feetbrowser_shoes.json`)
already live in the home directory. `appshot.py`'s `frozen` stage takes a
census of every file in the bundle before and after running it and fails if
anything changed, and the same script is run against the app on the mounted
read-only image.

One thing does write inside the bundle, and it is worth knowing about
before you meet it: `toes.py` resolves its config and its install directory
relative to the package, which inside the app is
`Contents/Resources/lib/toes/`. Reading and listing toes works anywhere.
*Installing* one writes there, so it works from `/Applications` and fails on
a mounted image. Nothing on the startup path touches it.

### What is not in there

Tcl, Tk and `_tkinter` are pruned, along with the test suite, IDLE, the
build headers, `ensurepip` and the developer tooling. `verify.sh` fails if
any of the toolkit comes back: this project deleted it, and shipping it
inside the app would be absurd.

`curl_cffi`, Pillow and cairosvg are all optional and none of them is
bundled. `net.py` imports `curl_cffi` inside a `try:` and falls back to its
own transport; `imagecodec.py` decodes PNG, GIF and PNM itself and only
reaches for Pillow and cairosvg for JPEG, WebP and SVG. In the bundle those
formats do not render, and everything else behaves exactly as it does in a
checkout without the extras installed -- which is how CI runs most of its
jobs.

The Cocoa backend builds no `NSMenu`, so the app has no menu bar and no
Cmd-Q. Closing the window quits it, as does Quit from the Dock icon. That is
the backend's business, not the packaging's, and it is mentioned here only
so it is not mistaken for a bundling mistake.

### The icon

The artwork lives once, as `packaging/art/feet.png`, and `icon.py` resamples
it to the ten iconset PNGs -- pure standard library, since macOS packaging
runs on the system Python that cannot import the browser's engine. Shrinking
is an area average of the source, so the 16pt icon is a fair sample of the
whole mark rather than one pixel in every N. `iconutil` turns the result
into `FeetBrowser.icns`.

## Universal binary

Both architectures, everywhere:

```
$ lipo -archs FeetBrowser.app/Contents/MacOS/FeetBrowser
x86_64 arm64
```

The Rust extension is built with `maturin --target universal2-apple-darwin`,
which compiles the crate once per architecture and `lipo`s the halves
together, so it cannot end up shipping only one of them. It is built with
`-i <the bundled python>` because pyo3 is not in abi3 mode here: the
extension is tied to one CPython minor version, and a wheel tagged for the
build machine's interpreter would simply not be importable in the bundle.
The launcher is compiled twice by hand -- `-mmacosx-version-min=10.13` for
x86_64 and `11.0` for arm64, which is when the hardware arrived -- and
`lipo`'d. CPython comes universal2 from python.org.

The Fortran decoders are the one thing the build machine cannot produce both
halves of. gfortran targets the machine it was installed on and Homebrew
ships no cross-compiler, so the workflow builds the x86_64 slices in a
separate job on `macos-15-intel`, uploads them, and `build.sh` takes them
through `FEETBROWSER_H264_X86_64` and `FEETBROWSER_AAC_X86_64` and `lipo`s
each with the arm64 half it built itself. Building them by hand needs the
same: two machines, or one and a pair of slices from somewhere. Everything
else about them -- the flags, the names, the ABI checks -- is the same on
both architectures and for both decoders, because all four slices go through
`python3 -m feetbrowser.<module> --build`.

`lipo -archs` proves a slice exists, not that it works, so
`.github/workflows/package-macos.yml` mounts the image on an Intel runner
and runs the app there.

## Gatekeeper

**The disk image is not signed with an Apple Developer ID and is not
notarised. That is a decision, not an oversight** -- see the end of this
section for what signing would cost and why the answer is no. Shipping
unsigned means every user meets Gatekeeper once, so the instructions below
are part of the product and need to be right.

On a Mac that downloaded the image from the internet, double-clicking
`FeetBrowser.app` shows

> "FeetBrowser" cannot be opened because Apple cannot check it for malicious
> software.

This is Gatekeeper doing its job, and there are two honest ways round it,
both of which apply to this one app and leave the rest of the system alone:

1. **System Settings > Privacy & Security.** Try to open the app, let it be
   refused, then open Privacy & Security and scroll to the bottom: the
   blocked app is named there with an **Open Anyway** button. Click it,
   confirm, and authenticate. macOS records consent for that one
   application and later launches are normal.
2. **Remove the quarantine attribute**, which is the same decision made from
   a terminal:

   ```
   xattr -d com.apple.quarantine /Applications/FeetBrowser.app
   ```

Control-clicking the app and choosing **Open** is the instruction most of
the internet still gives, and it stopped working in macOS 15 Sequoia:
Apple removed that contextual-menu override precisely because malicious
installers were talking people through it. It still works for later
launches, but never for the first one, which is the only launch that
needs it. Assume it does not work, and the System Settings route is the
one to document to anyone on Sequoia or newer.

Do **not** disable Gatekeeper globally (`spctl --master-disable`). It turns
the check off for everything you will ever download, to solve a problem with
one file, and this project is not worth that.

An app built from this repository by yourself is not quarantined at all --
the attribute is attached by the browser or mail client that downloaded the
file, not by the build -- so `build.sh` output runs on the machine that made
it with no ceremony.

Making the warning go away properly would need, and this is the whole list
-- recorded so the decision can be revisited by someone who knows what they
would be taking on, not because it is planned:
an Apple Developer Program membership (99 USD a year), a Developer ID
Application certificate to `codesign --options runtime --sign "Developer ID
Application: ..."` every Mach-O in the bundle with, a Developer ID Installer
or Application signature on the disk image, and notarisation -- uploading
the signed image to Apple with `xcrun notarytool submit --wait`, then
`xcrun stapler staple` on the result so the ticket travels with the file.
The build would grow a signing identity and an app-specific password, both
of which are secrets a fork cannot inherit. Everything else here is
deliberately arranged so that only those steps would need to be added: the
bundle is already fully signed, just ad-hoc, and the signing is already done
inside-out in the order a real identity would need.

## Checking a build

`build.sh` runs `verify.sh` on what it produced before it makes the image.
Run either by hand against any bundle, including one copied off a mounted
`.dmg`.

`verify.sh` fails on:

* a Mach-O that loads anything from outside the bundle other than `/System`
  and `/usr/lib`,
* a Mach-O missing either architecture,
* a file anywhere in the bundle containing the build machine's home
  directory -- a build path left in a `.pyc` or a panic string is a leak,
  and the Rust build is given `--remap-path-prefix` for exactly this reason,
* a missing icon, plist key, trust store, engine or launcher,
* an app that cannot decode H.264 or AAC. `verify.sh` runs the built app's
  own `--check-video` and `--check-audio`, with `PATH` cut back to the system
  directories so no gfortran and no Homebrew library can be reached, and
  compares the frame it decodes with the picture in `tests/fixtures/h264/`
  byte for byte and the samples it decodes with the reference floats in
  `tests/fixtures/aac/`. Two checks, because they are two libraries, and
  because a bundle with only the first one plays pictures in silence,
* Tcl, Tk or `_tkinter`.

Paths belonging to *upstream* CPython's own build machine are reported and
not failed on: they are inside binaries python.org published, and rewriting
somebody else's signed artifacts to hide them would be both futile and rude.

`appshot.py` runs the bundle rather than reading it, and makes the same
assertion `tests/x11_shot.py` makes about a real X11 window -- render a page
whose colours are known, count them, and insist they arrived in the right
order. It has four stages: `render` (from an unrelated working directory,
with the environment stripped to nothing and then with `PYTHONHOME`,
`PYTHONPATH` and `VIRTUAL_ENV` pointed at directories that do not exist),
`https` (a real https:// page, fetched twice -- once normally and once with
the trust store taken away, which have to differ), `gui` (launch it and ask
LaunchServices whose window it is) and `frozen` (nothing in the bundle was
written to). Set `FEETBROWSER_SKIP=gui` where there is no window server.
