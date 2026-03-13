# Operator Principles

This document captures the intended architecture and product stance for Operator.
It exists for two audiences:

- contributors, so development stays aligned on what belongs in the system and why
- users, so the high-level behavior of the system is understandable and predictable

If the current implementation differs from this document, this document wins.

## Core Use Case

Operator is a local, multi-transport, team-facing agent runtime.

Users interact with agents through transports such as Slack. Each agent has a
clear role, a workspace, tools, and persistent memory. Agents do real work in
files, continue work over time, delegate focused tasks to other agents, and run
recurring jobs.

The core loop is:

1. A user or job triggers an agent.
2. The agent receives its standing instructions and relevant context.
3. The agent works in its workspace using tools.
4. The agent returns results to the user or destination when needed.
5. The system persists the right state so the agent can continue later.

**Everything in Operator should justify itself against that loop.**

## Design Goals

### Prefer one strong way over many toggles

Operator should be opinionated. The system should have a small number of clear,
consistent concepts rather than many interchangeable storage modes, memory
backends, or lifecycle flags.

### Prefer files for human-facing knowledge

Anything humans may want to inspect, edit, review, diff, or debug should default
to files.

This includes:

- agent definitions
- job definitions
- skills
- work artifacts
- long-term memory content

### Prefer deterministic structure over hidden model state

Agents should not invent their own file layouts or memory conventions. When
agents create durable memory, they should do it through dedicated tools that
enforce a strict layout and lifecycle.

### Typed tools, not raw file composition

Agents interact with structured resources — jobs, skills, memory, state —
through dedicated tools with explicit typed parameters. The agent provides
structured data; the tool produces the file. This eliminates format errors,
ensures required fields are always present, and keeps tool signatures
self-documenting. No bundled skill is needed to teach the agent how to write
YAML or frontmatter.

### Separate authored definitions, memory, artifacts, and machine state

These are different things and should stay different:

- `AGENT.md` is the agent's charter
- memory is learned reusable information
- workspace files are work artifacts
- conversation history is episodic record
- runtime state is machine-managed coordination data

### Keep transports generic

A transport is how users interact with agents. Slack is one transport, not the
architecture itself. Transport code should handle ingress, egress, identities,
thread semantics, attachments, and platform-specific context, while leaving
agent behavior and memory semantics transport-agnostic.

Transport-specific interaction rules should still be explicit where they matter
to the product contract — but they belong under the transport, not in the core.

#### Slack contract

Conversations are intentionally thread-scoped: every top-level message addressed
to an agent starts a fresh session thread, and only later messages addressed to
the agent continue that session.

In channels, that means mention-gated interaction: an `@mention` at the top level
starts a session thread, and only subsequent `@mentions` inside that thread
continue it. Ambient human chatter in the thread is ignored.

In DMs, every message is implicitly addressed to the agent — there is no one else
the user could be talking to. A top-level DM starts a fresh session thread, and
all subsequent messages in that thread continue it without requiring a mention.

The point is focus and isolation, not maximal ambient context. If an agent needs
information outside the current Slack session, it should use Slack tools to
inspect it deliberately.

Prompt-facing transport snapshots should reflect current platform truth rather
than accumulate stale aliases. Injected channel lists should be rebuilt from
the current workspace view when channel lifecycle changes occur.

Human-friendly transport shorthands should fail closed when the platform cannot
resolve them uniquely. Outbound `@Name` expansion should only happen for unique
active display names; otherwise agents should resolve the person deliberately
and use the explicit `<@UID>` mention.

Ambient system-event surfaces should stay explicit and narrow. Reaction events
are the current conversational system-event surface; user/cache maintenance
events such as joins, renames, and channel lifecycle changes update transport
state but are not treated as ambient conversation context.

### Surface errors, don't swallow them

Operator's failure posture is loud and explicit:

- Errors are always surfaced to the user or operator, never silently dropped.
- Agents do not run degraded. If critical context cannot be loaded — rules,
  memory, agent definition — the failure is visible, not papered over.
