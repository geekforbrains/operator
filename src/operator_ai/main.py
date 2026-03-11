from __future__ import annotations

import asyncio
import base64
import collections
import fcntl
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

# Import tools to trigger registration
import operator_ai.tools  # noqa: F401
from operator_ai.agent import run_agent
from operator_ai.commands import CommandContext, dispatch_command
from operator_ai.config import OPERATOR_DIR, Config, ConfigError, RoleConfig, load_config
from operator_ai.jobs import JobRunner
from operator_ai.layout import ensure_layout
from operator_ai.log_context import new_run_id, set_run_context, setup_logging
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.messages import trim_incomplete_tool_turns
from operator_ai.prompts import assemble_system_prompt
from operator_ai.skills import install_bundled_skills
from operator_ai.status import StatusIndicator
from operator_ai.store import Store, get_store
from operator_ai.tools import memory as memory_tools
from operator_ai.tools import messaging
from operator_ai.tools import state as state_tools
from operator_ai.tools.context import (
    UserContext,
    get_user_context,
    set_skill_filter,
    set_user_context,
)
from operator_ai.tools.web import close_session
from operator_ai.transport.base import Attachment, IncomingMessage, MessageContext, Transport
from operator_ai.transport.registry import create_transport, transport_logger_names

logger = logging.getLogger("operator")

LOGS_DIR = OPERATOR_DIR / "logs"


def _format_tokens(n: int) -> str:
    if n >= 1000:
        v = n / 1000
        return f"{v:.0f}k" if v == int(v) else f"{v:.1f}k"
    return str(n)


def _format_usage(usage: dict[str, int]) -> str:
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    cached = usage.get("cache_read_input_tokens", 0)
    created = usage.get("cache_creation_input_tokens", 0)
    parts = [
        f"In {_format_tokens(prompt)}",
        f"Out {_format_tokens(completion)}",
        f"Cached {_format_tokens(cached)}",
    ]
    if created:
        parts.append(f"Written {_format_tokens(created)}")
    return "Usage: " + " / ".join(parts)


class AgentCancelledError(Exception):
    pass


def resolve_allowed_agents(
    roles: list[str], config_roles: dict[str, RoleConfig]
) -> set[str] | None:
    """Return the set of agent names a user may access, or None if admin (all access)."""
    if "admin" in roles:
        return None
    allowed: set[str] = set()
    for role in roles:
        role_cfg = config_roles.get(role)
        if role_cfg:
            allowed.update(role_cfg.agents)
    return allowed


_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_INLINE_SIZE = 5 * 1024 * 1024  # 5 MB — larger images saved to disk instead
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB — skip oversized files


async def process_attachments(
    attachments: list[Attachment],
    transport: Transport,
    workspace: Path,
) -> list[dict]:
    """Download attachments and return multimodal content blocks.

    Images are inlined as base64 image_url blocks.
    Other files are saved to workspace/uploads/ and described in a text block.
    """
    blocks: list[dict] = []
    uploads_dir = workspace / "uploads"

    for att in attachments:
        if att.size > MAX_DOWNLOAD_SIZE:
            blocks.append(
                {"type": "text", "text": f"[skipped: {att.filename} too large ({att.size} bytes)]"}
            )
            continue

        try:
            data = await transport.download_file(att)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to download attachment %s", att.filename, exc_info=True)
            blocks.append({"type": "text", "text": f"[failed to download: {att.filename}]"})
            continue

        if att.content_type in _IMAGE_TYPES and len(data) <= MAX_INLINE_SIZE:
            b64 = base64.b64encode(data).decode()
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
                }
            )
        else:
            # Save to workspace/uploads/ so the agent can access it
            uploads_dir.mkdir(parents=True, exist_ok=True)
            safe_name = Path(att.filename).name or "unnamed"
            dest = uploads_dir / safe_name
            # Avoid overwriting — append suffix if needed
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                counter = 1
                while dest.exists():
                    dest = uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            dest.write_bytes(data)
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"[file saved: uploads/{dest.name} ({att.content_type}, {len(data)} bytes)]"
                    ),
                }
            )

    return blocks


