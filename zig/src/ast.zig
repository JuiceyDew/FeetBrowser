//! AST node types, ported from `rust/src/ast.rs`.

const std = @import("std");

pub const LiteralVal = union(enum) {
    number: f64,
    str: []const u8,
    boolean: bool,
    null_val,
    undefined_val,
};

pub const ObjectPair = union(enum) {
    key: KeyPair,
    spread: *Node,

    pub const KeyPair = struct {
        name: []const u8,
        value: *Node,
    };
};

pub const FuncNode = struct {
    name: []const u8,
    params: []const []const u8,
    defaults: std.StringArrayHashMapUnmanaged(*Node),
    rest: ?[]const u8,
    body: []const *Node,
    body_expr: ?*Node,
    async_: bool,
    arrow: bool,
};

pub const ClassMethodNode = struct {
    name: []const u8,
    params: []const []const u8,
    defaults: std.StringArrayHashMapUnmanaged(*Node),
    rest: ?[]const u8,
    body: []const *Node,
    is_static: bool,
    accessor: ?[]const u8,
};

pub const ClassNode = struct {
    name: []const u8,
    superclass: ?*Node,
    methods: []const ClassMethodNode,
};

pub const DeclTarget = union(enum) {
    name: []const u8,
    pattern: *PatternNode,
};

pub const PatternNode = struct {
    kind: []const u8, // "array" or "object"
    parts: []const PatternPart,
    rest: ?DeclTarget,
};

pub const PatternPart = union(enum) {
    array: ArrayPart,
    object: ObjectPart,

    pub const ArrayPart = struct {
        target: DeclTarget,
        default: ?*Node,
    };

    pub const ObjectPart = struct {
        key: []const u8,
        target: DeclTarget,
        default: ?*Node,
    };
};

pub const SwitchCase = struct {
    label: []const u8, // "case" or "default"
    test_expr: ?*Node,
    body: []const *Node,
};

pub const VarDeclItem = struct {
    target: DeclTarget,
    value: ?*Node,
};

pub const Node = union(enum) {
    // Expressions
    literal: LiteralVal,
    identifier: []const u8,
    this_expr,
    array_lit: []const *Node,
    object_lit: []ObjectPair,
    unary: Unary,
    update: Update,
    binary: Binary,
    logical: Logical,
    conditional: Conditional,
    assign: Assign,
    call: Call,
    new_expr: New,
    member: Member,
    index: Index,
    function_expr: FuncNode,
    spread: *Node,
    pattern: PatternNode,
    template_literal: TemplateLiteral,
    arrow_func: FuncNode,
    class_expr: ClassNode,
    class_method: ClassMethodNode,
    super_expr,
    await_expr: *Node,
    regex: Regex,

    // Statements
    program: []const *Node,
    block: []const *Node,
    var_decl: VarDecl,
    function_decl: FuncNode,
    class_decl: ClassNode,
    expr_stmt: *Node,
    if_stmt: If,
    while_stmt: While,
    do_while: DoWhile,
    switch_stmt: Switch,
    for_stmt: For,
    for_in: ForIn,
    for_of: ForOf,
    return_stmt: ?*Node,
    break_stmt,
    continue_stmt,
    throw_stmt: *Node,
    try_catch: TryCatch,

    pub const Unary = struct { op: []const u8, operand: *Node };
    pub const Update = struct { op: []const u8, operand: *Node, prefix: bool };
    pub const Binary = struct { op: []const u8, left: *Node, right: *Node };
    pub const Logical = struct { op: []const u8, left: *Node, right: *Node };
    pub const Conditional = struct { cond: *Node, then_expr: *Node, else_expr: *Node };
    pub const Assign = struct { op: []const u8, target: *Node, value: *Node };
    pub const Call = struct { callee: *Node, args: []const *Node, optional: bool };
    pub const New = struct { callee: *Node, args: []const *Node };
    pub const Member = struct { obj: *Node, name: []const u8, optional: bool };
    pub const Index = struct { obj: *Node, index: *Node, optional: bool };
    pub const TemplateLiteral = struct { quasis: []const []const u8, exprs: []const *Node };
    pub const Regex = struct { source: []const u8, flags: []const u8 };

    pub const VarDecl = struct { kind: []const u8, decls: []const VarDeclItem };
    pub const If = struct { cond: *Node, then_expr: *Node, else_expr: ?*Node };
    pub const While = struct { cond: *Node, body: *Node };
    pub const DoWhile = struct { body: *Node, cond: *Node };
    pub const Switch = struct { expr: *Node, cases: []const SwitchCase };
    pub const For = struct { init: ?*Node, cond: ?*Node, update: ?*Node, body: *Node };
    pub const ForIn = struct { var_kind: ?[]const u8, name: []const u8, iterable: *Node, body: *Node };
    pub const ForOf = struct { var_kind: ?[]const u8, name: []const u8, iterable: *Node, body: *Node };
    pub const TryCatch = struct {
        try_block: *Node,
        catch_param: ?[]const u8,
        catch_block: ?*Node,
        finally_block: ?*Node,
    };
};
