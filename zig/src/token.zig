//! Tokenizer ported from `rust/src/token.rs`.

const std = @import("std");

pub const MAX_TOKENS: usize = 200_000;

pub const TokKind = enum { number, str, template, ident, kw, punct, regex };

pub const TokPayload = union(enum) {
    none,
    str: []const u8,
    number: f64,
    regex: RegexPayload,

    pub const RegexPayload = struct { source: []const u8, flags: []const u8 };
};

pub const Token = struct {
    kind: TokKind,
    text: []const u8,
    payload: TokPayload,
    offset: usize,
};

const KEYWORDS = [_][]const u8{
    "var", "let", "const", "function", "return", "if", "else", "while",
    "for", "break", "continue", "true", "false", "null", "undefined",
    "typeof", "throw", "try", "catch", "finally", "new", "this", "await",
    "class", "extends", "super", "static", "in", "instanceof", "delete",
    "void", "of", "switch", "case", "default", "do",
};

const PunctSpec = struct { text: []const u8, len: usize };
const PUNCT = [_]PunctSpec{
    .{ .text = ">>>=", .len = 4 },
    .{ .text = "...", .len = 3 }, .{ .text = "===", .len = 3 }, .{ .text = "!==", .len = 3 },
    .{ .text = "**=", .len = 3 }, .{ .text = "&&=", .len = 3 }, .{ .text = "||=", .len = 3 },
    .{ .text = "??=", .len = 3 }, .{ .text = ">>>", .len = 3 },
    .{ .text = "==", .len = 2 }, .{ .text = "!=", .len = 2 }, .{ .text = "<=", .len = 2 },
    .{ .text = ">=", .len = 2 }, .{ .text = "&&", .len = 2 }, .{ .text = "||", .len = 2 },
    .{ .text = "+=", .len = 2 }, .{ .text = "-=", .len = 2 }, .{ .text = "*=", .len = 2 },
    .{ .text = "/=", .len = 2 }, .{ .text = "%=", .len = 2 }, .{ .text = "++", .len = 2 },
    .{ .text = "--", .len = 2 }, .{ .text = "**", .len = 2 }, .{ .text = ">>=", .len = 2 },
    .{ .text = "<<=", .len = 2 }, .{ .text = "&=", .len = 2 }, .{ .text = "|=", .len = 2 },
    .{ .text = "^=", .len = 2 }, .{ .text = "??", .len = 2 }, .{ .text = "=>", .len = 2 },
    .{ .text = ">>", .len = 2 }, .{ .text = "<<", .len = 2 },
    .{ .text = "{", .len = 1 }, .{ .text = "}", .len = 1 }, .{ .text = "(", .len = 1 },
    .{ .text = ")", .len = 1 }, .{ .text = "[", .len = 1 }, .{ .text = "]", .len = 1 },
    .{ .text = ";", .len = 1 }, .{ .text = ",", .len = 1 }, .{ .text = ".", .len = 1 },
    .{ .text = ":", .len = 1 }, .{ .text = "?", .len = 1 }, .{ .text = "=", .len = 1 },
    .{ .text = "!", .len = 1 }, .{ .text = "+", .len = 1 }, .{ .text = "-", .len = 1 },
    .{ .text = "*", .len = 1 }, .{ .text = "/", .len = 1 }, .{ .text = "%", .len = 1 },
    .{ .text = "<", .len = 1 }, .{ .text = ">", .len = 1 }, .{ .text = "&", .len = 1 },
    .{ .text = "|", .len = 1 }, .{ .text = "^", .len = 1 }, .{ .text = "~", .len = 1 },
    .{ .text = "`", .len = 1 },
};

fn isKeyword(word: []const u8) bool {
    for (KEYWORDS) |k| {
        if (std.mem.eql(u8, k, word)) return true;
    }
    return false;
}

fn simpleEsc(c: u8) ?u8 {
    return switch (c) {
        'n' => '\n',
        't' => '\t',
        '\\' => '\\',
        '\'' => '\'',
        '"' => '"',
        '\n' => 0,
        else => null,
    };
}

