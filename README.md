# Operator

Operator is a local agent runtime that connects chat messages to LLMs via LiteLLM.

It is intentionally small and file-driven:
- Markdown files for agent prompts, jobs, and skills.
- SQLite for durable runtime state (memory, messages, runs, jobs, etc).
- A single execution path for both inbound chat and scheduled jobs.

## Core Features

- Multiple agents (`~/.operator/agents/*/AGENT.md`)
- Multiple transports (currently Slack)
- Skills discovery from `~/.operator/skills/*/SKILL.md`
- Scheduled jobs with `prerun` gating and `postrun` hooks
- Durable conversation history and run tracking in SQLite
- Slack thread continuity via persistent platform message index
- Turn-safe context truncation against model token budgets
- Vector memory with automatic harvesting and semantic search (sqlite-vec)

## Quickstart

```sh
pip install operator-ai
operator init
```

This creates `~/.operator/` with a starter config, system prompt, and a default agent. Next:

1. **Edit `~/.operator/operator.yaml`** — set your model, transport, and API key source.
2. **Set API keys** — export `ANTHROPIC_API_KEY` (or whichever provider you chose), plus transport tokens (e.g. `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`).
3. **Run it:**

```sh
operator
```

The `init` command is idempotent — running it again won't overwrite existing files.

## Install

```sh
pip install operator-ai
```

Or for development:

```sh
pip install -e .
```

## Configuration

Runtime config lives at `~/.operator/operator.yaml`. The starter config from `operator init` looks like:

```yaml
defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
  max_iterations: 25
  context_ratio: 0.5
  # env_file: "~/.env"        # Load API keys from a dotenv file

agents:
  default:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN
```

A more advanced example with multiple agents and model fallbacks:

```yaml
defaults:
  models:
    - "anthropic/claude-opus-4-6"
    - "openai/gpt-5.3-codex"
  max_iterations: 25
  context_ratio: 0.5
  max_output_tokens: null    # null = use each model's max; set to cap output length
  env_file: "~/.env"

agents:
  hermy:
    models:
      - "anthropic/claude-sonnet-4-6"
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN_HERMY
      app_token_env: SLACK_APP_TOKEN_HERMY
```

`models` is a fallback chain — if the first model errors (overloaded, rate limited, down), the next is tried automatically. Always use list format, even for a single model.

`max_output_tokens` controls the maximum response length per LLM call. When `null` (default), each model's full output capacity is used. Set an integer to cap all models uniformly. Can be overridden per-agent.

Agents without a `transport` block are available for jobs and sub-agent spawning but have no chat interface.

### Memory

```yaml
memory:
  embed_model: "openai/text-embedding-3-small"   # required when any service is enabled
  embed_dimensions: 1536
  max_memories: 10000                             # per scope, soft cap
  inject_top_k: 5                                 # memories injected per message
  inject_min_relevance: 0.1                       # cosine similarity threshold
  harvester:
    enabled: true
    schedule: "*/30 * * * *"                      # required when enabled — cron
    model: "openai/gpt-4.1-mini"                  # required when enabled
  cleaner:
    enabled: true
    schedule: "0 3 * * *"                         # required when enabled — cron
    model: "anthropic/claude-haiiku-4-5"          # required when enabled
```

The `harvester` and `cleaner` each have their own `enabled` flag. When enabled, `schedule` and `model` are required — startup will error if missing. When disabled, the fields can be left empty. `embed_model` is required when either service is enabled.

- **Harvester** — extracts facts from conversations using an LLM and stores them as vector embeddings in `operator.db` (via sqlite-vec).
- **Cleaner** — deduplicates, merges, and tidies stored memories by sending them through an LLM for normalization.

On each incoming message, relevant memories are retrieved by semantic similarity and injected into the user message as context.

Memories are scoped: `user` (personal facts), `agent` (agent-specific context), and `global` (shared knowledge). Memories can be **pinned** — pinned memories are always injected into the system prompt regardless of similarity.

API keys for the models are resolved from the environment (e.g. `OPENAI_API_KEY`), loaded via `env_file` in the config.

