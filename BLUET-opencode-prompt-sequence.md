# BLUET Build Sequence — Prompts for opencode

**How to use this doc:** Run these prompts in opencode **in order**. Each one assumes the previous prompts' output already exists in the repo. Do not skip ahead — later prompts depend on interfaces created earlier. After each prompt, check the "Verify before continuing" line before moving on. If opencode's output doesn't meet it, iterate on that prompt before starting the next one.

**Locked stack (paste this as a pinned reference / CLAUDE.md-equivalent if opencode supports one):**
- Orchestration: LangGraph (Python), stateful cyclic graphs
- Messaging: ZeroMQ (pyzmq) + asyncio event bus
- Agents: Pydantic-AI for structured LLM output
- Context/retrieval: Moss (`@moss-dev/moss`) — sub-3ms hybrid vector+AST search
- Guardrails: Enkrypt inline proxy (prompt injection, secrets, OWASP checks)
- Parsing: Tree-sitter (via tree-sitter-language-pack) + ANTLR4
- Verification: Z3 Solver, Hypothesis (Python), fast-check (TS)
- State: SQLite via SQLAlchemy 2.0 (`.bluet/state.db`)
- Sandbox: Docker + gVisor (`runsc`) preferred; falls back to hardened standard Docker (`--network none --read-only --cap-drop=ALL --memory=2g`) with a visible warning when gVisor specifically is missing but Docker works. Hard-fails only when no execution path exists at all — Docker itself unreachable, or Windows with no WSL2.
- CLI: Typer + Rich
- LSP: pygls
- Local inference: Ollama / vLLM
- Package management: `uv` primary, `pip`/`pyproject.toml` compatible as fallback
- Target platforms: Linux, macOS native; Windows via WSL2

---

## Phase 0 — Repo Scaffold & Environment

### Prompt 0.1 — Project skeleton
```
Create a new Python project called "bluet" using a src-layout package structure. 
Requirements:
- pyproject.toml configured for uv (uv.lock) with pip fallback compatibility — no setup.py.
- Python 3.11+ minimum.
- Package structure:
  bluet/
    cli/
    orchestrator/
    agents/
      analyzer/
      refactor/
      verifier/
    context_store/
    guardrails/
    sandbox/
    state/
    lsp/
  tests/
- Add dependency groups in pyproject.toml (not yet installed, just declared) for: 
  langgraph, pydantic-ai, pyzmq, tree-sitter, tree-sitter-language-pack, typer, rich, 
  sqlalchemy>=2.0, pygls, pytest, hypothesis. Use tree-sitter-language-pack, not 
  tree-sitter-languages — the latter is unmaintained and its bundled grammar binaries hit 
  an ABI mismatch crash against current tree-sitter versions; tree-sitter-language-pack is 
  the maintained successor built to track current ABI ranges. Do NOT add alembic here — 
  Prompt 1.1 uses plain create_all() for now and defers Alembic to Phase 4, so it shouldn't 
  sit unused in the dependency list for three phases first.
- Add a Makefile or justfile with targets: install, test, lint, run.
- Add a .gitignore appropriate for Python + any Rust/TS bindings later.
- Add a README stub with project name, one-line description, and a "Status: Phase 0" badge.
Do not implement any logic yet — this is scaffolding only. After creating it, run `uv sync` 
(or equivalent) and confirm the environment installs cleanly.
```
**Verify before continuing:** `uv sync` (or `pip install -e .`) completes with no errors; `pytest` runs (even with zero tests) without import errors.

