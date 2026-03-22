"""Integration tests for the WeChat listener plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from orcheo.listeners.compiler import compile_listener_subscriptions
from orcheo.listeners.models import ListenerCursor, ListenerSubscription
from orcheo.listeners.registry import listener_registry
from orcheo.plugins import load_enabled_plugins, reset_plugin_loader_for_tests
from orcheo.plugins.manager import PluginManager

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WECHAT_PLUGIN_SRC = PACKAGE_ROOT / "src"


def _set_plugin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    cache_dir = tmp_path / "cache"
    config_dir = tmp_path / "config"
    plugin_dir.mkdir()
    cache_dir.mkdir()
    config_dir.mkdir()
    monkeypatch.setenv("ORCHEO_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setenv("ORCHEO_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("ORCHEO_CONFIG_DIR", str(config_dir))


def _load_plugins() -> None:
    reset_plugin_loader_for_tests()
    load_enabled_plugins(force=True)


class RecordingListenerRepository:
    """Repository stub that records dispatched listener payloads and cursors."""

    def __init__(self) -> None:
        self.events: list[tuple[object, object]] = []
        self.cursor: ListenerCursor | None = None

    async def get_listener_cursor(
        self, subscription_id: object
    ) -> ListenerCursor | None:
        _ = subscription_id
        return self.cursor

    async def save_listener_cursor(self, cursor: ListenerCursor) -> ListenerCursor:
        self.cursor = cursor
        return cursor

    async def dispatch_listener_event(
        self, subscription_id: object, payload: object
    ) -> object:
        self.events.append((subscription_id, payload))
        return {"subscription_id": str(subscription_id)}


def _load_wechat_plugin_module() -> ModuleType:
    module_name = "test_orcheo_plugin_wechat_listener"
    module_path = WECHAT_PLUGIN_SRC / "orcheo_plugin_wechat_listener" / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


@pytest.mark.asyncio()
async def test_wechat_plugin_dispatches_fixture_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The WeChat validation plugin should register and dispatch fixtures."""
    _set_plugin_env(monkeypatch, tmp_path)
    manager = PluginManager()
    manager.install(str(PACKAGE_ROOT))

    _load_plugins()

    subscriptions = compile_listener_subscriptions(
        uuid4(),
        uuid4(),
        {
            "index": {
                "listeners": [
                    {
                        "node_name": "wechat_listener",
                        "platform": "wechat",
                        "account_id": "wx-account",
                        "test_events": [
                            {
                                "text": "hello from wechat",
                                "from_user_id": "wx-user-123",
                                "message_id": 42,
                                "context_token": "ctx-123",
                            }
                        ],
                    }
                ]
            }
        },
    )
    subscription = subscriptions[0]
    repository = RecordingListenerRepository()
    adapter = listener_registry.build_adapter(
        "wechat",
        repository=repository,
        subscription=subscription,
        runtime_id="wechat-runtime",
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(adapter.run(stop_event))
    await asyncio.sleep(0)
    stop_event.set()
    await task

    assert len(repository.events) == 1
    _subscription_id, payload = repository.events[0]
    assert payload.platform == "wechat"
    assert payload.message.text == "hello from wechat"
    assert payload.message.user_id == "wx-user-123"
    assert payload.reply_target["context_token"] == "ctx-123"
    assert payload.bot_identity == "wechat:wx-account"

    uninstall_impact = manager.uninstall("orcheo-plugin-wechat-listener")
    assert uninstall_impact.restart_required is True


def test_wechat_message_normalization_skips_bot_echoes() -> None:
    """Bot-authored OpenClaw messages should not dispatch back into workflows."""
    weixin_plugin = _load_wechat_plugin_module()
    subscription = ListenerSubscription(
        workflow_id=uuid4(),
        workflow_version_id=uuid4(),
        node_name="wechat_listener",
        platform="wechat",
        bot_identity_key="wechat:test-account",
        config={"account_id": "test-account"},
    )

    payload = weixin_plugin.normalize_weixin_message(
        subscription,
        {
            "message_id": 7,
            "from_user_id": "wx-user",
            "message_type": 2,
            "item_list": [{"type": 1, "text_item": {"text": "echo"}}],
        },
    )
    assert payload is None


@pytest.mark.asyncio()
async def test_wechat_adapter_polls_and_persists_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The long-poll adapter should import OpenClaw credentials and save cursor."""
    weixin_plugin = _load_wechat_plugin_module()
    state_dir = tmp_path / ".openclaw"
    accounts_dir = state_dir / "openclaw-weixin" / "accounts"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "wx-account.json").write_text(
        json.dumps(
            {
                "token": "test-token",
                "baseUrl": "https://example.weixin.test",
            }
        ),
        encoding="utf-8",
    )
    (accounts_dir / "wx-account.sync.json").write_text(
        json.dumps({"get_updates_buf": "cursor-from-openclaw"}),
        encoding="utf-8",
    )

    responses = [
        {
            "ret": 0,
            "msgs": [
                {
                    "seq": 10,
                    "message_id": 99,
                    "from_user_id": "wx-user-99",
                    "to_user_id": "wx-bot",
                    "session_id": "wx-session",
                    "message_type": 1,
                    "message_state": 0,
                    "context_token": "ctx-99",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "hello from polling"}}
                    ],
                }
            ],
            "get_updates_buf": "cursor-after-poll",
            "longpolling_timeout_ms": 1234,
        }
    ]

    async def fake_get_updates(
        *,
        base_url: str,
        token: str,
        get_updates_buf: str,
        timeout_ms: int,
    ) -> dict[str, object]:
        assert base_url == "https://example.weixin.test"
        assert token == "test-token"
        assert timeout_ms in {
            weixin_plugin.DEFAULT_LONG_POLL_TIMEOUT_MS,
            1234,
        }
        if responses:
            assert get_updates_buf == "cursor-from-openclaw"
            return responses.pop(0)
        await asyncio.sleep(0.01)
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}

    monkeypatch.setattr(weixin_plugin, "get_weixin_updates", fake_get_updates)

    subscription = ListenerSubscription(
        workflow_id=uuid4(),
        workflow_version_id=uuid4(),
        node_name="wechat_listener",
        platform="wechat",
        bot_identity_key="wechat:wx-account",
        config={
            "account_id": "wx-account",
            "openclaw_state_dir": str(state_dir),
        },
    )
    repository = RecordingListenerRepository()
    adapter = weixin_plugin.WeixinListenerAdapter(
        repository=repository,
        subscription=subscription,
        runtime_id="wechat-runtime",
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(adapter.run(stop_event))
    while not repository.events:
        await asyncio.sleep(0)
    stop_event.set()
    await task

    assert len(repository.events) == 1
    _subscription_id, payload = repository.events[0]
    assert payload.message.text == "hello from polling"
    assert payload.reply_target["to_user_id"] == "wx-user-99"
    assert payload.reply_target["context_token"] == "ctx-99"
    assert repository.cursor is not None
    assert repository.cursor.metadata["weixin_get_updates_buf"] == "cursor-after-poll"
    assert adapter.health().last_polled_at is not None
    assert adapter.health().last_polled_at.tzinfo is not None


@pytest.mark.asyncio()
async def test_wechat_reply_node_uses_openclaw_account_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reply node should resolve token/base URL from OpenClaw account data."""
    weixin_plugin = _load_wechat_plugin_module()
    state_dir = tmp_path / ".openclaw"
    accounts_dir = state_dir / "openclaw-weixin" / "accounts"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "wx-account.json").write_text(
        json.dumps(
            {
                "token": "reply-token",
                "baseUrl": "https://reply.weixin.test",
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def fake_send_weixin_text_message(
        *,
        base_url: str,
        token: str,
        to_user_id: str,
        context_token: str,
        message: str,
    ) -> None:
        captured.update(
            {
                "base_url": base_url,
                "token": token,
                "to_user_id": to_user_id,
                "context_token": context_token,
                "message": message,
            }
        )

    monkeypatch.setattr(
        weixin_plugin,
        "send_weixin_text_message",
        fake_send_weixin_text_message,
    )

    node = weixin_plugin.WechatReplyNode(
        name="reply",
        message="reply from workflow",
        account_id="wx-account",
        openclaw_state_dir=str(state_dir),
        reply_target={
            "to_user_id": "wx-user-77",
            "context_token": "ctx-77",
        },
    )

    result = await node.run({}, {})
    assert result == {"sent": True, "to_user_id": "wx-user-77"}
    assert captured == {
        "base_url": "https://reply.weixin.test",
        "token": "reply-token",
        "to_user_id": "wx-user-77",
        "context_token": "ctx-77",
        "message": "reply from workflow",
    }
