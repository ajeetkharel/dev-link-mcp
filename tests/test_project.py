import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from conftest import text

from dev_link_mcp.tools.project.detect import detect_project
from dev_link_mcp.tools.project.parsers import parse_junit_reports, parse_test_output
from dev_link_mcp.workspace import Workspace

TOOLS = {"project_detect", "install_deps", "run_tests", "run_lint", "run_format", "run_build"}
RUN_TOOLS = sorted(TOOLS - {"project_detect"})


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


async def detect(connect, path: str = ".") -> dict:
    async with connect("project") as client:
        r = await client.call_tool("project_detect", {"path": path})
    assert not r.is_error, text(r)
    return r.structured_content


async def call(connect, tool: str, args: dict):
    async with connect("project") as client:
        return await client.call_tool(tool, args)


MAKEFILE = """\
VERSION := 1.0
CC ?= gcc
.PHONY: test lint lint-fix format format-check build install
%.o: %.c
\t$(CC) -c $<
install:
\techo installing > installed.txt
build: install
\techo building
test:
\techo make-test-ran
lint:
\techo lint-check
lint-fix:
\techo lint-fixing
format:
\techo formatting
format-check:
\techo format-checking
"""


# --------------------------------------------------------------------------- registration


async def test_registers_expected_tools(connect):
    async with connect("project") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    assert tools["project_detect"].annotations.read_only_hint is True
    for name in RUN_TOOLS:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True


# --------------------------------------------------------------------------- project_detect


async def test_detect_empty_directory(connect):
    data = await detect(connect)
    assert data["path"] == "."
    assert data["manifests"] == []
    assert data["commands"] == {}
    assert data["languages"] == []


async def test_detect_node_with_pnpm_scripts_and_typescript(connect, workspace):
    pkg = {
        "scripts": {"test": "vitest run", "build": "tsc -b", "lint": "eslint .", "dev": "vite", "lint:fix": "x"},
        "dependencies": {"react": "^19"},
        "devDependencies": {"vitest": "^3", "eslint": "^9", "prettier": "^3"},
    }
    write(workspace, "package.json", json.dumps(pkg))
    write(workspace, "pnpm-lock.yaml", "")
    write(workspace, "tsconfig.json", "{}")
    write(workspace, "src/app.tsx", "export {}")
    data = await detect(connect)
    assert data["package_managers"] == ["pnpm"]
    assert "TypeScript" in data["languages"]
    assert {"React", "Vitest", "ESLint", "Prettier"} <= set(data["frameworks"])
    cmds = data["commands"]
    assert cmds["install"] == "pnpm install"
    assert cmds["test"] == "pnpm test"
    assert cmds["test_target"] == "pnpm test {target}"
    assert cmds["build"] == "pnpm run build"
    assert cmds["lint"] == "pnpm run lint"
    assert cmds["lint_fix"] == "pnpm run lint:fix"
    assert cmds["format"] == "pnpm exec prettier --write ."
    assert cmds["format_check"] == "pnpm exec prettier --check ."
    assert cmds["run"] == "pnpm run dev"
    assert data["scripts"]["build"] == "tsc -b"


async def test_detect_npm_placeholder_test_script_falls_back_to_jest(connect, workspace):
    pkg = {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}, "devDependencies": {"jest": "29"}}
    write(workspace, "package.json", json.dumps(pkg))
    cmds = (await detect(connect))["commands"]
    assert cmds["install"] == "npm install"
    assert cmds["test"] == "npx jest"
    assert cmds["test_target"] == "npx jest {target}"


async def test_detect_npm_test_target_uses_double_dash(connect, workspace):
    write(workspace, "package.json", json.dumps({"scripts": {"test": "jest"}, "packageManager": "npm@10.0.0"}))
    cmds = (await detect(connect))["commands"]
    assert cmds["test_target"] == "npm test -- {target}"


async def test_detect_invalid_package_json_warns(connect, workspace):
    write(workspace, "package.json", "{not json")
    data = await detect(connect)
    assert "package.json" in data["manifests"]
    assert any("not valid JSON" in w for w in data["warnings"])


