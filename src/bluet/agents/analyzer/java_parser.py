"""Java 8 parser plugin via ``tree-sitter-language-pack``.

Grammar note: ``Map store`` (raw/untyped, no generics) parses as a
``type_identifier`` rather than a ``parameterized_type``; either way the
field still lands in ``state_mutations`` because the analyzer keys on the
*declared variable / assigned target*, never on the generic arguments, so
raw collections need no special handling.
"""

from __future__ import annotations

from tree_sitter import Node

from bluet.agents.analyzer._nodes import line as _line
from bluet.agents.analyzer._nodes import text
from bluet.agents.analyzer.base import LanguageParser, register
from bluet.agents.analyzer.grammar import parser_for
from bluet.agents.analyzer.models import (
    BranchSpec,
    FunctionDef,
    LogicSpec,
    NondeterministicCall,
    _dedupe,
)

_JDBC_METHODS = {
    "getconnection",
    "preparecall",
    "preparestatement",
    "createstatement",
    "executequery",
    "executeupdate",
    "executebatch",
    "addbatch",
    "clearbatch",
    "registeroutparameter",
    "commit",
    "rollback",
    "setint",
    "setlong",
    "setstring",
    "setobject",
    "setdouble",
    "setboolean",
    "settimestamp",
    "setdate",
    "setnull",
    "setbinarystream",
    "setcharacterstream",
    "setbytes",
    "getstring",
    "getint",
    "getlong",
    "getobject",
    "getdouble",
    "getboolean",
    "gettimestamp",
    "next",
}

_JDBC_TYPES = {
    "connection",
    "drivermanager",
    "statement",
    "preparedstatement",
    "callablestatement",
    "resultset",
    "rowset",
    "databasemetadata",
    "resultsetmetadata",
}

_FILE_TYPES = {
    "file",
    "fileinputstream",
    "fileoutputstream",
    "filereader",
    "filewriter",
    "randomaccessfile",
    "scanner",
    "bufferedreader",
    "bufferedwriter",
}

_NETWORK_TYPES = {
    "socket",
    "serversocket",
    "datagramsocket",
    "socketchannel",
    "serversocketchannel",
    "url",
    "httpurlconnection",
    "urlconnection",
    "urlclassloader",
}

# Caller-side names (e.g. `Files.readAllLines` helpers) that are file I/O.
_FILES_HELPER_LEAVES = {
    "readallbytes",
    "readalllines",
    "readstring",
    "write",
    "copy",
    "delete",
    "move",
    "newbufferedreader",
    "newbufferedwriter",
    "newinputstream",
    "newoutputstream",
    "openconnection",
}

_NETWORK_CALL_LEAVES = {
    "connect",
    "openconnection",
    "getinputstream",
    "getoutputstream",
    "openstream",
}

_BRANCH_KINDS = {
    "if_statement": "if",
    "for_statement": "for",
    "enhanced_for_statement": "foreach",
    "while_statement": "while",
    "do_statement": "do",
    "switch_statement": "switch",
    "switch_expression": "switch",
    "ternary_expression": "ternary",
    "try_statement": "try",
    "try_with_resources_statement": "try",
}

_METHOD_CALLEE_LEAVES = _JDBC_METHODS | _FILES_HELPER_LEAVES | _NETWORK_CALL_LEAVES | {"createhell"}

_MUTATION_STATEMENTS = {
    "assignment_expression",
    "update_expression",
}

_ROLLUP = {
    "method_declaration",
    "constructor_declaration",
}


def _receiver_name(call: Node) -> str:
    obj = call.child_by_field_name("object")
    return text(obj).rsplit(".", 1)[-1] if obj is not None else ""


def _callee_name(call: Node) -> str:
    name = call.child_by_field_name("name")
    return text(name) if name is not None else text(call)


def _target(node: Node | None) -> str:
    """Reduce an assignment/update LHS to its root variable or field name."""
    if node is None:
        return "unknown"
    if node.type == "identifier":
        return text(node)
    if node.type in {"field_access", "array_access"}:
        for child in node.named_children:
            if child.type == "identifier":
                return text(child).rsplit(".", 1)[-1]
    if node.named_children:
        return _target(node.named_children[0])
    return text(node)


def _unescape(raw: str) -> str:
    """Strip a Java string literal's quotes and resolve common escapes."""
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            mapping = {
                "n": "\n",
                "t": "\t",
                "r": "\r",
                "\\": "\\",
                '"': '"',
                "'": "'",
                "0": "\0",
                "b": "\b",
                "f": "\f",
            }
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _receiver_is_network(receiver: str, callee: str) -> bool:
    """True for network-style receivers (raw/typed socket, URL, channels)."""
    return receiver in _NETWORK_TYPES


def _first_jdbc_target(call: Node, args: Node | None) -> str | None:
    """Extract the first string argument of a JDBC call (the SQL target)."""
    if args is None:
        return None
    for node in args.named_children:
        if node.type == "string_literal":
            return _unescape(text(node))
    return None


