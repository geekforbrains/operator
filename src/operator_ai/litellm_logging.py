from __future__ import annotations

import logging
import os

import litellm

LITELLM_LOGGER_NAMES = (
    "LiteLLM",
    "LiteLLM Router",
    "LiteLLM Proxy",
)


def _resolve_litellm_level() -> int:
    raw_level = os.environ.get("LITELLM_LOG", "").strip()
    if not raw_level:
        return logging.WARNING

    resolved = getattr(logging, raw_level.upper(), None)
    return resolved if isinstance(resolved, int) else logging.WARNING


def configure_litellm_logging(*, operator_logger_name: str = "operator") -> int:
    """Route LiteLLM logs through Operator handlers and suppress banner prints."""
    level = _resolve_litellm_level()
    litellm.suppress_debug_info = True

    if level <= logging.DEBUG:
        litellm._turn_on_debug()

    operator_logger = logging.getLogger(operator_logger_name)
    operator_handlers = list(operator_logger.handlers)

    for name in LITELLM_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.disabled = False
        logger.setLevel(level)
        if operator_handlers:
            logger.handlers.clear()
            for handler in operator_handlers:
                logger.addHandler(handler)
            logger.propagate = False

    return level
