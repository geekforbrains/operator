from __future__ import annotations

from pathlib import Path

from operator_ai.config import OPERATOR_DIR, Config, ConfigError, load_config
from operator_ai.memory import MemoryIndex, MemoryStore
from operator_ai.store import Store, get_store


def load_cli_config() -> Config | None:
    try:
        return load_config()
    except ConfigError:
        return None


def cli_base_dir(config: Config | None = None) -> Path:
    return (config.base_dir if config is not None else OPERATOR_DIR).resolve()


def cli_store(config: Config | None = None) -> Store:
    base_dir = cli_base_dir(config)
    return get_store(base_dir / "db" / "operator.db")


def cli_memory_store(config: Config | None = None) -> MemoryStore:
    base_dir = cli_base_dir(config)
    index_db = base_dir / "db" / "memory_index.db"
    index = MemoryIndex(index_db) if index_db.exists() else None
    return MemoryStore(base_dir=base_dir, index=index)
