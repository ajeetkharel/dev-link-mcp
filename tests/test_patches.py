import pytest
from mcp.server.mcpserver.exceptions import ToolError

from dev_link_mcp.tools.patches import (
    Hunk,
    apply_hunks,
    default_strip,
    detect_format,
    has_unnumbered_hunks,
    parse_openai_patch,
    parse_search_replace,
    parse_unnumbered_diff,
    unwrap,
)

# A patch ChatGPT sent that the unified-diff-only apply_patch rejected.
CHATGPT_PATCH = (
    "*** Begin Patch\n*** Update File: frontend/src/features/harness/harness-view.tsx\n@@\n"
    "       {page && attempts.length === 0 && (\n"
    "         <EmptyState>\n"
    '           <BrainCircuit size={28} aria-hidden="true" />\n'
    "-          No model attempts have been recorded for this run.\n"
    "+          {agentName\n"
    "+            ? `No model attempts have been recorded for ${agentName} or its assignment attempts.`\n"
    '+            : "No model attempts have been recorded for this run."}\n'
    "@@\n"
    "                         <tr\n"
    "                           key={attempt.id}\n"
    "*** End Patch"
)


def test_chatgpt_patch_parses_and_applies():
    (change,) = parse_openai_patch(CHATGPT_PATCH)
    assert change.action == "update" and change.path == "frontend/src/features/harness/harness-view.tsx"
    source = (
        "      {page && attempts.length === 0 && (\n"
        "        <EmptyState>\n"
        '          <BrainCircuit size={28} aria-hidden="true" />\n'
        "          No model attempts have been recorded for this run.\n"
        "        </EmptyState>\n"
        "                        <tr\n"
        "                          key={attempt.id}\n"
    )
    after = apply_hunks(source, change.hunks, "view.tsx")
    assert "No model attempts have been recorded for this run.\n        </EmptyState>" not in after
    assert "          {agentName\n" in after


def test_unwrap_strips_fence_and_heredoc():
    assert unwrap("```diff\n--- a\n+++ b\n```\n") == "--- a\n+++ b\n"
    assert unwrap("<<'EOF'\n*** Begin Patch\n*** End Patch\nEOF\n") == "*** Begin Patch\n*** End Patch\n"
    assert unwrap("--- a\n+++ b\n") == "--- a\n+++ b\n"


def test_default_strip():
    assert default_strip("diff --git a/x b/x\n") == 1
    assert default_strip("--- a/x\n+++ b/x\n@@ -1 +1 @@\n") == 1
    assert default_strip("--- x\n+++ x\n@@ -1 +1 @@\n") == 0
    assert default_strip("--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n") == 1


def test_has_unnumbered_hunks():
    assert not has_unnumbered_hunks("@@ -1,2 +1,3 @@ def f():\n")
    assert not has_unnumbered_hunks("@@ -1 +1 @@\n")
    assert has_unnumbered_hunks("@@ ... @@\n")
    assert has_unnumbered_hunks("@@\n")


def test_openai_patch_rejects_bad_lines():
    with pytest.raises(ToolError, match="not a change line"):
        parse_openai_patch("*** Begin Patch\n*** Update File: a\n@@\nbare\n*** End Patch\n")
    with pytest.raises(ToolError, match="must start with '\\+'"):
        parse_openai_patch("*** Begin Patch\n*** Add File: a\nbare\n*** End Patch\n")
    with pytest.raises(ToolError, match="has no changes"):
        parse_openai_patch("*** Begin Patch\n*** Update File: a\n*** End Patch\n")


def test_openai_patch_end_of_file_marker():
    (change,) = parse_openai_patch("*** Begin Patch\n*** Update File: a\n@@\n x\n+y\n*** End of File\n*** End Patch\n")
    assert change.hunks[0].at_end
    assert apply_hunks("x\nmid\nx\n", change.hunks, "a") == "x\nmid\nx\ny\n"


def test_search_replace_needs_a_path():
    with pytest.raises(ToolError, match="no file path"):
        parse_search_replace("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n")
    with pytest.raises(ToolError, match="no file path"):
        parse_search_replace("Here is the change:\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n")


def test_search_replace_unbalanced_markers():
    with pytest.raises(ToolError, match="no '======='"):
        parse_search_replace("a.py\n<<<<<<< SEARCH\na\n")
    with pytest.raises(ToolError, match="REPLACE"):
        parse_search_replace("a.py\n<<<<<<< SEARCH\na\n=======\nb\n")


def test_search_replace_unescapes_roo_markers():
    (change,) = parse_search_replace("a.md\n<<<<<<< SEARCH\n\\=======\n=======\n---\n>>>>>>> REPLACE\n")
    assert change.hunks[0].old == ["======="] and change.hunks[0].new == ["---"]


def test_unnumbered_diff_add_delete_rename():
    changes = parse_unnumbered_diff(
        "--- /dev/null\n+++ new.txt\n@@ @@\n+hi\n"
        "--- old.txt\n+++ /dev/null\n@@ @@\n-bye\n"
        "--- a.txt\n+++ b.txt\n@@ @@\n-x\n+y\n",
        0,
    )
    assert [(c.action, c.path, c.move_to) for c in changes] == [
        ("add", "new.txt", None),
        ("delete", "old.txt", None),
        ("update", "a.txt", "b.txt"),
    ]
    assert changes[0].content == "hi\n"


def test_apply_hunks_keeps_missing_final_newline():
    assert apply_hunks("a\nb", [Hunk(old=["b"], new=["c"])], "f") == "a\nc"


def test_apply_hunks_keeps_file_text_for_loosely_matched_context():
    content = "    say(“hi”)\nold\n"
    hunk = Hunk(old=['say("hi")', "old"], new=['say("hi")', "new"])
    assert apply_hunks(content, [hunk], "f") == "    say(“hi”)\nnew\n"


