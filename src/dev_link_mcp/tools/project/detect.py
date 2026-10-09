"""Project detection: reads manifests and build files under a directory and works out the
languages, package managers, frameworks and install/test/lint/format/build commands."""

import json
import os
import re
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dev_link_mcp.workspace import Workspace

# Command kinds a detector can fill. `test_target` holds a `{target}` placeholder for
# running one test file, package or name.
KINDS = ("install", "build", "test", "test_target", "lint", "lint_fix", "format", "format_check", "run")

_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
        ".gradle", ".next", ".nuxt", ".cache", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "vendor", "bin", "obj", ".idea", ".vscode", "coverage",
    }
)  # fmt: skip

_EXTENSIONS = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic", ".php": "PHP", ".rb": "Ruby", ".swift": "Swift",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".m": "Objective-C",
    ".sh": "Shell", ".bash": "Shell", ".lua": "Lua", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
    ".erl": "Erlang", ".hs": "Haskell", ".clj": "Clojure", ".r": "R", ".jl": "Julia", ".pl": "Perl",
    ".vue": "Vue", ".svelte": "Svelte", ".zig": "Zig", ".sql": "SQL",
}  # fmt: skip

_MANIFEST_NAMES = frozenset(
    {
        "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "Makefile", "makefile",
        "GNUmakefile", "justfile", "Justfile", ".justfile", "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "go.mod", "Cargo.toml", "composer.json", "Gemfile",
        "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "Dockerfile",
    }
)  # fmt: skip
_DOTNET_SUFFIXES = (".sln", ".csproj", ".fsproj", ".vbproj")

_MAX_SCAN_FILES = 5000
_MAX_SCAN_DEPTH = 4
_MAX_MANIFEST_BYTES = 2_000_000


@dataclass
class Detection:
    """Everything project_detect learned about one directory."""

    # Manifests are only read when they resolve inside this root, so a symlinked
    # Makefile cannot be used to read files outside the workspace. None when the
    # workspace is unconfined (--no-sandbox).
    confine: Path | None
    languages: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    # Per manifest, in precedence order: {kind: command}.
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)
    make_targets: list[str] = field(default_factory=list)
    just_recipes: list[str] = field(default_factory=list)
    compose_services: list[str] = field(default_factory=list)
    subprojects: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, attr: str, *values: str) -> None:
        items: list[str] = getattr(self, attr)
        for value in values:
            if value and value not in items:
                items.append(value)

    def source(self, manifest: str, commands: dict[str, str | None]) -> None:
        self.add("manifests", manifest)
        cleaned = {k: v for k, v in commands.items() if v}
        if cleaned:
            self.sources[manifest] = cleaned

    def commands(self) -> dict[str, str]:
        """First command per kind across sources; earlier sources win."""
        chosen: dict[str, str] = {}
        for cmds in self.sources.values():
            for kind in KINDS:
                if kind in cmds and kind not in chosen:
                    chosen[kind] = cmds[kind]
        return chosen

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": self.languages,
            "manifests": self.manifests,
            "package_managers": self.package_managers,
            "frameworks": self.frameworks,
            "commands": self.commands(),
            "commands_by_manifest": self.sources,
            "scripts": self.scripts,
            "make_targets": self.make_targets,
            "just_recipes": self.just_recipes,
            "compose_services": self.compose_services,
            "subprojects": self.subprojects,
            "warnings": self.warnings,
        }


def _read_text(path: Path, det: Detection) -> str | None:
    real = path.resolve()
    if det.confine is not None and not real.is_relative_to(det.confine):
        det.warnings.append(f"{path.name} points outside the workspace; skipped")
        return None
    # Reading a FIFO or device named like a manifest would block the worker thread forever.
    if not real.is_file():
        det.warnings.append(f"{path.name} is not a regular file; skipped")
        return None
    try:
        if real.stat().st_size > _MAX_MANIFEST_BYTES:
            det.warnings.append(f"{path.name} is larger than {_MAX_MANIFEST_BYTES} bytes; skipped")
            return None
        return real.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        det.warnings.append(f"cannot read {path.name}: {exc.strerror or exc}")
        return None


def _first_existing(root: Path, *names: str) -> Path | None:
    for name in names:
        if (root / name).is_file():
            return root / name
    return None


