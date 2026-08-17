@echo off
rem Run the FeetBrowser test suite on Windows. The counterpart of test.sh, and
rem it runs the same suites in the same order.
rem
rem The renderer draws into its own framebuffer, so nothing here needs a
rem display or a toolkit. The JavaScript engine is the Rust extension
rem feetbrowser_engine, so the suite runs out of the local venv maturin builds
rem it into, which needs a Rust toolchain and, on Windows only, a C++ linker
rem to go with it -- see the messages at the bottom of this file.
rem
rem A few suites step outside all that: test_win32.py opens real windows here
rem (test_cocoa.py and test_x11.py skip, as they do everywhere but their own
rem platform), and test_nav.py and smoke.py reach the network.
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found on PATH. Install it from python.org, or run
  echo this script's commands with "py -3" in place of "python".
  exit /b 1
)

rem test_win32.py opens dozens of real windows in a few seconds, and each one
rem would otherwise take the foreground and the keyboard -- which makes the
rem machine unusable for as long as the run lasts. QUIET drops that and
rem nothing else: the windows are still created, shown, drawn into and sent
rem real messages. Set FEETBROWSER_QUIET=0 to watch them work.
if not defined FEETBROWSER_QUIET set FEETBROWSER_QUIET=1

rem Same venv run.cmd builds, and unsealed for the same reason: the optional
rem image decoders (Pillow, cairosvg) live in the system python, and tests
rem that run without them are not testing what a user runs.
if not exist ".venv\Scripts\python.exe" python -m venv --system-site-packages .venv
if not exist ".venv\Scripts\python.exe" exit /b 1

rem A venv made before that flag existed is sealed, and a venv is only ever
rem created once, so the decoders would stay invisible on every machine that
rem already has one. Re-running venv over it rewrites pyvenv.cfg and leaves
rem what is installed inside untouched.
findstr /i /c:"include-system-site-packages = false" ".venv\pyvenv.cfg" >nul 2>&1
if not errorlevel 1 python -m venv --system-site-packages .venv
set PY=.venv\Scripts\python.exe

rem Every suite below runs behind a deadline. Several of them start HTTP
rem servers, open real windows or reach the network, and any of those can
rem stop forever rather than fail -- at which point the run says nothing at
rem all, which is exactly the report that is impossible to act on. The
rem watchdog turns a hang into every thread's stack and a non-zero exit.
rem See tests\watchdog.py; FEETBROWSER_TEST_TIMEOUT overrides the number.
set RUN=%PY% tests\watchdog.py 900

rem feetplayer, the media stack: the container readers, the audio output and
rem the Fortran decoders, which used to live in this tree and are their own
rem repository now. requirements.txt pins it to a commit; pip compiles the
rem Fortran during the install, which takes a minute, so the pin installed is
rem compared against the pin asked for and nothing is done when they agree.
rem "pip freeze" prints a VCS install as the requirement line that produced
rem it, which is why the comparison is a plain string match.
for /f "usebackq delims=" %%L in (`findstr /v /r /c:"^ *#" /c:"^ *$" requirements.txt`) do set "WANT=%%L"
"%PY%" -m pip freeze 2>nul | findstr /x /c:"%WANT%" >nul
if errorlevel 1 "%PY%" -m pip install -q -r requirements.txt || exit /b 1

rem Ensure the Rust JS engine (feetbrowser_engine) is built in the local venv.
"%PY%" -c "import feetbrowser_engine" >nul 2>&1
if not errorlevel 1 goto built
where /q cargo
if errorlevel 1 goto norust
"%PY%" -m pip install -q maturin || exit /b 1
".venv\Scripts\maturin.exe" develop --release --manifest-path rust/Cargo.toml
if errorlevel 1 goto nolinker
:built

