const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseSafe });

    const python_include = b.option([]const u8, "python-include", "Python include directory") orelse "";

    const mod = b.createModule(.{
        .root_source_file = b.path("src/lib.zig"),
        .target = target,
        .optimize = optimize,
    });
    if (python_include.len > 0) {
        mod.addIncludePath(.{ .cwd_relative = python_include });
    }
    mod.link_libc = true;

    const lib = b.addLibrary(.{
        .linkage = .dynamic,
        .name = "feetbrowser_engine",
        .root_module = mod,
    });
    b.installArtifact(lib);

    const tests = b.addTest(.{
        .root_module = mod,
    });
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);
}