def _pick(names: dict[str, str], candidates: tuple[str, ...], fmt: Callable[[str], str]) -> str | None:
    for name in candidates:
        if name in names:
            return fmt(name)
    return None


# Target and script names that map to each command kind, most specific first.
_TASK_NAMES: dict[str, tuple[str, ...]] = {
    "install": ("install", "deps", "setup", "bootstrap"),
    "build": ("build", "compile"),
    "test": ("test", "tests"),
    "lint": ("lint", "vet"),
    "lint_fix": ("lint-fix", "lint:fix", "fix"),
    "format": ("format", "fmt"),
    "format_check": ("format-check", "format:check", "fmt-check", "check-format"),
    "run": ("run", "dev", "serve", "start"),
}

# No two adjacent quantifiers may match the same characters: Makefiles and justfiles
# come from the workspace and a long whitespace run would otherwise backtrack quadratically.
_MAKE_TARGET = re.compile(r"^([^\s:#=][^:#=]*)::?(?!=)")
_JUST_RECIPE = re.compile(r"^@?([A-Za-z_][\w-]*)(?:\s[^:=]*)?:(?!=)")


def _detect_make(root: Path, det: Detection) -> None:
    path = _first_existing(root, "GNUmakefile", "makefile", "Makefile")
    if path is None:
        return
    text = _read_text(path, det)
    if text is None:
        return
    targets: list[str] = []
    for line in text.splitlines():
        m = _MAKE_TARGET.match(line)
        if not m:
            continue
        for name in m.group(1).split():
            # Pattern rules, variables and special targets (.PHONY) are not runnable tasks.
            if name.startswith(".") or "%" in name or "$" in name or name in targets:
                continue
            targets.append(name)
    det.make_targets = targets
    names = {t: t for t in targets}
    det.source(path.name, {kind: _pick(names, cands, lambda t: f"make {t}") for kind, cands in _TASK_NAMES.items()})


def _detect_just(root: Path, det: Detection) -> None:
    path = _first_existing(root, "justfile", "Justfile", ".justfile")
    if path is None:
        return
    text = _read_text(path, det)
    if text is None:
        return
    recipes: list[str] = []
    for line in text.splitlines():
        if line.startswith(("set ", "alias ", "export ", "import ", "mod ")):
            continue
        m = _JUST_RECIPE.match(line)
        if m and m.group(1) not in recipes:
            recipes.append(m.group(1))
    det.just_recipes = recipes
    names = {r: r for r in recipes}
    commands: dict[str, str | None] = {
        kind: _pick(names, cands, lambda r: f"just {r}") for kind, cands in _TASK_NAMES.items()
    }
    if commands["test"]:
        # just passes extra words to the recipe as arguments.
        commands["test_target"] = commands["test"] + " {target}"
    det.source(path.name, commands)


_NPM_PLACEHOLDER_TEST = "no test specified"

_JS_FRAMEWORKS = {
    "next": "Next.js", "react": "React", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit", "@angular/core": "Angular", "astro": "Astro", "@remix-run/react": "Remix",
    "solid-js": "Solid", "express": "Express", "fastify": "Fastify", "koa": "Koa", "@nestjs/core": "NestJS",
    "hono": "Hono", "electron": "Electron", "vite": "Vite", "webpack": "webpack", "jest": "Jest",
    "vitest": "Vitest", "mocha": "Mocha", "@playwright/test": "Playwright", "cypress": "Cypress",
    "eslint": "ESLint", "prettier": "Prettier", "@biomejs/biome": "Biome", "typescript": "TypeScript",
}  # fmt: skip


