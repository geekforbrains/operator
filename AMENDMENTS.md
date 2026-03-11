# Principles Amendments

Agreed changes from design review on 2026-03-11. Each item should be
merged into PRINCIPLES.md once implemented or finalized.

## 1. Skill and agent discovery

Skills and agents are discovered by scanning the filesystem at the start
of each agent run (each inbound message or job trigger). There is no hot
reload mid-turn and no file watchers. A newly created skill or agent is
available on the next request, not during the current one. Caching may
be added later if the scan becomes a performance concern.

## 2. Subagent permissions: user role, not parent agent

Effective permissions for a subagent are the intersection of the calling
user's role and the target agent's configured permissions. The parent
agent's permissions are not a factor. This means a user with broad
access can delegate through a restricted agent to a more capable one,
as long as their role permits it. The agent acts on behalf of the user,
not on its own authority.

## 3. Agent injection: annotate access, don't filter

All configured agents are injected into the prompt regardless of the
current user's access. Agents the user cannot access are annotated as
inaccessible. This lets the agent explain why it cannot delegate to a
particular agent rather than being unaware of its existence. The agent
is educated on the user's restrictions and can reject requests with
context.

## 4. Permission groups

Permissions support named groups that map to clusters of related tools
(e.g., `memory`, `files`, `messaging`, `jobs`). Groups are generated
into the config at init time with sensible defaults. From that point the
user owns them and can modify, split, or extend groups as needed.
Individual tools can still be added alongside groups for granularity.

When new tools are introduced in a future version, they are not
automatically added to any group. The user adds them manually. The CLI
provides a command to list all available tools so the user can see what
is new.

## 5. Timezone handling

All internal timestamps are stored in UTC. User timezone is a field on
the user record in the database. There is no system-wide default
timezone setting in config.

When a user's timezone is null, the agent's injected context includes a
note instructing it to ask the user for their timezone. Once set, the
agent uses the stored timezone for interpreting and presenting times.
All timezone conversion is handled by tools — the model never does
timezone math directly.

## 6. Username in agent context

The current user's username is injected into the agent's context on
every turn alongside their role and timezone. Agents use the username
for path-based lookups into user-scoped memory
(`memory/users/<name>/`). The username is resolved from transport
identity mapping to the user record, not guessed.