### Prompt 0.2 — Platform detection & WSL2 gate
```
In bluet/sandbox/, create a platform.py module that:
- Detects the host OS (Linux, macOS, Windows) at runtime.
- On Windows, checks whether WSL2 is installed and has a running distro accessible via `wsl.exe`.
- Exposes a function `get_invocation_mode()` returning "native" (Linux), "docker-desktop" 
  (macOS — commands run directly since Docker Desktop's Linux VM is transparent to the 
  caller), or "wsl2" (Windows — commands must route through `wsl.exe docker ...` rather 
  than calling `docker` directly). This function answers ONLY "how do I invoke Docker/shell 
  commands on this OS" — it does NOT determine whether gVisor is actually available, which 
  is a separate concern handled by `bluet doctor` in Prompt 0.3. Do not bake "gvisor" into 
  these return values — a Linux box can easily be missing `runsc` too, so the invocation 
  mode and the isolation backend are two independent axes and must stay named that way.
- If on Windows and WSL2 is not available, raise a clear, actionable BluetEnvironmentError 
  explaining that gVisor requires WSL2 on Windows, with a one-line instruction on how to 
  install it. Do NOT fall back to unsandboxed execution — fail loudly instead. (This case 
  alone is a hard fail because without WSL2 there's no Linux kernel path for Docker itself 
  to run through on Windows — unrelated to whether gVisor specifically is installed.)
Write unit tests that mock each OS case (use monkeypatch/mock, not actual OS detection) 
and confirm all three invocation-mode paths and the Windows-no-WSL2 failure path.
```
**Verify before continuing:** Tests pass for all 4 scenarios (Linux, macOS, Windows+WSL2, Windows without WSL2). Note: this hard-fail is specifically for "no Linux kernel path exists at all" (Windows without WSL2) — Prompt 0.3 handles the more common case of Docker being fine but gVisor specifically missing, which gets a graceful fallback, not a hard fail.

### Prompt 0.3 — `bluet doctor`: Docker & gVisor preflight
```
In bluet/cli/, implement `bluet doctor` as a standalone diagnostic command (also auto-run 
non-blockingly at the start of `bluet run`) that:
1. Checks Docker daemon status via `docker info`. If Docker isn't running/installed, report 
   this clearly with an install link — BLUET cannot execute sandboxed verification at all 
   without Docker, so this alone should block `bluet run` (not just warn).
2. If Docker is fine, test gVisor availability with a dry run: 
   `docker run --rm --runtime=runsc hello-world`.
3. Cache the result to a small JSON file (~/.bluet/doctor.json by default, path 
   overridable via BLUET_DOCTOR_CACHE) with shape {backend, checked_at, docker_ok, 
   gvisor_ok}. This is a lightweight, disposable cache — no SQLAlchemy/schema needed yet, 
   since the Job model doesn't exist until Prompt 1.1. If gVisor is available, backend="gvisor".
4. If gVisor is NOT available (but Docker is), do NOT hard-fail. Instead:
   - Fall back to hardened standard Docker: `--network none --read-only --cap-drop=ALL 
     --memory=2g` (make these limits configurable, these are sane defaults).
   - Print a clear terminal warning that verification will run with a weaker isolation 
     boundary than gVisor provides, and set backend="hardened-docker" in the cache file.
5. On Windows, route the `docker info` and `docker run --runtime=runsc` checks through the 
   invocation mode confirmed in Prompt 0.2 (i.e. via `wsl.exe docker ...`), not directly — 
   this is the OS invocation axis, separate from the gvisor/hardened-docker isolation 
   decision this prompt is making.
Print a summary table (Rich) of Docker status, gVisor status, backend selected, and resource 
limits in effect. Write tests mocking each combination (Docker down, Docker up + gVisor 
available, Docker up + gVisor missing) and confirm the correct backend, cache file contents, 
and exit behavior for each.
```
**Verify before continuing:** All three states report correctly and the "gVisor missing" case genuinely falls back rather than crashing; the "Docker missing" case genuinely blocks `bluet run` rather than proceeding with no isolation at all; the cache file is written correctly in all three cases.

---

## Phase 1 — Core Engine

### Prompt 1.1 — SQLite state layer
```
In bluet/state/, implement the persistence layer using SQLAlchemy 2.0 (async engine).
Create models for:
- Job (id, repo_path, status, backend, created_at, updated_at) — backend is populated from 
  the doctor cache written in Prompt 0.3 (~/.bluet/doctor.json): when a Job is created, read 
  that cache (re-running `bluet doctor`'s check inline if the cache is missing or stale) and 
  stamp the resulting value ("gvisor" or "hardened-docker") onto the Job row. This is what 
  makes the backend visible later via `bluet status` (Prompt 1.6) without needing a separate 
  status mechanism.
- AgentEvent (id, job_id FK, agent_name, event_type, payload_json, timestamp)
- ParityScore (id, job_id FK, module_path, score, verified_at)
Store the DB at .bluet/state.db relative to the target repo being refactored.
Schema management: use a simple init_schema() that calls Base.metadata.create_all() on the 
async engine — no Alembic yet. This is a prototype-stage schema that will still churn; 
Alembic's migration overhead would mostly be discarded right now, and a schema change just 
means deleting and recreating .bluet/state.db during development. Revisit this in Phase 4 
(Enterprise Hardening) once the schema has stabilized and/or real job history exists that a 
destructive reset would actually lose — that's the signal to introduce Alembic, not a fixed 
prompt number.
Write a repository/DAO layer with functions: create_job, update_job_status, log_event, 
record_parity_score, get_job_by_id. Add unit tests using an in-memory SQLite DB (aiosqlite) 
that verify each DAO function round-trips correctly.
```
**Verify before continuing:** Tests pass; a throwaway script can create a job, log 3 events, and read them back correctly.

