"""Configuration management for Operator."""

from __future__ import annotations

import json
import logging
import os
import shutil

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.operator")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

DEFAULT_PROVIDERS = {
    "claude": {"path": "claude", "models": ["opus", "sonnet", "haiku"]},
    "codex": {"path": "codex", "models": ["gpt-5.3-codex"]},
    "gemini": {"path": "gemini", "models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
}


def load_config() -> dict:
    """Load config from ~/.operator/config.json, creating defaults if needed."""
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
            log.debug("Loaded config from %s", CONFIG_FILE)
            return config
        except Exception:
            log.exception("Failed to load config.json, using defaults")

    config = _build_default_config()
    save_config(config)
    return config


def save_config(config: dict) -> None:
    """Save config to ~/.operator/config.json."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    log.debug("Saved config to %s", CONFIG_FILE)


def _build_default_config() -> dict:
    """Build default config with all providers and their models."""
    return {
        "working_dir": os.getcwd(),
        "telegram": {
            "bot_token": "",
            "allowed_user_ids": [],
        },
        "providers": resolve_providers(),
    }


def resolve_providers() -> dict:
    """Build provider config with absolute paths where available."""
    providers = {}
    for name, defaults in DEFAULT_PROVIDERS.items():
        resolved = shutil.which(name)
        providers[name] = {**defaults, "path": resolved or name}
    return providers


def detect_providers() -> dict[str, bool]:
    """Check which provider CLIs are available on PATH."""
    return {name: shutil.which(name) is not None for name in DEFAULT_PROVIDERS}
