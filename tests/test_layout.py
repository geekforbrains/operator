from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from operator_ai.config import Config
from operator_ai.layout import ensure_layout, ensure_user_memory


def _make_config(*agent_names: str) -> Config:
    agents = {name: {} for name in agent_names}
    return Config(defaults={"models": ["test/m"]}, agents=agents)


# ── Tree creation ───────────────────────────────────────────────


def test_ensure_layout_creates_full_tree(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config("hermy", "cora")

    with (
        patch("operator_ai.layout.OPERATOR_DIR", op_dir),
        patch("operator_ai.config.OPERATOR_DIR", op_dir),
    ):
        ensure_layout(config)

    # Top-level dirs
    assert (op_dir / "jobs").is_dir()
    assert (op_dir / "skills").is_dir()
    assert (op_dir / "shared").is_dir()
    assert (op_dir / "db").is_dir()

    # Global memory
    for sub in ("rules", "notes", "trash"):
        assert (op_dir / "memory" / "global" / sub).is_dir()

    # User memory parent
    assert (op_dir / "memory" / "users").is_dir()

    # Per-agent trees
    for name in ("hermy", "cora"):
        agent = op_dir / "agents" / name

        # Workspace subdirs
        for ws_sub in ("inbox", "work", "artifacts", "tmp"):
            assert (agent / "workspace" / ws_sub).is_dir()

        # Shared symlink
        link = agent / "workspace" / "shared"
        assert link.is_symlink()
        assert link.resolve() == (op_dir / "shared").resolve()

        # Memory subdirs
        for mem_sub in ("rules", "notes", "trash"):
            assert (agent / "memory" / mem_sub).is_dir()

        # State dir
        assert (agent / "state").is_dir()


def test_ensure_layout_creates_per_agent_shared_dirs(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config("alpha", "beta")

    with (
        patch("operator_ai.layout.OPERATOR_DIR", op_dir),
        patch("operator_ai.config.OPERATOR_DIR", op_dir),
    ):
        ensure_layout(config)

    assert (op_dir / "shared" / "alpha").is_dir()
    assert (op_dir / "shared" / "beta").is_dir()


# ── Symlink correctness ────────────────────────────────────────


def test_shared_symlink_points_to_shared_root(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config("hermy")

    with (
        patch("operator_ai.layout.OPERATOR_DIR", op_dir),
        patch("operator_ai.config.OPERATOR_DIR", op_dir),
    ):
        ensure_layout(config)

    link = op_dir / "agents" / "hermy" / "workspace" / "shared"
    target = op_dir / "shared"
    assert link.is_symlink()
    assert link.resolve() == target.resolve()

    # Writing via the symlink should appear in the shared root
    (link / "hermy" / "test.txt").write_text("hello")
    assert (target / "hermy" / "test.txt").read_text() == "hello"


# ── Idempotency ─────────────────────────────────────────────────


def test_ensure_layout_idempotent(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config("hermy")

    with (
        patch("operator_ai.layout.OPERATOR_DIR", op_dir),
        patch("operator_ai.config.OPERATOR_DIR", op_dir),
    ):
        ensure_layout(config)
        # Drop a marker file to prove it isn't wiped
        marker = op_dir / "agents" / "hermy" / "workspace" / "work" / "marker.txt"
        marker.write_text("keep me")

        ensure_layout(config)  # second run — must not raise or clobber

    assert marker.read_text() == "keep me"
    assert (op_dir / "agents" / "hermy" / "workspace" / "shared").is_symlink()


# ── No agents ───────────────────────────────────────────────────


def test_ensure_layout_no_agents(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config()  # no agents

    with (
        patch("operator_ai.layout.OPERATOR_DIR", op_dir),
        patch("operator_ai.config.OPERATOR_DIR", op_dir),
    ):
        ensure_layout(config)

    assert (op_dir / "jobs").is_dir()
    assert (op_dir / "memory" / "global" / "rules").is_dir()
    # No agent subdirs
    assert not (op_dir / "agents").exists()


# ── User memory ─────────────────────────────────────────────────


def test_ensure_user_memory(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config()

    with patch("operator_ai.config.OPERATOR_DIR", op_dir):
        ensure_user_memory("gavin", config)

    user_dir = op_dir / "memory" / "users" / "gavin"
    for sub in ("rules", "notes", "trash"):
        assert (user_dir / sub).is_dir()


def test_ensure_user_memory_idempotent(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    config = _make_config()

    with patch("operator_ai.config.OPERATOR_DIR", op_dir):
        ensure_user_memory("gavin", config)
        ensure_user_memory("gavin", config)  # no error

    assert (op_dir / "memory" / "users" / "gavin" / "rules").is_dir()