### Prompt 1.2 — ZeroMQ event bus
```
In bluet/orchestrator/, implement an async event bus using pyzmq (PUB/SUB pattern) that:
- Defines topics: task.analysis, task.refactor, task.verify, feedback.regression
- Exposes an EventBus class with async publish(topic, payload: dict) and 
  async subscribe(topic, handler: Callable) methods
- Payloads are JSON-serialized and include job_id (required in the payload dict passed to 
  publish() — not a constructor default; the bus itself is job-agnostic infrastructure) and 
  a timestamp. publish() should raise ValueError immediately if job_id is missing from the 
  payload, rather than letting a null FK reach the DB layer.
- Runs on asyncio, non-blocking — verify a slow subscriber on one topic does not block 
  publishing to another topic
- Every published event is also persisted via the log_event DAO from Prompt 1.1, as a 
  fire-and-forget asyncio task (publish() does not await the DB write) — this keeps publish() 
  non-blocking per the PRD's requirement that the bus prevent UI blocking during heavy 
  verification runs. This is safe to do loosely because AgentEvent is an audit trail, not the 
  resumability mechanism (that's LangGraph's SQLite checkpointer from Prompt 1.3) — losing a 
  log row to a crash between flushes is an acceptable gap, but a flush failure must be 
  logged/surfaced somewhere visible (not silently swallowed) so persistence issues don't go 
  unnoticed.
Write an integration test that starts the bus, subscribes two handlers to different topics, 
publishes to both, and confirms both handlers fire and events land in SQLite. Also test: 
publish() with a missing job_id raises immediately, and a simulated DB flush failure logs an 
error without crashing publish() or blocking other topics.
```
**Verify before continuing:** Integration test passes; manually run two subscribers in one test and confirm no blocking/deadlock under a simulated slow handler (e.g. asyncio.sleep in one handler); confirm a forced persistence failure is visibly logged rather than silent.

### Prompt 1.3 — LangGraph orchestrator skeleton
```
In bluet/orchestrator/, build the LangGraph StateGraph that will drive a refactor job.
Define a BluetState TypedDict with fields: job_id, repo_path, target_language, 
current_file, logic_spec (dict|None), proposed_code (str|None), parity_result (dict|None), 
retry_count (int).
Create graph nodes as stubs (no real logic yet, just pass-through functions that log via 
the EventBus from Prompt 1.2):
- analyze_node
- refactor_node  
- verify_node
- self_heal_node (routes back to refactor_node on failure, increments retry_count)
Define MAX_RETRIES = 3 as a named constant. retry_count counts *retries*, not total verify 
attempts — so this yields 1 initial verify attempt plus up to MAX_RETRIES self-heal/retry 
cycles, i.e. 4 total verify attempts in the worst case, with retry_count reaching 3 at the 
final failure. Wire the graph: analyze -> refactor -> verify -> (conditional: success -> END, 
failure + retry_count < MAX_RETRIES -> self_heal -> refactor, 
failure + retry_count >= MAX_RETRIES -> END with failure status).
Persist graph checkpoints using LangGraph's SQLite checkpointer pointed at .bluet/state.db 
so a job can pause and resume.
Write a test that runs the full stub graph end-to-end (mock verify_node to always fail) and 
asserts exactly: 4 verify_node calls, 3 self_heal_node calls (regression events), and a 
final retry_count of 3 before the graph terminates in the failure state.
```
**Verify before continuing:** Test confirms the graph terminates in both the success path and the max-retries failure path, and that a job can be resumed from a checkpoint after simulating a process restart.

