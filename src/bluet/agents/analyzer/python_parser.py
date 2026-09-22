"""Python parser plugin, tolerant of Python 2 syntax where it differs.

The bundled tree-sitter-python grammar still models Python 2 constructs, which
we verified parse without ERROR nodes:

- ``print x`` and ``print >>stream, x`` (``print_statement``)
- ``exec "code"`` (``exec_statement``)
- ``except SomeError, e:`` (old-style catch)
- ``raise ValueError, "message"`` (tuple-form raise)
- ``xrange(...)`` and long-suffix literals (``1L``)

Known limitation: Python 2 backtick-repr `` `expr` `` is mis-parsed by
tree-sitter-python as a ``string`` node rather than an expression, so it is
invisible to extraction (the file still parses without error).
"""

from __future__ import annotations

from tree_sitter import Node

from bluet.agents.analyzer._nodes import first_string, iter_scoped, line, short, text
from bluet.agents.analyzer.base import LanguageParser, register
from bluet.agents.analyzer.grammar import parser_for
from bluet.agents.analyzer.models import (
    BranchSpec,
    FunctionDef,
    LogicSpec,
    NondeterministicCall,
    _dedupe,
)

_BRANCH_KINDS = {
    "if_statement": "if",
    "for_statement": "for",
    "while_statement": "while",
    "try_statement": "try",
    "with_statement": "with",
    "conditional_expression": "conditional",
    "match_statement": "match",
}

_FILE_CALLEES = {"open", "file", "os.open", "io.open", "codecs.open"}
_CONSOLE_LEAVES = {"raw_input", "input", "getpass"}
_NETWORK_MODULES = {
    "httplib",
    "urllib",
    "urllib2",
    "requests",
    "socket",
    "ftplib",
    "poplib",
    "smtplib",
}

_NESTED_SKIP = frozenset({"function_definition", "class_definition", "lambda"})


def _target_name(write: Node) -> str:
    """Reduce an assignment/loop LHS to its root variable or field name."""
    node = write
    if node.type == "identifier":
        return text(node)
    if node.type in {"attribute", "subscript", "dotted_name"}:
        for child in node.named_children:
            if child.type == "identifier":
                return text(child)
    if node.type in {
        "tuple",
        "list",
        "pattern_list",
        "parenthesized_expression",
    }:
        first = node.named_children
        if first:
            return _target_name(first[0])
    return text(node)


@register
class PythonParser(LanguageParser):
    """Parses Python (2-tolerant) source into a :class:`LogicSpec`."""

    language = "python"

    def parse(self, source: str) -> LogicSpec:
        root = parser_for(self.language).parse(source.encode("utf-8")).root_node
        functions = [
            self._analyze_function(node)
            for node in iter_scoped(root)
            if node.type == "function_definition"
        ]
        return LogicSpec(functions=functions)

    def _analyze_function(self, fn: Node) -> FunctionDef:
        name = text(fn.child_by_field_name("name"))
        body = fn.child_by_field_name("body") or fn
        params = self._parameters(fn.child_by_field_name("parameters"))
        returns = self._returns(body)
        return FunctionDef(
            name=name,
            line=line(fn),
            params=params,
            returns=returns,
            inputs=params,
            outputs=returns,
            state_mutations=_dedupe(self._mutations(body)),
            branches=self._branches(body),
            nondeterministic_calls=self._nondeterministic(body),
        )

    def _parameters(self, parameters: Node | None) -> list[str]:
        if parameters is None:
            return []
        names: list[str] = []
        for node in parameters.named_children:
            name_field = node.child_by_field_name("name")
            if name_field is not None:
                names.append(text(name_field))
            elif node.type == "identifier":
                names.append(text(node))
        return names

    def _returns(self, body: Node) -> list[str]:
        returns: list[str] = []
        for node in iter_scoped(body, skip=_NESTED_SKIP):
            if node.type != "return_statement":
                continue
            expressions = node.named_children
            returns.append(text(expressions[-1]) if expressions else "None")
        return returns

    def _mutations(self, body: Node) -> list[str]:
        targets: list[str] = []
        for node in iter_scoped(body, skip=_NESTED_SKIP):
            if node.type in {"assignment", "augmented_assignment"}:
                left = node.child_by_field_name("left")
                if left is not None:
                    targets.append(_target_name(left))
        return targets

    def _branches(self, body: Node) -> list[BranchSpec]:
        branches: list[BranchSpec] = []
        for node in iter_scoped(body, skip=_NESTED_SKIP):
            kind = _BRANCH_KINDS.get(node.type)
            if kind is None:
                continue
            if node.type == "for_statement":
                target = node.child_by_field_name("left")
                label = short(_target_name(target)) if target is not None else None
            elif node.type in {"if_statement", "while_statement", "conditional_expression"}:
                condition = node.child_by_field_name("condition")
                label = short(text(condition)) if condition is not None else None
            else:
                label = None
            branches.append(BranchSpec(kind=kind, line=line(node), label=label))
        return branches

    def _nondeterministic(self, body: Node) -> list[NondeterministicCall]:
        calls: list[NondeterministicCall] = []
        for node in iter_scoped(body, skip=_NESTED_SKIP):
            if node.type == "call":
                classified = self._classify_call(node)
                if classified is not None:
                    calls.append(classified)
            elif node.type == "print_statement":
                calls.append(NondeterministicCall(kind="console", name="print", line=line(node)))
        return calls

    def _classify_call(self, call: Node) -> NondeterministicCall | None:
        callee = text(call.child_by_field_name("function"))
        leaf = callee.rsplit(".", 1)[-1]
        target = first_string(call.child_by_field_name("arguments"))
        if callee in _FILE_CALLEES:
            return NondeterministicCall(kind="file", name=callee, line=line(call), target=target)
        if leaf in _CONSOLE_LEAVES:
            return NondeterministicCall(kind="console", name=callee, line=line(call), target=None)
        if callee.split(".", 1)[0].lower() in _NETWORK_MODULES:
            return NondeterministicCall(kind="network", name=callee, line=line(call), target=target)
        return None