@register
class JavaParser(LanguageParser):
    language = "java"

    def parse(self, source: str) -> LogicSpec:
        root = parser_for(self.language).parse(source.encode("utf-8")).root_node
        functions = [self._method(fn) for fn in self._iter_methods(root)]
        return LogicSpec(functions=functions)

    def _iter_methods(self, root: Node):
        for node in self._walk(root):
            if node.type in _ROLLUP:
                yield node

    def _method(self, decl: Node) -> FunctionDef:
        name = self._method_name(decl)
        params = self._parameters(decl.child_by_field_name("parameters"))
        returns = self._returns(decl.child_by_field_name("body"))
        body = decl.child_by_field_name("body")
        return FunctionDef(
            name=name,
            line=_line(decl),
            params=params,
            inputs=params,
            outputs=returns,
            returns=returns,
            state_mutations=self._mutations(body),
            branches=self._branches(body),
            nondeterministic_calls=self._nondeterministic(body),
        )

    def _method_name(self, decl: Node) -> str:
        name = decl.child_by_field_name("name")
        if name is not None:
            return text(name)
        # constructors carry no `name` field; use the nearest identifier
        for child in decl.named_children:
            if child.type == "identifier":
                return text(child)
        return text(decl)

    def _parameters(self, parameters: Node | None) -> list[str]:
        if parameters is None:
            return []
        names = []
        for child in parameters.named_children:
            name = child.child_by_field_name("name")
            if name is not None:
                names.append(text(name))
        return names

    def _returns(self, body: Node | None) -> list[str]:
        if body is None:
            return []
        out = []
        for node in self._walk(body):
            if node.type != "return_statement":
                continue
            if node.named_children:
                out.append(text(node.named_children[-1]))
            else:
                out.append("void")
        return out

    def _mutations(self, body: Node | None) -> list[str]:
        if body is None:
            return []
        targets = []
        for node in self._walk(body):
            if node.type in _MUTATION_STATEMENTS:
                target = node.child_by_field_name("left")
                if target is not None:
                    targets.append(_target(target))
            elif node.type in {"local_variable_declaration"}:
                for declarator in node.children_by_field_name("declarator"):
                    name = declarator.child_by_field_name("name")
                    if name is not None:
                        targets.append(text(name))
        return _dedupe(targets)

    def _branches(self, body: Node | None) -> list[BranchSpec]:
        if body is None:
            return []
        branches = []
        for node in self._walk(body):
            kind = _BRANCH_KINDS.get(node.type)
            if kind is not None:
                label = self._branch_label(node)
                branches.append(BranchSpec(kind=kind, line=_line(node), label=label))
        return branches

    def _branch_label(self, node: Node) -> str | None:
        if node.type in {"if_statement", "while_statement", "do_statement"}:
            cond = node.child_by_field_name("condition")
            return text(cond) if cond else None
        if node.type == "enhanced_for_statement":
            name = node.child_by_field_name("name")
            return text(name) if name else None
        return None

    def _nondeterministic(self, body: Node | None) -> list[NondeterministicCall]:
        if body is None:
            return []
        calls = []
        for node in self._walk(body):
            if node.type == "method_invocation":
                call = self._jdbc_or_io_call(node)
                if call is not None:
                    calls.append(call)
            elif node.type == "object_creation_expression":
                call = self._creation_call(node)
                if call is not None:
                    calls.append(call)
        return calls

    def _first_string(self, args: Node | None) -> str | None:
        if args is None:
            return None
        for node in args.named_children:
            if node.type == "string_literal":
                return _unescape(text(node))
        return None

    def _jdbc_or_io_call(self, call: Node) -> NondeterministicCall | None:
        callee = _callee_name(call).lower()
        receiver = _receiver_name(call)
        kind = None
        target = None
        if callee in _JDBC_METHODS or receiver in _JDBC_TYPES:
            kind = "jdbc"
        elif receiver == "files" and callee in _FILES_HELPER_LEAVES:
            kind = "file"
        elif _receiver_is_network(receiver, callee) or callee in _NETWORK_CALL_LEAVES and receiver:
            kind = "network"
        if kind is None:
            return None
        args = call.child_by_field_name("arguments")
        if kind == "jdbc":
            target = _first_jdbc_target(call, args)
        elif args is not None:
            target = self._first_string(args)
        return NondeterministicCall(
            kind=kind,
            name=f"{receiver}.{callee}" if receiver else callee,
            line=_line(call),
            target=target,
        )

    def _creation_call(self, creation: Node) -> NondeterministicCall | None:
        type_node = creation.child_by_field_name("type")
        type_name = text(type_node).lower() if type_node else ""
        kind = None
        if type_name in _FILE_TYPES:
            kind = "file"
        elif type_name in _NETWORK_TYPES:
            kind = "network"
        if kind is None:
            return None
        args = creation.child_by_field_name("arguments")
        return NondeterministicCall(
            kind=kind,
            name=f"new {type_name}",
            line=_line(creation),
            target=self._first_string(args) if args else None,
        )

    def _walk(self, node: Node):
        yield node
        for child in node.named_children:
            yield from self._walk(child)
