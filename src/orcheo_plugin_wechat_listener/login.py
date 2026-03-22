"""Console login flow for the WeChat listener plugin."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from qrcode import QRCode

DEFAULT_ORCHEO_API_URL = "http://localhost:8000"
DEFAULT_WEIXIN_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WEIXIN_BOT_TYPE = "3"
DEFAULT_WEIXIN_LOGIN_TIMEOUT_SECONDS = 480.0
DEFAULT_WEIXIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_WEIXIN_LONG_POLL_TIMEOUT_SECONDS = 35.0
DEFAULT_WEIXIN_MAX_QR_REFRESHES = 3


class WeixinPluginLoginError(RuntimeError):
    """Raised when the plugin login flow cannot complete."""


@dataclass(slots=True)
class WeixinQrCode:
    """Active QR-code session details."""

    qrcode: str
    qr_content: str


@dataclass(slots=True)
class WeixinLoginResult:
    """Resolved Weixin login result."""

    account_id: str
    bot_token: str
    base_url: str
    user_id: str | None = None


def _optional_string(value: Any) -> str | None:
    """Return a stripped string or ``None`` when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_weixin_headers(
    *,
    route_tag: str | None = None,
    client_version: bool = False,
) -> dict[str, str]:
    """Build headers for the Tencent Weixin QR endpoints."""
    headers: dict[str, str] = {}
    if route_tag:
        headers["SKRouteTag"] = route_tag
    if client_version:
        headers["iLink-App-ClientVersion"] = "1"
    return headers


def _build_url(base_url: str, path: str, params: dict[str, str]) -> str:
    """Build a request URL with query params."""
    normalized_base = base_url.rstrip("/")
    return f"{normalized_base}/{path.lstrip('/')}?{urlencode(params)}"


