<p align="center">
  <img src="operator_banner.png" alt="Operator" width="500" />
</p>

<h1 align="center">🐒 Operator</h1>
<p align="center"><strong>Agents have joined the chat.</strong></p>

Operator deploys autonomous AI agents into your team's chat. Slack today, more platforms coming. Define agents in markdown, give them tools and permissions, and let them work alongside your team. They remember context, hand off tasks to each other, and run scheduled jobs while you sleep.

Built for teams, not just individuals. Multi-user auth, role-based access,
isolated memories, agent-to-agent delegation, model failover — the
boring-but-critical stuff that makes agents actually work in a team setting.

At a high level, Operator is intentionally opinionated: agents are defined in
markdown, work in files, and keep long-term memory as file-backed `rules/` and
`notes/` rather than hidden vector state. `rules/` are always injected,
`notes/` are searched on demand, and expired memory moves to `trash/` instead of
disappearing silently.

See [PRINCIPLES.md](PRINCIPLES.md) for the full product and engineering
guidelines behind Operator.

## Why Operator

- **Agents as teammates.** They live in your Slack channels, respond to messages, and post results — just like a coworker who never goes on PTO.
- **Multi-agent orchestration.** Agents delegate to each other via `spawn_agent`. A coordinator can dispatch work to a researcher, a coder, and a reviewer — each with their own prompt, tools, and permissions.
- **Team-native.** Multi-user auth with roles. Control who can talk to which agents. Isolated per-user memories. Not a single-player toy.
- **Markdown-driven.** Agents, jobs, and skills are markdown files with YAML frontmatter. Version them in git, review them in PRs, edit them in your editor. No dashboards, no YAML hellscapes.
- **Model-agnostic.** Supports 100+ LLM providers out of the box. Define fallback chains so if your primary model fails or returns an unusable response, the next one picks up automatically.
- **Runs on your machine.** No SaaS, no cloud dependency, no data leaving your network. Install it, run it, own it.

## Quickstart

```sh
pip install operator-ai
operator init
```

`operator init` creates the default Slack-focused scaffold under `~/.operator/`:

- writes `operator.yaml`, `.env`, `SYSTEM.md`, and `agents/operator/AGENT.md`
- creates the default workspace, memory, state, shared, db, jobs, skills, and logs directories
- gives the default `operator` agent full tool/skill access and full filesystem access
- writes built-in `permission_groups` at the bottom of `operator.yaml`
- prompts before overwriting an existing `operator.yaml`

If the `operator` script is not on your `PATH` yet right after `pip install`, use:

```sh
python3 -m operator_ai init
```

Then edit `~/.operator/operator.yaml`:

```yaml
runtime:
  env_file: ".env"
  show_usage: false
  reject_response: ignore

defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
  thinking: "off"
  max_iterations: 25
  context_ratio: 0.5
  hook_timeout: 30

agents:
  operator:
    sandbox: false
    permissions:
      # Define groups with @groupname or tool name
      # Example:
      #   tools: ["@memory", "@files"]
      #   tools: ["@memory", "read_file", "list_files"]
      tools: "*"
      skills: "*"
    transport:
      type: slack
      env:
        bot_token: SLACK_BOT_TOKEN
        app_token: SLACK_APP_TOKEN
      settings:
        include_archived_channels: false
        inject_channels_into_prompt: true
        inject_users_into_prompt: true
        expand_mentions: true

roles:
  guest:
    agents: []

permission_groups:
  memory:
    - save_rule
    - save_note
    - search_notes
    - list_rules
    - list_notes
    - read_note
    - forget_rule
    - forget_note
  files:
    - read_file
    - write_file
    - list_files
  messaging:
    - send_message
    - send_file
  skills:
    - create_skill
    - update_skill
    - delete_skill
    - list_skills
    - read_skill
    - run_skill
  jobs:
    - create_job
    - update_job
    - delete_job
    - enable_job
    - disable_job
    - list_jobs
  state:
    - get_state
    - set_state
    - append_state
    - pop_state
    - list_state
    - delete_state
  shell:
    - run_shell
  web:
    - web_fetch
  users:
    - manage_users
    - set_timezone
  agents:
    - spawn_agent
```

Transport config has three parts: `type`, `env`, and `settings`.

- `env` maps logical credential names to environment variable names
- `settings` covers non-secret transport behavior
- For Slack, the required fields are `type`, `env.bot_token`, and `env.app_token`

Users and channels are injected into the agent prompt by default so the agent knows who and what is available without a tool call. The channel list is a current snapshot refreshed on startup and channel lifecycle events, so renamed channels don't linger under old names. Override `inject_users_into_prompt` or `inject_channels_into_prompt` for large workspaces.

