# Operator

Personal AI agent that bridges Telegram (more to come) to CLI agents running on your server. Send a message, get a response from Claude, Codex, or Gemini — with live status updates as they work. Use your subscription, not an API key.

## Quick Start

```bash
# Install
pip install "operator-agent[telegram]"

# Setup (creates bot, links your account, installs service)
operator setup

# Or run manually
operator serve
```

## How It Works

Operator runs as a background service on your server. When you send a Telegram message:

1. Your message is routed to the active CLI agent (Claude, Codex, or Gemini)
2. The agent runs in your working directory with full access to your files
3. Live status updates show what the agent is doing (reading files, running commands, etc.)
4. The final response is sent back to you in Telegram

## Setup

### Prerequisites

- Python 3.10+
- At least one CLI agent: [`claude`](https://docs.anthropic.com/en/docs/claude-code), [`codex`](https://github.com/openai/codex), or [`gemini`](https://github.com/google-gemini/gemini-cli)
- A Telegram account

### Recommended: use pyenv + virtualenv

We recommend using [pyenv](https://github.com/pyenv/pyenv) to manage Python versions and installing Operator in a virtualenv to avoid conflicts with system packages.

```bash
# Install Python 3.12+ via pyenv (if needed)
pyenv install 3.13
pyenv shell 3.13

# Create and activate a virtualenv
python -m venv ~/.operator-venv
source ~/.operator-venv/bin/activate

# Install Operator
pip install "operator-agent[telegram]"
```

### Running `operator setup`

The setup wizard walks you through:

1. **Provider detection** — checks which CLI agents are on your PATH
2. **Telegram bot creation** — guides you through @BotFather to create a bot, validates your token, and auto-captures your user ID when you send your first message
3. **Working directory** — where agents run commands and create files
4. **Background service** — optionally installs a service that starts on boot (launchd on macOS, systemd on Linux)

Config is saved to `~/.operator/config.json`.

## Commands

Send these in your Telegram chat with the bot:

| Command | Description |
|---------|-------------|
| `!status` | Show active provider and model |
| `!use claude\|codex\|gemini` | Switch provider |
| `!claude` / `!codex` / `!gemini` | Shortcut for `!use` |
| `!models` | List available models |
| `!model <name\|index>` | Switch model |
| `!stop` | Kill running process |
| `!clear` | Clear current provider session |
| `!clear all` | Clear all sessions |
| `!restart` | Restart the service |

## Configuration

### `~/.operator/config.json`

```json
{
  "working_dir": "/home/you/projects",
  "telegram": {
    "bot_token": "123456:ABC-DEF...",
    "allowed_user_ids": [your_telegram_id]
  },
  "providers": {
    "claude": {
      "path": "claude",
      "models": ["opus", "sonnet", "haiku"]
    },
    "codex": {
      "path": "codex",
      "models": ["gpt-5.3-codex"]
    },
    "gemini": {
      "path": "gemini",
      "models": ["gemini-2.5-pro", "gemini-2.5-flash"]
    }
  }
}
```

- **working_dir** — where agents execute commands and create files
- **bot_token** — from @BotFather
- **allowed_user_ids** — Telegram user IDs that can use the bot (empty = allow all)
- **providers.*.path** — CLI binary name or full path
- **providers.*.models** — available models for each provider

### `~/.operator/state.json`

Runtime state managed automatically. Contains active provider/model per chat and session IDs for conversation continuity.

## Running as a Service

`operator setup` can install a background service. Management commands are shown at the end of setup.

### macOS (launchd)

```bash
# Start / Stop
launchctl load ~/Library/LaunchAgents/com.operator.agent.plist
launchctl unload ~/Library/LaunchAgents/com.operator.agent.plist

# Logs
tail -f ~/.operator/operator.log
```

### Linux (systemd)

```bash
# Start / Stop / Restart
systemctl --user start operator
systemctl --user stop operator
systemctl --user restart operator

# Status & Logs
systemctl --user status operator
journalctl --user -u operator -f
```

## Development

```bash
# Install with dev deps
pip install -e ".[telegram,dev]"

# Lint
ruff check src/

# Integration tests (requires CLI agents installed)
python tests/test_integration.py
```

## Versioning & Releases

This project uses [semver](https://semver.org/) (`MAJOR.MINOR.PATCH`). While pre-1.0, minor bumps may include breaking changes.

The version lives in `pyproject.toml` → `project.version` and is read at runtime via `importlib.metadata`.

### Release flow

```bash
# 1. Bump version in pyproject.toml
# 2. Update CHANGELOG.md
# 3. Commit and tag
git commit -m "release: v0.x.y"
git tag v0.x.y

# 4. Build and publish
python -m build
twine upload dist/*

# 5. Push
git push && git push --tags
```
