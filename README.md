<p align="center">
  <img src="operator_banner.png" alt="Operator" width="500" />
</p>

<h1 align="center">🐒 Operator</h1>
<p align="center"><strong>Agents have joined the chat.</strong></p>

Operator deploys autonomous AI agents into your team's chat — Slack today, more platforms coming. Define agents in markdown, give them tools and permissions, and let them work alongside your team. They remember context, hand off tasks to each other, and run scheduled jobs while you sleep.

Built for teams, not just individuals. Multi-user auth, role-based access, isolated memories, agent-to-agent delegation, model failover — the boring-but-critical stuff that makes agents actually work in a team setting.

## Why Operator

- **Agents as teammates.** They live in your Slack channels, respond to messages, and post results — just like a coworker who never goes on PTO.
- **Multi-agent orchestration.** Agents delegate to each other via `spawn_agent`. A coordinator can dispatch work to a researcher, a coder, and a reviewer — each with their own prompt, tools, and permissions.
- **Team-native.** Multi-user auth with roles. Control who can talk to which agents. Isolated per-user memories. Not a single-player toy.
- **Markdown-driven.** Agents, jobs, and skills are markdown files with YAML frontmatter. Version them in git, review them in PRs, edit them in your editor. No dashboards, no YAML hellscapes.
- **Model-agnostic.** Supports 100+ LLM providers out of the box. Define fallback chains so if your primary model is down, the next one picks up automatically.
- **Runs on your machine.** No SaaS, no cloud dependency, no data leaving your network. Install it, run it, own it.

## Quickstart

```sh
pip install operator-ai
operator init
```

This scaffolds `~/.operator/` with a starter config, system prompt, and a default agent.

### 1. Configure

Edit `~/.operator/operator.yaml`:

```yaml
defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
  max_iterations: 25

agents:
  operator:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN
```

### 2. Set your keys

Add API keys to `~/.operator/.env` or export them in your shell:

```sh
export ANTHROPIC_API_KEY="sk-..."
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
```

### 3. Add yourself

```sh
operator user add yourname --role admin slack YOUR_SLACK_USER_ID
```

### 4. Run it

```sh
operator                    # run in the foreground
operator service install    # or install as a background service
```

That's it. Message your agent in Slack. 🐒

> **Background service note:** `service install` captures your shell's PATH and embeds it in the service definition, so tools installed via Homebrew, pyenv, nvm, etc. are available to your agents even under launchd/systemd.

## What you get

### Agents

Markdown files at `~/.operator/agents/<name>/AGENT.md`. Each agent gets its own system prompt, workspace, model config, and permissions. Add YAML frontmatter with a `description` and agents automatically discover each other for delegation.

```yaml
---
name: researcher
description: Deep research agent with web access.
---

You are a research specialist. When given a topic...
```

### Jobs

Scheduled tasks with cron expressions, prerun gates, and postrun hooks. Agents run jobs autonomously and post results to your channels.

```yaml
---
name: daily-summary
schedule: "0 9 * * *"
agent: operator
---

Summarize the key events from the last 24 hours.
Post to #general with a thread for the full breakdown.
```

### Skills

Reusable capabilities at `~/.operator/skills/<name>/SKILL.md` — scripts, references, and assets that any agent can discover and use.

### Memory

Vector memory with automatic harvesting. Operator extracts facts from conversations, stores them as embeddings, and injects relevant context into future messages. Memories are scoped per-user, per-agent, and globally — so agents remember your preferences without leaking them to your teammates.

### Permissions

Opt-in access control. No permissions block = full access. Add one to lock an agent down to specific tools and skills. Roles control which users can talk to which agents. Simple to understand, hard to misconfigure.

```yaml
agents:
  public-bot:
    permissions:
      tools: [read_file, web_fetch, search_memories]
      skills: [summarize]

roles:
  team:
    agents: [operator, researcher]
```

### Model failover

`models` is a fallback chain. If the first model errors, rate limits, or goes down, the next one picks up. No downtime, no babysitting.

```yaml
defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
    - "openai/gpt-4.1"
```

## CLI

Operator ships with a full CLI for managing everything outside of chat.

```sh
operator                          # run the service
operator init                     # scaffold ~/.operator/
operator agents                   # list configured agents
operator tools                    # list available tools
operator skills                   # list discovered skills
operator job list                 # show all jobs with status
operator job run <name>           # trigger a job now
operator user list                # show all users
operator user add <name> ...      # add a user
operator memories                 # browse stored memories
operator kv list                  # inspect the key-value store
operator logs -f                  # tail logs
operator service install          # install as a system service
```

## Docs

Full documentation at **[operator.geekforbrains.com](https://operator.geekforbrains.com)**

## License

MIT
