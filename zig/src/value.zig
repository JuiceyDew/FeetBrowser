//! JS value model, environment, functions, promises, and shared helpers.
//!
//! Ported from `rust/src/value.rs`. Host objects (Python DOM nodes, native
//! callables) are held as `*anyopaque` pointers so this module stays free of
//! Python headers; the concrete PyObject casts live in `pybind.zig`.
//!
//! Memory: everything is allocated from a per-interpreter arena, so "cloning"
//! a value is just copying a pointer or slice — no refcounting. The arena is
//! destroyed with the interpreter.

const std = @import("std");

// The concrete interpreter lives in `interp.zig`. Function-pointer types use
// `*anyopaque` as the context so this module has no import cycle with it; the
// interpreter casts the context back to `*Interpreter` at the native-call
// boundary in `stdlib.zig` / `interp.zig`.

pub const MAX_STEPS: u64 = 8_000_000;
pub const MAX_ARRAY_LEN: usize = 1_000_000;
pub const MAX_STRING_OUT: usize = 32_000_000;
pub const MAX_TIMERS: usize = 10_000;
pub const MAX_DRAIN: usize = 1_000_000;

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

/// Control-flow / error signals. Payloads travel through the interpreter's
/// error-context fields (`err_msg`, `thrown_value`, `return_value`) so they
/// can cross the C/Python boundary without boxing.
pub const JsErr = error{
    JsError,
    Thrown,
    BreakSignal,
    ContinueSignal,
    ReturnSignal,
    Budget,
    Syntax,
};

pub const EvResult = JsErr!JsValue;

// --------------------------------------------------------------------------
// Value types
// --------------------------------------------------------------------------

pub const ValueArray = struct {
    items: std.ArrayListUnmanaged(JsValue) = .{},
};

pub const ValueObject = struct {
    props: std.StringArrayHashMapUnmanaged(JsValue) = .{},
};

pub const JSFunction = struct {
    name: []const u8,
    params: [][]const u8,
    defaults: std.StringArrayHashMapUnmanaged(*ast.Node),
    rest: ?[]const u8,
    body: []const *ast.Node,
    body_expr: ?*ast.Node,
    env: *Environment,
    async_: bool,
    arrow: bool,
    super_info: ?SuperInfo,
    prototype: ?*ValueObject,

    pub const SuperInfo = struct {
        parent_proto: JsValue,
        parent_ctor: JsValue,
    };
};

pub const PromiseState = enum { pending, resolved, rejected };

pub const JsPromise = struct {
    state: PromiseState = .pending,
    value: JsValue = .undefined_val,
    observers: std.ArrayListUnmanaged(*Observer) = .{},
};

/// A promise observer: `call(value, rejected)`. Closures in the Rust source
/// are turned into these records, which is why they carry a `kind` tag.
pub const Observer = struct {
    kind: ObserverKind,
    ctx: *anyopaque,
    value: JsValue = .undefined_val,

    pub const ObserverKind = enum {
        adopt, // adopt another promise (ctx = *JsPromise)
        then_cb, // a then/catch/finally handler (ctx = *ThenCtx)
        generic, // a generic fn(ctx, value, rejected) (ctx = fn ptr)
    };
};

pub const ThenCtx = struct {
    child: *JsPromise,
    on_ok: JsValue,
    on_err: JsValue,
};

pub const JsClass = struct {
    name: []const u8,
    prototype: *ValueObject,
    ctor: ?*JSFunction,
    parent: ?JsValue,
    statics: *ValueObject,
};

pub const JsClassInstance = struct {
    proto: *ValueObject,
    props: *ValueObject,
};

pub const JsSuper = struct {
    this_: JsValue,
    parent_proto: JsValue,
    parent_ctor: JsValue,
};

pub const JsMap = struct {
    store: std.StringArrayHashMapUnmanaged(JsValue) = .{},
};

pub const JsSet = struct {
    store: std.StringArrayHashMapUnmanaged(JsValue) = .{},
};

pub const JsHostError = struct {
    message: []const u8,
    name: []const u8,
};

pub const JsDate = struct {
    ms: f64,
};

pub const JsRegex = struct {
    source: []const u8,
    flags: []const u8,
    global_: bool,
    ignore_case: bool,
    multiline: bool,
    last_index: f64 = 0,
    // Lazily-created Python compiled regex (owned by Python; NULL until set).
    py_pattern: ?*anyopaque = null,
};

