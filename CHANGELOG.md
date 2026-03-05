# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-03-05

### Added

- Agent frontmatter — `AGENT.md` files support optional YAML frontmatter with `name` and `description` for inter-agent discovery
- Cross-agent spawning — `spawn_agent` accepts an optional `agent` parameter to delegate tasks to a different agent with its own prompt, models, workspace, sandbox, and permissions
- Spawn logging — every `spawn_agent` call is logged with parent agent, target agent, and nesting depth
- `OPERATOR_HOME` env var exposed in all shell commands, skill scripts, and job hooks
- `job-creator` bundled skill for teaching agents to create and manage scheduled jobs
- Prompts extracted to `.md` files under `prompts/` and loaded via `load_prompt()`

### Changed

- `AGENT.md` frontmatter is stripped before prompt injection — the LLM only sees the markdown body
- `operator agents` CLI now shows a Description column from agent frontmatter
- Sub-agent sandbox and permissions inheritance respects the target agent's config when spawning a different agent
- System prompt trimmed and reworded for clarity

### Fixed

- `!stop` now cancels the active task immediately instead of waiting for the next iteration
- Incomplete tool-call history trimmed on cancel and load to prevent malformed conversation state
- Stop flag cleared after cancelled run so subsequent requests aren't incorrectly cancelled

## [0.4.0] - 2026-03-05

### Added

- Users, roles, and auth — every inbound message is authenticated against a user database with role-based agent access control
- Agent permissions — flat allow-lists for tools and skills per agent
- Skill access tools (`read_skill`, `run_skill`) for agents without shell access
- Agent sandbox — `sandbox` config flag confines file tools to the workspace (default) or grants full filesystem access
- File/media support — Slack users can upload images, PDFs, and documents; images are sent as vision input, other files saved to `workspace/uploads/`
- `send_file` tool for uploading workspace files back to chat
- `operator job run` CLI command with dedicated CLI transport and logging
- `manage_users` admin-only tool for runtime user management

### Fixed

- `send_message` and `send_file` default to the current conversation's channel and thread
- Hook scripts respect shebang interpreter via `exec`
- Skill script paths expanded correctly in `argv[0]` for `run_skill`
- `manage_skill list` respects agent skill permissions
- Login shell PATH resolved correctly for `run_skill`

## [0.3.0] - 2026-03-04

### Added

- Agent permissions system with allow/deny lists for tools and skills
- Shared directory (`~/.operator/shared/`) symlinked into all agent workspaces
- Bundled skills shipped with the package (installed on `operator init`)
- `manage_skill` tool for agents to create, update, and delete skills at runtime
- `operator skills reset` CLI command to restore bundled skills to their original version
- Transport-scoped read tools: `read_channel` and `read_thread` for the Slack transport
- Configurable timezone via `defaults.timezone` (IANA format, defaults to UTC)

### Fixed

- Default agent renamed from `hermy`/`default` to `operator`

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
