from pathlib import Path

import pytest
from conftest import text

PY_SOURCE = '''import os

# helper comment
class Greeter(Base):
    """Says hello."""

    @property
    def name(self):
        return "x"

    async def greet(self, who):
        return f"hi {who}"


def make_greeter():
    def inner():
        pass
    return Greeter()
'''

TS_SOURCE = """interface Shape { area(): number }
type Id = string;
export class Circle implements Shape {
  area(): number { return 1; }
}
export function makeCircle(): Circle { return new Circle(); }
"""


def write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside"
    secret.mkdir()
    write(secret, "secret.py", "class Secret:\n    pass\n")
    return secret


# code_outline


async def test_outline_python_nests_methods_with_line_ranges(connect, workspace):
    write(workspace, "pkg/greet.py", PY_SOURCE)
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "pkg/greet.py"})
    assert not r.is_error, text(r)
    lines = text(r).splitlines()
    assert lines[0] == "pkg/greet.py (python, 18 lines)"
    assert "class Greeter  L4-12" in lines
    assert "  method name  L8-9" in lines
    assert "  method greet  L11-12" in lines
    assert "function make_greeter  L15-18" in lines
    assert "  function inner  L16-17" in lines


async def test_outline_typescript_includes_type_aliases(connect, workspace):
    write(workspace, "shapes.ts", TS_SOURCE)
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "shapes.ts"})
    assert not r.is_error, text(r)
    out = text(r)
    assert "interface Shape  L1" in out
    assert "type Id  L2" in out
    assert "class Circle  L3-5" in out
    assert "  method area  L4" in out
    assert "function makeCircle  L6" in out


async def test_outline_detects_language_from_shebang(connect, workspace):
    write(workspace, "bin/tool", "#!/usr/bin/env python3\ndef main():\n    pass\n")
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "bin/tool"})
    assert not r.is_error, text(r)
    assert "(python, 3 lines)" in text(r)
    assert "function main  L2-3" in text(r)


async def test_outline_unknown_language_returns_fallback_message(connect, workspace):
    write(workspace, "notes.txt", "def not_code(): pass\n")
    write(workspace, "data.zzz", "whatever\n")
    async with connect("code") as client:
        for name in ("notes.txt", "data.zzz"):
            r = await client.call_tool("code_outline", {"path": name})
            assert not r.is_error
            assert "language not recognized" in text(r)
            assert "read_file" in text(r)


async def test_outline_file_without_declarations(connect, workspace):
    write(workspace, "consts.py", "X = 1\n")
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "consts.py"})
    assert not r.is_error
    assert "(no declarations found)" in text(r)


async def test_outline_empty_file(connect, workspace):
    write(workspace, "empty.py", "")
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "empty.py"})
    assert not r.is_error
    assert "(python, 0 lines)" in text(r)


async def test_outline_errors(connect, workspace, rt):
    write(workspace, "img.py", b"\x89PNG\x00\x00binary")
    (workspace / "dir").mkdir()
    big = workspace / "big.py"
    big.write_text("x = 1\n" * (rt.config.limits.max_read_bytes // 6 + 10))
    async with connect("code") as client:
        r = await client.call_tool("code_outline", {"path": "img.py"})
        assert r.is_error and "binary" in text(r)
        r = await client.call_tool("code_outline", {"path": "dir"})
        assert r.is_error and "not a file" in text(r)
        r = await client.call_tool("code_outline", {"path": "missing.py"})
        assert r.is_error and "does not exist" in text(r)
        r = await client.call_tool("code_outline", {"path": "big.py"})
        assert r.is_error and "read limit" in text(r)


@pytest.mark.bwrap
async def test_outline_rejects_paths_outside_workspace(connect, workspace, outside):
    (workspace / "link.py").symlink_to(outside / "secret.py")
    (workspace / "linkdir").symlink_to(outside)
    async with connect("code") as client:
        for p in ("../outside/secret.py", str(outside / "secret.py"), "link.py", "linkdir/secret.py"):
            r = await client.call_tool("code_outline", {"path": p})
            assert r.is_error, p
            assert "outside the workspace" in text(r)


# find_symbol


async def test_find_symbol_confirms_definitions_only(connect, workspace):
    write(workspace, "pkg/greet.py", PY_SOURCE)
    write(workspace, "pkg/use.py", "from pkg.greet import Greeter\n\ng = Greeter()\n")
    write(workspace, "shapes.ts", TS_SOURCE)
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "Greeter"})
        assert not r.is_error, text(r)
        assert text(r).splitlines() == ["pkg/greet.py:4-12 class Greeter"]

        r = await client.call_tool("find_symbol", {"name": "area"})
        assert text(r).splitlines()[0] == "shapes.ts:4-4 method area (in Circle)"

        r = await client.call_tool("find_symbol", {"name": "greet"})
        assert "pkg/greet.py:11-12 method greet (in Greeter)" in text(r)


