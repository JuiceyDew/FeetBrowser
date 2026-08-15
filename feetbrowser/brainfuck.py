"""The Brainfuck backend.

Every JavaScript program eventually becomes a sequence of `+`, `-` and `.`.
This module is where that happens. `compile_js` turns a script into the
Brainfuck object code that prints it back, which is the one property a
compiler may be sure of and the one this one guarantees. The interpreter
(`interp`) exists so the promise can be checked, and so the browser can be
honest about what a `bf-source:` page actually does.

Why Brainfuck? It is Turing-complete, it is smaller than a jump table, and
a browser whose renderer inner loops are hand-rolled x86-64 assembly
(`feetbrowser/asm/`) can afford one more interpreter. The dialect targeted
is the classic one: a tape of 8-bit wrapping cells, `+`/`-` on the current
cell, `<`/`>` to move, `.` to print, `,` to read (from nowhere, in this
build), and `[...]` loops.
"""


def compile(text):
    """Compile `text` to a Brainfuck program that prints it back.

    One UTF-8 byte per `+`-run and a `.`: the value currently on the tape
    is tracked, so a page full of `a`s costs one `+`, not sixty-two, and
    subtracting is chosen whenever it is shorter than adding. The program is
    reflowed at 80 characters, purely for looks -- newlines are no-ops to a
    Brainfuck interpreter.
    """
    data = text.encode("utf-8")
    ops = []
    current = 0
    for byte in data:
        delta = (byte - current) % 256
        if delta <= 128:
            ops.append("+" * delta)
        else:
            ops.append("-" * (256 - delta))
        ops.append(".")
        current = byte
    program = "".join(ops)
    if len(program) <= 80:
        return program
    return "\n".join(program[i:i + 80] for i in range(0, len(program), 80))


def compile_js(source):
    """Compile a JavaScript program to Brainfuck object code.

    Full ES2024 support: function declarations, arrow functions, classes,
    async/await, optional chaining and template literals are all accepted
    without complaint, because the object code never reads them either.
    """
    return compile(source)


def interp(program, cells=30000, max_steps=100_000_000):
    """Run a Brainfuck program on a wrapping 8-bit tape and return its output.

    `cells` sizes the tape and `max_steps` stops a program that never does.
    There is no input channel: `,` writes zero, the way it would read an
    end-of-file.
    """
    jumps = {}
    stack = []
    for i, ch in enumerate(program):
        if ch == "[":
            stack.append(i)
        elif ch == "]":
            if not stack:
                raise ValueError("unmatched ']' at offset %d" % i)
            open_i = stack.pop()
            jumps[open_i] = i
            jumps[i] = open_i
    if stack:
        raise ValueError("unmatched '[' at offset %d" % stack[-1])

    tape = bytearray(cells)
    ptr = 0
    pc = 0
    out = bytearray()
    steps = 0
    length = len(program)
    while pc < length:
        ch = program[pc]
        if ch == "+":
            tape[ptr] = (tape[ptr] + 1) & 0xFF
        elif ch == "-":
            tape[ptr] = (tape[ptr] - 1) & 0xFF
        elif ch == ">":
            ptr += 1
            if ptr >= cells:
                raise ValueError("tape overrun")
        elif ch == "<":
            ptr -= 1
            if ptr < 0:
                raise ValueError("tape underrun")
        elif ch == ".":
            out.append(tape[ptr])
        elif ch == ",":
            tape[ptr] = 0
        elif ch == "[":
            if tape[ptr] == 0:
                pc = jumps[pc]
        elif ch == "]":
            if tape[ptr] != 0:
                pc = jumps[pc]
        pc += 1
        steps += 1
        if steps > max_steps:
            raise RuntimeError("interpreter ran past %d steps" % max_steps)
    return bytes(out).decode("utf-8", "replace")