"""Tests for workspace sandbox enforcement on file tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import operator_ai.tools  # noqa: F401
from operator_ai.tools import workspace
from operator_ai.tools.files import _resolve, list_files, read_file, write_file

# ---------------------------------------------------------------------------
# _resolve — sandboxed (default)
# ---------------------------------------------------------------------------


def test_resolve_relative_in_sandbox(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    (tmp_path / "foo.txt").write_text("hi")
    assert _resolve("foo.txt") == (tmp_path / "foo.txt").resolve()


def test_resolve_subdirectory_in_sandbox(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    sub = tmp_path / "work" / "draft.txt"
    sub.parent.mkdir(parents=True)
    sub.write_text("draft")
    assert _resolve("work/draft.txt") == sub.resolve()


def test_resolve_blocks_escape_in_sandbox(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    with pytest.raises(ValueError, match="path outside workspace"):
        _resolve("../../etc/passwd")


def test_resolve_blocks_absolute_in_sandbox(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    with pytest.raises(ValueError, match="path outside workspace"):
        _resolve("/etc/hosts")


def test_resolve_blocks_home_expansion_in_sandbox(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    with pytest.raises(ValueError, match="path outside workspace"):
        _resolve("~/some_file.txt")


# ---------------------------------------------------------------------------
# _resolve — unsandboxed
# ---------------------------------------------------------------------------


def test_resolve_allows_escape_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    result = _resolve("../../etc/passwd")
    assert result == (tmp_path / "../../etc/passwd").resolve()


def test_resolve_allows_absolute_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    assert _resolve("/etc/hosts") == Path("/etc/hosts").resolve()


def test_resolve_relative_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    (tmp_path / "foo.txt").write_text("hi")
    assert _resolve("foo.txt") == (tmp_path / "foo.txt").resolve()


# ---------------------------------------------------------------------------
# read_file — sandboxed
# ---------------------------------------------------------------------------


def test_read_file_relative_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    (tmp_path / "hello.txt").write_text("hello content")
    result = asyncio.run(read_file("hello.txt"))
    assert "hello content" in result


def test_read_file_blocks_absolute_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    target = tmp_path.parent / "outside.txt"
    target.write_text("outside content")
    try:
        result = asyncio.run(read_file(str(target)))
        assert "[error:" in result
        assert "path outside workspace" in result
    finally:
        target.unlink(missing_ok=True)


def test_read_file_blocks_escape_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    result = asyncio.run(read_file("../../../etc/passwd"))
    assert "[error:" in result
    assert "path outside workspace" in result


# ---------------------------------------------------------------------------
# read_file — unsandboxed
# ---------------------------------------------------------------------------


def test_read_file_absolute_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    target = tmp_path.parent / "outside.txt"
    target.write_text("outside content")
    try:
        result = asyncio.run(read_file(str(target)))
        assert "outside content" in result
    finally:
        target.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# write_file — sandboxed
# ---------------------------------------------------------------------------


def test_write_file_relative_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    result = asyncio.run(write_file("output.txt", "some content"))
    assert "Wrote" in result
    assert (tmp_path / "output.txt").read_text() == "some content"


def test_write_file_blocks_absolute_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    target = tmp_path.parent / "outside_write.txt"
    result = asyncio.run(write_file(str(target), "should not write"))
    assert "[error:" in result
    assert "path outside workspace" in result
    assert not target.exists()


def test_write_file_blocks_escape_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    result = asyncio.run(write_file("../../escape.txt", "nope"))
    assert "[error:" in result
    assert "path outside workspace" in result


# ---------------------------------------------------------------------------
# write_file — unsandboxed
# ---------------------------------------------------------------------------


def test_write_file_absolute_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    target = tmp_path.parent / "outside_write.txt"
    try:
        result = asyncio.run(write_file(str(target), "written outside"))
        assert "Wrote" in result
        assert target.read_text() == "written outside"
    finally:
        target.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# list_files — sandboxed
# ---------------------------------------------------------------------------


def test_list_files_workspace_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "file.txt").write_text("hi")
    result = asyncio.run(list_files("."))
    assert "inner" in result


def test_list_files_blocks_outside_sandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=True)
    result = asyncio.run(list_files(str(tmp_path.parent)))
    assert "[error:" in result
    assert "path outside workspace" in result


# ---------------------------------------------------------------------------
# list_files — unsandboxed
# ---------------------------------------------------------------------------


def test_list_files_outside_unsandboxed(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "file.txt").write_text("hi")
    try:
        result = asyncio.run(list_files(str(tmp_path.parent)))
        assert "outside_dir" in result
    finally:
        (outside / "file.txt").unlink(missing_ok=True)
        outside.rmdir()


# ---------------------------------------------------------------------------
# sandbox defaults to True
# ---------------------------------------------------------------------------


def test_sandbox_defaults_to_true(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    assert workspace.is_sandboxed() is True


def test_sandbox_explicit_false(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandbox=False)
    assert workspace.is_sandboxed() is False