async def test_find_symbol_kind_filter(connect, workspace):
    write(workspace, "shapes.ts", TS_SOURCE)
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "area", "kind": "class"})
        assert not r.is_error
        assert "no definition of 'area'" in text(r)
        # "function" also matches methods.
        r = await client.call_tool("find_symbol", {"name": "area", "kind": "function"})
        assert "shapes.ts:4-4 method area" in text(r)
        r = await client.call_tool("find_symbol", {"name": "Id", "kind": "type"})
        assert "shapes.ts:2-2 type Id" in text(r)
        r = await client.call_tool("find_symbol", {"name": "area", "kind": "bogus"})
        assert r.is_error


async def test_find_symbol_respects_path_gitignore_and_vendor_dirs(connect, workspace):
    write(workspace, "a/mod.py", "def target():\n    pass\n")
    write(workspace, "b/mod.py", "def target():\n    pass\n")
    write(workspace, "node_modules/x/mod.py", "def target():\n    pass\n")
    write(workspace, "ignored/mod.py", "def target():\n    pass\n")
    write(workspace, ".gitignore", "ignored/\n")
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "target"})
        assert text(r).splitlines() == ["a/mod.py:1-2 function target", "b/mod.py:1-2 function target"]
        r = await client.call_tool("find_symbol", {"name": "target", "path": "b"})
        assert text(r).splitlines() == ["b/mod.py:1-2 function target"]
        r = await client.call_tool("find_symbol", {"name": "target", "path": "a/mod.py"})
        assert text(r).splitlines() == ["a/mod.py:1-2 function target"]
        # An explicit path inside a vendor dir is still searched.
        r = await client.call_tool("find_symbol", {"name": "target", "path": "node_modules/x"})
        assert text(r).splitlines() == ["node_modules/x/mod.py:1-2 function target"]


async def test_find_symbol_max_results_and_unparsed_note(connect, workspace):
    for i in range(3):
        write(workspace, f"m{i}.py", "def dup():\n    pass\n")
    write(workspace, "notes.txt", "dup is mentioned here\n")
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "dup", "max_results": 2})
    out = text(r).splitlines()
    assert out[:2] == ["m0.py:1-2 function dup", "m1.py:1-2 function dup"]
    assert "note: 1 more definitions not shown; raise max_results" in out
    assert any("could not be parsed" in line for line in out)


@pytest.mark.bwrap
async def test_find_symbol_validation_and_escapes(connect, workspace, outside):
    (workspace / "linkdir").symlink_to(outside)
    async with connect("code") as client:
        for bad in ("", "   ", "two words"):
            r = await client.call_tool("find_symbol", {"name": bad})
            assert r.is_error, bad
        r = await client.call_tool("find_symbol", {"name": "x", "max_results": 0})
        assert r.is_error
        r = await client.call_tool("find_symbol", {"name": "x", "path": "nope"})
        assert r.is_error and "does not exist" in text(r)
        for p in ("..", "../outside", str(outside), "linkdir"):
            r = await client.call_tool("find_symbol", {"name": "Secret", "path": p})
            assert r.is_error, p
            assert "outside the workspace" in text(r)


async def test_find_symbol_skips_symlinked_files_pointing_outside(connect, workspace, outside):
    (workspace / "link.py").symlink_to(outside / "secret.py")
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "Secret"})
    assert not r.is_error
    assert text(r) == "no definition of 'Secret' found under ."


async def test_find_symbol_name_starting_with_dash_is_not_a_flag(connect, workspace):
    write(workspace, "a.py", "x = 1\n")
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "--files"})
    assert not r.is_error
    assert "no definition" in text(r)