async def test_detect_uv_pyproject_with_pytest_and_ruff(connect, workspace):
    write(
        workspace,
        "pyproject.toml",
        """
[project]
name = "demo"
dependencies = ["fastapi>=0.1", "Pydantic[email]"]
[project.scripts]
demo = "demo:main"
[dependency-groups]
dev = ["pytest>=8", "ruff"]
[build-system]
requires = ["uv_build"]
build-backend = "uv_build"
""",
    )
    write(workspace, "uv.lock", "")
    write(workspace, "demo/__init__.py", "")
    data = await detect(connect)
    assert data["package_managers"] == ["uv"]
    assert data["languages"][0] == "Python"
    assert {"FastAPI", "Pydantic", "pytest", "Ruff"} <= set(data["frameworks"])
    cmds = data["commands"]
    assert cmds == {
        "install": "uv sync",
        "build": "uv build",
        "test": "uv run pytest",
        "test_target": "uv run pytest {target}",
        "lint": "uv run ruff check .",
        "lint_fix": "uv run ruff check --fix .",
        "format": "uv run ruff format .",
        "format_check": "uv run ruff format --check .",
        "run": "uv run demo",
    }


async def test_detect_poetry_with_black(connect, workspace):
    write(
        workspace,
        "pyproject.toml",
        """
[tool.poetry]
name = "demo"
[tool.poetry.dependencies]
django = "^5"
[tool.poetry.group.dev.dependencies]
black = "*"
pytest = "*"
""",
    )
    write(workspace, "manage.py", "")
    data = await detect(connect)
    assert data["package_managers"] == ["poetry"]
    assert "Django" in data["frameworks"]
    cmds = data["commands"]
    assert cmds["install"] == "poetry install"
    assert cmds["test"] == "poetry run pytest"
    assert cmds["format"] == "poetry run black ."
    assert cmds["format_check"] == "poetry run black --check ."
    assert cmds["run"] == "poetry run python manage.py runserver"


async def test_detect_plain_requirements_uses_venv(connect, workspace):
    write(workspace, "requirements.txt", "# comment\nflask==3.0\n-r other.txt\n")
    write(workspace, "tests/test_a.py", "")
    data = await detect(connect)
    assert data["package_managers"] == ["pip"]
    assert "Flask" in data["frameworks"]
    cmds = data["commands"]
    assert cmds["install"] == "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    assert cmds["test"] == "python3 -m unittest discover"
    assert "lint" not in cmds


async def test_detect_existing_venv_is_used(connect, workspace):
    write(workspace, "requirements.txt", "pytest\n")
    write(workspace, ".venv/bin/python", "")
    cmds = (await detect(connect))["commands"]
    assert cmds["install"] == ".venv/bin/pip install -r requirements.txt"
    assert cmds["test"] == ".venv/bin/pytest"


async def test_detect_invalid_pyproject_warns(connect, workspace):
    write(workspace, "pyproject.toml", "[project\nname=")
    data = await detect(connect)
    assert "pyproject.toml" in data["manifests"]
    assert any("not valid TOML" in w for w in data["warnings"])


async def test_makefile_targets_win_over_language_commands(connect, workspace):
    write(workspace, "Makefile", MAKEFILE)
    write(workspace, "pyproject.toml", '[project]\nname="x"\ndependencies=["pytest"]\n')
    data = await detect(connect)
    assert data["make_targets"] == ["install", "build", "test", "lint", "lint-fix", "format", "format-check"]
    cmds = data["commands"]
    assert cmds["test"] == "make test"
    assert cmds["lint_fix"] == "make lint-fix"
    assert cmds["format_check"] == "make format-check"
    # make has no way to take a test target, so the Python template is used.
    assert cmds["test_target"] == "python3 -m pytest {target}"
    assert data["commands_by_manifest"]["pyproject.toml"]["test"] == "python3 -m pytest"


async def test_detect_justfile(connect, workspace):
    write(
        workspace,
        "justfile",
        "set shell := ['bash', '-c']\nversion := '1'\ntest *args:\n  pytest {{args}}\nfmt:\n  x\n",
    )
    data = await detect(connect)
    assert data["just_recipes"] == ["test", "fmt"]
    assert data["commands"]["test"] == "just test"
    assert data["commands"]["test_target"] == "just test {target}"
    assert data["commands"]["format"] == "just fmt"