pub const NativeFn = *const fn (ctx: *anyopaque, self: *const JsValue, args: []JsValue) EvResult;
pub const NativeGet = *const fn (ctx: *anyopaque, self: *const JsValue, name: []const u8) JsErr!JsValue;
pub const NativeSet = *const fn (ctx: *anyopaque, self: *const JsValue, name: []const u8, value: *const JsValue) JsErr!void;

/// A native host function/constructor object (Math, JSON, Map, Set, ...).
pub const Native = struct {
    name: []const u8,
    call: ?NativeFn = null,
    ctor: ?NativeFn = null,
    get: ?NativeGet = null,
    set: ?NativeSet = null,
};

/// A host callable (a Rust/Zig closure) exposed to JS, e.g. promise handlers.
pub const Callback = struct {
    call: *const fn (ctx: *anyopaque, self: *const JsValue, args: []JsValue) EvResult,
};

pub const JsValue = union(enum) {
    undefined_val,
    null_val,
    bool: bool,
    number: f64,
    str: []const u8,
    array: *ValueArray,
    object: *ValueObject,
    function: *JSFunction,
    promise: *JsPromise,
    class: *JsClass,
    instance: *JsClassInstance,
    map: *JsMap,
    set: *JsSet,
    date: *JsDate,
    regex: *JsRegex,
    js_error: *JsHostError,
    super_: *JsSuper,
    native: *Native,
    callback: *Callback,
    host: *anyopaque,

    pub fn makeObject(alloc: std.mem.Allocator) !JsValue {
        const o = try alloc.create(ValueObject);
        o.* = .{};
        return .{ .object = o };
    }

    pub fn makeArray(alloc: std.mem.Allocator) !JsValue {
        const a = try alloc.create(ValueArray);
        a.* = .{};
        return .{ .array = a };
    }

    pub fn makeStr(alloc: std.mem.Allocator, text: []const u8) !JsValue {
        return .{ .str = try alloc.dupe(u8, text) };
    }
};

// --------------------------------------------------------------------------
// Type predicates and coercions
// --------------------------------------------------------------------------

pub fn nullish(v: JsValue) bool {
    return v == .undefined_val or v == .null_val;
}

pub fn isObjectish(v: JsValue) bool {
    return switch (v) {
        .undefined_val, .null_val, .number, .str, .bool => false,
        else => true,
    };
}

pub fn truthy(v: JsValue) bool {
    return switch (v) {
        .undefined_val, .null_val => false,
        .bool => |b| b,
        .number => |n| n != 0 and n == n,
        .str => |s| s.len != 0,
        else => true,
    };
}

pub fn isNumberish(v: JsValue) bool {
    return v == .number or v == .str;
}

/// ToNumber for loose equality and arithmetic coercion.
pub fn toNumber(v: JsValue) f64 {
    return switch (v) {
        .undefined_val => std.math.nan(f64),
        .null_val => 0,
        .bool => |b| if (b) 1 else 0,
        .number => |n| n,
        .str => |s| blk: {
            const text = std.mem.trim(u8, s, " \t\r\n");
            if (text.len == 0) break :blk 0;
            break :blk parseNumber(text);
        },
        else => std.math.nan(f64),
    };
}

/// Coerce to int32 (ToInt32) used by bitwise ops and array indexes.
pub fn toInt32(v: JsValue) i32 {
    const n = @as(i64, @intFromFloat(@trunc(toNumber(v)))) & 0xFFFF_FFFF;
    return if (n & (1 << 31) != 0)
        @intCast(n - (1 << 32))
    else
        @intCast(n);
}

pub fn parseNumber(text0: []const u8) f64 {
    var prefix: [32]u8 = undefined;
    var t: []const u8 = text0;
    if (text0.len > 0 and text0[0] == '.') {
        t = std.fmt.bufPrint(&prefix, "0{s}", .{text0}) catch text0;
    } else if (text0.len > 0 and text0[text0.len - 1] == '.') {
        t = text0[0 .. text0.len - 1];
    }
    if (allDigits(t)) {
        const i = std.fmt.parseInt(i64, t, 10) catch return std.math.nan(f64);
        return @floatFromInt(i);
    }
    return std.fmt.parseFloat(f64, t) catch std.math.nan(f64);
}