async def test_find_symbol_method_kind_covers_python_and_rust_methods(connect, workspace):
    write(workspace, "pkg/greet.py", PY_SOURCE)
    write(workspace, "lib.rs", "struct S {}\nimpl S {\n    fn greet(&self) {}\n}\nfn greet() {}\n")
    async with connect("code") as client:
        r = await client.call_tool("find_symbol", {"name": "greet", "kind": "method"})
        assert text(r).splitlines() == [
            "lib.rs:3-3 method greet (in S)",
            "pkg/greet.py:11-12 method greet (in Greeter)",
        ]
        # Nested functions inside functions stay functions.
        r = await client.call_tool("find_symbol", {"name": "inner", "kind": "method"})
        assert "no definition" in text(r)
        r = await client.call_tool("find_symbol", {"name": "greet", "kind": "function"})
        assert "lib.rs:5-5 function greet" in text(r)


async def test_name_with_nul_byte_is_rejected(connect):
    async with connect("code") as client:
        for tool in ("find_symbol", "find_references"):
            r = await client.call_tool(tool, {"name": "a\x00b"})
            assert r.is_error
            assert "NUL" in text(r)


# find_references


async def test_find_references_groups_by_file_with_word_boundaries(connect, workspace):
    write(workspace, "pkg/greet.py", PY_SOURCE)
    write(workspace, "pkg/use.py", "from pkg.greet import Greeter\n\ng = Greeter()\nGreeterFactory = 1\n")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "Greeter"})
    assert not r.is_error, text(r)
    lines = text(r).splitlines()
    assert lines[0] == "4 match(es) in 2 file(s)"
    assert lines[1:] == [
        "pkg/greet.py (2)",
        "  4: class Greeter(Base):",
        "  18: return Greeter()",
        "pkg/use.py (2)",
        "  1: from pkg.greet import Greeter",
        "  3: g = Greeter()",
    ]


async def test_find_references_ignore_case_path_and_limits(connect, workspace):
    write(workspace, "a/x.py", "Token = 1\ntoken = 2\nTOKEN = 3\n")
    write(workspace, "b/y.py", "token\n")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "token", "path": "a"})
        assert text(r).splitlines() == ["1 match(es) in 1 file(s)", "a/x.py (1)", "  2: token = 2"]
        r = await client.call_tool("find_references", {"name": "token", "ignore_case": True, "max_results": 2})
        out = text(r).splitlines()
        assert out[0] == "4 match(es) in 2 file(s)"
        assert out[1:4] == ["a/x.py (3)", "  1: Token = 1", "  2: token = 2"]
        assert out[-1] == "note: 2 more match(es) not shown; raise max_results or narrow path"


async def test_find_references_regex_chars_are_literal(connect, workspace):
    write(workspace, "a.js", "const $el = 1;\nconst xel = 2;\n")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "a.b"})
        assert "no references to 'a.b'" in text(r)


async def test_find_references_long_lines_are_cut(connect, workspace):
    write(workspace, "long.py", "needle = '" + "x" * 1000 + "'\n")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "needle"})
    assert not r.is_error
    assert len(text(r)) < 600


async def test_find_references_keeps_form_feed_and_line_separator_in_content(connect, workspace):
    write(workspace, "a.py", "y = needle\x0c(); z = needle\u2028+1\n")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "needle"})
    lines = text(r).splitlines()
    assert lines[0] == "1 match(es) in 1 file(s)"
    assert "z = needle" in text(r)


async def test_find_references_totals_count_beyond_the_output_cap(connect, workspace, rt):
    # Enough matching lines that the raw rg output exceeds the agent-facing output cap.
    line = "needle = '" + "x" * 90 + "'\n"
    count = rt.config.limits.max_output_bytes // len(line) + 500
    for i in range(4):
        write(workspace, f"f{i}.py", line * count)
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "needle", "max_results": 10})
    out = text(r).splitlines()
    assert out[0] == f"{4 * count} match(es) in 4 file(s)"
    assert out[-1] == f"note: {4 * count - 10} more match(es) not shown; raise max_results or narrow path"


