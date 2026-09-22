"""Differential-parity harness: generate and run, then parse the results.

The generated ``test_parity.py`` runs in the sandbox alongside ``legacy.py``
(the original file) and ``proposed.py`` (the refactor candidate). For every
checkable function it drives *Hypothesis* ``@given`` property tests, calls both
implementations with identical inputs, and compares outputs. Any deviation
raises an ``AssertionError`` carrying a machine-readable one-line marker:

    BLUET_COUNTEREXAMPLE {json}

which :func:`parse_harness_output` reconstructs into :class:`CounterExample`
objects. Functions flagged non-deterministic by the analyzer are excluded
upstream (the verifier decides checkability, not this module).

The harness is pure-stdlib + Hypothesis so it runs identically under the local
subprocess runner and, later, the Docker-backed runner from Prompt 4.1.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from bluet.agents.analyzer.models import FunctionDef
from bluet.agents.refactor.models import CounterExample
from bluet.agents.verifier.strategies import strategy_exprs
from bluet.agents.verifier.z3_pass import synthesize_boundary_seeds

MARKER = "BLUET_COUNTEREXAMPLE "
_PASS_RE = re.compile(r"^BLUET_PASS ([A-Za-z_][A-Za-z0-9_]*)$", re.MULTILINE)

# Common prefix for both the test module and the pytest plugin.
_COMMON_PREFIX = """\
import sys
import json
import traceback

import hypothesis
from hypothesis import example, given, settings, HealthCheck
import hypothesis.strategies as st

import legacy as _legacy
import proposed as _proposed

MAX_EXAMPLES = {max_examples}

def _safe(value):
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)

def _eq(left, right):
    if isinstance(left, float) or isinstance(right, float):
        try:
            return abs(float(left) - float(right)) < 1e-9
        except (TypeError, ValueError):
            return False
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right

def _fail(fnname, inputs, expected, actual, note):
    payload = {{
        "function_name": fnname,
        "inputs": [_safe(value) for value in inputs],
        "expected_output": _safe(expected),
        "actual_output": _safe(actual),
        "diff_summary": note,
    }}
    raise AssertionError("{marker}" + json.dumps(payload))

def _resolve(mod, name):
    try:
        return getattr(mod, name)
    except AttributeError:
        return None

def _call(fn, args):
    try:
        return ("ok", fn(*args))
    except Exception as exc:
        return ("raised", type(exc).__name__ + ": " + str(exc))

"""

_SETTINGS = (
    "@settings(max_examples=MAX_EXAMPLES, deadline=None, database=None, "
    "suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much, "
    "HealthCheck.data_too_large])"
)

# Pytest plugin that emits BLUET_ markers for each test outcome.
_PYTEST_PLUGIN = """\
import sys

def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    name = report.nodeid.split("::")[-1]
    if report.passed:
        print(f"BLUET_PASS {name}", flush=True)
    elif report.failed:
        print(f"BLUET_FAIL {name}", flush=True)
        # Print the full traceback so the MARKER line appears in stdout.
        if report.longrepr:
            sys.stdout.write(str(report.longrepr))
            sys.stdout.write("\\n")