def _detect_node(root: Path, det: Detection) -> None:
    path = root / "package.json"
    if not path.is_file():
        return
    text = _read_text(path, det)
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        det.warnings.append(f"package.json is not valid JSON: {exc}")
        det.add("manifests", "package.json")
        return
    if not isinstance(data, dict):
        det.warnings.append("package.json does not contain an object")
        det.add("manifests", "package.json")
        return

    declared = str(data.get("packageManager") or "").split("@")[0]
    if declared in ("npm", "pnpm", "yarn", "bun"):
        pm = declared
    elif (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pm = "bun"
    else:
        pm = "npm"
    det.add("package_managers", pm)
    execute = {"npm": "npx", "pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}[pm]

    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    uses_ts = "typescript" in deps or (root / "tsconfig.json").exists()
    det.add("languages", "TypeScript" if uses_ts else "JavaScript")
    det.add("frameworks", *(label for name, label in _JS_FRAMEWORKS.items() if name in deps))

    raw_scripts = data.get("scripts")
    scripts = {k: str(v) for k, v in raw_scripts.items()} if isinstance(raw_scripts, dict) else {}
    if _NPM_PLACEHOLDER_TEST in scripts.get("test", ""):
        scripts.pop("test")
    det.scripts = scripts

    def script(name: str) -> str:
        # `bun test` is bun's own test runner, not the package.json script.
        return f"{pm} test" if name == "test" and pm != "bun" else f"{pm} run {name}"

    commands: dict[str, str | None] = {
        kind: _pick(scripts, cands, script) for kind, cands in _TASK_NAMES.items() if kind != "install"
    }
    commands["install"] = f"{pm} install"
    if commands["test"]:
        # npm needs `--` to forward arguments to the script; the others forward them as is.
        commands["test_target"] = commands["test"] + (" -- {target}" if pm == "npm" else " {target}")
    elif "vitest" in deps:
        commands["test"] = f"{execute} vitest run"
        commands["test_target"] = f"{execute} vitest run {{target}}"
    elif "jest" in deps:
        commands["test"] = f"{execute} jest"
        commands["test_target"] = f"{execute} jest {{target}}"
    if "eslint" in deps:
        commands["lint"] = commands["lint"] or f"{execute} eslint ."
        commands["lint_fix"] = commands["lint_fix"] or f"{execute} eslint . --fix"
    if "prettier" in deps:
        commands["format"] = commands["format"] or f"{execute} prettier --write ."
        commands["format_check"] = commands["format_check"] or f"{execute} prettier --check ."
    det.source("package.json", commands)


_PY_FRAMEWORKS = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "starlette": "Starlette", "aiohttp": "aiohttp",
    "tornado": "Tornado", "pyramid": "Pyramid", "sanic": "Sanic", "litestar": "Litestar", "streamlit": "Streamlit",
    "celery": "Celery", "sqlalchemy": "SQLAlchemy", "pydantic": "Pydantic", "pytest": "pytest", "ruff": "Ruff",
    "black": "Black", "mypy": "mypy", "flake8": "Flake8", "pandas": "pandas", "numpy": "NumPy",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "mcp": "MCP",
}  # fmt: skip
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_name(requirement: str) -> str | None:
    m = _REQ_NAME.match(requirement)
    return m.group(1).lower().replace("_", "-").split("[")[0] if m else None


def _python_deps(pyproject: dict[str, Any], root: Path, det: Detection) -> set[str]:
    """Normalized dependency names from pyproject tables and requirements files."""
    reqs: list[str] = []
    project = pyproject.get("project") or {}
    reqs += project.get("dependencies") or []
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += group
    for group in (pyproject.get("dependency-groups") or {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    tool = pyproject.get("tool") or {}
    reqs += (tool.get("uv") or {}).get("dev-dependencies") or []
    names = {n for r in reqs if isinstance(r, str) and (n := _req_name(r))}
    poetry = tool.get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    tables += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        names.update(k.lower().replace("_", "-") for k in table if isinstance(k, str))
    for req_file in sorted(root.glob("requirements*.txt")):
        text = _read_text(req_file, det)
        for line in (text or "").splitlines():
            if not line.lstrip().startswith(("#", "-")) and (n := _req_name(line)):
                names.add(n)
    return names


def _detect_python(root: Path, det: Detection) -> None:
    pyproject_path = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    setup_py = root / "setup.py"
    pipfile = root / "Pipfile"
    if not any(p.is_file() for p in (pyproject_path, requirements, setup_py, pipfile)):
        return
    pyproject: dict[str, Any] = {}
    if pyproject_path.is_file():
        text = _read_text(pyproject_path, det)
        if text is not None:
            try:
                pyproject = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                det.warnings.append(f"pyproject.toml is not valid TOML: {exc}")
    tool = pyproject.get("tool") or {}

    if (root / "uv.lock").exists() or "uv" in tool:
        pm = "uv"
    elif (root / "poetry.lock").exists() or "poetry" in tool:
        pm = "poetry"
    elif (root / "pdm.lock").exists() or "pdm" in tool:
        pm = "pdm"
    elif pipfile.is_file():
        pm = "pipenv"
    else:
        pm = "pip"
    det.add("package_managers", pm)
    det.add("languages", "Python")

    venv = next((v for v in (".venv", "venv") if (root / v / "bin" / "python").exists()), None)
    prefix = {"uv": "uv run ", "poetry": "poetry run ", "pdm": "pdm run ", "pipenv": "pipenv run "}.get(pm)

    def tool_cmd(name: str) -> str:
        if prefix:
            return prefix + name
        if venv:
            return f"{venv}/bin/{name}"
        return f"python3 -m {name}"

    python = (prefix + "python") if prefix else (f"{venv}/bin/python" if venv else "python3")
    deps = _python_deps(pyproject, root, det)
    det.add("frameworks", *(label for name, label in _PY_FRAMEWORKS.items() if name in deps))

    has_ruff = "ruff" in deps or "ruff" in tool or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists()
    has_pytest = (
        "pytest" in deps
        or "pytest" in tool
        or (root / "pytest.ini").exists()
        or (root / "conftest.py").exists()
        or (root / "tests" / "conftest.py").exists()
    )
    commands: dict[str, str | None] = {}
    if pm in ("uv", "poetry", "pdm"):
        commands["install"] = {"uv": "uv sync", "poetry": "poetry install", "pdm": "pdm install"}[pm]
    elif pm == "pipenv":
        commands["install"] = "pipenv install --dev"
    else:
        # System Pythons are usually "externally managed", so installs go into a venv.
        pip = f"{venv}/bin/pip" if venv else "python3 -m venv .venv && .venv/bin/pip"
        what = "-e ." if pyproject_path.is_file() or setup_py.is_file() else "-r requirements.txt"
        commands["install"] = f"{pip} install {what}"
    if "build-system" in pyproject:
        commands["build"] = {"uv": "uv build", "poetry": "poetry build", "pdm": "pdm build"}.get(
            pm, f"{python} -m build"
        )
    manage = (root / "manage.py").is_file()
    if has_pytest:
        commands["test"] = tool_cmd("pytest")
        commands["test_target"] = tool_cmd("pytest") + " {target}"
    elif manage:
        commands["test"] = f"{python} manage.py test"
        commands["test_target"] = f"{python} manage.py test {{target}}"
    elif (root / "tests").is_dir() or (root / "test").is_dir():
        commands["test"] = f"{python} -m unittest discover"
        commands["test_target"] = f"{python} -m unittest {{target}}"
    if has_ruff:
        commands["lint"] = tool_cmd("ruff") + " check ."
        commands["lint_fix"] = tool_cmd("ruff") + " check --fix ."
    elif "flake8" in deps:
        commands["lint"] = tool_cmd("flake8")
    if "black" in deps or "black" in tool:
        commands["format"] = tool_cmd("black") + " ."
        commands["format_check"] = tool_cmd("black") + " --check ."
    elif has_ruff:
        commands["format"] = tool_cmd("ruff") + " format ."
        commands["format_check"] = tool_cmd("ruff") + " format --check ."
    scripts = (pyproject.get("project") or {}).get("scripts") or {}
    if manage:
        commands["run"] = f"{python} manage.py runserver"
    elif scripts and prefix:
        commands["run"] = prefix + next(iter(scripts))
    manifest = next(p.name for p in (pyproject_path, requirements, setup_py, pipfile) if p.is_file())
    det.source(manifest, commands)


_GO_FRAMEWORKS = {
    "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo": "Echo", "github.com/gofiber/fiber": "Fiber",
    "github.com/go-chi/chi": "chi", "github.com/gorilla/mux": "gorilla/mux", "google.golang.org/grpc": "gRPC",
    "github.com/spf13/cobra": "Cobra",
}  # fmt: skip


def _detect_go(root: Path, det: Detection) -> None:
    path = root / "go.mod"
    if not path.is_file():
        return
    text = _read_text(path, det) or ""
    det.add("languages", "Go")
    det.add("package_managers", "go modules")
    det.add("frameworks", *(label for mod, label in _GO_FRAMEWORKS.items() if mod in text))
    golangci = any((root / f".golangci.{ext}").exists() for ext in ("yml", "yaml", "toml", "json"))
    det.source(
        "go.mod",
        {
            "install": "go mod download",
            "build": "go build ./...",
            "test": "go test ./...",
            "test_target": "go test {target}",
            "lint": "golangci-lint run" if golangci else "go vet ./...",
            "lint_fix": "golangci-lint run --fix" if golangci else None,
            "format": "gofmt -w .",
            "format_check": 'test -z "$(gofmt -l . | tee /dev/stderr)"',
            "run": "go run .",
        },
    )


_RUST_FRAMEWORKS = {
    "actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket", "warp": "warp", "tokio": "Tokio",
    "bevy": "Bevy", "tauri": "Tauri", "clap": "clap", "serde": "Serde",
}  # fmt: skip


def _detect_rust(root: Path, det: Detection) -> None:
    path = root / "Cargo.toml"
    if not path.is_file():
        return
    det.add("languages", "Rust")
    det.add("package_managers", "cargo")
    text = _read_text(path, det)
    if text is not None:
        try:
            cargo = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            det.warnings.append(f"Cargo.toml is not valid TOML: {exc}")
            cargo = {}
        deps: set[str] = set()
        for key in ("dependencies", "dev-dependencies"):
            deps.update(cargo.get(key) or {})
            deps.update((cargo.get("workspace") or {}).get(key) or {})
        det.add("frameworks", *(label for name, label in _RUST_FRAMEWORKS.items() if name in deps))
    det.source(
        "Cargo.toml",
        {
            "install": "cargo fetch",
            "build": "cargo build",
            "test": "cargo test",
            "test_target": "cargo test {target}",
            "lint": "cargo clippy --all-targets",
            "lint_fix": "cargo clippy --all-targets --fix --allow-dirty",
            "format": "cargo fmt",
            "format_check": "cargo fmt --check",
            "run": "cargo run",
        },
    )


def _detect_maven(root: Path, det: Detection) -> None:
    path = root / "pom.xml"
    if not path.is_file():
        return
    text = _read_text(path, det) or ""
    det.add("languages", "Kotlin" if "kotlin-maven-plugin" in text else "Java")
    det.add("package_managers", "maven")
    mvn = "./mvnw -B" if (root / "mvnw").is_file() else "mvn -B"
    spring = "spring-boot" in text
    if spring:
        det.add("frameworks", "Spring Boot")
    for marker, label in (("quarkus", "Quarkus"), ("micronaut", "Micronaut"), ("junit-jupiter", "JUnit 5")):
        if marker in text:
            det.add("frameworks", label)
    spotless = "spotless-maven-plugin" in text
    det.source(
        "pom.xml",
        {
            "install": f"{mvn} -DskipTests install",
            "build": f"{mvn} -DskipTests package",
            "test": f"{mvn} test",
            "test_target": f"{mvn} test -Dtest={{target}}",
            "lint": f"{mvn} checkstyle:check" if "maven-checkstyle-plugin" in text else None,
            "format": f"{mvn} spotless:apply" if spotless else None,
            "format_check": f"{mvn} spotless:check" if spotless else None,
            "run": f"{mvn} spring-boot:run" if spring else None,
        },
    )


def _detect_gradle(root: Path, det: Detection) -> None:
    path = _first_existing(root, "build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle")
    if path is None:
        return
    text = _read_text(path, det) or ""
    kotlin = "kotlin" in text
    det.add("languages", "Kotlin" if kotlin else "Java")
    det.add("package_managers", "gradle")
    gradle = ("./gradlew" if (root / "gradlew").is_file() else "gradle") + " --console=plain"
    spring = "org.springframework.boot" in text
    if spring:
        det.add("frameworks", "Spring Boot")
    if "com.android" in text:
        det.add("frameworks", "Android")
    commands: dict[str, str | None] = {
        "install": f"{gradle} dependencies",
        "build": f"{gradle} build -x test",
        "test": f"{gradle} test",
        "test_target": f"{gradle} test --tests {{target}}",
        "run": f"{gradle} bootRun" if spring else (f"{gradle} run" if "application" in text else None),
    }
    if "spotless" in text:
        commands.update(lint=f"{gradle} spotlessCheck", format=f"{gradle} spotlessApply")
        commands["format_check"] = f"{gradle} spotlessCheck"
    elif "ktlint" in text:
        commands.update(lint=f"{gradle} ktlintCheck", lint_fix=f"{gradle} ktlintFormat")
        commands.update(format=f"{gradle} ktlintFormat", format_check=f"{gradle} ktlintCheck")
    elif "detekt" in text:
        commands["lint"] = f"{gradle} detekt"
    elif "checkstyle" in text:
        commands["lint"] = f"{gradle} checkstyleMain"
    det.source(path.name, commands)


def _detect_dotnet(root: Path, det: Detection) -> None:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_file() and p.suffix in _DOTNET_SUFFIXES)
    except OSError:
        return
    if not entries:
        return
    det.add("package_managers", "nuget")
    languages = {".csproj": "C#", ".fsproj": "F#", ".vbproj": "Visual Basic"}
    projects = [p for p in entries if p.suffix != ".sln"]
    for proj in projects:
        det.add("languages", languages[proj.suffix])
        text = _read_text(proj, det) or ""
        if "Microsoft.NET.Sdk.Web" in text:
            det.add("frameworks", "ASP.NET Core")
        for marker, label in (("xunit", "xUnit"), ("NUnit", "NUnit"), ("MSTest", "MSTest")):
            if marker in text:
                det.add("frameworks", label)
    if not projects:
        det.add("languages", "C#")
    solutions = [p for p in entries if p.suffix == ".sln"]
    # dotnet refuses to guess when a folder has several project or solution files.
    candidates = solutions or projects
    arg = "" if len(candidates) == 1 else f" {shlex.quote(candidates[0].name)}"
    for extra in entries:
        det.add("manifests", extra.name)
    det.source(
        entries[0].name,
        {
            "install": f"dotnet restore{arg}",
            "build": f"dotnet build{arg}",
            "test": f"dotnet test{arg}",
            "test_target": f"dotnet test{arg} --filter {{target}}",
            "format": f"dotnet format{arg}",
            "format_check": f"dotnet format{arg} --verify-no-changes",
            "lint": f"dotnet format{arg} --verify-no-changes",
            "run": "dotnet run" if len(projects) == 1 else None,
        },
    )


def _detect_composer(root: Path, det: Detection) -> None:
    path = root / "composer.json"
    if not path.is_file():
        return
    det.add("languages", "PHP")
    det.add("package_managers", "composer")
    data: dict[str, Any] = {}
    text = _read_text(path, det)
    if text is not None:
        try:
            loaded = json.loads(text)
            data = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            det.warnings.append(f"composer.json is not valid JSON: {exc}")
    deps: set[str] = set()
    for key in ("require", "require-dev"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    raw_scripts = data.get("scripts")
    scripts: dict[str, Any] = raw_scripts if isinstance(raw_scripts, dict) else {}
    laravel = "laravel/framework" in deps
    if laravel:
        det.add("frameworks", "Laravel")
    if "symfony/framework-bundle" in deps:
        det.add("frameworks", "Symfony")
    commands: dict[str, str | None] = {"install": "composer install"}
    if "test" in scripts:
        commands["test"] = "composer test"
        commands["test_target"] = "composer test -- {target}"
    elif "pestphp/pest" in deps:
        commands.update(test="vendor/bin/pest", test_target="vendor/bin/pest {target}")
    elif "phpunit/phpunit" in deps:
        commands.update(test="vendor/bin/phpunit", test_target="vendor/bin/phpunit {target}")
    if "phpstan/phpstan" in deps or "larastan/larastan" in deps:
        commands["lint"] = "vendor/bin/phpstan analyse"
    if "laravel/pint" in deps:
        commands.update(format="vendor/bin/pint", format_check="vendor/bin/pint --test")
    elif "friendsofphp/php-cs-fixer" in deps:
        commands["format"] = "vendor/bin/php-cs-fixer fix"
        commands["format_check"] = "vendor/bin/php-cs-fixer fix --dry-run --diff"
    if laravel:
        commands["run"] = "php artisan serve"
    det.source("composer.json", commands)


# [ \t] rather than \s: with MULTILINE, \s* would run across blank lines from every line start.
_GEM = re.compile(r"""^[ \t]*gem[ \t]+['"]([^'"]+)['"]""", re.MULTILINE)


def _detect_ruby(root: Path, det: Detection) -> None:
    path = root / "Gemfile"
    if not path.is_file():
        return
    det.add("languages", "Ruby")
    det.add("package_managers", "bundler")
    gems = set(_GEM.findall(_read_text(path, det) or ""))
    rails = "rails" in gems
    if rails:
        det.add("frameworks", "Rails")
    if "sinatra" in gems:
        det.add("frameworks", "Sinatra")
    commands: dict[str, str | None] = {"install": "bundle install"}
    if gems & {"rspec", "rspec-rails", "rspec-core"}:
        commands.update(test="bundle exec rspec", test_target="bundle exec rspec {target}")
    elif rails:
        commands.update(test="bin/rails test", test_target="bin/rails test {target}")
    elif (root / "Rakefile").exists():
        commands["test"] = "bundle exec rake test"
    if gems & {"rubocop", "rubocop-rails"}:
        commands.update(lint="bundle exec rubocop", lint_fix="bundle exec rubocop -a")
    elif "standard" in gems:
        commands.update(lint="bundle exec standardrb", lint_fix="bundle exec standardrb --fix")
    if rails:
        commands["run"] = "bin/rails server"
    det.source("Gemfile", commands)


def _detect_compose(root: Path, det: Detection) -> None:
    for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        path = root / name
        if not path.is_file():
            continue
        det.add("manifests", name)
        text = _read_text(path, det)
        try:
            data = yaml.safe_load(text or "") or {}
        except yaml.YAMLError as exc:
            det.warnings.append(f"{name} is not valid YAML: {exc}")
            continue
        services = data.get("services") if isinstance(data, dict) else None
        if isinstance(services, dict):
            det.add("compose_services", *(str(s) for s in services))
    if (root / "Dockerfile").is_file():
        det.add("manifests", "Dockerfile")


# Task runners first: when a project has `make test`, that is what its authors run.
_DETECTORS: tuple[Callable[[Path, Detection], None], ...] = (
    _detect_make,
    _detect_just,
    _detect_node,
    _detect_python,
    _detect_go,
    _detect_rust,
    _detect_maven,
    _detect_gradle,
    _detect_dotnet,
    _detect_composer,
    _detect_ruby,
    _detect_compose,
)


def _scan(root: Path, det: Detection, workspace: Workspace) -> None:
    """Count source files per language and list nested directories that carry manifests."""
    counts: dict[str, int] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        if depth >= _MAX_SCAN_DEPTH:
            dirnames[:] = []
        if 0 < depth <= 2:
            found = sorted(f for f in filenames if f in _MANIFEST_NAMES or f.endswith(_DOTNET_SUFFIXES))
            if found:
                det.subprojects.append({"path": workspace.rel(here), "manifests": found})
        for name in filenames:
            seen += 1
            language = _EXTENSIONS.get(os.path.splitext(name)[1].lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
        if seen >= _MAX_SCAN_FILES:
            det.warnings.append(f"language scan stopped after {_MAX_SCAN_FILES} files")
            break
    manifest_langs = det.languages
    ranked = sorted(counts, key=lambda lang: -counts[lang])
    det.languages = ranked + [lang for lang in manifest_langs if lang not in counts]


def detect_project(root: Path, workspace: Workspace) -> Detection:
    """Inspect `root` (a directory already resolved through `workspace`) without running anything."""
    det = Detection(confine=workspace.root if workspace.confined else None)
    for detector in _DETECTORS:
        try:
            detector(root, det)
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            # Valid TOML/JSON/YAML can still hold the wrong types (`project = "x"`) or nest
            # too deeply to parse; one odd manifest should not hide the others.
            name = detector.__name__.removeprefix("_detect_")
            det.warnings.append(f"{name} manifest has an unexpected structure; skipped ({type(exc).__name__})")
    _scan(root, det, workspace)
    return det
