from __future__ import annotations

import asyncio
import base64
import collections
import fcntl
import functools
import logging
import os
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

# Import tools to trigger registration
import operator_ai.tools  # noqa: F401
from operator_ai.agent import configure_agent_tool_context, run_agent
from operator_ai.config import Config, ConfigError, RoleConfig, load_config
from operator_ai.jobs import JobRunner
from operator_ai.layout import ensure_layout
from operator_ai.log_context import new_run_id, set_run_context, setup_logging
from operator_ai.memory import MemoryIndex, MemoryStore, reindex_diff
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.messages import trim_incomplete_tool_turns
from operator_ai.run_prompt import ChatEnvelope, build_agent_system_prompt
from operator_ai.status import StatusIndicator
from operator_ai.store import Store, get_store, reset_store
from operator_ai.system_events import SystemEventBuffer
from operator_ai.tools import messaging
from operator_ai.tools.context import (
    UserContext,
    get_user_context,
    set_user_context,
)
from operator_ai.tools.web import close_session
from operator_ai.transport.base import Attachment, IncomingMessage, Transport
from operator_ai.transport.registry import create_transport, transport_logger_names

logger = logging.getLogger("operator")


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


class ConversationBusyError(Exception):
    pass


class RuntimeCapacityError(Exception):
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
STOP_WORDS = frozenset({"stop", "cancel"})


def _is_stop_signal(text: str) -> bool:
    return text.strip().lower() in STOP_WORDS