"""


def _argnames(fn: FunctionDef) -> list[str]:
    return [f"arg{i}" for i in range(len(fn.params))]


def _function_test(fn: FunctionDef, max_examples: int) -> str:
    arg_names = _argnames(fn)
    exprs = strategy_exprs(fn)
    given_args = ", ".join(exprs)
    arglist = ", ".join(arg_names)
    inputs_expr = f"[{', '.join(arg_names)}]" if arg_names else "[]"

    example_lines: list[str] = []
    for seed in synthesize_boundary_seeds(fn):
        if len(seed) != len(arg_names):
            continue
        example_lines.append(f"@example({', '.join(str(value) for value in seed)})")

    suffix = "()" if not arg_names else f"({arglist})"
    test_name = "test_" + re.sub(r"[^A-Za-z0-9_]", "_", fn.name)

    lines = [_SETTINGS]
    lines.extend(example_lines)
    lines.append(f"@given({given_args})")
    lines.append(f"def {test_name}{suffix}:")
    lines.append(f"    inputs = {inputs_expr}")
    lines.append(f"    legacy_fn = _resolve(_legacy, {fn.name!r})")
    lines.append(f"    proposed_fn = _resolve(_proposed, {fn.name!r})")
    lines.append("    if proposed_fn is None:")
    lines.append(
        f"        _fail({fn.name!r}, inputs, None, None, "
        f'"proposed code does not define function {fn.name!r}")'
    )
    lines.append("    exp = _call(legacy_fn, inputs)")
    lines.append("    act = _call(proposed_fn, inputs)")
    lines.append('    if exp[0] == "raised" and act[0] == "raised":')
    lines.append("        if exp[1] == act[1]:")
    lines.append("            return")
    lines.append(
        f'        _fail({fn.name!r}, inputs, exp[1], act[1], "legacy and proposed raised different exceptions")'
    )
    lines.append('    if exp[0] == "raised":')
    lines.append(
        f'        _fail({fn.name!r}, inputs, exp[1], act[1], "legacy raised while proposed returned a value")'
    )
    lines.append('    if act[0] == "raised":')
    lines.append(
        f'        _fail({fn.name!r}, inputs, exp[1], act[1], "proposed raised on a valid legacy input: " + act[1])'
    )
    lines.append("    if not _eq(exp[1], act[1]):")
    lines.append(f'        _fail({fn.name!r}, inputs, exp[1], act[1], "output mismatch")')
    return "\n".join(lines)


def build_harness_files(
    legacy_source: str,
    proposed_code: str,
    functions: Sequence[FunctionDef],
    *,
    max_examples: int = 200,
) -> dict[str, str]:
    """Return ``{relative_path: content}`` for a self-contained parity sandbox.

    Includes:
    - ``legacy.py``: the original source
    - ``proposed.py``: the refactor candidate
    - ``test_parity.py``: pytest test module with Hypothesis @given tests
    - ``bluet_pytest_plugin.py``: pytest plugin that emits BLUET_ markers
    """
    test_body = "\n\n".join(_function_test(fn, max_examples) for fn in functions)
    test_module = _COMMON_PREFIX.format(marker=MARKER, max_examples=max_examples) + test_body
    plugin_module = _COMMON_PREFIX.format(marker=MARKER, max_examples=max_examples) + _PYTEST_PLUGIN
    return {
        "legacy.py": legacy_source,
        "proposed.py": proposed_code,
        "test_parity.py": test_module,
        "bluet_pytest_plugin.py": plugin_module,
    }


def _parse_marker_line(combined: str) -> list[dict]:
    payloads: list[dict] = []
    for match in re.finditer(re.escape(MARKER) + r"(\{.*\})", combined):
        raw = match.group(1).strip()
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return payloads


def parse_harness_output(
    combined: str, function_names: Sequence[str]
) -> tuple[list[str], list[CounterExample]]:
    """Return ``(passes, counter_examples)`` from a harness run's combined output.

    A function is recorded as passing only when its ``BLUET_PASS`` line
    appears. Counter-examples are reconstructed from ``BLUET_COUNTEREXAMPLE``
    markers (the first per function wins).
    """
    passes = _PASS_RE.findall(combined)
    seen: set[str] = set()
    counter_examples: list[CounterExample] = []
    for payload in _parse_marker_line(combined):
        fn = payload.get("function_name")
        if not fn or fn in seen:
            continue
        seen.add(fn)
        counter_examples.append(
            CounterExample(
                function_name=str(fn),
                inputs=payload.get("inputs") or [],
                expected_output=payload.get("expected_output"),
                actual_output=payload.get("actual_output"),
                diff_summary=payload.get("diff_summary"),
                message=payload.get("diff_summary"),
            )
        )
    # Any checkable function that neither passed nor produced a marker (e.g.
    # the harness crashed before dispatching its test) becomes a pass-only-if
    # no failure was recorded for it — the caller decides how to grade a
    # crash. We do NOT invent counter-examples here.
    return [p for p in passes if p in function_names], counter_examples


__all__ = [
    "MARKER",
    "build_harness_files",
    "build_harness_files_java",
    "parse_harness_output",
]


# Java/JUnit harness for Java-to-Java parity verification
# Uses JUnit 5 + jqwik for property-based testing (similar to Hypothesis/fast-check)

_JAVA_COMMON_PREFIX = """\
package bluet.parity;

import net.jqwik.api.*;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

import java.lang.reflect.Method;
import java.util.*;

public class ParityTest {{

    private static final int MAX_EXAMPLES = {max_examples};

    private static Object callMethod(Object instance, String methodName, Object... args) {{
        try {{
            Class<?> clazz = instance.getClass();
            Method method = null;
            for (Method m : clazz.getDeclaredMethods()) {{
                if (m.getName().equals(methodName)) {{
                    method = m;
                    break;
                }}
            }}
            if (method == null) return null;
            method.setAccessible(true);
            return method.invoke(instance, args);
        }} catch (Exception e) {{
            return "EXCEPTION: " + e.getMessage();
        }}
    }}

    private static boolean equals(Object a, Object b) {{
        if (a == null && b == null) return true;
        if (a == null || b == null) return false;
        if (a instanceof Number && b instanceof Number) {{
            return Math.abs(((Number) a).doubleValue() - ((Number) b).doubleValue()) < 1e-9;
        }}
        return a.equals(b);
    }}