### Key-Value Store

Agents have a scoped key-value store in SQLite for operational state — tracking processed items, cursors, watermarks, etc. Keys are scoped by agent name and grouped by namespace.

- `kv_set(key, value, namespace?, ttl_hours?)` — store a value, optionally with auto-expiry.
- `kv_get(key, namespace?)` — retrieve a value.
- `kv_delete(key, namespace?)` — remove a key.
- `kv_list(namespace?, prefix?)` — list keys and values.

Use namespaces to group related keys (typically by job name). Use TTL to prevent unbounded growth for tracking sets (e.g. seen email IDs).

### Agent Workspace

Each agent has a workspace directory at `~/.operator/agents/<name>/workspace/`. This is the working directory for all tool calls — shell commands, file reads/writes, and `list_files` all resolve relative paths against it. Files written here persist across conversations and job runs.

## Filesystem Layout

Everything lives under `~/.operator/`:

```text
~/.operator/
├── operator.yaml
├── SYSTEM.md               # system preamble (auto-created from template)
├── logs/
├── state/
│   └── operator.db
├── agents/
│   └── <agent>/
│       ├── AGENT.md
│       └── workspace/
├── jobs/
│   └── <job>/
│       ├── JOB.md
│       └── scripts/
└── skills/
    └── <skill>/
        ├── SKILL.md
        └── scripts|references|assets/
```

## Running

```sh
operator
```

Logs are written to `~/.operator/logs/operator.log`.

## CLI

The `operator` command doubles as a CLI for inspecting and managing runtime state. Subcommands run standalone (no running service required).

### Init

```sh
operator init                  # scaffold ~/.operator with starter config and agent
```

### Service

```sh
operator service install       # generate and load a service definition (launchd/systemd)
operator service uninstall     # unload and remove the service definition
operator service start         # start the background service
operator service stop          # stop the background service
operator service restart       # restart the background service
operator service status        # show whether the service is running
```

### Logs

```sh
operator logs [-f/--follow] [-n/--lines N]
```

Tails `~/.operator/logs/operator.log`. Defaults to the last 50 lines.

### Jobs

```sh
operator job list              # all jobs with status, schedule, counters
operator job info <job-name>   # job config and runtime state
operator job run <job-name>    # trigger a job immediately (outside cron)
operator job enable <job-name> # enable a job
operator job disable <job-name># disable a job
```

### KV Store

```sh
operator kv get <key> [--agent/-a NAME] [--ns/-n NAMESPACE]
operator kv set <key> <value> [--agent/-a NAME] [--ns/-n NAMESPACE] [--ttl HOURS]
operator kv delete <key> [--agent/-a NAME] [--ns/-n NAMESPACE]
operator kv list [--agent/-a NAME] [--ns/-n NAMESPACE] [--prefix/-p PREFIX]
```

`kv get` prints the raw value and exits 0, or exits 1 if not found. `kv list` outputs JSON.

### Memories

```sh
operator memories [--scope/-s SCOPE] [--scope-id/-i ID] [--pinned] [--limit/-n N]
operator memories stats        # memory counts per scope
```

### Inspection

```sh
operator config                # print resolved configuration as JSON
operator agents                # list configured agents with transport and model info
operator skills                # list discovered skills with env status
```

### Agent Resolution

CLI commands that need an agent name resolve it in order: `--agent` flag, then `OPERATOR_AGENT` env var, then the default agent from config. In job hook scripts, `OPERATOR_AGENT` is set automatically so `--agent` can be omitted.

### Hook Environment

Job hook scripts (`prerun`, `postrun`) receive these environment variables:

| Variable | Description |
|----------|-------------|
| `JOB_NAME` | Name of the job being executed |
| `OPERATOR_AGENT` | Agent running the job |
| `OPERATOR_HOME` | Path to `~/.operator` |
| `OPERATOR_DB` | Path to the SQLite database |

## Job Format

Each job is `~/.operator/jobs/<name>/JOB.md` with YAML frontmatter and markdown body.

