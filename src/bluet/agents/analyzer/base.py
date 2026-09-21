"""Per-language parser plugins behind a common interface.

Every concrete parser targets ONE language and exposes the same
``parse(source: str) -> LogicSpec`` contract. New languages (Phase 4's COBOL)
are added by subclassing :class:`LanguageParser` and registering the subclass;
no changes to the interface or existing parsers are required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from bluet.agents.analyzer.models import LogicSpec

_SUFFIX_TO_LANGUAGE = {".py": "python", ".java": "java"}

_ALIASES = {
    "py": "python",
    "python2": "python",
    "python 2": "python",
    "python2.x": "python",
    "py2": "python",
    "python3": "python",
    "python 3": "python",
    "java8": "java",
    "java 8": "java",
    "jdk8": "java",
}


class LanguageParser(ABC):
    """Parses source text into a structurally valid :class:`LogicSpec`."""

    language: ClassVar[str]

    @abstractmethod
    def parse(self, source: str) -> LogicSpec:
        """Parse ``source`` and return its ``LogicSpec``.

        The parser is expected to be tolerant of syntax it cannot fully
        model: parse errors are surfaced via partial results, never raised.
        """


_PARSERS: dict[str, type[LanguageParser]] = {}


def register(parser_cls: type[LanguageParser]) -> type[LanguageParser]:
    """Register a parser implementation under ``parser_cls.language``."""
    name = (parser_cls.language or "").strip().lower()
    if not name:
        raise ValueError("LanguageParser requires a non-empty language name")
    _PARSERS[name] = parser_cls
    return parser_cls


def _normalize(language: str) -> str:
    key = " ".join(language.strip().lower().split())
    return _ALIASES.get(key, key)


def get_parser(language: str) -> LanguageParser:
    """Instantiate the :class:`LanguageParser` registered for ``language``."""
    try:
        return _PARSERS[_normalize(language)]()
    except KeyError as exc:
        raise ValueError(f"no LanguageParser registered for {language!r}") from exc


def parser_for_source(
    target_language: str | None, filename: str | None
) -> LanguageParser | None:
    """Resolve a parser from an explicit target language, else file suffix."""
    if target_language and target_language.strip():
        try:
            return get_parser(target_language)
        except ValueError:
            pass
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in _SUFFIX_TO_LANGUAGE:
            return get_parser(_SUFFIX_TO_LANGUAGE[suffix])
    return None