fn regexAllowed(prev: ?*const Token) bool {
    const p = prev orelse return true;
    return switch (p.kind) {
        .ident, .number, .str, .template, .regex => false,
        .kw => blk: {
            break :blk !(std.mem.eql(u8, p.text, "true") or std.mem.eql(u8, p.text, "false") or
                std.mem.eql(u8, p.text, "null") or std.mem.eql(u8, p.text, "undefined") or
                std.mem.eql(u8, p.text, "this") or std.mem.eql(u8, p.text, "super"));
        },
        .punct => blk: {
            break :blk !(std.mem.eql(u8, p.text, ")") or std.mem.eql(u8, p.text, "]") or
                std.mem.eql(u8, p.text, "}") or std.mem.eql(u8, p.text, "++") or
                std.mem.eql(u8, p.text, "--"));
        },
    };
}

fn findTemplateEnd(s: []const u8, start: usize) ?usize {
    var i: usize = start + 1;
    const n = s.len;
    while (i < n) {
        const c = s[i];
        if (c == '\\') {
            i += 2;
        } else if (c == '$' and i + 1 < n and s[i + 1] == '{') {
            var j: usize = i + 2;
            var d: i64 = 1;
            var q: ?u8 = null;
            while (j < n) {
                const cj = s[j];
                if (q) |quote| {
                    if (cj == '\\') {
                        j += 2;
                        continue;
                    }
                    if (cj == quote) q = null;
                } else if (cj == '\'' or cj == '"' or cj == '`') {
                    q = cj;
                } else if (cj == '{') {
                    d += 1;
                } else if (cj == '}') {
                    d -= 1;
                    if (d == 0) break;
                }
                j += 1;
            }
            if (j >= n) return null;
            i = j + 1;
        } else if (c == '`') {
            return i + 1;
        } else {
            i += 1;
        }
    }
    return null;
}

pub const TokenizeResult = union(enum) {
    tokens: []Token,
    err: []const u8,
};