```yaml
---
name: daily-summary
description: Summarize today's activity
schedule: "0 9 * * *"
agent: hermy
model: "anthropic/claude-sonnet-4-6"
hooks:
  prerun: scripts/check.sh
  postrun: scripts/notify.sh
enabled: true
---

Summarize the key events from the last 24 hours.
Post a one-line teaser to #general, then reply in a thread with the full summary.
```

Notes:
- `model` overrides the agent's model for this job (litellm format). When omitted, the agent's configured model chain is used.
- `prerun` is a gate: non-zero exit skips LLM execution.
- `postrun` receives model output on stdin.
- The agent uses `send_message` to post results to Slack channels. The prompt body
  should include posting instructions (which channels, whether to thread, etc.).
- If you have nothing to post, simply don't call `send_message`.

### Job Counters

Each job tracks four counters in SQLite:

| Counter | Incremented when |
|---------|-----------------|
| `run_count` | LLM actually executed (success or error) |
| `error_count` | LLM ran but threw an exception |
| `gate_count` | `prerun` hook returned non-zero (job skipped) |
| `skip_count` | Cron fired but the previous run was still in progress |

## Built-in Tools

- `run_shell`
- `read_file`
- `write_file`
- `list_files`
- `web_fetch`
- `send_message`
- `spawn_agent`
- `manage_job`
- `save_memory`
- `search_memories`
- `forget_memory`
- `list_memories`
- `kv_get`
- `kv_set`
- `kv_delete`
- `kv_list`

## Commands

Messages starting with `!` bypass the LLM.

- `!stop` cancels the active request in the current conversation.

## System Prompt Assembly

Ordered from most stable (cache-friendly) to least stable:

1. `SYSTEM.md` — system preamble (auto-created at `~/.operator/SYSTEM.md`)
2. `AGENT.md` — agent prompt body (verbatim)
3. `# Context` block (platform/channel/user/workspace) or `# Job` block
4. Pinned memories (from SQLite, always injected) — chat only
5. Available skills from scanned `skills/*/SKILL.md`
6. Transport extras (`transport.get_prompt_extra()`) — e.g. Slack channel list, messaging instructions

## Conversation and Routing Model

Slack conversations use canonical IDs:

- `slack:{agent_name}:{channel_id}:{root_ts}`

Where `root_ts` is:
- `thread_ts` for threaded replies
- `ts` for top-level posts

The runtime stores `platform_message_id -> conversation_id` mappings so replies to proactive job messages continue in the correct history.

## Architecture

```text
src/operator_ai/
├── cli.py               # typer CLI (kv, job subcommands) + service entry point
├── main.py              # service lifecycle, dispatch, commands
├── config.py            # yaml + env config
├── agent.py             # litellm loop and tool execution
├── truncation.py        # token-budget, exchange-safe history shaping
├── store.py             # sqlite persistence, vector search, KV, caches
├── memory.py            # MemoryStore (embed/save/search) + MemoryHarvester + MemoryCleaner
├── jobs.py              # job scan/schedule/hooks/delivery
├── skills.py            # skill scan/frontmatter/prompt block
├── prompts/
│   ├── __init__.py      # load_system_prompt, load_agent_prompt
│   ├── system.md        # default SYSTEM.md template
│   ├── harvester.md     # memory harvester prompt
│   └── cleaner.md       # memory cleaner prompt
├── transport/
│   ├── base.py
│   └── slack.py
└── tools/
    ├── registry.py
    ├── workspace.py
    ├── shell.py
    ├── files.py
    ├── web.py
    ├── messaging.py
    ├── subagent.py
    ├── memory.py
    ├── kv.py
    └── jobs.py
```

## Releasing

1. Update `version` in `pyproject.toml`.
2. Add an entry to `CHANGELOG.md` under a new `## [x.y.z] - YYYY-MM-DD` heading.
3. Commit: `git commit -am "release: vx.y.z"`
4. Tag: `git tag vx.y.z`
5. Push: `git push && git push --tags`

Pushing a `v*` tag triggers the GitHub Actions workflow that builds and publishes to PyPI.
