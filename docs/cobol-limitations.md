# COBOL Support Limitations

This document describes the known limitations and lossy mappings when translating COBOL to modern languages via BLUET. As noted in the PRD (Section 15), COBOL's arithmetic and control-flow model is genuinely harder to map than Python's or Java's.

## Fixed-Point Decimal Arithmetic

**COBOL:** `PIC 9(5)V99` - Fixed-point decimal with explicit decimal point position
**Modern equivalent:** Python `Decimal`, Java `BigDecimal`

**Limitations:**
- COBOL's fixed-point arithmetic has implicit decimal alignment that must be preserved
- Rounding modes (ROUNDED, TRUNCATE) may not map 1:1
- `ON SIZE ERROR` clauses have no direct equivalent in Python/Java exception handling
- Performance: COBOL fixed-point is typically hardware-accelerated on mainframes

**Current handling:** Mapped to `Decimal`/`BigDecimal` with best-effort rounding. Precision loss possible for intermediate calculations.

## GOTO and ALTER Statements

**COBOL:** Unstructured control flow via `GOTO label` and `ALTER label TO PROCEED TO label2`
**Modern equivalent:** No direct equivalent - requires control flow restructuring

**Limitations:**
- Creates irreducible control flow graphs
- `ALTER` modifies the target of a `GOTO` at runtime - impossible to represent statically
- Can create arbitrary cycles and entry points into paragraphs

**Current handling:** Flagged as non-deterministic/unsupported. Generates a warning and treats the paragraph as having unknown control flow. Cannot generate correct modern equivalent for programs using these constructs.

## PERFORM ... THRU (Multi-Paragraph Ranges)

**COBOL:** `PERFORM PARA-1 THRU PARA-5` executes a range of paragraphs
**Modern equivalent:** No direct equivalent - requires inlining or function extraction

**Limitations:**
- The range may contain multiple entry/exit points
- `GO TO` within the range can exit early (non-local exit)
- Nested `PERFORM THRU` creates complex nesting

**Current handling:** Mapped as a single function containing inlined code from all paragraphs in the range. This is lossy because:
- Original paragraph boundaries are lost
- `EXIT PARAGRAPH` / `EXIT PERFORM` behavior may not match
- Variable scoping differs (COBOL paragraphs share WORKING-STORAGE)

## REDEFINES and RENAMES (Memory Overlays)

**COBOL:** `REDEFINES` allows multiple data descriptions for the same memory. `RENAMES` creates alternative groupings.
**Modern equivalent:** No safe equivalent - violates type safety and memory isolation

**Limitations:**
- `REDEFINES` allows treating numeric data as alphanumeric and vice versa
- Can be used for bit manipulation, type punning
- `RENAMES` creates overlapping group items

**Current handling:** Only the primary definition is preserved. `REDEFINES`/`RENAMES` clauses are documented in notes but not represented in the LogicSpec. This loses the ability to detect type-punning bugs.

## CALL to External Programs

**COBOL:** `CALL 'PROGRAM' USING ...` with dynamic program names
**Modern equivalent:** Function calls / microservice calls

**Limitations:**
- Program name can be computed at runtime (dynamic CALL)
- Parameter passing: BY REFERENCE (default), BY VALUE, BY CONTENT
- Called program shares WORKING-STORAGE (global state)
- No standard interface definition (like headers/IDLs)

**Current handling:** Treated as non-deterministic external calls. Parameter mapping is best-effort. Shared state is not modeled.

## File I/O (Sequential/Indexed/Relative)

**COBOL:** `READ`, `WRITE`, `REWRITE`, `DELETE`, `START` on files with complex organizations
**Modern equivalent:** File I/O libraries / database access

**Limitations:**
- Indexed files (ISAM/VSAM) with alternate keys have no direct file-system equivalent
- Relative files with direct record access
- File status codes (FILE STATUS) for error handling
- `ACCESS MODE SEQUENTIAL/RANDOM/DYNAMIC`

**Current handling:** Mapped to generic file I/O operations. ISAM features are noted but not fully modeled. Sequential file handling is best-effort.

## Data Types Without Direct Equivalents

