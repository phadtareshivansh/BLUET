"""Persistent subprocess worker: parse source files with the real analyzer.

The tree-sitter / ANTLR analyser extensions can crash (access violation) on
unusual source, and importing the parser family costs seconds — so the TUI runs
this worker as a single long-lived background process. The dashboard stays
responsive on seeded counts, then refines to real ``LogicSpec`` data the moment
a result lands.

Protocol (newline-delimited JSON over stdin/stdout):

* Request:  one absolute source-file path per line.
* Response: ``{"name": <path>, "ok": true, "n_functions": …, "nondeterministic":
  […], "names": […]}`` — or ``{"name": <path>, "ok": false, "reason": …}``.
* A ``{"ready": true}`` line is emitted once the parser stack is imported.

Any crash inside the worker kills only the worker; the driver notices the pipe
closing and falls back to seeded counts for good.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bluet.agents.analyzer import parser_for_source


def _emit(obj: dict[str, object]) -> None:
    print(json.dumps(obj), flush=True)


def main() -> int:
    _emit({"ready": True})
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        name = line.strip()
        if not name:
            continue
        try:
            path = Path(name)
            parser = parser_for_source(None, path.name)
            if parser is None:
                _emit({"name": name, "ok": False, "reason": "no-parser"})
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            logic = parser.parse(source)
            _emit(
                {
                    "name": name,
                    "ok": True,
                    "n_functions": len(logic.functions),
                    "nondeterministic": [
                        fn.name for fn in logic.functions if fn.nondeterministic_calls
                    ],
                    "names": [fn.name for fn in logic.functions],
                }
            )
        except Exception as exc:  # noqa: BLE001 - degrade to seeded counts
            _emit({"name": name, "ok": False, "reason": repr(exc)})


if __name__ == "__main__":
    sys.exit(main())