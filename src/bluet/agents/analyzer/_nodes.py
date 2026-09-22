"""Shared tree-sitter traversal helpers used by the per-language parsers."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

_MAX_LABEL = 80


def text(node: Node | None) -> str:
    """Return a node's source text, decoded and stripped."""
    return node.text.decode("utf-8", errors="replace").strip() if node else ""


def line(node: Node | None) -> int:
    """Return a node's 1-based starting line."""
    return node.start_point.row + 1 if node else 0


def short(raw: str) -> str:
    """Collapse whitespace and cap the length of a branch label."""
    collapsed = " ".join(raw.split())
    if len(collapsed) <= _MAX_LABEL:
        return collapsed
    return f"{collapsed[: _MAX_LABEL - 1]}…"


def first_string(arguments: Node | None) -> str | None:
    """Return the first string literal in an argument list, unquoted."""
    if arguments is None:
        return None
    for child in arguments.named_children:
        if child.type == "string":
            return text(child).strip("\"'")
    return None


def iter_scoped(body: Node, *, skip: frozenset[str] = frozenset()) -> Iterator[Node]:
    """Yield nodes under ``body`` without descending into ``skip`` types."""
    for node in body.named_children:
        if node.type in skip:
            continue
        yield node
        yield from iter_scoped(node, skip=skip)
