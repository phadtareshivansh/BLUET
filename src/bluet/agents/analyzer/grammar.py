"""Tree-sitter grammar loading, centralized for the analyzer agent.

Grammars come from ``tree-sitter-language-pack`` (per Prompt 0.1), not from
``tree-sitter-languages``: the latter is unmaintained and its precompiled
bindings are ABI-incompatible with the pinned ``tree-sitter`` (its
``Language(path, name)`` constructor was removed from the current binding).
"""

from __future__ import annotations

from functools import cache

from tree_sitter import Parser
from tree_sitter_language_pack import get_parser as _get_pack_parser


@cache
def parser_for(language: str) -> Parser:
    """Return a cached :class:`Parser` for ``language`` (e.g. ``"python"``)."""
    return _get_pack_parser(language)