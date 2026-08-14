"""A from-scratch JavaScript engine for FeetBrowser.

A hand-written lexer, recursive-descent parser, and tree-walking interpreter
for a practical subset of ECMAScript, built around typed AST nodes.

The value model is shared with the DOM bridge (feetbrowser/jsdom.py):

    number     -> Python int or float
    string     -> Python str
    boolean    -> Python bool
    null       -> Python None
    undefined  -> the module singleton `UNDEFINED`
    array      -> Python list
    object     -> Python dict with str keys, or any "host object"
    function   -> JSFunction, or any Python callable (native)
    promise    -> JSPromise
    void       -> UNDEFINED

Asynchronous code is supported with generator coroutines: `_eval`/`_exec`
are generators that only `yield` at an `await` expression. A synchronous
driver (`_pump_sync`) runs them to completion in one pass (no `await`), while
an async driver (`_resume_async`) suspends the frame on a pending promise and
resumes it with the resolved value once the microtask runs.

Host objects implement the `js_get`/`js_set`/`js_call`/`js_new` protocol so
the DOM bridge and browser-provided natives (fetch, XMLHttpRequest, timers)
can plug straight into the interpreter.
"""

import datetime
import functools
import json
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class JSException(Exception):
    """Raised for any JavaScript-level error: syntax or runtime."""


class _Undefined:
    __slots__ = ()

    def __repr__(self):
        return "undefined"


#: The singleton representing the JS `undefined` value.
UNDEFINED = _Undefined()


