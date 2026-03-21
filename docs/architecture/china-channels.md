# China Channels Architecture

## Overview

G3KU now runs a single communication path for every supported chat platform:

- Python remains the only agent brain.
- `subsystems/china_channels_host` is the only platform communication host.
- Supported platforms are limited to `qqbot`, `dingtalk`, `wecom`, `wecom-app`, and `feishu-china`.

The previous Python multi-channel framework under `g3ku/channels/` has been fully retired.

## Runtime Boundary

### Python owns

- session lifecycle
- task execution and cancellation
- memory and governance
- runtime events and final replies
- bridge supervision and health status

### Node owns

- webhook and stream entrypoints
- platform SDK and authentication
- long-lived websocket and stream connections
- inbound normalization
- outbound platform delivery

## Control Flow

### Inbound

1. A platform sends a message to the Node host.
2. The host normalizes it into `inbound_message`.
3. The frame is forwarded to Python over the internal control WebSocket.
4. Python converts it into a G3KU session request and calls `SessionRuntimeBridge.prompt(...)`.

### Outbound

1. Python emits progress, tool hints, and final replies.
2. `ChinaBridgeTransport` encodes them as `deliver_message`.
3. The Node host delivers them through the matching platform sender.

## Configuration

The only supported communication config path is:

- `chinaBridge.channels.qqbot`
- `chinaBridge.channels.dingtalk`
- `chinaBridge.channels.wecom`
- `chinaBridge.channels.wecomApp`
- `chinaBridge.channels.feishuChina`

Top-level `channels.*` is no longer supported. Startup fails fast if any legacy channel config is present.

When `chinaBridge.enabled=true` and `chinaBridge.autoStart=true`, G3KU also auto-checks
`subsystems/china_channels_host` on startup and builds it before launch if `dist/` is missing or stale.

## Session Keys

The control WebSocket between Node and Python is global and is not bound to a single session at connect time.
Python resolves the destination session for each inbound message independently.

Python generates all session keys with this format:

- DM: `china:{channel}:{account_id}:dm:{peer_id}`
- Group: `china:{channel}:{account_id}:group:{peer_id}`
- Thread: append `:thread:{thread_id}`

## Operational Interfaces

- CLI: `g3ku china-bridge status`
- CLI: `g3ku china-bridge doctor`
- CLI: `g3ku china-bridge restart`
- API: `GET /api/china-bridge/status`
- API: `GET /api/china-bridge/doctor`
- API: `POST /api/china-bridge/restart`

## Intentional Removals

These are no longer part of the supported runtime architecture:

- `g3ku/channels/*`
- `bridge/*`
- `g3ku channels ...`
- non-China chat channel runtime support