- Tool failures are reported to the agent so it can adapt or report. The agent
  decides what to do; the runtime does not swallow the error on its behalf.
- Job run failures are recorded with the same visibility as hook failures.
  A failed agent run is not silently discarded.
- Retries are explicit, not automatic. The system does not guess whether a
  transient failure is worth retrying. If retry logic is needed, it belongs in
  hooks or deterministic code, not in silent runtime behavior.

The general rule: if something failed, someone should know.

## High-Level System Model

### Directory layout

Everything lives under `~/.operator/`:

```text
~/.operator/
  operator.yaml
  .env
  SYSTEM.md
  agents/
    <name>/
      AGENT.md
      workspace/
        inbox/
        work/
        artifacts/
        tmp/
        shared/          # symlink → ~/.operator/shared/
      memory/
        rules/
        notes/
        trash/
      state/
  jobs/
    <name>/
      JOB.md
      scripts/
  skills/
    <name>/
      SKILL.md
  memory/
    global/
      rules/
      notes/
      trash/
    users/
      <name>/
        rules/
        notes/
        trash/
  shared/
    <name>/              # one per agent, created at startup
  db/                     # SQLite database state
  logs/                   # runtime logs
```

Individual sections below explain each part in detail.

### Agent definitions

Agents are defined in markdown with YAML frontmatter at
`~/.operator/agents/<name>/AGENT.md`. The configured agent key in
`operator.yaml` is the runtime identity and source of truth. `AGENT.md`
frontmatter supplies the human-facing `description` used in delegation
awareness. The body is the stable, human-authored definition of the agent's
role, mission, capabilities, and hard constraints.

All configured agents are injected into the system prompt with their name and
description, regardless of the current user's access. Agents the user cannot
access are annotated as inaccessible. This lets the agent explain why it cannot
delegate to a particular agent rather than being unaware of its existence.

`AGENT.md` should change when the agent's charter changes. It should not be
rewritten casually in response to routine user feedback.

### Discovery

Skills and agents are discovered by scanning the filesystem at the start of
each agent run — each inbound message or job trigger. There is no hot reload
mid-turn and no file watchers. A newly created skill or agent is available on
the next request, not during the current one. Caching may be added later if
the scan becomes a performance concern.

### Prompt assembly

Operator has a shared global `SYSTEM.md` so common operating instructions do not
need to be duplicated across every agent.

The prompt is assembled broad to narrow — stable system context first,
per-turn specifics last:

1. `SYSTEM.md`
2. `AGENT.md`
3. available tools
4. discovered skills
5. known agents (inaccessible agents annotated, not filtered)
6. run envelope for the current execution mode
7. global rules
8. agent rules
9. user rules (when the interaction is user-scoped)

The run envelope is what makes a direct chat run feel like chat and a job run
feel like a job:

- chat runs inject transport prompt content plus resolved session context
  (platform, user, channel/thread semantics, timezone)
- job runs inject job context plus generic transport prompt content when the
  target agent has a transport

**The run envelope is execution context, not capability scope.** Tools, skills,
models, workspace, and permissions come from the active agent configuration.

Turn-local input is added after this base stack through the message list rather
than by mutating the stable system prompt. For chat runs, that includes the
current inbound message plus any thread-history, system-event, or transport
message-context blocks. For job runs, that is the job prompt body.

When a run is user-scoped, the username is resolved from transport identity
mapping to the user record, not guessed. That identity is used for user rules
and path-based lookups into user-scoped memory (`memory/users/<name>/`).

Delegated runs should use the same run-envelope prompt assembly path as direct
runs. The child run changes agent identity and input task, but it should not
fork a separate prompt-construction model.

### Context management

Operator enforces a context budget on every turn. The system prompt layers
described above are stable and always present. Conversation history is what gets
pruned — when the total context exceeds the budget, the oldest exchange groups
are dropped until it fits. Recent context is more valuable than complete history,
and anything worth retaining long-term should be captured in memory.

### Workspaces

An agent's workspace is where work happens. This is where the agent reads and
writes files, drafts plans, generates artifacts, collects research, and manages
project-local context.

