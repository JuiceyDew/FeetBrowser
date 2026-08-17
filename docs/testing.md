# Tests

```bash
./test.sh          # builds the Rust JS engine, then pyflakes + unit + JS + pixels + navigation + toe + Go + live smoke
test.cmd           # the same suites in the same order, on Windows
```

`test.sh` builds the Rust JS engine (`feetbrowser_engine`) into the local
`.venv` with `maturin develop --release` on first use and runs every Python
suite from that venv. A first run therefore needs a Rust toolchain (maturin
is installed into the venv automatically).

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), and `test_toes.py`
(which points the hub at a `file://` catalog in a temp dir). `test_nav.py` and
`smoke.py` load real sites over the network, so both need connectivity when
run the way `test.sh` runs them; [CI](../.github/workflows/ci.yml) points them
at `tests/fixtures` instead, so a pull request neither depends on a third
party being up nor sends them traffic.

The test files live in `tests/`:

```
tests/
  test_suites.py    every file below is run by test.sh and by CI, or this fails
  test_render.py    fonts, rasteriser, image codecs, canvas, event model
  test_cocoa.py     the macOS window, driven by real NSEvents (macOS only)
  test_x11.py       the X11 window, driven by real X events (needs a server)
  x11_shot.py       photographs a real X11 window with XGetImage (CI artifact)
  test_wayland.py   the Wayland window, driven by real compositor events
                    (needs a compositor; CI runs weston headless)
  test_win32.py     the Windows window, driven by real messages (Windows only)
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge
  test_shoes.py     the Shoes theme manager
  test_e2e.py       a fixture page in, its pixels back out
  test_nav.py       click-to-navigate, history, view-source
  download_cases.py downloads, against a local server (run from test_nav.py)
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  test_asmx11.py  the assembly pixel-packing kernels against their Python references
  test_asmselect.py the assembly selection nearest-boundary kernel against its Python reference
  smoke.py          end-to-end pipeline over a real socket
  fixture_server.py serves tests/fixtures over HTTP on loopback
  fixtures/         the pages the three end-to-end suites load
```

`test_e2e.py` is the one that looks at the screen. It fetches a page carrying
text, a PNG, a GIF, a background colour and a border, renders it to a PNG, and
then counts the colours in that PNG: each of those five things has a shade of
its own, so a layer that stops drawing takes a colour off the picture and the
test says which. It exists because `<img>` once stopped drawing anything at
all, on every page, and the suite had nothing that could tell.

`download_cases.py` is the one file here that is not named `test_*.py`, and
that is deliberate: it is a suite of its own: a local HTTP server serving
known lengths, chunked bodies, a connection cut mid-transfer and a shelf of
hostile filenames, but it runs from the end of `test_nav.py` rather than from
a runner, because saving a file is where a navigation ends. Naming it
`test_downloads.py` would put it in front of `test_suites.py` below, which
would then demand a line in `test.sh`, `test.cmd` and the workflow.

`test_suites.py` is the reason a new file in `tests/` cannot be forgotten.
`test.sh`, `test.cmd` and the workflow all name their suites one at a time:
the first two so the order and the comments are readable, the third so a red
job says which suite went red; and this fails if a file in `tests/` is
missing from any of them.

`test.sh` and `test.cmd` run each suite through `tests/watchdog.py`, which
gives it a deadline. Several suites start HTTP servers, open real windows or
reach the network, and any of those can stop forever rather than fail; a run
that hangs reports nothing, and interrupting it prints a traceback from
wherever the interrupt landed rather than from whatever was stuck. The
watchdog arms `faulthandler`'s timer instead, so passing the deadline dumps
every thread's stack (naming the line that hung), and exits non-zero. It is
a timer thread rather than `signal.alarm`, which is what makes it work on
Windows too. `FEETBROWSER_TEST_TIMEOUT` overrides the 900 seconds, and `0`
turns the deadline off for stepping through a suite in a debugger. CI invokes
the suites directly and so runs without it, relying on the job timeout.

