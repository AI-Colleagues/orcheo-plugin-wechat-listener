"""Tests for the plugin-scoped WeChat login command."""

from __future__ import annotations

import pytest

from orcheo_plugin_wechat_listener import login


def test_wait_for_weixin_login_refreshes_expired_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshes: list[tuple[str, int]] = []
    responses = iter(
        [
            {"status": "expired"},
            {
                "status": "confirmed",
                "ilink_bot_id": "wx-account",
                "bot_token": "token-123",
                "baseurl": "https://custom.weixin.test",
                "ilink_user_id": "user-1",
            },
        ]
    )

    monkeypatch.setattr(
        login,
        "_poll_weixin_qr_status",
        lambda **_: next(responses),
    )
    monkeypatch.setattr(
        login,
        "start_weixin_qr_session",
        lambda **_: login.WeixinQrCode(
            qrcode="qr-2",
            qr_content="https://weixin.qq.com/qr/2",
        ),
    )

    result = login.wait_for_weixin_login(
        login.WeixinQrCode(qrcode="qr-1", qr_content="https://weixin.qq.com/qr/1"),
        on_qr_refresh=lambda qr_code, count: refreshes.append((qr_code.qrcode, count)),
        sleep_fn=lambda _: None,
    )

    assert result.account_id == "wx-account"
    assert result.bot_token == "token-123"
    assert result.base_url == "https://custom.weixin.test"
    assert result.user_id == "user-1"
    assert refreshes == [("qr-2", 1)]


def test_upsert_orcheo_credentials_creates_and_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        login,
        "_list_orcheo_credentials",
        lambda **_: [{"id": "cred-1", "name": "wechat_account_id"}],
    )
    created: list[str] = []
    updated: list[str] = []

    monkeypatch.setattr(
        login,
        "_create_orcheo_credential",
        lambda **kwargs: (
            created.append(kwargs["payload"]["name"])
            or {
                "id": f"new-{kwargs['payload']['name']}",
                "name": kwargs["payload"]["name"],
            }
        ),
    )
    monkeypatch.setattr(
        login,
        "_update_orcheo_credential",
        lambda **kwargs: (
            updated.append(kwargs["payload"]["name"])
            or {"id": kwargs["credential_id"], "name": kwargs["payload"]["name"]}
        ),
    )

    result = login.upsert_orcheo_credentials(
        api_url="http://api.test",
        service_token="token",
        workflow_id=None,
        access="public",
        actor="tester",
        credentials=[
            {
                "name": "wechat_account_id",
                "provider": "wechat",
                "secret": "wx-account",
            },
            {
                "name": "wechat_bot_token",
                "provider": "wechat",
                "secret": "token-123",
            },
        ],
    )

    assert updated == ["wechat_account_id"]
    assert created == ["wechat_bot_token"]
    assert [item["action"] for item in result] == ["updated", "created"]


def test_run_login_prints_manual_commands_when_api_config_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        login,
        "start_weixin_qr_session",
        lambda **_: login.WeixinQrCode(
            qrcode="qr-1",
            qr_content="https://weixin.qq.com/qr/1",
        ),
    )
    monkeypatch.setattr(
        login,
        "wait_for_weixin_login",
        lambda *_args, **_kwargs: login.WeixinLoginResult(
            account_id="wx-account",
            bot_token="token-123",
            base_url="https://ilinkai.weixin.qq.com",
        ),
    )
    monkeypatch.setattr(login, "_print_qr", lambda _qr: None)

    exit_code = login.run_login(
        [
            "--print-commands",
            "--access",
            "public",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "WeChat login completed." in captured.out
    assert "orcheo credential create wechat_account_id" in captured.out


def test_run_login_saves_credentials_when_api_config_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        login,
        "start_weixin_qr_session",
        lambda **_: login.WeixinQrCode(
            qrcode="qr-1",
            qr_content="https://weixin.qq.com/qr/1",
        ),
    )
    monkeypatch.setattr(
        login,
        "wait_for_weixin_login",
        lambda *_args, **_kwargs: login.WeixinLoginResult(
            account_id="wx-account",
            bot_token="token-123",
            base_url="https://ilinkai.weixin.qq.com",
        ),
    )
    monkeypatch.setattr(login, "_print_qr", lambda _qr: None)
    monkeypatch.setattr(
        login,
        "upsert_orcheo_credentials",
        lambda **_: [
            {
                "action": "created",
                "credential": {"id": "cred-1", "name": "wechat_account_id"},
            },
            {
                "action": "created",
                "credential": {"id": "cred-2", "name": "wechat_bot_token"},
            },
            {
                "action": "created",
                "credential": {"id": "cred-3", "name": "wechat_base_url"},
            },
        ],
    )

    exit_code = login.run_login(
        [
            "--api-url",
            "http://api.test",
            "--service-token",
            "token",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "credentials were saved" in captured.out
    assert "created: wechat_account_id (cred-1)" in captured.out
