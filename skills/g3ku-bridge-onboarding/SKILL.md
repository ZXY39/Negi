---
name: g3ku-bridge-onboarding
description: Universal reference for onboarding any external channel bridge to G3KU via the External Agent API — token management rules, protocol walkthrough, bridge-build checklist, connectivity verification, troubleshooting.
---

# G3KU 外部渠道桥接对接（万能参考）

适用：用户要求把任意 IM / 渠道 / 自动化桥接入 g3ku（"对接渠道"、"让桥连上 g3ku"、"新建一个渠道桥"、"桥连不上"）。本 skill 是平台无关的对接总纲；权威契约见 `docs/architecture/external-agent-api.md`，参考实现见 `bridges/qq-onebot/`（独立包、零 g3ku import）。

## 参考文档（权威正文，动手前先读）

- `references/integration-manual.md` —— 接入手册：接口参考 + curl 样例 + 事件流 + 错误码 + 验收排障。
- `references/building-a-bridge.md` —— 适配层编写指南：通用变换规则 + qq-onebot 结构地图 + 最小骨架 + 验收清单。

本文只做触发级速览，字段名/端点/状态码一律以这两份为准。

## 0. 架构边界（三条铁律）

- g3ku 核心只暴露 External Agent API `/api/v1`（Bearer token 鉴权）；核心不出现任何平台特定代码——换一个 IM 平台就要改的代码不属于核心。
- 桥是独立进程 / 独立项目：平台登录、事件订阅、频控、消息分段、触发词识别全部归桥。
- 会话键 `ext:{bridge_id}:{hash}` 里只有 external_key 的摘要；external_key ↔ session_key 的权威映射在 g3ku 侧注册表，桥不要自行解析 session_key。

## 1. 对接流程（按序执行）

1. 确认总开关：web「外部接入」面板，或 `g3ku external status`（只读）。
2. 签发 token：在 web「外部接入」面板签发（默认自动生成；可自定义，重复值会被拒）。明文只显示一次。
3. 交付明文：交给桥的秘文存储（桥项目 `.env` / 密钥文件，权限收紧，**不进任何仓库**）。模型不得把明文写进 g3ku 仓库文件或会话转录；若模型代写桥配置，写完即从上下文清除。
4. 配置桥：`base_url` + `Authorization: Bearer <token>` + `bridge_id`（与签发标识一致）。
5. 启动桥并验收：
   - `g3ku external sessions` 出现 external_key ↔ session_key 映射；
   - web 会话列表「渠道」分组出现该桥会话（只读，见 §6）；
   - 发一条消息，SSE 收到 `turn.completed`（或 `turn.failed`）。

## 2. token 管理规则

- 管理面：web「外部接入」面板（签发 / 重新生成 / 启停 / 删除）；`g3ku external status` 只读诊断（掩码回显，锁定时显示占位提示）。
- 明文只在签发 / 重新生成响应里返回一次；其余读取只回掩码。
- 密文在 bootstrap secret overlay：落盘 `config.json` 只留占位；项目锁定时进程内无明文。
- 冲突码：bridge_id 重复 409 `bridge_id_exists`；token 值重复 409 `token_exists`。
- 吊销即失效：删除条目后该 token 立刻 401。

## 3. 协议速览（写桥必读）

- 鉴权：`Authorization: Bearer <token>`。401 = token 缺失 / 不匹配 / 条目禁用；403 = `external_api_disabled`；423 = 项目锁定；404 = 越权访问他人会话。
- 会话：`POST /api/v1/sessions`，body `{external_key}`，按 external_key 幂等 get-or-create，返回 session_key。
- 回合：`POST /api/v1/sessions/{id}/messages`，带 `Idempotency-Key` 头；同键重复提交返回原 turn_id + `status:"duplicate"`。
- 事件：`GET /api/v1/sessions/{id}/events`（SSE）。断线用 `Last-Event-ID` 回放续订；15s 心跳注释保活。
- 事件集：
  - `turn.started` / `turn.completed` / `turn.failed`：每回合终态恰好一个；`turn.failed.error` 是用户可读全文。
  - `reply.delta`：text 为最新思考段权威全文——**桥做全量替换，不追加**。
  - `progress`：`kind: milestone/tool/tool_error`；g3ku 发全量，**节流是桥的职责**。
  - `reply.final`：权威全文（已出站清洗）。
  - `outbound.created`：主动推送（cron / heartbeat / task 终态回流）。
- 控制：`POST /turns/{id}/pause`、`POST /sessions/{id}/cancel`、`DELETE /sessions/{id}`（清除语义：转录清空、注册表条目保留）。
- 附件：`data_base64` ≤ 5MiB；图片走 `image_url`，能否进模型由绑定的 `image_multimodal_enabled` 门控。

## 4. 新平台桥构建清单

- external_key 映射规则：dm / group / thread 的隔离粒度由桥决定（一个 external_key = 一个 g3ku 会话）。
- 提交幂等：每次用户消息生成唯一 `Idempotency-Key`，重试复用同键。
- SSE 韧性：断线重连 + `Last-Event-ID`；`reply.delta` 全量替换渲染；`reply.final` 归档。
- 主动推送：消费 `outbound.created` 并投递到平台。
- 终态纪律：等 `turn.completed` / `turn.failed` 再发下一轮，或显式 pause/cancel；不要假设提交即串行排队。
- 平台差异全在桥：频控、分段、触发词、@ 识别等不进 g3ku。
- 零 g3ku import：桥与 g3ku 只通过 HTTP / SSE 交互。
- 验收按 §1 第 5 步。

## 5. 排障

- 状态先看：`g3ku external status`（开关 / token 掩码 / 启停）、`g3ku external sessions`（注册表映射）。
- 401/403/423/404 对照 §3 鉴权行。
- 消息提交成功但桥收不到回复：确认订阅的 SSE 会话与提交会话是同一 session_id；长断线超过 `eventBufferSize` 窗口会淘汰事件。
- 主动推送不到达：发布侧（heartbeat 日志）→ drain（`ext outbound drained` / dropped 告警）→ 桥的 `outbound.created` 消费。
- 终态缺失导致桥状态悬挂：对照 `docs/architecture/external-agent-api.md` 的终态不变量检查执行器改动。

## 6. web 可见性

- `ext:*` 会话进入 CEO 会话列表渠道分组：分组键 `ext:{bridge_id}`，标签「外部桥接 · <bridge 备注>」，只读展示（不可改名 / 删除），标题优先注册表 title，其次 external_key。
