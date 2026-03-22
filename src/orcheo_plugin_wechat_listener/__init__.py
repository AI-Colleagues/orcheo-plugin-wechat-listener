"""WeChat listener plugin built around Tencent's OpenClaw Weixin API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import httpx
from orcheo.listeners.models import (
    ListenerCursor,
    ListenerDispatchMessage,
    ListenerDispatchPayload,
    ListenerHealthSnapshot,
    ListenerSubscription,
)
from orcheo.listeners.registry import ListenerMetadata
from orcheo.nodes.base import TaskNode
from orcheo.nodes.listeners import ListenerNode
from orcheo.nodes.registry import NodeMetadata
from orcheo.plugins import PluginAPI
from pydantic import Field

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"
DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_MS = 15_000
SESSION_EXPIRED_ERRCODE = -14
_CURSOR_METADATA_KEY = "weixin_get_updates_buf"
_LISTENER_NAMESPACE = UUID("ef37fbe8-dd2f-4b16-a7a0-08f361c30d19")


def _optional_string(value: Any) -> str | None:
    """Return a stripped string value or ``None`` when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolved_config_string(value: Any) -> str | None:
    """Return a non-placeholder config string or ``None``."""
    text = _optional_string(value)
    if text is None:
        return None
    if text.startswith("[[") and text.endswith("]]"):
        return None
    return text


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view for dict-like runtime inputs."""
    return value if isinstance(value, Mapping) else {}


def _resolve_openclaw_state_dir(config: Mapping[str, Any]) -> Path:
    """Return the OpenClaw state directory used by the official plugin."""
    explicit = _optional_string(config.get("openclaw_state_dir"))
    if explicit is not None:
        return Path(explicit).expanduser()
    for env_name in ("OPENCLAW_STATE_DIR", "CLAWDBOT_STATE_DIR"):
        env_value = _optional_string(os.environ.get(env_name))
        if env_value is not None:
            return Path(env_value).expanduser()
    return Path.home() / ".openclaw"


def _derive_raw_account_id(normalized_id: str) -> str | None:
    """Mirror OpenClaw's legacy filename compatibility logic."""
    if normalized_id.endswith("-im-bot"):
        return f"{normalized_id[:-7]}@im.bot"
    if normalized_id.endswith("-im-wechat"):
        return f"{normalized_id[:-10]}@im.wechat"
    return None


def _candidate_account_ids(account_id: str) -> tuple[str, ...]:
    """Return candidate OpenClaw account file names for one account id."""
    candidates = [account_id]
    raw_account_id = _derive_raw_account_id(account_id)
    if raw_account_id is not None:
        candidates.append(raw_account_id)
    return tuple(candidates)