### Prompt 1.4 — Analyzer Agent (real logic, no LLM yet)
```
In bluet/agents/analyzer/, implement the Analyzer Agent's core parsing logic:
- Use tree-sitter (via tree-sitter-language-pack — not tree-sitter-languages, which is 
  unmaintained and its precompiled grammars hit an ABI mismatch against current tree-sitter; 
  see Prompt 0.1) to parse a given source file.
- Support TWO languages from the start: Python (Python 2 syntax where it differs from 
  Python 3 should still parse — note any tree-sitter-python limitations you hit) and Java 8 
  (via tree-sitter-java). Structure the parser as a per-language plugin behind a common 
  interface (LanguageParser ABC with a parse(source) -> LogicSpec method) so Phase 4's 
  COBOL addition slots in the same way rather than requiring a rewrite.
- Extract: function/method definitions, control-flow graph (branches, loops), variable/field 
  mutations, and I/O calls (file, network, raw JDBC for Java) as a flagged 
  "non-deterministic" list.
- Output this as a structured LogicSpec Pydantic model with this shape:
  - functions: list[FunctionDef], where each FunctionDef carries name, line, params, 
    returns, inputs, outputs, state_mutations, branches, and nondeterministic_calls scoped 
    to that one function/method. This is the real unit of work — Verification Agent test 
    generation (Prompt 2.3) needs per-function signatures to generate meaningful Hypothesis 
    inputs, and self-heal counter-examples (Prompt 2.3) need to target a specific function, 
    not the whole file, or the self-heal loop can't act precisely.
  - Flat top-level fields (inputs, outputs, state_mutations, branches, 
    nondeterministic_calls) that are DERIVED by aggregating across functions, not 
    independently populated — these exist purely as a file-level convenience view (e.g. for 
    `bluet diff`'s quick non-deterministic-call summary in Prompt 2.6), computed from 
    `functions` rather than tracked separately, so the two views can't drift out of sync.
  - This is the object that fills BluetState.logic_spec from Prompt 1.3.
  - Conversion contract: BluetState.logic_spec is typed as `dict` (for LangGraph's SQLite 
    checkpoint serialization — Pydantic objects aren't checkpoint-safe as-is), so 
    analyze_node stores `logic_spec.model_dump()`, and any downstream node that needs the 
    real object (refactor_node in 2.2, verify_node in 2.3) reconstructs it via 
    `LogicSpec.model_validate(state["logic_spec"])`. Centralize this in one small helper 
    (e.g. a `get_logic_spec(state)` accessor) rather than repeating the validate call at 
    every call site.
- Wire this into analyze_node, replacing the stub.
Bundle two fixture sets under tests/fixtures/:
- python-legacy/ — 3 Python 2 files of increasing complexity (simple function, function 
  with a loop and branch, function with a file I/O call)
- java-legacy/ — 3 standalone Java 8 classes: one with state-mutating billing-style logic, 
  one with raw JDBC calls, one using untyped/raw Map types
Write tests asserting correct, structurally valid LogicSpec output for all 6 fixtures.
```
**Verify before continuing:** All 6 fixtures (3 Python, 3 Java) produce correct LogicSpec output; `bluet` can now genuinely analyze a real file in either language and log a real event to the bus and DB. Confirm with me which language is the priority for Phase 2's Refactor/Verification agents if time is tight — building both analyzer paths doesn't mean both need synthesis+verification depth simultaneously.

### Prompt 1.5 — Moss context store integration
```
In bluet/context_store/, integrate Moss (@moss-dev/moss) as the hybrid vector+AST search 
layer:
- Since Moss is a TypeScript/Rust-bound package, set up the Python<->Moss bridge (via 
  a subprocess/RPC layer, or Rust bindings if available — pick whichever integration path 
  Moss actually exposes, and document which one you used and why).
- Define the ContextStore ABC's methods (index_logic_spec, query_context) as `async def` 
  from the start, even for an in-memory default implementation that does no real I/O — the 
  real Moss SDK is inherently I/O-bound and needs genuine wall-clock async measurement, so a 
  sync ABC here would force dispatch logic (checking which kind of store you got) into every 
  future call site instead of a uniform `await store.query_context(...)` everywhere. Don't 
  make this decision sync-first and retrofit later; it's a one-line difference now and a 
  real refactor later.
- On analyze_node completion, index the file's AST nodes and a text embedding of each 
  function into Moss.
- Expose a query_context(query: str, top_k: int) function used later by the Refactor Agent.
- Add a benchmark test that indexes ~50 synthetic functions and asserts query latency is 
  under 3ms per the PRD's non-functional requirement — if it's not achievable with the 
  current integration approach, report the actual latency honestly and flag it rather than 
  silently passing the test. Report the actual mean/average latency explicitly alongside 
  p50/p95/p99 — the PRD's literal stated success metric (Section 13) is average latency, 
  not p95, so gate on whichever stricter bar you choose internally but surface the average 
  too for anyone auditing against the PRD text directly.
Note: Moss ships under a PolyForm Shield license (source-available, not open-source — 
restricts competing commercial uses). Flag this to whoever owns licensing/compliance before 
it becomes load-bearing in a shipped product; not a code concern, but worth surfacing now 
rather than after it's deeply integrated.
```
**Verify before continuing:** Indexing and querying work end-to-end against real data from Prompt 1.4's fixtures; latency numbers are reported honestly (flag to me directly if the <3ms target isn't met — this is a PRD success metric worth knowing about early).