class ConversationRuntime:
    def __init__(self) -> None:
        self._active = False
        self.cancelled = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._active

    def try_claim(self) -> bool:
        """Atomically check and mark as active.

        Because asyncio is single-threaded and this method contains no
        ``await``, the check-and-set is atomic — no other task can
        interleave between reading and writing ``_active``.
        """
        logger.debug("try_claim runtime=%s active=%s", id(self), self._active)
        if self._active:
            return False
        self._active = True
        return True

    def release(self) -> None:
        logger.debug("release runtime=%s", id(self))
        self._active = False
        self._task = None
        # Clear stale stop state so the next request in this thread starts cleanly.
        self.cancelled.clear()

    def attach_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def cancel(self) -> None:
        self.cancelled.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            self.cancelled.clear()
            raise AgentCancelledError()


class RuntimeManager:
    _MAX_RUNTIMES = 256

    def __init__(self) -> None:
        self._runtimes: collections.OrderedDict[str, ConversationRuntime] = (
            collections.OrderedDict()
        )

    def get_or_create(self, conversation_id: str) -> ConversationRuntime:
        runtime = self._runtimes.get(conversation_id)
        if runtime is not None:
            self._runtimes.move_to_end(conversation_id)
            return runtime
        runtime = ConversationRuntime()
        self._runtimes[conversation_id] = runtime
        while len(self._runtimes) > self._MAX_RUNTIMES:
            self._evict_one()
        return runtime

    def _evict_one(self) -> None:
        """Evict the oldest non-busy runtime, falling back to oldest if all are busy."""
        # Try to find the oldest idle runtime
        for cid, rt in self._runtimes.items():
            if not rt.busy:
                del self._runtimes[cid]
                return
        # All runtimes are busy — evict the oldest anyway to stay bounded
        evicted_id, _ = self._runtimes.popitem(last=False)
        logger.warning("All %d runtimes busy; evicted oldest %s", self._MAX_RUNTIMES, evicted_id)


