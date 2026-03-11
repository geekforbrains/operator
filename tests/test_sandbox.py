"""Tests for file path resolution (no sandboxing — paths are unrestricted)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import operator_ai.tools  # noqa: F401
from operator_ai.tools import workspace
from operator_ai.tools.files import _resolve, list_files, read_file, write_file

# --- _resolve ---


def test_resolve_relative(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    (tmp_path / "foo.txt").write_text("hi")
    assert _resolve("foo.txt") == (tmp_path / "foo.txt").resolve()


def test_resolve_allows_escape(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    result = _resolve("../../etc/passwd")
    assert result == (tmp_path / "../../etc/passwd").resolve()


def test_resolve_allows_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    assert _resolve("/etc/hosts") == Path("/etc/hosts").resolve()


# --- read_file ---


def test_read_file_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    target = tmp_path.parent / "outside.txt"
    target.write_text("outside content")
    try:
        result = asyncio.run(read_file(str(target)))
        assert "outside content" in result
    finally:
        target.unlink(missing_ok=True)


def test_read_file_relative(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    (tmp_path / "hello.txt").write_text("hello content")
    result = asyncio.run(read_file("hello.txt"))
    assert "hello content" in result


# --- write_file ---


def test_write_file_absolute(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    target = tmp_path.parent / "outside_write.txt"
    try:
        result = asyncio.run(write_file(str(target), "written outside"))
        assert "Wrote" in result
        assert target.read_text() == "written outside"
    finally:
        target.unlink(missing_ok=True)


def test_write_file_relative(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    result = asyncio.run(write_file("output.txt", "some content"))
    assert "Wrote" in result
    assert (tmp_path / "output.txt").read_text() == "some content"


# --- list_files ---


def test_list_files_follows_outside_dirs(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "file.txt").write_text("hi")

    result = asyncio.run(list_files(str(tmp_path.parent)))
    assert "outside_dir" in result
    # cleanup
    (outside / "file.txt").unlink(missing_ok=True)
    outside.rmdir()


def test_list_files_workspace(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "file.txt").write_text("hi")
    result = asyncio.run(list_files("."))
    assert "inner" in result