### Prompt 1.6 — CLI: `bluet run` (Phase 1 slice)
```
In bluet/cli/, using Typer + Rich, implement:
- `bluet run <path>` — creates a Job (via state DAO), kicks off the LangGraph orchestrator 
  on the given file/directory, shows a live Rich spinner/progress reflecting EventBus 
  messages as they arrive (subscribe to all topics, update a status line per file).
- `bluet status <job_id>` — reads job + latest events from SQLite, prints a summary table.
- At this phase, `bluet run` should genuinely run analyze_node (real) and stub 
  refactor/verify nodes, so the user sees real AST/logic-spec output printed as colorized 
  JSON or a tree view.
Write a CLI integration test (using Typer's CliRunner) that runs `bluet run` against a 
fixture file and asserts it exits 0 and status shows a completed analysis.
```
**Verify before continuing:** You can run `bluet run` against one of the `python-legacy/` fixtures bundled in Prompt 1.4 from a terminal and watch it actually analyze the file, not just print stub output. This is the Phase 1 milestone — confirm this works before touching Phase 2.

---

## Phase 2 — Synthesis & Verification

### Prompt 2.1 — Local LLM client & auto-provisioning
```
In bluet/agents/ (shared module, e.g. llm_client.py), implement a thin client for local 
inference supporting both Ollama and vLLM backends:

1. Non-blocking health check on startup against http://localhost:11434 (Ollama) or 
   http://localhost:8000 (vLLM), selected via BLUET_LLM_BACKEND env var (default: try 
   Ollama first, fall back to vLLM if Ollama isn't reachable).

2. Hardware detection: read available VRAM (via nvidia-smi/pynvml if present, else 
   report none) and system RAM (via psutil). On Apple Silicon (detect via 
   platform.machine() == "arm64" and platform.system() == "Darwin"), there is no discrete 
   VRAM to detect — it's unified memory shared between CPU and GPU — so do NOT fall through 
   to the generic "no GPU detected" dev-mode bucket by default. Instead, size the tier off 
   total system RAM (psutil) directly for this case, since RAM *is* the relevant capacity 
   here: e.g. 32GB+ unified memory -> mid tier, 16-24GB -> small tier, <16GB -> the smallest 
   viable model. Without this branch, every Mac without an eGPU — whether 8GB or 128GB of 
   unified memory — lands in the same dev-mode bucket, which is wrong in both directions 
   (under-serving a capable machine, or recommending a model too large for a small one).
   Map non-Apple-Silicon hardware to a tier:
   - 24GB+ VRAM -> qwen2.5-coder:32b-instruct-q4_K_M
   - 16-20GB VRAM -> qwen2.5-coder:14b-instruct-q4_K_M
   - <16GB VRAM / no GPU detected (dev mode) -> qwen2.5-coder:7b or deepseek-coder-v2:16b
   Make this table config-overridable (BLUET_LLM_MODEL forces a specific model regardless 
   of detected hardware).

3. On startup, if the backend is reachable, query its available models. If the 
   hardware-appropriate model isn't present, prompt interactively (Rich Confirm) offering 
   to auto-pull it, showing a live download progress bar (Ollama's /api/pull streams 
   progress — parse and render it), or to proceed with a lighter already-available model 
   instead. Support a --yes/-y flag to auto-accept the pull for non-interactive/CI use.

4. Standard chat-completion interface: complete(messages, response_model: type[BaseModel]) 
   using Pydantic-AI to force structured output matching response_model.

5. If the backend is unreachable entirely, fail with a clear, actionable error (don't let 
   a job hang) telling the user to start Ollama/vLLM, with the exact command to do so.
   If the interactive pull/fallback prompt (step 3) can't run because stdin isn't a TTY 
   (Confirm.ask raises EOFError in non-interactive sessions), catch it, fall back to the 
   lighter already-available model, and log a visible warning explaining why — never let 
   this fallback happen silently, since a user running non-interactively should still know 
   their preferred model was skipped.
6. Use a shared exception base (e.g. BluetLLMError) with distinct siblings for distinct 
   failure modes — connectivity/health-check failure (LLMUnavailableError) vs a response 
   that arrived but failed structured-output validation (LLMSchemaOutputError). Don't nest 
   one under the other by name; if something downstream ever wants to retry on schema 
   failures but not connectivity failures (plausible for self-heal in Prompt 2.2), a shared 
   parent keeps that filtering clean instead of one except clause accidentally catching both.

Write tests using a mocked HTTP layer (no real Ollama/vLLM instance required in CI) covering: 
backend unreachable, backend reachable + model present, backend reachable + model missing 
+ user accepts pull, backend reachable + model missing + user declines and falls back to a 
lighter model, and the --yes non-interactive path.
```
**Verify before continuing:** All mocked scenarios pass. If you have a real local Ollama instance, manually run through the interactive pull flow once to confirm the progress bar and prompts actually feel usable, not just functionally correct.

