"""YAML frontmatter utilities for markdown files.

Used by skills, jobs, and agents to parse and manipulate files with
``---``-delimited YAML frontmatter.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("operator.frontmatter")


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse YAML frontmatter between --- delimiters."""
    split = _split_frontmatter(text)
    if split is None:
        return None
    frontmatter_text, _ = split
    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        logger.warning("Malformed YAML frontmatter: %s", e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("YAML frontmatter is not a mapping (got %s)", type(parsed).__name__)
        return None
    return parsed


def extract_body(text: str) -> str:
    """Return the markdown body after the --- frontmatter block."""
    split = _split_frontmatter(text)
    if split is None:
        return text.strip()
    _, body = split
    return body.strip()


def rewrite_frontmatter(path: Path, updates: dict) -> bool:
    """Update specific fields in a file's YAML frontmatter, preserving the body.

    Returns True on success, False if frontmatter couldn't be parsed.
    """
    text = path.read_text()
    fm = parse_frontmatter(text)
    if not fm:
        return False
    fm.update(updates)
    body = extract_body(text)
    new_fm = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    path.write_text(f"---\n{new_fm}\n---\n\n{body}\n")
    return True


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split markdown into (frontmatter, body) when fenced by top-level --- lines."""
    if not text:
        return None

    # Allow UTF-8 BOM at file start.
    normalized = text.lstrip("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            frontmatter = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            return frontmatter, body
    return None
