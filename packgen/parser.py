# This file covered by GPL 3 license
# C. David Horsley 2025
"""Turn a C header into a :class:`~packgen.model.Schema` using tree-sitter.

Unlike a real compiler front end this does not run the preprocessor, so only
declarations written in the file itself are seen.  Three things are handled
anyway, because headers do not work without them:

* object-like ``#define``s and enumerators are folded, so they can be used as
  array bounds;
* ``#if`` conditions are folded where they can be, so the structs inside a
  dead branch are skipped rather than emitted against types the header never
  defines.  A condition that cannot be folded falls back to reading every
  branch -- that is what makes include guards work -- and any name that then
  means two different things is refused rather than guessed at;
* ``#ifdef __cplusplus`` blocks are dropped before parsing, because the
  ``extern "C" {`` they open in one block and close in another would otherwise
  leave tree-sitter looking at an unbalanced file.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path

import tree_sitter_c
from tree_sitter import Language, Node, Parser

from packgen.errors import ParseError, UnsupportedTypeError
from packgen.model import Field, Schema, Struct

C_LANGUAGE = Language(tree_sitter_c.language())


def _c_div(a: int, b: int) -> int:
    """Integer division that truncates toward zero, the way C does."""
    if b == 0:
        raise UnsupportedTypeError("division by zero in a constant expression")
    return -(-a // b) if (a < 0) != (b < 0) else a // b


def _c_mod(a: int, b: int) -> int:
    """Remainder with the sign of the dividend, the way C does."""
    return a - _c_div(a, b) * b


#: Shifting by a negative or absurd amount is undefined in C; Python raises
#: ValueError or eats all of memory, so both are refused up front.
_MAX_SHIFT = 64


def _c_shift(op: Callable[[int, int], int]) -> Callable[[int, int], int]:
    def shift(a: int, b: int) -> int:
        if not 0 <= b <= _MAX_SHIFT:
            raise UnsupportedTypeError(f"shift by {b} is undefined in C")
        return op(a, b)

    return shift


_BINARY_OPS: dict[str, Callable[[int, int], int]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": _c_div,
    "%": _c_mod,
    "<<": _c_shift(operator.lshift),
    ">>": _c_shift(operator.rshift),
    "|": operator.or_,
    "&": operator.and_,
    "^": operator.xor,
    # Logical and relational operators yield int in C, and turn up in the
    # `#if` conditions that decide whether a struct exists at all.
    "&&": lambda a, b: int(bool(a) and bool(b)),
    "||": lambda a, b: int(bool(a) or bool(b)),
    "==": lambda a, b: int(a == b),
    "!=": lambda a, b: int(a != b),
    "<": lambda a, b: int(a < b),
    ">": lambda a, b: int(a > b),
    "<=": lambda a, b: int(a <= b),
    ">=": lambda a, b: int(a >= b),
}

_UNARY_OPS: dict[str, Callable[[int], int]] = {
    "+": operator.pos,
    "-": operator.neg,
    "~": operator.inv,
    "!": lambda a: int(not a),
}

_DIRECTIVE = re.compile(rb"^[ \t]*#[ \t]*([A-Za-z_]+)(.*)$")

_TRAILING_COMMENT = re.compile(r"(/\*.*?\*/|//.*)$")


def _cplusplus_condition(keyword: str, rest: str) -> bool | None:
    """Whether a directive tests ``__cplusplus``, and how it evaluates in C++.

    ``True`` means the guarded branch is the C++ one, ``False`` means it is the
    C one, and ``None`` means the directive is about something else.
    """
    text = _TRAILING_COMMENT.sub("", rest).strip()
    if keyword == "ifdef":
        return True if text == "__cplusplus" else None
    if keyword == "ifndef":
        return False if text == "__cplusplus" else None
    if keyword == "if":
        condensed = re.sub(r"\s+", "", text)
        if condensed in {"defined(__cplusplus)", "defined__cplusplus", "__cplusplus"}:
            return True
        if condensed in {
            "!defined(__cplusplus)",
            "!defined__cplusplus",
            "!__cplusplus",
        }:
            return False
    return None


def _still_in_comment(line: bytes, in_comment: bool) -> bool:
    """Whether a block comment is still open at the end of ``line``."""
    index = 0
    while index < len(line):
        if in_comment:
            close = line.find(b"*/", index)
            if close == -1:
                return True
            in_comment = False
            index = close + 2
            continue
        block = line.find(b"/*", index)
        inline = line.find(b"//", index)
        if inline != -1 and (block == -1 or inline < block):
            return False  # rest of the line is a line comment
        if block == -1:
            return False
        in_comment = True
        index = block + 2
    return in_comment


def strip_cplusplus_blocks(source: bytes) -> bytes:
    """Blank out the C++ half of every ``#ifdef __cplusplus`` block.

    packgen reads a header as C, so those branches are dead code.  Leaving
    them in is not an option: the ``extern "C" {`` that almost every header
    opens in one block and closes in another leaves tree-sitter looking at
    unbalanced braces, and it rejects the whole file.  Suppressed lines are
    replaced by empty ones so that reported line numbers still line up.
    """
    lines = source.split(b"\n")
    kept: list[bytes] = []
    frames: list[str] = []  # "cxx" for blocks we evaluate, "plain" for the rest
    suppressed_at: int | None = None  # frame index whose branch we are dropping
    in_comment = False

    for line in lines:
        # A `#ifdef __cplusplus` inside a comment is not a directive, and
        # acting on one would quietly swallow the declarations after it.
        commented = in_comment
        in_comment = _still_in_comment(line, in_comment)

        match = None if commented else _DIRECTIVE.match(line)
        keyword = match.group(1).decode("utf-8", "replace") if match else ""
        rest = match.group(2).decode("utf-8", "replace") if match else ""

        if keyword in {"if", "ifdef", "ifndef"}:
            cxx = _cplusplus_condition(keyword, rest) if suppressed_at is None else None
            if cxx is not None:
                frames.append("cxx")
                if cxx:
                    suppressed_at = len(frames) - 1
                kept.append(b"")
                continue
            frames.append("plain")
        elif keyword in {"else", "elif", "elifdef", "elifndef"}:
            innermost = len(frames) - 1
            if frames and frames[-1] == "cxx" and suppressed_at in (None, innermost):
                # Flip: whichever branch we were keeping, keep the other.
                suppressed_at = None if suppressed_at is not None else innermost
                kept.append(b"")
                continue
        elif keyword == "endif" and frames:
            closing = len(frames) - 1
            kind = frames.pop()
            if kind == "cxx":
                if suppressed_at == closing:
                    suppressed_at = None
                kept.append(b"")
                continue

        kept.append(b"" if suppressed_at is not None else line)

    return b"\n".join(kept)


def _text(node: Node) -> str:
    assert node.text is not None
    return str(node.text.decode("utf-8"))


def _where(node: Node) -> str:
    return f"line {node.start_point[0] + 1}"


def _child(node: Node, field_name: str) -> Node | None:
    return node.child_by_field_name(field_name)


def _declared_name(node: Node) -> str | None:
    """The identifier a declarator eventually names, ignoring `*` and `[]`."""
    while node.type not in {"type_identifier", "identifier", "field_identifier"}:
        inner = _child(node, "declarator")
        if inner is None:
            return None
        node = inner
    return _text(node)


#: Why a typedef whose declarator is not a plain name cannot be packed.
_TYPEDEF_REASONS = {
    "pointer_declarator": "typedefs to pointers cannot be packed",
    "function_declarator": "typedefs to functions cannot be packed",
    "array_declarator": (
        "typedefs to array types are not supported; declare the array in the "
        "struct instead"
    ),
}


class _Parser:
    """Collects definitions from one translation unit."""

    def __init__(self, source: bytes, filename: str | None = None):
        # A directive on an unterminated last line is a parse error, and a
        # missing final newline is not worth rejecting a header over.
        if source and not source.endswith(b"\n"):
            source += b"\n"
        self.source = strip_cplusplus_blocks(source)
        self.filename = filename
        self.constants: dict[str, int] = {}
        #: Every name this file #defines, whether or not it has a foldable
        #: value.  `#ifdef` only asks whether a name exists.
        self.defined_names: set[str] = set()
        #: Names that exist but whose value packgen could not work out, mapped
        #: to the reason.  Using one as an array bound is an error rather than
        #: a guess.
        self.poisoned: dict[str, str] = {}
        self.structs: list[Struct] = []
        self.aliases: dict[str, str] = {}
        self.unsupported: dict[str, str] = {}
        self._parser = Parser(C_LANGUAGE)

    # -- entry point ----------------------------------------------------

    def parse(self) -> Schema:
        tree = self._parser.parse(self.source)
        root = tree.root_node
        if root.has_error:
            raise ParseError(
                f"{self._prefix()}could not parse: {self._first_error(root)}"
            )

        self._walk(root.named_children)

        return Schema(
            structs=tuple(self.structs),
            aliases=dict(self.aliases),
            unsupported=dict(self.unsupported),
            macros=frozenset(self.defined_names),
        )

    def _walk(self, nodes: list[Node]) -> None:
        for node in nodes:
            if node.type == "preproc_def":
                self._collect_define(node)
            elif node.type == "preproc_function_def":
                self._collect_function_define(node)
            elif node.type == "type_definition":
                self._collect_typedef(node)
            elif node.type in {"enum_specifier", "declaration"}:
                # `enum {...};` and typedef-less declarations still carry
                # enumerators that may be used as array bounds.
                self._collect_enumerators(node)
            elif node.type == "linkage_specification":
                # extern "C" { ... }, once the #ifdef around it has been
                # stripped, or written out in full.
                body = _child(node, "body")
                self._walk(body.named_children if body else [])
            elif node.type == "preproc_else":
                self._walk(node.named_children)
            elif node.type.startswith(("preproc_if", "preproc_elif")):
                self._walk(self._live_branch(node))

    def _live_branch(self, node: Node) -> list[Node]:
        """The children of a conditional block that are actually compiled.

        Descending into every branch is what makes include guards work, but
        applied blindly it also emits the structs inside ``#if 0``, producing
        code that names types the header does not define.  So the condition is
        folded where it can be: ``#if 0``, ``#ifdef`` of a macro this file
        never defines, and anything built from constants it does define.  An
        unfoldable condition falls back to taking every branch, where the
        duplicate-name guards keep a genuine conflict from slipping through.
        """
        alternative = _child(node, "alternative")
        head = _child(node, "condition") or _child(node, "name")
        body = [
            child
            for child in node.named_children
            if (head is None or child.id != head.id)
            and (alternative is None or child.id != alternative.id)
        ]

        truth = self._condition_truth(node, head)
        if truth is None:
            return body + ([alternative] if alternative is not None else [])
        if truth:
            return body
        return [alternative] if alternative is not None else []

    def _condition_truth(self, node: Node, head: Node | None) -> bool | None:
        """Whether a conditional is taken, or ``None`` if that is unknowable."""
        if head is None:
            return None

        if node.type in {"preproc_ifdef", "preproc_elifdef"}:
            negated = node.children and node.children[0].type in {
                "#ifndef",
                "#elifndef",
            }
            defined = _text(head) in self.defined_names
            return (not defined) if negated else defined

        try:
            # In `#if`, C replaces every identifier it does not know with 0,
            # which is what makes `#ifdef`-style guards on unknown macros fold.
            return self._eval(head, undefined_is_zero=True) != 0
        except UnsupportedTypeError:
            return None

    def _add_struct(self, struct: Struct, node: Node) -> None:
        if any(existing.name == struct.name for existing in self.structs):
            raise UnsupportedTypeError(
                self._error(
                    node,
                    f"{struct.name!r} is defined more than once under a condition "
                    f"packgen could not fold, so it cannot choose a layout",
                )
            )
        self.structs.append(struct)

    def _prefix(self) -> str:
        return f"{self.filename}: " if self.filename else ""

    def _error(self, node: Node, message: str) -> str:
        return f"{self._prefix()}{_where(node)}: {message}"

    @staticmethod
    def _first_error(root: Node) -> str:
        # Zero-width MISSING nodes are absent from `children`, so the tree has
        # to be walked with a cursor to find them.
        cursor = root.walk()
        deepest: Node | None = None
        while True:
            node = cursor.node
            if node is not None and (node.is_error or node.is_missing):
                deepest = node
                if node.is_missing:
                    break
            if node is not None and node.has_error and cursor.goto_first_child():
                continue
            while not cursor.goto_next_sibling():
                if not cursor.goto_parent():
                    return _Parser._describe_error(deepest)
        return _Parser._describe_error(deepest)

    @staticmethod
    def _describe_error(node: Node | None) -> str:
        if node is None:
            return "syntax error"
        if node.is_missing:
            return f"missing {node.type} at {_where(node)}"
        text = _text(node)
        if len(text) > 40:
            text = text[:40] + "..."
        return f"unexpected {text!r} at {_where(node)}"

    # -- constants ------------------------------------------------------

    def _define(self, name: str, value: int | None, reason: str) -> None:
        """Record a constant, or poison the name if its value is in doubt.

        Both branches of an ``#if`` are walked, so a name can arrive twice.
        Silently keeping the last one would mean generating a layout for a
        build configuration the caller may not be using.
        """
        if value is None:
            self.constants.pop(name, None)
            self.poisoned.setdefault(name, reason)
            return
        if name in self.poisoned:
            return
        if name in self.constants and self.constants[name] != value:
            del self.constants[name]
            self.poisoned[name] = (
                f"{name!r} is defined more than once with different values, under "
                f"a condition packgen could not fold to choose between them"
            )
            return
        self.constants[name] = value

    def _collect_define(self, node: Node) -> None:
        name = _child(node, "name")
        value = _child(node, "value")
        if name is None:
            return
        self.defined_names.add(_text(name))
        if value is None:
            return  # bare #define FOO, nothing to fold, but it is defined
        try:
            self._define(_text(name), self._eval_text(value.text or b""), "")
        except UnsupportedTypeError as exc:
            self._define(_text(name), None, str(exc))

    def _collect_function_define(self, node: Node) -> None:
        """Record a function-like macro's name, but never a value.

        packgen cannot expand one, so it is poisoned for array-bound
        purposes.  The name still matters twice over: ``#ifdef FOO`` is
        true for a function-like macro, and a header defining
        ``unmarshal_s_t(x)`` would silently eat the declaration packgen
        emits for struct ``s_t``.
        """
        name = _child(node, "name")
        if name is None:
            return
        text = _text(name)
        self.defined_names.add(text)
        self._define(text, None, f"{text!r} is a function-like macro")

    def _collect_enumerators(self, node: Node) -> None:
        for enum in self._descend(node, "enumerator_list"):
            next_value: int | None = 0
            reason = ""
            for enumerator in enum.named_children:
                if enumerator.type != "enumerator":
                    continue
                name = _child(enumerator, "name")
                value = _child(enumerator, "value")
                if name is None:
                    continue
                if value is not None:
                    try:
                        next_value = self._eval(value)
                        reason = ""
                    except UnsupportedTypeError as exc:
                        # Enumerators after this one are numbered relative to
                        # it, so none of them can be trusted either.
                        next_value = None
                        reason = f"packgen could not evaluate {_text(value)!r} ({exc})"
                self._define(_text(name), next_value, reason)
                if next_value is not None:
                    next_value += 1

    @staticmethod
    def _descend(node: Node, type_: str) -> list[Node]:
        found = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == type_:
                found.append(current)
            else:
                stack.extend(current.named_children)
        return found

    def _eval_text(self, text: bytes) -> int:
        """Evaluate a fragment of C that was not parsed as an expression."""
        tree = self._parser.parse(b"(" + text.strip() + b");")
        statement = tree.root_node.named_children
        if tree.root_node.has_error or not statement:
            raise UnsupportedTypeError(f"not an integer constant: {text!r}")
        return self._eval(statement[0].named_children[0])

    def _eval(self, node: Node, undefined_is_zero: bool = False) -> int:
        """Evaluate a constant integer expression node.

        ``undefined_is_zero`` follows the preprocessor's rule that an unknown
        identifier in an ``#if`` stands for 0.  It must stay off everywhere
        else: in an array bound an unknown name is an error, not a zero.
        """
        evaluate = partial(self._eval, undefined_is_zero=undefined_is_zero)
        match node.type:
            case "number_literal":
                return self._eval_literal(node)
            case "preproc_defined":
                names = [c for c in node.named_children if c.type == "identifier"]
                if not names:
                    raise UnsupportedTypeError("malformed defined()")
                return int(_text(names[0]) in self.defined_names)
            case "identifier" | "type_identifier":
                name = _text(node)
                if name in self.poisoned:
                    raise UnsupportedTypeError(self.poisoned[name])
                if name not in self.constants:
                    if undefined_is_zero:
                        return 0
                    raise UnsupportedTypeError(
                        f"{name!r} is not a known constant; packgen folds "
                        f"object-like #defines and enumerators from this file only"
                    )
                return self.constants[name]
            case "parenthesized_expression":
                return evaluate(node.named_children[0])
            case "binary_expression":
                left, right = _child(node, "left"), _child(node, "right")
                op = _child(node, "operator")
                if left is None or right is None or op is None:
                    raise UnsupportedTypeError("malformed constant expression")
                try:
                    binary_op = _BINARY_OPS[_text(op)]
                except KeyError:
                    raise UnsupportedTypeError(
                        f"operator {_text(op)!r} is not allowed in an array bound"
                    ) from None
                return binary_op(evaluate(left), evaluate(right))
            case "unary_expression":
                argument, op = _child(node, "argument"), _child(node, "operator")
                if argument is None or op is None:
                    raise UnsupportedTypeError("malformed constant expression")
                try:
                    unary_op = _UNARY_OPS[_text(op)]
                except KeyError:
                    raise UnsupportedTypeError(
                        f"operator {_text(op)!r} is not allowed in an array bound"
                    ) from None
                return unary_op(evaluate(argument))
            case "char_literal":
                body = _text(node)[1:-1]
                if len(body) != 1:
                    raise UnsupportedTypeError(f"cannot evaluate {_text(node)!r}")
                return ord(body)
            case _:
                raise UnsupportedTypeError(
                    f"cannot evaluate {_text(node)!r} as a constant"
                )

    @staticmethod
    def _eval_literal(node: Node) -> int:
        text: str = _text(node).rstrip("uUlL")
        # C spells octal with a bare leading zero, Python wants 0o.
        if len(text) > 1 and text[0] == "0" and text[1] not in "xXbBoO.":
            text = "0o" + text[1:]
        try:
            return int(text, 0)
        except ValueError:
            raise UnsupportedTypeError(
                f"{_text(node)!r} is not an integer literal"
            ) from None

    # -- typedefs -------------------------------------------------------

    def _collect_typedef(self, node: Node) -> None:
        type_node = _child(node, "type")
        declarators = node.children_by_field_name("declarator")
        if type_node is None or not declarators:
            return

        names = []
        for declarator in declarators:
            if declarator.type != "type_identifier":
                # `typedef struct {...} *p_t;`, `typedef uint8_t buf_t[4];`
                # and friends.  The name has to come out of the declarator
                # tree; the raw text still carries the `*` or the `[4]`.
                name = _declared_name(declarator)
                if name is not None:
                    self.unsupported[name] = _TYPEDEF_REASONS.get(
                        declarator.type,
                        f"packgen cannot pack a typedef of this shape "
                        f"({_text(declarator)!r})",
                    )
                continue
            names.append(_text(declarator))
        if not names:
            return

        if type_node.type == "struct_specifier":
            body = _child(type_node, "body")
            if body is None:
                for name in names:
                    self.unsupported[name] = (
                        "the struct has no body in this file; packgen does not "
                        "follow #include"
                    )
                return
            fields = self._struct_fields(body)
            for name in names:
                self._add_struct(Struct(name=name, fields=fields), node)
            return

        if type_node.type in {"union_specifier", "enum_specifier"}:
            if type_node.type == "enum_specifier":
                self._collect_enumerators(type_node)
            kind = type_node.type.removesuffix("_specifier")
            for name in names:
                self.unsupported[name] = (
                    f"{kind}s have no portable packed representation"
                )
            return

        # A plain alias: typedef uint32_t word_t;
        target = self._type_name(type_node, allow_unsupported=True)
        for name in names:
            if target is None:
                self.unsupported[name] = "the aliased type is not a fixed-width type"
            else:
                self.aliases[name] = target

    # -- struct bodies --------------------------------------------------

    def _struct_fields(self, body: Node) -> tuple[Field, ...]:
        fields: list[Field] = []
        for child in body.named_children:
            if child.type == "comment":
                continue
            if child.type.startswith("preproc_"):
                raise UnsupportedTypeError(
                    self._error(
                        child,
                        "conditional compilation inside a struct body would "
                        "silently change the packed layout",
                    )
                )
            if child.type != "field_declaration":
                continue
            fields.extend(self._field_declaration(child))
        return tuple(fields)

    def _field_declaration(self, node: Node) -> list[Field]:
        type_node = _child(node, "type")
        if type_node is None:
            return []

        if any(child.type == "bitfield_clause" for child in node.named_children):
            raise UnsupportedTypeError(
                self._error(node, "bit fields have no portable packed layout")
            )

        qualifiers = [
            _text(child)
            for child in node.named_children
            if child.type == "type_qualifier"
        ]
        if "const" in qualifiers:
            # unmarshal_* assigns to every member, so a const one would only
            # fail later, in the C compiler, on generated code.
            raise UnsupportedTypeError(
                self._error(node, "const members cannot be unpacked into")
            )

        declarators = node.children_by_field_name("declarator")
        if not declarators:
            raise UnsupportedTypeError(
                self._error(node, "anonymous members are not supported")
            )

        type_ = self._type_name(type_node)
        if type_ is None:
            raise UnsupportedTypeError(
                self._error(
                    type_node,
                    f"{_text(type_node)!r} is not a fixed-width type; use the "
                    f"C99 <stdint.h> types so the packed layout is portable",
                )
            )

        fields = []
        for declarator in declarators:
            name, dims, pointer = self._declarator(declarator)
            fields.append(Field(name=name, type=type_, dims=dims, pointer=pointer))
        return fields

    def _type_name(self, node: Node, allow_unsupported: bool = False) -> str | None:
        """The spelling of a type, or ``None`` if packgen cannot pack it."""
        match node.type:
            case "primitive_type" | "type_identifier":
                return _text(node)
            case (
                "sized_type_specifier"
                | "struct_specifier"
                | "union_specifier"
                | "enum_specifier"
            ):
                return None
            case _:
                if allow_unsupported:
                    return None
                raise UnsupportedTypeError(
                    self._error(node, f"unsupported type {_text(node)!r}")
                )

    def _declarator(self, node: Node) -> tuple[str, tuple[int, ...], int]:
        """Unwrap a declarator into ``(name, dims, pointer_depth)``."""
        match node.type:
            case "field_identifier" | "identifier":
                return _text(node), (), 0
            case "array_declarator":
                inner = _child(node, "declarator")
                size = _child(node, "size")
                if inner is None:
                    raise UnsupportedTypeError(
                        self._error(node, "malformed array declaration")
                    )
                if size is None:
                    raise UnsupportedTypeError(
                        self._error(
                            node,
                            "arrays must have a fixed size; packgen does not "
                            "handle variable length data",
                        )
                    )
                name, dims, pointer = self._declarator(inner)
                try:
                    extent = self._eval(size)
                except UnsupportedTypeError as exc:
                    raise UnsupportedTypeError(self._error(size, str(exc))) from None
                if extent <= 0:
                    raise UnsupportedTypeError(
                        self._error(size, f"array bound must be positive, got {extent}")
                    )
                return name, (*dims, extent), pointer
            case "pointer_declarator":
                inner = _child(node, "declarator")
                if inner is None:
                    raise UnsupportedTypeError(
                        self._error(node, "malformed pointer declaration")
                    )
                name, dims, pointer = self._declarator(inner)
                return name, dims, pointer + 1
            case "function_declarator":
                raise UnsupportedTypeError(
                    self._error(node, "function members are not supported")
                )
            case _:
                raise UnsupportedTypeError(
                    self._error(node, f"unsupported declarator {_text(node)!r}")
                )


def parse_source(source: str | bytes, filename: str | None = None) -> Schema:
    """Parse C source text into a :class:`~packgen.model.Schema`."""
    if isinstance(source, str):
        source = source.encode("utf-8")
    return _Parser(source, filename=filename).parse()


def parse_header(path: str | Path) -> Schema:
    """Parse a header file into a :class:`~packgen.model.Schema`."""
    path = Path(path)
    return _Parser(path.read_bytes(), filename=str(path)).parse()
