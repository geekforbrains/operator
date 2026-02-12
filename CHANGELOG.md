# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-02-12

### Added

- Auto-install system prompt files (CLAUDE.md, AGENTS.md, GEMINI.md) to working directory on `operator serve`
- Bundled system prompt as package data so it ships with the package
- Existing user-customized prompt files are never overwritten

### Removed

- Symlinked AGENTS.md and GEMINI.md from repo root (canonical source is now `src/operator_agent/system_prompt.md`)

## [0.2.0] - 2026-02-12

### Added

- Telegram file upload support (documents, photos, audio, voice, video)
- Files downloaded to `{working_dir}/uploads/` and passed to the active agent
- Caption text included as context alongside the file path

## [0.1.1] - 2026-02-12

### Fixed

- Lowered Python requirement from 3.12 to 3.10

## [0.1.0] - 2026-02-12

Initial public release.

### Added

- Interactive setup wizard (`operator setup`) with provider detection, Telegram bot onboarding, and working directory configuration
- Telegram transport with live status updates as agents work
- Support for Claude, Codex, and Gemini CLI agents
- Chat commands for switching providers, models, stopping tasks, and managing sessions
- Background service installation for macOS (launchd) and Linux (systemd)
- Platform-aware setup summary with service management commands