class Dispatcher:
    _SEEN_TTL = 60  # seconds to remember message IDs

    def __init__(
        self,
        config: Config,
        store: Store,
        runtimes: RuntimeManager,
        memory_store: MemoryStore | None = None,
    ):
        self.config = config
        self.store = store
        self.runtimes = runtimes
        self.memory_store = memory_store
        self.transports: dict[str, Transport] = {}
        self._seen_messages: collections.OrderedDict[str, float] = collections.OrderedDict()

    def register_transport(self, transport: Transport) -> None:
        self.transports[transport.name] = transport

    def _dedup(self, msg: IncomingMessage) -> bool:
        """Return True if this message_id was already dispatched recently."""
        key = f"{msg.transport_name}:{msg.message_id}"
        now = asyncio.get_running_loop().time()
        # Evict stale entries
        while self._seen_messages:
            oldest_key, oldest_time = next(iter(self._seen_messages.items()))
            if now - oldest_time > self._SEEN_TTL:
                self._seen_messages.pop(oldest_key)
            else:
                break
        if key in self._seen_messages:
            return True
        self._seen_messages[key] = now
        return False

    async def handle_message(self, msg: IncomingMessage) -> None:
        transport = self.transports.get(msg.transport_name)
        if transport is None:
            logger.error("No transport for %s", msg.transport_name)
            return

        # Deduplicate: skip if we've already dispatched this exact message
        if self._dedup(msg):
            logger.debug("Duplicate message %s, skipping", msg.message_id)
            return

        # Auth check
        username = self.store.resolve_username(msg.user_id)
        if not username:
            logger.warning("%s rejected — unknown user", msg.user_id)
            await self._handle_rejection(msg, transport)
            return

        roles = self.store.get_user_roles(username)
        user_tz = self.store.get_user_timezone(username)
        allowed_agents = resolve_allowed_agents(roles, self.config.roles)

        agent_name = transport.agent_name
        if allowed_agents is not None and agent_name not in allowed_agents:
            logger.warning(
                "%s (%s) message to %s rejected — not allowed",
                msg.user_id,
                username,
                agent_name,
            )
            await self._handle_rejection(msg, transport)
            return

        set_user_context(UserContext(username=username, roles=roles, timezone=user_tz))

        set_run_context(agent=agent_name, run_id=new_run_id())
        conversation_id = self.store.lookup_platform_message(
            msg.transport_name, msg.root_message_id
        )
        if not conversation_id:
            conversation_id = transport.build_conversation_id(msg)
        runtime = self.runtimes.get_or_create(conversation_id)
        logger.debug(
            "handle_message msg_id=%s conv=%s runtime=%s",
            msg.message_id[:8] if msg.message_id else "?",
            conversation_id,
            id(runtime),
        )

        # Resolve platform context (cached)
        ctx = await transport.resolve_context(msg)
        ctx.username = username
        ctx.roles = roles
        ctx.timezone = user_tz
        logger.info(
            "message from %s in %s thread=%s",
            ctx.user_name,
            ctx.channel_name,
            msg.root_message_id[:8],
        )

        system_prompt = self._build_system_prompt(
            agent_name,
            ctx,
            username,
            transport,
            msg.is_private,
            allowed_agents=allowed_agents,
        )
        self.store.ensure_conversation(
            conversation_id=conversation_id,
            transport_name=msg.transport_name,
            channel_id=msg.channel_id,
            root_thread_id=msg.root_message_id,
            metadata={
                "agent": agent_name,
                "user_id": msg.user_id if msg.is_private else "",
                "is_private": msg.is_private,
            },
        )
        self.store.ensure_system_message(conversation_id, system_prompt)
        self.store.index_platform_message(msg.transport_name, msg.root_message_id, conversation_id)
        if msg.message_id and msg.message_id != msg.root_message_id:
            self.store.index_platform_message(msg.transport_name, msg.message_id, conversation_id)

        # Handle !commands before touching the LLM
        if msg.text.startswith("!"):
            await self._handle_command(msg, transport, runtime, conversation_id)
            return

        # Claim the conversation — atomic check-and-set (no yield between
        # read and write, so no other task can interleave in asyncio).
        if not runtime.try_claim():
            logger.info("conversation %s busy, rejecting", conversation_id)
            await transport.send(
                msg.channel_id,
                "Still processing a request. Send `!stop` to cancel it.",
                thread_id=msg.root_message_id,
            )
            return

        try:
            current = asyncio.current_task()
            if current is not None:
                runtime.attach_task(current)
            await self._run_conversation(msg, transport, runtime, conversation_id, agent_name)
        finally:
            runtime.release()

    async def _run_conversation(
        self,
        msg: IncomingMessage,
        transport: Transport,
        runtime: ConversationRuntime,
        conversation_id: str,
        agent_name: str,
    ) -> None:
        messages = self.store.load_messages(conversation_id)

        user_ctx = get_user_context()
        username = user_ctx.username if user_ctx else ""

        # Context snapshot injection
        context_parts: list[str] = []

        # Thread history — only on first interaction (no prior agent messages)
        is_new_conversation = len(messages) <= 1  # only system message
        if is_new_conversation and msg.message_id != msg.root_message_id:
            thread_ctx = await transport.get_thread_context(msg)
            if thread_ctx:
                context_parts.append(
                    '<context_snapshot source="thread_history">\n'
                    "Snapshot of this thread before you were added. "
                    "Provided for awareness only — these messages were "
                    "not directed at you.\n\n"
                    f"{thread_ctx}\n"
                    "</context_snapshot>"
                )

        # Prepend to user message text
        msg_text = msg.text
        if context_parts:
            msg_text = "\n\n".join(context_parts) + "\n\n" + msg.text

        # Build user message — multimodal if attachments present
        if msg.attachments:
            workspace_path = self.config.agent_workspace(agent_name)
            attachment_blocks = await process_attachments(
                msg.attachments, transport, workspace_path
            )
            content_blocks: list[dict] = []
            if msg_text:
                content_blocks.append({"type": "text", "text": msg_text})
            content_blocks.extend(attachment_blocks)
            user_message: dict = attach_message_created_at(
                {"role": "user", "content": content_blocks},
                created_at=msg.created_at,
            )
        else:
            user_message = attach_message_created_at(
                {"role": "user", "content": msg_text},
                created_at=msg.created_at,
            )
        messages.append(user_message)
        self.store.append_messages(conversation_id, [user_message])
        persisted_count = len(messages)

        messaging.configure(
            {
                "transport": transport,
                "channel_id": msg.channel_id,
                "thread_id": msg.root_message_id,
            }
        )

        # Configure memory and state tools with execution context
        memory_tools.configure(
            {
                "memory_store": self.memory_store,
                "user_id": username,
                "agent_name": transport.agent_name,
                "allow_user_scope": msg.is_private,
            }
        )
        state_tools.configure({"agent_name": transport.agent_name})

        set_skill_filter(self.config.agent_skill_filter(agent_name))

        msg_count = sum(1 for m in messages if m.get("role") == "user")
        logger.info("conversation %s — message #%d", conversation_id, msg_count)
        extra_tools = transport.get_tools()
        extra_tool_labels: dict[str, collections.abc.Callable[[dict[str, object]], str]] = {}
        for tool in extra_tools:
            if callable(tool.status_label):
                extra_tool_labels[tool.name] = tool.status_label
            elif isinstance(tool.status_label, str):
                label = tool.status_label
                extra_tool_labels[tool.name] = lambda _args, text=label: text

        async def on_message(text: str) -> None:
            preview = text[:25].replace("\n", " ")
            logger.info("→ %s…", preview)
            message_id = await transport.send(msg.channel_id, text, thread_id=msg.root_message_id)
            self.store.index_platform_message(msg.transport_name, message_id, conversation_id)

        status = StatusIndicator(
            transport,
            msg.channel_id,
            msg.root_message_id,
            tool_labels=extra_tool_labels,
        )

        async def on_tool_call(name: str, args: dict) -> None:
            if name:
                status.set_tool(name, args)
            else:
                status.clear_tool()

        usage = {} if self.config.runtime.show_usage else None

        try:
            await status.start()
            await run_agent(
                messages=messages,
                models=self.config.agent_models(agent_name),
                max_iterations=self.config.agent_max_iterations(agent_name),
                workspace=str(self.config.agent_workspace(agent_name)),
                agent_name=agent_name,
                on_message=on_message,
                check_cancelled=runtime.check_cancelled,
                on_tool_call=on_tool_call,
                context_ratio=self.config.agent_context_ratio(agent_name),
                max_output_tokens=self.config.agent_max_output_tokens(agent_name),
                thinking=self.config.agent_thinking(agent_name),
                extra_tools=extra_tools,
                usage=usage,
                tool_filter=self.config.agent_tool_filter(agent_name),
                shared_dir=self.config.shared_dir,
                config=self.config,
            )
            logger.info("conversation %s — done", conversation_id)
            if usage:
                usage_line = _format_usage(usage)
                await transport.send(msg.channel_id, usage_line, thread_id=msg.root_message_id)
        except (AgentCancelledError, asyncio.CancelledError):
            logger.info("conversation %s — stopped by user", conversation_id)
            await transport.send(msg.channel_id, "Request stopped.", thread_id=msg.root_message_id)
        except Exception as e:
            logger.error("agent error: %s: %s", type(e).__name__, e)
            await transport.send(msg.channel_id, f"[error: {e}]", thread_id=msg.root_message_id)
        finally:
            await status.stop()
            pending_messages = messages[persisted_count:]
            safe_messages = trim_incomplete_tool_turns(pending_messages)
            if len(safe_messages) != len(pending_messages):
                logger.warning(
                    "conversation %s — trimmed %d incomplete trailing message(s)",
                    conversation_id,
                    len(pending_messages) - len(safe_messages),
                )
            self.store.append_messages(
                conversation_id,
                safe_messages,
            )

    def _build_system_prompt(
        self,
        agent_name: str,
        ctx: MessageContext,
        user_id: str,
        transport: Transport,
        is_private: bool,
        allowed_agents: set[str] | None = None,
    ) -> str:
        sections: list[str] = []
        transport_prompt = transport.get_prompt_extra()
        if transport_prompt:
            sections.append(transport_prompt)
        context_prompt = ctx.to_prompt(
            workspace=str(self.config.agent_workspace(agent_name)),
            operator_home=str(OPERATOR_DIR),
        )
        if context_prompt:
            sections.append(context_prompt)

        return assemble_system_prompt(
            config=self.config,
            agent_name=agent_name,
            memory_store=self.memory_store,
            username=user_id,
            is_private=is_private,
            transport_extra="\n\n".join(sections),
            skill_filter=self.config.agent_skill_filter(agent_name),
            allowed_agents=allowed_agents,
        )

    async def _handle_command(
        self,
        msg: IncomingMessage,
        transport: Transport,
        runtime: ConversationRuntime,
        conversation_id: str,
    ) -> None:
        parts = msg.text.strip().split()
        cmd_name = parts[0][1:].lower()  # strip "!" prefix
        args = parts[1:]

        ctx = CommandContext(
            args=args,
            agent_name=transport.agent_name,
            store=self.store,
            config=self.config,
            memory_store=self.memory_store,
            runtime=runtime,
            transport=transport,
        )

        response = await dispatch_command(cmd_name, ctx)
        if response:
            await transport.send(msg.channel_id, response, thread_id=msg.root_message_id)

    async def _handle_rejection(self, msg: IncomingMessage, transport: Transport) -> None:
        if self.config.runtime.reject_response == "announce":
            await transport.send(
                msg.channel_id,
                "You don't have access to this agent.",
                thread_id=msg.root_message_id,
            )
        # "ignore" = silently drop


