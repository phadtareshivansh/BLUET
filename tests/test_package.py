"""Smoke test: package imports and exposes a version."""

import bluet


def test_version() -> None:
    assert isinstance(bluet.__version__, str)
    assert bluet.__version__.startswith("0.1.")


def test_package_imports() -> None:
    assert bluet.__name__ == "bluet"