def _load_openclaw_account_data(
    account_id: str,
    *,
    state_dir: Path,
) -> dict[str, Any] | None:
    """Load the official OpenClaw account JSON for a Weixin account."""
    accounts_dir = state_dir / "openclaw-weixin" / "accounts"
    for candidate in _candidate_account_ids(account_id):
        account_path = accounts_dir / f"{candidate}.json"
        try:
            payload = json.loads(account_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            logger.warning("Invalid OpenClaw Weixin account file: %s", account_path)
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _load_openclaw_sync_buf(
    account_id: str,
    *,
    state_dir: Path,
) -> str | None:
    """Load the official OpenClaw sync cursor for one Weixin account."""
    accounts_dir = state_dir / "openclaw-weixin" / "accounts"
    for candidate in _candidate_account_ids(account_id):
        sync_path = accounts_dir / f"{candidate}.sync.json"
        try:
            payload = json.loads(sync_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            logger.warning("Invalid OpenClaw Weixin sync file: %s", sync_path)
            continue
        if isinstance(payload, dict):
            sync_buf = _optional_string(payload.get("get_updates_buf"))
            if sync_buf is not None:
                return sync_buf

    legacy_sync_path = (
        state_dir
        / "agents"
        / "default"
        / "sessions"
        / ".openclaw-weixin-sync"
        / "default.json"
    )
    try:
        payload = json.loads(legacy_sync_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        logger.warning("Invalid legacy OpenClaw Weixin sync file: %s", legacy_sync_path)
        return None
    if isinstance(payload, dict):
        return _optional_string(payload.get("get_updates_buf"))
    return None


def _resolve_base_url(config: Mapping[str, Any]) -> str:
    """Resolve the Weixin API base URL from config or OpenClaw account data."""
    configured = _resolved_config_string(config.get("base_url"))
    account_id = _resolved_config_string(config.get("account_id"))
    if account_id is not None and (
        configured is None or configured == DEFAULT_WEIXIN_BASE_URL
    ):
        account_data = _load_openclaw_account_data(
            account_id,
            state_dir=_resolve_openclaw_state_dir(config),
        )
        if isinstance(account_data, Mapping):
            saved_base_url = _optional_string(account_data.get("baseUrl"))
            if saved_base_url is not None:
                return saved_base_url
    if configured is not None:
        return configured
    return DEFAULT_WEIXIN_BASE_URL


def _resolve_bot_token(config: Mapping[str, Any]) -> str | None:
    """Resolve the bearer token from config or an OpenClaw account file."""
    configured = _resolved_config_string(config.get("bot_token") or config.get("token"))
    if configured is not None:
        return configured
    account_id = _resolved_config_string(config.get("account_id"))
    if account_id is None:
        return None
    account_data = _load_openclaw_account_data(
        account_id,
        state_dir=_resolve_openclaw_state_dir(config),
    )
    if not isinstance(account_data, Mapping):
        return None
    return _optional_string(account_data.get("token"))


def get_weixin_long_poll_block_reason(config: Mapping[str, Any]) -> str | None:
    """Return a blocking reason when the Weixin listener is misconfigured."""
    token = _resolve_bot_token(config)
    if token is not None:
        return None
    account_id = _resolved_config_string(config.get("account_id"))
    if account_id is None:
        return (
            "Weixin bot_token is missing. Configure bot_token directly or set "
            "account_id so the plugin can read the OpenClaw account file."
        )
    state_dir = _resolve_openclaw_state_dir(config)
    return (
        f"Weixin token not found for account_id={account_id!r} in "
        f"{state_dir / 'openclaw-weixin' / 'accounts'}."
    )


def _build_headers(*, token: str | None) -> dict[str, str]:
    """Build request headers matching the official OpenClaw Weixin plugin."""
    uint32_text = str(secrets.randbits(32))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uint32_text.encode("utf-8")).decode("utf-8"),
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _post_weixin_json(
    *,
    base_url: str,
    endpoint: str,
    token: str | None,
    body: Mapping[str, Any],
    timeout_ms: int,
) -> dict[str, Any]:
    """POST a JSON request to the OpenClaw Weixin HTTP API."""
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
        response = await client.post(
            url,
            headers=_build_headers(token=token),
            json=body,
        )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


async def get_weixin_updates(
    *,
    base_url: str,
    token: str,
    get_updates_buf: str,
    timeout_ms: int,
) -> dict[str, Any]:
    """Fetch the next OpenClaw Weixin long-poll batch."""
    return await _post_weixin_json(
        base_url=base_url,
        endpoint="ilink/bot/getupdates",
        token=token,
        body={
            "get_updates_buf": get_updates_buf,
            "base_info": {
                "channel_version": (f"orcheo-plugin-wechat-listener/{PLUGIN_VERSION}"),
            },
        },
        timeout_ms=timeout_ms + 5_000,
    )


async def send_weixin_text_message(
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    context_token: str,
    message: str,
) -> None:
    """Send one text message through the OpenClaw Weixin HTTP API."""
    await _post_weixin_json(
        base_url=base_url,
        endpoint="ilink/bot/sendmessage",
        token=token,
        body={
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"orcheo-wechat-{secrets.token_hex(8)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": message},
                    }
                ],
                "context_token": context_token,
            },
            "base_info": {
                "channel_version": (f"orcheo-plugin-wechat-listener/{PLUGIN_VERSION}"),
            },
        },
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
    )