pub fn allDigits(text: []const u8) bool {
    if (text.len == 0) return false;
    for (text) |c| {
        if (!std.ascii.isDigit(c)) return false;
    }
    return true;
}

/// An integer array index if `name` is a canonical decimal integer string.
pub fn intIndex(name: []const u8) ?i64 {
    const i = std.fmt.parseInt(i64, name, 10) catch return null;
    var buf: [32]u8 = undefined;
    const s = std.fmt.bufPrint(&buf, "{d}", .{i}) catch return null;
    if (!std.mem.eql(u8, s, name)) return null;
    return i;
}

pub fn divide(a: f64, b: f64) f64 {
    if (b == 0) {
        if (a == 0) return std.math.nan(f64);
        return if (a > 0) std.math.inf(f64) else -std.math.inf(f64);
    }
    return a / b;
}

pub fn modulo(a: f64, b: f64) f64 {
    if (b == 0) return std.math.nan(f64);
    return @mod(a, b);
}

/// A hashable key for Map/Set that treats primitives by value and objects by
/// identity, mirroring `_map_key`.
pub fn mapKey(alloc: std.mem.Allocator, v: JsValue) ![]const u8 {
    return switch (v) {
        .undefined_val => alloc.dupe(u8, "u"),
        .null_val => alloc.dupe(u8, "n"),
        .bool => |b| std.fmt.allocPrint(alloc, "b:{any}", .{b}),
        .number => |n| blk: {
            if (n != n) break :blk alloc.dupe(u8, "num:nan");
            break :blk std.fmt.allocPrint(alloc, "num:{d}", .{n});
        },
        .str => |s| std.fmt.allocPrint(alloc, "s:{s}", .{s}),
        else => std.fmt.allocPrint(alloc, "obj:{d}", .{@intFromPtr(objPtr(v))}),
    };
}

fn objPtr(v: JsValue) *const anyopaque {
    return switch (v) {
        .array => |p| p,
        .object => |p| p,
        .function => |p| p,
        .promise => |p| p,
        .class => |p| p,
        .instance => |p| p,
        .map => |p| p,
        .set => |p| p,
        .date => |p| p,
        .regex => |p| p,
        .js_error => |p| p,
        .super_ => |p| p,
        .native => |p| p,
        .callback => |p| p,
        .host => |p| p,
        else => @ptrCast(&(v)),
    };
}

pub fn safeChar(text: []const u8, i: i64) []const u8 {
    if (i < 0) return "";
    // Byte-indexed approximation (JS strings are UTF-16; ASCII paths match).
    const idx: usize = @intCast(i);
    if (idx >= text.len) return "";
    return text[idx .. idx + 1];
}

pub fn safeCode(text: []const u8, i: i64) f64 {
    if (i < 0) return std.math.nan(f64);
    const idx: usize = @intCast(i);
    if (idx >= text.len) return std.math.nan(f64);
    return @floatFromInt(text[idx]);
}

pub fn jsPad(alloc: std.mem.Allocator, text: []const u8, length: i64, fill: []const u8, left: bool) ![]const u8 {
    if (fill.len == 0) return text;
    const need = length - @as(i64, @intCast(text.len));
    if (need <= 0) return text;
    if (need > MAX_STRING_OUT) return error.JsError;
    const un: usize = @intCast(need);
    var padded = try alloc.alloc(u8, un);
    var i: usize = 0;
    while (i < un) : (i += 1) {
        padded[i] = fill[i % fill.len];
    }
    if (left) {
        return std.fmt.allocPrint(alloc, "{s}{s}", .{ padded, text });
    }
    return std.fmt.allocPrint(alloc, "{s}{s}", .{ text, padded });
}

pub fn isJsFunction(v: JsValue) bool {
    return switch (v) {
        .function, .native, .callback => true,
        .host => true, // host objects may be callable; typeof_value decides
        else => false,
    };
}

/// Reference identity for object-like values, mirroring Python `is`.
pub fn sameRef(a: JsValue, b: JsValue) bool {
    return switch (a) {
        .array => |x| b == .array and x == b.array,
        .object => |x| b == .object and x == b.object,
        .function => |x| b == .function and x == b.function,
        .promise => |x| b == .promise and x == b.promise,
        .class => |x| b == .class and x == b.class,
        .instance => |x| b == .instance and x == b.instance,
        .map => |x| b == .map and x == b.map,
        .set => |x| b == .set and x == b.set,
        .date => |x| b == .date and x == b.date,
        .regex => |x| b == .regex and x == b.regex,
        .js_error => |x| b == .js_error and x == b.js_error,
        .super_ => |x| b == .super_ and x == b.super_,
        .native => |x| b == .native and x == b.native,
        .callback => |x| b == .callback and x == b.callback,
        .host => |x| b == .host and x == b.host,
        else => false,
    };
}