def _parse_json_response(response: httpx.Response, *, action: str) -> dict[str, Any]:
    """Validate and parse a JSON response body."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeixinPluginLoginError(f"Weixin {action} returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise WeixinPluginLoginError(f"Weixin {action} returned an unexpected payload.")
    return payload


def _get_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    action: str,
) -> dict[str, Any]:
    """Issue a GET request and return a JSON object."""
    try:
        response = httpx.get(url, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise WeixinPluginLoginError(
            f"Weixin {action} failed with HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise WeixinPluginLoginError(f"Weixin {action} failed: {exc}") from exc
    return _parse_json_response(response, action=action)


def start_weixin_qr_session(
    *,
    api_base_url: str = DEFAULT_WEIXIN_API_BASE_URL,
    bot_type: str = DEFAULT_WEIXIN_BOT_TYPE,
    route_tag: str | None = None,
) -> WeixinQrCode:
    """Fetch a QR code for a new Weixin login session."""
    payload = _get_json(
        _build_url(
            api_base_url,
            "ilink/bot/get_bot_qrcode",
            {"bot_type": bot_type},
        ),
        headers=_build_weixin_headers(route_tag=route_tag),
        timeout_seconds=30.0,
        action="QR start",
    )
    qrcode = _optional_string(payload.get("qrcode"))
    qr_content = _optional_string(payload.get("qrcode_img_content"))
    if qrcode is None or qr_content is None:
        raise WeixinPluginLoginError(
            "Weixin QR start response was missing QR-code fields."
        )
    return WeixinQrCode(qrcode=qrcode, qr_content=qr_content)


def _poll_weixin_qr_status(
    *,
    api_base_url: str,
    qrcode: str,
    route_tag: str | None = None,
) -> dict[str, Any]:
    """Fetch one QR-login status response."""
    return _get_json(
        _build_url(
            api_base_url,
            "ilink/bot/get_qrcode_status",
            {"qrcode": qrcode},
        ),
        headers=_build_weixin_headers(route_tag=route_tag, client_version=True),
        timeout_seconds=DEFAULT_WEIXIN_LONG_POLL_TIMEOUT_SECONDS,
        action="QR status poll",
    )


def _resolve_confirmed_login(payload: dict[str, Any]) -> WeixinLoginResult:
    """Build a login result from a confirmed status payload."""
    account_id = _optional_string(payload.get("ilink_bot_id"))
    bot_token = _optional_string(payload.get("bot_token"))
    base_url = _optional_string(payload.get("baseurl")) or DEFAULT_WEIXIN_API_BASE_URL
    user_id = _optional_string(payload.get("ilink_user_id"))
    if account_id is None or bot_token is None:
        raise WeixinPluginLoginError(
            "Weixin login was confirmed but the server did not return "
            "account credentials."
        )
    return WeixinLoginResult(
        account_id=account_id,
        bot_token=bot_token,
        base_url=base_url,
        user_id=user_id,
    )


def _refresh_qr_code(
    *,
    api_base_url: str,
    bot_type: str,
    route_tag: str | None,
    refresh_count: int,
    max_qr_refreshes: int,
    on_qr_refresh: Any | None,
) -> WeixinQrCode:
    """Return a refreshed QR code or raise when the refresh budget is exhausted."""
    if refresh_count > max_qr_refreshes:
        raise WeixinPluginLoginError(
            "Weixin login timed out after the QR code expired repeatedly."
        )
    refreshed = start_weixin_qr_session(
        api_base_url=api_base_url,
        bot_type=bot_type,
        route_tag=route_tag,
    )
    if callable(on_qr_refresh):
        on_qr_refresh(refreshed, refresh_count)
    return refreshed


def wait_for_weixin_login(
    initial_qr: WeixinQrCode,
    *,
    api_base_url: str = DEFAULT_WEIXIN_API_BASE_URL,
    bot_type: str = DEFAULT_WEIXIN_BOT_TYPE,
    route_tag: str | None = None,
    timeout_seconds: float = DEFAULT_WEIXIN_LOGIN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_WEIXIN_POLL_INTERVAL_SECONDS,
    max_qr_refreshes: int = DEFAULT_WEIXIN_MAX_QR_REFRESHES,
    on_scan: Any | None = None,
    on_qr_refresh: Any | None = None,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> WeixinLoginResult:
    """Poll until the user confirms the Weixin QR login."""
    active_qr = initial_qr
    deadline = monotonic_fn() + max(timeout_seconds, 1.0)
    refresh_count = 0
    scanned_reported = False

    while monotonic_fn() < deadline:
        payload = _poll_weixin_qr_status(
            api_base_url=api_base_url,
            qrcode=active_qr.qrcode,
            route_tag=route_tag,
        )
        status = _optional_string(payload.get("status"))
        if status is None:
            raise WeixinPluginLoginError(
                "Weixin QR status response was missing the status field."
            )

        if status == "wait":
            sleep_fn(poll_interval_seconds)
            continue

        if status == "scaned":
            if not scanned_reported and callable(on_scan):
                on_scan()
            scanned_reported = True
            sleep_fn(poll_interval_seconds)
            continue

        if status == "expired":
            refresh_count += 1
            active_qr = _refresh_qr_code(
                api_base_url=api_base_url,
                bot_type=bot_type,
                route_tag=route_tag,
                refresh_count=refresh_count,
                max_qr_refreshes=max_qr_refreshes,
                on_qr_refresh=on_qr_refresh,
            )
            scanned_reported = False
            continue

        if status == "confirmed":
            return _resolve_confirmed_login(payload)

        raise WeixinPluginLoginError(f"Unsupported Weixin QR status: {status}")

    raise WeixinPluginLoginError("Weixin login timed out before confirmation.")


def _render_qr_content(content: str) -> str:
    """Render QR content as a terminal-friendly block grid."""
    qr = QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    return "\n".join("".join("██" if cell else "  " for cell in row) for row in matrix)


def _build_orcheo_headers(service_token: str) -> dict[str, str]:
    """Build authenticated request headers for the Orcheo API."""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {service_token}",
    }


def _list_orcheo_credentials(
    *,
    api_url: str,
    service_token: str,
    workflow_id: str | None,
) -> list[dict[str, Any]]:
    """Return credentials visible to the caller."""
    params = {"workflow_id": workflow_id} if workflow_id else None
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/api/credentials",
            params=params,
            headers=_build_orcheo_headers(service_token),
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise WeixinPluginLoginError(
            f"Failed to list Orcheo credentials: HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise WeixinPluginLoginError(f"Failed to reach the Orcheo API: {exc}") from exc
    payload = response.json()
    if not isinstance(payload, list):
        raise WeixinPluginLoginError(
            "Orcheo credentials list returned an unexpected payload."
        )
    return [item for item in payload if isinstance(item, dict)]


def _create_orcheo_credential(
    *,
    api_url: str,
    service_token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create one Orcheo credential."""
    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/credentials",
            json=payload,
            headers=_build_orcheo_headers(service_token),
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise WeixinPluginLoginError(
            f"Failed to create Orcheo credential '{payload['name']}': "
            f"HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise WeixinPluginLoginError(f"Failed to reach the Orcheo API: {exc}") from exc
    result = response.json()
    if not isinstance(result, dict):
        raise WeixinPluginLoginError(
            "Orcheo create credential returned an unexpected payload."
        )
    return result


def _update_orcheo_credential(
    *,
    api_url: str,
    service_token: str,
    credential_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Update one Orcheo credential."""
    try:
        response = httpx.patch(
            f"{api_url.rstrip('/')}/api/credentials/{credential_id}",
            json=payload,
            headers=_build_orcheo_headers(service_token),
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        credential_name = payload.get("name", credential_id)
        raise WeixinPluginLoginError(
            f"Failed to update Orcheo credential '{credential_name}': "
            f"HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise WeixinPluginLoginError(f"Failed to reach the Orcheo API: {exc}") from exc
    result = response.json()
    if not isinstance(result, dict):
        raise WeixinPluginLoginError(
            "Orcheo update credential returned an unexpected payload."
        )
    return result


def upsert_orcheo_credentials(
    *,
    api_url: str,
    service_token: str,
    workflow_id: str | None,
    access: str,
    actor: str,
    credentials: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Create or update credential secrets in the Orcheo vault."""
    existing = _list_orcheo_credentials(
        api_url=api_url,
        service_token=service_token,
        workflow_id=workflow_id,
    )
    existing_by_name = {str(item.get("name", "")).casefold(): item for item in existing}
    results: list[dict[str, Any]] = []

    for item in credentials:
        name = item["name"]
        base_payload: dict[str, Any] = {
            "actor": actor,
            "name": name,
            "provider": item["provider"],
            "secret": item["secret"],
            "access": access,
        }
        if workflow_id is not None:
            base_payload["workflow_id"] = workflow_id

        current = existing_by_name.get(name.casefold())
        if current is None:
            create_payload = dict(base_payload)
            create_payload["scopes"] = []
            create_payload["kind"] = "secret"
            credential = _create_orcheo_credential(
                api_url=api_url,
                service_token=service_token,
                payload=create_payload,
            )
            results.append({"action": "created", "credential": credential})
            continue

        credential = _update_orcheo_credential(
            api_url=api_url,
            service_token=service_token,
            credential_id=str(current["id"]),
            payload=base_payload,
        )
        results.append({"action": "updated", "credential": credential})

    return results


def _format_credential_commands(
    *,
    workflow_id: str | None,
    access: str,
    api_url: str | None,
    credentials: list[dict[str, str]],
) -> list[str]:
    """Return manual Orcheo credential-create commands."""
    commands: list[str] = []
    for item in credentials:
        parts = ["orcheo"]
        if api_url:
            parts.extend(["--api-url", api_url])
        parts.extend(
            [
                "credential",
                "create",
                item["name"],
                "--provider",
                item["provider"],
                "--secret",
                item["secret"],
                "--access",
                access,
            ]
        )
        if workflow_id is not None:
            parts.extend(["--workflow-id", workflow_id])
        commands.append(" ".join(shlex.quote(part) for part in parts))
    return commands


def _print_manual_commands(commands: list[str]) -> None:
    """Print manual credential creation commands."""
    print("Run these commands to save the WeChat credentials in Orcheo:")
    print("Ensure ORCHEO_SERVICE_TOKEN is set in your shell before running them.")
    for command in commands:
        print(command)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse plugin login CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="orcheo-wechat-plugin-login",
        description=(
            "Retrieve Tencent WeChat credentials and save them into the Orcheo vault."
        ),
    )
    parser.add_argument("--api-url", default=os.getenv("ORCHEO_API_URL"))
    parser.add_argument("--service-token", default=os.getenv("ORCHEO_SERVICE_TOKEN"))
    parser.add_argument("--workflow-id")
    parser.add_argument(
        "--access",
        default="public",
        choices=("private", "shared", "public"),
        help="Credential access level. Defaults to public.",
    )
    parser.add_argument("--account-name", default="wechat_account_id")
    parser.add_argument("--token-name", default="wechat_bot_token")
    parser.add_argument("--base-url-name", default="wechat_base_url")
    parser.add_argument("--api-base-url", default=DEFAULT_WEIXIN_API_BASE_URL)
    parser.add_argument("--bot-type", default=DEFAULT_WEIXIN_BOT_TYPE)
    parser.add_argument("--route-tag")
    parser.add_argument("--timeout-seconds", type=float, default=480.0)
    parser.add_argument("--actor", default="cli")
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print manual Orcheo credential commands instead of calling the API.",
    )
    return parser.parse_args(argv)


