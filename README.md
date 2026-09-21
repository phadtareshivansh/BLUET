# bluet

Agentic incremental refactoring toolbox.

![Status](https://img.shields.io/badge/Status-Phase%200-yellow)

Built with a langgraph-driven agent pipeline (analyzer → refactor → verifier), tree-sitter analysis, a
SQLAlchemy-backed context store, guardrails, an isolated sandbox, and a pygls LSP surface.

## Latency NFR (Moss context retrieval)

PRD Section 13's success metric is **average** context-query latency. The strict bar we
gate on is **p95 < 3ms**; the measured **mean** is surfaced explicitly for PRD text parity,
alongside p50/p95/p99. All numbers are **measured** — never interpolated — and only the real
Moss backend is a benchmark subject (the in-memory store never is).

```sh
uv sync --extra moss          # install the real Moss SDK
export MOSS_PROJECT_ID=...    # from moss.dev portal
export MOSS_PROJECT_KEY=...
make bench-moss               # indexes 50 synthetic functions, runs ~30 queries, prints mean/p50/p95/p99
```

Without the SDK or credentials the benchmark **loud-skips** with setup instructions — it never
silently passes. On a budget miss it **fails loudly** with the real measured numbers. Moss
advertises a sub-10ms p99 design target; our 3ms gate is intentionally stricter, and an honest
miss here is the signal to revisit the integration approach, not to weaken the test.

## Moss integration (context store)

`bluet.context_store.MossContextStore` drives the **first-party async Python SDK** (`pip
install moss`) directly — no subprocess/RPC layer, no hand-rolled Rust FFI. Moss's Python SDK
already wraps its Rust core (`inferedge-moss-core`), is async-first, and is the documented way
in, so a Python-side bridge would only duplicate a maintained API we can `await` uniformly.
Indexing uses Moss's **real-time `SessionIndex`** (`await client.session(...)` + `add_docs`),
which embeds locally (~1-5ms) — the hot path the PRD measures — with cloud persistence
available later via `push_index`/`create_index`. The `ContextStore` ABC and every backend are
`async def` from day one: one uniform `await` at every call site, no sync/async dispatch.

## License gate: Moss SDK is PolyForm Shield

The `moss` package ships under **PolyForm Shield 1.0.0** — source-available, **not**
open-source, and it **restricts competing commercial uses**. It is fine for internal/testing
and OSS-only products, but it must be reviewed by whoever owns licensing/compliance **before
it becomes load-bearing in a shipped product**. Moss is isolated behind the `bluet[moss]`
optional extra precisely so the default install never pulls it in. See the module docstring
in `src/bluet/context_store/moss.py`.

## Latency NFR — context retrieval under 3 ms

PRD Section 13's success metric is **average** context-query latency. `bluet`
gates on the **stricter** p95 < 3 ms and surfaces the mean (plus p50/p95/p99)
so it can be audited against the PRD text directly. Numbers are always
**measured**, never interpolated, and only the real Moss backend is the
benchmark subject — the in-memory store is never measured.

Run it (real SDK + credentials required):

```sh
uv sync --extra moss            # install the moss SDK (PolyForm Shield — see License gate)
export MOSS_PROJECT_ID=...      # from the moss.dev portal
export MOSS_PROJECT_KEY=...
make bench-moss                 # indexes ~50 synthetic functions, reports mean/p50/p95/p99
```

Without the SDK or credentials the benchmark **loud-skips** with setup
instructions — it never quietly passes. On a budget miss it **fails loudly**
with the real measured numbers in the assertion message.

## Moss integration (context store)

`bluet.context_store.MossContextStore` drives the **first-party async Python
SDK** (`pip install moss`) directly — no subprocess/RPC layer and no hand-rolled
Rust FFI. Moss's Rust core is already wrapped by that SDK
(`inferedge-moss-core` wheels), so a Python-side bridge would only duplicate a
maintained, versioned API that we can `await` uniformly. The store uses Moss's
documented **real-time session** model (`SessionIndex`): `index_logic_spec`
writes one document per analyzed function (~1–5 ms/add_docs, matching the
hot path), `query_context` reads from the same session. Cloud persistence
(`create_index`/`load_index`/`push_index`) is the documented durability path
when we need it. The `ContextStore` ABC and every backend are `async def` — one
uniform `await at every call site, no sync/async dispatch.

## License gate — PolyForm Shield (Moss SDK)

The `moss` package ships under **PolyForm Shield 1.0.0**: source-available,
**not** open-source, and it **restricts competing commercial uses**. It's fine
for internal/testing use and for open-source-only products, but **must be reviewed
by whoever owns licensing/compliance before it becomes load-bearing in a shipped
product**. `bluet[moss]` is an optional extra on purpose — the default install
never pulls it in, and nothing outside the real-SDK backend imports it.