Workspace files are not the same thing as memory. They are work products and
task context.

Operator should provide a fixed default workspace layout rather than making the
top-level structure agent-specific.

At a minimum, each workspace should include reserved directories such as:

- `inbox/` for inbound files and imported reference material
- `work/` for active working files and intermediate outputs
- `artifacts/` for final deliverables
- `tmp/` for disposable scratch files
- `shared/` for cross-agent file sharing

`shared/` lives at `~/.operator/shared/` and is symlinked into each agent's
workspace so every agent sees the same shared root. By convention, agents read
from any subdirectory and write to their own `shared/<agent>/` area. The base
workspace contract and `SYSTEM.md` should steer agents toward this shared path
for cross-agent exchange.

Inside `shared/`, files are organized into per-agent subdirectories so it is
clear which agent produced what:

```text
shared/
  operator/
  researcher/
```

The runtime ensures these subdirectories exist for every configured agent at
startup. If a new agent is added to `operator.yaml`, its shared directory is
created automatically.

Inbound attachments and imported source files should land in `inbox/` so they
remain available as workspace artifacts instead of living only in transient
conversation context.

The base workspace contract belongs in `SYSTEM.md`, not in a generated
workspace-specific file. That keeps the rules in one global place instead of
duplicating them across agents.

`AGENT.md` may extend the workspace conventions for a specific agent, but it
should not redefine the base top-level workspace structure.

### Workspace sandbox

Agents are sandboxed to their workspace by default. File tools (`read_file`,
`write_file`, `list_files`) reject paths that resolve outside the agent's
workspace directory. This is the safe default — most agents have no reason to
read or write files outside their own workspace, and the workspace layout
provides standard directories for every common use case.

The sandbox is controlled by a single `sandbox` flag on the agent config:

- `sandbox: true` (default) — file tools are confined to the workspace.
- `sandbox: false` — file tools can reach the full filesystem.

`run_shell` is always unrestricted regardless of the sandbox flag. Shell
execution is inherently unconstrained — granting it is the explicit signal
that the operator trusts this agent with full machine access. This is why
`run_shell` should be granted sparingly and only to agents that genuinely
need it.

Tools should not individually restrict their own reach. The two layers of
control are **permissions** (which tools an agent has) and **sandbox**
(whether file tools are confined to the workspace). Tools that are granted
should work fully within their boundary — not be artificially limited in a
way that makes them useless.

The `shared/` symlink lives inside the workspace, so cross-agent file
exchange works naturally within the sandbox. An agent can read from any
agent's shared area and write to its own — no sandbox escape needed.

### Runtime state

Operator has two kinds of non-memory state. They serve different purposes and
live in different places.

#### Database state

High-volume, relational data that the system queries and manages internally
lives in SQLite. Operator is not aiming to support multiple database backends.

This covers:

- conversations and message history
- transport message indexes
- user identities and roles
- job run state
- core runtime bookkeeping

Database state is not user-facing. Users do not inspect or edit it directly.

All timestamps in the database are stored as unix timestamps (REAL columns,
seconds since epoch). This is compact, timezone-agnostic, and supports
efficient range queries without string parsing. Conversion to human-readable
format happens at the display boundary using the user's timezone from their
profile.

Human-facing files — memory frontmatter, job definitions, skill metadata —
use ISO 8601 strings because those are authored or inspected by humans.

The boundary rule: unix floats go into the database, human-readable strings
go into files and display output.

#### Agent state

Small, per-agent operational data lives in file-backed JSON documents in a
reserved `state/` directory within each agent's directory.

This covers:

- cursors
- watermarks
- cooldowns
- counters
- last-processed markers
- history lists (e.g., previously used values to avoid repetition)

Agent state is human-inspectable but not user-facing knowledge. If a human
would reasonably want to understand it as knowledge — a preference, a fact, a
behavioral rule — it belongs in memory, not state.