def _print_qr(qr_code: WeixinQrCode) -> None:
    """Print the QR code and its raw content."""
    print(_render_qr_content(qr_code.qr_content))
    print(qr_code.qr_content)


def _handle_qr_refresh(qr_code: WeixinQrCode, refresh_count: int) -> None:
    """Print a refreshed QR code."""
    print(f"QR code expired. Refreshing ({refresh_count}/3). Scan the new code.")
    _print_qr(qr_code)


def run_login(argv: list[str] | None = None) -> int:
    """Run the plugin login command."""
    args = _parse_args(argv)

    if args.access in {"private", "shared"} and args.workflow_id is None:
        raise WeixinPluginLoginError(
            "--workflow-id is required when --access is private or shared."
        )

    print("Fetching a WeChat QR code. Scan it in WeChat and confirm the login.")
    initial_qr = start_weixin_qr_session(
        api_base_url=args.api_base_url,
        bot_type=args.bot_type,
        route_tag=args.route_tag,
    )
    _print_qr(initial_qr)

    login = wait_for_weixin_login(
        initial_qr,
        api_base_url=args.api_base_url,
        bot_type=args.bot_type,
        route_tag=args.route_tag,
        timeout_seconds=args.timeout_seconds,
        on_scan=lambda: print("QR code scanned. Confirm the login in WeChat."),
        on_qr_refresh=_handle_qr_refresh,
    )

    credential_specs = [
        {
            "name": args.account_name,
            "provider": "wechat",
            "secret": login.account_id,
        },
        {
            "name": args.token_name,
            "provider": "wechat",
            "secret": login.bot_token,
        },
        {
            "name": args.base_url_name,
            "provider": "wechat",
            "secret": login.base_url,
        },
    ]

    manual_commands = _format_credential_commands(
        workflow_id=args.workflow_id,
        access=args.access,
        api_url=args.api_url,
        credentials=credential_specs,
    )

    if args.print_commands or not args.api_url or not args.service_token:
        print("WeChat login completed.")
        _print_manual_commands(manual_commands)
        return 0

    try:
        updates = upsert_orcheo_credentials(
            api_url=args.api_url,
            service_token=args.service_token,
            workflow_id=args.workflow_id,
            access=args.access,
            actor=args.actor,
            credentials=credential_specs,
        )
    except WeixinPluginLoginError as exc:
        print(str(exc), file=sys.stderr)
        _print_manual_commands(manual_commands)
        return 1

    print("WeChat login completed and credentials were saved.")
    for item in updates:
        credential = item["credential"]
        print(
            f"{item['action']}: {credential.get('name')} "
            f"({credential.get('id', 'unknown-id')})"
        )
    return 0


def main() -> None:
    """Run the console entrypoint."""
    try:
        raise SystemExit(run_login())
    except WeixinPluginLoginError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = [
    "WeixinLoginResult",
    "WeixinPluginLoginError",
    "WeixinQrCode",
    "main",
    "run_login",
    "start_weixin_qr_session",
    "upsert_orcheo_credentials",
    "wait_for_weixin_login",
]