### Prompt 2.2 — Refactor Agent (Python 2 → target language)
```
Scope: build and prove this agent against Python 2 fixtures ONLY for now. Java 8 uses the 
same interface but gets wired in later (Prompt 2.6) once this pattern is validated — don't 
split effort across both languages yet.

In bluet/agents/refactor/, implement the real Refactor Agent:
- Takes a LogicSpec (from Prompt 1.4) and target_language, uses query_context (Moss, 
  Prompt 1.5) to pull relevant existing-codebase context, and calls the LLM client 
  (Prompt 2.1) via Pydantic-AI to synthesize target code as a structured 
  ProposedCode model (file_path, code, imports_added, notes).
- Use Jinja2 templates for boilerplate scaffolding (module headers, import blocks) so the 
  LLM only fills in logic, not repetitive structure.
- After generation, run the appropriate formatter (gofmt/rustfmt/prettier depending on 
  target_language) as a subprocess and fail gracefully with a clear error if the formatter 
  isn't installed, rather than crashing.
- Wire this into refactor_node from Prompt 1.3, replacing the stub.
- On the self-heal path (counter-examples from Verification Agent, once it exists in 2.3), 
  accept an optional counter_examples param and include it in the prompt as context for 
  correction.
- Self-heal scope, decided explicitly rather than left ambiguous: regenerate the ENTIRE 
  file on each self-heal pass, not an incremental per-function patch. Simpler for a 
  prototype and avoids building merge logic to splice a regenerated function back into an 
  existing file. counter_examples still carries function_name (see Prompt 2.3's 
  CounterExample model) so the LLM knows precisely what to fix even though the whole file 
  gets resynthesized. Revisit incremental per-function patching later if whole-file 
  regeneration proves unreliable or slow at scale — larger multi-method Java classes in 
  Prompt 2.6 are the most likely place this assumption gets tested.
Write tests with a mocked LLM client (deterministic fake output) that confirm the full 
pipeline: LogicSpec in -> formatted ProposedCode out.
```
**Verify before continuing:** Tests pass with the mocked LLM; if you have a real local model running, manually run one real end-to-end synthesis and sanity-check the output code by eye.