| COBOL Type | Description | Mapping |
|------------|-------------|---------|
| `PIC 9(n)` | Fixed-length integer | `int` (with range validation) |
| `PIC 9(n)V9(m)` | Fixed-point decimal | `Decimal` / `BigDecimal` |
| `PIC S9(n)` | Signed integer | `int` |
| `PIC X(n)` | Alphanumeric | `str` (fixed-length) |
| `PIC A(n)` | Alphabetic | `str` |
| `USAGE COMP` | Binary integer | `int` |
| `USAGE COMP-3` | Packed decimal | `Decimal` |
| `USAGE INDEX` | Table index | `int` |
| `USAGE POINTER` | Memory address | Not supported |
| `USAGE PROCEDURE-POINTER` | Function pointer | Not supported |

## Group Items and Nested Structures

**COBOL:** Hierarchical `01` level groups with subordinate `02-49` levels
**Modern equivalent:** Classes / dataclasses / structs

**Limitations:**
- `OCCURS` (arrays) with variable-length via `DEPENDING ON`
- `FILLER` bytes for alignment
- `SYNCHRONIZED` alignment clauses
- `JUSTIFIED RIGHT` for right-aligned alphanumeric

**Current handling:** Flattened to top-level parameters. Nested structure is partially preserved in notes but not in the LogicSpec function signatures.

## EVALUATE Statement

**COBOL:** Multi-way branch with `ALSO`, `THRU`, `WHEN OTHER`
**Modern equivalent:** `match`/`switch` / `if-elif-else` chains

**Limitations:**
- `ALSO` creates multi-condition evaluation
- `THRU` for range matching
- Can evaluate multiple subjects simultaneously
- `WHEN OTHER` as catch-all

**Current handling:** Mapped to `if-elif-else` chain. `ALSO`/`THRU` features are simplified.

## Special Registers and Intrinsic Functions

**COBOL:** `RETURN-CODE`, `SORT-RETURN`, `WHEN-COMPILED`, `DATE`, `TIME`, `RANDOM`, etc.
**Modern equivalent:** System calls / library functions

**Limitations:**
- Some are read-only, some read-write
- `RETURN-CODE` affects program exit status
- Intrinsic functions have specific semantics (e.g., `FUNCTION DATE-OF-INTEGER`)

**Current handling:** Mapped to equivalent library calls where possible. Some flagged as non-deterministic.

## SORT/MERGE

**COBOL:** `SORT file ON KEY ...` with `INPUT PROCEDURE` / `OUTPUT PROCEDURE`
**Modern equivalent:** External sort utilities / `sorted()` with custom keys

**Limitations:**
- Can process millions of records with minimal memory
- `INPUT PROCEDURE` releases records one at a time
- `OUTPUT PROCEDURE` returns records one at a time
- Multiple sort keys with `ASCENDING/DESCENDING`

**Current handling:** Not supported - flagged as non-deterministic external operation.

## Report Writer

**COBOL:** `REPORT SECTION` with `RD`, `CONTROL`, `PAGE`, `HEADING`, `DETAIL`, `FOOTING`
**Modern equivalent:** Template engines / reporting libraries

**Limitations:**
- Declarative report layout with automatic page breaks, control breaks
- `SUM`, `COUNT`, `AVERAGE` accumulators
- `LINE`, `COLUMN` positioning
- `NEXT PAGE`, `NEXT GROUP` logic

**Current handling:** Not supported - flagged as non-deterministic.

## Summary of Current BLUET COBOL Support

| Feature | Supported | Notes |
|---------|-----------|-------|
| Basic arithmetic (ADD, SUBTRACT, MULTIPLY, DIVIDE) | ✅ Partial | Mapped to Decimal |
| PERFORM loops (TIMES, UNTIL, VARYING) | ✅ Partial | Mapped to while/for |
| IF / EVALUATE | ✅ Partial | Mapped to if-elif |
| MOVE / assignment | ✅ | Mapped to assignment |
| READ / WRITE (sequential) | ⚠️ Flagged | Non-deterministic |
| CALL external | ⚠️ Flagged | Non-deterministic |
| GOTO / ALTER | ❌ | Unsupported |
| REDEFINES / RENAMES | ❌ | Documented only |
| Indexed/Relative files | ❌ | Unsupported |
| SORT / MERGE | ❌ | Unsupported |
| Report Writer | ❌ | Unsupported |

## Recommendations

1. **Pre-process COBOL:** Use a COBOL modernization tool to eliminate `GOTO`/`ALTER` before BLUET analysis
2. **Isolate external calls:** Wrap `CALL` statements in service interfaces
3. **Convert file I/O:** Migrate VSAM/ISAM to database before refactoring
4. **Review Decimal precision:** Verify financial calculations after refactoring
5. **Manual review required:** All COBOL refactoring output requires human verification