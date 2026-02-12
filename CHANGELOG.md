# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-02-12

Initial public release.

### Added

- Interactive setup wizard (`operator setup`) with provider detection, Telegram bot onboarding, and working directory configuration
- Telegram transport with live status updates as agents work
- Support for Claude, Codex, and Gemini CLI agents
- Chat commands for switching providers, models, stopping tasks, and managing sessions
- Background service installation for macOS (launchd) and Linux (systemd)
- Platform-aware setup summary with service management commands