async def test_detect_go_module(connect, workspace):
    write(workspace, "go.mod", "module x\n\nrequire github.com/gin-gonic/gin v1.9.0\n")
    write(workspace, ".golangci.yml", "")
    write(workspace, "main.go", "package main")
    data = await detect(connect)
    assert data["languages"] == ["Go"]
    assert "Gin" in data["frameworks"]
    assert data["commands"]["test"] == "go test ./..."
    assert data["commands"]["lint"] == "golangci-lint run"
    assert data["commands"]["test_target"] == "go test {target}"


async def test_detect_rust_crate(connect, workspace):
    write(workspace, "Cargo.toml", '[package]\nname="x"\n[dependencies]\naxum = "0.7"\ntokio = {version="1"}\n')
    data = await detect(connect)
    assert data["package_managers"] == ["cargo"]
    assert {"Axum", "Tokio"} <= set(data["frameworks"])
    assert data["commands"]["format_check"] == "cargo fmt --check"


async def test_detect_maven_wrapper_spring(connect, workspace):
    write(
        workspace,
        "pom.xml",
        "<project><parent><artifactId>spring-boot-starter-parent</artifactId></parent>"
        "<plugin><artifactId>spotless-maven-plugin</artifactId></plugin></project>",
    )
    write(workspace, "mvnw", "#!/bin/sh")
    data = await detect(connect)
    assert data["languages"] == ["Java"]
    assert "Spring Boot" in data["frameworks"]
    cmds = data["commands"]
    assert cmds["test"] == "./mvnw -B test"
    assert cmds["test_target"] == "./mvnw -B test -Dtest={target}"
    assert cmds["run"] == "./mvnw -B spring-boot:run"
    assert cmds["format_check"] == "./mvnw -B spotless:check"


async def test_detect_gradle_kotlin(connect, workspace):
    write(
        workspace,
        "build.gradle.kts",
        'plugins { kotlin("jvm"); id("org.springframework.boot"); id("org.jlleitschuh.gradle.ktlint") }',
    )
    data = await detect(connect)
    assert data["languages"] == ["Kotlin"]
    cmds = data["commands"]
    assert cmds["test"] == "gradle --console=plain test"
    assert cmds["test_target"] == "gradle --console=plain test --tests {target}"
    assert cmds["run"] == "gradle --console=plain bootRun"
    assert cmds["lint_fix"] == "gradle --console=plain ktlintFormat"


async def test_detect_dotnet_project(connect, workspace):
    write(workspace, "App.csproj", '<Project Sdk="Microsoft.NET.Sdk.Web"><PackageReference Include="xunit"/></Project>')
    data = await detect(connect)
    assert data["languages"] == ["C#"]
    assert {"ASP.NET Core", "xUnit"} <= set(data["frameworks"])
    assert data["commands"]["test"] == "dotnet test"
    assert data["commands"]["format_check"] == "dotnet format --verify-no-changes"
    assert data["commands"]["run"] == "dotnet run"


async def test_detect_dotnet_multiple_solutions_names_one(connect, workspace):
    write(workspace, "A.sln", "")
    write(workspace, "B.sln", "")
    cmds = (await detect(connect))["commands"]
    assert cmds["build"] == "dotnet build A.sln"
    assert "run" not in cmds


async def test_detect_composer_laravel(connect, workspace):
    composer = {"require": {"laravel/framework": "^11"}, "require-dev": {"phpunit/phpunit": "^10", "laravel/pint": "1"}}
    write(workspace, "composer.json", json.dumps(composer))
    data = await detect(connect)
    assert "Laravel" in data["frameworks"]
    cmds = data["commands"]
    assert cmds["test"] == "vendor/bin/phpunit"
    assert cmds["format_check"] == "vendor/bin/pint --test"
    assert cmds["run"] == "php artisan serve"


async def test_detect_rails_with_rspec(connect, workspace):
    write(
        workspace, "Gemfile", "source 'https://rubygems.org'\ngem 'rails', '~> 7'\ngem \"rspec-rails\"\ngem 'rubocop'\n"
    )
    data = await detect(connect)
    assert "Rails" in data["frameworks"]
    cmds = data["commands"]
    assert cmds["test"] == "bundle exec rspec"
    assert cmds["lint_fix"] == "bundle exec rubocop -a"
    assert cmds["run"] == "bin/rails server"