def _extract_text_from_item_list(item_list: Any) -> str | None:
    """Return a plain-text preview for a Weixin item list."""
    if not isinstance(item_list, list):
        return None
    parts: list[str] = []
    for item in item_list:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == 1:
            text_item = item.get("text_item")
            if isinstance(text_item, Mapping):
                text = _optional_string(text_item.get("text"))
                if text is not None:
                    parts.append(text)
        elif item_type == 2:
            parts.append("[Image]")
        elif item_type == 3:
            voice_item = item.get("voice_item")
            if isinstance(voice_item, Mapping):
                transcription = _optional_string(voice_item.get("text"))
                parts.append(transcription or "[Voice]")
            else:
                parts.append("[Voice]")
        elif item_type == 4:
            file_item = item.get("file_item")
            if isinstance(file_item, Mapping):
                file_name = _optional_string(file_item.get("file_name"))
                parts.append(f"[File] {file_name}" if file_name else "[File]")
            else:
                parts.append("[File]")
        elif item_type == 5:
            parts.append("[Video]")
    cleaned = [part for part in parts if part]
    if not cleaned:
        return None
    return "\n".join(cleaned)


def _build_weixin_dedupe_key(message: Mapping[str, Any]) -> str:
    """Return a stable dedupe key for one Weixin message."""
    message_id = message.get("message_id")
    if message_id is not None:
        return f"wechat:message:{message_id}"
    seq = message.get("seq")
    session_id = _optional_string(message.get("session_id")) or "unknown"
    if seq is not None:
        return f"wechat:session:{session_id}:seq:{seq}"
    digest = hashlib.sha256(
        json.dumps(message, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"wechat:hash:{digest}"


def normalize_weixin_message(
    subscription: ListenerSubscription,
    message: Mapping[str, Any],
) -> ListenerDispatchPayload | None:
    """Normalize one OpenClaw Weixin API message."""
    message_type = message.get("message_type")
    if message_type == 2:
        return None

    from_user_id = _optional_string(message.get("from_user_id"))
    if from_user_id is None:
        return None

    account_id = _resolved_config_string(subscription.config.get("account_id"))
    text = _extract_text_from_item_list(message.get("item_list"))
    message_id = message.get("message_id")
    session_id = _optional_string(message.get("session_id"))
    create_time_ms = message.get("create_time_ms")
    item_types = [
        item.get("type")
        for item in message.get("item_list", [])
        if isinstance(item, Mapping)
    ]

    reply_target = {
        "to_user_id": from_user_id,
        "context_token": _optional_string(message.get("context_token")),
        "session_id": session_id,
        "account_id": account_id,
        "base_url": _resolve_base_url(subscription.config),
    }

    return ListenerDispatchPayload(
        platform=subscription.platform,
        event_type="message",
        dedupe_key=_build_weixin_dedupe_key(message),
        bot_identity=subscription.bot_identity_key,
        message=ListenerDispatchMessage(
            chat_id=session_id or from_user_id,
            message_id=str(message_id) if message_id is not None else None,
            user_id=from_user_id,
            text=text,
            chat_type="direct",
            metadata={
                "session_id": session_id,
                "item_types": item_types,
                "message_type": message_type,
                "message_state": message.get("message_state"),
            },
        ),
        reply_target=reply_target,
        raw_event=dict(message),
        metadata={
            "provider": "wechat",
            "transport": "openclaw-http-long-poll",
            "account_id": account_id,
            "create_time_ms": create_time_ms,
            "to_user_id": _optional_string(message.get("to_user_id")),
        },
    )


def normalize_weixin_test_event(
    subscription: ListenerSubscription,
    event: Mapping[str, Any],
    *,
    index: int,
) -> ListenerDispatchPayload:
    """Normalize a fixture event into the shared listener payload."""
    text = _optional_string(event.get("text"))
    message = {
        "seq": event.get("seq", index + 1),
        "message_id": event.get("message_id", index + 1),
        "from_user_id": event.get("from_user_id", f"wechat-user-{index + 1}"),
        "to_user_id": event.get("to_user_id"),
        "session_id": event.get(
            "session_id",
            f"wechat-session-{index + 1}",
        ),
        "message_type": event.get("message_type", 1),
        "message_state": event.get("message_state", 0),
        "context_token": event.get(
            "context_token",
            f"wechat-context-{index + 1}",
        ),
        "item_list": (
            event.get("item_list")
            if isinstance(event.get("item_list"), list)
            else ([{"type": 1, "text_item": {"text": text}}] if text else [])
        ),
    }
    payload = normalize_weixin_message(subscription, message)
    if payload is None:  # pragma: no cover - defensive
        raise ValueError("Failed to normalize Weixin fixture event.")
    return payload


def _build_bot_identity_key(item: Mapping[str, Any]) -> str:
    """Return the stable bot identity used in dispatch payloads."""
    explicit = _optional_string(item.get("bot_identity_key"))
    if explicit is not None:
        return explicit
    account_id = _resolved_config_string(item.get("account_id"))
    if account_id is not None:
        return f"wechat:{account_id}"
    token = _resolved_config_string(item.get("bot_token") or item.get("token"))
    if token is not None:
        return f"wechat:{token[:12]}"
    node_name = _optional_string(item.get("node_name") or item.get("name")) or "default"
    return f"wechat:{node_name}"


def compile_weixin_listener(
    *,
    workflow_id: UUID,
    workflow_version_id: UUID,
    item: dict[str, Any],
    platform_id: str,
) -> ListenerSubscription | None:
    """Compile a Weixin listener subscription with account-aware identity."""
    node_name = _optional_string(item.get("node_name") or item.get("name"))
    if node_name is None:
        return None
    bot_identity_key = _build_bot_identity_key(item)
    subscription_id = uuid5(
        _LISTENER_NAMESPACE,
        f"{workflow_version_id}:{platform_id}:{node_name}:{bot_identity_key}",
    )
    config = {
        key: value
        for key, value in item.items()
        if key not in {"name", "node_name", "type", "platform"}
    }
    return ListenerSubscription(
        id=subscription_id,
        workflow_id=workflow_id,
        workflow_version_id=workflow_version_id,
        node_name=node_name,
        platform=platform_id,
        bot_identity_key=bot_identity_key,
        config=config,
    )


def _cursor_get_updates_buf(cursor: ListenerCursor | None) -> str | None:
    """Read the persisted Weixin long-poll cursor from repository state."""
    if cursor is None:
        return None
    metadata = cursor.metadata
    if not isinstance(metadata, Mapping):
        return None
    return _optional_string(metadata.get(_CURSOR_METADATA_KEY))


async def _save_cursor(
    repository: Any,
    *,
    subscription_id: UUID,
    get_updates_buf: str,
    cursor: ListenerCursor | None,
) -> ListenerCursor:
    """Persist the current Weixin long-poll cursor."""
    metadata = {}
    if cursor is not None and isinstance(cursor.metadata, Mapping):
        metadata = dict(cursor.metadata)
    metadata[_CURSOR_METADATA_KEY] = get_updates_buf
    next_cursor = ListenerCursor(
        subscription_id=subscription_id,
        metadata=metadata,
    )
    return await repository.save_listener_cursor(next_cursor)


async def _wait_or_stop(stop_event: asyncio.Event, *, timeout_seconds: float) -> bool:
    """Wait for a timeout or shutdown signal."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


class WechatListenerPluginNode(ListenerNode):
    """Declare a WeChat listener subscription from an external plugin package."""

    platform: str = "wechat"
    account_id: str = "[[wechat_account_id]]"
    bot_token: str = "[[wechat_bot_token]]"
    base_url: str = DEFAULT_WEIXIN_BASE_URL
    openclaw_state_dir: str = ""
    bootstrap_openclaw_cursor: bool = True
    long_poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS
    test_events: list[dict[str, Any]] = Field(default_factory=list)


class WechatReplyNode(TaskNode):
    """Reply to a WeChat user through the OpenClaw HTTP API."""

    message: str = Field(description="Reply text content")
    to_user_id: str = ""
    context_token: str = ""
    account_id: str = "[[wechat_account_id]]"
    bot_token: str = "[[wechat_bot_token]]"
    base_url: str = DEFAULT_WEIXIN_BASE_URL
    openclaw_state_dir: str = ""
    reply_target: dict[str, Any] | str = Field(default_factory=dict)
    raw_event: dict[str, Any] | str = Field(default_factory=dict)

    async def run(self, state: Any, config: Any) -> dict[str, Any]:
        """Send one text reply."""
        del state, config
        if not self.message.strip():
            raise ValueError("WechatReplyNode.message must not be empty.")

        reply_target = _coerce_mapping(self.reply_target)
        raw_event = _coerce_mapping(self.raw_event)
        node_config = {
            "account_id": self.account_id,
            "bot_token": self.bot_token,
            "base_url": self.base_url,
            "openclaw_state_dir": self.openclaw_state_dir,
        }
        to_user_id = (
            _optional_string(self.to_user_id)
            or _optional_string(reply_target.get("to_user_id"))
            or _optional_string(raw_event.get("from_user_id"))
        )
        if to_user_id is None:
            raise ValueError(
                "WechatReplyNode could not determine to_user_id from config, "
                "reply_target, or raw_event."
            )

        context_token = (
            _optional_string(self.context_token)
            or _optional_string(reply_target.get("context_token"))
            or _optional_string(raw_event.get("context_token"))
        )
        if context_token is None:
            raise ValueError(
                "WechatReplyNode requires a context_token from the listener event."
            )

        token = _resolve_bot_token(node_config)
        if token is None:
            raise ValueError(get_weixin_long_poll_block_reason(node_config))

        await send_weixin_text_message(
            base_url=_resolve_base_url(
                {
                    **node_config,
                    "base_url": (
                        _optional_string(reply_target.get("base_url")) or self.base_url
                    ),
                }
            ),
            token=token,
            to_user_id=to_user_id,
            context_token=context_token,
            message=self.message,
        )
        return {"sent": True, "to_user_id": to_user_id}


class WechatListenerAdapter:
    """Receive WeChat messages through the OpenClaw long-poll API."""

    def __init__(
        self,
        *,
        repository: Any,
        subscription: ListenerSubscription,
        runtime_id: str,
    ) -> None:
        self._repository = repository
        self.subscription = subscription
        self._runtime_id = runtime_id
        self._status = "starting"
        self._detail: str | None = None
        self._last_polled_at: datetime | None = None
        self._last_event_at: datetime | None = None
        self._consecutive_failures = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        """Dispatch fixture events or start long polling until stopped."""
        events = self.subscription.config.get("test_events", [])
        if isinstance(events, list) and events:
            await self._run_fixture_mode(events=events, stop_event=stop_event)
            return
        await self._run_long_poll(stop_event)

    async def _run_fixture_mode(
        self,
        *,
        events: list[Any],
        stop_event: asyncio.Event,
    ) -> None:
        self._status = "healthy"
        self._detail = "running in fixture mode"
        for index, item in enumerate(events):
            if stop_event.is_set():
                break
            event = item if isinstance(item, Mapping) else {"text": str(item)}
            payload = normalize_weixin_test_event(
                self.subscription,
                event,
                index=index,
            )
            await self._repository.dispatch_listener_event(
                self.subscription.id,
                payload,
            )
            self._last_event_at = datetime.now()
        await stop_event.wait()
        self._status = "stopped"

    async def _run_long_poll(self, stop_event: asyncio.Event) -> None:
        block_reason = get_weixin_long_poll_block_reason(self.subscription.config)
        if block_reason is not None:
            self._status = "error"
            self._detail = f"blocked: {block_reason}"
            logger.warning(
                "Weixin listener subscription %s is blocked: %s",
                self.subscription.id,
                block_reason,
            )
            await stop_event.wait()
            self._status = "stopped"
            return

        token = _resolve_bot_token(self.subscription.config)
        if token is None:  # pragma: no cover - defensive
            raise RuntimeError("Weixin token unexpectedly missing after validation.")

        cursor = await self._repository.get_listener_cursor(self.subscription.id)
        get_updates_buf = _cursor_get_updates_buf(cursor) or ""
        if not get_updates_buf and bool(
            self.subscription.config.get("bootstrap_openclaw_cursor", True)
        ):
            account_id = _resolved_config_string(
                self.subscription.config.get("account_id")
            )
            if account_id is not None:
                get_updates_buf = (
                    _load_openclaw_sync_buf(
                        account_id,
                        state_dir=_resolve_openclaw_state_dir(self.subscription.config),
                    )
                    or ""
                )

        base_url = _resolve_base_url(self.subscription.config)
        poll_timeout_ms = int(
            self.subscription.config.get(
                "long_poll_timeout_ms",
                DEFAULT_LONG_POLL_TIMEOUT_MS,
            )
        )

        while not stop_event.is_set():
            try:
                response = await self._poll_once_or_stop(
                    stop_event,
                    base_url=base_url,
                    token=token,
                    get_updates_buf=get_updates_buf,
                    timeout_ms=poll_timeout_ms,
                )
                if response is None:
                    break

                self._last_polled_at = datetime.now()
                next_timeout_ms = response.get("longpolling_timeout_ms")
                if isinstance(next_timeout_ms, int | float) and next_timeout_ms > 0:
                    poll_timeout_ms = int(next_timeout_ms)

                ret = response.get("ret")
                errcode = response.get("errcode")
                if (ret not in (None, 0)) or (errcode not in (None, 0)):
                    await self._handle_api_error(
                        stop_event,
                        response=response,
                    )
                    continue

                raw_cursor = response.get("get_updates_buf")
                new_get_updates_buf = (
                    _optional_string(raw_cursor)
                    if raw_cursor is not None
                    else get_updates_buf
                )
                if new_get_updates_buf != get_updates_buf:
                    cursor = await _save_cursor(
                        self._repository,
                        subscription_id=self.subscription.id,
                        get_updates_buf=new_get_updates_buf,
                        cursor=cursor,
                    )
                    get_updates_buf = new_get_updates_buf

                for message in response.get("msgs", []):
                    if not isinstance(message, Mapping):
                        continue
                    payload = normalize_weixin_message(self.subscription, message)
                    if payload is None:
                        continue
                    await self._repository.dispatch_listener_event(
                        self.subscription.id,
                        payload,
                    )
                    self._last_event_at = datetime.now()

                self._status = "healthy"
                self._detail = None
                self._consecutive_failures = 0
            except asyncio.CancelledError:  # pragma: no cover - task cancellation
                raise
            except Exception as exc:
                self._consecutive_failures += 1
                self._status = "backoff"
                self._detail = str(exc)
                backoff_seconds = min(
                    30.0,
                    max(1.0, float(self._consecutive_failures)),
                )
                if await _wait_or_stop(
                    stop_event,
                    timeout_seconds=backoff_seconds,
                ):
                    break
        self._status = "stopped"

    async def _poll_once_or_stop(
        self,
        stop_event: asyncio.Event,
        *,
        base_url: str,
        token: str,
        get_updates_buf: str,
        timeout_ms: int,
    ) -> dict[str, Any] | None:
        """Return one long-poll response or ``None`` when shutting down."""
        poll_task = asyncio.create_task(
            get_weixin_updates(
                base_url=base_url,
                token=token,
                get_updates_buf=get_updates_buf,
                timeout_ms=timeout_ms,
            )
        )
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {poll_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if stop_task in done:
            await asyncio.gather(*pending, return_exceptions=True)
            return None
        await asyncio.gather(*pending, return_exceptions=True)
        return await poll_task

    async def _handle_api_error(
        self,
        stop_event: asyncio.Event,
        *,
        response: Mapping[str, Any],
    ) -> None:
        """Handle a non-success OpenClaw API response."""
        errcode = response.get("errcode")
        ret = response.get("ret")
        errmsg = _optional_string(response.get("errmsg")) or "unknown OpenClaw error"
        if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
            self._status = "backoff"
            self._detail = (
                "Weixin session expired in OpenClaw (errcode -14); "
                "waiting before retry."
            )
            await _wait_or_stop(
                stop_event,
                timeout_seconds=float(
                    self.subscription.config.get("session_pause_seconds", 3600)
                ),
            )
            return
        raise RuntimeError(
            f"Weixin getUpdates failed: ret={ret}, errcode={errcode}, errmsg={errmsg}"
        )

    def health(self) -> ListenerHealthSnapshot:
        """Return the current adapter health snapshot."""
        return ListenerHealthSnapshot(
            subscription_id=self.subscription.id,
            runtime_id=self._runtime_id,
            status=self._status,
            platform=self.subscription.platform,
            last_polled_at=self._last_polled_at,
            last_event_at=self._last_event_at,
            consecutive_failures=self._consecutive_failures,
            detail=self._detail,
        )


WeixinListenerPluginNode = WechatListenerPluginNode
WeixinReplyNode = WechatReplyNode
WeixinListenerAdapter = WechatListenerAdapter


class WechatListenerPlugin:
    """Plugin entry point for the WeChat listener package."""

    def register(self, api: PluginAPI) -> None:
        """Register the WeChat listener node, reply node, and adapter."""
        api.register_node(
            NodeMetadata(
                name="WechatListenerPluginNode",
                description="Receive WeChat events through the plugin contract.",
                category="trigger",
            ),
            WechatListenerPluginNode,
        )
        api.register_node(
            NodeMetadata(
                name="WeixinListenerPluginNode",
                description="Backwards-compatible alias for WechatListenerPluginNode.",
                category="trigger",
            ),
            WechatListenerPluginNode,
        )
        api.register_node(
            NodeMetadata(
                name="WechatReplyNode",
                description="Reply to WeChat messages through OpenClaw HTTP APIs.",
                category="messaging",
            ),
            WechatReplyNode,
        )
        api.register_node(
            NodeMetadata(
                name="WeixinReplyNode",
                description="Backwards-compatible alias for WechatReplyNode.",
                category="messaging",
            ),
            WechatReplyNode,
        )
        api.register_listener(
            ListenerMetadata(
                id="wechat",
                display_name="WeChat Listener",
                description="Receive WeChat messages through OpenClaw long polling.",
            ),
            compile_weixin_listener,
            lambda *, repository, subscription, runtime_id: WechatListenerAdapter(
                repository=repository,
                subscription=subscription,
                runtime_id=runtime_id,
            ),
        )
        api.register_listener(
            ListenerMetadata(
                id="weixin",
                display_name="WeChat Listener",
                description="Backwards-compatible alias for the WeChat listener.",
            ),
            compile_weixin_listener,
            lambda *, repository, subscription, runtime_id: WechatListenerAdapter(
                repository=repository,
                subscription=subscription,
                runtime_id=runtime_id,
            ),
        )


plugin = WechatListenerPlugin()
