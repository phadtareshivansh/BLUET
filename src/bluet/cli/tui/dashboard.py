"""Pure rendering helpers for the BIOS dashboard — no Textual imports.

Everything here is a plain ``str``/Rich transformation, so the CRT aesthetic
(materialize-from-noise, glitch strips, block-font logotype, parity bars) and
the event-line formatter are unit-testable without launching a terminal.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from typing import Any

from rich.text import Text

# Palette (mirrors the BIOS stylesheet) — blue CRT.
MINT = "#58C7F3"
DIM = "#29608F"
AMBER = "#F6C945"
RED = "#D64545"
GREEN = "#4FD68A"

#: Alphabet the live-event log uses for guardrail/ambient glyphs.
NOISE_CHARS = "█▓▒░┃┆│╱╲"

#: 3-wide, 5-tall classic block font: "1" = inked pixel, "0" = empty.
_PIXEL_FONT: dict[str, list[str]] = {
    "B": ["111", "101", "111", "101", "111"],
    "L": ["100", "100", "100", "100", "111"],
    "U": ["101", "101", "101", "101", "111"],
    "E": ["111", "100", "111", "100", "111"],
    "T": ["111", "010", "010", "010", "010"],
    "-": ["000", "000", "111", "000", "000"],
    " ": ["000", "000", "000", "000", "000"],
}

#: Vertical four-station pipeline with the self-heal return lane on the right.
PIPELINE_TALL = [
    "┌─ANALYZE──┐",
    "│ {load:^7} │",
    "└────┬─────┘",
    "     ▼",
    "┌─SYNTHESIS┐◄──────────────┐",
    "│ {synth:^8} │              │",
    "└────┬──────┘              │",
    "     ▼     self.heal ≤ MAX │",
    "┌─VERIFY───┐               │",
    "│ {verify:^8} │───────────────┘",
    "└────┬──────┘",
    "     ▼",
    "┌─PARITY───┐",
    "│ {parity:^8} │",
    "└──────────┘",
]

#: Compact horizontal flow used when the panel is too short for the tall art.
PIPELINE_COMPACT = [
    "┌ANALYZ┐ ┌SYNTH┐ ┌VERIF┐ ┌PARI┐",
    "│{load:^5}│►│{synth:^4}│►│{verify:^5}│►│{parity:^4}│",
    "└──────┘ └─────┘ └──┬──┘ └─────┘",
    "                 retry≤{max} ────┘",
]


def pad_rows(rows: Sequence[str], width: int) -> list[str]:
    """Center ``rows`` into a fixed ``width``, trimming any overflow."""
    out: list[str] = []
    for row in rows:
        if len(row) >= width:
            out.append(row[:width])
            continue
        gap = width - len(row)
        left = gap // 2
        out.append(" " * left + row + " " * (gap - left))
    return out


def materialize(
    rows: Sequence[str],
    progress: float,
    rng: random.Random | None = None,
) -> list[str]:
    """Dissolve ``rows`` through a static-noise field toward the real diagram.

    ``progress`` sweeps 0 (pure texture) to 1 (fully resolved).
    """
    rng = rng or random.Random()
    keep = max(0.0, min(1.0, progress))
    out: list[str] = []
    for row in rows:
        chars: list[str] = []
        for ch in row:
            if ch.isspace() or rng.random() < keep:
                chars.append(ch)
            else:
                chars.append(rng.choice(NOISE_CHARS))
        out.append("".join(chars))
    return out


def apply_glitch(rows: Sequence[str], rng: random.Random | None = None) -> list[str]:
    """Slap 1–2 vertical glitch strips onto the diagram (vertical-bar texture)."""
    rng = rng or random.Random()
    out = [list(row) for row in rows]
    width = max(len(row) for row in out) if out else 0
    if width == 0:
        return list(rows)
    for _ in range(rng.randint(1, 2)):
        col = rng.randrange(width)
        if all(row[col].isspace() for row in out):
            continue
        for row in out:
            if col < len(row) and rng.random() < 0.7:
                row[col] = rng.choice(NOISE_CHARS)
    return ["".join(row) for row in out]


def parity_bar(score: float | None, width: int = 10) -> str:
    """Render a 0..1 ``score`` as a block-character bar cell by cell."""
    score = 0.0 if score is None else max(0.0, min(1.0, score))
    filled = round(score * width)
    cells = ["█" if i < filled else ("░" if i == filled else " ") for i in range(width)]
    return "".join(cells)


def _render_glyph(letter: str, dither: random.Random | None = None) -> list[str]:
    glyph = _PIXEL_FONT.get(letter, _PIXEL_FONT[" "])
    rows: list[str] = []
    for band in glyph:
        row = "".join("██" if p == "1" else "  " for p in band)
        rows.append(row)
    return rows


def wordmark(word: str) -> list[str]:
    """5-row, double-width block-font rendering of ``word``."""
    rows = ["" for _ in range(5)]
    for letter in word.upper():
        glyph = _render_glyph(letter)
        for r, band in enumerate(glyph):
            rows[r] += band
            if letter != " ":
                rows[r] += " "
    return [row.rstrip() for row in rows]


def wordmark_glitched(word: str, rng: random.Random | None = None) -> list[str]:
    """Chroma-ghost variant of :func:`wordmark` — 1–2 bands shifted sideways."""
    rng = rng or random.Random()
    base = wordmark(word)
    for _ in range(rng.randint(1, 2)):
        band = rng.randrange(5)
        shift = rng.choice((-1, 1))
        row = base[band]
        if not row:
            continue
        base[band] = (row[1:] + " ") if shift < 0 else (" " + row[:-1])
    return base


def join_rows(rows: Iterable[str]) -> str:
    """Join cell-screen rows into a single newline-terminated string."""
    return "\n".join(rows).rstrip("\n")


def stage_markers(analyze: str, synth: str, verify: str, parity: str) -> str:
    """Compose a module's four ``BANK 1`` stage cells (✓ … — ✗)."""
    return (
        f"ANALYZE {analyze}  SYNTHESIZE {synth}  VERIFY {verify}  PARITY {parity}"
    )