async def test_detect_compose_services_and_subprojects(connect, workspace):
    write(workspace, "compose.yaml", "services:\n  web: {image: nginx}\n  db: {image: postgres}\n")
    write(workspace, "Dockerfile", "FROM scratch")
    write(workspace, "services/api/go.mod", "module api\n")
    write(workspace, "frontend/package.json", "{}")
    write(workspace, "node_modules/dep/package.json", "{}")
    data = await detect(connect)
    assert data["compose_services"] == ["web", "db"]
    assert {"compose.yaml", "Dockerfile"} <= set(data["manifests"])
    paths = {s["path"]: s["manifests"] for s in data["subprojects"]}
    assert paths == {"frontend": ["package.json"], "services/api": ["go.mod"]}


async def test_detect_invalid_compose_yaml_warns(connect, workspace):
    write(workspace, "compose.yml", "services: [unclosed")
    data = await detect(connect)
    assert any("not valid YAML" in w for w in data["warnings"])


async def test_detect_subdirectory(connect, workspace):
    write(workspace, "web/package.json", json.dumps({"scripts": {"build": "vite build"}}))
    data = await detect(connect, "web")
    assert data["path"] == "web"
    assert data["commands"]["build"] == "npm run build"


async def test_detect_languages_ranked_by_file_count(connect, workspace):
    for i in range(3):
        write(workspace, f"src/m{i}.py", "")
    write(workspace, "web/app.ts", "")
    write(workspace, "node_modules/x/index.js", "")
    write(workspace, ".hidden/a.rb", "")
    data = await detect(connect)
    assert data["languages"] == ["Python", "TypeScript"]


@pytest.mark.bwrap
async def test_detect_symlinked_manifest_outside_is_not_read(connect, workspace, tmp_path):
    outside = write(tmp_path, "secret/Makefile", "leak-target:\n\techo hi\n")
    (workspace / "Makefile").symlink_to(outside)
    data = await detect(connect)
    assert data["make_targets"] == []
    assert "leak-target" not in json.dumps(data)
    assert any("outside the workspace" in w for w in data["warnings"])


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("pyproject.toml", 'project = "x"\n', ("python manifest has an unexpected structure",)),
        ("pyproject.toml", "[project]\ndependencies = 1\n", ("python manifest has an unexpected structure",)),
        ("Cargo.toml", "workspace = 1\n", ("rust manifest has an unexpected structure",)),
        # Python 3.14 bounds json's recursion by the C stack left, so with a large stack the array
        # parses (and is not an object) and with a small one it raises RecursionError.
        (
            "package.json",
            "[" * 100_000 + "]" * 100_000,
            ("node manifest has an unexpected structure", "package.json does not contain an object"),
        ),
        ("compose.yaml", "[" * 50_000 + "]" * 50_000, ("compose manifest has an unexpected structure",)),
    ],
    ids=["pyproject-string", "pyproject-dependencies", "cargo-workspace", "package-json-nested", "compose-nested"],
)
async def test_detect_wrongly_typed_manifest_warns(connect, workspace, name, content, expected):
    write(workspace, name, content)
    write(workspace, "go.mod", "module example.com/x\n")
    data = await detect(connect)
    assert any(w.startswith(expected) for w in data["warnings"])
    # The other detectors still ran.
    assert data["commands"]["test"] == "go test ./..."


async def test_detect_fifo_manifest_is_skipped(connect, workspace):
    write(workspace, "setup.py", "")
    os.mkfifo(workspace / "requirements-dev.txt")
    async with asyncio.timeout(10):
        data = await detect(connect)
    assert any("requirements-dev.txt is not a regular file" in w for w in data["warnings"])
    assert data["package_managers"] == ["pip"]


async def test_detect_long_whitespace_lines_are_fast(connect, workspace):
    blank = " " * 300_000
    write(workspace, "Makefile", f"a{blank}b\ntest:\n\techo t\n")
    write(workspace, "justfile", f"a{blank}b\nlint:\n  echo l\n")
    write(workspace, "Gemfile", "\n" * 300_000 + "gem 'rails'\n")
    started = time.monotonic()
    data = await detect(connect)
    assert time.monotonic() - started < 5
    assert data["make_targets"] == ["test"]
    assert data["just_recipes"] == ["lint"]
    assert "Rails" in data["frameworks"]


