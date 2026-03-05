# SPEC.md Implementation Tasks

Ordered by dependency. Each task should be a single, reviewable unit of work.

---

## Task 1: Flatten permissions config models ✅

Replace the nested `ToolPermissions`/`SkillPermissions` classes with flat values on `PermissionsConfig`. Add `RoleConfig`, update `SettingsConfig`, and add `roles` to `Config`.

**SPEC sections:** 4, 13

**Files:**
- `config.py` — Replace `ToolPermissions` and `SkillPermissions` with flat `PermissionsConfig` (`tools: list[str] | Literal["*"] | None`, same for `skills`). Add `RoleConfig(agents: list[str])`. Add `reject_response: Literal["announce", "ignore"] = "ignore"` to `SettingsConfig`. Add `roles: dict[str, RoleConfig]` to `Config`. Add validator: `admin` as a key in `roles` is a config error.
- `config.py` — Simplify `agent_tool_filter()` and `agent_skill_filter()` to match new flat structure. `None` or `"*"` = unrestricted, `[list]` = set-membership check. No more `allow`/`deny` branches.

**Notes:** No backward compatibility with old allow/deny format. Clean break per SPEC section 14.

---

## Task 2: Store — user/identity/role tables and functions ✅

Add the three user-related tables and all store functions for user management.

**SPEC sections:** 1, 12

**Files:**
- `store.py` — Add `users`, `user_identities`, `user_roles` tables in `_init_db()`. Add `User` dataclass. Add functions: `add_user`, `remove_user`, `get_user`, `list_users`, `add_identity`, `remove_identity`, `resolve_username`, `add_role`, `remove_role`, `get_user_roles`.

**Notes:** `resolve_username(platform_id) -> str | None` is the hot-path auth lookup — single index lookup on `user_identities.platform_id`. Username validation: lowercase alphanumeric, dots, hyphens, 1-64 chars.

---

## Task 3: UserContext and context vars ✅

Create the context variable infrastructure for user identity and skill filtering at tool execution time.

**SPEC sections:** 6 (UserContext), 5 (skill filter context)

**Files:**
- `tools/context.py` — New file. `UserContext` dataclass (`username: str`, `roles: list[str]`). Context var `_user_var`. Getter/setter functions. Skill filter context var for runtime skill permission checking.

**Notes:** `get_user_context()` must handle `LookupError` gracefully — returns `None` when not set (job runs). This is the same `contextvars` pattern as `tools/workspace.py`.

---

## Task 4: Auth flow in dispatcher ✅

Insert authentication check between receiving a message and routing to the agent. Every inbound message is checked — no toggle.

**SPEC sections:** 3

**Files:**
- `main.py` — In `Dispatcher.handle_message()`, after resolving the transport, call `store.resolve_username(msg.user_id)`. If no username, handle rejection. Resolve roles, compute allowed agents (union of all role agent lists; admin = all). If target agent not in allowed agents, reject. Set `UserContext` before proceeding. Handle `reject_response` setting (`announce` sends a reply, `ignore` silently drops).
- `main.py` — On startup, if no users exist in DB, log a warning: `"No users configured. Run: operator user add <username> --role admin <transport> <id>"`

**Notes:** Auth applies to inbound transport messages only. Jobs bypass auth entirely (no user context). The `_conversation_memory_scopes` and memory tool configure calls need to receive `username` instead of `msg.user_id` — see Task 8.

---

## Task 5: MessageContext username and system prompt update ✅

Add the username to the system prompt context block so the agent knows who it's talking to.

**SPEC sections:** 10

**Files:**
- `transport/base.py` — Add `username: str = ""` field to `MessageContext`. Update `to_prompt()` to show `User: gavin (Gavin Vickery via slack)` format when username is set, falling back to current format otherwise.
- `main.py` — After auth lookup resolves `platform_id -> username`, set `ctx.username = username` before passing to `_build_system_prompt`.

---

## Task 6: Memory scopes use username ✅

Switch memory scope user_id from platform-specific ID to stable username.

**SPEC sections:** 9

**Files:**
- `main.py` — In `_run_conversation`, pass `username` (from auth lookup) instead of `msg.user_id` to `_conversation_memory_scopes()` and `memory_tools.configure()`. The `user_id` key in the memory config dict now holds the username value.
- `main.py` — In `_build_system_prompt`, pass `username` to `_conversation_memory_scopes()`.

