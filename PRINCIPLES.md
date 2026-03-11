# Operator Principles

This document captures the intended architecture and product stance for Operator.
It exists for two audiences:

- contributors, so development stays aligned on what belongs in the system and why
- users, so the high-level behavior of the system is understandable and predictable

If the current implementation differs from this document, treat this document as
the preferred direction for future development.

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
4. The agent replies through the transport.
5. The system persists the right state so the agent can continue later.

Everything in Operator should justify itself against that loop.

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
    <name>.md
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
```

Individual sections below explain each part in detail.

### Agent definitions

Agents are defined in markdown with YAML frontmatter at
`~/.operator/agents/<name>/AGENT.md`. The frontmatter includes a `name` and
`description`. The body is the stable, human-authored definition of the agent's
role, mission, capabilities, and hard constraints.

All configured agents are automatically discovered and injected into the system
prompt with their name and description so that every agent is aware of every
other agent. This enables delegation without requiring agents to be explicitly
told about each other.

`AGENT.md` should change when the agent's charter changes. It should not be
rewritten casually in response to routine user feedback.

### Prompt assembly

Operator has a shared global `SYSTEM.md` so common operating instructions do not
need to be duplicated across every agent.

At a high level, the intended prompt order is:

1. `SYSTEM.md`
2. `AGENT.md`
3. available tools
4. discovered skills
5. known agents
6. transport-specific prompt content
7. global rules
8. agent rules
9. user rules, when the interaction is private and user-scoped rules apply

This order is intentional.

`SYSTEM.md` goes first because it defines the universal operating contract for
all agents.

`AGENT.md` comes next because it defines the specific agent's charter and role.

Available tools and discovered skills follow because they define what the agent
can do. Tools are listed so the agent knows its capabilities. Skills are
automatically discovered and injected so the agent knows what authored
instructions are available to it. Both are scoped by the agent's permissions.

Known agents come next so the agent is aware of other agents it can delegate
to. Each agent is listed with its name and description.

Transport-specific prompt content comes after that because it explains the
environment the agent is operating in. This includes platform semantics and
relevant transport context such as channel identifiers, user identifiers,
threading behavior, and any other transport-specific details the agent may need
for efficient tool use.

Rules then follow in broad-to-narrow order:

- global rules
- agent rules
- user rules

This lets the system layer broad shared behavior first, then refine it with
agent-specific behavior, then refine it again with user-specific behavior when
appropriate. The most specific rules appear last.

Transient context such as the current conversation state, thread snapshots,
retrieved notes, or other per-request context may be added after this base
instruction stack. Those are request context, not standing prompt layers.

### Context management

Operator prunes conversation context continuously rather than letting it grow
until it must be compacted.

Many systems allow conversations to fill the context window and then
auto-compact, which is slow and loses significant detail. In practice, old
context is rarely still relevant. By pruning progressively, Operator keeps the
active conversation sharp, focused, and within the model's context window so
it does not degrade into hallucination.

The system prompt layers described above are stable and always present. What
gets pruned is conversation history — older turns are dropped as newer turns
arrive. This is a deliberate tradeoff: recent context is more valuable than
complete history, and anything worth retaining long-term should be captured in
memory, not preserved by keeping old messages around.

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
workspace so every agent sees the same shared root. Agents read from any
subdirectory and write to their own. This gives every agent access to shared
files without reaching into each other's workspaces directly.

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

The base workspace contract belongs in `SYSTEM.md`, not in a generated
workspace-specific file. That keeps the rules in one global place instead of
duplicating them across agents.

`AGENT.md` may extend the workspace conventions for a specific agent, but it
should not redefine the base top-level workspace structure.

### Storage boundaries

Operator draws a firm line between different kinds of stored information:

- `SYSTEM.md` and `AGENT.md` hold authored standing instructions
- memory files hold learned reusable knowledge
- workspace files hold work products and task-local context
- conversation history in SQLite holds episodic record
- state files hold small operational state

These boundaries matter. If something is user-facing knowledge, it should not
be hidden inside state. If something is machine bookkeeping, it should not be
promoted into long-term memory files.

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

#### Agent state

Small, per-agent operational data lives in file-backed state documents in a
reserved `state/` directory within each agent's directory. This replaces the
older key-value store concept.

This covers:

- cursors
- watermarks
- cooldowns
- counters
- last-processed markers

Agent state is human-inspectable but not user-facing knowledge. If a human
would reasonably want to understand it as knowledge — a preference, a fact, a
behavioral rule — it belongs in memory, not state.

Agents should not manage state through arbitrary file writes. Operator exposes
dedicated state tools that read and write structured state documents in the
reserved state area. This keeps state deterministic and debuggable without
reintroducing a generic key-value concept.

### Jobs

Jobs are scheduled tasks defined as markdown files with YAML frontmatter,
following the same pattern as agents and skills. They live at
`~/.operator/jobs/<name>.md`. The frontmatter includes the schedule (cron
expression), the target agent, and optional prerun gates and postrun hooks.
The body is the prompt the agent receives when the job runs.

Jobs run within the target agent's context — they use the agent's permissions,
memory, and workspace.

### Subagents

Subagents are a way to delegate focused work to another agent. They reuse the
same basic model: standing instructions, scoped memory, workspace access, tool
use, and persisted state.

When a subagent is spawned, it receives its own identity — its own `AGENT.md`,
memory, and skills. It does not inherit the parent agent's context or memory
scope.

However, a subagent's permissions cannot exceed those of the agent that spawned
it. The effective permissions are the intersection of the subagent's own
permissions and the calling agent's permissions. This prevents privilege
escalation through delegation — a restricted agent cannot gain broader access
by spawning a less restricted one.

### Skills

Skills are reusable capabilities defined as markdown files following the
Agent Skills specification (https://agentskills.io/specification). They live at
`~/.operator/skills/<name>/SKILL.md` and are automatically discovered and
injected into the agent's context.

Skills are not tools. Tools are code that agents execute. Skills are authored
instructions, references, and assets that teach an agent how to perform a
specific task. An agent's available skills are determined by its permissions
configuration.

### Permissions and roles

Access in Operator is closed by default. Agents, tools, and skills are not
available to users unless explicitly granted.

- Users have roles. Roles determine which agents a user can interact with.
- Agents have permissions. Permissions determine which tools and skills an
  agent can use.
- A new agent with no permissions block has access to nothing. Access must be
  explicitly opened.

Permissions are enforced at two layers. Only permitted tools and skills are
injected into the agent's context, so the agent never sees what it cannot use.
Additionally, tool calls are checked at runtime and rejected programmatically,
so even if an agent is tricked into calling a tool by name, the call will fail.

This is an allowlist model, not a denylist. In a team-facing system, the safe
default is locked down. Forgetting to configure permissions should result in
less access, not more.

The first admin user is created through the CLI during setup. From there,
admins can manage access through the CLI or by asking an agent with user
management tools.

## Memory Model

### Memory is required

Long-term memory is a core capability. Agents work across many tasks and
conversations over time and must retain important details.

### Embeddings are not required

Operator's long-term memory model does not depend on embeddings or vector
search. The memory source of truth is file-backed markdown.

This keeps memory:

- human-readable
- editable by hand
- easy to debug
- transport-agnostic
- version-controllable
- free from an extra mandatory model dependency

### Rules and notes

Operator uses a single file-backed memory system with two behaviors:

- `rules/` are always injected into the prompt. They should remain short,
  curated, and high-signal. If a rule does not need to shape behavior every
  time, it should not be a rule.
- `notes/` are searched on demand. They hold durable knowledge that should not
  automatically bloat the prompt on every turn.

This replaces older ideas such as `pinned`, `candidate`, and `durable`. There
is no separate pinned-memory concept.

Rule examples:

- "Prefer concise answers unless extra depth is requested."
- "Use `uv` rather than `pip` unless the user explicitly asks otherwise."
- "Never run destructive git commands without explicit confirmation."

Note examples:

- "Release date moved to April 3."
- "The staging API base URL is ..."
- "User lives in the Vancouver timezone."

### Memory file layout

Memory is stored as one file per memory item.

Example layout:

```text
~/.operator/
  memory/
    global/
      rules/
      notes/
      trash/
    users/
      gavin/
        rules/
        notes/
        trash/
  agents/
    operator/
      AGENT.md
      memory/
        rules/
        notes/
        trash/