def test_detect_unconfined_directory_outside_root(workspace, tmp_path):
    outside = tmp_path / "elsewhere"
    write(outside, "Makefile", "test:\n\techo t\n")
    write(outside, "svc/go.mod", "module example.com/svc\n")
    det = detect_project(outside, Workspace(workspace, confined=False))
    assert det.make_targets == ["test"]
    assert det.subprojects == [{"path": str(outside / "svc"), "manifests": ["go.mod"]}]


def test_junit_unconfined_reads_reports_outside_root(workspace, tmp_path):
    outside = tmp_path / "elsewhere"
    write(outside, "junit.xml", '<testsuite><testcase name="a"/></testsuite>')
    assert parse_junit_reports(outside, workspace, 0) is None
    s = parse_junit_reports(outside, None, 0)
    assert s is not None and s.passed == 1


async def test_detect_bun_test_script_runs_the_script(connect, workspace):
    write(workspace, "package.json", json.dumps({"scripts": {"test": "vitest run", "build": "tsc"}}))
    write(workspace, "bun.lock", "")
    cmds = (await detect(connect))["commands"]
    # `bun test` would start bun's built-in runner instead of the script.
    assert cmds["test"] == "bun run test"
    assert cmds["test_target"] == "bun run test {target}"
    assert cmds["build"] == "bun run build"


@pytest.mark.parametrize("tool", ["project_detect", *RUN_TOOLS])
@pytest.mark.bwrap
async def test_path_escape_rejected(connect, tool, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)
    for bad in ("../", str(outside), "link"):
        r = await call(
            connect, tool, {"path": bad, "command": "echo hi"} if tool != "project_detect" else {"path": bad}
        )
        assert r.is_error, (tool, bad)
        assert "outside the workspace" in text(r)


@pytest.mark.parametrize("tool", ["project_detect", *RUN_TOOLS])
async def test_path_must_be_existing_directory(connect, tool, workspace):
    write(workspace, "file.txt", "x")
    for bad, msg in (("missing", "does not exist"), ("file.txt", "not a directory")):
        args = {"path": bad} if tool == "project_detect" else {"path": bad, "command": "echo hi"}
        r = await call(connect, tool, args)
        assert r.is_error
        assert msg in text(r)


# --------------------------------------------------------------------------- run tools


@pytest.mark.parametrize("tool", RUN_TOOLS)
async def test_no_detected_command_is_actionable_error(connect, tool):
    r = await call(connect, tool, {})
    assert r.is_error
    assert "Pass `command` explicitly" in text(r)


@pytest.mark.parametrize("tool", RUN_TOOLS)
async def test_empty_command_rejected(connect, tool):
    r = await call(connect, tool, {"command": "  "})
    assert r.is_error
    assert "command is empty" in text(r)


async def test_makefile_driven_tools(connect, workspace):
    write(workspace, "Makefile", MAKEFILE)
    async with connect("project") as client:
        install = text(await client.call_tool("install_deps", {}))
        build = text(await client.call_tool("run_build", {}))
        lint = text(await client.call_tool("run_lint", {}))
        lint_fix = text(await client.call_tool("run_lint", {"fix": True}))
        fmt = text(await client.call_tool("run_format", {}))
        fmt_check = text(await client.call_tool("run_format", {"check": True}))
        tests = (await client.call_tool("run_tests", {})).structured_content
    assert install.startswith("$ make install\n(cwd: .)\nexit_code=0")
    assert (workspace / "installed.txt").read_text().strip() == "installing"
    assert "building" in build
    assert "lint-check" in lint
    assert "lint-fixing" in lint_fix
    assert "$ make format\n" in fmt
    assert "format-checking" in fmt_check
    assert tests["command"] == "make test"
    assert tests["ok"] is True
    assert "make-test-ran" in tests["output"]
    assert tests["passed"] is None and tests["summary_format"] is None


async def test_lint_fix_missing_is_error(connect, workspace):
    write(workspace, "Makefile", "lint:\n\techo lint\n")
    r = await call(connect, "run_lint", {"fix": True})
    assert r.is_error
    assert "no lint fix command detected" in text(r)


