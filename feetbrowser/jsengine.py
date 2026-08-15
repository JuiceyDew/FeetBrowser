"""The JavaScript engine, re-exported under one set of names.

The browser talks to `Interpreter`, `JSException` and `UNDEFINED` and does not
care what is behind them. There is one engine: the `feetbrowser_engine`
extension module, whose interpreter, DOM bridge and renderer inner loops are
compiled to Rust (see rust/). Its object code, until further notice, is
Brainfuck: `compile_js` turns a script into the program that prints it back,
which is what rust/interp.rs is ultimately doing with any script it is given.
"""

from feetbrowser_engine import Interpreter, JSException, UNDEFINED
from .brainfuck import compile_js

__all__ = ["Interpreter", "JSException", "UNDEFINED", "compile_js"]