State values are restricted to scalar types (string, number, boolean) and
ordered lists of scalars. Agents never construct or parse JSON directly. The
tools accept and return typed primitives, and the runtime handles
serialization. This keeps state deterministic and debuggable — each key is a
single inspectable file with a clear type, not an opaque blob the agent
invented.

The state tool surface:

- `get_state` / `set_state` — read and write scalar values
- `append_state` / `pop_state` — push to and consume from ordered lists
- `list_state` / `delete_state` — enumerate and remove keys

### Timezone handling

User timezone is a field on the user record in the database. There is no
system-wide default timezone setting in config.

When a user's timezone is null, the agent's injected context includes a note
instructing it to ask the user for their timezone. Once set, the agent uses the
stored timezone for interpreting and presenting times. Timezone persistence and
display formatting are handled by runtime code and dedicated tools rather than
freehand model date math.

### Jobs

Jobs are scheduled tasks defined as markdown files with YAML frontmatter.
They live at `~/.operator/jobs/<name>/JOB.md`. The frontmatter includes the
schedule (cron expression), the target agent, and optional prerun gates and
postrun hooks. The body is the prompt the agent receives when the job runs.

Jobs run within the target agent's context — they use the agent's permissions,
memory, and workspace.

Job runs are ephemeral. Each run creates a fresh context, executes, and
terminates. There is no persistent conversation to resume between runs.
Anything worth retaining should be written to memory, state, or workspace
files during the run.

The job tool surface:

- `create_job` — explicit fields: name, schedule, prompt, description,
  agent, model, max_iterations, enabled, prerun, postrun
- `update_job` — full replace with the same explicit fields
- `delete_job` / `enable_job` / `disable_job` / `list_jobs`

#### Hooks

Hooks are the mechanism for deterministic, scriptable control over job
execution. They let operators gate whether a job runs and post-process
its output using ordinary shell scripts, keeping that logic out of the
model and in version-controllable code.

Hooks are specified as `prerun` and `postrun` parameters on the job tools,
as paths relative to the job directory. Both are optional. By convention,
hook scripts live at `scripts/prerun.sh` and `scripts/postrun.sh` inside
the job directory. Hook paths must not escape the job directory.

**Prerun hooks** run before the agent. A non-zero exit code gates the job —
the run is skipped and recorded as gated. A zero exit code allows the job
to proceed. Critically, the hook's stdout on success is injected into the
job prompt as `<prerun_output>`. This serves two purposes: the script
decides whether the job should run, and when it does run, it passes in
only the data the job needs. This keeps the model focused on a
pre-filtered input rather than doing its own data gathering.

Prefer prerun scripts for anything deterministic — API calls, data fetching,
date logic, filtering, rate limiting. Reserve the model for interpretation
and formatting. A well-written prerun script reduces LLM token usage and
makes job behavior more predictable.

**Postrun hooks** run after the agent completes. The agent's final text
output is piped to the hook's stdin. A non-zero exit code marks the run
as failed. Postrun hooks are useful for forwarding results, triggering
downstream systems, or validating output.

Hook scripts receive environment variables for context: `JOB_NAME`,
`OPERATOR_AGENT`, `OPERATOR_HOME`, and `OPERATOR_DB`.

Hooks have a configurable timeout. The default is 30 seconds. This can be
overridden in `operator.yaml` under `defaults.hook_timeout` (in seconds).
If a hook exceeds its timeout, the process is killed and the job is
treated as gated (prerun) or failed (postrun).

### Subagents

Subagents are fresh child runs used to offload focused work. If no target agent
is specified, the child run is a fresh run of the current agent. If a target
agent is specified, the child run uses that agent's own `AGENT.md`, memory,
skills, workspace, and configured permissions.

Subagent runs are ephemeral. They return a result to the parent run and are not
treated as durable conversations that can be resumed later. Anything that needs
to survive beyond the child run should be written to files, memory, or state.

Delegation preserves the current execution mode while dropping parent
conversation history:

- chat-triggered child runs keep the current session envelope (user identity,
  transport semantics, current channel/thread bindings) but start with a fresh
  conversation state
