"""Tests for runtime components and dispatcher control flow."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from operator_ai.config import Config, RoleConfig
from operator_ai.main import (
    AgentCancelledError,
    ConversationBusyError,
    ConversationRuntime,
    Dispatcher,
    RuntimeCapacityError,
    RuntimeManager,
    create_transports,
    resolve_allowed_agents,
)
from operator_ai.store import Store
from operator_ai.transport.base import IncomingMessage, MessageContext, Transport


class DummyTransport(Transport):
    platform = "slack"

    def __init__(self, name: str = "operator", agent_name: str = "operator") -> None:
        self.name = name
        self.agent_name = agent_name
        self.sent: list[tuple[str, str, str | None]] = []
        self.deleted: list[tuple[str, str, str | None]] = []

    async def start(self, on_message):  # pragma: no cover - not used in these tests
        raise NotImplementedError

    async def stop(self) -> None:
        return None

    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        self.sent.append((channel_id, text, thread_id))
        return f"msg-{len(self.sent)}"

    async def resolve_context(self, msg: IncomingMessage) -> MessageContext:
        return MessageContext(
            platform=self.platform,
            channel_id=msg.channel_id,
            channel_name="#general",
            user_id=msg.user_id,
            user_name="Alice",
        )

    async def delete(
        self,
        channel_id: str,
        message_id: str,
        thread_id: str | None = None,
    ) -> None:
        self.deleted.append((channel_id, message_id, thread_id))


def _config(base_dir: Path) -> Config:
    config = Config(defaults={"models": ["test/m"]}, agents={"operator": {}})
    config.set_base_dir(base_dir)
    return config


def _message(
    text: str,
    *,
    message_id: str,
    root_message_id: str,
    is_private: bool = True,
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user_id="slack:U1",
        channel_id="C1",
        message_id=message_id,
        root_message_id=root_message_id,
        transport_name="operator",
        is_private=is_private,
    )


def _texts(transport: DummyTransport) -> list[str]:
    return [text for _, text, _ in transport.sent]


def _strip_created_at(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in message.items() if key != "_operator_created_at"}
        for message in messages
    ]


# ── resolve_allowed_agents ────────────────────────────────────────


def test_admin_returns_none() -> None:
    result = resolve_allowed_agents(["admin"], {})
    assert result is None


def test_admin_with_other_roles_returns_none() -> None:
    roles_cfg = {"viewer": RoleConfig(agents=["hermy"])}
    result = resolve_allowed_agents(["admin", "viewer"], roles_cfg)
    assert result is None


def test_single_role_returns_agents() -> None:
    roles_cfg = {"ops": RoleConfig(agents=["hermy", "cora"])}
    result = resolve_allowed_agents(["ops"], roles_cfg)
    assert result == {"hermy", "cora"}


def test_multiple_roles_union() -> None:
    roles_cfg = {
        "ops": RoleConfig(agents=["hermy"]),
        "dev": RoleConfig(agents=["cora", "pearl"]),
    }
    result = resolve_allowed_agents(["ops", "dev"], roles_cfg)
    assert result == {"hermy", "cora", "pearl"}


def test_unknown_role_ignored() -> None:
    roles_cfg = {"ops": RoleConfig(agents=["hermy"])}
    result = resolve_allowed_agents(["ops", "nonexistent"], roles_cfg)
    assert result == {"hermy"}


def test_no_roles_returns_empty() -> None:
    result = resolve_allowed_agents([], {})
    assert result == set()


def test_role_with_no_agents() -> None:
    roles_cfg = {"empty": RoleConfig(agents=[])}
    result = resolve_allowed_agents(["empty"], roles_cfg)
    assert result == set()


def test_create_transports_uses_normalized_transport_config(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_create_transport(
        *,
        type_name: str,
        agent_name: str,
        env: dict[str, object],
        settings: dict[str, object],
        store: Store,
    ) -> object:
        captured["type_name"] = type_name
        captured["agent_name"] = agent_name
        captured["env"] = env
        captured["settings"] = settings
        captured["store"] = store
        return object()

    monkeypatch.setattr("operator_ai.main.create_transport", fake_create_transport)

    config = Config(
        defaults={"models": ["test/m"]},
        agents={
            "operator": {
                "transport": {
                    "type": "slack",
                    "env": {
                        "bot_token": "SLACK_BOT_TOKEN",
                        "app_token": "SLACK_APP_TOKEN",
                    },
                    "settings": {
                        "inject_users_into_prompt": False,
                    },
                }
            }
        },
    )

    with Store(path=tmp_path / "operator.db") as store:
        transports = create_transports(config, store)

    assert len(transports) == 1
    assert captured["type_name"] == "slack"
    assert captured["agent_name"] == "operator"
    assert captured["store"] is not None

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["bot_token"] == "SLACK_BOT_TOKEN"
    assert env["app_token"] == "SLACK_APP_TOKEN"

    settings = captured["settings"]
    assert isinstance(settings, dict)
    assert settings["inject_users_into_prompt"] is False
    assert settings["inject_channels_into_prompt"] is True


# ── ConversationRuntime ────────────────────────────────────────────


def test_claim_and_release() -> None:
    rt = ConversationRuntime()
    assert not rt.busy

    assert rt.try_claim() is True
    assert rt.busy
    assert rt.try_claim() is False

    rt.release()
    assert not rt.busy
    assert rt.try_claim() is True


def test_cancel_sets_event() -> None:
    rt = ConversationRuntime()
    assert not rt.cancelled.is_set()

    rt.cancel()
    assert rt.cancelled.is_set()


def test_check_cancelled_raises() -> None:
    rt = ConversationRuntime()
    rt.cancel()

    with pytest.raises(AgentCancelledError):
        rt.check_cancelled()

    assert not rt.cancelled.is_set()
    rt.check_cancelled()


def test_release_clears_cancelled() -> None:
    rt = ConversationRuntime()
    rt.try_claim()
    rt.cancel()
    assert rt.cancelled.is_set()

    rt.release()
    assert not rt.cancelled.is_set()


def test_attach_task_and_cancel() -> None:
    async def _run() -> None:
        rt = ConversationRuntime()
        rt.try_claim()

        async def _long_running() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(_long_running())
        rt.attach_task(task)
        rt.cancel()

        assert task.cancelled() or rt.cancelled.is_set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())


# ── RuntimeManager ────────────────────────────────────────────────


def test_claim_tracks_active_runtime_until_release() -> None:
    mgr = RuntimeManager()
    rt = mgr.claim("conv-1")

    assert isinstance(rt, ConversationRuntime)
    assert rt.busy
    assert mgr.get("conv-1") is rt

    mgr.release("conv-1", rt)
    assert mgr.get("conv-1") is None


def test_claim_same_conversation_raises_busy() -> None:
    mgr = RuntimeManager()
    rt = mgr.claim("conv-1")

    with pytest.raises(ConversationBusyError):
        mgr.claim("conv-1")

    mgr.release("conv-1", rt)


def test_claim_new_conversation_raises_when_active_capacity_reached() -> None:
    mgr = RuntimeManager()
    mgr._MAX_ACTIVE_RUNTIMES = 1

    rt = mgr.claim("conv-1")
    with pytest.raises(RuntimeCapacityError):
        mgr.claim("conv-2")

    mgr.release("conv-1", rt)
    rt2 = mgr.claim("conv-2")
    assert mgr.get("conv-2") is rt2


# ── Dispatcher stop/cancel control path ───────────────────────────


@pytest.fixture
def configured_store(tmp_path: Path) -> Store:
    store = Store(path=tmp_path / "operator.db")
    store.add_user("alice")
    store.add_identity("alice", "slack:U1")
    store.add_role("alice", "admin")
    return store


def test_stop_signal_without_active_request(configured_store: Store, tmp_path: Path) -> None:
    async def _run() -> None:
        config = _config(tmp_path)
        dispatcher = Dispatcher(config, configured_store, RuntimeManager())
        transport = DummyTransport()
        dispatcher.register_transport(transport)

        await dispatcher.handle_message(_message(" stop ", message_id="1", root_message_id="1"))

        assert _texts(transport) == ["No active request to stop."]
        assert configured_store.lookup_platform_message("operator", "1") is None

    asyncio.run(_run())


def test_stop_signal_cancels_active_conversation(
    monkeypatch,
    configured_store: Store,
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        config = _config(tmp_path)
        dispatcher = Dispatcher(config, configured_store, RuntimeManager())
        transport = DummyTransport()
        dispatcher.register_transport(transport)

        def fake_system_prompt(*_args: Any, **_kwargs: Any) -> str:
            return "system"

        monkeypatch.setattr("operator_ai.main.build_agent_system_prompt", fake_system_prompt)

        started = asyncio.Event()

        async def fake_run_agent(**_kwargs: Any) -> str:
            started.set()
            await asyncio.sleep(60)
            return ""

        monkeypatch.setattr("operator_ai.main.run_agent", fake_run_agent)

        first = _message("hello", message_id="1", root_message_id="1")
        task = asyncio.create_task(dispatcher.handle_message(first))
        await started.wait()

        await dispatcher.handle_message(_message("stop", message_id="2", root_message_id="1"))
        await task

        conversation_id = transport.build_conversation_id(first)
        assert dispatcher.runtimes.get(conversation_id) is None
        assert "Cancelling…" in _texts(transport)
        assert "Request stopped." in _texts(transport)
        assert _strip_created_at(configured_store.load_messages(conversation_id)) == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]

    asyncio.run(_run())


def test_stop_words_require_exact_match(
    monkeypatch,
    configured_store: Store,
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        config = _config(tmp_path)
        dispatcher = Dispatcher(config, configured_store, RuntimeManager())
        transport = DummyTransport()
        dispatcher.register_transport(transport)

        def fake_system_prompt(*_args: Any, **_kwargs: Any) -> str:
            return "system"

        monkeypatch.setattr("operator_ai.main.build_agent_system_prompt", fake_system_prompt)

        started = asyncio.Event()
        allow_finish = asyncio.Event()

        async def fake_run_agent(**_kwargs: Any) -> str:
            started.set()
            await allow_finish.wait()
            return ""

        monkeypatch.setattr("operator_ai.main.run_agent", fake_run_agent)

        first = _message("hello", message_id="1", root_message_id="1")
        task = asyncio.create_task(dispatcher.handle_message(first))
        await started.wait()

        await dispatcher.handle_message(
            _message("please stop", message_id="2", root_message_id="1")
        )
        assert "Still processing a request. Say `stop` to stop it." in _texts(transport)
        assert not task.done()

        await dispatcher.handle_message(_message("cancel", message_id="3", root_message_id="1"))
        allow_finish.set()
        await task

        assert "Cancelling…" in _texts(transport)

    asyncio.run(_run())


def test_dispatch_replies_in_the_inbound_thread(
    monkeypatch,
    configured_store: Store,
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        config = _config(tmp_path)
        dispatcher = Dispatcher(config, configured_store, RuntimeManager())
        transport = DummyTransport()
        dispatcher.register_transport(transport)

        def fake_system_prompt(*_args: Any, **_kwargs: Any) -> str:
            return "system"

        monkeypatch.setattr("operator_ai.main.build_agent_system_prompt", fake_system_prompt)

        async def fake_run_agent(**kwargs: Any) -> str:
            await kwargs["on_message"]("Threaded reply")
            return ""

        monkeypatch.setattr("operator_ai.main.run_agent", fake_run_agent)

        await dispatcher.handle_message(_message("hello", message_id="1", root_message_id="1"))

        assert transport.sent[-1] == ("C1", "Threaded reply", "1")

    asyncio.run(_run())