### Prompt 2.3 — Verification Agent (Python 2 → target language)
```
Scope: Python 2 fixtures only for now, matching Prompt 2.2's scope — Java 8 verification 
follows in Prompt 2.6.

In bluet/agents/verifier/, implement the Verification Agent:
- Property-based test generation using Hypothesis (Python targets) / fast-check (TS targets), 
  generated per-function by iterating `logic_spec.functions` (Prompt 1.4) and using each 
  FunctionDef's own inputs/outputs/branches — NOT the flat aggregate view on LogicSpec, 
  which loses the per-function signature Hypothesis needs to generate valid inputs.
- Run the legacy code and the proposed modern code side-by-side against the same generated 
  inputs, inside the sandbox execution backend from Phase 0 (bluet/sandbox/platform.py) — 
  for this prompt you can stub the sandbox call itself (subprocess.run locally) since gVisor 
  wiring is Phase 4; just define the interface now so Phase 4 can slot in.
- Compare outputs for exact parity; on mismatch, produce a structured CounterExample model 
  (function_name, input, expected_output, actual_output, diff_summary) — function_name is 
  required, not optional: it's what lets self-heal (Prompt 2.2) tell the LLM precisely which 
  function's logic is wrong, even though the current self-heal scope regenerates the whole 
  file.
- Add a Z3 Solver pass for a specific subset first: numeric boundary conditions and simple 
  branch-coverage constraints (not full program equivalence — that's out of scope for one 
  prompt; note in code comments what Z3 is and isn't covering here).
- Wire into verify_node, replacing the stub, and wire failures to feed CounterExample lists 
  back into self_heal_node -> refactor_node per the LangGraph design from 1.3.
Write tests with a deliberately buggy "proposed code" fixture and confirm the agent catches 
the mismatch and produces a correct CounterExample.
```
**Verify before continuing:** The full self-healing loop now works end-to-end with real (not stubbed) analyze -> refactor -> verify -> counter-example -> re-refactor, at least for simple functions. This is the PRD's core "zero-regression" claim — test it thoroughly before moving on.

### Prompt 2.4 — Enkrypt guardrails integration
```
In bluet/guardrails/, integrate Enkrypt as an inline proxy that inspects:
- Inputs to the Refactor Agent (check for anything resembling hardcoded secrets or 
  sensitive data in the legacy code being sent to the local LLM, even though it's local-only 
  — this is a defense-in-depth check per the PRD's security requirements)
- Outputs from the Refactor Agent (scan generated code for OWASP-flagged patterns before 
  it's written to disk)
Wire this as a pre/post hook around refactor_node: if Enkrypt flags something, log it as a 
GuardrailEvent via the EventBus and halt that job with a clear status rather than silently 
proceeding, requiring explicit user confirmation via CLI to override and continue.
Write tests with fixture inputs that should and shouldn't trigger flags, confirming the halt 
behavior and that overriding via CLI flag works.
```
**Verify before continuing:** A deliberately planted "fake secret" in a fixture file correctly halts the job; overriding proceeds only with explicit CLI confirmation.

### Prompt 2.5 — CLI: `bluet diff`
```
In bluet/cli/, implement `bluet diff <job_id>` showing a colorized side-by-side AST/code 
diff (Rich's diff rendering or a custom colorizer) between legacy and proposed code for a 
completed job, plus the parity score and any remaining counter-examples if verification 
failed. Add a `--export` flag to write the diff to a file.
```
**Verify before continuing:** Run the full pipeline end-to-end on a real fixture (`bluet run` then `bluet diff`) and confirm the output is genuinely readable and useful, not just technically correct.

### Prompt 2.6 — Extend Refactor & Verification to Java 8
```
Now that the Python 2 path (Prompts 2.2-2.5) is proven end-to-end, extend the Refactor 
Agent and Verification Agent to handle the Java 8 fixtures bundled back in Prompt 1.4 
(state-mutating billing logic, raw JDBC calls, untyped Map usage):
- Refactor Agent: add Java 8 -> target-language prompt templates and Jinja2 boilerplate; 
  reuse the same LLM client, Moss context lookup, and formatter-subprocess pattern from 2.2 
  — do not rebuild the pipeline, just add the Java branch.
- Verification Agent: add fast-check-based property test generation for the TS-targeted 
  side of Java parity checks (per the PRD's fast-check reference for TS verification), 
  reusing the same side-by-side execution and CounterExample model from 2.3.
- Pay particular attention to JDBC calls and raw Map/untyped-collection usage — these are 
  exactly the "non-deterministic" and "type coercion" risk categories the PRD calls out, so 
  don't let the LLM silently paper over them; they should surface as flagged items in the 
  LogicSpec and be visible in `bluet diff` output.
Write tests using the java-legacy/ fixtures confirming the same self-healing loop 
(synthesize -> verify -> counter-example -> re-synthesize) works for Java as it does for 
Python.
```
**Verify before continuing:** Run `bluet run` against all 3 Java fixtures end-to-end. Compare how much the self-healing loop had to iterate versus the Python fixtures — if Java needs meaningfully more retries, that's useful signal for how Phase 4's COBOL work will go, worth noting before moving on.

---

## Phase 3 — Developer Experience