async def test_explicit_command_runs_in_subdirectory(connect, workspace):
    (workspace / "sub").mkdir()
    r = await call(connect, "run_build", {"path": "sub", "command": "pwd; echo out > built.txt; exit 3"})
    assert not r.is_error
    out = text(r)
    assert "(cwd: sub)" in out
    assert "exit_code=3" in out
    assert str(workspace / "sub") in out
    assert (workspace / "sub" / "built.txt").exists()


async def test_run_tests_parses_pytest_output(connect, workspace):
    write(
        workspace,
        "out.txt",
        "tests/test_a.py .F.s\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_two - AssertionError: assert 1 == 2\n"
        "=================== 1 failed, 2 passed, 1 skipped in 0.12s ====================\n",
    )
    r = await call(connect, "run_tests", {"command": "cat out.txt; exit 1"})
    assert not r.is_error
    data = r.structured_content
    assert data["ok"] is False and data["exit_code"] == 1
    assert data["summary_format"] == "pytest"
    assert (data["passed"], data["failed"], data["skipped"]) == (2, 1, 1)
    assert data["failures"] == [{"name": "tests/test_a.py::test_two", "message": "AssertionError: assert 1 == 2"}]


async def test_run_tests_target_appended_to_explicit_command(connect):
    r = await call(connect, "run_tests", {"command": "echo got", "target": "a b;rm -rf x"})
    data = r.structured_content
    assert data["command"] == "echo got 'a b;rm -rf x'"
    assert "got a b;rm -rf x" in data["output"]


async def test_run_tests_target_uses_detected_template(connect, workspace):
    write(workspace, "justfile", "test *args:\n  echo {{args}}\n")
    r = await call(connect, "run_tests", {"target": "tests/x.py::t"})
    assert not r.is_error
    assert r.structured_content["command"] == "just test tests/x.py::t"


async def test_run_tests_target_unsupported_for_make_only(connect, workspace):
    write(workspace, "Makefile", "test:\n\techo hi\n")
    r = await call(connect, "run_tests", {"target": "x"})
    assert r.is_error
    assert "cannot add a target" in text(r) and "make test" in text(r)


async def test_run_tests_empty_target_rejected(connect):
    r = await call(connect, "run_tests", {"command": "true", "target": " "})
    assert r.is_error


async def test_run_tests_reads_fresh_junit_reports(connect, workspace):
    write(
        workspace,
        "fixture.xml",
        '<testsuite name="s"><testcase classname="AppTest" name="ok"/>'
        '<testcase classname="AppTest" name="bad"><failure message="expected 1">trace</failure></testcase>'
        '<testcase classname="AppTest" name="err"><error>boom\nmore</error></testcase>'
        '<testcase classname="AppTest" name="skip"><skipped/></testcase></testsuite>',
    )
    cmd = "mkdir -p target/surefire-reports && cp fixture.xml target/surefire-reports/TEST-App.xml"
    r = await call(connect, "run_tests", {"command": cmd})
    data = r.structured_content
    assert data["summary_format"] == "junit-xml"
    assert (data["passed"], data["failed"], data["skipped"]) == (1, 2, 1)
    assert data["failures"] == [
        {"name": "AppTest.bad", "message": "expected 1"},
        {"name": "AppTest.err", "message": "boom"},
    ]


async def test_run_tests_ignores_stale_junit_reports(connect, workspace):
    report = write(workspace, "target/surefire-reports/TEST-old.xml", '<testsuite><testcase name="a"/></testsuite>')
    old = time.time() - 3600
    os.utime(report, (old, old))
    r = await call(connect, "run_tests", {"command": "echo nothing"})
    assert r.structured_content["summary_format"] is None
    assert r.structured_content["passed"] is None


async def test_run_tests_timeout(connect):
    r = await call(connect, "run_tests", {"command": "sleep 10", "timeout_s": 0.5})
    data = r.structured_content
    assert data["timed_out"] is True
    assert data["ok"] is False
    assert "TIMED OUT" in data["output"]


async def test_timeout_must_be_positive(connect):
    r = await call(connect, "run_build", {"command": "true", "timeout_s": 0})
    assert r.is_error


