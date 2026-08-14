//! Recursive-descent parser ported from `rust/src/parser.rs`.

const std = @import("std");
const ast = @import("ast.zig");
const token = @import("token.zig");
const value = @import("value.zig");

const Node = ast.Node;
const Token = token.Token;
const TokKind = token.TokKind;
const TokPayload = token.TokPayload;

pub const Parser = struct {
    alloc: std.mem.Allocator,
    source: []const u8,
    tokens: []Token,
    pos: usize,
    async_depth: usize,
    err_msg: []const u8 = "",

    pub fn init(alloc: std.mem.Allocator, source: []const u8) Parser {
        const r = token.tokenize(alloc, source);
        return switch (r) {
            .tokens => |toks| .{ .alloc = alloc, .source = source, .tokens = toks, .pos = 0, .async_depth = 0 },
            .err => |msg| .{ .alloc = alloc, .source = source, .tokens = &.{}, .pos = 0, .async_depth = 0, .err_msg = msg },
        };
    }

    fn mk(self: *Parser, n: Node) ParseError!*Node {
        const p = try self.alloc.create(Node);
        p.* = n;
        return p;
    }

    fn fail(self: *Parser, comptime fmt: []const u8, fargs: anytype) ParseError {
        self.err_msg = std.fmt.allocPrint(self.alloc, fmt, fargs) catch "";
        return error.Syntax;
    }

    fn syntaxAt(self: *Parser, offset: usize, comptime fmt: []const u8, fargs: anytype) ParseError {
        var line: usize = 1;
        for (self.source[0..offset]) |c| {
            if (c == '\n') line += 1;
        }
        const detail = std.fmt.allocPrint(self.alloc, fmt, fargs) catch "";
        self.err_msg = std.fmt.allocPrint(self.alloc, "SyntaxError on line {d}: {s}", .{ line, detail }) catch "";
        return error.Syntax;
    }

    fn syntax(self: *Parser, msg: []const u8) ParseError {
        const offset = if (self.peek()) |t| t.offset else self.source.len;
        return self.syntaxAt(offset, "{s}", .{msg});
    }

    fn peek(self: *Parser) ?*const Token {
        if (self.pos < self.tokens.len) return &self.tokens[self.pos];
        return null;
    }

    fn peek2(self: *Parser) ?*const Token {
        if (self.pos + 1 < self.tokens.len) return &self.tokens[self.pos + 1];
        return null;
    }

    fn peekN(self: *Parser, n: usize) ?*const Token {
        if (self.pos + n < self.tokens.len) return &self.tokens[self.pos + n];
        return null;
    }

    fn isPunctAt(_: *Parser, t: ?*const Token, text: []const u8) bool {
        const tt = t orelse return false;
        return tt.kind == .punct and std.mem.eql(u8, tt.text, text);
    }

    fn peek2IsPunct(self: *Parser, text: []const u8) bool {
        return self.isPunctAt(self.peek2(), text);
    }

    fn peek3IsArrow(self: *Parser) bool {
        return self.isPunctAt(self.peekN(3), "=>");
    }

    fn peekIsPunct(self: *Parser, text: []const u8) bool {
        return self.isPunctAt(self.peek(), text);
    }

    fn matchPunct(self: *Parser, text: []const u8) bool {
        if (self.peekIsPunct(text)) {
            self.pos += 1;
            return true;
        }
        return false;
    }

    fn matchKw(self: *Parser, text: []const u8) bool {
        if (self.peek()) |t| {
            if (t.kind == .kw and std.mem.eql(u8, t.text, text)) {
                self.pos += 1;
                return true;
            }
        }
        return false;
    }

    fn expectPunct(self: *Parser, text: []const u8) ParseError!void {
        if (!self.matchPunct(text)) {
            return self.fail("expected '{s}'", .{text});
        }
    }

    fn matchIdent(self: *Parser) ?[]const u8 {
        if (self.peek()) |t| {
            if (t.kind == .ident) {
                const txt = t.text;
                self.pos += 1;
                return txt;
            }
        }
        return null;
    }

    fn expectIdent(self: *Parser) ParseError![]const u8 {
        return self.matchIdent() orelse self.syntax("expected identifier");
    }

    fn expectPropertyName(self: *Parser) ParseError![]const u8 {
        if (self.peek()) |t| {
            if (t.kind == .ident or t.kind == .kw) {
                const txt = t.text;
                self.pos += 1;
                return txt;
            }
        }
        return self.syntax("expected property name");
    }

    fn nextIsKw(self: *Parser, text: []const u8) bool {
        if (self.peek2()) |t| {
            return t.kind == .kw and std.mem.eql(u8, t.text, text);
        }
        return false;
    }

    // -- grammar ------------------------------------------------------------

    pub fn parseProgram(self: *Parser) ParseError!*Node {
        if (self.err_msg.len > 0) return error.Syntax;
        const stmts = try self.parseStmtsUntil(null);
        return self.mk(.{ .program = stmts });
    }

    pub fn parseExpression(self: *Parser) ParseError!*Node {
        if (self.err_msg.len > 0) return error.Syntax;
        return self.expression();
    }

    fn statement(self: *Parser) ParseError!*Node {
        if (self.peekIsPunct("{")) {
            const body = try self.parseStmtsUntil("}");
            return self.mk(.{ .block = body });
        }
        if (self.peek()) |t| {
            if (t.kind == .kw) {
                const text = t.text;
                if (try self.stmtForKeyword(text)) |n| return n;
            } else if (t.kind == .ident and std.mem.eql(u8, t.text, "async") and self.nextIsKw("function")) {
                self.pos += 2;
                const name = try self.expectIdent();
                var f = try self.functionRest(true);
                f.name = name;
                return self.mk(.{ .function_decl = f });
            }
        }
        const expr = try self.expression();
        return self.mk(.{ .expr_stmt = expr });
    }

    fn stmtForKeyword(self: *Parser, text: []const u8) ParseError!?*Node {
        if (std.mem.eql(u8, text, "var") or std.mem.eql(u8, text, "let") or std.mem.eql(u8, text, "const")) {
            self.pos += 1;
            const decls = try self.declarationList();
            return try self.mk(.{ .var_decl = .{ .kind = text, .decls = decls } });
        } else if (std.mem.eql(u8, text, "function")) {
            self.pos += 1;
            const f = try self.functionDeclaration(false);
            return try self.mk(.{ .function_decl = f });
        } else if (std.mem.eql(u8, text, "class")) {
            self.pos += 1;
            const c = try self.classDeclaration();
            return try self.mk(.{ .class_decl = c });
        } else if (std.mem.eql(u8, text, "return")) {
            self.pos += 1;
            const rv = try self.returnValue();
            return try self.mk(.{ .return_stmt = rv });
        } else if (std.mem.eql(u8, text, "if")) {
            self.pos += 1;
            return try self.ifStatement();
        } else if (std.mem.eql(u8, text, "while")) {
            self.pos += 1;
            return try self.whileStatement();
        } else if (std.mem.eql(u8, text, "do")) {
            self.pos += 1;
            return try self.doWhileStatement();
        } else if (std.mem.eql(u8, text, "switch")) {
            self.pos += 1;
            return try self.switchStatement();
        } else if (std.mem.eql(u8, text, "for")) {
            self.pos += 1;
            return try self.forStatement();
        } else if (std.mem.eql(u8, text, "break")) {
            self.pos += 1;
            return try self.mk(.break_stmt);
        } else if (std.mem.eql(u8, text, "continue")) {
            self.pos += 1;
            return try self.mk(.continue_stmt);
        } else if (std.mem.eql(u8, text, "throw")) {
            self.pos += 1;
            const expr = try self.expression();
            return try self.mk(.{ .throw_stmt = expr });
        } else if (std.mem.eql(u8, text, "try")) {
            self.pos += 1;
            return try self.tryStatement();
        }
        return null;
    }

    fn returnValue(self: *Parser) ParseError!?*Node {
        if (self.peek()) |t| {
            if (!(t.kind == .punct and (std.mem.eql(u8, t.text, ";") or std.mem.eql(u8, t.text, "}")))) {
                return try self.expression();
            }
        }
        return null;
    }

    fn parseStmtsUntil(self: *Parser, closing: ?[]const u8) ParseError![]*Node {
        if (closing) |c| {
            try self.expectPunct("{");
            _ = c;
        }
        var stmts = std.ArrayListUnmanaged(*Node).empty;
        while (true) {
            if (self.peek() == null) {
                if (closing != null) {
                    return self.fail("expected '{s}'", .{closing.?});
                }
                break;
            }
            if (closing) |c| {
                if (self.peek()) |t| {
                    if (t.kind == .punct and std.mem.eql(u8, t.text, c)) {
                        self.pos += 1;
                        break;
                    }
                }
            }
            if (self.matchPunct(";")) continue;
            try stmts.append(self.alloc, try self.statement());
            _ = self.matchPunct(";");
        }
        return stmts.items;
    }

    fn declarationList(self: *Parser) ParseError![]ast.VarDeclItem {
        var decls = std.ArrayListUnmanaged(ast.VarDeclItem).empty;
        while (true) {
            const target = try self.declarationTarget();
            const val = if (self.matchPunct("=")) try self.expression() else null;
            try decls.append(self.alloc, .{ .target = target, .value = val });
            if (!self.matchPunct(",")) break;
        }
        return decls.items;
    }

    fn declarationTarget(self: *Parser) ParseError!ast.DeclTarget {
        if (self.peek()) |t| {
            if (t.kind == .ident) {
                const name = t.text;
                self.pos += 1;
                return .{ .name = name };
            }
            if (t.kind == .punct and (std.mem.eql(u8, t.text, "[") or std.mem.eql(u8, t.text, "{"))) {
                return self.pattern();
            }
        }
        return self.syntax("expected identifier");
    }

    fn pattern(self: *Parser) ParseError!ast.DeclTarget {
        if (self.peekIsPunct("[")) {
            self.pos += 1;
            var parts = std.ArrayListUnmanaged(ast.PatternPart).empty;
            var rest: ?ast.DeclTarget = null;
            while (true) {
                if (self.matchPunct("]")) break;
                if (self.matchPunct(",")) continue;
                if (self.matchPunct("...")) {
                    rest = try self.patternTarget();
                    _ = self.matchPunct(",");
                    try self.expectPunct("]");
                    break;
                }
                const target = try self.patternTarget();
                const default = if (self.matchPunct("=")) try self.assign() else null;
                try parts.append(self.alloc, .{ .array = .{ .target = target, .default = default } });
                if (!self.matchPunct(",")) {
                    try self.expectPunct("]");
                    break;
                }
            }
            const pn = try self.alloc.create(ast.PatternNode);
            pn.* = .{ .kind = "array", .parts = parts.items, .rest = rest };
            return .{ .pattern = pn };
        }
        try self.expectPunct("{");
        var parts = std.ArrayListUnmanaged(ast.PatternPart).empty;
        var rest: ?ast.DeclTarget = null;
        while (true) {
            if (self.matchPunct("}")) break;
            if (self.matchPunct(",")) continue;
            if (self.matchPunct("...")) {
                rest = try self.patternTarget();
                _ = self.matchPunct(",");
                try self.expectPunct("}");
                break;
            }
            const key = blk: {
                if (self.peek()) |t| {
                    if (t.kind == .ident or t.kind == .str or t.kind == .kw) {
                        const k = t.text;
                        self.pos += 1;
                        break :blk .{ .k = k, .is_ident = t.kind == .ident };
                    }
                }
                return self.syntax("expected property name");
            };
            var target: ast.DeclTarget = undefined;
            if (self.matchPunct(":")) {
                target = try self.patternTarget();
            } else {
                if (!key.is_ident) return self.syntax("expected ':' in destructuring");
                target = .{ .name = key.k };
            }
            const default = if (self.matchPunct("=")) try self.assign() else null;
            try parts.append(self.alloc, .{ .object = .{ .key = key.k, .target = target, .default = default } });
            if (!self.matchPunct(",")) {
                try self.expectPunct("}");
                break;
            }
        }
        const pn = try self.alloc.create(ast.PatternNode);
        pn.* = .{ .kind = "object", .parts = parts.items, .rest = rest };
        return .{ .pattern = pn };
    }

    fn patternTarget(self: *Parser) ParseError!ast.DeclTarget {
        if (self.peek()) |t| {
            if (t.kind == .ident) {
                const name = t.text;
                self.pos += 1;
                return .{ .name = name };
            }
            if (t.kind == .punct and (std.mem.eql(u8, t.text, "[") or std.mem.eql(u8, t.text, "{"))) {
                return self.pattern();
            }
        }
        return self.syntax("expected identifier in destructuring");
    }

    fn functionDeclaration(self: *Parser, async_: bool) ParseError!ast.FuncNode {
        const name = try self.expectIdent();
        var f = try self.functionRest(async_);
        f.name = name;
        return f;
    }

    fn functionRest(self: *Parser, async_: bool) ParseError!ast.FuncNode {
        const params = try self.paramList();
        if (async_) self.async_depth += 1;
        const body = self.parseStmtsUntil("}");
        if (async_) self.async_depth -= 1;
        const b = try body;
        return .{
            .name = "",
            .params = params.names,
            .defaults = params.defaults,
            .rest = params.rest,
            .body = b,
            .body_expr = null,
            .async_ = async_,
            .arrow = false,
        };
    }

    const ParamList = struct {
        names: []const []const u8,
        defaults: std.StringArrayHashMapUnmanaged(*Node),
        rest: ?[]const u8,
    };

    fn paramList(self: *Parser) ParseError!ParamList {
        try self.expectPunct("(");
        var names = std.ArrayListUnmanaged([]const u8).empty;
        var defaults = std.StringArrayHashMapUnmanaged(*Node){};
        var rest: ?[]const u8 = null;
        while (true) {
            if (self.matchPunct(")")) break;
            const is_rest = self.matchPunct("...");
            const name = try self.expectIdent();
            if (is_rest) {
                rest = name;
            } else {
                try names.append(self.alloc, name);
                if (self.matchPunct("=")) {
                    const d = try self.assign();
                    try defaults.put(self.alloc, name, d);
                }
            }
            if (self.matchPunct(")")) break;
            try self.expectPunct(",");
        }
        return .{ .names = names.items, .defaults = defaults, .rest = rest };
    }

    fn arrowRest(self: *Parser, names: []const []const u8, defaults: std.StringArrayHashMapUnmanaged(*Node), rest: ?[]const u8, async_: bool) ParseError!*Node {
        try self.expectPunct("=>");
        var f = ast.FuncNode{
            .name = "",
            .params = names,
            .defaults = defaults,
            .rest = rest,
            .body = &.{},
            .body_expr = null,
            .async_ = async_,
            .arrow = true,
        };
        if (self.peekIsPunct("{")) {
            f.body = try self.parseStmtsUntil("}");
            return self.mk(.{ .arrow_func = f });
        }
        if (async_) self.async_depth += 1;
        const expr = self.assign();
        if (async_) self.async_depth -= 1;
        f.body_expr = try expr;
        return self.mk(.{ .arrow_func = f });
    }

    fn parenFollowedByArrow(self: *Parser) bool {
        var depth: i64 = 0;
        var i = self.pos;
        while (i < self.tokens.len) {
            const t = &self.tokens[i];
            if (t.kind == .punct) {
                if (std.mem.eql(u8, t.text, "(") or std.mem.eql(u8, t.text, "[") or std.mem.eql(u8, t.text, "{")) {
                    depth += 1;
                } else if (std.mem.eql(u8, t.text, ")") or std.mem.eql(u8, t.text, "]") or std.mem.eql(u8, t.text, "}")) {
                    depth -= 1;
                    if (depth == 0 and std.mem.eql(u8, t.text, ")")) {
                        if (i + 1 < self.tokens.len) {
                            const nt = &self.tokens[i + 1];
                            return nt.kind == .punct and std.mem.eql(u8, nt.text, "=>");
                        }
                        return false;
                    }
                }
            }
            i += 1;
        }
        return false;
    }

    fn ifStatement(self: *Parser) ParseError!*Node {
        const cb = try self.condBody();
        const else_ = if (self.matchKw("else")) try self.statement() else null;
        return self.mk(.{ .if_stmt = .{ .cond = cb.cond, .then_expr = cb.body, .else_expr = else_ } });
    }

    fn whileStatement(self: *Parser) ParseError!*Node {
        const cb = try self.condBody();
        return self.mk(.{ .while_stmt = .{ .cond = cb.cond, .body = cb.body } });
    }

    fn doWhileStatement(self: *Parser) ParseError!*Node {
        const body = try self.statement();
        _ = self.matchPunct(";");
        _ = self.matchKw("while");
        try self.expectPunct("(");
        const cond = try self.expression();
        try self.expectPunct(")");
        _ = self.matchPunct(";");
        return self.mk(.{ .do_while = .{ .body = body, .cond = cond } });
    }

    fn switchStatement(self: *Parser) ParseError!*Node {
        try self.expectPunct("(");
        const expr = try self.expression();
        try self.expectPunct(")");
        try self.expectPunct("{");
        var cases = std.ArrayListUnmanaged(ast.SwitchCase).empty;
        while (true) {
            if (self.peek()) |t| {
                if (t.kind == .punct and std.mem.eql(u8, t.text, "}")) {
                    self.pos += 1;
                    break;
                }
            } else return self.syntax("expected '}'");
            if (self.matchKw("case")) {
                const test_expr = try self.expression();
                try self.expectPunct(":");
                const body = try self.caseBody();
                try cases.append(self.alloc, .{ .label = "case", .test_expr = test_expr, .body = body });
            } else if (self.matchKw("default")) {
                try self.expectPunct(":");
                const body = try self.caseBody();
                try cases.append(self.alloc, .{ .label = "default", .test_expr = null, .body = body });
            } else {
                return self.syntax("expected 'case' or 'default'");
            }
        }
        return self.mk(.{ .switch_stmt = .{ .expr = expr, .cases = cases.items } });
    }

    fn caseBody(self: *Parser) ParseError![]*Node {
        var stmts = std.ArrayListUnmanaged(*Node).empty;
        while (true) {
            if (self.peek()) |t| {
                if (t.kind == .punct and std.mem.eql(u8, t.text, "}")) break;
                if (t.kind == .kw and (std.mem.eql(u8, t.text, "case") or std.mem.eql(u8, t.text, "default"))) break;
            } else return self.syntax("expected '}'");
            if (self.matchPunct(";")) continue;
            try stmts.append(self.alloc, try self.statement());
        }
        return stmts.items;
    }

    const CondBody = struct { cond: *Node, body: *Node };

    fn condBody(self: *Parser) ParseError!CondBody {
        try self.expectPunct("(");
        const cond = try self.expression();
        try self.expectPunct(")");
        const body = try self.statement();
        return .{ .cond = cond, .body = body };
    }

    fn forStatement(self: *Parser) ParseError!*Node {
        try self.expectPunct("(");
        const head_kw = blk: {
            if (self.peek()) |t| {
                if (t.kind == .kw and
                    (std.mem.eql(u8, t.text, "var") or std.mem.eql(u8, t.text, "let") or std.mem.eql(u8, t.text, "const")))
                {
                    break :blk t.text;
                }
            }
            break :blk null;
        };
        if (head_kw) |kind| {
            const save = self.pos;
            self.pos += 1;
            if (self.matchIdent()) |name| {
                if (self.peek()) |t2| {
                    if (t2.kind == .kw and (std.mem.eql(u8, t2.text, "in") or std.mem.eql(u8, t2.text, "of"))) {
                        const op = t2.text;
                        self.pos += 1;
                        const iterable = try self.expression();
                        try self.expectPunct(")");
                        const body = try self.statement();
                        if (std.mem.eql(u8, op, "in")) {
                            return self.mk(.{ .for_in = .{ .var_kind = kind, .name = name, .iterable = iterable, .body = body } });
                        }
                        return self.mk(.{ .for_of = .{ .var_kind = kind, .name = name, .iterable = iterable, .body = body } });
                    }
                }
            }
            self.pos = save;
        } else {
            const save = self.pos;
            if (self.matchIdent()) |name| {
                if (self.peek()) |t2| {
                    if (t2.kind == .kw and (std.mem.eql(u8, t2.text, "in") or std.mem.eql(u8, t2.text, "of"))) {
                        const op = t2.text;
                        self.pos += 1;
                        const iterable = try self.expression();
                        try self.expectPunct(")");
                        const body = try self.statement();
                        if (std.mem.eql(u8, op, "in")) {
                            return self.mk(.{ .for_in = .{ .var_kind = null, .name = name, .iterable = iterable, .body = body } });
                        }
                        return self.mk(.{ .for_of = .{ .var_kind = null, .name = name, .iterable = iterable, .body = body } });
                    }
                }
                self.pos = save;
            }
        }
        const init_node = if (self.peekIsPunct(";"))
            null
        else if (self.peek()) |t| blk: {
            if (t.kind == .kw and (std.mem.eql(u8, t.text, "var") or std.mem.eql(u8, t.text, "let") or std.mem.eql(u8, t.text, "const"))) {
                const kind = t.text;
                self.pos += 1;
                const decls = try self.declarationList();
                break :blk try self.mk(.{ .var_decl = .{ .kind = kind, .decls = decls } });
            } else {
                const expr = try self.expression();
                break :blk try self.mk(.{ .expr_stmt = expr });
            }
        } else null;
        try self.expectPunct(";");
        const cond = if (self.peekIsPunct(";")) null else try self.expression();
        try self.expectPunct(";");
        const update = if (self.peekIsPunct(")")) null else try self.expression();
        try self.expectPunct(")");
        const body = try self.statement();
        return self.mk(.{ .for_stmt = .{ .init = init_node, .cond = cond, .update = update, .body = body } });
    }

    fn tryStatement(self: *Parser) ParseError!*Node {
        const try_body = try self.parseStmtsUntil("}");
        var catch_param: ?[]const u8 = null;
        var catch_block: ?*Node = null;
        if (self.matchKw("catch")) {
            try self.expectPunct("(");
            catch_param = try self.expectIdent();
            try self.expectPunct(")");
            const cb = try self.parseStmtsUntil("}");
            catch_block = try self.mk(.{ .block = cb });
        }
        const finally_block = if (self.matchKw("finally"))
            blk: {
                const fb = try self.parseStmtsUntil("}");
                break :blk try self.mk(.{ .block = fb });
            }
        else
            null;
        return self.mk(.{ .try_catch = .{
            .try_block = try self.mk(.{ .block = try_body }),
            .catch_param = catch_param,
            .catch_block = catch_block,
            .finally_block = finally_block,
        } });
    }

    // -- expressions --------------------------------------------------------

    fn expression(self: *Parser) ParseError!*Node {
        return self.assign();
    }

    const AssignOps = [_][]const u8{ "=", "+=", "-=", "*=", "/=", "%=", "**=", "&=", "|=", "^=", "<<=", ">>=", ">>>=", "&&=", "||=", "??=" };

    fn isAssignOp(text: []const u8) bool {
        for (AssignOps) |op| {
            if (std.mem.eql(u8, op, text)) return true;
        }
        return false;
    }

    fn assign(self: *Parser) ParseError!*Node {
        const left = try self.conditional();
        if (self.peek()) |t| {
            if (t.kind == .punct and isAssignOp(t.text)) {
                const op = t.text;
                self.pos += 1;
                const right = try self.assign();
                switch (left.*) {
                    .identifier, .member, .index => {},
                    else => return self.syntax("invalid assignment target"),
                }
                return self.mk(.{ .assign = .{ .op = op, .target = left, .value = right } });
            }
        }
        return left;
    }

    fn conditional(self: *Parser) ParseError!*Node {
        const cond = try self.or_();
        if (self.matchPunct("?")) {
            const then_expr = try self.assign();
            try self.expectPunct(":");
            const else_expr = try self.assign();
            return self.mk(.{ .conditional = .{ .cond = cond, .then_expr = then_expr, .else_expr = else_expr } });
        }
        return cond;
    }

    fn or_(self: *Parser) ParseError!*Node {
        var node = try self.and_();
        while (true) {
            if (self.matchPunct("||")) {
                const right = try self.and_();
                node = try self.mk(.{ .logical = .{ .op = "||", .left = node, .right = right } });
            } else if (self.matchPunct("??")) {
                const right = try self.and_();
                node = try self.mk(.{ .logical = .{ .op = "??", .left = node, .right = right } });
            } else break;
        }
        return node;
    }

    fn and_(self: *Parser) ParseError!*Node {
        return self.logicalChain("&&", Parser.bitwiseOr);
    }

    fn logicalChain(self: *Parser, op: []const u8, comptime sub: fn (*Parser) ParseError!*Node) ParseError!*Node {
        var node = try sub(self);
        while (self.matchPunct(op)) {
            const right = try sub(self);
            node = try self.mk(.{ .logical = .{ .op = op, .left = node, .right = right } });
        }
        return node;
    }

    fn bitwiseOr(self: *Parser) ParseError!*Node {
        return self.binop("|", Parser.bitwiseXor);
    }

    fn bitwiseXor(self: *Parser) ParseError!*Node {
        return self.binop("^", Parser.bitwiseAnd);
    }

    fn bitwiseAnd(self: *Parser) ParseError!*Node {
        return self.binop("&", Parser.equality);
    }

    fn equality(self: *Parser) ParseError!*Node {
        return self.binopMulti(&.{ "==", "!=", "===", "!==" }, Parser.relational);
    }

    fn relational(self: *Parser) ParseError!*Node {
        return self.binopMulti(&.{ "<", "<=", ">", ">=", "in", "instanceof" }, Parser.shift);
    }

    fn shift(self: *Parser) ParseError!*Node {
        return self.binopMulti(&.{ "<<", ">>", ">>>" }, Parser.additive);
    }

    fn additive(self: *Parser) ParseError!*Node {
        return self.binopMulti(&.{ "+", "-" }, Parser.multiplicative);
    }

    fn multiplicative(self: *Parser) ParseError!*Node {
        return self.binopMulti(&.{ "*", "/", "%" }, Parser.exponent);
    }

    fn exponent(self: *Parser) ParseError!*Node {
        const node = try self.unary();
        if (self.matchPunct("**")) {
            const right = try self.exponent();
            return self.mk(.{ .binary = .{ .op = "**", .left = node, .right = right } });
        }
        return node;
    }

    fn binop(self: *Parser, op: []const u8, comptime sub: fn (*Parser) ParseError!*Node) ParseError!*Node {
        var node = try sub(self);
        while (self.matchPunct(op)) {
            const right = try sub(self);
            node = try self.mk(.{ .binary = .{ .op = op, .left = node, .right = right } });
        }
        return node;
    }

    fn binopMulti(self: *Parser, ops: []const []const u8, comptime sub: fn (*Parser) ParseError!*Node) ParseError!*Node {
        var node = try sub(self);
        while (true) {
            const v = blk: {
                if (self.peek()) |t| {
                    if (t.kind == .punct or t.kind == .kw) {
                        for (ops) |op| {
                            if (std.mem.eql(u8, t.text, op)) {
                                const txt = t.text;
                                self.pos += 1;
                                break :blk txt;
                            }
                        }
                    }
                }
                break :blk null;
            };
            const val = v orelse break;
            const right = try sub(self);
            node = try self.mk(.{ .binary = .{ .op = val, .left = node, .right = right } });
        }
        return node;
    }

    fn unary(self: *Parser) ParseError!*Node {
        if (self.peek()) |t| {
            if (t.kind == .punct and
                (std.mem.eql(u8, t.text, "!") or std.mem.eql(u8, t.text, "-") or std.mem.eql(u8, t.text, "+") or
                    std.mem.eql(u8, t.text, "~") or std.mem.eql(u8, t.text, "++") or std.mem.eql(u8, t.text, "--")))
            {
                const op = t.text;
                self.pos += 1;
                if (std.mem.eql(u8, op, "++") or std.mem.eql(u8, op, "--")) {
                    const operand = try self.unary();
                    return self.mk(.{ .update = .{ .op = op, .operand = operand, .prefix = true } });
                }
                const operand = try self.unary();
                return self.mk(.{ .unary = .{ .op = op, .operand = operand } });
            }
            if (t.kind == .kw and
                (std.mem.eql(u8, t.text, "typeof") or std.mem.eql(u8, t.text, "delete") or std.mem.eql(u8, t.text, "void")))
            {
                const op = t.text;
                self.pos += 1;
                const operand = try self.unary();
                return self.mk(.{ .unary = .{ .op = op, .operand = operand } });
            }
            if (t.kind == .kw and std.mem.eql(u8, t.text, "await")) {
                if (self.async_depth == 0) return self.syntax("await is only valid in async functions");
                self.pos += 1;
                const operand = try self.unary();
                return self.mk(.{ .await_expr = operand });
            }
        }
        return self.call();
    }

    fn call(self: *Parser) ParseError!*Node {
        var node = try self.primary();
        while (true) {
            if (self.matchPunct("(")) {
                const args = try self.callArgs();
                node = try self.mk(.{ .call = .{ .callee = node, .args = args, .optional = false } });
            } else if (self.matchPunct(".")) {
                const name = try self.expectPropertyName();
                node = try self.mk(.{ .member = .{ .obj = node, .name = name, .optional = false } });
            } else if (self.matchPunct("?.")) {
                if (self.peekIsPunct("(")) {
                    const args = try self.callArgs();
                    node = try self.mk(.{ .call = .{ .callee = node, .args = args, .optional = true } });
                } else if (self.peekIsPunct("[")) {
                    self.pos += 1;
                    const index = try self.expression();
                    try self.expectPunct("]");
                    node = try self.mk(.{ .index = .{ .obj = node, .index = index, .optional = true } });
                } else {
                    const name = try self.expectPropertyName();
                    node = try self.mk(.{ .member = .{ .obj = node, .name = name, .optional = true } });
                }
            } else if (self.matchPunct("[")) {
                const index = try self.expression();
                try self.expectPunct("]");
                node = try self.mk(.{ .index = .{ .obj = node, .index = index, .optional = false } });
            } else if (self.matchPunct("++")) {
                node = try self.mk(.{ .update = .{ .op = "++", .operand = node, .prefix = false } });
            } else if (self.matchPunct("--")) {
                node = try self.mk(.{ .update = .{ .op = "--", .operand = node, .prefix = false } });
            } else break;
        }
        return node;
    }

    fn callArgs(self: *Parser) ParseError![]*Node {
        var out = std.ArrayListUnmanaged(*Node).empty;
        while (true) {
            if (self.matchPunct(")")) break;
            if (self.matchPunct("...")) {
                const expr = try self.expression();
                try out.append(self.alloc, try self.mk(.{ .spread = expr }));
            } else {
                try out.append(self.alloc, try self.expression());
            }
            if (self.matchPunct(")")) break;
            try self.expectPunct(",");
        }
        return out.items;
    }

    fn arrayItem(self: *Parser) ParseError!*Node {
        if (self.matchPunct("...")) {
            const expr = try self.expression();
            return self.mk(.{ .spread = expr });
        }
        return self.expression();
    }

    fn newExpression(self: *Parser) ParseError!*Node {
        const callee = try self.primary();
        const args = if (self.matchPunct("(")) try self.callArgs() else &.{};
        return self.mk(.{ .new_expr = .{ .callee = callee, .args = args } });
    }

    fn primary(self: *Parser) ParseError!*Node {
        if (self.peek()) |t| {
            switch (t.kind) {
                .number => {
                    const v = switch (t.payload) {
                        .number => |n| n,
                        else => std.math.nan(f64),
                    };
                    self.pos += 1;
                    return self.mk(.{ .literal = .{ .number = v } });
                },
                .str => {
                    const s = switch (t.payload) {
                        .str => |st| st,
                        else => "",
                    };
                    self.pos += 1;
                    return self.mk(.{ .literal = .{ .str = s } });
                },
                .regex => {
                    const rp = switch (t.payload) {
                        .regex => |r| r,
                        else => token.TokPayload.RegexPayload{ .source = "", .flags = "" },
                    };
                    self.pos += 1;
                    return self.mk(.{ .regex = .{ .source = rp.source, .flags = rp.flags } });
                },
                .template => {
                    const raw = switch (t.payload) {
                        .str => |s| s,
                        else => "",
                    };
                    self.pos += 1;
                    return self.templateLiteral(raw);
                },
                .kw => {
                    const v = t.text;
                    self.pos += 1;
                    if (std.mem.eql(u8, v, "true")) return self.mk(.{ .literal = .{ .boolean = true } });
                    if (std.mem.eql(u8, v, "false")) return self.mk(.{ .literal = .{ .boolean = false } });
                    if (std.mem.eql(u8, v, "null")) return self.mk(.{ .literal = .null_val });
                    if (std.mem.eql(u8, v, "undefined")) return self.mk(.{ .literal = .undefined_val });
                    if (std.mem.eql(u8, v, "function")) {
                        const f = try self.functionExpression(false);
                        return self.mk(.{ .function_expr = f });
                    }
                    if (std.mem.eql(u8, v, "this")) return self.mk(.this_expr);
                    if (std.mem.eql(u8, v, "new")) return self.newExpression();
                    if (std.mem.eql(u8, v, "class")) {
                        const c = try self.classExpression();
                        return self.mk(.{ .class_expr = c });
                    }
                    if (std.mem.eql(u8, v, "super")) return self.mk(.super_expr);
                    return self.fail("unexpected keyword '{s}'", .{v});
                },
                .ident => {
                    const v = t.text;
                    if (std.mem.eql(u8, v, "async") and self.nextIsKw("function")) {
                        self.pos += 2;
                        const f = try self.functionExpression(true);
                        return self.mk(.{ .function_expr = f });
                    }
                    if (std.mem.eql(u8, v, "async")) {
                        if (self.peek2()) |t2| {
                            if (t2.kind == .ident and self.peek3IsArrow()) {
                                self.pos += 1;
                                const name = self.matchIdent().?;
                                var tmp1 = [1][]const u8{name};
                                return self.arrowRest(&tmp1, .{}, null, true);
                            }
                        }
                        if (self.peek2IsPunct("(") and self.parenFollowedByArrow()) {
                            self.pos += 1;
                            const pl = try self.paramList();
                            return self.arrowRest(pl.names, pl.defaults, pl.rest, true);
                        }
                    }
                    if (self.peek2IsPunct("=>")) {
                        self.pos += 1;
                        var tmp2 = [1][]const u8{v};
                        return self.arrowRest(&tmp2, .{}, null, false);
                    }
                    self.pos += 1;
                    return self.mk(.{ .identifier = v });
                },
                .punct => {
                    const v = t.text;
                    if (std.mem.eql(u8, v, "(")) {
                        if (self.parenFollowedByArrow()) {
                            const pl = try self.paramList();
                            return self.arrowRest(pl.names, pl.defaults, pl.rest, false);
                        }
                        self.pos += 1;
                        var node = try self.expression();
                        while (self.matchPunct(",")) {
                            node = try self.expression();
                        }
                        try self.expectPunct(")");
                        return node;
                    }
                    if (std.mem.eql(u8, v, "[")) {
                        self.pos += 1;
                        const items = try self.arrayItems();
                        return self.mk(.{ .array_lit = items });
                    }
                    if (std.mem.eql(u8, v, "{")) {
                        self.pos += 1;
                        const pairs = try self.objectPairs();
                        return self.mk(.{ .object_lit = pairs });
                    }
                },
            }
        }
        return self.syntax("unexpected token");
    }

    fn arrayItems(self: *Parser) ParseError![]*Node {
        var out = std.ArrayListUnmanaged(*Node).empty;
        while (true) {
            if (self.matchPunct("]")) break;
            if (self.matchPunct(",")) continue;
            try out.append(self.alloc, try self.arrayItem());
            if (self.matchPunct("]")) break;
            try self.expectPunct(",");
        }
        return out.items;
    }

    fn objectPairs(self: *Parser) ParseError![]ast.ObjectPair {
        var out = std.ArrayListUnmanaged(ast.ObjectPair).empty;
        while (true) {
            if (self.matchPunct("}")) break;
            if (self.matchPunct(",")) continue;
            if (self.matchPunct("...")) {
                try out.append(self.alloc, .{ .spread = try self.expression() });
                if (self.matchPunct("}")) break;
                try self.expectPunct(",");
                continue;
            }
            const key = blk: {
                if (self.peek()) |t| {
                    if (t.kind == .ident or t.kind == .str or t.kind == .kw) {
                        const k = t.text;
                        self.pos += 1;
                        break :blk k;
                    }
                }
                return self.syntax("expected property name");
            };
            if (self.matchPunct(":")) {
                const val = try self.expression();
                try out.append(self.alloc, .{ .key = .{ .name = key, .value = val } });
            } else {
                try out.append(self.alloc, .{ .key = .{ .name = key, .value = try self.mk(.{ .identifier = key }) } });
            }
            if (self.matchPunct("}")) break;
            try self.expectPunct(",");
        }
        return out.items;
    }

    fn functionExpression(self: *Parser, async_: bool) ParseError!ast.FuncNode {
        var name: []const u8 = "";
        if (self.peek()) |t| {
            if (t.kind == .ident) {
                name = t.text;
                self.pos += 1;
            }
        }
        var f = try self.functionRest(async_);
        f.name = name;
        return f;
    }

    fn templateLiteral(self: *Parser, raw: []const u8) ParseError!*Node {
        var quasis = std.ArrayListUnmanaged([]const u8).empty;
        var exprs = std.ArrayListUnmanaged(*Node).empty;
        var parts = splitTemplate(self.alloc, raw);
        defer parts.deinit(self.alloc);
        for (parts.items) |part| {
            try quasis.append(self.alloc, part.quasi);
            if (part.expr) |src| {
                var sub = Parser.init(self.alloc, src);
                try exprs.append(self.alloc, try sub.parseExpression());
            }
        }
        return self.mk(.{ .template_literal = .{ .quasis = quasis.items, .exprs = exprs.items } });
    }

    fn classDeclaration(self: *Parser) ParseError!ast.ClassNode {
        const name = try self.expectIdent();
        var c = ast.ClassNode{ .name = name, .superclass = null, .methods = &.{} };
        if (self.matchKw("extends")) {
            c.superclass = try self.expression();
        }
        c.methods = try self.classBody();
        return c;
    }

    fn classExpression(self: *Parser) ParseError!ast.ClassNode {
        var c = ast.ClassNode{ .name = "", .superclass = null, .methods = &.{} };
        if (self.peek()) |t| {
            if (t.kind == .ident) {
                c.name = t.text;
                self.pos += 1;
            }
        }
        if (self.matchKw("extends")) {
            c.superclass = try self.expression();
        }
        c.methods = try self.classBody();
        return c;
    }

    fn classBody(self: *Parser) ParseError![]ast.ClassMethodNode {
        try self.expectPunct("{");
        var methods = std.ArrayListUnmanaged(ast.ClassMethodNode).empty;
        while (true) {
            if (self.matchPunct("}")) break;
            if (self.matchPunct(";")) continue;
            var is_static = false;
            var accessor: ?[]const u8 = null;
            if (self.peek()) |t| {
                if (t.kind == .kw and std.mem.eql(u8, t.text, "static")) {
                    if (!self.peek2IsPunct("(")) {
                        self.pos += 1;
                        is_static = true;
                    }
                }
            }
            if (self.peek()) |t| {
                if (t.kind == .ident and (std.mem.eql(u8, t.text, "get") or std.mem.eql(u8, t.text, "set"))) {
                    if (!self.peek2IsPunct("(") and !self.peek2IsPunct("=") and !self.peek2IsPunct(";") and
                        !self.peek2IsPunct("}") and !self.peek2IsPunct(","))
                    {
                        accessor = t.text;
                        self.pos += 1;
                    }
                }
            }
            const name = try self.expectPropertyName();
            if (!self.peekIsPunct("(")) return self.syntax("expected '(' in class method");
            const pl = try self.paramList();
            const body = try self.parseStmtsUntil("}");
            try methods.append(self.alloc, .{
                .name = name,
                .params = pl.names,
                .defaults = pl.defaults,
                .rest = pl.rest,
                .body = body,
                .is_static = is_static,
                .accessor = accessor,
            });
        }
        return methods.items;
    }
};

