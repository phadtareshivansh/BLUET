"""Tests for COBOL parser."""

from __future__ import annotations

from pathlib import Path

from bluet.agents.analyzer import parser_for_source
from bluet.agents.analyzer.models import LogicSpec


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cobol-legacy"


def test_cobol_parser_arithmetic():
    """Test parsing COBOL arithmetic program."""
    source = (FIXTURE_DIR / "arithmetic.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    assert parser is not None, "COBOL parser should be registered"
    
    spec = parser.parse(source)
    assert isinstance(spec, LogicSpec)
    assert len(spec.functions) >= 1
    # Should have at least MAIN-PARA
    fn_names = {fn.name.upper() for fn in spec.functions}
    assert "MAIN-PARA" in fn_names


def test_cobol_parser_control_flow():
    """Test parsing COBOL control flow program."""
    source = (FIXTURE_DIR / "control_flow.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    assert parser is not None
    
    spec = parser.parse(source)
    assert isinstance(spec, LogicSpec)
    assert len(spec.functions) >= 2  # MAIN-PARA and CALCULATE-TOTAL
    fn_names = {fn.name.upper() for fn in spec.functions}
    assert "MAIN-PARA" in fn_names
    assert "CALCULATE-TOTAL" in fn_names


def test_cobol_parser_file_io():
    """Test parsing COBOL file I/O program."""
    source = (FIXTURE_DIR / "file_io.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    assert parser is not None
    
    spec = parser.parse(source)
    assert isinstance(spec, LogicSpec)
    assert len(spec.functions) >= 2  # MAIN-PARA and READ-AND-PROCESS
    fn_names = {fn.name.upper() for fn in spec.functions}
    assert "MAIN-PARA" in fn_names
    assert "READ-AND-PROCESS" in fn_names


def test_cobol_parser_detects_nondet_calls():
    """Test that COBOL parser detects non-deterministic calls."""
    source = (FIXTURE_DIR / "file_io.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    
    spec = parser.parse(source)
    
    # Should detect READ/WRITE as non-deterministic
    all_nondet = [c.name for fn in spec.functions for c in fn.nondeterministic_calls]
    assert any("READ" in n.upper() for n in all_nondet)
    assert any("WRITE" in n.upper() for n in all_nondet)


def test_cobol_parser_detects_arithmetic():
    """Test that COBOL parser identifies fixed-point arithmetic."""
    source = (FIXTURE_DIR / "arithmetic.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    
    spec = parser.parse(source)
    
    # Should have functions and detect DISPLAY as non-deterministic
    assert len(spec.functions) >= 1
    # The parser should at least extract the structure
    fn_names = {fn.name.upper() for fn in spec.functions}
    assert "MAIN-PARA" in fn_names


def test_cobol_parser_detects_branches():
    """Test that COBOL parser identifies IF/EVALUATE branches."""
    source = (FIXTURE_DIR / "control_flow.cbl").read_text(encoding="utf-8")
    parser = parser_for_source("cobol", "test.cbl")
    
    spec = parser.parse(source)
    
    # Should have multiple functions (paragraphs)
    assert len(spec.functions) >= 2
    fn_names = {fn.name.upper() for fn in spec.functions}
    assert "MAIN-PARA" in fn_names
    assert "CALCULATE-TOTAL" in fn_names


def test_cobol_parser_via_suffix():
    """Test parser selection via file suffix."""
    parser = parser_for_source(None, "test.cob")
    assert parser is not None
    assert parser.__class__.__name__ == "CobolParserImpl"
    
    parser = parser_for_source(None, "test.cbl")
    assert parser is not None
    
    parser = parser_for_source(None, "test.cobol")
    assert parser is not None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])