async def process_attachments(
    attachments: list[Attachment],
    transport: Transport,
    workspace: Path,
) -> list[dict]:
    """Download attachments and return multimodal content blocks.

    All attachments are saved to workspace/inbox/ so they remain available as
    workspace artifacts. Small images are also inlined as base64 image_url
    blocks for direct visual inspection by the model.
    """
    blocks: list[dict] = []
    inbox_dir = workspace / "inbox"

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

        inbox_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(att.filename).name or "unnamed"
        dest = inbox_dir / safe_name
        # Avoid overwriting — append suffix if needed
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = inbox_dir / f"{stem}_{counter}{suffix}"
                counter += 1
        dest.write_bytes(data)
        blocks.append(
            {
                "type": "text",
                "text": f"[file saved: inbox/{dest.name} ({att.content_type}, {len(data)} bytes)]",
            }
        )

        if att.content_type in _IMAGE_TYPES and len(data) <= MAX_INLINE_SIZE:
            b64 = base64.b64encode(data).decode()
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
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
    _MAX_ACTIVE_RUNTIMES = 256

    def __init__(self) -> None:
        self._runtimes: dict[str, ConversationRuntime] = {}

    def get(self, conversation_id: str) -> ConversationRuntime | None:
        return self._runtimes.get(conversation_id)

    def claim(self, conversation_id: str) -> ConversationRuntime:
        runtime = self._runtimes.get(conversation_id)
        if runtime is not None:
            raise ConversationBusyError()
        if len(self._runtimes) >= self._MAX_ACTIVE_RUNTIMES:
            raise RuntimeCapacityError()
        runtime = ConversationRuntime()
        runtime.try_claim()
        self._runtimes[conversation_id] = runtime
        return runtime

    def release(self, conversation_id: str, runtime: ConversationRuntime) -> None:
        tracked = self._runtimes.get(conversation_id)
        runtime.release()
        if tracked is runtime:
            del self._runtimes[conversation_id]
        elif tracked is not None:
            logger.warning("Conversation %s runtime mismatch on release", conversation_id)


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
        self._system_events = SystemEventBuffer()

    def register_transport(self, transport: Transport) -> None:
        self.transports[transport.agent_name] = transport
        transport.set_system_event_handler(
            functools.partial(self._handle_system_event, transport.agent_name)
        )

    async def _handle_system_event(
        self, transport_name: str, channel_id: str, message_id: str, text: str
    ) -> None:
        """Route a system event to the correct conversation buffer."""
        conversation_id = self.store.lookup_platform_message(transport_name, message_id)
        if conversation_id:
            logger.info("System event routed to %s: %s", conversation_id, text)
            self._system_events.enqueue(conversation_id, text)
        else:
            logger.info(
                "System event for untracked message %s in %s, skipping", message_id, channel_id
            )

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

        if _is_stop_signal(msg.text):
            await self._handle_stop_signal(msg, transport, conversation_id)
            return

        try:
            runtime = self.runtimes.claim(conversation_id)
        except ConversationBusyError:
            logger.info("conversation %s busy, rejecting", conversation_id)
            await transport.send(
                msg.channel_id,
                "Still processing a request. Say `stop` to stop it.",
                thread_id=msg.root_message_id,
            )
            return
        except RuntimeCapacityError:
            logger.warning("Active conversation cap reached; rejecting %s", conversation_id)
            await transport.send(
                msg.channel_id,
                "Operator is busy handling other conversations. Try again shortly.",
                thread_id=msg.root_message_id,
            )
            return

        logger.debug(
            "handle_message msg_id=%s conv=%s runtime=%s",
            msg.message_id[:8] if msg.message_id else "?",
            conversation_id,
            id(runtime),
        )

        current = asyncio.current_task()
        if current is not None:
            runtime.attach_task(current)

        try:
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

            run_envelope = ChatEnvelope(
                context=ctx,
                transport_prompt=transport.get_prompt_extra(),
                is_private=msg.is_private,
            )
            system_prompt = build_agent_system_prompt(
                config=self.config,
                agent_name=agent_name,
                memory_store=self.memory_store,
                username=username,
                skill_filter=self.config.agent_skill_filter(agent_name),
                allowed_agents=allowed_agents,
                run_envelope=run_envelope,
            )
            self.store.ensure_conversation(conversation_id)
            self.store.ensure_system_message(conversation_id, system_prompt)
            self.store.index_platform_message(
                msg.transport_name, msg.root_message_id, conversation_id
            )
            if msg.message_id and msg.message_id != msg.root_message_id:
                self.store.index_platform_message(
                    msg.transport_name, msg.message_id, conversation_id
                )
            await self._run_conversation(
                msg,
                transport,
                runtime,
                conversation_id,
                agent_name,
                run_envelope,
                allowed_agents=allowed_agents,
            )
        except asyncio.CancelledError:
            logger.info("conversation %s — stopped by user", conversation_id)
            await transport.send(
                msg.channel_id,
                "Request stopped.",
                thread_id=msg.root_message_id,
            )
        finally:
            self.runtimes.release(conversation_id, runtime)

    async def _run_conversation(
        self,
        msg: IncomingMessage,
        transport: Transport,
        runtime: ConversationRuntime,
        conversation_id: str,
        agent_name: str,
        run_envelope: ChatEnvelope,
        *,
        allowed_agents: set[str] | None = None,
    ) -> None:
        messages = self.store.load_messages(conversation_id)

        user_ctx = get_user_context()
        username = user_ctx.username if user_ctx else ""

        # Collect context blocks from all sources and prepend to user message
        context_parts = await self._collect_context(msg, transport, conversation_id, messages)
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

        configure_agent_tool_context(
            agent_name=transport.agent_name,
            base_dir=self.config.base_dir,
            skill_filter=self.config.agent_skill_filter(agent_name),
            memory_store=self.memory_store,
            username=username,
            allow_user_scope=msg.is_private,
        )

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
                skill_filter=self.config.agent_skill_filter(agent_name),
                shared_dir=self.config.shared_dir,
                config=self.config,
                memory_store=self.memory_store,
                username=username,
                allow_user_scope=msg.is_private,
                allowed_agents=allowed_agents,
                base_dir=self.config.base_dir,
                run_envelope=run_envelope,
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

    # --- Context collection ---

    async def _collect_context(
        self,
        msg: IncomingMessage,
        transport: Transport,
        conversation_id: str,
        messages: list[dict],
    ) -> list[str]:
        """Collect all context blocks to prepend to the user message."""
        blocks: list[str] = []

        # Thread history (first interaction only)
        block = await self._thread_history_context(msg, transport, messages)
        if block:
            blocks.append(block)

        # Buffered system events (reactions, pins, etc.)
        block = self._system_events_context(conversation_id)
        if block:
            blocks.append(block)

        # Transport-provided per-message context (message metadata, etc.)
        transport_blocks = await transport.get_message_context(msg)
        blocks.extend(transport_blocks)

        return blocks

    async def _thread_history_context(
        self,
        msg: IncomingMessage,
        transport: Transport,
        messages: list[dict],
    ) -> str | None:
        """Build thread history context block for new conversations."""
        is_new_conversation = len(messages) <= 1  # only system message
        if not is_new_conversation or msg.message_id == msg.root_message_id:
            return None
        thread_ctx = await transport.get_thread_context(msg)
        if not thread_ctx:
            return None
        return (
            '<context_snapshot source="thread_history">\n'
            "Snapshot of this thread before you were added. "
            "Provided for awareness only — these messages were "
            "not directed at you.\n\n"
            f"{thread_ctx}\n"
            "</context_snapshot>"
        )

    def _system_events_context(self, conversation_id: str) -> str | None:
        """Drain buffered system events and format as a context block."""
        events = self._system_events.drain(conversation_id)
        if not events:
            return None
        logger.info(
            "Injecting %d system event(s) into conversation %s",
            len(events),
            conversation_id,
        )
        event_lines = []
        for ev in events:
            dt = datetime.fromtimestamp(ev.ts, tz=UTC)
            t = dt.strftime("%-I:%M %p")
            event_lines.append(f"- [{t}] {ev.text}")
        return (
            '<context_snapshot source="system_events">\n'
            "Recent platform events since your last response:\n\n"
            + "\n".join(event_lines)
            + "\n</context_snapshot>"
        )

    async def _handle_stop_signal(
        self,
        msg: IncomingMessage,
        transport: Transport,
        conversation_id: str,
    ) -> None:
        runtime = self.runtimes.get(conversation_id)
        if runtime and runtime.busy:
            logger.info("conversation %s stop requested", conversation_id)
            runtime.cancel()
            await transport.send(msg.channel_id, "Cancelling…", thread_id=msg.root_message_id)
            return
        await transport.send(
            msg.channel_id,
            "No active request to stop.",
            thread_id=msg.root_message_id,
        )

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
                agent_name=agent_name,
                env=tc.env,
                settings=tc.settings,
                store=store,
            )
            transports.append(transport)
        except ValueError as e:
            logger.warning("Skipping transport for agent '%s': %s", agent_name, e)
    return transports


