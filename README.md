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

## Quick Start (Docker Stack / Canvas)

If you are using the repository Docker stack and Canvas, the plugin, workflow,
credentials, backend, and worker must all point at the same backend.

1. [Install the plugin](#installation) into the stack runtime (use `--runtime stack`).
2. In Canvas, create a workflow from the `WeChat Private Listener` template.
3. [Connect the WeChat account](#connect-wechat-through-the-plugin-command) and
   create the workflow-scoped credentials. Scan the QR code in WeChat, then run
   the printed `orcheo credential create` commands against the same
   `ORCHEO_API_URL`.
4. Verify the workflow is ready, then send a real WeChat message:

   ```bash
   ORCHEO_API_URL=http://localhost:8000 \
   orcheo workflow show <workflow-id>
   ```

   `credential_readiness.status` should be `ready`.

## Installation

Install the Orcheo plugin from the Git repository into the same runtime as your
backend:

```bash
orcheo plugin install \
  'git+https://github.com/AI-Colleagues/orcheo-plugin-wechat-listener.git'
```

For the repository Docker stack, include `--runtime stack` and restart:

```bash
orcheo plugin install \
  'git+https://github.com/AI-Colleagues/orcheo-plugin-wechat-listener.git' \
  --runtime stack
docker compose restart backend worker
```

## Connect WeChat Through The Plugin Command

The plugin ships a bootstrap command that:

- fetches a Tencent WeChat QR code
- waits for scan + confirmation
- writes `wechat_account_id`, `wechat_bot_token`, and `wechat_base_url`
  into the Orcheo credential vault when `ORCHEO_API_URL` and
  `ORCHEO_SERVICE_TOKEN` are configured
- otherwise prints the exact `orcheo credential create ...` commands to run

Run it directly from GitHub (no clone required):

```bash
uvx --from 'orcheo-plugin-wechat-listener @ git+https://github.com/AI-Colleagues/orcheo-plugin-wechat-listener.git' \
  orcheo-wechat-plugin-login \
  --workflow-id <workflow-id> \
  --access private \
  --print-commands
```

Or from a local checkout of this repository:

```bash
uv run --project packages/plugins/wechat_listener \
  orcheo-wechat-plugin-login \
  --workflow-id <workflow-id> \
  --access private \
  --print-commands
```

To write credentials directly to the vault instead of printing commands, set
`ORCHEO_API_URL` and `ORCHEO_SERVICE_TOKEN` and omit `--print-commands`.

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

For Canvas-backed workflows, create those credentials in the same backend that
Canvas is configured to use. In this repository, that usually means matching
`ORCHEO_API_URL` to `VITE_ORCHEO_BACKEND_URL`.

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
