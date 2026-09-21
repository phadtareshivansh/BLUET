.PHONY: install test lint run check

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