# China Channels Architecture

## Overview

G3KU runs a single China communication runtime with these rules:

- Python remains the only agent brain.
- `subsystems/china_channels_host` is the only Node communication host.
- Supported canonical channel ids are `qqbot`, `dingtalk`, `wecom`, `wecom-app`, `wecom-kf`, `wechat-mp`, and `feishu-china`.
- `feishu-china` is supported as a frozen compatibility channel; new development should prefer the other six channels.

The previous Python multi-channel framework under `g3ku/channels/` is retired.

## Runtime Boundary

### Python owns

- session lifecycle
- task execution and cancellation
- memory and governance
- bridge supervision, health, and config persistence
- admin APIs, doctor/status output, and UI management

### Node owns

- platform SDKs and authentication
- webhook and websocket ingress
- platform-specific outbound delivery
- inbound normalization into the internal bridge protocol

## Source Layout

`subsystems/china_channels_host` is split into two layers:

- `src/vendor/*`: direct upstream runtime files mirrored from `openclaw-china-2026.3.22`
- `src/*.ts` and thin wrapper files: G3KU-native host, config loader, registry reader, protocol, and runtime bridge

The vendor layer should stay as close to upstream as possible. Any G3KU-specific behavior belongs in the native layer.

## Configuration

The only supported config path is `chinaBridge.channels.<channel-id>`, where `<channel-id>` is one of:

- `qqbot`
- `dingtalk`
- `wecom`
- `wecom-app`
- `wecom-kf`
- `wechat-mp`
- `feishu-china`

Top-level `channels.*` remains unsupported. G3KU may migrate legacy key names such as `wecomApp` or `feishuChina` on load, but it always writes canonical ids back to config.

When `chinaBridge.enabled=true` and `chinaBridge.autoStart=true`, G3KU builds and launches `subsystems/china_channels_host` automatically.

## Control Flow

### Inbound

1. A platform sends a message to the Node host.
2. The host normalizes it into `inbound_message`.
3. The frame is forwarded to Python over the internal control WebSocket.
4. Python resolves the destination session and calls `SessionRuntimeBridge.prompt(...)`.

### Outbound

1. Python emits progress, tool hints, and final replies.
2. `ChinaBridgeTransport` encodes them as `deliver_message`.
3. The Node host delivers them through the matching upstream sender.

## Session Keys

Python generates all China channel session keys with this format:

- DM: `china:{channel}:{account_id}:dm:{peer_id}`
- Group: `china:{channel}:{account_id}:group:{peer_id}`
- Thread: append `:thread:{thread_id}`

The `channel` segment always uses the canonical id, including hyphenated ids such as `wecom-app`, `wecom-kf`, and `wechat-mp`.

## Operational Interfaces

- CLI: `g3ku china-bridge status`
- CLI: `g3ku china-bridge doctor`
- CLI: `g3ku china-bridge restart`
- API: `GET /api/china-bridge/status`
- API: `GET /api/china-bridge/doctor`
- API: `GET /api/china-bridge/channels`
- API: `PUT /api/china-bridge/channels/{channel_id}`
- API: `POST /api/china-bridge/channels/{channel_id}/test`

## Maintenance Notes

- The registry source of truth is `subsystems/china_channels_host/channel_registry.json`.
- Python and Node both consume that registry; avoid hardcoding channel sets in new code.
- If upstream adds or removes a channel, update the registry, vendor mirror, wrapper files, and tests in the same change.
