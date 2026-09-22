"""Refactor Agent: ``LogicSpec`` -> formatted ``ProposedCode``.

Owns the whole generation pipeline the way the Verification Agent (Prompt 2.3)
owns parity sampling:

1. pull relevant context through :class:`~bluet.context_store.ContextStore`
   (soft-fail to no context on :class:`~bluet.context_store.ContextIndexError`);
2. render the Jinja2 scaffold (module header/import block) plus a per-function
   summary of the ``LogicSpec`` into a system prompt;
3. hand the LLM the scaffold + context hits + optional counter-examples and ask
   for a structured :class:`ProposedCode` (whole-file regeneration — self-heal
   rewrites the entire file, never a per-function patch);
4. run the result through the language formatter (``ruff format`` for python).

Every moving part (LLM, context store, formatter) is constructor-injectable so
tests can drive the full pipeline with a deterministic fake LLM and never touch
a network or subprocess.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from jinja2 import Environment

from bluet.agents.analyzer.models import FunctionDef, LogicSpec
from bluet.agents.llm_client import LLMClient
from bluet.agents.refactor.errors import RefactorError
from bluet.agents.refactor.formatters import format_code
from bluet.agents.refactor.models import CounterExample, ProposedCode
from bluet.context_store import ContextHit, ContextIndexError, ContextStore

# Validated import paths exist; the analyzer __init__ re-exports SymbolTable and
# others, but the analyzer base holds parser language aliases we re-derive here.
_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python": "python",
    "python2": "python",
    "python3": "python",
    "java": "java",
    "java8": "java",
}
_SUFFIX_TO_LANGUAGE: dict[str, str] = {".py": "python", ".java": "java"}

_TEMPLATES: dict[str, Environment] = {
    "python": Environment(keep_trailing_newline=True).from_string(
        Path(resources.files("bluet.agents.refactor").joinpath("templates", "python.py.j2")).read_text()
    ),
    "java": Environment(keep_trailing_newline=True).from_string(
        Path(resources.files("bluet.agents.refactor").joinpath("templates", "java.py.j2")).read_text()
    ),
}


def normalize_language(target_language: str | None, *, filename: str | None = None) -> str:
    """Resolve the analyzer's language names (py/python2/java8/... ) to canonical.

    Falls back to the file suffix when ``target_language`` is empty (the CLI
    sends ``""`` and lets the analyzer detect from the filename).
    """
    raw = (target_language or "").strip().lower()
    if not raw and filename:
        raw = _SUFFIX_TO_LANGUAGE.get(Path(filename).suffix.lower(), "")
    canonical = _LANGUAGE_ALIASES.get(raw)
    if canonical is None:
        raise RefactorError(
            f"unsupported target_language={target_language!r} (filename={filename!r}); "
            "supported: python, java"
        )
    return canonical


def _function_stub(fn: FunctionDef, language: str = "python") -> str:
    """Render one function as a scaffold stub the model must reimplement."""
    params = ", ".join(fn.params) if fn.params else ""
    io: list[str] = []
    if fn.nondeterministic_calls:
        io.append("i/o: " + ", ".join(sorted({c.name for c in fn.nondeterministic_calls})))
    if fn.state_mutations:
        io.append("mutates: " + ", ".join(fn.state_mutations))
    hint = f"  // {('; '.join(io))}" if io else ""
    if language == "java":
        # Java method stub - we don't know return type from analyzer, use void
        return_type = "void"
        return f"    public {return_type} {fn.name}({params}) {{{hint}\n        // TODO: implement\n    }}"
    # Python default
    hint = f"  # {('; '.join(io))}" if io else ""
    return f"def {fn.name}({params}):{hint}  # line {fn.line}\n    pass"


def _function_summaries(spec: LogicSpec) -> list[dict[str, object]]:
    """Compact per-function spec summary embedded in the user turn."""
    summaries: list[dict[str, object]] = []
    for fn in spec.functions:
        summaries.append(
            {
                "name": fn.name,
                "line": fn.line,
                "params": fn.params,
                "returns": fn.returns,
                "inputs": fn.inputs,
                "nondeterministic": [call.name for call in fn.nondeterministic_calls],
                "state_mutations": fn.state_mutations,
            }
        )
    return summaries


class RefactorAgent:
    """Synthesize a formatted ``ProposedCode`` for one ``LogicSpec``."""

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        context_store: ContextStore | None = None,
        formatter: Callable[[str, str], str] = format_code,
    ) -> None:
        from bluet.context_store.memory import InMemoryContextStore

        self._llm = llm if llm is not None else LLMClient()
        self._context_store = context_store if context_store is not None else InMemoryContextStore()
        self._formatter = formatter

    async def refactor(
        self,
        spec: LogicSpec,
        *,
        target_language: str,
        current_file: str,
        counter_examples: list[CounterExample] | None = None,
    ) -> ProposedCode:
        """Run one refactor pass; returns a formatted :class:`ProposedCode`.

        Raises:
            LLMUnavailableError / LLMSchemaOutputError: backend unreachable or the
                model couldn't emit ``ProposedCode`` (let the graph node turn either
                into a ``feedback.warning`` — fail-soft, never a crash).
            RefactorError: language unsupported or formatter missing/failed.
        """
        language = normalize_language(target_language, filename=current_file)
        context_hits = await self._query_context(spec, language)
        counter_examples = list(counter_examples or [])
        messages = self._build_messages(spec, language, context_hits, counter_examples)

        if not self._llm.ready:
            await self._llm.start()
        result = await self._llm.complete(messages, ProposedCode)
        if not isinstance(result, ProposedCode):
            result = ProposedCode.model_validate(result.model_dump())

        formatted = self._formatter(result.code, language)
        return result.model_copy(
            update={
                "code": formatted,
                "file_path": current_file,
                "imports_added": result.imports_added,
            }
        )

    async def _query_context(self, spec: LogicSpec, language: str) -> list[ContextHit]:
        functions = "; ".join(fn.name for fn in spec.functions[:8])
        query = f"{language} refactor: {functions}" if functions else f"{language} refactor"
        try:
            return await self._context_store.query_context(query, top_k=3)
        except ContextIndexError:
            return []

    def _build_messages(
        self,
        spec: LogicSpec,
        language: str,
        context_hits: list[ContextHit],
        counter_examples: list[CounterExample],
    ) -> list[dict[str, str]]:
        template = _TEMPLATES[language]
        import_comment = "# :: imports_added are placed below by the model ::" if language == "python" else "// :: imports_added are placed below by the model ::"
        scaffold = template.render(
            module_docstring=f"Refactored target module — {language}.",
            imports=import_comment,
            bodies="\n\n".join(_function_stub(fn, language) for fn in spec.functions),
        )
        user: dict[str, object] = {
            "functions": _function_summaries(spec),
            "context": [
                {
                    "id": hit.id,
                    "text": hit.text,
                    "score": hit.score,
                    "metadata": hit.metadata,
                }
                for hit in context_hits
            ],
            "counter_examples": [cx.model_dump() for cx in counter_examples],
        }
        system = "\n".join(
            [
                "You are the Refactor Agent of an incremental-refactor tool.",
                f"Regenerate the ENTIRE target file below in {language}, preserving every",
                "public function signature and the module structure from the scaffold.",
                "Fill in real logic for every function.",
                "",
                "SCAFFOLD (mirror this structure exactly):",
                scaffold,
            ]
        )
        if counter_examples:
            system += (
                "\n\nCounter-examples are regressions the verifier caught. The whole file is "
                "regenerated, so fix the named regressions DURING generation of the full file."
            )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, indent=2)},
        ]
