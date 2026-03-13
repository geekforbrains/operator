from __future__ import annotations

import asyncio
import collections
import functools
import logging
from datetime import UTC, datetime

from operator_ai.agent import RunConfig, configure_agent_tool_context, run_agent
from operator_ai.config import Config, RoleConfig
from operator_ai.log_context import new_run_id, set_run_context
from operator_ai.main.attachments import process_attachments
from operator_ai.main.runtime import (
    AgentCancelledError,
    ConversationBusyError,
    ConversationRuntime,
    RuntimeCapacityError,
    RuntimeManager,
)
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.messages import trim_incomplete_tool_turns
from operator_ai.run_prompt import ChatEnvelope, build_agent_system_prompt
from operator_ai.status import StatusIndicator
from operator_ai.store import Store
from operator_ai.system_events import SystemEventBuffer
from operator_ai.tools import messaging
from operator_ai.tools.context import (
    UserContext,
    get_user_context,
    set_user_context,
)
from operator_ai.transport.base import IncomingMessage, Transport

logger = logging.getLogger("operator")

STOP_WORDS = frozenset({"stop", "cancel"})


def _is_stop_signal(text: str) -> bool:
    return text.strip().lower() in STOP_WORDS


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
                rc=RunConfig(
                    models=self.config.agent_models(agent_name),
                    max_iterations=self.config.agent_max_iterations(agent_name),
                    workspace=str(self.config.agent_workspace(agent_name)),
                    agent_name=agent_name,
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
                ),
                on_message=on_message,
                check_cancelled=runtime.check_cancelled,
                on_tool_call=on_tool_call,
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