The generated `permission_groups` block stays at the bottom of the file so the main runtime, model, and transport settings stay easy to scan.

Add your keys to `~/.operator/.env`:

```sh
ANTHROPIC_API_KEY="sk-..."
SLACK_BOT_TOKEN="xoxb-..."
SLACK_APP_TOKEN="xapp-..."
```

```sh
operator user add yourname --role admin slack YOUR_SLACK_USER_ID
```

```sh
operator                    # run in the foreground
operator service install    # or install as a background service
operator service start      # start the background service when needed
```

That's it. Message your agent in Slack. 🐒

> **Background service note:** `service install` captures your shell's PATH and embeds it in the service definition, so tools installed via Homebrew, pyenv, nvm, etc. are available to your agents even under launchd/systemd.

## What you get

### Agents

Markdown files at `~/.operator/agents/<name>/AGENT.md`. Each agent gets its own system prompt, workspace, model config, and permissions. The configured agent key in `operator.yaml` is the runnable identity; `AGENT.md` frontmatter supplies the human-facing `description` used when other agents consider delegation.

```yaml
---
description: Deep research agent with web access.
---

You are a research specialist. When given a topic...
```

### Delegation

Use `spawn_agent` to hand off focused work to a fresh child run. The child run
switches to the target agent's own `AGENT.md`, workspace, memory, tools,
skills, and permissions.

Delegation preserves the current run mode instead of copying the parent
conversation. A chat-triggered child run keeps the current session envelope
(user, transport, current channel/thread bindings). A job-triggered child run
stays in job mode and keeps job semantics.

That preserved envelope is runtime context only. The child still runs with the
target agent's own tools, skills, and permissions.

This is an agent swap, not a conversation fork. The child starts with fresh
conversation state, works the delegated task, and returns a final result to the
parent.

### Jobs

Scheduled tasks with cron expressions, prerun gates, and postrun hooks. Agents create and manage jobs using deterministic tools with explicit typed parameters — no raw YAML authoring required.

```python
create_job(
    name="daily-summary",
    schedule="0 9 * * *",
    prompt="Summarize the key events from the last 24 hours.\nPost to #general with a thread for the full breakdown.",
    agent="operator",
)
```

Six tools cover the full lifecycle: `create_job`, `update_job`, `delete_job`, `enable_job`, `disable_job`, `list_jobs`. Each tool has explicit parameters for every field — the tool assembles the job file internally.

**Prerun scripts** gate execution and inject data. A prerun script's stdout is passed into the job prompt as `<prerun_output>`, so the model works on pre-filtered data instead of making redundant API calls. Use scripts for anything deterministic (data fetching, date logic, filtering) and reserve the model for interpretation and formatting.

Hook scripts have a configurable timeout (`defaults.hook_timeout`, default 30s) — if a hook exceeds it, the job is gated or failed.

### Skills

Reusable capabilities at `~/.operator/skills/<name>/SKILL.md` — authored instructions, references, and assets that teach agents how to perform specific tasks. Skills are not tools; they are knowledge that any agent can discover and use.

Agents create and manage skills using deterministic tools with explicit typed parameters — no raw YAML authoring required.

```python
create_skill(
    name="pr-reviewer",
    description="Reviews GitHub PRs by analyzing diffs and posting structured review comments. Use when asked to review a PR or check code quality.",
    instructions="# PR Reviewer\n\n## Steps\n\n1. Fetch the PR diff using `gh pr diff <number>`\n2. Analyze for security issues, logic errors, style consistency\n3. Post a structured review using `gh pr review`",
    env="GITHUB_TOKEN",
)
```

Four tools cover the full lifecycle: `create_skill`, `update_skill`, `delete_skill`, `list_skills`. Each tool has explicit parameters for every field — the tool assembles the SKILL.md file internally.

### Memory

Operator keeps long-term memory as markdown files — the source of truth is always human-readable and editable. Memory is scoped per-user, per-agent, and globally so agents can remember preferences and reusable knowledge without leaking them across the wrong contexts.

Memory has two behaviors:

- `rules/`: always injected and meant to stay short, curated, and high-signal
- `notes/`: searched on demand for durable knowledge that should not bloat every prompt

Each memory item lives in its own markdown file with minimal frontmatter. Short-lived memory uses `expires_at`, and expired items move to `trash/` instead of disappearing silently. See [PRINCIPLES.md](PRINCIPLES.md) for the full memory model and reasoning behind it.

#### Search

Notes are searched using SQLite FTS5 with Porter stemming and BM25 relevance ranking. The index is derived from memory files and rebuilt automatically on startup. After manually editing memory files, run `operator memory index` to update the index.