rem The Rust half has 60 tests of its own -- the regexp engine, the CSS
rem matcher, the layout reproductions. They ran nowhere: CI only ever did
rem `cargo check --all-targets`, which compiles a test without running it.
rem They cost hundredths of a second. The tokenizer and tree builder are
rem `footnote`'s now, and it runs its own suite.
where /q cargo
if errorlevel 1 goto nocargotest
cargo test -q --manifest-path rust/Cargo.toml || exit /b 1
:nocargotest

"%PY%" -c "import pyflakes" >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip install -q pyflakes || exit /b 1
)

"%PY%" -m pyflakes feetbrowser tests || exit /b 1
rem Every suite below, and nothing missing.
%RUN% tests\test_suites.py || exit /b 1
rem The from-scratch Discord Rich Presence client, against a fake local socket.
%RUN% tests\test_discord.py || exit /b 1
%RUN% tests\test_render.py || exit /b 1
rem test_win32.py opens real windows here; the other two skip, and it is the
rem one that skips everywhere else.
%RUN% tests\test_cocoa.py || exit /b 1
%RUN% tests\test_x11.py || exit /b 1
%RUN% tests\test_win32.py || exit /b 1
rem test_wayland.py has no wayland here; its offline half runs and the live
rem half says so.
%RUN% tests\test_wayland.py || exit /b 1
rem A <video> element's soundtrack, and the pictures that follow it. The
rem audio stack itself is feetplayer's now, and is tested there.
%RUN% tests\test_audio.py || exit /b 1
%RUN% tests\test_units.py || exit /b 1
rem The version guard release.yml runs before it builds anything.
%RUN% tests\test_release_version.py || exit /b 1
%RUN% tests\test_js.py || exit /b 1
%RUN% tests\test_shoes.py || exit /b 1
%RUN% tests\test_settings.py || exit /b 1
rem A fixture page in, its pixels back out.
%RUN% tests\test_e2e.py || exit /b 1
%RUN% tests\test_nav.py || exit /b 1
%RUN% tests\test_toes.py || exit /b 1
rem No assembler here, so this checks the pure-Python fallback.
%RUN% tests\test_asmx11.py || exit /b 1
%RUN% tests\test_asmselect.py || exit /b 1
%RUN% tests\smoke.py || exit /b 1
exit /b 0

rem The same two toolchain failures run.cmd explains, said shorter because
rem anyone running the tests has already been through run.cmd once. Written to
rem stderr by redirecting the whole subroutine rather than every line in it.
:norust
call :say_norust 1>&2
exit /b 1

:nolinker
call :say_nolinker 1>&2
exit /b 1

:say_norust
echo The tests need a Rust toolchain, and there is not one on this machine.
echo The JavaScript engine is a Rust extension rather than Python, so it has
echo to be compiled before anything can import it. Install rustup-init.exe
echo from https://rustup.rs, then read on -- Windows needs one more thing.
echo.
goto :say_linker

:say_nolinker
echo.
echo The JavaScript engine did not build. The compiler's own output is above.
echo If it mentions link.exe or dlltool.exe, or a missing linker, that is the
echo system side of a Rust install rather than anything about this repository.
echo.

:say_linker
echo Rust compiles the code but does not link it, and Windows ships no linker
echo for it to use. Install Build Tools for Visual Studio, from
echo https://visualstudio.microsoft.com/downloads/, and tick the "Desktop
echo development with C++" workload in its installer. Ticking it is the part
echo that gets missed: the Build Tools without that workload leave you with no
echo link.exe at all, and the error is identical to never having installed
echo them. Open a new command prompt afterwards, then run this script again.
echo.
echo rustup's GNU toolchain (rustup default stable-x86_64-pc-windows-gnu) is a
echo second route, but it trades one install for another rather than avoiding
echo one: it wants MinGW-w64's binutils on PATH, and without them stops at
echo "error calling dlltool 'dlltool.exe': program not found". MSYS2, at
echo https://www.msys2.org, is one way to get them. Visual Studio is the route
echo this project builds against.
goto :eof