def _setup_logging(log_dir: Path) -> None:
    setup_logging(
        log_dir=log_dir,
        stderr=os.isatty(2),
        noisy_loggers=("httpx", "httpcore", "litellm", "openai", *transport_logger_names()),
    )


def _acquire_lock(base_dir: Path) -> int:
    """Acquire an exclusive process lock. Returns the fd (keep open for lifetime).

    Raises SystemExit if another instance is already running.
    """
    lock_path = base_dir / "operator.lock"
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
    try:
        config = load_config()
    except ConfigError as e:
        raise SystemExit(str(e)) from None

    _setup_logging(config.logs_dir())

    lock_fd = _acquire_lock(config.base_dir)  # held for process lifetime
    transport_tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()
    handlers_installed = False
    job_runner: JobRunner | None = None
    sweep_task: asyncio.Task[None] | None = None
    transports: list[Transport] = []
    memory_index: MemoryIndex | None = None
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        stop.set()

    try:
        # Bootstrap directory layout
        ensure_layout(config)

        if not any(a.transport for a in config.agents.values()):
            logger.error("No transports configured in %s", config.base_dir / "operator.yaml")
            sys.exit(1)

        store = get_store(config.db_dir() / "operator.db")

        # Build the memory index (FTS5 + optional vector)
        embed_fn = None
        embed_dims = 1536
        if config.defaults.embeddings:
            embed_dims = config.defaults.embeddings.dimensions
            embed_model = config.defaults.embeddings.model

            def embed_fn(text: str) -> list[float]:
                import litellm

                resp = litellm.embedding(model=embed_model, input=[text])
                return resp.data[0]["embedding"]

        memory_index = MemoryIndex(
            config.db_dir() / "memory_index.db",
            embed_fn=embed_fn,
            embedding_dimensions=embed_dims,
        )
        memory_store = MemoryStore(base_dir=config.base_dir, index=memory_index)

        # Startup reindex — only changed files
        try:
            reindex_diff(memory_store, memory_index)
        except Exception:
            logger.exception("Startup memory reindex failed (non-fatal)")

        if not store.list_users():
            logger.warning(
                "No users configured. Run: operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>"
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
                transport_name: str = transport.agent_name,
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
            logger.info("Transport starting for agent '%s'", transport.agent_name)

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
        if memory_index is not None:
            memory_index.close()
        reset_store()
        os.close(lock_fd)


if __name__ == "__main__":
    asyncio.run(async_main())