#### Optional: Vector Embeddings

For semantic search (e.g., "deployment schedule" matching notes about "release cadence"), configure an embedding model:

```yaml
defaults:
  embeddings:
    model: "openai/text-embedding-3-small"
    dimensions: 1536
```

When configured, embeddings are computed on write and used alongside FTS5 via Reciprocal Rank Fusion. Install `sqlite-vec` for vector storage:

```sh
pip install sqlite-vec
```

Without embeddings, FTS5 handles all search. No external model dependency is required for core memory functionality.

### Permissions and sandbox

Restrict by default. A new agent with no `permissions` block has access to nothing — permissions are an allowlist. Roles control which users can talk to which agents.

Agents are also **sandboxed by default** — file tools (`read_file`, `write_file`, `list_files`) are confined to the agent's workspace. Set `sandbox: false` to unlock full filesystem access. `run_shell` is always unrestricted — granting it is the explicit trust signal for full machine access.

```yaml
agents:
  operator:
    sandbox: false  # full filesystem access
    permissions:
      tools: "*"
      skills: "*"

  researcher:
    # sandbox: true (default) — file tools confined to workspace
    permissions:
      tools: [read_file, write_file, list_files, web_fetch, search_notes]
      skills: "*"

  public-bot:
    permissions:
      tools: [web_fetch, search_notes]
      skills: [summarize]

roles:
  team:
    agents: [operator, researcher]
```

### Thinking

Use `thinking` to request a reasoning level without exposing provider-specific knobs:

```yaml
defaults:
  thinking: "off"

agents:
  researcher:
    models:
      - "anthropic/claude-sonnet-4-6"
    thinking: "high"

  planner:
    models:
      - "openai/o3"
    thinking: "medium"

  fast-bot:
    models:
      - "gemini/gemini-2.5-flash"
    thinking: "low"
```

Supported values: `off`, `low`, `medium`, `high`.

Operator maps these to LiteLLM reasoning controls when the selected model supports them. If a fallback model does not support reasoning control, Operator omits the param and continues normally. Jobs inherit the agent's thinking level; there is no per-job thinking override.

### Model failover

`models` is a fallback chain. If the first model errors, rate limits, goes down, or returns an unusable response, the next one picks up. No downtime, no babysitting.

```yaml
defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
    - "openai/gpt-4.1"
```

### Time-aware history

User requests, job prompts, and sub-agent task messages carry their creation time into model input, rendered like `[Monday, 2026-03-09T09:22:40-07:00]` in the user's configured timezone. This keeps the agent temporally grounded without mutating the stable system prompt.

### Slack

Slack conversations are intentionally thread-scoped. A top-level channel mention or DM starts a fresh session rooted at that message. Later messages continue the same session only when they are directed at the agent in that thread. In channels, that means mentioning the agent again; unmentioned human replies are ignored. This keeps each conversation focused and prevents the agent from jumping into every thread reply.

When the agent needs context outside the current session, it should reach for Slack tools like `slack_read_channel` or `slack_read_thread`.

**Reactions.** Agents can add and remove emoji reactions via `slack_add_reaction` and `slack_remove_reaction`. When users react to messages the agent has seen, those reactions appear as system events in the next interaction.

**System events.** Lightweight platform events (currently reactions) that are too noisy to trigger a full agent run but useful as ambient context. Events are buffered per conversation and injected into the next user message:

```
<context_snapshot source="system_events">
Recent platform events since your last response:

- [3:18 AM] Reaction :thumbsup: added by Alice on message 1773310673.238109 in #general
</context_snapshot>
```

The buffer is capped at 20 events per conversation, consecutive duplicates are suppressed, and events are drained on read. No persistence — if the process restarts, pending events are lost.

**Tools.** Slack transport tools are namespaced with a `slack_` prefix: `slack_find_users`, `slack_list_channels`, `slack_read_channel`, `slack_read_thread`, `slack_add_reaction`, `slack_remove_reaction`. When multiple users share a display name, use `slack_find_users` and the returned `<@UID>` mention instead of relying on raw `@Name`.

**Message context.** Each inbound message includes its message ID and channel ID so the agent can react to or reference the message it's responding to without an extra API call.

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
operator memory list              # browse file-backed memory
operator memory search <query>    # search notes (FTS5)
operator memory index             # rebuild search index from files
operator memory index --force     # full rebuild (drop + reindex)
operator logs -f                  # tail logs
operator service install          # install as a system service
```

## Docs

Full documentation at **[operator.geekforbrains.com](https://operator.geekforbrains.com)**

## License

MIT
