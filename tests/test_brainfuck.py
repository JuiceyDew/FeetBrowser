"""The Brainfuck backend: what the JS engine's object code looks like."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.brainfuck import compile, compile_js, interp
from feetbrowser.net import URL


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def test_compile_round_trips():
    for text in ("", "Hello, world!", "alert('hi');\n",
                 "<p>one\ntwo</p>", "var x = {a: 1}; x?.a;",
                 "🦶 FeetBrowser", "ï»¿ utf-8 stays put"):
        eq(interp(compile(text)), text, "round trip of %r" % text)


def test_js_compiles_to_brainfuck():
    js = 'console.log("Hello, world!");'
    bf = compile_js(js)
    assert all(c in "+-<>,.[]\n" for c in bf), "only Brainfuck tokens, please"
    eq(interp(bf), js, "the object code prints the program back")


def test_jsengine_exports_the_compiler():
    from feetbrowser.jsengine import compile_js as engine_compile
    eq(interp(engine_compile("1 + 1;")), "1 + 1;")


def test_compile_uses_whatever_add_is_shorter():
    eq(compile("A"), "+" * 65 + ".", "a low target is pluses")
    # "¿" is U+00BF, whose UTF-8 bytes C2 BF both sit past the halfway mark,
    # so both are reached by subtracting rather than adding.
    bf = compile("¿").replace("\n", "")
    assert all(c in "-." for c in bf), "high bytes use only minuses"
    eq(interp(bf), "¿", "and the minuses still round-trip")


def test_tape_cells_wrap_at_256():
    eq(interp("+" * 65 + "."), "A")
    eq(interp("+" * 255 + "+."), "\x00", "256 wraps around to 0")


def test_loop_matching():
    for bad in ("[", "]", "[][", "[[ ]"):
        try:
            interp(bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for %r" % bad)


def test_classic_hello_world_program():
    hello = ("++++++++[>++++[>++>+++>+++>+<<<<-]>+>+>->>+[<]<-]>>."
             ">---.+++++++..+++.>>.<-.<.+++.------.--------.>>+.>++.")
    eq(interp(hello), "Hello World!\n")


def test_a_loop_is_skipped_when_the_cell_is_zero():
    eq(interp("[...]."), "\x00")


def test_brainfuck_prefix_parses():
    u = URL("bf-source:https://example.com")
    assert u.brainfuck and not u.view_source, "bf-source sets the brainfuck flag"
    eq(u.scheme, "https"); eq(u.host, "example.com")
    assert str(u).startswith("bf-source:https://example.com"), str(u)


def test_brainfuck_prefix_survives_resolve_and_adopt():
    u = URL("bf-source:https://example.com/a")
    eq(str(u.resolve("b.html")), "bf-source:https://example.com/b.html")
    other = URL("bf-source:https://example.com/c")
    u._adopt(other)
    assert u.brainfuck, "adopting a redirect keeps the brainfuck flag"


def test_brainfuck_source_renders_a_program():
    from feetbrowser.browser import Tab
    from feetbrowser.layout import DrawText
    tab = Tab(700)
    tab.load(URL("bf-source:data:text/html,<h1>hi</h1>"))
    texts = [c.text for c in tab.display_list if isinstance(c, DrawText)]
    blob = "".join(texts)
    assert "+" in blob and "." in blob, "the page shows a Brainfuck program"
    eq(interp(blob), "<h1>hi</h1>", "the drawn program prints the page back")


def test_view_source_is_unaffected():
    u = URL("view-source:https://example.com")
    assert u.view_source and not u.brainfuck


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} BRAINFUCK TESTS PASSED")


if __name__ == "__main__":
    main()