def create_transports(config: Config, store: Store) -> list[Transport]:
    transports: list[Transport] = []
    for agent_name, agent_cfg in config.agents.items():
        tc = agent_cfg.transport
        if tc is None:
            continue
        try:
            transport = create_transport(
                type_name=tc.type,
                name=agent_name,
                agent_name=agent_name,
                env=tc.env,
                settings=tc.settings,
                store=store,
            )
            transports.append(transport)
        except ValueError as e:
            logger.warning("Skipping transport for agent '%s': %s", agent_name, e)
    return transports


def _setup_logging() -> None:
    setup_logging(
        log_dir=LOGS_DIR,
        stderr=os.isatty(2),
        noisy_loggers=("httpx", "httpcore", "litellm", "openai", *transport_logger_names()),
    )


def _acquire_lock() -> int:
    """Acquire an exclusive process lock. Returns the fd (keep open for lifetime).

    Raises SystemExit if another instance is already running.
    """
    lock_path = OPERATOR_DIR / "operator.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        logger.error("Another operator process is already running")
        sys.exit(1)
    return fd


_SWEEP_INTERVAL = 3600  # seconds — sweep expired memories once per hour


async def _sweep_loop(memory_store: MemoryStore) -> None:
    """Periodically sweep expired memory files to trash."""
    try:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL)
            try:
                memory_store.sweep_expired()
            except Exception:
                logger.exception("Memory sweep failed")
    except asyncio.CancelledError:
        return


