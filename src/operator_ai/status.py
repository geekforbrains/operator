"""Canned status messages shown while the agent is processing."""

from __future__ import annotations

import random

STATUS_MESSAGES = [
    "Pressing buttons...",
    "Generalizing knowledge...",
    "Consulting the oracle...",
    "Rearranging neurons...",
    "Connecting the dots...",
    "Warming up the hamsters...",
    "Pondering existence...",
    "Shuffling bits...",
    "Reading the fine print...",
    "Calibrating intuition...",
    "Asking nicely...",
    "Brewing thoughts...",
    "Summoning inspiration...",
    "Crunching context...",
    "Feeding the model...",
    "Dusting off the thesaurus...",
    "Herding electrons...",
    "Reticulating splines...",
    "Consulting the manual...",
    "Counting backwards from infinity...",
    "Aligning the stars...",
    "Polishing the answer...",
    "Untangling threads...",
    "Negotiating with the GPU...",
    "Checking the vibe...",
]


def pick_status_messages(n: int = 10) -> list[str]:
    """Return *n* random status messages from the pool."""
    return random.sample(STATUS_MESSAGES, min(n, len(STATUS_MESSAGES)))