# --------------------------------------------------------------------------- junit parser


def test_junit_symlink_outside_workspace_is_ignored(workspace, tmp_path):
    outside = write(tmp_path, "elsewhere/r.xml", '<testsuite><testcase name="a"/></testsuite>')
    (workspace / "target").mkdir()
    (workspace / "target" / "surefire-reports").symlink_to(outside.parent)
    assert parse_junit_reports(workspace, workspace, 0) is None


def test_junit_testsuites_root_and_garbage(workspace):
    write(
        workspace,
        "build/test-results/test/a.xml",
        '<testsuites><testsuite><testcase name="a"/></testsuite></testsuites>',
    )
    write(workspace, "build/test-results/test/b.xml", "<not-closed")
    write(workspace, "build/test-results/test/c.xml", "<other/>")
    s = parse_junit_reports(workspace, workspace, 0)
    assert s is not None and (s.passed, s.failed) == (1, 0)


# --------------------------------------------------------------------------- output parsers

SAMPLES = {
    "pytest-quiet": ("..F\n1 failed, 2 passed, 3 warnings in 0.50s\n", "pytest", (2, 1, 0)),
    "pytest-errors": (
        "ERROR tests/t.py - ImportError\n==== 1 error, 1 xfailed in 1.2s (0:00:01) ====\n",
        "pytest",
        (0, 1, 1),
    ),
    "pytest-none": ("==== no tests ran in 0.01s ====\n", "pytest", (0, 0, 0)),
    "jest": (
        "  ● Math › adds\n\n    expect(received).toBe(expected)\n"
        "  ● Console\n\n"
        "Tests:       1 failed, 1 skipped, 4 passed, 6 total\n",
        "jest",
        (4, 1, 1),
    ),
    "vitest": (
        " FAIL  src/a.test.ts > math > adds\nAssertionError: expected 3 to be 4\n"
        " Test Files  1 failed (1)\n      Tests  1 failed | 2 passed (3)\n",
        "vitest",
        (2, 1, 0),
    ),
    "mocha": (
        "  3 passing (12ms)\n  1 pending\n  1 failing\n\n  1) Array\n       #indexOf():\n     Error: nope\n",
        "mocha",
        (3, 1, 1),
    ),
    "cargo": (
        "test a ... ok\ntest b ... FAILED\n\nfailures:\n\n---- b stdout ----\nthread 'b' panicked at src/lib.rs:5:\n"
        "assertion failed\n\ntest result: FAILED. 1 passed; 1 failed; 2 ignored; 0 measured\n"
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured\n",
        "cargo",
        (4, 1, 2),
    ),
    "go-verbose": (
        "=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n--- FAIL: TestB (0.00s)\n"
        "    b_test.go:9: want 1 got 2\n--- SKIP: TestC (0.00s)\nFAIL\nFAIL\texample.com/x\t0.01s\n",
        "go test",
        (1, 1, 1),
    ),
    "go-packages-with-failure": (
        "--- FAIL: TestA (0.00s)\n    a_test.go:5: boom\nFAIL\nFAIL\texample.com/a\t0.01s\n"
        "ok  \texample.com/b\t0.02s\nFAIL\n",
        "go test (per package)",
        (1, 1, 0),
    ),
    "go-packages": (
        "ok  \texample.com/a\t0.01s\n?   \texample.com/b\t[no test files]\nok  \texample.com/c\t(cached)\n",
        "go test (per package)",
        (2, 0, 1),
    ),
    "maven": (
        "[INFO] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s\n"
        "[ERROR] Failures: \n[ERROR]   AppTest.testIt:12 expected: <1> but was: <2>\n"
        "[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 1\n",
        "maven",
        (2, 2, 1),
    ),
    "gradle": (
        "AppTest > testIt() FAILED\n    org.opentest4j.AssertionFailedError at AppTest.java:10\n"
        "5 tests completed, 1 failed, 1 skipped\n",
        "gradle",
        (3, 1, 1),
    ),
    "dotnet": (
        "  Failed Tests.UnitTest1.Adds [12 ms]\n  Error Message:\n   Assert.Equal() Failure\n"
        "Failed!  - Failed:     1, Passed:     4, Skipped:     1, Total:     6, Duration: 20 ms\n",
        "dotnet test",
        (4, 1, 1),
    ),
    "rspec": (
        "3 examples, 1 failure, 1 pending\n\nFailed examples:\n\nrspec ./spec/a_spec.rb:4 # A does b\n",
        "rspec",
        (1, 1, 1),
    ),
    "phpunit-ok": ("OK (5 tests, 9 assertions)\n", "phpunit", (5, 0, 0)),
    "phpunit-fail": (
        "1) Tests\\FooTest::testBar\nFailed asserting that false is true.\n\n"
        "FAILURES!\nTests: 4, Assertions: 4, Failures: 1, Skipped: 1.\n",
        "phpunit",
        (2, 1, 1),
    ),
    "unittest": (
        "======================================================================\n"
        "FAIL: test_b (t.T.test_b)\n"
        "----------------------------------------------------------------------\n"
        'Traceback (most recent call last):\n  File "t.py", line 5, in test_b\n'
        "AssertionError: 1 != 2\n\n"
        "----------------------------------------------------------------------\n"
        "Ran 4 tests in 0.001s\n\nFAILED (failures=1, skipped=1)\n",
        "unittest",
        (2, 1, 1),
    ),
}


