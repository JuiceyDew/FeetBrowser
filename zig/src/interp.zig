//! Interpreter core (port of `rust/src/interp.rs`). Placeholder for now —
//! the full interpreter lands in the next pass. value.zig references this
//! type through function pointers, so it must exist for the front-end to
//! compile.

const std = @import("std");
const value = @import("value.zig");

pub const Interpreter = struct {
    arena: std.heap.ArenaAllocator,
    err_msg: []const u8 = "",
    thrown_value: value.JsValue = .undefined_val,
    return_value: value.JsValue = .undefined_val,
    globals: value.ValueObject = .{},
    logs: std.ArrayListUnmanaged([]const u8) = .{},
    steps: u64 = 0,
};
