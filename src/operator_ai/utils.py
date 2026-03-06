from __future__ import annotations


def truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s