@pytest.mark.parametrize("name", SAMPLES)
def test_parse_test_output_formats(name):
    output, fmt, counts = SAMPLES[name]
    s = parse_test_output(output)
    assert s is not None
    assert s.format == fmt
    assert (s.passed, s.failed, s.skipped) == counts


def test_parse_failure_details():
    assert parse_test_output(SAMPLES["jest"][0]).failures == [
        {"name": "Math › adds", "message": "expect(received).toBe(expected)"}
    ]
    assert parse_test_output(SAMPLES["cargo"][0]).failures == [
        {"name": "b", "message": "thread 'b' panicked at src/lib.rs:5: assertion failed"}
    ]
    assert parse_test_output(SAMPLES["go-verbose"][0]).failures == [
        {"name": "TestB", "message": "b_test.go:9: want 1 got 2"}
    ]
    assert parse_test_output(SAMPLES["maven"][0]).failures == [
        {"name": "AppTest.testIt", "message": "expected: <1> but was: <2>"}
    ]
    assert parse_test_output(SAMPLES["gradle"][0]).failures[0]["name"] == "AppTest.testIt()"
    assert parse_test_output(SAMPLES["dotnet"][0]).failures == [
        {"name": "Tests.UnitTest1.Adds", "message": "Assert.Equal() Failure"}
    ]
    assert parse_test_output(SAMPLES["rspec"][0]).failures == [{"name": "A does b", "message": "./spec/a_spec.rb:4"}]
    assert parse_test_output(SAMPLES["phpunit-fail"][0]).failures == [
        {"name": "Tests\\FooTest::testBar", "message": "Failed asserting that false is true."}
    ]
    assert parse_test_output(SAMPLES["unittest"][0]).failures == [
        {"name": "t.T.test_b", "message": "AssertionError: 1 != 2"}
    ]
    assert parse_test_output(SAMPLES["go-packages-with-failure"][0]).failures == [
        {"name": "TestA", "message": "a_test.go:5: boom"}
    ]
    assert parse_test_output(SAMPLES["mocha"][0]).failures == [{"name": "Array", "message": "#indexOf():"}]


def test_parse_strips_ansi_and_caps_failures():
    lines = [f"FAILED t.py::test_{i} - boom" for i in range(80)]
    output = "\x1b[31m" + "\n".join(lines) + "\n\x1b[1m==== 80 failed in 1.00s ====\x1b[0m\n"
    s = parse_test_output(output)
    assert s is not None and s.failed == 80
    assert len(s.failures) == 50


def test_parse_long_whitespace_line_is_fast():
    output = "=" + " " * 300_000 + "x\n" + "a" + "\t" * 300_000 + "b\n2 passed in 0.10s\n"
    started = time.monotonic()
    s = parse_test_output(output)
    assert time.monotonic() - started < 2
    assert s is not None and s.passed == 2


def test_parse_unknown_output_returns_none():
    assert parse_test_output("hello\nworld\n") is None
    assert parse_test_output("") is None