const JsErr = value.JsErr;
const ParseError = error{ Syntax, OutOfMemory };

const TemplatePart = struct { quasi: []const u8, expr: ?[]const u8 };

fn splitTemplate(alloc: std.mem.Allocator, raw: []const u8) std.ArrayListUnmanaged(TemplatePart) {
    var parts = std.ArrayListUnmanaged(TemplatePart).empty;
    var buf = std.ArrayListUnmanaged(u8).empty;
    var i: usize = 0;
    const n = raw.len;
    while (i < n) {
        const ch = raw[i];
        if (ch == '\\') {
            if (i + 1 < n) {
                buf.append(alloc, ch) catch return parts;
                buf.append(alloc, raw[i + 1]) catch return parts;
                i += 2;
                continue;
            }
        }
        if (ch == '$' and i + 1 < n and raw[i + 1] == '{') {
            var j = i + 2;
            var d: i64 = 1;
            var q: ?u8 = null;
            while (j < n) {
                const c = raw[j];
                if (q) |quote| {
                    if (c == '\\') {
                        j += 2;
                        continue;
                    }
                    if (c == quote) q = null;
                } else if (c == '\'' or c == '"' or c == '`') {
                    q = c;
                } else if (c == '{') {
                    d += 1;
                } else if (c == '}') {
                    d -= 1;
                    if (d == 0) break;
                }
                j += 1;
            }
            if (j >= n) {
                buf.append(alloc, ch) catch return parts;
                i += 1;
                continue;
            }
            const expr = raw[i + 2 .. j];
            parts.append(alloc, .{ .quasi = buf.items, .expr = expr }) catch return parts;
            buf = std.ArrayListUnmanaged(u8).empty;
            i = j + 1;
        } else {
            buf.append(alloc, ch) catch return parts;
            i += 1;
        }
    }
    parts.append(alloc, .{ .quasi = buf.items, .expr = null }) catch return parts;
    return parts;
}

pub fn parseProgram(alloc: std.mem.Allocator, source: []const u8) ParseError!*Node {
    var p = Parser.init(alloc, source);
    return p.parseProgram();
}
