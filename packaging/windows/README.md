# Packaging FeetBrowser for Windows

This directory turns the repository into `FeetBrowser.exe`: a folder somebody
can unzip anywhere and double-click, on a Windows machine with no Python, no
Rust and no Visual Studio on it.

```
packaging/windows/
    build.ps1              assembles the bundle
    verify-bundle.ps1      proves an assembled bundle works
    launcher/              the Rust crate that becomes FeetBrowser.exe
    bundle/                the files that get copied into the bundle as-is
```

`.github/workflows/package-windows.yml` runs both scripts: `build.ps1` on one
runner, `verify-bundle.ps1` on a second, clean one.


## What the bundle is

Three things in one directory:

1. **CPython**, from python.org's [Windows embeddable
   package](https://www.python.org/downloads/windows/), a zip of a plain
   interpreter with no installer, published specifically to be redistributed
   inside other applications. The version and its SHA-256 are pinned at the
   top of `build.ps1` and the hash is checked on every build.
2. **`feetbrowser/`**, copied in verbatim. It is pure Python; there is nothing
   to do to it.
3. **`feetbrowser_engine\`**, taken out of a maturin wheel: the Rust
   rasteriser, font engine, image decoders and JavaScript engine. Current
   maturin wraps the extension in a package of its own: a directory holding
   `feetbrowser_engine.cp313-win_amd64.pyd` and a three-line `__init__.py`
   that re-exports it. `build.ps1` copies whatever shape the wheel has
   rather than assuming one, so a future maturin that goes back to a bare
   `.pyd` at the root of the wheel will bundle just as happily; what it
   insists on is that there is exactly one engine `.pyd` in there.

4. **The decoders**: `feetbrowser\_h264_<digest>.dll` and
   `feetbrowser\_aac_<digest>.dll`, compiled from `fortran\` by gfortran at
   packaging time, with `fortran\` itself shipped beside the package.
   `feetbrowser\h264.py` and `feetbrowser\aac.py` otherwise compile those
   sources on demand, which is right for a checkout and impossible on a
   user's machine -- and the failure was silent to everyone who had a
   compiler, which is everyone who develops the browser. Shipping the video
   one alone left the same bug wearing a quieter costume: a bundle that
   plays pictures with no sound. Each DLL is named after a hash of the
   sources it was built from and the loader recomputes it, so a stale one is
   not preferred over the sources, it is not found.
   Whether gfortran's runtime ends up inside those DLLs or beside them is
   decided by reading each finished file's import table: `build_library`
   tries `-static` first and falls back through the narrower static flags,
   and copies whatever it still could not link in -- `libgfortran-5.dll`
   and its own dependencies -- next to the decoder, where
   `LOAD_WITH_ALTERED_SEARCH_PATH` finds them. Both decoders land in the
   same directory and want the same runtime, so whichever is built second
   finds those DLLs already there. It is a build-time check
   rather than a claim because the flags that fail here fail invisibly:
   every one of them links, and the difference only shows up on a machine
   that has no compiler. The verification job is what proves it, running
   with `PATH` cut back to the system directories.

Plus `FeetBrowser.exe`, which is the subject of most of this document.

Nothing is frozen. There is no PyInstaller, no cx_Freeze, no py2exe and no
Nuitka, and that is a deliberate choice rather than an oversight. Every
freezer works by guessing which modules the program imports and then
rewriting how imports work, which is exactly the sort of machinery this
project exists to not have. The embeddable package needs none of it: it is a
normal interpreter that finds a normal `feetbrowser/` directory on a normal
`sys.path`. The bundle is inspectable (the `.py` files are right there, and
`python.exe` in the folder is a working REPL with the browser importable),
and the failure modes are the ones any Python programmer already knows.

### The layout, and why everything is in one flat directory

```
FeetBrowser\
    FeetBrowser.exe            the launcher
    FeetBrowser._pth           sys.path
    python313.dll              CPython
    python3.dll                the stable-ABI forwarder, unused but shipped
    python313.zip              its standard library
    python313._pth             sys.path again, under CPython's own name
    python.exe, pythonw.exe    CPython's interpreters, kept for debugging
    _ssl.pyd, _socket.pyd, ... CPython's extension modules
    libssl-3.dll, libcrypto-3.dll, libffi-8.dll, sqlite3.dll
    vcruntime140.dll, vcruntime140_1.dll
    python.cat                 Microsoft's signature catalogue for the above
    LICENSE.txt                the PSF license, for all of the above
    feetbrowser_engine\        the engine, as maturin packages it
    feetbrowser\               the browser
    toes\                      where extensions get installed
    LICENSE, README.md, README-FIRST.txt
    install.ps1, uninstall.ps1
```

A tidier tree (`runtime\` for CPython, `app\` for us) was the first
instinct and is a mistake. Windows searches an executable's own directory for
its DLLs first, so `FeetBrowser.exe` has to sit next to `python313.dll`; and
`._pth` resolution is defined relative to the interpreter, so splitting the
tree means the path file has to describe a layout instead of just being in
it. One directory removes both problems and matches how the embeddable
package is documented to be used. It looks cluttered. It is thirty files, all
of them explicable, and none of the interesting failure modes.

### `._pth`, which is the whole mechanism

The embeddable package ships a file called `python313._pth`:

```
python313.zip
.
```

Its presence is a switch. When CPython starts and finds a `._pth` beside the
DLL or the executable, it stops doing everything else it normally does to
work out `sys.path`: `PYTHONPATH` is ignored, `PYTHONHOME` is ignored, the
registry is ignored, `site` is not imported, there is no `site-packages` and
no user site directory. `sys.path` becomes exactly the lines in the file,
resolved relative to the directory the file is in.

For us, those two lines already say the right thing. `python313.zip` is the
standard library; `.` is the bundle directory, which is where `feetbrowser\`
and `feetbrowser_engine\` are. So the "how do we get our package
importable given that the embeddable package has no pip and no site-packages"
problem has a one-word answer: we don't need to, they are already on the
path.

`build.ps1` writes a second copy called `FeetBrowser._pth`. CPython looks for
a `._pth` named after the DLL *and* one named after the running executable,
and which of those two wins has shifted between releases; with both present
and identical, the answer is the same either way.

This file is also what makes the isolation claim true rather than hopeful,
and the CI job leans on it directly: it sets `PYTHONHOME` and `PYTHONPATH` to
a directory that does not exist before running anything. If the `._pth` ever
stopped taking effect, the interpreter would fail to start, loudly, in CI,
rather than quietly borrowing a Python from somebody's machine.

### What is in the standard library zip, and what is not

The stdlib arrives as `python313.zip`, bytecode only, no `.py` sources. Every
top-level module `feetbrowser/*.py` imports:

```
base64 codecs collections copy ctypes hashlib heapq html importlib itertools
json math os platform re shutil socket ssl struct subprocess sys tempfile
threading time traceback urllib zlib
```

That list is accounted for: most from the zip, `itertools`, `math`, `sys`, `time` and
`zlib` compiled straight into `python313.dll`, and the ones with a C half from
the `.pyd` files beside it. `ssl` needs `_ssl.pyd`, `libssl-3.dll` and
`libcrypto-3.dll`; `socket` needs `_socket.pyd` and `select.pyd`; `ctypes`
needs `_ctypes.pyd` and `libffi-8.dll`. The embeddable package includes every
one of them. `verify-bundle.ps1` asserts it by importing them inside the
bundle rather than trusting this paragraph, because https is not a
nice-to-have in a web browser.

Two consequences of a bytecode-only stdlib worth knowing: tracebacks through
standard library frames show no source lines, and `inspect.getsource` on a
stdlib function fails. Neither affects the browser.

Nothing is pruned. `sqlite3.dll` and `winsound.pyd` are dead weight the
browser will never touch, and they stay, because the CPython half of the
bundle is then byte-for-byte what python.org published, which keeps
`python.cat` meaningful, keeps the licensing question to "we redistributed
it unmodified", and means a CPython bump is a two-line change rather than a
re-derivation of a file list.

### The one optional third-party import

`net.py`'s `URL.request_impersonated()` does this:

```python
try:
    from curl_cffi import requests as cffi
except ImportError:
    return self.request()
```

That `except` is the only thing between a packaged browser and a traceback
the first time somebody navigates to a site that asks for impersonation,
because a bundle with no pip in it will never have `curl_cffi` and every user
is therefore on the fallback path. `verify-bundle.ps1` asserts both halves:
that the module really is absent, and that a navigation through
`request_impersonated()` still comes back with a page.

It is the only such import left in the browser. Images are the engine's own
PNG, GIF, JPEG and Netpbm decoders, so there is no Pillow to miss; the
self-check asserts that `PIL` and `cairosvg` are not importable either, on
the grounds that a Python image pipeline picks those up by accident more
easily than anything else.


## `FeetBrowser.exe`

Windows cannot double-click a `.py` file, so something has to be a real PE
binary. `launcher/` is that binary: about 250 lines of Rust with no
dependencies at all.

Rust is already a build dependency of this project, so the launcher costs
nothing new, and a `.exe` gives us the three things a `.bat` or a `.py` could
never have: an icon in Explorer and on the taskbar, no console window
flashing up on launch, and version information an administrator can read.

### Loading `python313.dll` rather than spawning `python.exe`

The two ways to write this launcher are to run `python.exe -m feetbrowser` as
a child process, or to load `python313.dll` and call `Py_Main` in-process.
The child-process version is simpler and more robust and it is not what this
does, so here is the reasoning.

Loading the DLL wins on three specific things:

* **One process.** One PID, one thing for Task Manager and for a kill script,
  and a taskbar button that belongs to `FeetBrowser.exe` and carries its
  icon. The child-process version has the window owned by a `pythonw.exe`
  that the user never asked for.
* **No console, without fighting for it.** `python.exe` in the embeddable
  package is a console-subsystem binary; launching it from a GUI process
  pops a console unless it is suppressed with `CREATE_NO_WINDOW`, and then
  `--version` has nowhere to print. `pythonw.exe` avoids the console by never
  having any output at all. In-process, the question does not arise: this
  binary is `windows_subsystem = "windows"` and there is no second process.
* **DPI.** `python.exe` ships a manifest that marks the process DPI-aware
  before any FeetBrowser code runs, which makes `win32.py`'s own
  `SetProcessDpiAwarenessContext` call fail and leaves the browser on the
  coarser system-DPI path. Our manifest deliberately says nothing about DPI
  so that the window backend gets to choose per-monitor-v2 for itself.

The cost is a `LoadLibraryExW` + `GetProcAddress` + one `transmute`, and the
risk that `Py_Main` is not where we think it is, which is checked at run
time, with a message box rather than a silent exit, and by CI on every build.

`Py_Main` is part of CPython's stable ABI and is exported by every
`python3NN.dll`, so it is found at run time rather than linked against. That
also means no import library and no Python headers are needed to build the
launcher, which is just as well: the embeddable package ships neither. The
DLL is located by scanning the launcher's own directory for `python3NN.dll`,
so bumping CPython in `build.ps1` does not require rebuilding the launcher.

The interpreter is handed:

```
FeetBrowser.exe -X utf8 -m feetbrowser <everything the user typed>
```

`-m feetbrowser` because `feetbrowser/__main__.py` is the browser's real CLI:
`--help`, `--version`, `--screenshot`, the `--toe-*` family and a bare URL.
Python stops parsing its own options at `-m <module>`, so user arguments
reach `sys.argv` untouched even when they start with a dash. `-X utf8` so
that a page title printed to a redirected stdout does not die on the console
codepage.

### Console output from a windowless binary

A `windows`-subsystem binary has no console, which is the entire point; but
`FeetBrowser.exe --version` typed at a prompt still has to print something.

Redirection already works: `cmd.exe` passes its standard handles to GUI
processes exactly as it does to console ones, so `FeetBrowser.exe --version >
out.txt` writes the file with no help from us. The interactive case is
handled with `AttachConsole(ATTACH_PARENT_PROCESS)`, and only when
`GetStdHandle` shows nothing already there; stealing the handles back for
the console when the user asked for a file would throw their output away.

The one visible artefact is the standard one for this technique: `cmd.exe`
returns its prompt immediately and the output arrives after it. That is the
price of not flashing a console box at everybody who double-clicks the icon.

### Icon, version block, manifest

`launcher/resources/` holds an `.rc` script, a `.manifest` and a `.ico`.
`build.rs` finds `rc.exe` in the Windows SDK, compiles the script, and passes
the resulting `.res` to the linker as one `rustc-link-arg`. The usual answer
here is the `winres` or `embed-resource` crate; doing it directly is about
the same amount of code and keeps the launcher's dependency list empty, which
is the one thing about this project genuinely worth protecting.

Without an SDK, `build.rs` warns and produces a working but unadorned binary;
`build.ps1` sets `FEETBROWSER_REQUIRE_RESOURCES=1`, which turns that warning
into a hard error, so the thing users download always has its icon.

The manifest is parsed by `build.ps1` before anything is compiled, which
looks like belt and braces and is not. `rc.exe` embeds the file without
reading it, and a manifest that is not well-formed XML does not degrade
gracefully at run time: the loader refuses to create the process, and the
only thing anybody sees is a message box saying "the side-by-side
configuration is incorrect", which mentions neither XML nor the file. This
cost a CI run to a double hyphen inside an XML comment, where it is illegal.
Now it is a build error with a line number.

`launcher/resources/FeetBrowser.ico` is a committed resample of the one
artwork file, the shipped `feetbrowser/icon.png`: seven sizes, PNG entries.
Nothing generates it at build time -- the launcher just links the committed
blob.


## Building it

On Windows, with Rust, a gfortran (MSYS2's, Strawberry Perl's, or
`choco install mingw` -- `build.ps1` looks for all three and takes
`FEETBROWSER_GFORTRAN` over any of them) and a Python whose minor version
matches the pinned one (3.13):

```powershell
python -m pip install maturin
maturin build --release --manifest-path rust/Cargo.toml --out dist
.\packaging\windows\build.ps1
```

The result is `build\windows\FeetBrowser\` and
`build\windows\FeetBrowser-windows-x64.zip`. The first run downloads the
embeddable package into `packaging\windows\.cache\`; later runs reuse it.

The wheel's interpreter tag is checked against the pinned CPython version.
The extension is not `abi3` (`wheels.yml` explains why in detail), so a
`cp312` wheel in a 3.13 bundle would build a zip that fails on the user's
machine with `DLL load failed while importing feetbrowser_engine`. That is
the single most likely way to ship a broken bundle, so `build.ps1` refuses
rather than warns.

`build.ps1 -SkipLauncher` assembles everything but the `.exe`, for looking at
the layout on a machine with no Rust. The result is not shippable.

### Why CI builds its own engine instead of using `wheels.yml`'s

`wheels.yml` already produces Windows `cp39`–`cp314` wheels, and the `cp313`
one is exactly what this bundle needs. The packaging workflow builds its own
anyway, because `wheels.yml` runs on `rust/**` pull requests and on tags, so
on a pull request that only touches `packaging/`, no wheel exists for that
commit. Fetching one would mean reaching across workflow runs for the newest
green build of some *other* commit, and the artifact would then contain an
engine that was never tested against the code shipped beside it. One
interpreter's worth of maturin is about ninety seconds. On a tag both
workflows run from the same commit and produce the same engine.


## Verifying it

`verify-bundle.ps1` takes an unpacked bundle and drives it. It is meant to be
run somewhere the project does not exist:

```powershell
.\packaging\windows\verify-bundle.ps1 -Root 'C:\Program Files\FeetBrowser'
```

It sets `PYTHONHOME` and `PYTHONPATH` to a directory that does not exist
before it starts, so it proves something even on a developer's own machine.
The checks are:

1. the folder has the files it claims to, one `python3NN.dll` and one
   `feetbrowser_engine*.pyd`;
2. `FeetBrowser.exe` is subsystem 2 (`WINDOWS_GUI`), read straight out of the
   PE header ("no console window flashes up" is a property of that one
   16-bit field), and carries version information;
3. `--version` and `--help` answer correctly;
4. from inside the bundle: the interpreter and every `sys.path` entry are
   under the bundle root, the poisoned environment variables were ignored,
   there is no `site-packages`, `curl_cffi`/`PIL`/`cairosvg` are genuinely
   absent, the engine imports, and OpenSSL loads CA certificates out of the
   Windows certificate store;
5. https works, through `feetbrowser.net.URL` itself, and
   `request_impersonated()` falls back cleanly without `curl_cffi`;
6. a page served over a real socket by the bundle's own `python.exe` renders
   to a PNG, which is then decoded by the bundle's own image decoder and
   counted: 640×320 pixels of `#1e90ff` for the swatch and a few thousand red
   ones for the heading, so layout, the font engine and the rasteriser all
   have to have run;
7. `--screenshot` writes to a path with a space and an accent in it;
8. launching it the way Explorer does (no redirected handles, just a URL),
   producing a real `HWND` within a minute;
9. `--check-video` finds the prebuilt H.264 decoder, loads it, decodes
   `mb1.264` and compares the result with the picture a reference decoder
   produced, byte for byte; `--check-audio` does the same for the AAC
   decoder with `lowrate.aac`, numerically rather than byte for byte,
   because AAC is not a bit-exact format. All four fixtures travel with this
   script in the `verify-script` artifact, because the job that runs it has
   no checkout to take them from.

In CI all of this happens twice, on a runner that never checks the repository
out: once from `C:\Program Files\FeetBrowser`, and once from a directory
called `Café Über Test`. The runner's own Python is renamed out of the hosted
tool cache and `PATH` is cut back to the system directories, and a step
asserts that `python`, `python3`, `pythonw`, `py`, `pip` and `conda` are all
unreachable before anything else runs. That assertion earns its keep: the
image also has `py.exe`, the PEP 397 launcher, sitting in `C:\Windows`
itself, where cutting `PATH` back to the system directories does not reach
it. It gets renamed too.


## Installing it

There is no `setup.exe`, and that is a decision rather than a gap.

A self-extracting installer written in Rust would have meant hand-rolling COM
calls to `IShellLink` for the Start Menu shortcut, an embedded payload, an
uninstaller that can delete the directory it is running from, and a first
Windows-only code path in this repository that nobody could test locally.
WiX would have meant an MSI toolchain as a real third-party build dependency
for a project whose entire premise is not having those. Either could be done
well; neither could be done well *and* verified in this change, and a shaky
installer is worse than no installer.

What ships instead is `install.ps1` in the bundle, which does the part of an
installer that a portable folder genuinely cannot do for itself:

* copies the folder to `%LOCALAPPDATA%\Programs\FeetBrowser`;
* creates a Start Menu shortcut, via `WScript.Shell`;
* registers under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\FeetBrowser`, so
  it appears in Settings → Apps like anything else, with a working Uninstall
  button.

Per user, no administrator rights, nothing outside `HKCU` and the user's own
profile. `uninstall.ps1` reverses all three, and CI runs the round trip and
checks that nothing is left behind. Users right-click and choose "Run with
PowerShell"; it is not a double-click, and that is the honest cost of the
decision.

If a real installer is wanted later, the portable bundle is the payload for
it either way, so nothing here is wasted work.


## Signing, and the warning users will see

**The binaries are not signed, and this change does not pretend otherwise.**

Downloading the zip will make SmartScreen warn, and the first run of
`FeetBrowser.exe` will show "Windows protected your PC" with a **More info →
Run anyway** to get past it. The zip itself arrives with a Mark-of-the-Web,
so Windows may also want it unblocked via Properties → Unblock before it will
extract cleanly. `README-FIRST.txt` in the bundle tells users all of this in
plain language, and tells them what to check instead of a signature.

Unsigned is the chosen way to ship this, not a stage on the way to signing.
What signing would take is recorded here so the decision can be revisited by
someone who knows what they would be taking on:

* An OV or EV code-signing certificate from a CA: a few hundred dollars a
  year, and issued only to a verified legal entity, which a pseudonymous
  hobby project is not.
* Since June 2023, the private key must live on FIPS 140-2 Level 2 hardware:
  a USB token, an HSM, or a cloud signing service. A key in a CI secret is no
  longer an option for a publicly trusted certificate.
* `signtool sign /fd sha256 /tr <timestamp-url> /td sha256` over
  `FeetBrowser.exe`, and separately over the `.pyd`, since a signed `.exe`
  says nothing about the rest of the folder.
* Reputation. An OV certificate does not silence SmartScreen on day one; the
  warning fades as installs accumulate. Only EV certificates get immediate
  reputation, and they are the expensive ones.
* Azure Trusted Signing is the cheap modern route (about $10/month, no
  hardware token) but requires an organisation that has existed for three
  years or more.

An unsigned build that says so is the honest artifact. A build that claimed
to be signed, or that told users to disable SmartScreen, would be worse than
the warning.


## Reproducing a build

Everything that goes into the bundle is pinned or committed:

* the CPython version *and its SHA-256*, in `build.ps1`; a mirror serving
  something else fails the build rather than shipping;
* the launcher's `Cargo.lock`, which has no dependencies in it to drift;
* the engine, built from `rust/` at the commit being packaged.

The launcher is built with `--remap-path-prefix` so the build machine's
directory layout does not end up inside the binary.

The bundle is not bit-for-bit reproducible: the Rust compiler version, the
MSVC linker version and PE timestamps all vary between runs. Making it so
would mean pinning a toolchain and passing `/Brepro`, which is a worthwhile
change and a separate one.


## Requirements

64-bit Windows 10 or later, which is what CPython 3.13 requires. The UCRT
(`api-ms-win-crt-*`) is part of the OS from Windows 10 onwards; the MSVC
runtime the engine needs (`vcruntime140.dll`, `vcruntime140_1.dll`) is in the
bundle. There is no arm64 build: cross-compiling one is easy and there is no
runner to test it on, and the browser has no window backend for it yet.
