# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.2] - 2026-03-04

### Fixed

- Prompt caching: split system prompt into stable prefix and dynamic suffix with Anthropic cache breakpoints so the prefix is reused across turns
- Add rolling cache breakpoint on conversation history (penultimate user message) for multi-turn savings
- Read OpenAI `cached_tokens` from `prompt_tokens_details` for unified cache reporting

### Added

- Per-run ID logging via ContextVar for tracing agent runs in logs
- Usage line now shows cache write tokens and prefixed with `Usage:`

## [0.2.1] - 2026-03-04

### Added

- Per-job model override via `model` field in JOB.md frontmatter

## [0.2.0] - 2026-03-04

### Added

- `operator init` command to scaffold `~/.operator/` with starter config and agent

### Fixed

- Use API token for PyPI publish workflow

## [0.1.0] - 2026-03-04

### Added

- Initial public beta release
- Pydantic-AI agent with LiteLLM provider support
- Telegram adapter with polling transport
- SQLite persistence with WAL mode
- Background task scheduler with check scripts
- Managed service lifecycle (start/stop/health/logs)
- 14 built-in tools (shell, file, web, memory, tasks, services)
- Turn-based context pruning for long conversations
- CLI: init, serve, backup, restore, skills
- Thinking level support (off/low/medium/high/max)