**Notes:** The key name `user_id` in the memory tools context dict stays for minimal churn — only the value changes. Job runs don't set UserContext, so memory tools need to handle missing user scope (already handled by existing `user_id=""` fallback).

---

## Task 7: Role-gated tool enforcement ✅

Add role-based gating to tool execution in the agent loop.

**SPEC sections:** 6

**Files:**
- `tools/context.py` — Add `ROLE_GATED_TOOLS: dict[str, str]` constant (initially `{"manage_users": "admin"}`).
- `agent.py` — Before executing a tool function, check if the tool name is in `ROLE_GATED_TOOLS`. If yes, read `UserContext`. If user doesn't have the required role, return error: `[error: this tool requires the 'admin' role]`. If `UserContext` is not set (job run), also return error.

**Notes:** The tool stays in the LLM's tool list — only execution is blocked for unauthorized users.

---

## Task 8: Skill access tools (read_skill, run_skill) ✅

Two new built-in tools for agents to interact with skills without needing `run_shell`.

**SPEC sections:** 5

**Files:**
- `tools/skills_access.py` — New file. `read_skill(skill, path="")`: returns SKILL.md or a specific file within the skill dir. Path traversal blocked (no `..`). `run_skill(skill, command, timeout=120)`: `shlex.split(command)`, `subprocess` with `shell=False`, cwd = agent workspace. Path expansion for `scripts/`, `references/`, `assets/` prefixed args. Env: inherit process env, set `SKILL_DIR`, strip transport-config env vars and `OPERATOR_*` vars. Output capped at 16KB.
- `tools/skills_access.py` — Both tools check skill name against agent's skill permissions at runtime (read from skill filter context var). Return `[error: skill 'X' is not available to this agent]` on violation.
- `tools/__init__.py` — Register the new module.

**Notes:** `run_skill` uses `shell=False` — this is the security boundary. No command filtering needed. See SPEC section 5 for detailed execution semantics and path expansion rules.

---

## Task 9: manage_users tool ✅

Chat-based user management for admins.

**SPEC sections:** 7

**Files:**
- `tools/users.py` — New file. `manage_users(action, username="", role="", transport="", external_id="")`. Actions: `list`, `add`, `remove`, `link`, `unlink`, `add_role`, `remove_role`. Uses store functions from Task 2.
- `tools/__init__.py` — Register the new module.

**Notes:** Gated by `admin` role via `ROLE_GATED_TOOLS` (Task 7). Validate username format on `add`. `add` creates user + assigns role (no identity — use `link`). `remove` cascades.

---

## Task 10: Sub-agent UserContext inheritance ✅

Pass UserContext through to sub-agents so role-gated tools work consistently.

**SPEC sections:** 6 (sub-agents inherit user context)

**Files:**
- `tools/subagent.py` — In `spawn_agent`, read current `UserContext` and ensure it's propagated into the child's context. Since `asyncio.create_task` with `contextvars.copy_context()` already copies context vars, this should work automatically — verify and add a test.

**Notes:** The `copy_context()` call already exists. Verify that `UserContext` set before the parent's `run_agent` call is visible in the child task.

---

## Task 11: CLI — operator user ✅

Command-line user management for bootstrap and administration.

**SPEC sections:** 8

**Files:**
- `cli.py` — Add `user_app = typer.Typer()` and register as `app.add_typer(user_app, name="user")`. Commands: `add` (with `--role`, positional `transport` and `external_id`), `remove`, `link`, `unlink`, `list`, `info`, `add-role`, `remove-role`. Uses store functions from Task 2.

**Notes:** `--role` is required on `add`. `add` creates user + assigns role + links identity in one command. Warn if role is not `admin` and not in config.

---

## Task 12: CLI — operator tools ✅

List registered built-in tools to help users configure permissions.

**SPEC sections:** 8

**Files:**
- `cli.py` — Add `operator tools` command. Reads from `tools/registry.py:get_tools()`. Table output: tool name and description. Append note: "Transports may provide additional tools at runtime."

**Notes:** Must import tools to trigger registration (same as `main.py` does). Shows built-in tools only.

---

## Task 13: operator init updates ✅

Update the starter config scaffold and post-init instructions.

**SPEC sections:** 15

**Files:**
- `cli.py` — Update `_STARTER_CONFIG` to include `roles:` block with empty `guest` role and `settings:` block with `reject_response: ignore`. Update post-init console output to remind user: `"Add yourself: operator user add <username> --role admin <transport> <id>"`