def test_apply_hunks_in_order_after_previous_hunk():
    content = "x\nmid\nx\n"
    hunks = [Hunk(old=["mid"], new=["MID"]), Hunk(old=["x"], new=["X"])]
    assert apply_hunks(content, hunks, "f") == "x\nMID\nX\n"


def test_apply_hunks_drops_trailing_blank_context_when_missing():
    assert apply_hunks("a\nb\n", [Hunk(old=["b", ""], new=["c", ""])], "f") == "a\nc\n"


def test_apply_hunks_reports_missing_lines_and_anchor():
    with pytest.raises(ToolError, match="not found"):
        apply_hunks("a\n", [Hunk(old=["zzz"], new=["y"])], "f")
    with pytest.raises(ToolError, match="context line not found: 'class Nope'"):
        apply_hunks("a\n", [Hunk(old=["a"], new=["b"], anchors=["class Nope"])], "f")


def test_apply_hunks_empty_old_appends():
    assert apply_hunks("", [Hunk(new=["first"])], "f") == "first\n"
    assert apply_hunks("a\n", [Hunk(new=["b"])], "f") == "a\nb\n"


def test_detect_format_uses_the_first_marker():
    diff_of_patch_file = (
        "--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1,4 +1,4 @@\n *** Begin Patch\n *** Update File: app.py\n"
        "-    old\n+    new\n *** End Patch\n"
    )
    assert detect_format(diff_of_patch_file) == "unified"
    assert detect_format("--- a/m.txt\n+++ b/m.txt\n@@ -1 +1 @@\n <<<<<<< SEARCH\n-a\n+b\n") == "unified"
    block_with_patch_text = "a.md\n<<<<<<< SEARCH\nx\n=======\n*** Begin Patch\n>>>>>>> REPLACE\n"
    assert detect_format(block_with_patch_text) == "search_replace"
    assert detect_format("just text\n") is None


def test_openai_markers_must_start_the_line():
    changes = parse_openai_patch(
        "*** Begin Patch\n*** Update File: d.md\n@@\n-x\n+y\n *** End Patch\n *** Update File: e.md\n"
        " *** End of File\n*** Update File: e.md\n@@\n-a\n+b\n*** End Patch\n"
    )
    assert [c.path for c in changes] == ["d.md", "e.md"]
    assert changes[0].hunks[0].old == ["x", "*** End Patch", "*** Update File: e.md", "*** End of File"]
    assert not changes[0].hunks[0].at_end


def test_default_strip_ignores_removed_comment_lines():
    patch = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- old comment\n+-- new comment\n select 1;\n"
    assert default_strip(patch) == 1


def test_unwrap_keeps_a_fence_that_names_the_file():
    patch = "```app.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n```\n"
    (change,) = parse_search_replace(unwrap(patch))
    assert change.path == "app.py"


def test_search_replace_prose_line_reuses_previous_path():
    patch = (
        "app.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
        "\nThen:\n<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE\n"
        "Update src/x.py as well:\n<<<<<<< SEARCH\ne\n=======\nf\n>>>>>>> REPLACE\n"
    )
    (change,) = parse_search_replace(patch)
    assert change.path == "app.py" and len(change.hunks) == 3


def test_unnumbered_diff_rejects_a_line_without_prefix():
    with pytest.raises(ToolError, match="'b' is not a diff line"):
        parse_unnumbered_diff("--- a/f\n+++ b/f\n@@\n-a\nb\n+A\n", 1)


def test_unnumbered_diff_refuses_git_only_sections():
    with pytest.raises(ToolError, match="needs git"):
        parse_unnumbered_diff("diff --git a/r b/s\nrename from r\nrename to s\n--- a/g\n+++ b/g\n@@\n-x\n+y\n", 1)


def test_unnumbered_diff_uses_line_numbers_as_hint():
    (f, _) = parse_unnumbered_diff("--- a/f\n+++ b/f\n@@ -4,1 +4,1 @@\n-x\n+X\n--- a/g\n+++ b/g\n@@\n-y\n+Y\n", 1)
    assert apply_hunks("x\ny\nz\nx\n", f.hunks, "f") == "x\ny\nz\nX\n"


def test_openai_pure_add_with_trailing_blank_line_appends():
    (change,) = parse_openai_patch("*** Begin Patch\n*** Update File: f\n@@\n+new\n\n*** End Patch\n")
    assert apply_hunks("a\n\nb\n", change.hunks, "f") == "a\n\nb\nnew\n"
    assert apply_hunks("a\nb\n", change.hunks, "f") == "a\nb\nnew\n"


def test_apply_hunks_keeps_mixed_line_endings():
    assert apply_hunks("a\r\nb\nc\n", [Hunk(old=["c"], new=["C"])], "f") == "a\r\nb\nC\r\n"
    assert apply_hunks("a\r\nb\n", [Hunk(old=["a"], new=["A", "A2"])], "f") == "A\r\nA2\r\nb\n"


def test_apply_hunks_appending_to_file_without_final_newline():
    assert apply_hunks("a", [Hunk(new=["b"])], "f") == "a\nb"


def test_empty_search_on_existing_file_is_refused():
    with pytest.raises(ToolError, match="SEARCH part is empty"):
        apply_hunks("a\n", [Hunk(new=["b"], unique=True)], "f")
    assert apply_hunks("", [Hunk(new=["b"], unique=True)], "f") == "b\n"


def test_openai_move_to_needs_a_path():
    with pytest.raises(ToolError, match="has no path"):
        parse_openai_patch("*** Begin Patch\n*** Update File: a\n*** Move to:\n@@\n-x\n+y\n*** End Patch\n")
