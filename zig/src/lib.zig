//! FeetBrowser JS engine (Zig port). Module root.

const std = @import("std");

pub const ast = @import("ast.zig");
pub const value = @import("value.zig");
pub const token = @import("token.zig");
pub const parser = @import("parser.zig");
pub const interp = @import("interp.zig");

test "tokenizer + parser front-end" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const alloc = arena.allocator();

    const prog = try parser.parseProgram(alloc,
        \\var a = 1 + 2 * 3;
        \\function f(x) { return x * 2; }
        \\var obj = { key: "value" };
        \\var t = `sum: ${1 + 1}`;
    );
    try std.testing.expect(prog.* == .program);
    try std.testing.expectEqual(@as(usize, 4), prog.program.len);
}