async def async_main() -> None:
    _setup_logging()

    lock_fd = _acquire_lock()  # held for process lifetime
    transport_tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()
    handlers_installed = False
    job_runner: JobRunner | None = None
    sweep_task: asyncio.Task[None] | None = None
    transports: list[Transport] = []
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        stop.set()

    try:
        try:
            config = load_config()
        except ConfigError as e:
            raise SystemExit(str(e)) from None

        # Bootstrap directory layout
        ensure_layout(config)

        install_bundled_skills(config.skills_dir())

        if not any(a.transport for a in config.agents.values()):
            logger.error("No transports configured in %s", OPERATOR_DIR / "operator.yaml")
            sys.exit(1)

        store = get_store()
        memory_store = MemoryStore(base_dir=OPERATOR_DIR)

        if not store.list_users():
            logger.warning(
                "No users configured. Run: operator setup (or operator user add <username> --role admin <transport> <id>)"
            )

        runtimes = RuntimeManager()
        dispatcher = Dispatcher(config, store, runtimes, memory_store=memory_store)
        transports = create_transports(config, store)

        if not transports:
            logger.error("No transports could be started (check env vars)")
            sys.exit(1)

        # Register transports (but don't start yet — start() blocks)
        for transport in transports:
            dispatcher.register_transport(transport)

        # Start job runner and memory sweep.
        job_runner = JobRunner(config, dispatcher.transports, store, memory_store=memory_store)
        job_runner.start()
        sweep_task = asyncio.create_task(_sweep_loop(memory_store))

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
        handlers_installed = True

        # Start transports as background tasks and stop if one exits unexpectedly.
        for transport in transports:
            task = asyncio.create_task(transport.start(dispatcher.handle_message))

            def _on_done(
                done: asyncio.Task[None],
                *,
                transport_name: str = transport.name,
            ) -> None:
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    logger.exception(
                        "Transport '%s' crashed; stopping operator",
                        transport_name,
                        exc_info=exc,
                    )
                    stop.set()
                    return
                logger.error(
                    "Transport '%s' exited unexpectedly; stopping operator", transport_name
                )
                stop.set()

            task.add_done_callback(_on_done)
            transport_tasks.append(task)
            logger.info("Transport '%s' starting (agent: %s)", transport.name, transport.agent_name)

        logger.info(
            "Operator running with %d transport(s). Ctrl+C to stop.",
            len(transports),
        )
        await stop.wait()
    finally:
        logger.info("Shutting down...")
        if handlers_installed:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError):
                    loop.remove_signal_handler(sig)

        if sweep_task:
            sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await sweep_task

        if job_runner:
            await job_runner.stop()

        for task in transport_tasks:
            task.cancel()
        if transport_tasks:
            await asyncio.gather(*transport_tasks, return_exceptions=True)
        for transport in transports:
            await transport.stop()

        await close_session()
        os.close(lock_fd)


if __name__ == "__main__":
    asyncio.run(async_main())