### Prompt 3.1 — LSP server
```
In bluet/lsp/, using pygls, implement a BLUET Language Server Protocol server that:
- Supports textDocument/codeAction to trigger a refactor job on the open file
- Sends custom notifications (bluet/parityStatus) with live status as the LangGraph job 
  progresses, reusing the EventBus subscription pattern from the CLI
- Provides inline diagnostics showing which functions have unresolved counter-examples
Write an LSP integration test using pygls's test client that opens a fixture file, triggers 
the code action, and confirms status notifications arrive in order.
```
**Verify before continuing:** Test passes; if feasible, manually confirm it connects from a real VS Code instance with a minimal client config.

### Prompt 3.2 — VS Code extension shell
```
Create a minimal VS Code extension (TypeScript) in a vscode-extension/ directory that 
connects to the BLUET LSP server from 3.1, shows the custom parityStatus notifications in 
the status bar, and renders side-by-side diffs using VS Code's native diff view. Keep this 
minimal — status bar + diff view only, no custom webview UI yet.
```
**Verify before continuing:** Extension loads in the VS Code extension host, connects to a running `bluet` LSP instance, and shows live status during a `bluet run`.

---

## Phase 4 — Enterprise Hardening

### Prompt 4.1 — Real gVisor/Docker sandbox wiring
```
Replace the stubbed sandbox call in the Verification Agent (Prompt 2.3) with real execution. 
Two independent decisions feed this, and they should stay separate rather than merged into 
one "backend" concept:
- The invocation mode from bluet/sandbox/platform.py (Prompt 0.2 — native / docker-desktop 
  / wsl2): HOW to run Docker/shell commands on this OS.
- The isolation backend from `bluet doctor` (Prompt 0.3, stored on the Job in Prompt 1.1 — 
  gvisor / hardened-docker): WHICH container runtime flags to use.
- Build a minimal Docker image per target language runtime needed for side-by-side execution
- Use docker-py to run legacy and modern code in separate containers, using whichever 
  isolation backend doctor selected: --runtime=runsc if backend="gvisor", or the hardened 
  flags (--network none --read-only --cap-drop=ALL --memory=2g) if backend="hardened-docker"
- CPU/RAM caps are config-driven regardless of which backend is active
- On Windows, route container execution through the wsl2 invocation mode confirmed in 
  Prompt 0.2 — orthogonal to which isolation backend is active
- The Job's `backend` field (populated at job creation in Prompt 1.1 from the doctor cache) 
  already tells you which mode to run in — read it rather than re-querying Docker/gVisor 
  mid-verification. If a job somehow reaches this point with a stale/missing backend value, 
  re-run the doctor check inline as a safety fallback.
Write an integration test (may require Docker running locally — mark it as such / skippable 
in CI if Docker isn't available) that runs a real fixture through both backend paths 
(mocking gVisor absence for one run) and confirms resource caps are enforced and containers 
are torn down after execution in both cases.
```
**Verify before continuing:** Real sandboxed side-by-side execution works on your actual dev machine under whichever backend is available; confirm containers don't leak/persist after a job completes or fails, and that the active backend is visibly surfaced to the user, not just logged internally.

### Prompt 4.2 — COBOL support
```
Java 8 is already handled (Prompts 1.4 and 2.6) — this prompt adds the last legacy language 
named in the PRD: COBOL.

Extend the Analyzer Agent's LanguageParser interface (Prompt 1.4) with a COBOL implementation 
via an ANTLR4 grammar. Then extend the Refactor and Verification Agents (following the same 
pattern established in Prompt 2.6 for Java) to handle COBOL -> target-language synthesis 
and verification.
Note explicitly in code/docs where COBOL's semantics (fixed-point decimal arithmetic, 
GOTO-based control flow, PERFORM loops) require special-casing in the LogicSpec model versus 
the Python/Java path — this was flagged as an open risk in the PRD, so don't smooth over 
places where the mapping is genuinely lossy or uncertain.
Add fixtures and tests for at least one non-trivial COBOL program covering arithmetic and 
control flow.
```
**Verify before continuing:** Report back honestly on parity — COBOL's arithmetic and control-flow model is genuinely harder to map than Python's or Java's; flag anything that doesn't fully work rather than papering over it. This is the PRD's own stated open risk (Section 15), so a partial/imperfect result here is expected and should be documented, not hidden.

---

## After each phase
Before starting the next phase's prompts, run the full test suite (`pytest` / `make test`) 
and do one manual end-to-end `bluet run` against a real (not fixture) small legacy file. 
This catches integration drift that unit tests alone miss.