```

This design makes active always-injected memory obvious to a human:
everything in `rules/` is active rule memory.

It also makes update, delete, expiry, and debugging deterministic because every
memory item has its own path.

### Path is identity

Memory files do not need an internal `id` field. In a file-backed system, the
relative path is the memory reference.

Examples:

```text
users/gavin/rules/concise-answers.md
agents/operator/notes/release-process.md
```

That is readable by humans and stable enough for dedicated memory tools to use
for update and delete operations.

### Scope and behavior come from the path

The concepts of scope and type are necessary, but they do not need to be stored
as frontmatter on every file.

They are derived from the directory structure:

- scope comes from the path location
- behavior comes from whether the file lives under `rules/` or `notes/`

This keeps the files self-organizing without duplicating metadata.

### Minimal frontmatter

Memory frontmatter should stay minimal and only include data that changes
behavior, lifecycle, or debugging.

The default shape is:

```md
---
created_at: 2026-03-11T10:15:00Z
updated_at: 2026-03-11T10:15:00Z
expires_at: 2026-04-03T00:00:00Z
---

Release date moved to April 3.
```

Guidelines:

- `created_at` is kept for audit and debugging
- `updated_at` is kept for freshness and deterministic sweeps
- `expires_at` is optional and supports short-lived memory

The following fields are intentionally omitted unless a future need proves they
are necessary:

- `id`
- `scope`
- `kind`
- `source`

### Notes search

Notes are searched by filename and content using text-based tools. There is no
semantic layer — search is substring and pattern matching across the notes
directory.

This works because the write-time memory tools enforce descriptive filenames.
Determinism comes from the point of creation, not retrieval. If the tool names
files well, search does not need to be smart.

Operator expects efficient search tools such as ripgrep to be available on the
host, but falls back to standard system utilities when they are not. This
improves performance under ideal conditions without creating a hard dependency.

### Memory creation

Memories are created explicitly — either by an agent using dedicated memory
tools during a conversation, or by a human creating files directly.

There is no background harvester or automatic extraction process. The agent
decides what is worth remembering in the moment, and the human can always
create, edit, or remove memory files by hand. This keeps memory intentional
and predictable rather than dependent on a background LLM process guessing
what matters.

### Memory lifecycle

#### Short-lived memory

Some memory is only useful for a limited time. Examples include dates, temporary
priorities, time-bound instructions, and short-term facts.

Memory tools accept a `ttl` parameter — a human-friendly duration such as
`"3d"`, `"2w"`, or `"1h"`. The tool converts this to an absolute `expires_at`
timestamp on disk. The `expires_at` field is never exposed as a tool parameter.

This is intentional. Language models are unreliable at date math. Asking a model
to compute "three days from now" as an ISO timestamp invites silent errors —
wrong dates, wrong timezones, off-by-one days. By accepting only a relative
duration, the tool keeps the model's job simple ("how long should this last?")
and lets deterministic code handle the arithmetic. The persisted `expires_at`
is an absolute timestamp because it is easier for humans to inspect and easier
for code to query deterministically.

#### Trash instead of hard delete

When a memory item expires, it should be moved to `trash/` rather than being
deleted immediately.

Principles:

- agents never read `trash/`
- users can inspect `trash/`
- expiry is deterministic
- memory remains debuggable

This preserves the advantages of a file-backed system while keeping active
memory clean.

### Memory tools

Agents should not manage memory through generic file writing. They should use
dedicated memory tools that enforce the directory layout and lifecycle rules.

At a high level, the memory tool layer should support:

- creating rule memory
- creating note memory
- searching notes
- listing rules and notes
- updating a memory item by path
- forgetting a memory item
- sweeping expired memory into `trash/`

The tools may hide the file details from the agent, but the files remain the
source of truth for humans.

## Where Feedback Goes

### Update `AGENT.md` when the charter changes

If a human intentionally changes the agent's role, mission, or hard constraints,
that belongs in `AGENT.md`.

### Update `rules/` when reusable behavior changes

If feedback should shape future behavior in similar situations, it belongs in
rule memory.

Examples:

- "Be more concise with me."
- "Always prioritize bug risks over style comments in reviews."

### Update `notes/` when durable knowledge should be retained

If something is a fact, reference point, or non-always-injected preference, it
belongs in note memory.

Examples:

- "Release date moved to April 3."
- "The user is traveling this week."

### Keep one-off instructions in the conversation

Not every message becomes memory. Thread-local instructions and one-off task
constraints should stay in the conversation history unless they become clearly
reusable.

## Consequences of This Design

### Benefits

- users can inspect and edit memory, state, and agent definitions directly
- agents use deterministic tools rather than inventing file structures
- memory remains understandable without a vector database or background workers
- behavior is easier to reason about because `rules/` means always injected and
  `notes/` means searched
- closed-by-default permissions prevent accidental over-exposure
- subagent permission scoping prevents privilege escalation
- continuous context pruning keeps conversations sharp without lossy compaction
- the entire system is debuggable by reading files on disk

### Tradeoffs

- The system gives up fuzzy semantic recall from embeddings. Operator prefers
  transparency and inspectability over flexible but harder-to-debug retrieval.
  If retrieval needs to improve later, indexing may evolve internally, but the
  file-backed model remains the source of truth.
- Without a background harvester, agents must be taught to use memory tools
  proactively. Memory quality depends on tool use, not automatic extraction.
- Closed-by-default permissions add setup friction. This is intentional —
  the cost of forgetting to restrict is higher than the cost of forgetting to
  grant.

## Summary

Operator is a markdown-defined, team-facing agent runtime where:

- agents, jobs, and skills are defined as markdown files with frontmatter
- agents auto-discover each other for delegation
- skills follow the Agent Skills specification and are injected automatically
- work happens in structured workspaces with a fixed default layout
- cross-agent file sharing goes through `shared/`, not direct workspace access
- long-term memory is file-backed with no embeddings or background harvesting
- memory is created explicitly by agents or humans, not extracted automatically
- `rules/` are always injected, `notes/` are searched on demand
- determinism comes from write-time tooling — good filenames make search simple
- expired memory moves to `trash/` instead of being deleted
- agent state is file-backed and separate from memory and workspace
- database state covers high-volume runtime data in SQLite
- access is closed by default — permissions are an allowlist, not a denylist
- subagents cannot exceed the permissions of the agent that spawned them
- context is pruned continuously, not compacted after overflow
- transports are generic interaction adapters, not the architecture itself