pub fn looseEq(a: JsValue, b: JsValue) bool {
    const na = nullish(a);
    const nb = nullish(b);
    if (na or nb) return na and nb;
    if (a == .str and b == .str) return std.mem.eql(u8, a.str, b.str);
    if (isNumberish(a) or isNumberish(b)) {
        const ca = toNumber(a);
        const cb = toNumber(b);
        if (ca != ca or cb != cb) return false;
        return ca == cb;
    }
    if (isObjectish(a) and isObjectish(b)) return sameRef(a, b);
    return false;
}

pub fn strictEq(a: JsValue, b: JsValue) bool {
    const ta = jsTypeof(a);
    const tb = jsTypeof(b);
    if (!std.mem.eql(u8, ta, tb)) return false;
    if (std.mem.eql(u8, ta, "object") or std.mem.eql(u8, ta, "function"))
        return sameRef(a, b);
    return switch (a) {
        .number => |x| x == b.number and x == x,
        .str => |x| std.mem.eql(u8, x, b.str),
        .bool => |x| x == b.bool,
        .undefined_val => true,
        .null_val => true,
        else => sameRef(a, b),
    };
}

pub fn jsTypeof(v: JsValue) []const u8 {
    return switch (v) {
        .undefined_val => "undefined",
        .null_val => "object",
        .bool => "boolean",
        .str => "string",
        .number => "number",
        .function => "function",
        .native, .callback => "function",
        .host => "object",
        else => "object",
    };
}

// --------------------------------------------------------------------------
// Environment
// --------------------------------------------------------------------------

pub const Environment = struct {
    parent: ?*Environment,
    vars: ValueObject,
    lets: ValueObject,
    consts: ValueObject,
    function_scope: ?*Environment,

    pub fn init(alloc: std.mem.Allocator, parent: ?*Environment) !*Environment {
        const env = try alloc.create(Environment);
        env.* = .{
            .parent = parent,
            .vars = .{},
            .lets = .{},
            .consts = .{},
            .function_scope = if (parent) |p| p.function_scope else null,
        };
        return env;
    }

    /// A fresh function-invocation scope: its own var scope.
    pub fn function(alloc: std.mem.Allocator, parent: ?*Environment) !*Environment {
        const env = try init(alloc, parent);
        env.function_scope = env;
        return env;
    }

    pub fn setVar(self: *Environment, alloc: std.mem.Allocator, name: []const u8, value: JsValue) !void {
        const scope = self.function_scope orelse self;
        try scope.vars.props.put(alloc, name, value);
    }

    pub fn setLet(self: *Environment, alloc: std.mem.Allocator, name: []const u8, value: JsValue) !void {
        try self.lets.props.put(alloc, name, value);
    }

    pub fn setConst(self: *Environment, alloc: std.mem.Allocator, name: []const u8, value: JsValue) !void {
        try self.consts.props.put(alloc, name, value);
    }

    pub fn get(self: *Environment, name: []const u8) JsValue {
        var env: ?*Environment = self;
        while (env) |e| {
            if (e.lets.props.get(name)) |v| return v;
            if (e.consts.props.get(name)) |v| return v;
            if (e.vars.props.get(name)) |v| return v;
            env = e.parent;
        }
        return .undefined_val;
    }

    pub fn assign(self: *Environment, alloc: std.mem.Allocator, name: []const u8, value: JsValue) JsErr!void {
        var env: ?*Environment = self;
        while (env) |e| {
            if (e.lets.props.contains(name)) {
                try e.lets.props.put(alloc, name, value);
                return;
            }
            if (e.consts.props.contains(name)) {
                return error.JsError;
            }
            if (e.vars.props.contains(name)) {
                try e.vars.props.put(alloc, name, value);
                return;
            }
            env = e.parent;
        }
        try self.vars.props.put(alloc, name, value);
    }
};

const ast = @import("ast.zig");