    private static void fail(String fnName, Object[] inputs, Object expected, Object actual, String note) {{
        StringBuilder sb = new StringBuilder();
        sb.append("BLUET_COUNTEREXAMPLE ");
        sb.append("{{");
        sb.append("\\"function_name\\":\\"").append(fnName).append("\\",");
        sb.append("\\"inputs\\":[");
        for (int i = 0; i < inputs.length; i++) {{
            if (i > 0) sb.append(",");
            Object v = inputs[i];
            if (v instanceof String) sb.append("\\"").append(v).append("\\"");
            else sb.append(v);
        }}
        sb.append("],");
        sb.append("\\"expected_output\\":");
        if (expected instanceof String) sb.append("\\"").append(expected).append("\\"");
        else if (expected == null) sb.append("null");
        else sb.append(expected);
        sb.append(",");
        sb.append("\\"actual_output\\":");
        if (actual instanceof String) sb.append("\\"").append(actual).append("\\"");
        else if (actual == null) sb.append("null");
        else sb.append(actual);
        sb.append(",");
        sb.append("\\"diff_summary\\":\\"").append(note).append("\\"");
        sb.append("}}");
        System.out.println(sb.toString());
        fail(note);
    }}

    @BeforeEach
    void setUp() {{
    }}

"""

_JAVA_TEST_TEMPLATE = """\
    @Property
    @Report(Reporting.GENERATED)
    void test{fnName}({params}) {{
        Object legacyResult = callMethod(new Legacy(), "{fnName}", {argNames});
        Object proposedResult = callMethod(new Proposed(), "{fnName}", {argNames});
        Object[] inputs = new Object[]{{{argNames}}};
        if (legacyResult instanceof String && ((String) legacyResult).startsWith("EXCEPTION:") &&
            proposedResult instanceof String && ((String) proposedResult).startsWith("EXCEPTION:")) {{
            if (legacyResult.equals(proposedResult)) return;
            fail("{fnName}", inputs, legacyResult, proposedResult, "legacy and proposed raised different exceptions");
        }}
        if (legacyResult instanceof String && ((String) legacyResult).startsWith("EXCEPTION:")) {{
            fail("{fnName}", inputs, legacyResult, proposedResult, "legacy raised while proposed returned a value");
        }}
        if (proposedResult instanceof String && ((String) proposedResult).startsWith("EXCEPTION:")) {{
            fail("{fnName}", inputs, legacyResult, proposedResult, "proposed raised on a valid legacy input: " + proposedResult);
        }}
        if (!equals(legacyResult, proposedResult)) {{
            fail("{fnName}", inputs, legacyResult, proposedResult, "output mismatch");
        }}
        System.out.println("BLUET_PASS {fnName}");
    }}

"""

_JAVA_ARBITRARIES_TEMPLATE = """\
    // Arbitraries for property-based testing
    @Provide
    Arbitrary<Integer> intArbitrary() {{
        return Arbitraries.integers().between({INT_MIN}, {INT_MAX});
    }}

    @Provide
    Arbitrary<Double> doubleArbitrary() {{
        return Arbitraries.doubles().between(-1000.0, 1000.0);
    }}

    @Provide
    Arbitrary<Boolean> booleanArbitrary() {{
        return Arbitraries.booleans();
    }}

    @Provide
    Arbitrary<String> stringArbitrary() {{
        return Arbitraries.strings().alpha().ofMinLength(1).ofMaxLength(20);
    }}

    @Provide
    Arbitrary<List<Integer>> listIntArbitrary() {{
        return Arbitraries.lists(intArbitrary()).ofMaxSize(10);
    }}
"""


def _java_argnames(fn: FunctionDef) -> list[str]:
    return [f"arg{i}" for i in range(len(fn.params))]


def _java_arbitrary_expr(fn: FunctionDef) -> str:
    """Generate jqwik arbitrary expression for a function's parameters."""
    from bluet.agents.verifier.strategies import strategy_exprs, INT_MIN, INT_MAX

