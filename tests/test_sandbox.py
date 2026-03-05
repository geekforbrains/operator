from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import operator_ai.tools  # noqa: F401
from operator_ai.tools import workspace
from operator_ai.tools.files import _resolve, list_files, read_file, write_file

# --- _resolve ---


def test_resolve_sandboxed_relative(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    (tmp_path / "foo.txt").write_text("hi")
    assert _resolve("foo.txt") == (tmp_path / "foo.txt").resolve()


def test_resolve_sandboxed_rejects_escape(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    with pytest.raises(ValueError, match="escapes workspace"):
        _resolve("../../etc/passwd")


def test_resolve_sandboxed_rejects_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    with pytest.raises(ValueError, match="escapes workspace"):
        _resolve("/etc/passwd")


def test_resolve_unsandboxed_relative(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    (tmp_path / "foo.txt").write_text("hi")
    assert _resolve("foo.txt") == (tmp_path / "foo.txt").resolve()


def test_resolve_unsandboxed_allows_escape(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    result = _resolve("../../etc/passwd")
    assert result == (tmp_path / "../../etc/passwd").resolve()


def test_resolve_unsandboxed_allows_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    assert _resolve("/etc/hosts") == Path("/etc/hosts").resolve()


# --- read_file ---


def test_read_file_unsandboxed_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    target = tmp_path.parent / "outside.txt"
    target.write_text("outside content")
    try:
        result = asyncio.run(read_file(str(target)))
        assert "outside content" in result
    finally:
        target.unlink(missing_ok=True)


def test_read_file_sandboxed_rejects_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    result = asyncio.run(read_file("/etc/hosts"))
    assert "escapes workspace" in result


# --- write_file ---


def test_write_file_unsandboxed_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    target = tmp_path.parent / "outside_write.txt"
    try:
        result = asyncio.run(write_file(str(target), "written outside"))
        assert "Wrote" in result
        assert target.read_text() == "written outside"
    finally:
        target.unlink(missing_ok=True)


def test_write_file_sandboxed_rejects_escape(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    result = asyncio.run(write_file("../../evil.txt", "bad"))
    assert "escapes workspace" in result


# --- list_files ---


def test_list_files_unsandboxed_follows_outside_dirs(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=False)
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "file.txt").write_text("hi")

    result = asyncio.run(list_files(str(tmp_path.parent)))
    assert "outside_dir" in result
    # cleanup
    (outside / "file.txt").unlink(missing_ok=True)
    outside.rmdir()


def test_list_files_sandboxed_hides_outside_dirs(tmp_path: Path):
    workspace.set_workspace(tmp_path, sandboxed=True)
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "file.txt").write_text("hi")
    result = asyncio.run(list_files("."))
    assert "inner" in result