class _JSThrow(Exception):
    """A `throw` of an arbitrary JS value; carries the thrown value."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Return(BaseException):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Break(BaseException):
    __slots__ = ()


class _Continue(BaseException):
    __slots__ = ()


class _Suspend:
    """Yielded by `await` to ask the async driver to resume later."""

    __slots__ = ("promise",)

    def __init__(self, promise):
        self.promise = promise


def _nullish(value):
    return value is None or value is UNDEFINED


def _is_numberish(value):
    return isinstance(value, (int, float, str))


def _to_number(value):
    """Coerce a JS value to a number; non-numeric values become NaN."""
    if value is None:
        return 0  # Number(null) === 0
    if value is UNDEFINED:
        return float("nan")
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0
        try:
            return _parse_number(text)
        except ValueError:
            return float("nan")
    if isinstance(value, (int, float)):
        return value
    return float("nan")


def _parse_number(text):
    if text.startswith("."):
        text = "0" + text
    if text.endswith("."):
        text = text[:-1]
    return int(text) if _all_digits(text) else float(text)


def _all_digits(text):
    return all(ch in "0123456789" for ch in text) and text != ""


def _int_index(name):
    try:
        index = int(name)
    except (TypeError, ValueError):
        return None
    if name != str(index):
        return None
    return index


def _is_objectish(value):
    if value is None or value is UNDEFINED:
        return False
    if isinstance(value, (int, float, str, bool)):
        return False
    return True


def _divide(a, b):
    if b == 0:
        if a == 0:
            return float("nan")
        return float("inf") if a > 0 else float("-inf")
    return a / b


def _modulo(a, b):
    if b == 0:
        return float("nan")
    return a % b


def _to_int32(value):
    """Coerce a JS value to a signed 32-bit integer (ToInt32)."""
    n = int(_to_number(value)) & 0xFFFFFFFF
    return n - (1 << 32) if n & (1 << 31) else n


def _map_key(k):
    """A hashable key for Map/Set that treats primitives by value."""
    if k is UNDEFINED:
        return ("u",)
    if k is None:
        return ("n",)
    if isinstance(k, bool):
        return ("b", k)
    if isinstance(k, (int, float)):
        if k != k:
            return ("num", "nan")
        return ("num", k)
    return ("obj", id(k))


def _safe_char(text, i):
    if i < 0 or i >= len(text):
        return ""
    return text[i]


def _safe_code(text, i):
    if i < 0 or i >= len(text):
        return float("nan")
    return ord(text[i])


def _js_pad(text, length, fill, left):
    if fill == "":
        return text
    need = length - len(text)
    if need <= 0:
        return text
    if need > _MAX_STRING_OUT:
        raise JSException("String padding result is too large")
    reps = (need // len(fill)) + 1
    padded = (fill * reps)[:need]
    return padded + text if left else text + padded


def _is_js_function(v):
    return isinstance(v, JSFunction) or callable(v) or hasattr(v, "js_call")


def _loose_eq(a, b):
    na, nb = _nullish(a), _nullish(b)
    if na or nb:
        return na and nb
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if _is_numberish(a) or _is_numberish(b):
        ca, cb = _to_number(a), _to_number(b)
        if ca != ca or cb != cb:
            return False  # NaN never equals anything
        return ca == cb
    if _is_objectish(a) and _is_objectish(b):
        return a is b
    return False


def _strict_eq(a, b):
    ta = _typeof(a)
    if ta != _typeof(b):
        return False
    if ta in ("object", "function"):
        return a is b
    if a != a and b != b:
        return False  # NaN is never equal to anything
    return a == b


def _typeof(value):
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, JSFunction) or callable(value) \
            or hasattr(value, "js_call") or hasattr(value, "js_new"):
        return "function"
    return "object"


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass
class Literal:
    value: object


@dataclass
class Identifier:
    name: str


@dataclass
class This:
    pass


@dataclass
class ArrayLit:
    items: list = field(default_factory=list)


@dataclass
class ObjectLit:
    pairs: list = field(default_factory=list)  # [(key, expr), ...]


@dataclass
class Unary:
    op: str
    operand: object


@dataclass
class Update:
    op: str            # "++" or "--"
    operand: object
    prefix: bool


@dataclass
class Binary:
    op: str
    left: object
    right: object


@dataclass
class Logical:
    op: str            # "&&" or "||"
    left: object
    right: object


@dataclass
class Conditional:
    cond: object
    then_expr: object
    else_expr: object


@dataclass
class Assign:
    op: str            # "=", "+=", "-=", "*=", "/="
    target: object
    value: object


@dataclass
class Call:
    callee: object
    args: list = field(default_factory=list)
    optional: bool = False


@dataclass
class New:
    callee: object
    args: list = field(default_factory=list)


@dataclass
class Member:
    obj: object
    name: str
    optional: bool = False


@dataclass
class Index:
    obj: object
    index: object
    optional: bool = False


@dataclass
class FunctionExpr:
    name: str
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False
    defaults: dict = field(default_factory=dict)
    rest: str = None


@dataclass
class Spread:
    expr: object


@dataclass
class Pattern:
    kind: str            # "array" or "object"
    parts: list = field(default_factory=list)
    rest: object = None  # rest target (str or Pattern) or None
    # For "object" parts: (key, target, default); for "array": (target, default)


@dataclass
class TemplateLiteral:
    quasis: list = field(default_factory=list)
    exprs: list = field(default_factory=list)


@dataclass
class ArrowFunc:
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False
    defaults: dict = field(default_factory=dict)
    rest: str = None
    body_expr: object = None


@dataclass
class ClassExpr:
    name: str = None
    superclass: object = None
    methods: list = field(default_factory=list)


@dataclass
class ClassMethod:
    name: str
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    is_static: bool = False
    accessor: str = None
    defaults: dict = field(default_factory=dict)
    rest: str = None


@dataclass
class Super:
    pass


@dataclass
class Await:
    expr: object


# --- statements ---------------------------------------------------------


@dataclass
class Program:
    statements: list = field(default_factory=list)


@dataclass
class Block:
    statements: list = field(default_factory=list)


@dataclass
class VarDecl:
    kind: str          # "var", "let", "const"
    decls: list = field(default_factory=list)  # [(name, expr|None), ...]


@dataclass
class FunctionDecl:
    name: str
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False
    defaults: dict = field(default_factory=dict)
    rest: str = None


@dataclass
class ClassDecl:
    name: str
    superclass: object = None
    methods: list = field(default_factory=list)


@dataclass
class ExprStmt:
    expr: object


@dataclass
class If:
    cond: object
    then: object
    else_: object = None


@dataclass
class While:
    cond: object
    body: object


@dataclass
class DoWhile:
    body: object
    cond: object


@dataclass
class Switch:
    expr: object
    cases: list = field(default_factory=list)  # [("case"|"default", test, stmts)]


@dataclass
class For:
    init: object
    cond: object
    update: object
    body: object


@dataclass
class ForIn:
    var_kind: str          # "var", "let", "const", or None
    name: str
    iterable: object
    body: object


@dataclass
class ForOf:
    var_kind: str
    name: str
    iterable: object
    body: object


@dataclass
class Return:
    value: object = None


@dataclass
class Break:
    pass


@dataclass
class Continue:
    pass


@dataclass
class Throw:
    expr: object


@dataclass
class TryCatch:
    try_block: object
    catch_param: str = None
    catch_block: object = None
    finally_block: object = None


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------


class Environment:
    """A lexical scope. `var` bindings live on the function scope."""

    __slots__ = ("vars", "lets", "consts", "parent", "function_scope")

    def __init__(self, parent=None):
        self.vars = {}
        self.lets = {}
        self.consts = {}
        self.parent = parent
        self.function_scope = parent.function_scope if parent else self

    def get(self, name):
        env = self
        while env is not None:
            if name in env.lets:
                return env.lets[name]
            if name in env.consts:
                return env.consts[name]
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        return UNDEFINED

    def assign(self, name, value):
        env = self
        while env is not None:
            if name in env.lets:
                env.lets[name] = value
                return
            if name in env.consts:
                raise JSException(f"Assignment to constant variable '{name}'.")
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        self.vars[name] = value

    def set_var(self, name, value):
        self.function_scope.vars[name] = value

    def set_let(self, name, value):
        self.lets[name] = value

    def set_const(self, name, value):
        self.consts[name] = value


# --------------------------------------------------------------------------
# Functions, promises
# --------------------------------------------------------------------------


class JSFunction:
    """A JavaScript closure: a function declaration, expression, arrow, or
    class method."""

    __slots__ = ("params", "defaults", "rest", "body", "body_expr", "env",
                 "interp", "name", "async_", "arrow", "super_info", "_proto")

    def __init__(self, params, body, env, interp, name="", async_=False,
                 defaults=None, rest=None, arrow=False, body_expr=None,
                 super_info=None):
        self.params = params
        self.defaults = defaults or {}
        self.rest = rest
        self.body = body
        self.body_expr = body_expr
        self.env = env
        self.interp = interp
        self.name = name
        self.async_ = async_
        self.arrow = arrow
        self.super_info = super_info  # (parent_proto, parent_ctor) or None
        self._proto = None

    def prototype_obj(self):
        """The `.prototype` object, created lazily."""
        if self._proto is None:
            self._proto = {"constructor": self}
        return self._proto

    def set_prototype(self, value):
        self._proto = value

    def __repr__(self):
        return f"function {self.name}()"


class JSPromise:
    """A real Promise with a microtask-scheduled `then` chain."""

    PENDING, FULFILLED, REJECTED = 0, 1, 2

    def __init__(self, interp):
        self._interp = interp
        self._state = JSPromise.PENDING
        self._value = UNDEFINED
        self._observers = []  # Python callbacks: cb(value, rejected)

    # -- state ------------------------------------------------------------

    @property
    def pending(self):
        return self._state == JSPromise.PENDING

    @property
    def rejected(self):
        return self._state == JSPromise.REJECTED

    @property
    def value(self):
        return self._value

    def resolve(self, value):
        self._settle(True, value)

    def reject(self, reason):
        self._settle(False, reason)

    def _settle(self, ok, value):
        if self._state != JSPromise.PENDING:
            return
        if ok:
            if isinstance(value, JSPromise):
                if value is self:
                    return self._settle(False, "Chaining cycle detected")
                if value.pending:
                    value._observers.append(self._adopt)
                    return
                if value.rejected:
                    return self._settle(False, value.value)
                value = value.value
            else:
                thenable = self._interp._thenable_method(value)
                if thenable is not None:
                    self._assimilate(value, thenable)
                    return
        self._state = JSPromise.FULFILLED if ok else JSPromise.REJECTED
        self._value = value
        observers, self._observers = self._observers, []
        for cb in observers:
            self._interp.enqueue(lambda cb=cb: cb(value, not ok))
        if not ok and not self._observers:
            self._interp._note_unhandled_rejection(value)

    def _adopt(self, value, rejected):
        self._settle(not rejected, value)

    def _assimilate(self, thenable, then):
        def on_ok(v):
            self.resolve(v)

        def on_err(e):
            self.reject(e)

        try:
            self._interp._call_value(then, [on_ok, on_err])
        except Exception:
            self.reject("Error while assimilating thenable")

    def _on_settle(self, cb):
        """Register a Python callback; scheduled as a microtask when settled."""
        if self._state == JSPromise.PENDING:
            self._observers.append(cb)
        else:
            ok = self._state == JSPromise.FULFILLED
            value = self._value
            self._interp.enqueue(lambda: cb(value, not ok))

    # -- JS surface -------------------------------------------------------

    def then(self, on_ok=None, on_err=None):
        child = JSPromise(self._interp)

        def cb(value, rejected):
            handler = on_err if rejected else on_ok
            if handler is None or handler is UNDEFINED:
                if rejected:
                    child.reject(value)
                else:
                    child.resolve(value)
                return
            try:
                result = self._interp._call_value(handler, [value])
            except _JSThrow as t:
                child.reject(t.value)
                return
            except JSException as e:
                child.reject(str(e))
                return
            child.resolve(result)

        self._on_settle(cb)
        return child

    def catch(self, on_err=None):
        return self.then(None, on_err)

    def finally_(self, cb=None):
        child = JSPromise(self._interp)

        def run_settle(value, rejected):
            try:
                result = self._interp._call_value(
                    cb, []) if cb is not None and cb is not UNDEFINED \
                    else UNDEFINED
            except (_JSThrow, JSException) as e:
                child.reject(e.value if isinstance(e, _JSThrow) else str(e))
                return
            if isinstance(result, JSPromise):
                def cont(_v, _r):
                    if _r:
                        child.reject(_v)
                    elif rejected:
                        child.reject(value)
                    else:
                        child.resolve(value)
                result._on_settle(cont)
            else:
                if rejected:
                    child.reject(value)
                else:
                    child.resolve(value)

        self._on_settle(run_settle)
        return child

    def js_get(self, name):
        if name == "then":
            return self.then
        if name == "catch":
            return self.catch
        if name == "finally":
            return self.finally_
        return UNDEFINED


class JSPromiseCtor:
    """The `Promise` global: constructor + statics."""

    def __init__(self, interp):
        self._interp = interp

    def js_get(self, name):
        if name == "resolve":
            return lambda value=UNDEFINED: self._static_resolve(value)
        if name == "reject":
            return lambda reason=UNDEFINED: self._static_reject(reason)
        if name == "all":
            return self._static_all
        if name == "race":
            return self._static_race
        return UNDEFINED

    def js_new(self, executor=UNDEFINED):
        p = JSPromise(self._interp)
        if executor is not UNDEFINED and executor is not None:
            try:
                self._interp._call_value(executor, [p.resolve, p.reject])
            except (_JSThrow, JSException):
                p.reject("Promise executor threw")
        return p

    def js_call(self, *args):
        return self.js_new(args[0] if args else UNDEFINED)

    def _static_resolve(self, value):
        p = JSPromise(self._interp)
        p.resolve(value)
        return p

    def _static_reject(self, reason):
        p = JSPromise(self._interp)
        p.reject(reason)
        return p

    def _static_all(self, iterable):
        interp = self._interp
        p = JSPromise(interp)
        items = list(iterable) if isinstance(iterable, list) else []
        if not items:
            p.resolve([])
            return p
        results = [UNDEFINED] * len(items)
        remaining = [len(items)]

        def attach(index):
            item = interp._as_promise(items[index])

            def cb(value, rejected):
                if rejected:
                    p.reject(value)
                    return
                results[index] = value
                remaining[0] -= 1
                if remaining[0] == 0:
                    p.resolve(results)

            item._on_settle(cb)

        for i in range(len(items)):
            attach(i)
        return p

    def _static_race(self, iterable):
        interp = self._interp
        p = JSPromise(interp)
        items = list(iterable) if isinstance(iterable, list) else []
        for item in items:
            interp._as_promise(item)._on_settle(
                lambda v, r: p.reject(v) if r else p.resolve(v))
        return p


class JSError:
    """Minimal Error host object: `.message`, `.name`."""

    def __init__(self, message=""):
        self.message = str(message if message is not UNDEFINED else "")
        self.name = "Error"

    def js_get(self, name):
        if name == "message":
            return self.message
        if name == "name":
            return self.name
        if name == "stack":
            return f"{self.name}: {self.message}"
        return UNDEFINED

    def js_repr(self):
        return f"{self.name}: {self.message}"


class _ErrorCtor:
    """The `Error` global: a constructor object returning JSError instances."""

    def js_new(self, message=""):
        return JSError(message)

    def js_call(self, *args):
        return JSError(args[0] if args else "")


class JSGlobalObject:
    """The `window`/`globalThis` object: mirrors the interpreter globals."""

    __slots__ = ("interp",)

    def __init__(self, interp):
        self.interp = interp

    def js_get(self, name):
        if name in self.interp.globals:
            return self.interp.globals[name]
        return UNDEFINED

    def js_set(self, name, value):
        self.interp.globals[name] = value

    def js_repr(self):
        return "[object Window]"


class JSRexExp:
    """A compiled regular expression (RegExp host object)."""

    def __init__(self, interp, pattern, flags=""):
        self.interp = interp
        self.source = pattern
        self.flags = flags
        self.global_ = "g" in flags
        self.ignore_case = "i" in flags
        self.last_index = 0
        opts = 0
        if self.ignore_case:
            opts |= re.IGNORECASE
        if "m" in flags:
            opts |= re.MULTILINE
        if "s" in flags:
            opts |= re.DOTALL
        try:
            self._re = re.compile(pattern, opts)
        except re.error:
            self._re = re.compile(r"(?!)")  # never matches

    def _search(self, text, start=0):
        try:
            return self._re.search(text, start)
        except (re.error, TypeError):
            return None

    def test(self, text):
        text = str(text)
        m = self._search(text, self.last_index if self.global_ else 0)
        if m is None:
            if self.global_:
                self.last_index = 0
            return False
        if self.global_:
            self.last_index = m.end()
        return True

    def exec_(self, text):
        text = str(text)
        m = self._search(text, self.last_index if self.global_ else 0)
        if m is None:
            if self.global_:
                self.last_index = 0
            return None
        if self.global_:
            self.last_index = m.end()
        out = [m.group(0)]
        for g in m.groups():
            out.append(UNDEFINED if g is None else g)
        return out

    def js_get(self, name):
        if name == "source":
            return self.source
        if name == "flags":
            return self.flags
        if name == "global":
            return self.global_
        if name == "ignoreCase":
            return self.ignore_case
        if name == "multiline":
            return "m" in self.flags
        if name == "lastIndex":
            return self.last_index
        if name == "test":
            return self.test
        if name == "exec":
            return self.exec_
        return UNDEFINED

    def js_set(self, name, value):
        if name == "lastIndex":
            self.last_index = _to_int32(value) if not _nullish(value) else 0

    def js_repr(self):
        return f"/{self.source}/{self.flags}"


class _RegExpCtor:
    def __init__(self, interp):
        self.interp = interp

    def _make(self, args):
        if not args or args[0] is UNDEFINED:
            return JSRexExp(self.interp, "")
        if isinstance(args[0], JSRexExp) and len(args) == 1:
            return args[0]
        pattern = self.interp.repr(args[0])
        flags = ""
        if len(args) > 1 and args[1] is not UNDEFINED:
            flags = self.interp.repr(args[1])
        return JSRexExp(self.interp, pattern, flags)

    def js_new(self, *args):
        return self._make(list(args))

    def js_call(self, *args):
        return self._make(list(args))


class JSClass:
    """A JavaScript `class`: constructor body + prototype of methods."""

    def __init__(self, interp, name, ctor, prototype, parent=None):
        self.interp = interp
        self.name = name
        self.ctor = ctor        # JSFunction for the constructor (or None)
        self.prototype = prototype  # dict of instance methods (+ __proto__)
        self.parent = parent
        self.statics = {}       # static members live on the class object

    def js_get(self, name):
        if name == "prototype":
            return self.prototype
        if name == "name":
            return self.name
        if name == "length":
            return len(self.ctor.params) if self.ctor else 0
        if name in self.statics:
            return self.statics[name]
        if name in self.prototype:
            return self.prototype[name]
        if self.parent is not None:
            return self.interp.js_get(self.parent, name)
        return UNDEFINED

    def js_set(self, name, value):
        if name in self.statics or name not in self.prototype:
            self.statics[name] = value
        else:
            self.prototype[name] = value

    def js_call(self, *args):
        raise JSException(
            f"Class constructor {self.name} cannot be invoked without 'new'")

    def js_new(self, *args):
        return self._construct(list(args))

    def _construct(self, args):
        obj = JSClassInstance(self.prototype)
        self._construct_on_obj(obj, args)
        return obj

    def _construct_on_obj(self, obj, args):
        if self.ctor is not None:
            self.interp._construct_on(obj, self.ctor, args)
        elif self.parent is not None:
            self.parent._construct_on_obj(obj, args)

    def js_repr(self):
        return f"class {self.name}"


class JSClassInstance:
    """An instance created via `new`; reads walk the prototype chain."""

    __slots__ = ("_props", "_proto")

    def __init__(self, proto):
        self._props = {}
        self._proto = proto

    def js_get(self, name):
        if name in self._props:
            return self._props[name]
        p = self._proto
        while p is not None:
            if isinstance(p, dict) and name in p:
                return p[name]
            p = p.get("__proto__") if isinstance(p, dict) else None
        return UNDEFINED

    def js_set(self, name, value):
        self._props[name] = value

    def js_repr(self):
        return "[object Object]"


class JSSuper:
    """The value of `super` inside a class method."""

    __slots__ = ("interp", "this", "parent_proto", "parent_ctor")

    def __init__(self, interp, this, parent_proto, parent_ctor):
        self.interp = interp
        self.this = this
        self.parent_proto = parent_proto
        self.parent_ctor = parent_ctor

    def js_get(self, name):
        if isinstance(self.parent_proto, dict) and name in self.parent_proto:
            return self.parent_proto[name]
        return UNDEFINED

    def js_call(self, *args):
        if self.parent_ctor is not None:
            self.interp._construct_on(self.this, self.parent_ctor, list(args))
        return self.this

    def js_repr(self):
        return "super"


class _StringCtor:
    """The `String` global: conversion plus static helpers."""

    def __init__(self, interp):
        self.interp = interp

    def js_call(self, *args):
        return self.interp.repr(args[0]) if args else ""

    def js_new(self, *args):
        return self.js_call(*args)

    def js_get(self, name):
        if name == "fromCharCode":
            return lambda *cs: "".join(chr(max(0, _to_int32(c))) for c in cs)
        if name == "fromCodePoint":
            return lambda *cs: "".join(chr(c) for c in cs)
        if name == "raw":
            return lambda parts, *subs: "".join(
                [parts[0]] + [self.interp.repr(s) + parts[i + 1]
                              for i, s in enumerate(subs)])
        return UNDEFINED


class _NumberCtor:
    """The `Number` global: conversion plus static helpers."""

    def __init__(self, interp):
        self.interp = interp

    def js_call(self, *args):
        return _to_number(args[0]) if args else 0.0

    def js_new(self, *args):
        return self.js_call(*args)

    def js_get(self, name):
        if name == "isNaN":
            return lambda v: isinstance(v, (int, float)) and v != v
        if name == "isFinite":
            return lambda v: isinstance(v, (int, float)) \
                and v == v and abs(v) != float("inf")
        if name == "parseInt":
            return self.interp.globals.get("parseInt")
        if name == "parseFloat":
            return self.interp.globals.get("parseFloat")
        if name == "MAX_VALUE":
            return 1.7976931348623157e308
        if name == "MIN_VALUE":
            return 5e-324
        if name == "MAX_SAFE_INTEGER":
            return 2 ** 53 - 1
        if name == "MIN_SAFE_INTEGER":
            return -(2 ** 53 - 1)
        if name == "POSITIVE_INFINITY":
            return float("inf")
        if name == "NEGATIVE_INFINITY":
            return float("-inf")
        return UNDEFINED


class _ArrayCtor:
    """The `Array` global."""

    def __init__(self, interp):
        self.interp = interp

    def js_call(self, *args):
        if len(args) == 1 and isinstance(args[0], (int, float)) \
                and float(args[0]).is_integer() and args[0] >= 0:
            length = int(args[0])
            if length > _MAX_ARRAY_LEN:
                raise JSException(f"Array length {length} exceeds the allowed "
                                  "maximum")
            return [UNDEFINED] * length
        return list(args)

    def js_new(self, *args):
        return self.js_call(*args)

    def js_get(self, name):
        if name == "isArray":
            return lambda v: isinstance(v, list)
        if name == "from":
            def from_(*a):
                src = a[0] if a else UNDEFINED
                if isinstance(src, list):
                    return list(src)
                if isinstance(src, str):
                    return list(src)
                return []
            return from_
        return UNDEFINED


class _ObjectGlobal:
    """The `Object` global."""

    def __init__(self, interp):
        self.interp = interp

    def js_call(self, *args):
        v = args[0] if args else UNDEFINED
        if v is None or v is UNDEFINED:
            return {}
        if isinstance(v, dict):
            return v
        return {}

    def js_new(self, *args):
        return self.js_call(*args)

    def js_get(self, name):
        if name == "keys":
            return lambda o: list(o.keys()) if isinstance(o, dict) else []
        if name == "values":
            return lambda o: list(o.values()) if isinstance(o, dict) else []
        if name == "entries":
            return lambda o: [[k, v] for k, v in o.items()] \
                if isinstance(o, dict) else []
        if name == "assign":
            def assign_(*objs):
                out = {}
                for o in objs:
                    if isinstance(o, dict):
                        out.update(o)
                return out
            return assign_
        if name == "create":
            return lambda proto: JSClassInstance(
                proto) if _is_objectish(proto) else JSClassInstance(None)
        if name == "getPrototypeOf":
            def gpo(o):
                if isinstance(o, JSClassInstance):
                    return o._proto
                return UNDEFINED
            return gpo
        if name == "setPrototypeOf":
            def spo(o, proto):
                if isinstance(o, JSClassInstance):
                    o._proto = proto
                return o
            return spo
        if name == "defineProperty":
            def define_property(obj, key, desc):
                if isinstance(obj, dict):
                    if isinstance(desc, dict) and "value" in desc:
                        obj[str(key)] = desc["value"]
                    elif isinstance(desc, dict) and "get" in desc \
                            and callable(desc.get("get")):
                        obj[str(key)] = desc["get"]
                return obj
            return define_property
        if name == "freeze":
            return lambda o: o
        if name == "hasOwnProperty":
            return lambda o, k: isinstance(o, dict) and str(k) in o
        if name == "prototype":
            proto = {
                "hasOwnProperty":
                    lambda obj, key: isinstance(obj, dict) and key in obj,
                "toString": lambda obj: self.interp.repr(obj),
                "valueOf": lambda obj: obj,
            }
            return proto
        return UNDEFINED


class JSMap:
    def __init__(self, interp):
        self.interp = interp
        self._store = {}

    def js_get(self, name):
        if name == "set":
            def set_(k, v):
                self._store[_map_key(k)] = v
                return self
            return set_
        if name == "get":
            return lambda k: self._store.get(_map_key(k), UNDEFINED)
        if name == "has":
            return lambda k: _map_key(k) in self._store
        if name == "delete":
            def del_(k):
                kk = _map_key(k)
                if kk in self._store:
                    del self._store[kk]
                    return True
                return False
            return del_
        if name == "clear":
            return lambda: self._store.clear()
        if name == "size":
            return len(self._store)
        if name == "forEach":
            def for_each(fn):
                for k, v in list(self._store.items()):
                    self.interp._call_value(fn, [v, k, self])
            return for_each
        return UNDEFINED

    def js_repr(self):
        return "[object Map]"


class JSSet:
    def __init__(self, interp):
        self.interp = interp
        self._store = {}

    def js_get(self, name):
        if name == "add":
            def add(v):
                self._store[_map_key(v)] = v
                return self
            return add
        if name == "has":
            return lambda v: _map_key(v) in self._store
        if name == "delete":
            def del_(v):
                kk = _map_key(v)
                if kk in self._store:
                    del self._store[kk]
                    return True
                return False
            return del_
        if name == "clear":
            return lambda: self._store.clear()
        if name == "size":
            return len(self._store)
        if name == "forEach":
            def for_each(fn):
                for v in list(self._store.values()):
                    self.interp._call_value(fn, [v, v, self])
            return for_each
        return UNDEFINED

    def js_repr(self):
        return "[object Set]"


class _MapCtor:
    def __init__(self, interp):
        self.interp = interp

    def js_new(self, *args):
        m = JSMap(self.interp)
        if args and isinstance(args[0], (list, dict)):
            if isinstance(args[0], dict):
                pairs = args[0].items()
            else:
                pairs = (p if isinstance(p, (list, tuple)) and len(p) == 2
                         else (p, UNDEFINED) for p in args[0])
            for k, v in pairs:
                m._store[_map_key(k)] = v
        return m

    def js_call(self, *args):
        return JSMap(self.interp)


class _SetCtor:
    def __init__(self, interp):
        self.interp = interp

    def js_new(self, *args):
        s = JSSet(self.interp)
        if args and isinstance(args[0], list):
            for v in args[0]:
                s._store[_map_key(v)] = v
        return s

    def js_call(self, *args):
        return JSSet(self.interp)


class JSDate:
    def __init__(self, ms):
        self._ms = float(ms)
        self._dt = None
        self._utc = None
        try:
            self._dt = datetime.datetime.fromtimestamp(self._ms / 1000.0)
        except (OverflowError, OSError, ValueError):
            self._dt = None
        try:
            self._utc = datetime.datetime.fromtimestamp(
                self._ms / 1000.0, datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            self._utc = None

    def _getters(self):
        d, u = self._dt, self._utc
        return {
            "getTime": lambda: self._ms,
            "valueOf": lambda: self._ms,
            "getFullYear": lambda: d.year,
            "getMonth": lambda: d.month - 1,
            "getDate": lambda: d.day,
            "getDay": lambda: d.weekday(),
            "getHours": lambda: d.hour,
            "getMinutes": lambda: d.minute,
            "getSeconds": lambda: d.second,
            "getMilliseconds": lambda: int(self._ms) % 1000,
            "getUTCFullYear": lambda: u.year,
            "getUTCMonth": lambda: u.month - 1,
            "getUTCDate": lambda: u.day,
            "getUTCDay": lambda: u.weekday(),
            "getUTCHours": lambda: u.hour,
            "getUTCMinutes": lambda: u.minute,
            "getUTCSeconds": lambda: u.second,
            "getUTCMilliseconds": lambda: int(self._ms) % 1000,
            "toISOString": lambda: u.strftime("%Y-%m-%dT%H:%M:%S.") +
                "%03dZ" % (int(self._ms) % 1000),
            "toUTCString": lambda: u.strftime("%a, %d %b %Y %H:%M:%S") + " GMT",
            "toLocaleString": lambda: d.strftime("%a %b %d %Y %H:%M:%S"),
            "toString": lambda: d.strftime("%a %b %d %Y %H:%M:%S") +
                " GMT+0000" if d else "Invalid Date",
            "toDateString": lambda: d.strftime("%a %b %d %Y"),
            "toTimeString": lambda: d.strftime("%H:%M:%S") + " GMT+0000",
        }

    def js_get(self, name):
        if self._dt is None:
            if name in ("getTime", "valueOf"):
                return lambda: self._ms
            return lambda: float("nan")
        g = self._getters()
        if name in g:
            return g[name]
        return UNDEFINED

    def js_repr(self):
        if self._dt is None:
            return "Invalid Date"
        return self._dt.strftime("%a %b %d %Y %H:%M:%S") + " GMT+0000"


class _DateCtor:
    def __init__(self, interp):
        self.interp = interp

    def js_call(self, *args):
        return JSDate(self._make_ms(list(args)))

    def js_new(self, *args):
        return JSDate(self._make_ms(list(args)))

    def js_get(self, name):
        if name == "now":
            return lambda: time.time() * 1000.0
        if name == "parse":
            return lambda s: self._parse_ms(self.interp.repr(s))
        if name == "UTC":
            return lambda *args: self._make_ms(list(args), utc=True)
        return UNDEFINED

    def _make_ms(self, args, utc=False):
        if not args:
            return self.interp._now * 1000.0
        if len(args) == 1:
            v = args[0]
            if isinstance(v, (int, float)):
                return float(v)
            return self._parse_ms(self.interp.repr(v))
        nums = [_to_number(a) for a in args]
        y, mo = int(nums[0]), int(nums[1])
        d = int(nums[2]) if len(nums) > 2 else 1
        h = int(nums[3]) if len(nums) > 3 else 0
        mi = int(nums[4]) if len(nums) > 4 else 0
        s = int(nums[5]) if len(nums) > 5 else 0
        ms = int(nums[6]) if len(nums) > 6 else 0
        if 0 <= y <= 99:
            y += 1900
        try:
            if utc:
                dt = datetime.datetime(y, mo + 1, d, h, mi, s, ms * 1000,
                                       datetime.timezone.utc)
            else:
                dt = datetime.datetime(y, mo + 1, d, h, mi, s, ms * 1000)
            return dt.timestamp() * 1000.0
        except ValueError:
            return float("nan")

    def _parse_ms(self, text):
        fmts = [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%a %b %d %Y %H:%M:%S",
        ]
        for f in fmts:
            try:
                dt = datetime.datetime.strptime(text, f)
                return dt.timestamp() * 1000.0
            except ValueError:
                pass
        return float("nan")


class _LocalStorage:
    def __init__(self, interp):
        self.interp = interp
        self._data = {}

    def js_get(self, name):
        if name == "getItem":
            return lambda k: self._data.get(str(k), None)
        if name == "setItem":
            def set_item(k, v):
                self._data[str(k)] = self.interp.repr(v)
            return set_item
        if name == "removeItem":
            def remove_item(k):
                self._data.pop(str(k), None)
            return remove_item
        if name == "clear":
            return lambda: self._data.clear()
        if name == "key":
            def key(i):
                keys = list(self._data)
                idx = _to_int32(i)
                return keys[idx] if 0 <= idx < len(keys) else None
            return key
        if name == "length":
            return len(self._data)
        return UNDEFINED

    def js_set(self, name, value):
        self._data[str(name)] = self.interp.repr(value)


def _js_round(x):
    """JS Math.round semantics: half away from zero."""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def _js_math():
    m = math
    return {
        "PI": m.pi, "E": m.e, "LN2": m.log(2), "LN10": m.log(10),
        "LOG2E": m.log2(m.e), "LOG10E": m.log10(m.e),
        "SQRT2": m.sqrt(2), "SQRT1_2": m.sqrt(0.5),
        "abs": lambda x: abs(_to_number(x)),
        "ceil": lambda x: m.ceil(_to_number(x)),
        "floor": lambda x: m.floor(_to_number(x)),
        "round": lambda x: _js_round(_to_number(x)),
        "trunc": lambda x: int(_to_number(x)),
        "sign": lambda x: _js_sign(_to_number(x)),
        "sqrt": lambda x: m.sqrt(_to_number(x)),
        "cbrt": lambda x: _to_number(x) ** (1.0 / 3.0)
        if _to_number(x) >= 0 else -((-_to_number(x)) ** (1.0 / 3.0)),
        "exp": lambda x: m.exp(_to_number(x)),
        "log": lambda x: m.log(_to_number(x)),
        "log2": lambda x: m.log2(_to_number(x)),
        "log10": lambda x: m.log10(_to_number(x)),
        "pow": lambda a, b: _to_number(a) ** _to_number(b),
        "sin": lambda x: m.sin(_to_number(x)),
        "cos": lambda x: m.cos(_to_number(x)),
        "tan": lambda x: m.tan(_to_number(x)),
        "asin": lambda x: m.asin(_to_number(x)),
        "acos": lambda x: m.acos(_to_number(x)),
        "atan": lambda x: m.atan(_to_number(x)),
        "atan2": lambda y, x: m.atan2(_to_number(y), _to_number(x)),
        "sinh": lambda x: m.sinh(_to_number(x)),
        "cosh": lambda x: m.cosh(_to_number(x)),
        "tanh": lambda x: m.tanh(_to_number(x)),
        "hypot": lambda *xs: m.hypot(*[_to_number(x) for x in xs]),
        "max": lambda *xs: max([_to_number(x) for x in xs])
        if xs else float("-inf"),
        "min": lambda *xs: min([_to_number(x) for x in xs])
        if xs else float("inf"),
        "random": random.random,
        "fround": lambda x: float(_to_number(x)),
    }


def _js_sign(x):
    if x != x or x == 0:
        return x
    return -1.0 if x < 0 else 1.0


def _js_json_parse(text):
    text = str(text)
    if text.strip() == "":
        raise JSException("Unexpected end of JSON input")
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise JSException(f"JSON.parse: {exc}") from None


def _js_json_escape(s):
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _js_json_stringify(value):
    def stringify(v, seen):
        if v is None:
            return "null"
        if v is UNDEFINED:
            return None
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (int, float)):
            if v != v or v in (float("inf"), float("-inf")):
                return "null"
            return str(int(v)) if float(v).is_integer() else str(v)
        if isinstance(v, str):
            return '"' + _js_json_escape(v) + '"'
        if isinstance(v, (list, tuple)):
            if id(v) in seen:
                return None
            seen.add(id(v))
            parts = []
            for item in v:
                p = stringify(item, seen)
                parts.append("null" if p is None else p)
            seen.remove(id(v))
            return "[" + ",".join(parts) + "]"
        if isinstance(v, dict):
            if id(v) in seen:
                return None
            seen.add(id(v))
            parts = []
            for k, val in v.items():
                p = stringify(val, seen)
                if p is None:
                    continue
                parts.append('"%s":%s' % (_js_json_escape(str(k)), p))
            seen.remove(id(v))
            return "{" + ",".join(parts) + "}"
        if isinstance(v, JSClassInstance):
            if id(v) in seen:
                return None
            seen.add(id(v))
            parts = []
            for k, val in v._props.items():
                p = stringify(val, seen)
                if p is None:
                    continue
                parts.append('"%s":%s' % (_js_json_escape(str(k)), p))
            seen.remove(id(v))
            return "{" + ",".join(parts) + "}"
        if isinstance(v, JSError):
            return None
        return None

    result = stringify(value, set())
    return UNDEFINED if result is None else result


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_KEYWORDS = {
    "var", "let", "const", "function", "return", "if", "else", "while",
    "for", "break", "continue", "true", "false", "null", "undefined",
    "typeof", "throw", "try", "catch", "finally", "new", "this", "await",
    "class", "extends", "super", "static", "in", "instanceof", "delete",
    "void", "of", "switch", "case", "default", "do",
}

# Longest match first, so the tokenizer greedily groups '===', '!=', etc.
_PUNCT = (
    (4, ">>>="),
    (3, "..."), (3, "==="), (3, "!=="), (3, "**="), (3, "&&="),
    (3, "||="), (3, "??="), (3, ">>>"),
    (2, "=="), (2, "!="), (2, "<="), (2, ">="), (2, "&&"), (2, "||"),
    (2, "+="), (2, "-="), (2, "*="), (2, "/="), (2, "%="), (2, "++"),
    (2, "--"), (2, "**"), (2, ">>="), (2, "<<="), (2, "&="), (2, "|="),
    (2, "^="), (2, "??"), (2, "=>"), (2, ">>"), (2, "<<"),
    (1, "{"), (1, "}"), (1, "("), (1, ")"), (1, "["), (1, "]"),
    (1, ";"), (1, ","), (1, "."), (1, ":"), (1, "?"), (1, "="), (1, "!"),
    (1, "+"), (1, "-"), (1, "*"), (1, "/"), (1, "%"), (1, "<"), (1, ">"),
    (1, "&"), (1, "|"), (1, "^"), (1, "~"), (1, "`"),
)

#: Simple backslash escapes in string literals; "\n" is a line continuation.
_SIMPLE_ESC = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"',
               "\n": ""}

#: Defensive cap so pathological inputs cannot exhaust memory in the lexer.
_MAX_TOKENS = 200_000

#: Max single allocation for JS array/string operations, so `Array(1e9)` or
#: `"x".repeat(1e9)` can't allocate gigabytes in one call.
_MAX_ARRAY_LEN = 1_000_000
_MAX_STRING_OUT = 32_000_000

#: Bounds on the timer/microtask machinery to stop timer- and microtask-
#: storms from growing unboundedly or pinning the UI thread forever.
_MAX_TIMERS = 10_000
_MAX_DRAIN = 1_000_000


def _regex_allowed(prev):
    """A `/` starts a regex literal unless the previous token ends a value."""
    if prev is None:
        return True
    kind, value = prev[0], prev[1]
    if kind in ("ident", "number", "string", "template", "regex"):
        return False
    if kind == "kw":
        return value not in ("true", "false", "null", "undefined", "this",
                             "super")
    if kind == "punct":
        return value not in (")", "]", "}", "++", "--")
    return True


def _find_template_end(s, start):
    """Index just past the closing backtick of a template at s[start] == '`',
    or -1 if the template is unterminated."""
    i, n = start + 1, len(s)
    depth = 0
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            if depth == 0:
                return i + 1
            inner = _find_template_end(s, i)
            if inner == -1:
                return -1
            i = inner
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if c == "}" and depth > 0:
            depth -= 1
        i += 1
    return -1


def _split_template(raw):
    """Split template raw source into [(quasi, expr_source|None), ...]."""
    parts = []
    buf = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 < n:
                buf.append(ch)
                buf.append(raw[i + 1])
                i += 2
                continue
        if ch == "$" and i + 1 < n and raw[i + 1] == "{":
            j = i + 2
            d = 1
            q = None
            while j < n:
                c = raw[j]
                if q is not None:
                    if c == "\\":
                        j += 2
                        continue
                    if c == q:
                        q = None
                elif c in "'\"`":
                    q = c
                elif c == "{":
                    d += 1
                elif c == "}":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            if j >= n:
                buf.append(raw[i:])
                break
            parts.append(("".join(buf), raw[i + 2:j]))
            buf = []
            i = j + 1
        else:
            buf.append(ch)
            i += 1
    parts.append(("".join(buf), None))
    return parts


class _Tokenizer:
    """Tokenize a source string into (kind, value, offset) triples."""

    def __init__(self, source):
        self.source = source

    def tokenize(self):
        tokens = []
        s, i, n = self.source, 0, len(self.source)
        while i < n:
            ch = s[i]
            prev = tokens[-1] if tokens else None
            if ch in " \t\r\n":
                i += 1
            elif s.startswith("//", i):
                nl = s.find("\n", i)
                i = n if nl == -1 else nl + 1
            elif s.startswith("/*", i):
                end = s.find("*/", i + 2)
                if end == -1:
                    self._fail(i, "unterminated block comment")
                i = end + 2
            elif ch in "0123456789" or (
                    ch == "." and i + 1 < n and s[i + 1] in "0123456789"):
                j = i
                if ch == "0" and i + 1 < n and s[i + 1] in "xX":
                    j = i + 2
                    while j < n and s[j] in "0123456789abcdefABCDEF":
                        j += 1
                    tokens.append(("number",
                                   int(s[i + 2:j], 16) if j > i + 2 else 0, i))
                    i = j
                    if len(tokens) > _MAX_TOKENS:
                        raise JSException("Too many tokens")
                    continue
                if ch == "0" and i + 1 < n and s[i + 1] in "bB":
                    j = i + 2
                    while j < n and s[j] in "01":
                        j += 1
                    tokens.append(("number",
                                   int(s[i + 2:j], 2) if j > i + 2 else 0, i))
                    i = j
                    if len(tokens) > _MAX_TOKENS:
                        raise JSException("Too many tokens")
                    continue
                while j < n and s[j] in "0123456789":
                    j += 1
                if j < n and s[j] == ".":
                    j += 1
                    while j < n and s[j] in "0123456789":
                        j += 1
                if j < n and s[j] in "eE":
                    k = j + 1
                    if k < n and s[k] in "+-":
                        k += 1
                    if k < n and s[k] in "0123456789":
                        while k < n and s[k] in "0123456789":
                            k += 1
                        j = k
                tokens.append(("number", _parse_number(s[i:j]), i))
                i = j
            elif ch in ('"', "'"):
                quote = ch
                i += 1
                buf = []
                while True:
                    if i >= n:
                        self._fail(i, "unterminated string literal")
                    c = s[i]
                    if c == "\\":
                        i += 1
                        if i >= n:
                            self._fail(i, "unterminated string literal")
                        esc = s[i]
                        i += 1
                        if esc in _SIMPLE_ESC:
                            buf.append(_SIMPLE_ESC[esc])
                        elif esc in "xu":
                            size = 4 if esc == "u" else 2
                            try:
                                buf.append(chr(int(s[i:i + size], 16)))
                                i += size
                            except ValueError:
                                pass
                        else:
                            buf.append(esc)
                    elif c == quote:
                        i += 1
                        tokens.append(("string", "".join(buf), i))
                        break
                    elif c == "\n":
                        self._fail(i, "unterminated string literal")
                    else:
                        buf.append(c)
                        i += 1
            elif ch == "`":
                j = _find_template_end(s, i)
                if j == -1:
                    self._fail(i, "unterminated template literal")
                tokens.append(("template", s[i + 1:j - 1], i))
                i = j
            elif ch.isalpha() or ch in "_$":
                j = i
                while j < n and (s[j].isalnum() or s[j] in "_$"):
                    j += 1
                word = s[i:j]
                kind = "kw" if word in _KEYWORDS else "ident"
                tokens.append((kind, word, i))
                i = j
            elif ch == "/" and _regex_allowed(prev):
                j = i + 1
                buf = []
                in_class = False
                while j < n:
                    c = s[j]
                    if c == "\\":
                        buf.append(c)
                        j += 1
                        if j < n:
                            buf.append(s[j])
                            j += 1
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        j += 1
                        break
                    elif c == "\n":
                        self._fail(i, "unterminated regular expression")
                    buf.append(c)
                    j += 1
                else:
                    self._fail(i, "unterminated regular expression")
                flags = ""
                while j < n and s[j].isalpha():
                    flags += s[j]
                    j += 1
                tokens.append(("regex", ("".join(buf), flags), i))
                i = j
            elif ch in "{}()[];,.;:?!<>=+-*/%&|^~@#`":
                if ch == "?" and i + 1 < n and s[i + 1] == "." and \
                        (i + 2 >= n or s[i + 2] not in "0123456789"):
                    tokens.append(("punct", "?.", i))
                    i += 2
                    if len(tokens) > _MAX_TOKENS:
                        raise JSException("Too many tokens")
                    continue
                matched = False
                for length, text in _PUNCT:
                    if length <= n - i and s[i:i + length] == text:
                        tokens.append(("punct", text, i))
                        i += length
                        matched = True
                        break
                if not matched:
                    tokens.append(("punct", ch, i))
                    i += 1
            else:
                self._fail(i, f"unexpected character {ch!r}")
            if len(tokens) > _MAX_TOKENS:
                raise JSException("Too many tokens")
        return tokens

    def _fail(self, offset, msg):
        line = self.source.count("\n", 0, offset) + 1
        raise JSException(f"SyntaxError on line {line}: {msg}")


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class _Parser:
    _STMT = None  # set below the class

    def __init__(self, source):
        self.source = source
        self.tokens = _Tokenizer(source).tokenize()
        self.pos = 0
        self.async_depth = 0

    # -- token helpers ------------------------------------------------------

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return (None, None, len(self.source))

    def _peek2(self):
        if self.pos + 1 < len(self.tokens):
            return self.tokens[self.pos + 1]
        return (None, None, len(self.source))

    def _peek_n(self, n):
        if self.pos + n < len(self.tokens):
            return self.tokens[self.pos + n]
        return (None, None, len(self.source))

    def _peek2_is(self, text):
        kind, value, _ = self._peek2()
        return kind == "punct" and value == text

    def _peek3_is_arrow(self):
        kind, value, _ = self._peek_n(3)
        return kind == "punct" and value == "=>"

    def _peek_is_punct(self, text):
        kind, value, _ = self._peek()
        return kind == "punct" and value == text

    def _match_punct(self, text):
        kind, value, _ = self._peek()
        if kind == "punct" and value == text:
            self.pos += 1
            return text
        return None

    def _match_kw(self, text):
        kind, value, _ = self._peek()
        if kind == "kw" and value == text:
            self.pos += 1
            return text
        return None

    def _expect_punct(self, text):
        if self._match_punct(text) is None:
            self._syntax(f"expected '{text}'")

    def _match_ident(self):
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        return None

    def _expect_ident(self):
        name = self._match_ident()
        if name is None:
            self._syntax("expected identifier")
        return name

    def _expect_property_name(self):
        """Property names may be keywords too (promise.catch, obj.default)."""
        kind, value, _ = self._peek()
        if kind in ("ident", "kw"):
            self.pos += 1
            return value
        self._syntax("expected property name")

    def _next_is_kw(self, text):
        kind, value, _ = self._peek2()
        return kind == "kw" and value == text

    def _syntax(self, msg):
        self._fail(self._peek()[2], msg)

    def _fail(self, offset, msg):
        line = self.source.count("\n", 0, offset) + 1
        raise JSException(f"SyntaxError on line {line}: {msg}")

    # -- grammar ------------------------------------------------------------

    def parse_program(self):
        return Program(self._parse_stmts_until(None))

    def parse_expression(self):
        """Parse a standalone expression (used for template substitutions)."""
        return self._expression()

    def _statement(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value == "{":
            return Block(self._parse_stmts_until("}"))
        if kind == "kw" and value in self._STMT:
            self.pos += 1
            return self._STMT[value](self)
        if kind == "ident" and value == "async" \
                and self._next_is_kw("function"):
            self.pos += 1
            self.pos += 1  # consume 'function'
            name = self._expect_ident()
            params, defaults, rest, body = self._function_rest(True)
            return FunctionDecl(name, params, body, True, defaults, rest)
        return ExprStmt(self._expression())

    def _parse_stmts_until(self, closing):
        if closing is not None:
            self._expect_punct("{")
        stmts = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                if closing is not None:
                    self._syntax(f"expected '{closing}'")
                break
            if kind == "punct" and value == closing:
                self.pos += 1
                break
            if self._match_punct(";"):
                continue
            stmts.append(self._statement())
            self._match_punct(";")
        return stmts

    def _block(self):
        return Block(self._parse_stmts_until("}"))

    def _declaration_list(self):
        decls = []
        while True:
            target = self._declaration_target()
            value = None
            if self._match_punct("="):
                value = self._expression()
            decls.append((target, value))
            if self._match_punct(",") is None:
                break
        return decls

    def _declaration_target(self):
        """A var/let/const target: a plain name or a destructuring pattern."""
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        if kind == "punct" and value in ("[", "{"):
            return self._pattern()
        self._syntax("expected identifier")

    def _pattern(self):
        """Parse a destructuring pattern: [a, ...rest] or { k: a, ... }."""
        kind, value, _ = self._peek()
        if kind == "punct" and value == "[":
            self.pos += 1
            parts = []
            rest = None
            while True:
                if self._match_punct("]"):
                    break
                if self._match_punct(","):
                    continue
                if self._match_punct("..."):
                    rest = self._pattern_target()
                    self._match_punct(",")
                    self._expect_punct("]")
                    break
                target = self._pattern_target()
                default = None
                if self._match_punct("="):
                    default = self._assign()
                parts.append((target, default))
                if self._match_punct(",") is None:
                    self._expect_punct("]")
                    break
            return Pattern("array", parts, rest)
        self._expect_punct("{")
        parts = []
        rest = None
        while True:
            if self._match_punct("}"):
                break
            if self._match_punct(","):
                continue
            if self._match_punct("..."):
                rest = self._pattern_target()
                self._match_punct(",")
                self._expect_punct("}")
                break
            kt, kv, _ = self._peek()
            if kt in ("ident", "string", "kw"):
                self.pos += 1
                key = kv
            else:
                self._syntax("expected property name")
            target = None
            default = None
            if self._match_punct(":"):
                target = self._pattern_target()
            else:
                if kt != "ident":
                    self._syntax("expected ':' in destructuring")
                target = key
            if self._match_punct("="):
                default = self._assign()
            parts.append((key, target, default))
            if self._match_punct(",") is None:
                self._expect_punct("}")
                break
        return Pattern("object", parts, rest)

    def _pattern_target(self):
        """A single target inside a pattern: name or nested pattern."""
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        if kind == "punct" and value in ("[", "{"):
            return self._pattern()
        self._syntax("expected identifier in destructuring")

    def _function_declaration(self, async_):
        name = self._expect_ident()
        params, defaults, rest, body = self._function_rest(async_)
        return FunctionDecl(name, params, body, async_, defaults, rest)

    def _function_rest(self, async_):
        params, defaults, rest = self._param_list()
        if async_:
            self.async_depth += 1
        try:
            body = self._parse_stmts_until("}")
        finally:
            if async_:
                self.async_depth -= 1
        return params, defaults, rest, body

    def _param_list(self):
        """Parse `(a, b = 1, ...rest)` -> (names, defaults, rest)."""
        names, defaults, rest = [], {}, None

        def item():
            nonlocal rest
            is_rest = self._match_punct("...")
            name = self._expect_ident()
            if is_rest:
                rest = name
                return
            names.append(name)
            if self._match_punct("="):
                defaults[name] = self._assign()

        self._list("(", ")", item)
        return names, defaults, rest

    def _arrow_rest(self, params, defaults, rest, async_):
        self._expect_punct("=>")
        kind, value, _ = self._peek()
        if kind == "punct" and value == "{":
            body = self._parse_stmts_until("}")
            return ArrowFunc(params, body, async_, defaults, rest, None)
        if async_:
            self.async_depth += 1
        try:
            expr = self._assign()
        finally:
            if async_:
                self.async_depth -= 1
        return ArrowFunc(params, None, async_, defaults, rest, expr)

    def _paren_followed_by_arrow(self):
        """True when the `(` at pos is `(params) => ...`."""
        depth = 0
        i = self.pos
        n = len(self.tokens)
        while i < n:
            kind, value, _ = self.tokens[i]
            if kind == "punct":
                if value in ("(", "[", "{"):
                    depth += 1
                elif value in (")", "]", "}"):
                    depth -= 1
                    if depth == 0 and value == ")":
                        nxt = self.tokens[i + 1] \
                            if i + 1 < n else (None, None, None)
                        return nxt[0] == "punct" and nxt[1] == "=>"
            i += 1
        return False

    def _list(self, opener, closer, item, trailing=False):
        """Parse a comma-separated list; `trailing` allows a trailing comma
        (arrays/objects) instead of rejecting it (args/params)."""
        if opener is not None:
            self._expect_punct(opener)
        out = []
        if trailing or self._match_punct(closer) is None:
            while True:
                if trailing and self._match_punct(closer):
                    break
                out.append(item())
                if self._match_punct(closer):
                    break
                self._expect_punct(",")
        return out

    def _return_statement(self):
        kind, value, _ = self._peek()
        if kind is not None and not (kind == "punct" and value in (";", "}")):
            return Return(self._expression())
        return Return(None)

    def _if_statement(self):
        cond, then = self._cond_body()
        else_stmt = self._statement() if self._match_kw("else") else None
        return If(cond, then, else_stmt)

    def _while_statement(self):
        cond, body = self._cond_body()
        return While(cond, body)

    def _do_while_statement(self):
        body = self._statement()
        self._match_punct(";")  # ASI: `do stmt; while(...)`
        self._match_kw("while")
        self._expect_punct("(")
        cond = self._expression()
        self._expect_punct(")")
        self._match_punct(";")
        return DoWhile(body, cond)

    def _switch_statement(self):
        self._expect_punct("(")
        expr = self._expression()
        self._expect_punct(")")
        self._expect_punct("{")
        cases = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                self._syntax("expected '}'")
            if self._match_punct("}"):
                break
            if self._match_kw("case"):
                test = self._expression()
                self._expect_punct(":")
                cases.append(("case", test, self._case_body()))
            elif self._match_kw("default"):
                self._expect_punct(":")
                cases.append(("default", None, self._case_body()))
            else:
                self._syntax("expected 'case' or 'default'")
        return Switch(expr, cases)

    def _case_body(self):
        stmts = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                self._syntax("expected '}'")
            if kind == "punct" and value == "}":
                break
            if kind == "kw" and value in ("case", "default"):
                break
            if self._match_punct(";"):
                continue
            stmts.append(self._statement())
        return stmts

    def _cond_body(self):
        """Parse `(expression)` then a statement body."""
        self._expect_punct("(")
        cond = self._expression()
        self._expect_punct(")")
        return cond, self._statement()

    def _for_statement(self):
        self._expect_punct("(")
        kind, value, _ = self._peek()
        # `for (x in obj)` and `for (x of arr)` / with a var kind.
        if kind == "kw" and value in ("var", "let", "const"):
            save = self.pos
            self.pos += 1
            name = self._match_ident()
            if name is not None:
                k2, v2, _ = self._peek()
                if k2 == "kw" and v2 in ("in", "of"):
                    self.pos += 1
                    iterable = self._expression()
                    self._expect_punct(")")
                    body = self._statement()
                    if v2 == "in":
                        return ForIn(value, name, iterable, body)
                    return ForOf(value, name, iterable, body)
            self.pos = save
        else:
            save = self.pos
            name = self._match_ident()
            if name is not None:
                k2, v2, _ = self._peek()
                if k2 == "kw" and v2 in ("in", "of"):
                    self.pos += 1
                    iterable = self._expression()
                    self._expect_punct(")")
                    body = self._statement()
                    if v2 == "in":
                        return ForIn(None, name, iterable, body)
                    return ForOf(None, name, iterable, body)
            self.pos = save
        init = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ";"):
            if kind == "kw" and value in ("var", "let", "const"):
                self.pos += 1
                init = VarDecl(value, self._declaration_list())
            else:
                init = ExprStmt(self._expression())
        self._expect_punct(";")
        cond = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ";"):
            cond = self._expression()
        self._expect_punct(";")
        update = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ")"):
            update = self._expression()
        self._expect_punct(")")
        body = self._statement()
        return For(init, cond, update, body)

    def _throw_statement(self):
        return Throw(self._expression())

    def _try_statement(self):
        try_block = self._block()
        catch_param = None
        catch_block = None
        if self._match_kw("catch"):
            self._expect_punct("(")
            catch_param = self._expect_ident()
            self._expect_punct(")")
            catch_block = self._block()
        finally_block = self._block() if self._match_kw("finally") else None
        return TryCatch(try_block, catch_param, catch_block, finally_block)

    # -- expressions --------------------------------------------------------

    def _expression(self):
        return self._assign()

    def _assign(self):
        left = self._conditional()
        kind, value, _ = self._peek()
        if kind == "punct" and value in (
                "=", "+=", "-=", "*=", "/=", "%=", "**=", "&=", "|=",
                "^=", "<<=", ">>=", ">>>=", "&&=", "||=", "??="):
            self.pos += 1
            right = self._assign()
            if not isinstance(left, (Identifier, Member, Index)):
                self._syntax("invalid assignment target")
            return Assign(value, left, right)
        return left

    def _conditional(self):
        cond = self._or()
        if self._match_punct("?"):
            then_expr = self._assign()
            self._expect_punct(":")
            else_expr = self._assign()
            return Conditional(cond, then_expr, else_expr)
        return cond

    def _or(self):
        node = self._and()
        while True:
            if self._match_punct("||"):
                node = Logical("||", node, self._and())
            elif self._match_punct("??"):
                node = Logical("??", node, self._and())
            else:
                break
        return node

    def _and(self):
        return self._logical_chain("&&", self._bitwise_or)

    def _logical_chain(self, op, sub):
        node = sub()
        while self._match_punct(op):
            node = Logical(op, node, sub())
        return node

    def _bitwise_or(self):
        return self._binop("|", self._bitwise_xor)

    def _bitwise_xor(self):
        return self._binop("^", self._bitwise_and)

    def _bitwise_and(self):
        return self._binop("&", self._equality)

    def _equality(self):
        return self._binop(("==", "!=", "===", "!=="), self._relational)

    def _relational(self):
        return self._binop(("<", "<=", ">", ">=", "in", "instanceof"),
                           self._shift)

    def _shift(self):
        return self._binop(("<<", ">>", ">>>"), self._additive)

    def _additive(self):
        return self._binop(("+", "-"), self._multiplicative)

    def _multiplicative(self):
        return self._binop(("*", "/", "%"), self._exponent)

    def _exponent(self):
        node = self._unary()
        if self._match_punct("**"):
            right = self._exponent()  # right-associative
            return Binary("**", node, right)
        return node

    def _binop(self, ops, sub):
        if isinstance(ops, str):
            ops = (ops,)
        node = sub()
        while True:
            value = self._op_in(ops)
            if value is None:
                break
            node = Binary(value, node, sub())
        return node

    def _op_in(self, texts):
        kind, value, _ = self._peek()
        if value in texts:
            self.pos += 1
            return value
        return None

    def _unary(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("!", "-", "+", "~", "++", "--"):
            self.pos += 1
            if value in ("++", "--"):
                return Update(value, self._unary(), True)
            return Unary(value, self._unary())
        if kind == "kw" and value in ("typeof", "delete", "void"):
            self.pos += 1
            return Unary(value, self._unary())
        if kind == "kw" and value == "await":
            if self.async_depth == 0:
                self._syntax("await is only valid in async functions")
            self.pos += 1
            return Await(self._unary())
        return self._call()

    def _call(self):
        node = self._primary()
        while True:
            if self._match_punct("("):
                node = Call(node, self._args())
            elif self._match_punct("."):
                node = Member(node, self._expect_property_name())
            elif self._match_punct("?."):
                kind, value, _ = self._peek()
                if kind == "punct" and value == "(":
                    call = Call(node, self._args())
                    call.optional = True
                    node = call
                elif kind == "punct" and value == "[":
                    index = self._expression()
                    self._expect_punct("]")
                    idx = Index(node, index)
                    idx.optional = True
                    node = idx
                else:
                    member = Member(node, self._expect_property_name())
                    member.optional = True
                    node = member
            elif self._match_punct("["):
                index = self._expression()
                self._expect_punct("]")
                node = Index(node, index)
            elif self._match_punct("++"):
                node = Update("++", node, False)
            elif self._match_punct("--"):
                node = Update("--", node, False)
            else:
                break
        return node

    def _args(self):
        return self._list(None, ")", self._arg_item)

    def _arg_item(self):
        if self._match_punct("..."):
            return Spread(self._expression())
        return self._expression()

    def _array_item(self):
        if self._match_punct("..."):
            return Spread(self._expression())
        return self._expression()

    def _new_expression(self):
        callee = self._primary()
        args = self._args() if self._match_punct("(") else []
        return New(callee, args)

    def _primary(self):
        kind, value, _ = self._peek()
        if kind in ("number", "string"):
            self.pos += 1
            return Literal(value)
        if kind == "regex":
            self.pos += 1
            return Literal(JSRexExp(self, value[0], value[1]))
        if kind == "template":
            self.pos += 1
            return self._template_literal(value)
        if kind == "kw":
            self.pos += 1
            if value in ("true", "false", "null", "undefined"):
                return Literal({"true": True, "false": False,
                                "null": None, "undefined": UNDEFINED}[value])
            if value == "function":
                return self._function_expression(False)
            if value == "this":
                return This()
            if value == "new":
                return self._new_expression()
            if value == "class":
                return self._class_expression()
            if value == "super":
                return Super()
            self._syntax(f"unexpected keyword '{value}'")
        if kind == "ident":
            if value == "async" and self._next_is_kw("function"):
                self.pos += 1
                self.pos += 1  # consume 'function'
                return self._function_expression(True)
            if value == "async":
                k2, v2, _ = self._peek2()
                if k2 == "ident" and self._peek3_is_arrow():
                    self.pos += 1
                    name = self._match_ident()
                    return self._arrow_rest([name], {}, None, True)
                if k2 == "punct" and v2 == "(" \
                        and self._paren_followed_by_arrow():
                    self.pos += 1
                    params, defaults, rest = self._param_list()
                    return self._arrow_rest(params, defaults, rest, True)
            if self._peek2_is("=>"):
                self.pos += 1
                return self._arrow_rest([value], {}, None, False)
            self.pos += 1
            return Identifier(value)
        if kind == "punct":
            if value == "(":
                if self._paren_followed_by_arrow():
                    params, defaults, rest = self._param_list()
                    return self._arrow_rest(params, defaults, rest, False)
                self.pos += 1
                node = self._expression()
                # Comma expressions inside parens: (0, fn)(...), (a, b).
                while self._match_punct(","):
                    node = self._expression()
                self._expect_punct(")")
                return node
            if value == "[":
                return ArrayLit(self._list("[", "]", self._array_item,
                                           trailing=True))
            if value == "{":
                return ObjectLit(self._list("{", "}", self._object_pair,
                                            trailing=True))
        self._syntax("unexpected token")

    def _function_expression(self, async_):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        params, defaults, rest, body = self._function_rest(async_)
        return FunctionExpr(name, params, body, async_, defaults, rest)

    def _template_literal(self, raw):
        quasis, exprs = [], []
        for quasi, expr_src in _split_template(raw):
            quasis.append(quasi)
            if expr_src is not None:
                exprs.append(_Parser(expr_src).parse_expression())
        return TemplateLiteral(quasis, exprs)

    def _class_declaration(self):
        name = self._expect_ident()
        superclass = None
        if self._match_kw("extends"):
            superclass = self._expression()
        methods = self._class_body()
        return ClassDecl(name, superclass, methods)

    def _class_expression(self):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        superclass = None
        if self._match_kw("extends"):
            superclass = self._expression()
        methods = self._class_body()
        return ClassExpr(name, superclass, methods)

    def _class_body(self):
        methods = []
        self._expect_punct("{")
        while True:
            if self._match_punct("}"):
                break
            if self._match_punct(";"):
                continue
            is_static = False
            accessor = None
            kt, vt, _ = self._peek()
            if kt == "kw" and vt == "static":
                k2, v2, _ = self._peek2()
                if not (k2 == "punct" and v2 == "("):
                    self.pos += 1
                    is_static = True
                    kt, vt, _ = self._peek()
            if kt == "ident" and vt in ("get", "set"):
                k2, v2, _ = self._peek2()
                if not (k2 == "punct" and v2 in ("(", "=", ";", "}", ",")):
                    self.pos += 1
                    accessor = vt
                    name = self._expect_property_name()
                else:
                    name = self._expect_property_name()
            else:
                name = self._expect_property_name()
            if self._peek_is_punct("("):
                params, defaults, rest = self._param_list()
                body = self._parse_stmts_until("}")
                methods.append(ClassMethod(name, params, body, is_static,
                                           accessor, defaults, rest))
            else:
                self._syntax("expected '(' in class method")
        return methods

    def _object_pair(self):
        if self._match_punct("..."):
            return Spread(self._expression())
        kind, value, _ = self._peek()
        if kind in ("ident", "string", "kw"):
            self.pos += 1
            key = value
        else:
            self._syntax("expected property name")
        if self._match_punct(":"):
            return key, self._expression()
        # Property shorthand: { name } === { name: name }.
        if kind == "ident":
            return key, Identifier(key)
        self._syntax("expected ':' after property name")


_Parser._STMT = {
    "var": lambda s: VarDecl("var", s._declaration_list()),
    "let": lambda s: VarDecl("let", s._declaration_list()),
    "const": lambda s: VarDecl("const", s._declaration_list()),
    "function": lambda s: s._function_declaration(False),
    "class": lambda s: s._class_declaration(),
    "return": lambda s: s._return_statement(),
    "if": lambda s: s._if_statement(),
    "while": lambda s: s._while_statement(),
    "do": lambda s: s._do_while_statement(),
    "switch": lambda s: s._switch_statement(),
    "for": lambda s: s._for_statement(),
    "break": lambda s: Break(),
    "continue": lambda s: Continue(),
    "throw": lambda s: s._throw_statement(),
    "try": lambda s: s._try_statement(),
}


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------


@dataclass
class _Timer:
    id: int
    due: float
    fn: object
    args: list
    interval: float = 0
    repeat: bool = False


class Interpreter:
    """Parses and executes JavaScript against a shared global scope.

    `run(source)` executes a whole program; `call(fn, *args)` invokes a
    function. `drain()` runs pending microtasks and due timers, and
    `advance(ms)` moves the virtual clock forward (used by the host's poll).
    """

    def __init__(self):
        def _js_log(*args):
            self.logs.append(" ".join(self.repr(a) for a in args))

        def _js_boolean(*args):
            return self._truthy(args[0]) if args else False

        def _js_parse_int(text, radix=None):
            text = str(text).lstrip()
            hexp = text.lower().startswith("0x")
            base = (16 if radix is None and hexp
                    else int(radix) if radix is not None else 10)
            if base == 0:
                base = 16 if hexp else \
                    8 if text.startswith("0") and len(text) > 1 else 10
            prefix_len = 2 if base == 16 and hexp else 0
            digits = 0
            for ch in text[prefix_len:]:
                if ch.lower() in "0123456789abcdefghijklmnopqrstuvwxyz"[:base]:
                    digits += 1
                else:
                    break
            return float("nan") if digits == 0 \
                else int(text[:prefix_len + digits], base)

        def _js_parse_float(text):
            match = re.match(
                r"^[+-]?(?:\d+\.?\d*|\.\d+|[iI][nN][fF]i?n?i?t?y?)",
                str(text).strip())
            if not match:
                return float("nan")
            tok = match.group(0)
            if tok.lower() == "infinity":
                return float("inf")
            try:
                return float(tok)
            except ValueError:
                return float("nan")

        self.logs = []
        self.globals = {
            "console": {"log": _js_log},
            "String": _StringCtor(self),
            "Number": _NumberCtor(self),
            "Boolean": _js_boolean,
            "Array": _ArrayCtor(self),
            "Object": _ObjectGlobal(self),
            "parseInt": _js_parse_int,
            "parseFloat": _js_parse_float,
            "NaN": float("nan"),
            "Infinity": float("inf"),
            "Promise": JSPromiseCtor(self),
            "Error": _ErrorCtor(),
            "RegExp": _RegExpCtor(self),
            "Date": _DateCtor(self),
            "Map": _MapCtor(self),
            "Set": _SetCtor(self),
            "Math": _js_math(),
            "JSON": {"parse": _js_json_parse, "stringify": _js_json_stringify},
            "setTimeout": self._native_set_timeout,
            "setInterval": self._native_set_interval,
            "clearTimeout": self._native_clear_timer,
            "clearInterval": self._native_clear_timer,
            "queueMicrotask": self._native_queue_microtask,
            "document": UNDEFINED,
            "window": UNDEFINED,
            "fetch": UNDEFINED,
            "XMLHttpRequest": UNDEFINED,
        }
        self.globals["window"] = JSGlobalObject(self)
        self.globals["globalThis"] = self.globals["window"]
        self.globals["localStorage"] = _LocalStorage(self)
        self._global_env = Environment()
        self._global_env.vars = self.globals
        self._microtasks = deque()
        self._timers = []
        self._timer_seq = 0
        self._now = 0.0

    # -- public API ------------------------------------------------------

    def run(self, source):
        """Parse and execute a whole program statement-by-statement."""
        program = _Parser(source).parse_program()
        try:
            self._pump_sync(self._exec_block(program.statements,
                                             self._global_env))
        except (_Return, _Break, _Continue):
            raise JSException("Illegal statement outside its context.") from None
        except _JSThrow as t:
            raise JSException(self.repr(t.value)) from None
        except JSException:
            raise
        except Exception as exc:
            raise JSException(str(exc)) from None

    def call(self, fn, *args):
        """Call a JSFunction, a plain Python callable, or a host object."""
        try:
            return self._call_value(fn, list(args))
        except _JSThrow as t:
            raise JSException(self.repr(t.value)) from None
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None

    def create_promise(self):
        return JSPromise(self)

    def repr(self, value):
        """JS-style string of a value."""
        if value is UNDEFINED:
            return "undefined"
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            if value == float("inf"):
                return "Infinity"
            if value == float("-inf"):
                return "-Infinity"
            if value != value:
                return "NaN"
            return str(int(value)) if float(value).is_integer() else str(value)
        if isinstance(value, list):
            return ",".join(self.repr(item) for item in value)
        if isinstance(value, JSFunction):
            return f"function {value.name}"
        js_repr = getattr(value, "js_repr", None)
        if callable(js_repr):
            return js_repr()
        if _is_objectish(value):
            return "[object Object]"
        return str(value)

    # -- host-object member access ---------------------------------------

    def js_get(self, obj, name):
        """Read member `name` off `obj` using the shared value model."""
        if isinstance(obj, dict):
            return obj.get(str(name), UNDEFINED)
        if isinstance(obj, list):
            return self._list_get(obj, name)
        if isinstance(obj, str):
            return self._string_get(obj, name)
        if isinstance(obj, (int, float)):
            return self._number_get(obj, name)
        if isinstance(obj, JSFunction):
            if name == "length":
                return len(obj.params)
            if name == "name":
                return obj.name
            if name == "prototype":
                return obj.prototype_obj()
            if name == "call":
                return lambda this_arg=UNDEFINED, *args: \
                    self._call_value(obj, list(args), this_arg)
            if name == "apply":
                def apply(this_arg=UNDEFINED, args=None):
                    arg_list = list(args) if isinstance(args, list) else []
                    return self._call_value(obj, arg_list, this_arg)
                return apply
            if name == "bind":
                def bind(this_arg=UNDEFINED, *pre):
                    def bound(*args):
                        return self._call_value(
                            obj, list(pre) + list(args), this_arg)
                    return bound
                return bind
            return UNDEFINED
        if callable(obj):
            if name == "call":
                return lambda this_arg=UNDEFINED, *args: \
                    self._call_value(obj, [this_arg, *args])
            if name == "apply":
                def apply(this_arg=UNDEFINED, args=None):
                    arg_list = list(args) if isinstance(args, list) else []
                    return self._call_value(obj, [this_arg, *arg_list])
                return apply
            if name == "bind":
                def bind(this_arg=UNDEFINED, *pre):
                    def bound(*args):
                        return self._call_value(
                            obj, [this_arg, *pre, *args])
                    return bound
                return bind
            return UNDEFINED
        return self._member_tail(obj, name)

    def js_set(self, obj, name, value):
        """Write member `name` on `obj` using the shared value model."""
        if isinstance(obj, dict):
            obj[str(name)] = value
            return
        if isinstance(obj, list):
            if name == "length":
                obj[:] = obj[:max(0, int(value))]
                return
            index = _int_index(name)
            if index is not None and index >= 0:
                # arr[5] = x grows the array with holes filled by undefined.
                if index >= _MAX_ARRAY_LEN:
                    raise JSException(
                        f"Array index {index} exceeds the allowed maximum")
                obj.extend([UNDEFINED] * (index + 1 - len(obj)))
                obj[index] = value
                return
            return
        if isinstance(obj, JSFunction):
            if name == "prototype":
                obj.set_prototype(value)
            return
        self._member_tail(obj, name, write=True, value=value)

    def _member_tail(self, obj, name, write=False, value=None):
        if _nullish(obj) or isinstance(obj, (str, int, float, bool, JSFunction)):
            return UNDEFINED if not write else None
        method = getattr(obj, "js_set" if write else "js_get", None)
        if method is not None:
            try:
                result = method(str(name)) if not write \
                    else method(str(name), value)
            except Exception as exc:
                raise (exc if isinstance(exc, (JSException, _JSThrow))
                       else JSException(str(exc))) from None
            return None if write else self._to_js(result)
        if write:
            return None
        if hasattr(obj, "__getitem__"):
            try:
                return self._to_js(obj[str(name)])
            except Exception:
                return UNDEFINED
        return UNDEFINED

    # -- native array/string members -------------------------------------

    def _default_compare(self, value):
        return self.repr(value)

    def _list_get(self, arr, name):
        if name == "length":
            return len(arr)
        if name == "push":
            def push(*values):
                arr.extend(values)
                return len(arr)
            return push
        if name == "pop":
            def pop():
                if not arr:
                    return UNDEFINED
                return arr.pop()
            return pop
        if name == "join":
            def join(sep=","):
                return (sep if isinstance(sep, str) else ",").join(
                    self.repr(item) for item in arr)
            return join
        if name == "indexOf":
            def index_of(value, start=0):
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = s if s >= 0 else max(len(arr) + s, 0)
                for i in range(s, len(arr)):
                    if _strict_eq(arr[i], value):
                        return i
                return -1
            return index_of
        if name == "lastIndexOf":
            def last_index_of(value, start=None):
                s = len(arr) - 1 if start is None or start is UNDEFINED \
                    else _to_int32(start)
                s = min(s, len(arr) - 1)
                s = s if s >= 0 else len(arr) + s
                for i in range(s, -1, -1):
                    if _strict_eq(arr[i], value):
                        return i
                return -1
            return last_index_of
        if name == "includes":
            return lambda value: any(_strict_eq(v, value) for v in arr)
        if name == "concat":
            def concat(*others):
                out = list(arr)
                for o in others:
                    if isinstance(o, list):
                        out.extend(o)
                    elif o is not UNDEFINED:
                        out.append(o)
                return out
            return concat
        if name == "reverse":
            def reverse():
                arr.reverse()
                return arr
            return reverse
        if name == "shift":
            def shift():
                if not arr:
                    return UNDEFINED
                return arr.pop(0)
            return shift
        if name == "unshift":
            def unshift(*values):
                arr[0:0] = list(values)
                return len(arr)
            return unshift
        if name == "slice":
            def slice_(start=0, end=None):
                n = len(arr)
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = max(0, s if s >= 0 else n + s)
                e = n if end is None or end is UNDEFINED else _to_int32(end)
                e = max(0, e if e >= 0 else n + e)
                return arr[s:max(s, min(e, n))]
            return slice_
        if name == "splice":
            def splice(start, delete_count=None, *items):
                n = len(arr)
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = max(0, min(n, s if s >= 0 else n + s))
                dc = n - s if delete_count is None or delete_count is UNDEFINED \
                    else _to_int32(delete_count)
                dc = max(0, min(dc, n - s))
                removed = arr[s:s + dc]
                arr[s:s + dc] = list(items)
                return removed
            return splice
        if name == "sort":
            def sort(compare_fn=UNDEFINED):
                if compare_fn is UNDEFINED:
                    arr.sort(key=self._default_compare)
                else:
                    arr.sort(key=functools.cmp_to_key(
                        lambda a, b: _to_number(
                            self._call_value(compare_fn, [a, b]))))
                return arr
            return sort
        if name == "toString":
            return lambda: self.repr(arr)
        if name == "map":
            def map_(fn):
                return [self._call_value(fn, [item, i, arr])
                        for i, item in enumerate(arr)]
            return map_
        if name == "filter":
            def filter_(fn):
                return [item for i, item in enumerate(arr)
                        if self._truthy(self._call_value(fn, [item, i, arr]))]
            return filter_
        if name == "forEach":
            def for_each(fn):
                for i, item in enumerate(arr):
                    self._call_value(fn, [item, i, arr])
                return UNDEFINED
            return for_each
        if name == "find":
            def find(fn):
                for i, item in enumerate(arr):
                    if self._truthy(self._call_value(fn, [item, i, arr])):
                        return item
                return UNDEFINED
            return find
        if name == "findIndex":
            def find_index(fn):
                for i, item in enumerate(arr):
                    if self._truthy(self._call_value(fn, [item, i, arr])):
                        return i
                return -1
            return find_index
        if name == "some":
            def some(fn):
                return any(
                    self._truthy(self._call_value(fn, [item, i, arr]))
                    for i, item in enumerate(arr))
            return some
        if name == "every":
            def every(fn):
                return all(
                    self._truthy(self._call_value(fn, [item, i, arr]))
                    for i, item in enumerate(arr))
            return every
        if name == "reduce":
            def reduce(fn, initial=UNDEFINED):
                acc = initial
                start = 0
                if acc is UNDEFINED:
                    if not arr:
                        raise JSException(
                            "Reduce of empty array with no initial value")
                    acc = arr[0]
                    start = 1
                for i in range(start, len(arr)):
                    acc = self._call_value(fn, [acc, arr[i], i, arr])
                return acc
            return reduce
        if name == "reduceRight":
            def reduce_right(fn, initial=UNDEFINED):
                if not arr:
                    if initial is UNDEFINED:
                        raise JSException(
                            "Reduce of empty array with no initial value")
                    return initial
                acc = arr[-1] if initial is UNDEFINED else initial
                start = len(arr) - 2 if initial is UNDEFINED else len(arr) - 1
                for i in range(start, -1, -1):
                    acc = self._call_value(fn, [acc, arr[i], i, arr])
                return acc
            return reduce_right
        if name == "flat":
            def flat(depth=1):
                d = _to_int32(depth) if depth is not UNDEFINED else 1

                def flatten(x, rem):
                    out = []
                    for v in x:
                        if rem > 0 and isinstance(v, list):
                            out.extend(flatten(v, rem - 1))
                        else:
                            out.append(v)
                    return out
                return flatten(arr, max(0, d))
            return flat
        if name == "at":
            def at(idx):
                i = _to_int32(idx)
                if i < 0:
                    i += len(arr)
                if 0 <= i < len(arr):
                    return arr[i]
                return UNDEFINED
            return at
        index = _int_index(name)
        if index is not None and -len(arr) <= index < len(arr):
            return arr[index]
        return UNDEFINED

    def _number_get(self, num, name):
        if name == "toFixed":
            return lambda digits=0: self._to_fixed(num, digits)
        if name == "toString":
            return lambda radix=None: self._number_to_string(num, radix)
        if name == "toLocaleString":
            return lambda: self._number_to_string(num, None)
        if name == "valueOf":
            return lambda: num
        return UNDEFINED

    def _to_fixed(self, num, digits):
        d = _to_int32(digits) if digits is not UNDEFINED else 0
        d = max(0, min(100, d))
        if num != num:
            return "NaN"
        if num == float("inf"):
            return "Infinity"
        if num == float("-inf"):
            return "-Infinity"
        return format(num, f".{d}f")

    def _number_to_string(self, num, radix):
        if num != num:
            return "NaN"
        if num == float("inf"):
            return "Infinity"
        if num == float("-inf"):
            return "-Infinity"
        if radix is not None and radix is not UNDEFINED:
            base = _to_int32(radix)
            if base < 2 or base > 36:
                raise JSException("toString() radix must be between 2 and 36")
            neg = num < 0
            n = int(abs(num))
            digits = "0123456789abcdefghijklmnopqrstuvwxyz"
            out = []
            if n == 0:
                out = ["0"]
            while n:
                out.append(digits[n % base])
                n //= base
            return ("-" if neg else "") + "".join(reversed(out))
        return self.repr(num)

    def _string_get(self, text, name):
        if name == "length":
            return len(text)
        if name == "charAt":
            return lambda idx=0: _safe_char(text, _to_int32(idx))
        if name == "at":
            def at(idx):
                i = _to_int32(idx)
                if i < 0:
                    i += len(text)
                return _safe_char(text, i)
            return at
        if name == "charCodeAt":
            return lambda idx=0: _safe_code(text, _to_int32(idx))
        if name == "indexOf":
            def index_of(sub, start=0):
                s = _to_int32(start) if start is not UNDEFINED else 0
                return text.find(str(sub), max(0, s))
            return index_of
        if name == "lastIndexOf":
            def last_index_of(sub, start=None):
                s = len(text) - 1 if start is None or start is UNDEFINED \
                    else min(_to_int32(start), len(text) - 1)
                return text.rfind(str(sub), 0, max(0, s) + 1)
            return last_index_of
        if name == "includes":
            return lambda sub: str(sub) in text
        if name == "startsWith":
            return lambda sub: text.startswith(str(sub))
        if name == "endsWith":
            return lambda sub: text.endswith(str(sub))
        if name == "toLowerCase":
            return lambda: text.lower()
        if name == "toUpperCase":
            return lambda: text.upper()
        if name == "toLocaleLowerCase":
            return lambda: text.lower()
        if name == "toLocaleUpperCase":
            return lambda: text.upper()
        if name == "trim":
            return lambda: text.strip()
        if name == "trimStart":
            return lambda: text.lstrip()
        if name == "trimEnd":
            return lambda: text.rstrip()
        if name == "slice":
            def sl(start=0, end=None):
                n = len(text)
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = max(0, s if s >= 0 else n + s)
                e = n if end is None or end is UNDEFINED else _to_int32(end)
                e = max(0, e if e >= 0 else n + e)
                return text[s:max(s, min(e, n))]
            return sl
        if name == "substring":
            def substring(start, end=None):
                n = len(text)
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = max(0, min(n, s))
                e = n if end is None or end is UNDEFINED else _to_int32(end)
                e = max(0, min(n, e))
                if s > e:
                    s, e = e, s
                return text[s:e]
            return substring
        if name == "substr":
            def substr(start, length=None):
                n = len(text)
                s = _to_int32(start) if start is not UNDEFINED else 0
                s = max(0, s if s >= 0 else n + s)
                ln = n if length is None or length is UNDEFINED \
                    else max(0, _to_int32(length))
                return text[s:s + ln]
            return substr
        if name == "concat":
            return lambda *others: text + "".join(self.repr(o) for o in others)
        if name == "repeat":
            def repeat(count):
                c = max(0, _to_int32(count))
                if c and len(text) > _MAX_STRING_OUT // c:
                    raise JSException("String.prototype.repeat result is too large")
                return text * c
            return repeat
        if name == "padStart":
            return lambda ln, fill=" ": _js_pad(
                text, max(0, _to_int32(ln)), str(fill), True)
        if name == "padEnd":
            return lambda ln, fill=" ": _js_pad(
                text, max(0, _to_int32(ln)), str(fill), False)
        if name == "split":
            return lambda sep=UNDEFINED, limit=None: \
                self._string_split(text, sep, limit)
        if name == "match":
            return lambda regex: self._string_match(text, regex)
        if name == "matchAll":
            return lambda regex: self._string_match(text, regex)
        if name == "replace":
            return lambda pat, repl: self._string_replace(text, pat, repl)
        if name == "replaceAll":
            return lambda pat, repl: self._string_replace(text, pat, repl,
                                                          all_=True)
        if name == "localeCompare":
            return lambda other: 0 if text == str(other) \
                else (-1 if text < str(other) else 1)
        if name == "toString":
            return lambda: text
        if name == "valueOf":
            return lambda: text
        index = _int_index(name)
        if index is not None and -len(text) <= index < len(text):
            return text[index]
        return UNDEFINED

    def _string_split(self, text, sep, limit):
        if sep is UNDEFINED or sep is None:
            return [text]
        if isinstance(sep, JSRexExp):
            maxsplit = _to_int32(limit) if limit is not None \
                and limit is not UNDEFINED else 0
            return sep._re.split(text, maxsplit=maxsplit)
        s = self.repr(sep)
        if s == "":
            return list(text)
        if limit is not None and limit is not UNDEFINED:
            return text.split(s, _to_int32(limit))
        return text.split(s)

    def _string_match(self, text, regex):
        if not isinstance(regex, JSRexExp):
            raise JSException("String.prototype.match: not a RegExp")
        if regex.global_:
            out = regex._re.findall(text)
            return out if out else None
        m = regex._re.search(text)
        if m is None:
            return None
        res = [m.group(0)]
        res.extend(UNDEFINED if g is None else g for g in m.groups())
        return res

    def _repl_text(self, repl):
        if isinstance(repl, str):
            return repl
        return self.repr(repl)

    def _string_replace(self, text, pat, repl, all_=False):
        if isinstance(pat, JSRexExp):
            count = 0 if (pat.global_ or all_) else 1
            if _is_js_function(repl):
                def sub_fn(m):
                    args = [m.group(0)]
                    args.extend(UNDEFINED if g is None else g
                                for g in m.groups())
                    return self.repr(self._call_value(repl, args))
                return pat._re.sub(sub_fn, text, count=count)
            return pat._re.sub(lambda m: self._repl_text(repl), text,
                               count=count)
        pat_str = str(pat) if isinstance(pat, str) else self.repr(pat)
        if all_:
            return text.replace(pat_str, self._repl_text(repl))
        if pat_str == "":
            return self._repl_text(repl) + text
        idx = text.find(pat_str)
        if idx < 0:
            return text
        return text[:idx] + self._repl_text(repl) + text[idx + len(pat_str):]

    # -- timers / microtasks ---------------------------------------------

    def advance(self, ms):
        """Move the virtual clock forward; due timers fire on the next drain."""
        self._now += float(ms)

    def enqueue(self, job):
        self._microtasks.append(job)

    def drain(self):
        """Run pending microtasks and due timers until quiescent.

        Each microtask/timer callback gets a fresh step budget, and a total
        processed-work cap stops microtask/timer storms from pinning the UI
        thread forever."""
        processed = 0
        while True:
            while self._microtasks:
                if processed >= _MAX_DRAIN:
                    self.logs.append("JS error: too many queued microtasks")
                    self._microtasks.clear()
                    return
                job = self._microtasks.popleft()
                processed += 1
                try:
                    job()
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
            due = [t for t in self._timers if t.due <= self._now]
            if not due:
                return
            for t in due:
                if processed >= _MAX_DRAIN:
                    self.logs.append("JS error: too many timer callbacks")
                    return
                self._timers.remove(t)
                processed += 1
                try:
                    self._call_value(t.fn, t.args)
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
                if t.repeat:
                    t.due += t.interval
                    self._timers.append(t)

    def _error_text(self, e):
        if isinstance(e, _JSThrow):
            return "JS error: " + self.repr(e.value)
        return "JS error: " + str(e)

    def _note_unhandled_rejection(self, reason):
        self.logs.append("Unhandled promise rejection: " + self.repr(reason))

    def _native_set_timeout(self, fn, ms=0):
        return self._schedule_timer(fn, _to_number(ms), repeat=False)

    def _native_set_interval(self, fn, ms=0):
        return self._schedule_timer(fn, _to_number(ms), repeat=True)

    def _schedule_timer(self, fn, ms, repeat):
        if len(self._timers) >= _MAX_TIMERS:
            raise JSException("Too many timers")
        self._timer_seq += 1
        timer_id = self._timer_seq
        self._timers.append(_Timer(timer_id, self._now + max(0, ms), fn, [],
                                   interval=max(0, ms), repeat=repeat))
        return timer_id

    def _native_clear_timer(self, timer_id):
        for i, t in enumerate(self._timers):
            if t.id == timer_id:
                del self._timers[i]
                return
        return UNDEFINED

    def _native_queue_microtask(self, fn):
        self.enqueue(lambda: self._call_value(fn, []))

    # -- evaluation ---------------------------------------------------------

    def _to_js(self, value):
        return value

    def _truthy(self, value):
        if value is False or value is UNDEFINED or value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0 and value == value  # NaN is falsy
        return value != ""

    def _index_name(self, value):
        return value if isinstance(value, str) else self.repr(value)

    def _pump_sync(self, gen):
        """Drive a generator to completion. Suspension means an `await`
        leaked into synchronous code, which is a parser-level error."""
        value = None
        while True:
            try:
                value = gen.send(value)
            except StopIteration as stop:
                return stop.value
            if isinstance(value, _Suspend):
                raise JSException("await is only valid in async functions")

    def _call_value(self, fn, args, this_arg=UNDEFINED):
        if isinstance(fn, JSFunction):
            if fn.async_:
                return self._start_async_call(fn, args, this_arg)
            return self._pump_sync(self._call_function(fn, args, this_arg))
        if fn is UNDEFINED or fn is None:
            raise JSException(f"{self.repr(fn)} is not a function.")
        try:
            if hasattr(fn, "js_call"):
                return self._to_js(fn.js_call(*args))
            if callable(fn):
                return self._to_js(fn(*args))
        except Exception as exc:
            raise (exc if isinstance(exc, (JSException, _JSThrow))
                   else JSException(str(exc))) from None
        raise JSException(f"{self.repr(fn)} is not a function.")

    def _construct(self, callee, args):
        if hasattr(callee, "js_new"):
            return self._to_js(callee.js_new(*args))
        if isinstance(callee, JSFunction):
            obj = JSClassInstance(callee.prototype_obj())
            result = self._pump_sync(self._call_function(callee, args, obj))
            if _is_objectish(result):
                return result
            return obj
        raise JSException(f"{self.repr(callee)} is not a constructor")

    def _bind_args(self, scope, fn, args):
        """Bind positional args, applying defaults, rest, and `this`."""
        args = list(args)
        for i, name in enumerate(fn.params):
            if i < len(args):
                scope.set_var(name, args[i])
            elif fn.defaults and name in fn.defaults:
                value = self._pump_sync(
                    self._eval(fn.defaults[name], scope))
                scope.set_var(name, value)
            else:
                scope.set_var(name, UNDEFINED)
        if fn.rest is not None:
            scope.set_var(fn.rest, args[len(fn.params):])

    def _set_this(self, scope, fn, this_arg):
        if fn.arrow:
            scope.vars["this"] = fn.env.get("this")
        elif this_arg is not UNDEFINED:
            scope.vars["this"] = this_arg
        if fn.super_info is not None:
            scope.vars["__super__"] = JSSuper(
                self, this_arg, fn.super_info[0], fn.super_info[1])

    def _call_function(self, fn, args, this_arg=UNDEFINED):
        scope = Environment(fn.env)
        scope.function_scope = scope  # private var scope per invocation
        self._bind_args(scope, fn, args)
        self._set_this(scope, fn, this_arg)
        try:
            if fn.body_expr is not None:
                return (yield from self._eval(fn.body_expr, scope))
            yield from self._exec_block(fn.body, scope)
        except _Return as ret:
            return ret.value
        except (_Break, _Continue):
            raise JSException("Break or continue outside of a loop.") from None
        return UNDEFINED

    def _construct_on(self, obj, fn, args):
        """Run a class constructor body with `this` bound to `obj`."""
        scope = Environment(fn.env)
        scope.function_scope = scope
        self._bind_args(scope, fn, args)
        self._set_this(scope, fn, obj)
        try:
            self._pump_sync(self._exec_block(fn.body, scope))
        except _Return as ret:
            if _is_objectish(ret.value):
                return ret.value
        return obj

    def _start_async_call(self, fn, args, this_arg=UNDEFINED):
        promise = JSPromise(self)
        scope = Environment(fn.env)
        scope.function_scope = scope
        args = list(args)
        for i, name in enumerate(fn.params):
            if i < len(args):
                scope.set_var(name, args[i])
            elif fn.defaults and name in fn.defaults:
                try:
                    value = self._pump_sync(
                        self._eval(fn.defaults[name], scope))
                except (JSException, _JSThrow) as e:
                    promise.reject(str(e))
                    return promise
                scope.set_var(name, value)
            else:
                scope.set_var(name, UNDEFINED)
        if fn.rest is not None:
            scope.set_var(fn.rest, args[len(fn.params):])
        self._set_this(scope, fn, this_arg)
        gen = self._exec_block(fn.body, scope)
        self._resume_async(gen, promise, None, False)
        return promise

    def _resume_async(self, gen, promise, send_value, is_throw):
        """Advance an async coroutine until it completes or suspends."""
        try:
            if is_throw:
                value = gen.throw(_JSThrow(send_value))
            else:
                value = gen.send(send_value)
        except StopIteration as stop:
            promise.resolve(stop.value)
            return
        except _Return as ret:
            promise.resolve(ret.value)
            return
        except (_Break, _Continue):
            promise.reject("Break or continue outside of a loop.")
            return
        except _JSThrow as t:
            promise.reject(t.value)
            return
        except JSException as e:
            promise.reject(str(e))
            return
        if isinstance(value, _Suspend):
            p = value.promise

            def cont(settled_value, rejected):
                self._resume_async(gen, promise, settled_value, rejected)

            p._on_settle(cont)
            return
        promise.resolve(value)

    def _as_promise(self, value):
        if isinstance(value, JSPromise):
            return value
        then = self._thenable_method(value)
        if then is not None:
            p = JSPromise(self)
            p._assimilate(value, then)
            return p
        p = JSPromise(self)
        p.resolve(value)
        return p

    def _thenable_method(self, value):
        if not _is_objectish(value):
            return None
        try:
            then = self.js_get(value, "then")
        except Exception:
            return None
        if isinstance(then, JSFunction) or callable(then):
            return then
        return None

    def _eval(self, node, env):
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Identifier):
            name = node.name
            value = env.get(name)
            if value is UNDEFINED and name in self.globals:
                return self.globals[name]
            return value
        if isinstance(node, This):
            return env.get("this")
        if isinstance(node, Super):
            sup = env.get("__super__")
            return sup if isinstance(sup, JSSuper) else UNDEFINED
        if isinstance(node, ArrayLit):
            out = []
            for item in node.items:
                if isinstance(item, Spread):
                    val = yield from self._eval(item.expr, env)
                    if isinstance(val, (list, str)):
                        out.extend(val)
                    else:
                        out.append(val)
                else:
                    out.append((yield from self._eval(item, env)))
            return out
        if isinstance(node, ObjectLit):
            out = {}
            for pair in node.pairs:
                if isinstance(pair, Spread):
                    val = yield from self._eval(pair.expr, env)
                    if isinstance(val, dict):
                        out.update(val)
                    elif isinstance(val, JSClassInstance):
                        for k, v in val._props.items():
                            out[k] = v
                    continue
                key, expr = pair
                out[key] = yield from self._eval(expr, env)
            return out
        if isinstance(node, FunctionExpr):
            return JSFunction(node.params, node.body, env, self,
                              node.name or "", node.async_, node.defaults,
                              node.rest)
        if isinstance(node, ArrowFunc):
            return JSFunction(node.params, node.body, env, self, "",
                              node.async_, node.defaults, node.rest,
                              arrow=True, body_expr=node.body_expr)
        if isinstance(node, ClassExpr):
            return (yield from self._eval_class(node, env))
        if isinstance(node, TemplateLiteral):
            out = node.quasis[0]
            for expr, quasi in zip(node.exprs, node.quasis[1:]):
                out += self.repr((yield from self._eval(expr, env)))
                out += quasi
            return out
        if isinstance(node, Unary):
            return (yield from self._eval_unary(node, env))
        if isinstance(node, Update):
            return (yield from self._eval_update(node, env))
        if isinstance(node, Binary):
            left = yield from self._eval(node.left, env)
            right = yield from self._eval(node.right, env)
            return self._eval_binary(node.op, left, right)
        if isinstance(node, Logical):
            left = yield from self._eval(node.left, env)
            if node.op == "??":
                if _nullish(left):
                    return (yield from self._eval(node.right, env))
                return left
            if self._truthy(left) == (node.op == "||"):
                return left
            return (yield from self._eval(node.right, env))
        if isinstance(node, Conditional):
            if self._truthy((yield from self._eval(node.cond, env))):
                return (yield from self._eval(node.then_expr, env))
            return (yield from self._eval(node.else_expr, env))
        if isinstance(node, Assign):
            return (yield from self._eval_assign(node, env))
        if isinstance(node, Call):
            return (yield from self._eval_call(node, env))
        if isinstance(node, New):
            callee = yield from self._eval(node.callee, env)
            args = yield from self._eval_args(node.args, env)
            return self._construct(callee, args)
        if isinstance(node, Member):
            obj = yield from self._eval(node.obj, env)
            if node.optional and _nullish(obj):
                return UNDEFINED
            return self.js_get(obj, node.name)
        if isinstance(node, Index):
            obj = yield from self._eval(node.obj, env)
            if node.optional and _nullish(obj):
                return UNDEFINED
            name = self._index_name((yield from self._eval(node.index, env)))
            return self.js_get(obj, name)
        if isinstance(node, Await):
            value = yield from self._eval(node.expr, env)
            promise = self._as_promise(value)
            if promise.rejected:
                raise _JSThrow(promise.value)
            if promise.pending:
                return (yield _Suspend(promise))
            return promise.value
        raise JSException(f"Unknown expression {type(node).__name__}.")

    def _eval_args(self, args, env):
        out = []
        for a in args:
            if isinstance(a, Spread):
                val = yield from self._eval(a.expr, env)
                if isinstance(val, list):
                    out.extend(val)
                elif isinstance(val, str):
                    out.extend(val)
                else:
                    out.append(val)
            else:
                out.append((yield from self._eval(a, env)))
        return out

    def _eval_class(self, node, env):
        parent = None
        if node.superclass is not None:
            parent = yield from self._eval(node.superclass, env)
            if not isinstance(parent, (JSClass, JSFunction)):
                raise JSException("Class extends value is not a constructor")
        name = node.name or ""
        prototype = {}
        statics = {}
        if parent is not None:
            proto = parent.prototype if isinstance(parent, JSClass) \
                else parent.prototype_obj()
            prototype["__proto__"] = proto
        parent_ctor = parent.ctor if isinstance(parent, JSClass) \
            else (parent if isinstance(parent, JSFunction) else None)
        super_info = (prototype.get("__proto__")
                      if parent is not None else None, parent_ctor)
        ctor_fn = None
        for m in node.methods:
            fn = JSFunction(m.params, m.body, env, self, name, False,
                            m.defaults, m.rest, super_info=super_info)
            if m.name == "constructor" and not m.is_static:
                ctor_fn = fn
            elif m.is_static:
                statics[m.name] = fn
            else:
                prototype[m.name] = fn
        cls = JSClass(self, name, ctor_fn, prototype, parent)
        cls.statics = statics
        return cls

    def _eval_unary(self, node, env):
        operand = yield from self._eval(node.operand, env)
        if node.op == "!":
            return not self._truthy(operand)
        if node.op == "-":
            return -_to_number(operand)
        if node.op == "+":
            return _to_number(operand)
        if node.op == "~":
            return ~_to_int32(operand)
        if node.op == "typeof":
            return _typeof(operand)
        if node.op == "void":
            return UNDEFINED
        if node.op == "delete":
            obj, name = yield from self._lvalue(node.operand, env)
            if obj is None:
                return False
            if isinstance(obj, dict):
                obj.pop(name, None)
                return True
            if isinstance(obj, JSClassInstance):
                obj._props.pop(name, None)
                return True
            if isinstance(obj, list):
                idx = _int_index(name)
                if idx is not None and 0 <= idx < len(obj):
                    obj[idx] = UNDEFINED
                return True
            return True
        raise JSException(f"Unknown unary operator '{node.op}'.")

    def _eval_update(self, node, env):
        current = yield from self._read_lvalue(node.operand, env)
        value = _to_number(current) + (1 if node.op == "++" else -1)
        yield from self._write_lvalue(node.operand, env, value)
        return value if node.prefix else current

    def _eval_binary(self, op, left, right):
        if op in ("+", "-", "*", "/", "%", "**", "&", "|", "^", "<<", ">>",
                  ">>>"):
            return self._binary_op(op, left, right)
        if op == "in":
            return self._eval_in(left, right)
        if op == "instanceof":
            return self._eval_instanceof(left, right)
        return self._compare(op, left, right)

    def _eval_in(self, key, obj):
        name = key if isinstance(key, str) else self.repr(key)
        if isinstance(obj, dict):
            return name in obj
        if isinstance(obj, JSClassInstance):
            if name in obj._props:
                return True
            p = obj._proto
            while p is not None:
                if isinstance(p, dict) and name in p:
                    return True
                p = p.get("__proto__") if isinstance(p, dict) else None
            return False
        if isinstance(obj, list):
            return _int_index(name) is not None
        return False

    def _eval_instanceof(self, obj, ctor):
        if isinstance(ctor, JSClass):
            target = ctor.prototype
        elif isinstance(ctor, JSFunction):
            target = ctor.prototype_obj()
        else:
            for name, types in (
                    ("Array", (list,)),
                    ("Object", (dict, list, JSClassInstance)),
                    ("RegExp", (JSRexExp,)),
                    ("Map", (JSMap,)),
                    ("Set", (JSSet,)),
                    ("Date", (JSDate,)),
                    ("String", (str,)),
                    ("Number", (int, float))):
                if ctor is self.globals.get(name):
                    return isinstance(obj, types)
            raise JSException(
                "Right-hand side of 'instanceof' is not callable")
        if not isinstance(obj, JSClassInstance):
            return False
        p = obj._proto
        while p is not None:
            if p is target:
                return True
            p = p.get("__proto__") if isinstance(p, dict) else None
        return False

    def _compare(self, op, left, right):
        if op == "==":
            result = _loose_eq(left, right)
        elif op == "!=":
            result = not _loose_eq(left, right)
        elif op == "===":
            result = _strict_eq(left, right)
        elif op == "!==":
            result = not _strict_eq(left, right)
        elif op == "<":
            return self._ordered(left, right)
        elif op == "<=":
            return not self._ordered(right, left)
        elif op == ">":
            return self._ordered(right, left)
        elif op == ">=":
            return not self._ordered(left, right)
        else:
            raise JSException(f"Unknown operator '{op}'.")
        return result

    def _ordered(self, left, right):
        if isinstance(left, str) and isinstance(right, str):
            return left < right
        return _to_number(left) < _to_number(right)

    def _binary_op(self, op, left, right):
        if op == "+":
            # Arrays participate in string concatenation via their join
            # representation (so [] + [] === "", [] + 5 === "5"), matching JS
            # ToPrimitive on arrays.
            if isinstance(left, (str, list)) or isinstance(right, (str, list)):
                return self.repr(left) + self.repr(right)
            return _to_number(left) + _to_number(right)
        if op == "**":
            a, b = _to_number(left), _to_number(right)
            try:
                return a ** b
            except (ValueError, OverflowError, ZeroDivisionError):
                return float("nan")
        left, right = _to_number(left), _to_number(right)
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return _divide(left, right)
        if op == "%":
            return _modulo(left, right)
        if op == "&":
            return _to_int32(left) & _to_int32(right)
        if op == "|":
            return _to_int32(left) | _to_int32(right)
        if op == "^":
            return _to_int32(left) ^ _to_int32(right)
        if op == "<<":
            return _to_int32(_to_int32(left) << (_to_int32(right) & 31))
        if op == ">>":
            return _to_int32(_to_int32(left) >> (_to_int32(right) & 31))
        if op == ">>>":
            return ((_to_int32(left) & 0xFFFFFFFF) >> (_to_int32(right) & 31))
        raise JSException(f"Unknown binary operator '{op}'.")

    def _eval_assign(self, node, env):
        value = yield from self._eval(node.value, env)
        obj, name = yield from self._lvalue(node.target, env)
        if node.op == "=":
            if obj is None:
                env.assign(name, value)
            else:
                self.js_set(obj, name, value)
            return value
        current = env.get(name) if obj is None else self.js_get(obj, name)
        op = node.op
        if op in ("&&=", "||=", "??="):
            if op == "&&=":
                result = value if self._truthy(current) else current
            elif op == "||=":
                result = current if self._truthy(current) else value
            else:
                result = value if _nullish(current) else current
        else:
            result = self._binary_op(op[:-1], current, value)
        if obj is None:
            env.assign(name, result)
        else:
            self.js_set(obj, name, result)
        return result

    def _eval_call(self, node, env):
        callee_node = node.callee
        if isinstance(callee_node, Super):
            sup = yield from self._eval(callee_node, env)
            if not isinstance(sup, JSSuper):
                raise JSException("'super' keyword unexpected here")
            args = yield from self._eval_args(node.args, env)
            return sup.js_call(*args)
        if isinstance(callee_node, (Member, Index)):
            obj = yield from self._eval(callee_node.obj, env)
            if callee_node.optional and _nullish(obj):
                return UNDEFINED
            if isinstance(callee_node, Member):
                name = callee_node.name
            else:
                name = self._index_name(
                    (yield from self._eval(callee_node.index, env)))
            fn = self.js_get(obj, name)
            args = yield from self._eval_args(node.args, env)
            if isinstance(obj, JSSuper):
                return self._call_value(fn, args, this_arg=obj.this)
            return self._call_value(fn, args, this_arg=obj)
        fn = yield from self._eval(callee_node, env)
        if node.optional and _nullish(fn):
            return UNDEFINED
        args = yield from self._eval_args(node.args, env)
        return self._call_value(fn, args)

    def _lvalue(self, target, env):
        if isinstance(target, Identifier):
            return None, target.name
        if isinstance(target, Member):
            obj = yield from self._eval(target.obj, env)
            return obj, target.name
        if isinstance(target, Index):
            obj = yield from self._eval(target.obj, env)
            name = self._index_name((yield from self._eval(target.index, env)))
            return obj, name
        raise JSException("Invalid assignment target")

    def _read_lvalue(self, target, env):
        obj, name = yield from self._lvalue(target, env)
        return env.get(name) if obj is None else self.js_get(obj, name)

    def _write_lvalue(self, target, env, value):
        obj, name = yield from self._lvalue(target, env)
        if obj is None:
            env.assign(name, value)
        else:
            self.js_set(obj, name, value)

    # -- statements ---------------------------------------------------------

    def _exec_block(self, statements, env):
        for stmt in statements:
            if isinstance(stmt, FunctionDecl):
                env.set_var(stmt.name, JSFunction(
                    stmt.params, stmt.body, env, self, stmt.name, stmt.async_,
                    stmt.defaults, stmt.rest))
        for stmt in statements:
            yield from self._exec(stmt, env)

    def _exec(self, node, env):
        if isinstance(node, Block):
            yield from self._exec_block(node.statements, Environment(env))
        elif isinstance(node, VarDecl):
            setter = {"var": env.set_var, "let": env.set_let,
                      "const": env.set_const}[node.kind]
            for target, expr in node.decls:
                if node.kind == "const" and expr is None:
                    raise JSException(
                        f"Missing initializer in const declaration "
                        f"'{self._target_name(target)}'.")
                value = UNDEFINED if expr is None \
                    else (yield from self._eval(expr, env))
                if isinstance(target, Pattern):
                    yield from self._bind_pattern(target, value, env, setter)
                else:
                    setter(target, value)
        elif isinstance(node, ClassDecl):
            cls = yield from self._eval_class(node, env)
            env.set_var(node.name, cls)
        elif isinstance(node, FunctionDecl):
            pass  # hoisted by _exec_block
        elif isinstance(node, ExprStmt):
            yield from self._eval(node.expr, env)
        elif isinstance(node, If):
            if self._truthy((yield from self._eval(node.cond, env))):
                yield from self._exec(node.then, env)
            elif node.else_ is not None:
                yield from self._exec(node.else_, env)
        elif isinstance(node, While):
            while self._truthy((yield from self._eval(node.cond, env))):
                try:
                    yield from self._exec(node.body, env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif isinstance(node, DoWhile):
            while True:
                try:
                    yield from self._exec(node.body, env)
                except _Break:
                    break
                except _Continue:
                    pass
                if not self._truthy((yield from self._eval(node.cond, env))):
                    break
        elif isinstance(node, Switch):
            yield from self._exec_switch(node, env)
        elif isinstance(node, For):
            yield from self._exec_for(node, env)
        elif isinstance(node, ForIn):
            yield from self._exec_for_in(node, env)
        elif isinstance(node, ForOf):
            yield from self._exec_for_of(node, env)
        elif isinstance(node, Return):
            value = UNDEFINED if node.value is None \
                else (yield from self._eval(node.value, env))
            raise _Return(value)
        elif isinstance(node, Break):
            raise _Break()
        elif isinstance(node, Continue):
            raise _Continue()
        elif isinstance(node, Throw):
            raise _JSThrow((yield from self._eval(node.expr, env)))
        elif isinstance(node, TryCatch):
            yield from self._exec_try(node, env)
        else:
            raise JSException(f"Unknown statement {type(node).__name__}.")

    def _exec_for(self, node, env):
        child = Environment(env)
        if node.init is not None:
            yield from self._exec(node.init, child)
        while node.cond is None or \
                self._truthy((yield from self._eval(node.cond, child))):
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                pass
            if node.update is not None:
                yield from self._eval(node.update, child)

    def _exec_switch(self, node, env):
        value = yield from self._eval(node.expr, env)
        start = None
        default = None
        for i, (kind, test, _) in enumerate(node.cases):
            if kind == "default":
                default = i
                continue
            if _strict_eq(value, (yield from self._eval(test, env))):
                start = i
                break
        if start is None:
            start = default
        if start is None:
            return
        for kind, _, stmts in node.cases[start:]:
            try:
                for stmt in stmts:
                    yield from self._exec(stmt, env)
            except _Break:
                break
            except _Continue:
                raise

    def _target_name(self, target):
        return target if isinstance(target, str) else "..."

    def _pattern_setter(self, env, var_kind):
        if var_kind is None:
            return env.assign
        return {"var": env.set_var, "let": env.set_let,
                "const": env.set_const}[var_kind]

    def _bind_pattern(self, pattern, value, env, setter):
        if pattern.kind == "array":
            items = list(value) if isinstance(value, list) else []
            for i, (target, default) in enumerate(pattern.parts):
                item = items[i] if i < len(items) else UNDEFINED
                if item is UNDEFINED and default is not None:
                    item = (yield from self._eval(default, env))
                yield from self._bind_target(target, item, env, setter)
            if pattern.rest is not None:
                rest = items[len(pattern.parts):]
                yield from self._bind_target(pattern.rest, rest, env, setter)
            return
        if isinstance(value, dict):
            src = value
        else:
            src = {key: self.js_get(value, key)
                   for key, _, _ in pattern.parts}
            if pattern.rest is not None:
                for key in self._own_keys(value):
                    src.setdefault(key, self.js_get(value, key))
        for key, target, default in pattern.parts:
            item = src.get(key, UNDEFINED)
            if item is UNDEFINED and default is not None:
                item = (yield from self._eval(default, env))
            yield from self._bind_target(target, item, env, setter)
        if pattern.rest is not None:
            rest = {k: v for k, v in src.items()}
            yield from self._bind_target(pattern.rest, rest, env, setter)

    def _own_keys(self, value):
        if isinstance(value, dict):
            return list(value.keys())
        if isinstance(value, list):
            return [str(i) for i in range(len(value))]
        return []

    def _bind_target(self, target, value, env, setter):
        if isinstance(target, str):
            setter(target, value)
            return
        if isinstance(target, Pattern):
            yield from self._bind_pattern(target, value, env, setter)
            return
        if isinstance(target, Identifier):
            setter(target.name, value)

    def _bind_loop_var(self, env, var_kind, name, value):
        if var_kind is not None:
            setter = {"var": env.set_var, "let": env.set_let,
                      "const": env.set_const}[var_kind]
            setter(name, value)
        else:
            env.assign(name, value)

    def _exec_for_in(self, node, env):
        obj = yield from self._eval(node.iterable, env)
        keys = []
        if isinstance(obj, dict):
            keys = list(obj.keys())
        elif isinstance(obj, JSClassInstance):
            seen = set()
            for k in obj._props:
                keys.append(k)
                seen.add(k)
            p = obj._proto
            while p is not None:
                if isinstance(p, dict):
                    for k in p:
                        if k != "__proto__" and k not in seen:
                            keys.append(k)
                            seen.add(k)
                    p = p.get("__proto__")
                else:
                    p = None
        elif isinstance(obj, list):
            keys = list(range(len(obj)))
        for key in keys:
            child = Environment(env)
            self._bind_loop_var(child, node.var_kind, node.name, key)
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                continue

    def _exec_for_of(self, node, env):
        obj = yield from self._eval(node.iterable, env)
        if isinstance(obj, list):
            items = list(obj)
        elif isinstance(obj, str):
            items = list(obj)
        else:
            items = []
        for item in items:
            child = Environment(env)
            self._bind_loop_var(child, node.var_kind, node.name, item)
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                continue

    def _exec_try(self, node, env):
        error = None  # ("throw", value) or ("error", message)
        try:
            yield from self._exec(node.try_block, Environment(env))
        except _JSThrow as t:
            error = ("throw", t.value)
        except JSException as e:
            error = ("error", str(e))
        except (_Return, _Break, _Continue):
            if node.finally_block is not None:
                yield from self._exec(node.finally_block, env)
            raise
        if error is not None and node.catch_block is not None:
            child = Environment(env)
            if node.catch_param:
                child.set_let(node.catch_param, error[1])
            yield from self._exec(node.catch_block, child)
        elif error is not None:
            if node.finally_block is not None:
                yield from self._exec(node.finally_block, env)
            if error[0] == "throw":
                raise _JSThrow(error[1])
            raise JSException(error[1])
        if node.finally_block is not None:
            yield from self._exec(node.finally_block, env)