**The decoder suites are not in this repository.** `test_h264.py`,
`test_aac.py`, `test_mp3.py` and `test_pcm.py` moved to
[feetplayer](https://github.com/67plays/feetplayer) with the decoders they
test, and feetplayer's own `test.sh` runs them. Nothing about them was
weakened in the move: H.264 is still compared sample for sample, AAC and
Layer III numerically against thresholds that are the measured error with a
small margin, the stages that can be compared exactly are still compared
exactly, and the tests whose job is the *fixtures* rather than the decoder --
the ones that assert the stereo tools, the noise substitution, the TNS filter
and all eight scalefactor band layouts are still being reached -- went with
them.

What this repository still tests of the media stack is the seam:
`test_render.py` drives `feetplayer.mediacodec` through `feetbrowser.media`
for containers, scheduling, dropping and resync, and `test_audio.py` is the
browser's half of the sound path -- the clock a `<video>` is scheduled
against, a seek, and a `<video>` element asking for its own audio. A
feetplayer whose interface had drifted fails here by name, which is what a
pinned dependency is for.

**Four binary fixtures stayed, and they are not read by any suite in this
directory.** `tests/fixtures/h264/mb1.264`, `mb1.i420.z`,
`tests/fixtures/aac/lowrate.aac` and `lowrate.f32.z` are read by the three
*packagers*. A machine with no gfortran is exactly what a packaged
application runs on, and until the packaging started shipping a compiled
decoder every downloaded copy of the browser refused H.264 while every
developer's checkout played it. That is checked where it can only be checked
-- inside the built artifact, by `packaging/macos/verify.sh`,
`packaging/linux/verify-in-container.sh` and
`packaging/windows/verify-bundle.ps1`, each of which runs the bundle's own
`--check-video` and `--check-audio` against these four files with `PATH` cut
back so no compiler and no stray runtime library can answer for it. Deleting
the fixture directories along with the suites that used to read them would
have broken all three packagers, and nothing in `tests/` would have noticed.

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.

The exceptions are `test_cocoa.py`, `test_x11.py` and `test_win32.py`, and
deliberately so. They open real windows and feed them real platform events,
because the
platform layer is the one place a mistake is invisible from Python: a stale
attribute in the mouse path once swallowed every click with the browser
underneath looking healthy. Each skips itself with a message where its
platform is not there, so the suite is green on all three either way.

Real windows have real manners, and a suite that opens dozens of them in a
few seconds inherits all of them: each one centres itself on the display,
raises above everything and takes the keyboard, so for as long as the run
lasts the machine belongs to the tests. `FEETBROWSER_QUIET` drops exactly
those three habits and nothing else: a quiet window is still created,
mapped, sized, drawn into and sent real events, so every assertion in those
suites is testing what it tested before. `test.sh` and `test.cmd` set it, so
the ordinary way of running the tests already leaves you your machine; set
`FEETBROWSER_QUIET=0` when you want to watch the windows work.

The mechanism differs per platform because the manners do: macOS runs the app
under the accessory activation policy (no Dock icon, never becomes the active
app) and orders each window in at the back instead of making it key; X11 maps
the window override-redirect, which is the one portable way to tell a window
manager not to place, decorate, raise or focus something; every other route
is a hint it may ignore; Windows shows the window with `SW_SHOWNOACTIVATE`
and skips `SetForegroundWindow`. Each backend's suite asserts the promise
rather than the mechanism: open a quiet window, then check the keyboard did
not move to it and that the window is nonetheless real and the size asked
for. That last half matters: quiet is only worth having if the window it
leaves behind is still the one the other tests are reading pixels off.

One thing quiet does not do is make the windows invisible. On macOS they sit
behind whatever you are working in; under X11 they map where the server puts
them. Not ordering the window in at all was tried and does not work: AppKit
gives an unordered window no usable backing store, and seventeen tests fail.
If you want silence rather than good manners, the window suites skip cleanly
when their platform is absent: quit XQuartz and `test_x11.py`'s live half
steps aside on its own.

`test_x11.py` splits in half. The arithmetic and the lookup tables (scanline
padding, the byte layout a visual's channel masks imply, keysym names, wheel
buttons) are plain functions over plain values, and those tests run
everywhere, including on macOS and Windows. The rest needs a server, and asks
it real questions: XGetGeometry for the window's true size, XSendEvent for
input, and XGetImage to read the frame back off the server and check the
colours arrived in the right order. CI runs that half on Linux under
`xvfb-run`, and `x11_shot.py` uploads the resulting window as a PNG so a human
can see what the Linux build actually drew, after checking that the three
colour swatches on that page came back present and in order, which is what a
wrong channel mask or byte order permutes.

`test_wayland.py` splits the same way, and the shape of the halves follows
from the protocol. The pure half (XRGB8888 packing, fixed-point conversion,
button numbering, keysym and scroll-step translation) runs everywhere. The
live half needs a compositor, and since a compositor owns the pixels after we
hand them over, it reads them back out of the shared-memory buffer the client
itself mmaps -- the same honesty as XGetImage, from the only place that can
answer. Input is the one thing the live half cannot exercise: weston's
headless backend has no seat, so no pointer and no keyboard exist to send
events from. That path is covered by the pure translation tests and by
reading, and is the first thing to verify on a real desktop.

`test_win32.py` splits the same way, and for the same reason. Its offline
half (DIB stride rounding, the BGRX byte order, virtual-key to keysym
translation, the wheel-delta arithmetic) runs anywhere. Its other half opens
real windows, pumps real messages through the window procedure and reads
pixels back out of GDI, and that half only runs on the `windows-latest` rows
of the matrix. Treat those rows as the verification of anything in `win32.py`:
nothing else executes a line of it, and until they existed nothing ever had.

Two things they do not verify, and it is worth being plain about which. The
runners are headless and run at 96 DPI, so neither the DPI handling nor
`WM_DPICHANGED` is ever exercised; and nothing there drags a window by its
title bar, so the timer that keeps the browser running inside Windows' modal
loops is not exercised either. Those parts are written against the API
documentation and checked by reading.

CI runs the offline suite on every interpreter the engine supports (3.9
through 3.14) and on macOS and Windows as well as Linux, so `test_cocoa.py`
and `test_win32.py` open real windows somewhere rather than only proving
their skips are clean.

One job, `unused-image-libraries`, exists to be the negative. Every other job
runs on a machine with no Pillow and no cairosvg, which means an import of
either would fail there and prove nothing: a browser that cannot reach for a
library and a browser that does not are indistinguishable when the library is
absent. So that job installs both, checks they really are importable, and runs
the suites; `test_units.py` and `test_e2e.py` both assert afterwards that
neither module reached `sys.modules`, the second of them after fetching and
drawing a page with a photograph on it.

The Linux jobs build the Rust engine and run `test_js.py` once against it;
the macOS and Windows lines do the same. The Rust engine's own tests are
covered by the `rust` job, which never crosses into Python, so running them
once says as much as running them on all eight interpreters would.