pub fn tokenize(alloc: std.mem.Allocator, source: []const u8) TokenizeResult {
    const fail = struct {
        fn line(src: []const u8, offset: usize) usize {
            var c: usize = 1;
            for (src[0..offset]) |ch| {
                if (ch == '\n') c += 1;
            }
            return c;
        }
    };

    var tokens = std.ArrayListUnmanaged(Token).empty;
    const n = source.len;
    var i: usize = 0;

    while (i < n) {
        const ch = source[i];
        const prev: ?*const Token = if (tokens.items.len > 0) &tokens.items[tokens.items.len - 1] else null;

        if (ch == ' ' or ch == '\t' or ch == '\r' or ch == '\n') {
            i += 1;
        } else if (std.mem.startsWith(u8, source[i..], "//")) {
            if (std.mem.indexOfScalar(u8, source[i..], '\n')) |d| {
                i += d + 1;
            } else i = n;
        } else if (std.mem.startsWith(u8, source[i..], "/*")) {
            if (std.mem.indexOf(u8, source[i + 2 ..], "*/")) |d| {
                i += 2 + d + 2;
            } else {
                const ln = fail.line(source, i);
                const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unterminated block comment", .{ln}) catch return .{ .err = "" };
                return .{ .err = msg };
            }
        } else if (std.ascii.isDigit(ch) or
            (ch == '.' and i + 1 < n and std.ascii.isDigit(source[i + 1])))
        {
            var j = i;
            if (ch == '0' and i + 1 < n and (source[i + 1] == 'x' or source[i + 1] == 'X')) {
                j = i + 2;
                while (j < n and std.ascii.isHex(source[j])) j += 1;
                const digits = source[i + 2 .. j];
                const v: u64 = if (j > i + 2) std.fmt.parseInt(u64, digits, 16) catch 0 else 0;
                tokens.append(alloc, .{
                    .kind = .number,
                    .text = source[i..j],
                    .payload = .{ .number = @floatFromInt(v) },
                    .offset = i,
                }) catch return .{ .err = "" };
                i = j;
            } else if (ch == '0' and i + 1 < n and (source[i + 1] == 'b' or source[i + 1] == 'B')) {
                j = i + 2;
                while (j < n and (source[j] == '0' or source[j] == '1')) j += 1;
                const digits = source[i + 2 .. j];
                const v: u64 = if (j > i + 2) std.fmt.parseInt(u64, digits, 2) catch 0 else 0;
                tokens.append(alloc, .{
                    .kind = .number,
                    .text = source[i..j],
                    .payload = .{ .number = @floatFromInt(v) },
                    .offset = i,
                }) catch return .{ .err = "" };
                i = j;
            } else {
                while (j < n and std.ascii.isDigit(source[j])) j += 1;
                if (j < n and source[j] == '.') {
                    j += 1;
                    while (j < n and std.ascii.isDigit(source[j])) j += 1;
                }
                if (j < n and (source[j] == 'e' or source[j] == 'E')) {
                    var k = j + 1;
                    if (k < n and (source[k] == '+' or source[k] == '-')) k += 1;
                    if (k < n and std.ascii.isDigit(source[k])) {
                        while (k < n and std.ascii.isDigit(source[k])) k += 1;
                        j = k;
                    }
                }
                const num = parseNumberText(source[i..j]);
                tokens.append(alloc, .{
                    .kind = .number,
                    .text = source[i..j],
                    .payload = .{ .number = num },
                    .offset = i,
                }) catch return .{ .err = "" };
                i = j;
            }
        } else if (ch == '"' or ch == '\'') {
            const quote = ch;
            i += 1;
            var buf = std.ArrayListUnmanaged(u8).empty;
            var terminated = false;
            while (true) {
                if (i >= n) break;
                const c = source[i];
                if (c == '\\') {
                    i += 1;
                    if (i >= n) break;
                    const esc = source[i];
                    i += 1;
                    if (simpleEsc(esc)) |s| {
                        if (s != 0) buf.append(alloc, s) catch return .{ .err = "" };
                    } else if (esc == 'x' or esc == 'u') {
                        const size: usize = if (esc == 'u') 4 else 2;
                        if (i + size <= n) {
                            const hex = source[i .. i + size];
                            if (std.fmt.parseInt(u21, hex, 16)) |cp| {
                                var bytes: [4]u8 = undefined;
                                const len = std.unicode.utf8Encode(cp, &bytes) catch 0;
                                if (len > 0) buf.appendSlice(alloc, bytes[0..len]) catch return .{ .err = "" };
                            } else |_| {}
                            i += size;
                        }
                    } else {
                        buf.append(alloc, esc) catch return .{ .err = "" };
                    }
                } else if (c == quote) {
                    i += 1;
                    terminated = true;
                    break;
                } else if (c == '\n') {
                    break;
                } else {
                    buf.append(alloc, c) catch return .{ .err = "" };
                    i += 1;
                }
            }
            if (!terminated) {
                const ln = fail.line(source, i);
                const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unterminated string literal", .{ln}) catch return .{ .err = "" };
                return .{ .err = msg };
            }
            const text = buf.items;
            tokens.append(alloc, .{
                .kind = .str,
                .text = text,
                .payload = .{ .str = text },
                .offset = i,
            }) catch return .{ .err = "" };
        } else if (ch == '`') {
            const j = findTemplateEnd(source, i) orelse {
                const ln = fail.line(source, i);
                const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unterminated template literal", .{ln}) catch return .{ .err = "" };
                return .{ .err = msg };
            };
            const raw = source[i + 1 .. j - 1];
            tokens.append(alloc, .{
                .kind = .template,
                .text = raw,
                .payload = .{ .str = raw },
                .offset = i,
            }) catch return .{ .err = "" };
            i = j;
        } else if (std.ascii.isAlphabetic(ch) or ch == '_' or ch == '$') {
            var j = i;
            while (j < n and (std.ascii.isAlphanumeric(source[j]) or source[j] == '_' or source[j] == '$')) j += 1;
            const word = source[i..j];
            const kw = isKeyword(word);
            tokens.append(alloc, .{
                .kind = if (kw) .kw else .ident,
                .text = word,
                .payload = .none,
                .offset = i,
            }) catch return .{ .err = "" };
            i = j;
        } else if (ch == '/' and regexAllowed(prev)) {
            var j = i + 1;
            var buf = std.ArrayListUnmanaged(u8).empty;
            var in_class = false;
            var terminated = false;
            while (j < n) {
                const c = source[j];
                if (c == '\\') {
                    buf.append(alloc, c) catch return .{ .err = "" };
                    j += 1;
                    if (j < n) {
                        buf.append(alloc, source[j]) catch return .{ .err = "" };
                        j += 1;
                    }
                    continue;
                }
                if (c == '[') {
                    in_class = true;
                } else if (c == ']') {
                    in_class = false;
                } else if (c == '/' and !in_class) {
                    j += 1;
                    terminated = true;
                    break;
                } else if (c == '\n') {
                    const ln = fail.line(source, i);
                    const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unterminated regular expression", .{ln}) catch return .{ .err = "" };
                    return .{ .err = msg };
                }
                buf.append(alloc, c) catch return .{ .err = "" };
                j += 1;
            }
            if (!terminated) {
                const ln = fail.line(source, i);
                const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unterminated regular expression", .{ln}) catch return .{ .err = "" };
                return .{ .err = msg };
            }
            var flags = std.ArrayListUnmanaged(u8).empty;
            while (j < n and std.ascii.isAlphabetic(source[j])) {
                flags.append(alloc, source[j]) catch return .{ .err = "" };
                j += 1;
            }
            const pat = buf.items;
            tokens.append(alloc, .{
                .kind = .regex,
                .text = pat,
                .payload = .{ .regex = .{ .source = pat, .flags = flags.items } },
                .offset = i,
            }) catch return .{ .err = "" };
            i = j;
        } else if (std.mem.indexOfScalar(u8, "{ } ( ) [ ] ; , . : ? ! < > = + - * / % & | ^ ~ @ # `", ch) != null) {
            if (ch == '?' and i + 1 < n and source[i + 1] == '.' and
                (i + 2 >= n or !std.ascii.isDigit(source[i + 2])))
            {
                tokens.append(alloc, .{
                    .kind = .punct,
                    .text = "?.",
                    .payload = .none,
                    .offset = i,
                }) catch return .{ .err = "" };
                i += 2;
            } else {
                var matched = false;
                for (PUNCT) |spec| {
                    if (spec.len <= n - i and std.mem.eql(u8, source[i .. i + spec.len], spec.text)) {
                        tokens.append(alloc, .{
                            .kind = .punct,
                            .text = spec.text,
                            .payload = .none,
                            .offset = i,
                        }) catch return .{ .err = "" };
                        i += spec.len;
                        matched = true;
                        break;
                    }
                }
                if (!matched) {
                    const one = source[i..];
                    const slice = one[0..1];
                    tokens.append(alloc, .{
                        .kind = .punct,
                        .text = slice,
                        .payload = .none,
                        .offset = i,
                    }) catch return .{ .err = "" };
                    i += 1;
                }
            }
        } else {
            const ln = fail.line(source, i);
            const msg = std.fmt.allocPrint(alloc, "SyntaxError on line {d}: unexpected character {c}", .{ ln, ch }) catch return .{ .err = "" };
            return .{ .err = msg };
        }
        if (tokens.items.len > MAX_TOKENS) {
            const msg = std.fmt.allocPrint(alloc, "Too many tokens", .{}) catch return .{ .err = "" };
            return .{ .err = msg };
        }
    }
    return .{ .tokens = tokens.items };
}

fn parseNumberText(text: []const u8) f64 {
    var buf: [64]u8 = undefined;
    var t: []const u8 = text;
    if (text.len > 0 and text[0] == '.') {
        t = std.fmt.bufPrint(&buf, "0{s}", .{text}) catch text;
    } else if (text.len > 0 and text[text.len - 1] == '.') {
        t = text[0 .. text.len - 1];
    }
    return std.fmt.parseFloat(f64, t) catch std.math.nan(f64);
}
