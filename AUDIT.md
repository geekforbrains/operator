# Principles Audit

This file tracks the ongoing section-by-section review of `PRINCIPLES.md`
against the current implementation. Review one section at a time, record the
status, and note only the concrete changes made to bring code and principles
back into alignment.

## Latest Changes

- `Memory Model`: complete
- Replaced path-based agent memory tools with deterministic key-based
  `save_rule`, `save_note`, `forget_rule`, and `forget_note` operations so
  agents work in terms of scope and stable keys instead of filesystem paths.
- Enforced private-conversation gating for all user-scoped memory operations,
  including reads and listing, rather than only gating writes.
- Made expiry take effect at read time so expired rules stop injecting and
  expired notes stop appearing in list/search immediately; the sweeper now acts
  only as cleanup into `trash/`.
- Clarified in `PRINCIPLES.md` that rules are standing instructions, TTL applies
  only to notes, and agent-facing tools should prefer deterministic domain
  operations over raw paths when possible.
- Removed the dead embedding/harvester/cleaner memory config surface and stale
  test references so the schema reflects the current explicit file-backed
  memory model.
- `High-Level System Model`: complete
- Aligned `workspace/shared` on the shared root, created per-agent shared
  subdirectories at layout time, and updated workspace guidance to treat
  `shared/<agent>/` as the convention for cross-agent exchange.
- Moved persisted attachment imports into `inbox/` so inbound files remain
  available as workspace artifacts instead of living only in message context.
- Removed the stray top-level scaffolded `state/` directory so runtime state
  lives only in `db/` and per-agent `state/` directories.
- Removed the runtime-only role gate for `manage_users` so agents run with
  their configured toolsets once a user can access that agent.
- Added a self-service `set_timezone` tool and clarified timezone handling so
  persistence and formatting are runtime/tool responsibilities.
- Clarified subagents as ephemeral child runs, with omitted `agent` meaning a
  fresh run of the current agent and explicit `agent` selecting another agent's
  own identity and tool surface.
- `Design Goals`: complete
- Refactored transport config into a generic `type` plus validated `env` and
  `settings` sections, so config loading and runtime transport creation keep
  secrets, non-secret transport behavior, and transport type distinct.
- Added regression coverage for inline transport config, explicit
  transport env/settings config, starter-config generation, and runtime
  transport creation.
- Updated bundled prompt/skill guidance and runtime status labels to use the
  current state tool names instead of removed KV/read-write terminology.
- `Core Use Case`: complete
- Removed the sandbox concept from runtime config, subagent handoff plumbing,
  and related docs/tests.
- Clarified in `PRINCIPLES.md` that agents should have the smallest useful tool
  and skill surface, with shell access granted sparingly.
- Updated the core loop wording so agents return results to the user or
  destination when needed, which better matches scheduled jobs.