def _fmt_prop(payload: dict[str, Any], key: str, default: str = "?") -> str:
    value = payload.get(key)
    return str(value) if value not in (None, "") else default


def _topic(payload: dict[str, Any]) -> str:
    topic = str(payload.get("topic", ""))
    if topic == "task.context":
        return "task.index"
    return topic


def _counterexample_text(payload: dict[str, Any], cx: dict[str, Any]) -> str:
    inputs = cx.get("inputs")
    expected = cx.get("expected_output")
    actual = cx.get("actual_output")
    fn = cx.get("function_name", "function")
    left = "input=" + (repr(inputs) if inputs not in (None, "") else "∅")
    expected_s = "∅" if expected is None else repr(expected)
    actual_s = "∅" if actual is None else repr(actual)
    return f"@counterexample  function {fn}: {left} expected={expected_s} actual={actual_s}"


def format_event_line(payload: dict[str, Any]) -> list[Text]:
    """Turn one EventBus payload into 1–2 BIOS log lines (Rich Text)."""
    topic = _topic(payload)
    file = _fmt_prop(payload, "current_file")
    out: list[Text] = []
    head = Text()
    head.append(f"@{topic}", style=DIM)

    if topic == "task.analysis":
        stage = payload.get("stage")
        if stage == "start":
            head.append("   START   ", style=MINT)
            head.append(f"file={file}", style=MINT)
        else:
            n = _fmt_prop(payload, "n_functions", "0")
            flagged = payload.get("nondeterministic") or payload.get(
                "flagged_non_deterministic"
            )
            body = Text(f"   LogicSpec built: {n} functions", style=MINT)
            if flagged:
                body.append(", ", style=MINT)
                body.append(f"{len(flagged)} flagged non-deterministic", style=AMBER)
                if isinstance(flagged, list):
                    body.append(
                        f" ({', '.join(str(f) for f in flagged[:4])})", style=AMBER
                    )
            line = head; line.append_text(body)
            out.append(line)

    elif topic == "task.index":
        latency = _fmt_prop(payload, "latency_ms")
        engine = _fmt_prop(payload, "engine", "moss")
        within = payload.get("within_budget", True)
        tail = "OK" if within else "LATENCY BUDGET MISS"
        line = head
        line.append("   indexed ", style=MINT)
        line.append(f"engine={engine} latency={latency}ms", style=AMBER)
        line.append(f" [{tail}]", style=MINT if within else AMBER)
        out.append(line)

    elif topic == "task.refactor":
        stage = payload.get("stage")
        if stage == "start":
            head.append("   START   ", style=MINT)
            head.append(f"target={_fmt_prop(payload, 'target_language')}", style=MINT)
            head.append(f"  file={file}", style=DIM)
        else:
            line = head
            line.append("   ProposedCode generated (schema-valid)", style=MINT)
            if payload.get("guardrail") == "clean":
                line.append(" · ", style=DIM)
                line.append("guardrail: clean", style=GREEN)
            imports = payload.get("imports_added")
            if imports:
                line.append(f" (+{len(imports)} imports)", style=DIM)
            out.append(line)

    elif topic == "task.verify":
        stage = payload.get("stage")
        if stage == "start":
            head.append("   START   ", style=MINT)
            head.append(f"sandbox={_fmt_prop(payload, 'sandbox')}", style=MINT)
            head.append(f"  file={file}", style=DIM)
        else:
            status = payload.get("status")
            score = payload.get("score")
            n_checks = payload.get("n_checks", 0)
            if status == "pass" or status == "skip":
                tail = "SKIP" if status == "skip" else "PARITY OK"
                line = head
                line.append("   ", style=MINT)
                if score is not None:
                    line.append(f"score={score}", style=GREEN)
                    line.append(f"  checks={n_checks}", style=DIM)
                line.append(f" [{tail}]", style=GREEN)
                out.append(line)
            else:
                cxs = payload.get("counter_examples") or []
                if cxs:
                    cx = cxs[0]
                    fn = cx.get("function_name", "function")
                    line = head
                    line.append(f"   function {fn}: MISMATCH", style=RED)
                    out.append(line)
                    out.append(Text(_counterexample_text(payload, cx), style=AMBER))
                else:
                    line = head
                    line.append(f"   FAILED {_fmt_prop(payload, 'reason', 'harness error')}", style=RED)
                    out.append(line)

    elif topic == "feedback.regression":
        retry = _fmt_prop(payload, "retry_count", "?")
        max_retries = payload.get("max_retries", 3)
        line = head
        line.append(f"   retry {retry}/{max_retries} → refactor", style=AMBER)
        out.append(line)

    elif topic == "feedback.warning":
        source = payload.get("source", "orchestrator")
        reason = payload.get("reason", "warning")
        if payload.get("severity") == "override":
            line = head
            line.append("   OVERRIDE accepted — ", style=GREEN)
            line.append("resume (verified by human operator)", style=GREEN)
            out.append(line)
        elif source == "guardrail" and payload.get("severity") == "block":
            v = payload.get("guardrail_violation") or {}
            line = head
            line.append("   HALT ", style=AMBER)
            line.append(
                f"{v.get('category', 'guardrail')}: {v.get('pattern', reason)} "
                f"(line {v.get('line', '?')})",
                style=AMBER,
            )
            line.append(" — OVERRIDE REQUIRED", style=AMBER)
            out.append(line)
        else:
            line = head
            line.append(f"   {_fmt_prop(payload, 'severity', 'warn')} ", style=AMBER)
            line.append(f"{source}: {reason}", style=AMBER)
            out.append(line)

    else:
        line = head
        line.append(f"   {_fmt_prop(payload, 'stage', 'event')} ", style=MINT)
        line.append(f"file={file}", style=DIM)
        out.append(line)

    if not out:
        out.append(head)
    return out