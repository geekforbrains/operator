# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.7.0] - 2026-03-09

### Added

- User message creation timestamps are now rendered into live model input for chat, jobs, and sub-agents using a local format like `[Monday, 2026-03-09T09:22:40-07:00]`
- Stored messages now carry a nullable `created_at` column so new conversations preserve message creation time without rewriting legacy rows

### Changed

- Time awareness now comes from per-message timestamp rendering instead of a request-only current-time system block
- The stable system prompt no longer carries runtime timezone guidance; `runtime.timezone` now drives cron matching, Slack timestamp formatting, and rendered user-message timestamps

### Fixed

- Memory harvester and cleaner model calls now use the same fallback-chain behavior as agent requests, including support for `models:` and recovery onto later providers
- Memory worker and top-level agent failures now log concise single-line errors instead of noisy tracebacks for expected model/runtime failures

## [0.6.2] - 2026-03-06

### Fixed

- OpenAI reasoning models with tool calls now route through LiteLLM's Responses bridge when `reasoning_effort` is enabled, avoiding the `gpt-5.4` `/v1/chat/completions` `BadRequestError` that required `/v1/responses`

## [0.6.1] - 2026-03-06

### Fixed

- Anthropic models no longer receive `reasoning_effort="none"` when `thinking: off`, avoiding the LiteLLM 1.82.0 `AttributeError` that previously broke every call with the default thinking setting

## [0.6.0] - 2026-03-06

### Added

- Guided onboarding via `operator setup` — picks a provider, detects timezone, stores credentials in `.env`, creates the first admin user, and can start the runtime immediately with `--run`
- `python -m operator_ai` entrypoint for environments where the `operator` script is not yet on `PATH`
- Simple reasoning control via `thinking: off|low|medium|high` at the defaults and per-agent level
- Memory retention classes (`candidate` and `durable`) plus `memory.candidate_ttl_days` for retention-aware recall and expiry

### Changed

- Runtime/process settings now live under `runtime` in `operator.yaml` (`timezone`, `env_file`, `show_usage`, and `reject_response`)
- Current time is injected just in time for live model requests in chats, jobs, and sub-agents while the stable system prompt now carries only the timezone contract
- Jobs and sub-agents inherit the resolved agent thinking level, runtime prompt, and skill filter more consistently
- Memory recall is stricter about scope and retention — public contexts no longer list user-scoped memories, and private contexts only expose the current user's memories
- Legacy memory tables are reset on first run of this release to support retention/expiry metadata; existing memories and memory worker state are discarded
- Harvester and cleaner prompts are stricter about rejecting short-lived conversational noise and preserving retention metadata

### Fixed

- `run_skill` now expands environment variable references in arguments after env sanitization, while still preserving `$OPERATOR_HOME`
- Cross-provider fallbacks drop reasoning metadata before each model call so retries stay compatible across providers
- Config loading now raises `ConfigError` internally instead of terminating via `SystemExit`
- Slack thread timestamps now render in the configured runtime timezone
- Same-agent `spawn_agent(...)` calls once again inherit the current agent's full prompt/runtime context even when no explicit `agent=` override is provided

## [0.5.1] - 2026-03-05

### Fixed

- Replace `LOGIN_SHELL` subprocess wrapping with proper PATH resolution — `operator service install` now embeds the full interactive PATH in the launchd plist / systemd unit, so tools installed via pyenv, nvm, Homebrew, etc. work correctly under background services (#15)
- Cleaner error output when a tool command is not found (no more full tracebacks for simple `FileNotFoundError`)

### Changed

- `.env` file uses `setdefault` semantics — shell environment takes precedence over `.env` values
- `operator init` generates `.env` for API key defaults only (PATH is handled by the service definition)

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
