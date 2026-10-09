"""Test runner output and JUnit XML parsers for `run_tests`.

To add a runner, write `_parse_<runner>(lines) -> SuiteSummary | None` that returns None
when the output is not its format, add it to `_PARSERS` (order matters: the first match
wins, so more specific formats go first), and add a `SAMPLES` row in tests/test_project.py.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_MAX_FAILURES = 50
_MAX_MESSAGE = 500
_MAX_JUNIT_FILES = 500
_MAX_JUNIT_BYTES = 20_000_000

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class SuiteSummary:
    format: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def fail(self, name: str, message: str = "") -> None:
        if len(self.failures) < _MAX_FAILURES and not any(f["name"] == name for f in self.failures):
            self.failures.append({"name": name.strip(), "message": message.strip()[:_MAX_MESSAGE]})


def _next_nonblank(lines: list[str], start: int, stop: int = 6) -> str:
    for line in lines[start : start + stop]:
        if line.strip():
            return line.strip()
    return ""


def _counts(text: str) -> dict[str, int]:
    """'1 failed, 2 passed' or '1 failed | 2 passed' to {'failed': 1, 'passed': 2}."""
    out: dict[str, int] = {}
    for num, word in re.findall(r"(\d+) ([a-z]+)", text):
        out[word] = out.get(word, 0) + int(num)
    return out


_PYTEST_WORDS = {"passed", "failed", "skipped", "error", "errors", "xfailed", "xpassed", "deselected", "warning",
                 "warnings", "rerun"}  # fmt: skip
_PYTEST_SUMMARY = re.compile(r"^=*\s*(\S.*?) in \d+(?:\.\d+)?s\b")
_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$")


def _parse_pytest(lines: list[str]) -> SuiteSummary | None:
    counts: dict[str, int] | None = None
    for line in reversed(lines):
        m = _PYTEST_SUMMARY.match(line.strip())
        if not m:
            continue
        body = m.group(1).strip()
        if body == "no tests ran":
            counts = {}
            break
        parts = [p.strip() for p in body.split(",")]
        if parts and all(re.fullmatch(r"\d+ [a-z]+", p) and p.split()[1] in _PYTEST_WORDS for p in parts):
            counts = _counts(body)
            break
    if counts is None:
        return None
    s = SuiteSummary("pytest")
    s.passed = counts.get("passed", 0) + counts.get("xpassed", 0)
    s.failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    s.skipped = counts.get("skipped", 0) + counts.get("xfailed", 0)
    for line in lines:
        if m := _PYTEST_FAIL.match(line.strip()):
            s.fail(m.group(1), m.group(2) or "")
    return s


def _parse_jest(lines: list[str]) -> SuiteSummary | None:
    totals = [line for line in lines if re.match(r"^Tests:\s+.*\d+ total", line.strip())]
    if not totals:
        return None
    counts = _counts(totals[-1])
    s = SuiteSummary("jest", counts.get("passed", 0), counts.get("failed", 0))
    s.skipped = counts.get("skipped", 0) + counts.get("todo", 0) + counts.get("pending", 0)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*● (.+)$", line)
        if m and m.group(1).strip() != "Console":
            s.fail(m.group(1), _next_nonblank(lines, i + 1))
    return s


def _parse_vitest(lines: list[str]) -> SuiteSummary | None:
    totals = [line for line in lines if re.match(r"^\s*Tests\s+.+\(\d+\)\s*$", line)]
    if not totals:
        return None
    counts = _counts(totals[-1])
    s = SuiteSummary("vitest", counts.get("passed", 0), counts.get("failed", 0))
    s.skipped = counts.get("skipped", 0) + counts.get("todo", 0)
    for i, line in enumerate(lines):
        if m := re.match(r"^\s*FAIL\s+(.+?)\s*$", line):
            s.fail(m.group(1), _next_nonblank(lines, i + 1))
    return s


def _parse_mocha(lines: list[str]) -> SuiteSummary | None:
    found: dict[str, int] = {}
    failing_at = None
    for i, line in enumerate(lines):
        if m := re.match(r"^\s*(\d+) (passing|failing|pending)\b", line):
            found[m.group(2)] = int(m.group(1))
            if m.group(2) == "failing":
                failing_at = i
    if "passing" not in found and "failing" not in found:
        return None
    s = SuiteSummary("mocha", found.get("passing", 0), found.get("failing", 0), found.get("pending", 0))
    if failing_at is not None:
        for i in range(failing_at + 1, len(lines)):
            if m := re.match(r"^\s+\d+\) (.+)$", lines[i]):
                s.fail(m.group(1).rstrip(":"), _next_nonblank(lines, i + 1))
    return s


_CARGO_RESULT = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored")


def _parse_cargo(lines: list[str]) -> SuiteSummary | None:
    results = [m for line in lines if (m := _CARGO_RESULT.search(line))]
    if not results:
        return None
    s = SuiteSummary("cargo")
    for m in results:
        s.passed += int(m.group(1))
        s.failed += int(m.group(2))
        s.skipped += int(m.group(3))
    headers = {
        m.group(1): i for i, line in enumerate(lines) if (m := re.match(r"^---- (\S+) stdout ----$", line.strip()))
    }
    for line in lines:
        if m := re.match(r"^test (\S+) \.\.\. FAILED$", line.strip()):
            name = m.group(1)
            idx = headers.get(name)
            message = ""
            if idx is not None:
                message = " ".join(x.strip() for x in lines[idx + 1 : idx + 4] if x.strip())
            s.fail(name, message)
    return s


_GO_RESULT = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
_GO_PACKAGE = re.compile(r"^(ok|FAIL|\?)\s+\S+\s+(?:\d+(?:\.\d+)?s|\(cached\)|\[no test files\]|\[build failed\])")


def _parse_go(lines: list[str]) -> SuiteSummary | None:
    results = [(i, m) for i, line in enumerate(lines) if (m := _GO_RESULT.match(line))]
    packages = [m.group(1) for line in lines if (m := _GO_PACKAGE.match(line))]
    if not results and not packages:
        return None
    s = SuiteSummary("go test")
    # Without -v, go test prints `--- FAIL` lines but never `--- PASS`, so per-test counts
    # would report 0 passed. Count packages instead and still list the failing tests.
    verbose = any(line.startswith("=== RUN") for line in lines)
    if not verbose and packages:
        s.format = "go test (per package)"
        s.passed = packages.count("ok")
        s.failed = packages.count("FAIL")
        s.skipped = packages.count("?")
    for i, m in results:
        status, name = m.groups()
        if status == "FAIL":
            detail = []
            for line in lines[i + 1 : i + 6]:
                if _GO_RESULT.match(line) or not line.startswith((" ", "\t")):
                    break
                detail.append(line.strip())
            s.fail(name, " ".join(detail))
        if s.format != "go test":
            continue
        if status == "PASS":
            s.passed += 1
        elif status == "SKIP":
            s.skipped += 1
        else:
            s.failed += 1
    return s


_MAVEN_TOTAL = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)")
_MAVEN_FAIL = re.compile(r"^\[ERROR\]\s{2,}([\w$]+(?:\.[\w$]+)+)(?::\d+)?\s+(.*)$")


def _parse_maven(lines: list[str]) -> SuiteSummary | None:
    totals = [m for line in lines if "Time elapsed" not in line and (m := _MAVEN_TOTAL.search(line))]
    if not totals:
        totals = [m for line in lines if (m := _MAVEN_TOTAL.search(line))]
    if not totals:
        return None
    run, failures, errors, skipped = (int(g) for g in totals[-1].groups())
    s = SuiteSummary("maven", run - failures - errors - skipped, failures + errors, skipped)
    for line in lines:
        if m := _MAVEN_FAIL.match(line.strip()):
            s.fail(m.group(1), m.group(2))
    return s


def _parse_gradle(lines: list[str]) -> SuiteSummary | None:
    totals = [m for line in lines if (m := re.match(r"^(\d+) tests? completed(.*)$", line.strip()))]
    if not totals:
        return None
    total = int(totals[-1].group(1))
    counts = _counts(totals[-1].group(2))
    s = SuiteSummary("gradle", 0, counts.get("failed", 0), counts.get("skipped", 0))
    s.passed = total - s.failed - s.skipped
    for i, line in enumerate(lines):
        if m := re.match(r"^(.+?) > (.+?) FAILED$", line.strip()):
            s.fail(f"{m.group(1)}.{m.group(2)}", _next_nonblank(lines, i + 1))
    return s


_DOTNET_TOTAL = re.compile(r"(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+)")


def _parse_dotnet(lines: list[str]) -> SuiteSummary | None:
    totals = [m for line in lines if (m := _DOTNET_TOTAL.search(line))]
    if not totals:
        return None
    s = SuiteSummary("dotnet test")
    for m in totals:
        s.failed += int(m.group(1))
        s.passed += int(m.group(2))
        s.skipped += int(m.group(3))
    for i, line in enumerate(lines):
        if m := re.match(r"^\s*Failed (\S+) \[", line):
            message = ""
            for j in range(i + 1, min(i + 6, len(lines))):
                if "Error Message:" in lines[j]:
                    message = _next_nonblank(lines, j + 1)
                    break
            s.fail(m.group(1), message)
    return s


def _parse_rspec(lines: list[str]) -> SuiteSummary | None:
    totals = [m for line in lines if (m := re.match(r"^(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", line))]
    if not totals:
        return None
    examples, failures, pending = (int(g or 0) for g in totals[-1].groups())
    s = SuiteSummary("rspec", examples - failures - pending, failures, pending)
    for line in lines:
        if m := re.match(r"^rspec (\S+) # (.+)$", line.strip()):
            s.fail(m.group(2), m.group(1))
    return s


def _parse_phpunit(lines: list[str]) -> SuiteSummary | None:
    s = SuiteSummary("phpunit")
    found = False
    for line in lines:
        stripped = line.strip()
        if m := re.match(r"^OK \((\d+) tests?,", stripped):
            s.passed, found = int(m.group(1)), True
        elif m := re.match(r"^Tests: (\d+), Assertions: \d+(.*)$", stripped):
            fields = {k.lower(): int(v) for k, v in re.findall(r"(\w+): (\d+)", m.group(2))}
            s.failed = fields.get("errors", 0) + fields.get("failures", 0)
            s.skipped = fields.get("skipped", 0) + fields.get("incomplete", 0)
            s.passed = int(m.group(1)) - s.failed - s.skipped
            found = True
    if not found:
        return None
    for i, line in enumerate(lines):
        if m := re.match(r"^\d+\) (\S.*)$", line):
            s.fail(m.group(1), _next_nonblank(lines, i + 1))
    return s


def _parse_unittest(lines: list[str]) -> SuiteSummary | None:
    ran_at = next((i for i in range(len(lines) - 1, -1, -1) if re.match(r"^Ran \d+ tests? in", lines[i])), None)
    if ran_at is None:
        return None
    ran = int(lines[ran_at].split()[1])
    fields: dict[str, int] = {}
    for line in lines[ran_at + 1 :]:
        if m := re.match(r"^(OK|FAILED)(?: \((.*)\))?$", line.strip()):
            fields = {k.strip(): int(v) for k, v in re.findall(r"([a-z ]+)=(\d+)", m.group(2) or "")}
            break
    s = SuiteSummary("unittest")
    s.failed = fields.get("failures", 0) + fields.get("errors", 0)
    s.skipped = fields.get("skipped", 0) + fields.get("expected failures", 0)
    s.passed = ran - s.failed - s.skipped
    for i, line in enumerate(lines):
        if m := re.match(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", line):
            # The exception line is the last line of the traceback block.
            message = ""
            for nxt in lines[i + 2 :]:
                if nxt.startswith(("=====", "-----")):
                    break
                if nxt.strip():
                    message = nxt.strip()
            s.fail(m.group(2), message)
    return s


# Most specific formats first; unittest's "Ran N tests" also appears in Django output.
_PARSERS: tuple[Callable[[list[str]], SuiteSummary | None], ...] = (
    _parse_pytest,
    _parse_jest,
    _parse_vitest,
    _parse_cargo,
    _parse_go,
    _parse_maven,
    _parse_gradle,
    _parse_dotnet,
    _parse_rspec,
    _parse_phpunit,
    _parse_mocha,
    _parse_unittest,
)


def parse_test_output(output: str) -> SuiteSummary | None:
    """Pass/fail counts from a test runner's console output, or None if no format matched."""
    lines = _ANSI.sub("", output).splitlines()
    for parser in _PARSERS:
        summary = parser(lines)
        if summary is not None:
            return summary
    return None


