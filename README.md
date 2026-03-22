# orcheo-plugin-wechat-listener

An Orcheo plugin that connects WeChat to Orcheo workflows through the
OpenClaw WeChat long-poll HTTP API. Incoming messages are normalized into the
shared `ListenerDispatchPayload` contract and dispatched to workflows that
subscribe to the `wechat` listener platform.

This package is based on the OpenClaw WeChat plugin flow published by Tencent:

- `@tencent-weixin/openclaw-weixin-cli@latest`
- `@tencent-weixin/openclaw-weixin`

As of 2026-03-22, the npm `latest` version for both packages is `1.0.2`.

## What this plugin provides

| Component | Name | Description |
|---|---|---|
| Node | `WechatListenerPluginNode` | Declares a WeChat listener subscription in a workflow |
| Node | `WechatReplyNode` | Sends a text reply through the same OpenClaw WeChat backend |
| Listener | `wechat` | Runtime adapter that manages the long-poll session |

## Requirements

- Python 3.12+
- Orcheo backend with the listener runtime running
- A WeChat account connected through Tencent's OpenClaw WeChat channel

## Installation

Install the Orcheo plugin:

```bash
orcheo plugin install ./packages/plugins/wechat_listener
```

Then restart the backend and worker processes:

```bash
docker compose restart backend worker
```

## Connect WeChat Through The Plugin Command

The plugin ships its own bootstrap command:

```bash
orcheo-wechat-plugin-login
```

When working from this repository, you can run it without a global install via:

```bash
uv run --project packages/plugins/wechat_listener orcheo-wechat-plugin-login
```

That command:

- fetches a Tencent WeChat QR code
- waits for scan + confirmation
- writes `wechat_account_id`, `wechat_bot_token`, and `wechat_base_url`
  into the Orcheo credential vault when `ORCHEO_API_URL` and
  `ORCHEO_SERVICE_TOKEN` are configured
- otherwise prints the exact `orcheo credential create ...` commands to run

Example:

```bash
ORCHEO_API_URL=http://localhost:8000 \
ORCHEO_SERVICE_TOKEN=your-token \
uv run --project packages/plugins/wechat_listener \
  orcheo-wechat-plugin-login \
  --workflow-id <workflow-id>
```

## Connect WeChat Through OpenClaw

Tencent's official quick installer is:

```bash
npx -y @tencent-weixin/openclaw-weixin-cli@latest install
```

That flow installs `@tencent-weixin/openclaw-weixin`, opens the QR-code login,
and stores the resulting token and account files under the OpenClaw state
directory (normally `~/.openclaw/openclaw-weixin/accounts/`).

This Orcheo plugin can also reuse those saved credentials directly. In the
common case, you only need to configure the `account_id` that OpenClaw created.

## Configuration

Add a `WechatListenerPluginNode` to your workflow and configure either:

| Field | Description |
|---|---|
| `account_id` | OpenClaw WeChat account ID; the plugin loads token/base URL from the local OpenClaw state |
| `bot_token` | Explicit bearer token override if you do not want to read the OpenClaw account file |
| `base_url` | OpenClaw WeChat API base URL, default `https://ilinkai.weixin.qq.com` |
| `openclaw_state_dir` | Optional override for the OpenClaw state directory |

Credentials can still be interpolated from the Orcheo credential vault with the
`[[credential_key]]` syntax.

If the listener starts without a saved Orcheo cursor, it can bootstrap from the
OpenClaw `get_updates_buf` sync file for the same account, which helps avoid
replaying old messages after migration.

## Replying To Messages

Use `WechatReplyNode` to send a plain-text reply. The node expects the inbound
`reply_target` or `raw_event` from the listener payload so it can recover the
`to_user_id` and `context_token` required by the OpenClaw WeChat API.

## Development

```bash
cd packages/plugins/wechat_listener
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

## Further Reading

- [Plugin Reference](https://orcheo.readthedocs.io/custom_nodes_and_tools/)
- [CLI Reference](https://orcheo.readthedocs.io/cli_reference/)
- [OpenClaw WeChat plugin on npm](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)