- job-triggered child runs stay in job mode, keep job semantics, and do not
  acquire a synthetic user/chat context

This is an agent swap, not a conversation fork. The child run gets a fresh
input task, the target agent's own prompt/capability surface, and the current
run mode's envelope.

Preserving the current envelope does not preserve the parent's capability
surface. Delegation keeps session/job context while switching to the target
agent's own tools, skills, models, workspace, and permissions.

Users may delegate only to agents they can access directly. Once an agent is
selected, it runs with its own configured tool and skill surface. The parent
agent's permissions are not inherited by the child run.

Jobs are the exception to user-level access checks because they do not run with
a user context. A job may delegate to another agent, but the child still runs
with the target agent's configured tool and skill permissions.

### Skills

Skills are reusable capabilities defined as markdown files following the
Agent Skills specification (https://agentskills.io/specification). They live at
`~/.operator/skills/<name>/SKILL.md` and are automatically discovered and
injected into the agent's context.

Skills are not tools. Tools are code that agents execute. Skills are authored
instructions, references, and assets that teach an agent how to perform a
specific task. An agent's available skills are determined by its permissions
configuration.

The skill tool surface:

- `create_skill` — explicit fields: name, description, instructions, env
- `update_skill` — full replace with the same explicit fields
- `delete_skill` / `list_skills`

### Permissions, roles, and sandbox

Operator's security model is restrict by default. Every layer starts closed
and requires explicit grants to open.

- **Users** have roles. Roles determine which agents a user can interact with.
- **Agents** have permissions. Permissions determine which tools and skills
  an agent can use.
- **Sandbox** confines file tools to the workspace by default. Agents that
  need broader filesystem access must be explicitly unsandboxed.

A new agent with no permissions block has access to nothing. A new agent
without a `sandbox` override is confined to its workspace. Forgetting to
configure either should result in less access, not more.

These are two orthogonal layers:

1. **Permissions** control which tools exist in the agent's context. An agent
   that does not have `run_shell` cannot execute shell commands, period.
2. **Sandbox** controls what file tools can reach. When sandboxed, file tools
   are confined to the workspace. When unsandboxed, file tools have full
   filesystem access.

Tools should not individually restrict their own reach beyond sandbox
enforcement. A tool that is granted should work fully within its boundary.
Artificially limiting granted tools makes them useless and pushes agents
toward workarounds.

`run_shell` is always unrestricted regardless of sandbox. Shell execution is
inherently unconstrained — granting it is the explicit signal that the
operator trusts this agent with full machine access.

Permission groups allow clusters of related tools to be referenced by name
using an `@group` prefix (e.g., `@memory`, `@files`). Groups are generated
into `operator.yaml` at init time with sensible defaults. From that point the
user owns them and can modify, split, or extend groups as needed. Individual
tools can still be added alongside groups for granularity.

Built-in tool groups are derived from the central tool registry when the init
scaffold is generated, so a fresh `operator init` picks up newly added built-in
tools automatically. Existing user configs remain user-owned and are not
rewritten on upgrade.

Permissions are enforced at two layers. Only permitted tools and skills are
injected into the agent's context, so the agent never sees what it cannot use.
Additionally, tool calls are checked at runtime and rejected programmatically,
so even if an agent is tricked into calling a tool by name, the call will fail.

The first admin user is created through the CLI after `operator init`, using
`operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>`. From
there, admins can manage access through the CLI or by asking an agent with
user management tools.

## Memory Model

### Memory is required

Long-term memory is a core capability. Agents work across many tasks and
conversations over time and must retain important details. Memories are created
explicitly — either by an agent using dedicated memory tools during a
conversation, or by a human creating files directly. There is no background
harvester or automatic extraction process. The agent decides what is worth
remembering in the moment, and the human can always create, edit, or remove
memory files by hand. This keeps memory intentional and predictable.

### Files are the source of truth

Memory content lives in file-backed markdown. The retrieval layer uses a
SQLite FTS5 index derived from those files, but files remain authoritative
and human-editable. The index is disposable and rebuildable at any time
via `operator memory index`.

This keeps memory human-readable, editable by hand, easy to debug,
transport-agnostic, and version-controllable.

### Embeddings are optional

Vector embeddings can be configured for semantic search but are not required.
When configured, embeddings are computed on write and stored alongside the
FTS5 index. When not configured, search uses FTS5 with Porter stemming — no
external model dependency needed.

The index database lives at `~/.operator/db/memory_index.db`, separate from
the main `operator.db`, so it can be safely deleted and rebuilt without
affecting conversations, users, or other runtime state.

### Rules and notes

Operator uses a single file-backed memory system with two behaviors:

- `rules/` are always injected into the prompt. They should remain short,
  curated, and high-signal. Rules are standing instructions. If something does
  not need to shape behavior every time, it should not be a rule.
- `notes/` are searched on demand. They hold durable knowledge that should not
  automatically bloat the prompt on every turn. Time-bound knowledge belongs in
  notes rather than rules.

Rule examples:

- "Prefer concise answers unless extra depth is requested."
- "Use `uv` rather than `pip` unless the user explicitly asks otherwise."
- "Never run destructive git commands without explicit confirmation."

Note examples:

- "Release date moved to April 3."
- "The staging API base URL is ..."
- "User lives in the Vancouver timezone."

### File layout and identity

Memory is stored as one file per memory item. The directory layout is shown in
the main directory tree above. Every memory item has its own path, making
update, delete, and expiry deterministic.

The file system is the schema. Memory files do not need an internal `id`
field — the relative path is the identity. Scope comes from the path location.
Behavior comes from whether the file lives under `rules/` or `notes/`. This
keeps the files self-organizing without duplicating metadata in frontmatter.

Examples:

```text
users/gavin/rules/concise-answers.md
agents/operator/notes/release-process.md
```

Agent-facing tools should not expose raw file paths when a more deterministic
domain shape exists. For memory, agents should work in terms of scope, kind,
and a short stable key. The runtime maps that key to the underlying file path.

### Minimal frontmatter

Memory frontmatter should stay minimal and only include data that changes
behavior, lifecycle, or debugging:

```md
---
created_at: 2026-03-11T10:15:00Z
updated_at: 2026-03-11T10:15:00Z
expires_at: 2026-04-03T00:00:00Z
---

Release date moved to April 3.
```

- `created_at` is kept for audit and debugging
- `updated_at` is kept for freshness and deterministic sweeps
- `expires_at` is optional and supports short-lived memory

### Notes search

Notes are searched using SQLite FTS5 with Porter stemming and BM25 relevance
ranking. The FTS5 index is derived from the memory files on disk and is
rebuilt automatically on startup (hash-diff — only changed files are
reindexed). The `operator memory index` CLI command triggers a manual
reindex after human edits to memory files.

When vector embeddings are configured, search results are fused using
Reciprocal Rank Fusion across FTS5 and vector similarity. This provides
semantic search — "deployment schedule" can find notes about "release
cadence" — without making embeddings a hard requirement.

Determinism at write time still matters. The memory tools enforce descriptive
filenames that make FTS5 search effective even without embeddings.

### Memory lifecycle

Some memory is only useful for a limited time — dates, temporary priorities,
time-bound instructions. Note tools accept a relative `ttl` (e.g., `"3d"`,
`"2w"`) and compute an absolute `expires_at` timestamp deterministically.
Rules do not expire. The model never does date math directly.

When a memory item expires, it is moved to `trash/` rather than hard deleted.
Agents never read `trash/`, but users can inspect it. Expiry takes effect at
read time. This preserves debuggability while keeping active memory clean.

### Memory tools

Agents should not manage memory through generic file writing. They should use
dedicated memory tools that enforce the directory layout and lifecycle rules.

The memory tool layer supports:

- saving rule memory by deterministic key
- saving note memory by deterministic key
- searching notes
- listing rules and notes
- forgetting a memory item by deterministic key
- sweeping expired memory into `trash/`

The tools may hide the file details from the agent, but the files remain the
source of truth for humans.