# Where Maven, Gradle and common runner flags (--junitxml, jest-junit) leave JUnit XML reports.
_JUNIT_GLOBS = (
    "target/surefire-reports/*.xml",
    "target/failsafe-reports/*.xml",
    "*/target/surefire-reports/*.xml",
    "build/test-results/**/*.xml",
    "*/build/test-results/**/*.xml",
    "test-results/*.xml",
    "junit*.xml",
    "report.xml",
)


def parse_junit_reports(cwd: Path, confine: Path | None, since: float) -> SuiteSummary | None:
    """Summary from JUnit XML files under `cwd` written at or after `since` (epoch seconds).

    Files that resolve outside `confine` (when given), oversized files and unparsable
    files are skipped. Returns None when no fresh report exists.
    """
    files: list[Path] = []
    for pattern in _JUNIT_GLOBS:
        for path in cwd.glob(pattern):
            real = path.resolve()
            if real in files or (confine is not None and not real.is_relative_to(confine)) or not real.is_file():
                continue
            try:
                st = real.stat()
            except OSError:
                continue
            if st.st_mtime >= since and st.st_size <= _MAX_JUNIT_BYTES:
                files.append(real)
            if len(files) >= _MAX_JUNIT_FILES:
                break
    s = SuiteSummary("junit-xml")
    parsed = False
    for path in files:
        try:
            # The reports come from the workspace; Python's bundled expat refuses entity
            # expansion bombs, and the size cap bounds the rest.
            root = ET.parse(path).getroot()  # noqa: S314
        except (ET.ParseError, OSError):
            continue
        if root.tag not in ("testsuite", "testsuites"):
            continue
        parsed = True
        for case in root.iter("testcase"):
            name = ".".join(p for p in (case.get("classname"), case.get("name")) if p)
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is not None:
                s.failed += 1
                message = problem.get("message") or (problem.text or "").strip().split("\n")[0]
                s.fail(name, message)
            elif case.find("skipped") is not None:
                s.skipped += 1
            else:
                s.passed += 1
    return s if parsed else None