@pytest.mark.bwrap
async def test_find_references_no_match_and_escapes(connect, workspace, outside):
    write(workspace, "a.py", "x = 1\n")
    (workspace / "linkdir").symlink_to(outside)
    (workspace / "link.py").symlink_to(outside / "secret.py")
    async with connect("code") as client:
        r = await client.call_tool("find_references", {"name": "nothing_here"})
        assert not r.is_error
        assert text(r) == "no references to 'nothing_here' under ."
        r = await client.call_tool("find_references", {"name": "Secret"})
        assert "secret.py" not in text(r)
        for p in ("../outside", str(outside), "linkdir", "link.py"):
            r = await client.call_tool("find_references", {"name": "Secret", "path": p})
            assert r.is_error, p
            assert "outside the workspace" in text(r)
        r = await client.call_tool("find_references", {"name": "a b"})
        assert r.is_error


# code_stats


async def test_code_stats_counts_lines_per_language(connect, workspace):
    write(workspace, "a.py", "# comment\nx = 1\n\ny = 2\n")
    write(workspace, "b.py", "z = 3\n")
    write(workspace, "c.js", "// c\nlet a = 1;\n")
    write(workspace, "node_modules/dep/index.js", "let b = 2;\n")
    write(workspace, "ignored/skip.py", "q = 1\n")
    write(workspace, ".gitignore", "ignored/\n")
    write(workspace, ".hidden/h.py", "h = 1\n")
    write(workspace, "img.png", b"\x89PNG\x00\x00")
    write(workspace, "readme.txt", "plain\n")
    async with connect("code") as client:
        r = await client.call_tool("code_stats", {})
    assert not r.is_error, text(r)
    data = r.structured_content
    langs = {entry["language"]: entry for entry in data["languages"]}
    assert set(langs) == {"python", "javascript"}
    assert langs["python"] == {"language": "python", "files": 2, "lines": 5, "code": 3, "comment": 1, "blank": 1}
    assert langs["javascript"] == {
        "language": "javascript",
        "files": 1,
        "lines": 2,
        "code": 1,
        "comment": 1,
        "blank": 0,
    }
    assert data["totals"] == {"files": 3, "lines": 7, "code": 4, "comment": 2, "blank": 1}
    assert data["skipped"]["binary"] == 1
    assert data["skipped"]["unrecognized"] == 1
    assert data["path"] == "."
    # Sorted by code lines, most first.
    assert [e["language"] for e in data["languages"]] == ["python", "javascript"]


async def test_code_stats_subdir_single_file_and_too_large(connect, workspace, rt):
    write(workspace, "src/a.py", "x = 1\n")
    write(workspace, "other/b.py", "y = 1\n")
    (workspace / "src/big.py").write_text("x = 1\n" * (rt.config.limits.max_read_bytes // 6 + 10))
    async with connect("code") as client:
        r = await client.call_tool("code_stats", {"path": "src"})
        data = r.structured_content
        assert data["path"] == "src"
        assert data["totals"]["files"] == 1
        assert data["skipped"]["too_large"] == 1
        r = await client.call_tool("code_stats", {"path": "other/b.py"})
        assert r.structured_content["totals"] == {"files": 1, "lines": 1, "code": 1, "comment": 0, "blank": 0}


async def test_code_stats_empty_dir(connect, workspace):
    (workspace / "empty").mkdir()
    async with connect("code") as client:
        r = await client.call_tool("code_stats", {"path": "empty"})
    assert not r.is_error
    assert r.structured_content["languages"] == []
    assert r.structured_content["totals"]["files"] == 0


@pytest.mark.bwrap
async def test_code_stats_rejects_escapes_and_skips_outside_symlinks(connect, workspace, outside):
    write(workspace, "a.py", "x = 1\n")
    (workspace / "link.py").symlink_to(outside / "secret.py")
    (workspace / "linkdir").symlink_to(outside)
    async with connect("code") as client:
        r = await client.call_tool("code_stats", {})
        assert r.structured_content["totals"]["files"] == 1
        for p in ("..", "../outside", str(outside), "linkdir", "link.py"):
            r = await client.call_tool("code_stats", {"path": p})
            assert r.is_error, p
            assert "outside the workspace" in text(r)
        r = await client.call_tool("code_stats", {"path": "missing"})
        assert r.is_error and "does not exist" in text(r)


async def test_tools_are_read_only(connect):
    async with connect("code") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"code_outline", "find_symbol", "find_references", "code_stats"}
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
