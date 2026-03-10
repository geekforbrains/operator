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
- **Time-aware request history.** User requests, job prompts, and sub-agent task messages carry their creation time into model input, rendered like `[Monday, 2026-03-09T09:22:40-07:00]` in your configured timezone without mutating the stable system prompt.
- **Portable thinking controls.** Set `thinking: off|low|medium|high` instead of provider-specific reasoning budgets. Operator maps it when the concrete model supports reasoning and drops it safely when it does not.
- **Model-agnostic.** Supports 100+ LLM providers out of the box. Define fallback chains so if your primary model is down, the next one picks up automatically. Failover applies to agents, jobs, memory harvesting, and memory cleaning.
- **Runs on your machine.** No SaaS, no cloud dependency, no data leaving your network. Install it, run it, own it.

## Quickstart

```sh
pip install operator-ai
operator setup
```

`operator setup` takes you from a fresh install to a working Slack-backed agent:

- scaffolds `~/.operator/`
- asks which model provider you want to use: Anthropic, OpenAI, or Gemini
- detects your local timezone and lets you confirm or change it
- saves the API keys and Slack tokens operator needs
- creates your first admin user
- leaves you one command away from the first message

Use `operator setup --run` if you want it to start the runtime immediately after onboarding.

If the `operator` script is not on your `PATH` yet right after `pip install`, use:

```sh
python3 -m operator_ai setup --run
```

### What setup asks for

- your model provider: Anthropic, OpenAI, or Gemini
- your timezone, defaulting to the current system timezone
- the matching API key for that provider
- `SLACK_BOT_TOKEN` (`xoxb-*`)
- `SLACK_APP_TOKEN` (`xapp-*`)
- your Slack user ID

### Manual setup

If you want to configure everything by hand instead, run:

```sh
operator init
```

Then edit `~/.operator/operator.yaml`:

```yaml
runtime:
  timezone: "America/Vancouver"
  env_file: ".env"
  show_usage: false
  reject_response: ignore

defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
  thinking: medium
  max_iterations: 25

memory:
  embed_model: "openai/text-embedding-3-small"
  inject_top_k: 3
  inject_min_relevance: 0.3
  candidate_ttl_days: 14

agents:
  operator:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN
      include_archived_channels: false
      inject_channels_into_prompt: false
      channel_cache_ttl_seconds: 900
      warm_channels_on_startup: true
```

Slack channel discovery is cached per bot, warms once on startup, refreshes lazily on demand, and ignores archived channels by default. Override those defaults in the transport block if you want archived channels included, prompt-time channel injection enabled, or a different cache TTL.

Add your keys to `~/.operator/.env` or export them in your shell:

```sh
export ANTHROPIC_API_KEY="sk-..."
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
```

```sh
operator user add yourname --role admin slack YOUR_SLACK_USER_ID
```

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

Memories can be:

- `candidate`: short-lived recall that auto-expires after `memory.candidate_ttl_days`
- `durable`: long-lived memory until explicitly removed
- `pinned`: separate from retention and always injected into the system prompt

Tune recall with `memory.inject_top_k`, `memory.inject_min_relevance`, and `memory.candidate_ttl_days`. Inspect live memory state with `operator memories` and `operator memories stats`.

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

### Thinking

Use `thinking` to request a simple reasoning level without exposing provider-specific knobs:

```yaml
defaults:
  thinking: off

agents:
  researcher:
    models:
      - "anthropic/claude-sonnet-4-6"
    thinking: high

  planner:
    models:
      - "openai/o3"
    thinking: medium

  fast-bot:
    models:
      - "gemini/gemini-2.5-flash"
    thinking: low
```

Supported values:

- `off`
- `low`
- `medium`
- `high`

Operator maps these to LiteLLM reasoning controls when the selected model supports them. For most reasoning-capable models that means `reasoning_effort`; for Anthropic, `thinking: off` is sent by omitting the control entirely for LiteLLM compatibility. If a fallback model does not support reasoning control, Operator omits the param and continues normally. Jobs inherit the agent's thinking level; there is no per-job thinking override.

## CLI

Operator ships with a full CLI for managing everything outside of chat.

```sh
operator                          # run the service
operator init                     # scaffold ~/.operator/
operator setup                    # guided onboarding
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
