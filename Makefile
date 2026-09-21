.PHONY: install test lint run check bench-moss bench-moss

# Bootstrap / sync the environment (installs all groups incl. dev).
install:
	uv sync

# Run the test suite.
test:
	uv run pytest

# Lint the codebase.
lint:
	uv run ruff check .

# Print package info / entry smoke.
run:
	uv run python -c "import bluet; print(bluet.__version__)"

# Convenience: everything.
check: lint test

# Moss latency NFR benchmark (real SDK; loud-skips without creds).
# Requires: MOSS_PROJECT_ID and MOSS_PROJECT_KEY exported (see README "Latency NFR").
bench-moss:
	uv run pytest tests/benchmarks/test_moss_latency.py -q -s

# Moss latency NFR benchmark (real SDK; loud-skips without creds).
# Requires: uv sync --extra moss, MOSS_PROJECT_ID and MOSS_PROJECT_KEY set.
# -s keeps the honest mean/p50/p95/p99 latency numbers visible.
bench-moss:
	uv run pytest tests/benchmarks/test_moss_latency.py -q -s