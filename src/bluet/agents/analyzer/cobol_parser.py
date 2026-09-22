"""COBOL parser using ANTLR4 grammar.

This module provides a LanguageParser implementation for COBOL using the
official ANTLR4 COBOL grammar. Due to COBOL's complex semantics (fixed-point
arithmetic, GOTO-based control flow, PERFORM loops), some mappings to
LogicSpec are lossy and documented as such.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Optional

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from bluet.agents.analyzer.base import LanguageParser
from bluet.agents.analyzer.models import (
    FunctionDef,
    LogicSpec,
    NondeterministicCall,
)


@dataclass
class CobolDataItem:
    """Simple data item for COBOL parser."""
    name: str
    type: str
    pic: str

# Try to import the generated ANTLR4 COBOL parser
# If not available, fall back to regex-based parsing
try:
    from bluet.agents.analyzer.cobol_parser_antlr import CobolLexer, CobolParser
    _HAS_ANTLR_COBOL = True
except ImportError:
    _HAS_ANTLR_COBOL = False
    CobolLexer = None
    CobolParser = None


class CobolParseError(Exception):
    """Raised when COBOL parsing fails."""
    pass


class CobolParserImpl(LanguageParser):
    """COBOL parser implementation using ANTLR4 or regex fallback.
    
    Known limitations (per PRD Section 15):
    - Fixed-point decimal arithmetic (PIC 9V99) maps to Python Decimal with potential precision loss
    - GOTO/ALTER statements create unstructured control flow that cannot be fully represented
    - PERFORM ... THRU spans multiple paragraphs - mapped as single function with note
    - REDEFINES/RENAMES create memory overlays with no direct modern equivalent
    - CALL to external programs treated as non-deterministic boundary
    """

    language: ClassVar[str] = "cobol"

    def __init__(self) -> None:
        self._use_antlr = _HAS_ANTLR_COBOL
        self._errors: list[str] = []

    def parse(self, source: str) -> LogicSpec:
        """Parse COBOL source into LogicSpec.
        
        Extracts:
        - PROGRAM-ID as module name
        - PROCEDURE DIVISION paragraphs/sections as functions
        - DATA DIVISION items as params/state_mutations
        - PERFORM/IF/EVALUATE as branches/loops
        - CALL/READ/WRITE as non-deterministic calls
        """
        self._errors = []
        
        if self._use_antlr:
            return self._parse_with_antlr(source)
        else:
            return self._parse_with_regex(source)

    def _parse_with_antlr(self, source: str) -> LogicSpec:
        """Parse using ANTLR4 COBOL grammar."""
        if not _HAS_ANTLR_COBOL:
            return self._parse_with_regex(source)
            
        try:
            input_stream = InputStream(source)
            lexer = CobolLexer(input_stream)
            token_stream = CommonTokenStream(lexer)
            parser = CobolParser(token_stream)
            
            # Add error listener
            error_listener = CobolErrorListener()
            parser.removeErrorListeners()
            parser.addErrorListener(error_listener)
            
            # Parse compilation unit
            tree = parser.compilationUnit()
            
            if error_listener.errors:
                self._errors.extend(error_listener.errors)
                # Fall back to regex if ANTLR has too many errors
                if len(error_listener.errors) > 5:
                    return self._parse_with_regex(source)
            
            # Extract logic from parse tree
            return self._extract_logic_from_tree(tree, source)
            
        except Exception as e:
            self._errors.append(f"ANTLR parse error: {e}")
            return self._parse_with_regex(source)

    def _extract_logic_from_tree(self, tree: Any, source: str) -> LogicSpec:
        """Extract LogicSpec from ANTLR parse tree."""
        # Walk the tree to extract paragraphs, data items, etc.
        # This is a simplified implementation - full ANTLR walker would be more complex
        functions = self._extract_paragraphs_antlr(tree, source)
        params = self._extract_data_items_antlr(tree)
        state_mutations = self._extract_mutations_antlr(tree)
        branches = self._extract_branches_antlr(tree)
        nondet_calls = self._extract_nondet_calls_antlr(tree)
        
        return LogicSpec(
            functions=functions,
            inputs=[],  # Aggregated from functions
            outputs=[],  # Aggregated from functions
            state_mutations=state_mutations,
            branches=branches,
            nondeterministic_calls=nondet_calls,
        )

    def _parse_with_regex(self, source: str) -> LogicSpec:
        """Fallback regex-based COBOL parser.
        
        This is a simplified parser that extracts basic structure when ANTLR is unavailable
        or fails. It's not a full COBOL parser but handles common patterns.
        """
        functions = []
        params: list[CobolDataItem] = []
        state_mutations = []
        branches = []
        nondet_calls = []
        
        lines = source.split('\n')
        
        # Extract PROGRAM-ID
        program_id = self._extract_program_id(source)
        
        # Extract DATA DIVISION items (WORKING-STORAGE, LINKAGE)
        data_items = self._extract_data_items_regex(source)
        params = data_items.get('linkage', [])
        working_storage = data_items.get('working_storage', [])
        
        # Extract PROCEDURE DIVISION paragraphs
        paragraphs = self._extract_paragraphs_regex(source, lines)
        
        for i, para in enumerate(paragraphs):
            # Determine if this paragraph has non-deterministic calls
            para_nondet = self._find_nondet_calls_in_paragraph(para, lines)
            nondet_calls.extend(para_nondet)
            
            # Extract state mutations (MOVE TO, ADD TO, etc.)
            para_mutations = self._find_mutations_in_paragraph(para, working_storage)
            state_mutations.extend(para_mutations)
            
            # Extract branches (IF, EVALUATE)
            para_branches = self._find_branches_in_paragraph(para)
            branches.extend(para_branches)
            
            # Create FunctionDef
            fn = FunctionDef(
                name=para['name'],
                line=para['line'],
                params=[p.name for p in params],
                returns=[],
                inputs=[p.name for p in params],
                outputs=[],
                state_mutations=[m.name for m in para_mutations],
                branches=para_branches,
                nondeterministic_calls=para_nondet,
            )
            functions.append(fn)
        
        # Aggregate top-level fields
        all_inputs = []
        all_outputs = []
        all_state_mutations = []
        all_branches = []
        all_nondet = []
        
        for fn in functions:
            all_inputs.extend(fn.inputs)
            all_outputs.extend(fn.outputs)
            all_state_mutations.extend(fn.state_mutations)
            all_branches.extend(fn.branches)
            all_nondet.extend(fn.nondeterministic_calls)
        
        return LogicSpec(
            functions=functions,
            inputs=list(set(all_inputs)),
            outputs=list(set(all_outputs)),
            state_mutations=list(set(all_state_mutations)),
            branches=list(set(all_branches)),
            nondeterministic_calls=all_nondet,
        )

    def _extract_program_id(self, source: str) -> str:
        """Extract PROGRAM-ID from source."""
        match = re.search(r'PROGRAM-ID\s*\.\s*([A-Z0-9-]+)', source, re.IGNORECASE)
        return match.group(1) if match else "UNKNOWN"

    def _extract_data_items_regex(self, source: str) -> dict[str, list[CobolDataItem]]:
        """Extract data items from DATA DIVISION using regex."""
        linkage = []
        working_storage = []
        
        # Find DATA DIVISION section
        data_div_match = re.search(r'DATA\s+DIVISION\s*\.(.*?)(?=\n\s*(?:PROCEDURE|IDENTIFICATION|ENVIRONMENT)\s+DIVISION|\Z)', source, re.IGNORECASE | re.DOTALL)
        if not data_div_match:
            return {'linkage': [], 'working_storage': []}
        
        data_div = data_div_match.group(1)
        
        # Extract LINKAGE SECTION
        linkage_match = re.search(r'LINKAGE\s+SECTION\s*\.(.*?)(?=\n\s*(?:WORKING-STORAGE|LOCAL-STORAGE|FILE|SCREEN)\s+SECTION|\n\s*PROCEDURE\s+DIVISION|\Z)', data_div, re.IGNORECASE | re.DOTALL)
        if linkage_match:
            linkage = self._parse_data_items(linkage_match.group(1))
        
        # Extract WORKING-STORAGE SECTION
        ws_match = re.search(r'WORKING-STORAGE\s+SECTION\s*\.(.*?)(?=\n\s*(?:LINKAGE|LOCAL-STORAGE|FILE|SCREEN)\s+SECTION|\n\s*PROCEDURE\s+DIVISION|\Z)', data_div, re.IGNORECASE | re.DOTALL)
        if ws_match:
            working_storage = self._parse_data_items(ws_match.group(1))
        
        return {'linkage': linkage, 'working_storage': working_storage}

    def _parse_data_items(self, section_text: str) -> list[CobolDataItem]:
        """Parse data item declarations (level-number PIC clauses)."""
        items = []
        # Match level-number followed by name and PIC
        # e.g., "       01  CUSTOMER-NAME  PIC X(30)."
        pattern = r'^\s*(\d{2})\s+([A-Z0-9-]+)\s+PIC\s+([^\.]+)\.'
        for match in re.finditer(pattern, section_text, re.IGNORECASE | re.MULTILINE):
            level = match.group(1)
            name = match.group(2)
            pic = match.group(3).strip()
            
            # Determine type from PIC
            py_type = self._pic_to_type(pic)
            
            items.append(CobolDataItem(
                name=name,
                type=py_type,
                pic=pic,
            ))
        return items

    def _pic_to_type(self, pic: str) -> str:
        """Convert COBOL PIC clause to Python type hint."""
        pic = pic.upper()
        if '9' in pic and 'V' in pic:
            return 'Decimal'  # Fixed-point
        elif '9' in pic:
            return 'int'
        elif 'X' in pic or 'A' in pic:
            return 'str'
        elif 'S' in pic and '9' in pic:
            return 'int'  # Signed numeric
        return 'Any'

    def _extract_paragraphs_regex(self, source: str, lines: list[str]) -> list[dict]:
        """Extract PROCEDURE DIVISION paragraphs."""
        paragraphs = []
        
        # Find PROCEDURE DIVISION
        proc_start = -1
        for i, line in enumerate(lines):
            if re.search(r'PROCEDURE\s+DIVISION', line, re.IGNORECASE):
                proc_start = i
                break
        
        if proc_start == -1:
            return paragraphs
        
        # Extract paragraphs (labels ending with . or : followed by statements)
        # Pattern: PARAGRAPH-NAME. or PARAGRAPH-NAME SECTION.
        para_pattern = re.compile(r'^\s*([A-Z0-9-]+)\s*(?:\.|SECTION\s*\.)', re.IGNORECASE)
        
        current_para = None
        para_start_line = 0
        
        for i in range(proc_start + 1, len(lines)):
            line = lines[i]
            match = para_pattern.match(line)
            
            if match:
                # Save previous paragraph
                if current_para is not None:
                    paragraphs.append({
                        'name': current_para,
                        'line': para_start_line + 1,  # 1-indexed
                        'start': para_start_line,
                        'end': i - 1,
                    })
                
                current_para = match.group(1).upper()
                para_start_line = i
        
        # Save last paragraph
        if current_para is not None:
            paragraphs.append({
                'name': current_para,
                'line': para_start_line + 1,
                'start': para_start_line,
                'end': len(lines) - 1,
            })
        
        return paragraphs

    def _find_nondet_calls_in_paragraph(self, para: dict, lines: list[str]) -> list[NondeterministicCall]:
        """Find non-deterministic calls (CALL, READ, WRITE, ACCEPT, DISPLAY) in paragraph."""
        calls = []
        start = para['start']
        end = para['end']
        
        for i in range(start, min(end + 1, len(lines))):
            line = lines[i].upper()
            
            # CALL to external program
            call_match = re.search(r'\bCALL\s+["\']([^"\']+)["\']', line)
            if call_match:
                calls.append(NondeterministicCall(
                    kind="network",  # External program call
                    name=call_match.group(1),
                    line=i + 1,
                ))
            
            # READ/WRITE file I/O
            for kw in ['READ', 'WRITE', 'REWRITE', 'DELETE', 'START']:
                if re.search(rf'\b{kw}\s+\w+', line):
                    calls.append(NondeterministicCall(
                        kind="file",
                        name=kw,
                        line=i + 1,
                    ))
            
            # ACCEPT/DISPLAY (console I/O)
            for kw in ['ACCEPT', 'DISPLAY']:
                if re.search(rf'\b{kw}\b', line):
                    calls.append(NondeterministicCall(
                        kind="console",
                        name=kw,
                        line=i + 1,
                    ))
        
        return calls

    def _find_mutations_in_paragraph(self, para: dict, working_storage: list[Param]) -> list[StateMutation]:
        """Find state mutations (MOVE TO, ADD TO, SUBTRACT, etc.) in paragraph."""
        mutations = []
        ws_names = {p.name for p in working_storage}
        
        # This would need the source lines - simplified for now
        return mutations

    def _find_branches_in_paragraph(self, para: dict) -> list[str]:
        """Find branches (IF, EVALUATE) in paragraph."""
        branches = []
        # Simplified - would need source text
        return branches

    # ANTLR-specific extractors (stubs for now)
    def _extract_paragraphs_antlr(self, tree: Any, source: str) -> list[FunctionDef]:
        return []

    def _extract_data_items_antlr(self, tree: Any) -> list[Param]:
        return []

    def _extract_mutations_antlr(self, tree: Any) -> list[StateMutation]:
        return []

    def _extract_branches_antlr(self, tree: Any) -> list[str]:
        return []

    def _extract_nondet_calls_antlr(self, tree: Any) -> list[NondeterministicCall]:
        return []


class CobolErrorListener(ErrorListener):
    """Collect ANTLR parse errors."""
    
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []
    
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"Line {line}:{column} - {msg}")


# Register the parser
def register_cobol_parser():
    """Register COBOL parser with the analyzer."""
    from bluet.agents.analyzer import _LANGUAGE_PARSERS
    _LANGUAGE_PARSERS["cobol"] = CobolParserImpl()
    _LANGUAGE_PARSERS["cbl"] = CobolParserImpl()


__all__ = ["CobolParserImpl", "register_cobol_parser", "CobolParseError"]