    exprs = strategy_exprs(fn)
    # Map Python hypothesis strategies to jqwik arbitraries
    # For simplicity, use a combined arbitrary based on first param type
    if not exprs:
        return "intArbitrary()"
    first = exprs[0]
    if "float" in first.lower() or "double" in first.lower():
        return "doubleArbitrary()"
    if "bool" in first.lower():
        return "booleanArbitrary()"
    if "text" in first.lower() or "string" in first.lower():
        return "stringArbitrary()"
    if "list" in first.lower():
        return "listIntArbitrary()"
    return "intArbitrary()"


def _java_function_test(fn: FunctionDef, max_examples: int) -> str:
    arg_names = _java_argnames(fn)
    # For jqwik @ForAll, we need parameter declarations with @ForAll annotations
    param_types = []
    param_annotations = []
    from bluet.agents.verifier.strategies import strategy_exprs
    exprs = strategy_exprs(fn)
    for i, expr in enumerate(exprs):
        if "float" in expr.lower() or "double" in expr.lower():
            param_types.append("double")
            param_annotations.append(f"@ForAll(\"doubleArbitrary\")")
        elif "bool" in expr.lower():
            param_types.append("boolean")
            param_annotations.append(f"@ForAll(\"booleanArbitrary\")")
        elif "text" in expr.lower() or "string" in expr.lower():
            param_types.append("String")
            param_annotations.append(f"@ForAll(\"stringArbitrary\")")
        elif "list" in expr.lower():
            param_types.append("List<Integer>")
            param_annotations.append(f"@ForAll(\"listIntArbitrary\")")
        else:
            param_types.append("int")
            param_annotations.append(f"@ForAll(\"intArbitrary\")")
    # Build parameter list with annotations
    params_parts = []
    for ann, t, n in zip(param_annotations, param_types, arg_names):
        params_parts.append(f"{ann} {t} {n}")
    params = ", ".join(params_parts)
    return _JAVA_TEST_TEMPLATE.format(
        fnName=fn.name,
        params=params,
        argNames=", ".join(arg_names),
    )


_JAVA_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <groupId>bluet.parity</groupId>
    <artifactId>parity-test</artifactId>
    <version>1.0-SNAPSHOT</version>
    <properties>
        <maven.compiler.source>8</maven.compiler.source>
        <maven.compiler.target>8</maven.compiler.target>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    </properties>
    <dependencies>
        <dependency>
            <groupId>net.jqwik</groupId>
            <artifactId>jqwik</artifactId>
            <version>1.7.3</version>
            <scope>test</scope>
        </dependency>
        <dependency>
            <groupId>org.junit.jupiter</groupId>
            <artifactId>junit-jupiter</artifactId>
            <version>5.10.0</version>
            <scope>test</scope>
        </dependency>
        <dependency>
            <groupId>org.junit.platform</groupId>
            <artifactId>junit-platform-suite</artifactId>
            <version>1.10.0</version>
            <scope>test</scope>
        </dependency>
    </dependencies>
    <build>
        <plugins>
            <plugin>
                <groupId>org.apache.maven.plugins</groupId>
                <artifactId>maven-surefire-plugin</artifactId>
                <version>3.2.5</version>
                <configuration>
                    <includes>
                        <include>**/ParityTest.java</include>
                    </includes>
                </configuration>
            </plugin>
        </plugins>
    </build>
</project>
"""


def build_harness_files_java(
    legacy_source: str,
    proposed_code: str,
    functions: Sequence[FunctionDef],
    *,
    max_examples: int = 200,
) -> dict[str, str]:
    """Return ``{relative_path: content}`` for a self-contained Java parity sandbox.

    Includes:
    - ``Legacy.java``: the original source (renamed class)
    - ``Proposed.java``: the refactor candidate (renamed class)
    - ``ParityTest.java``: JUnit + jqwik test class
    - ``pom.xml``: Maven build file with jqwik dependency
    """
    from bluet.agents.verifier.strategies import INT_MIN, INT_MAX

    # Wrap legacy source in a class if it's not already
    legacy_wrapped = _wrap_java_class(legacy_source, "Legacy")
    proposed_wrapped = _wrap_java_class(proposed_code, "Proposed")

    test_body = "\n".join(_java_function_test(fn, max_examples) for fn in functions)
    arbitraries = _JAVA_ARBITRARIES_TEMPLATE.format(INT_MIN=INT_MIN, INT_MAX=INT_MAX)
    test_module = _JAVA_COMMON_PREFIX.format(max_examples=max_examples) + test_body + arbitraries + "\n}"

    return {
        "Legacy.java": legacy_wrapped,
        "Proposed.java": proposed_wrapped,
        "ParityTest.java": test_module,
        "pom.xml": _JAVA_POM,
    }


def _wrap_java_class(source: str, class_name: str) -> str:
    """Wrap Java source code in a class with the given name if needed."""
    source = source.strip()
    if source.startswith("public class") or source.startswith("class"):
        # Already has a class declaration, rename it
        import re
        source = re.sub(r"(public\s+)?class\s+\w+", f"public class {class_name}", source, count=1)
        return source
    # No class declaration, wrap it
    return f"public class {class_name} {{\n{source}\n}}"
