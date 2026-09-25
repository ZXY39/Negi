# Fix Plan: QQ 官方适配器支持多账号（一号一桥，零特例，不迁移存量会话）

> Driver: 操作员要求"一个 QQ 号控制一个 QQ 渠道会话"，且明确不需要额外的数据隔离工作（记忆共享与否都不作为需求）
>
> Measured on: 2026-09-25，`.g3ku` 现网数据 + `ext:qq-official:f8a8001865631301` 这条在途会话
>
> Status: 计划（未实施）
>
> Scope: `qqBot` 配置形状与密钥覆盖层路径、QQ 服务生命周期、外部接入管理面与状态、已定决策 A（存量会话不迁移）的数据收口；**不动**核心运行时、会话键公式、registry 形状

---

## 0. Executive Summary

五个决定性事实，它们决定方案形状：

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| D1 | 桥身份从哪来 | **来自 token 表的键**：`externalApi.tokens.<bridge_id>`（`g3ku/runtime/api/external_auth.py:4`、`:25`、`:49-58` 遍历字典匹配后注入 `bridge_id`）。请求里没有任何 `bridge_id` 参数 | "支持 N 个号" = 自动签发 N 条 token 条目，走的就是现在 `_ensure_qq_official_token`（`g3ku/qq_official/service.py:127-136`）那条路，只是键换成派生值。不需要新概念 |
| D2 | 会话键会不会被重算 | **不会**。`resolve_or_create` 先查 `(bridge_id, external_key)` 索引，只有 miss 才 `build_external_session_key`（`g3ku/runtime/external_sessions.py:120-138`），且它是全仓唯一调用点 | 存量会话的 `ext:qq-official:*` 是一条普通历史记录。改格式不需要代码认识它，也就无需为它写分支 |
| D3 | 渠道语义认不认 bridge 段 | **只认前缀**。`is_channel_session_key` = `startswith(("china:", "ext:"))`（`g3ku/runtime/session_keys.py:185-191`），frontdoor 连续性前缀表同理（`session_agent.py:141`）。`normalize_bridge_id` 把非法字符折成 `-`，`qq-official-<数字appId>` 原样通过 | `ext:qq-official:*` 与 `ext:qq-official-<appId>:*` 在"目录分组 / 历史不可改"两条轴上完全同构。仓库已有 `china:*` 与 `ext:*` 两代格式并存零分支的先例 |
| D4 | "单号"实际写死在哪 | 四处：配置对象 `QqBotConfig`（`g3ku/config/schema.py:862-876`：`enabled/app_id/app_secret/sandbox`）、进程内单实例 `_global_qq_official_service`（`g3ku/shells/web.py:61`、`:367-385`）、服务内单 `_task`（`service.py:31-37`，`_config_signature` 在 `:55` 已是 diff 形状）、密钥覆盖层点路径 `qqBot.appSecret`（`g3ku/security/bootstrap.py:194-196` 抽取、`:227` 剥离、解锁回填） | 工作量集中在 D4 这四处，其中只有密钥路径有回归风险 |
| D5 | 真去迁移存量会话要多贵 | 实测该键的引用面：文件/JSON 侧约 19.5k 次，其中 15.8k 在 323 个只读诊断工件里（sidecar 存的是 `frontdoor_request_body_history` 的**绝对路径**，所以目录必须连带改名）；DB 侧 6 列 428 行（`tasks.session_id` 53、`task_events` 118、`task_commands` 157、三种 outbox 46/53/1）；另有 cron 1、registry 1、`web-ceo-state.json` 1、`audit.jsonl` 5、长期记忆 2 文件 227、日志 9.3k（不可也不该改写） | 全仓**没有**会话键重写的先例（现存 `_migrate_*` 全在配置层，见 `g3ku/config/loader.py:222-240`）。跨 10+ 面一致性重写需停机且无回滚工具 ⇒ 决策 A：不迁移 |

两条已定决策（不要重开）：

1. **不迁移存量会话（A）**。旧号在新格式下第一次来信就是新会话；旧键的转录 / sidecar / 记忆作用域原样留着作历史。
2. **代码零特例**。`bridge_id = f"qq-official-{app_id}"` 是一条对所有号一视同仁的**规则**，包括现在这个号——它同样从 `qqBot.appId` 折进列表、同样得到新键。老号不保留 `qq-official` 字面量，因此没有任何 legacy 分支。

Required invariants:

> 一个 QQ 号 = 一条 bridge_id = 一条自动签发的 token = 进程内一个 `QqOfficialService` 实例 = 一条 botpy 连接。五者同生同灭，任何一环不得跨号共享句柄。

> 出站作用域只经 registry 的 bridge 归属收敛，不靠运行时代码过滤 `external_key` 前缀。这是 A 方案能免掉"跨号误投"整类缺陷的前提；一旦哪天想回退成"多号共用一条 bridge_id"，`GET /outbox/pending` 与常驻对账会立刻把别的号会话的滞留推送抓过来投，症状是重试 5 次跳毒消息的静默丢。

---

## 1. 现状与目标形状

现状（`docs/architecture/external-agent-api.md`「内置官方 QQ 适配器」）：`qqBot` 一个对象、`bridge_id` 常量 `qq-official`（`g3ku/qq_official/messages.py:18`）、进程内一个服务实例、`sync_from_config` 按 `enabled|app_id|sandbox` 签名决定重启。

目标：

```
qqBot:
  enabled: true            # 总开关，保留
  accounts:
    - appId: "…"           # 唯一键，bridge_id 由它派生，不可手填
      appSecret: "…"       # 覆盖层字段，落盘只留占位
      sandbox: false
      enabled: true
      label: "…"           # 可选，仅管理面展示
```

配置层迁移沿用既有先例的形状（`_migrate_removed_china_bridge_config` 返回 `changed` 让 `load_config` 复存一次）：把老的 `qqBot.appId/appSecret/sandbox` 折成 `accounts[0]`，`enabled` 顶层与账号级取与。**这条迁移本身就是"零特例"的执行者**——老号被折进列表后走同一套派生规则，不保留旧 bridge_id。

---

## 2. 实施步骤

| 阶段 | 内容 | 落点 |
| --- | --- | --- |
| P0 | 配置列表化：`QqBotConfig` 改 `{enabled, accounts[]}`，加 `_migrate_legacy_qq_bot_account`；`_runtime_config_payload` 的 `qqBot` 段同步（`g3ku/config/loader.py:542-546`），`("qqBot",)` 豁免前缀不变（`:568`） | `g3ku/config/schema.py:862-876`、`loader.py` |
| P1 | 密钥覆盖层按账号：抽取/剥离/回填三处从 `SCONFIG.qqBot.appSecret` 改为索引路径（`qqBot.accounts.<i>.appSecret`），保留一次旧点路径的读兼容；写入必须原子（磁盘满截 0 字节的教训：空覆盖层会让守卫反向拒绝一切写入） | `g3ku/security/bootstrap.py:194-196`、`:227`、解锁回填处 |
| P2 | 服务注册表：`_global_qq_official_service` → `dict[bridge_id, QqOfficialService]`；`_sync_qq_official_service` 在既有模块级锁内做 diff（新增→建实例并 start、变更→按签名重启、消失→`stop()` 并摘除）；崩溃退避与 `sync_from_config` 复活逻辑按实例独立，`_run` 的"干净返回即终态"语义不变 | `g3ku/shells/web.py:61`、`:367-385`、`g3ku/qq_official/service.py` |
| P3 | 管理面：`/api/qq-bot/settings` GET/PUT 改列表语义（PUT 整表替换，appSecret 空串=保留原值这条按账号成立）、`/api/qq-bot/status` 返回 `[{bridgeId, appId, state, detail}]`；前端「外部接入」的官方 QQ 面板改账号行 + 增删；每号 token 的 `label` 写 appId，管理页 token 列表已能展示，无需新逻辑 | `main/api/admin_rest.py` 的 `/api/qq-bot/*`、`g3ku/web/frontend/org_graph_external.js` |
| P4 | A 的数据收口（一次性运维，不是代码）：`.g3ku/cron/jobs.json` 里那条每日提醒的 `to` 改指新会话键；停用/删除孤儿凭证 `externalApi.tokens["qq-official"]` | 无代码 |

P2 的两条实现约束：`_restart` 里 `await stop()` 的窗口内 `_task=None`（原注释已说明无锁并发会各自建桥、先建者沦为无人持有的孤儿任务 ⇒ 双 botpy 连接、每条消息双份投递），diff 全程持同一把模块级锁即可，不要改成每号一锁（diff 需要原子性）；`stop()` 在关闭序列里要遍历全部实例（`web.py:1062` 那处 global 清理）。

---

## 3. 误伤表

| 担心 | 判定 |
| --- | --- |
| 两号共用 bridge_id 时互相抢 outbox pending | 本方案按号分 bridge_id，由构造不成立（见不变量 2）。这条是"想改回共用"时的代价说明，不是待办 |
| 同一自然人在两个号下变成两条会话、两份长期记忆 | 成立（QQ 开放平台的 openid 按 AppID 隔离——**这条我未查证，实现前需在你的控制台或平台文档核一次**）。操作员已明确"不需要额外隔离工作"，per-号作用域是现状默认，不加代码 |
| 反过来想"多号共享同一份记忆" | 不是默认行为。`memory_scope.chat_id` 由会话键推出（实盘值 `"qq-official:f8a8001865631301"`），要共享得显式做作用域归一，属新工作项 |
| 存量会话变成"网页能写、QQ 收不到" | A 的预期结果，不是 bug。症状链可诊断：发布时 `no live subscriber` WARNING → 对账 `external outbox reconcile: republished` → 始终无 `qq-official delivered` → 24h 后标 `expired`。收口动作 = P4 |
| cron 改指前丢一次提醒 | 真会丢（滞留推送无人消费）。P4 必须在切号之前做完 |
| 配额与频控翻倍可见性 | 主动消息额度按 AppID 各算一份，这其实是要多号的主要收益；但每号独立 `error` 状态、独立退避，管理面必须逐号展示，否则一个号被限流会被误读成全坏 |
| 密钥覆盖层列表化的写坏风险 | 最高危的一处：路径形状变了但旧 `.g3ku/secret-realms` 条目还是老键 ⇒ 解锁后 appSecret 回填不上 = 表现为"号全掉线且状态显示未配置"。P1 必须带一次旧键读兼容 + 一个"回填后 `app_secret` 非空"的显式断言 |
| 会话文件名变长 | 未测。`safe_session` 清洗层是否有长度上限需实现期确认；appId 9–10 位数字 ⇒ 新键比旧键长约 11 字符，理论余量大 |

## 4. 用户视角前后对照

| 场景 | 现在 | 之后 |
| --- | --- | --- |
| 接第二个 QQ 号 | 只能换号（改 `qqBot.appId`），两号不能同时在跑 | 面板加一行、贴 AppID/AppSecret，两号并行，各自一条会话 |
| 一个号被平台限流 | 全局唯一桥，渠道整体哑掉 | 该号 `error` + 独立退避重连，另一号照常收发 |
| 老会话（今天这条 18 MB） | 正常使用 | 变历史只读；新消息进新键会话。每日提醒经 P4 指向新会话 |

## 5. 本计划不会让它变好的

- 一个号内部的多用户/多群仍是各自的会话（本来如此）。
- 跨号"同一个人"的身份合并：平台不给全局 uid，需要单独设计。
- QQ 端语音无 ASR、普通回合 `reply.final` 只走 hub 不进持久 outbox（断线超过 `eventBufferSize` 即逐出）等既有缺口，见 `external-agent-api.md`。
- 本仓库另一条在案缺陷（`turn.failed` 不投递到渠道）不受本计划影响，用户在任一号下失败时仍收不到错误说明。

## 6. 验收办法

1. 覆盖层往返：配两号 → 存盘 → 读盘只留占位 → 解锁后**两号的 `app_secret` 都非空**。这一步不过，后面全不用测（历史事故形态是空覆盖层让守卫反向拒绝写入）。
2. 配置迁移：手改 `config.json` 成老形状（`qqBot.appId` 顶层）→ 启动一次 → 断言被折成 `accounts[0]` 且复存一次、`bridge_id` 派生成 `qq-official-<appId>`、**没有** `qq-official` 字面量残留（`grep -rn '"qq-official"' g3ku/` 只应命中常量与派生前缀）。
3. 双号并跑：两号各发一条消息 → `GET /api/qq-bot/status` 两行都 `connected`；日志里两条 `qq-official delivered` 各自的 target；registry 出现两个 `bridge_id`、两条会话键。
4. 隔离性：A 号的 pump 不得消费 B 号的 outbox 记录（把 B 号会话的一条滞留推送置 pending、只让 A 号在线 ⇒ B 号记录年龄继续增长、`no live subscriber` 仍指向 B）。
5. 单号故障：把一号的 AppSecret 改错 → 该号 `error` 且按退避重试，另一号收发不受影响；改回后 `sync_from_config` 分钟级复活。
6. 收口验证（P4）：cron 改指后等一次真实触发，`qq-official delivered` 出现在**新**会话的目标上；孤儿 token `qq-official` 已停用且 `g3ku external status` 不再列它为启用。
7. 关闭序列：进程退出时两个实例都被 `stop()`（不留悬挂 botpy 任务）。

## 7. 测试

新增/扩展放 `tests/`（默认被跟踪）：

- `tests/resources/test_qq_official_admin.py`：settings 列表往返 + appSecret 空串保留原值 + status 数组形状。
- `tests/resources/test_qq_official_bridge.py`：注入两号时 bridge 只消费自己会话的 pending（对应用户验收第 4 步）。
- 新增 `tests/test_qq_bot_multi_account_config.py`：老形状迁移、派生 bridge_id、`enabled` 与账号级开关的与语义。
- `tests/test_security_overlay_guard.py`：加索引路径的抽取/剥离/回填往返，含"旧点路径仍可读一次"。
- 回归：`tests/resources/test_external_api_admin_tokens.py`（多号自动签发条目不与手工条目冲突）、`tests/resources/test_external_outbox.py`、`tests/resources/test_ceo_channel_session_web_input.py`（`ext:` 前缀判定不受影响）。

## 8. 文档影响

按 `g3ku-architecture-maintenance` 判定，属"契约 + 操作员工作流"变更：

- `docs/architecture/external-agent-api.md`「内置官方 QQ 适配器」：配置段、生命周期、崩溃自愈、pump 存活各段就地改写为"一号一桥"；出站作用域收敛的不变量落在这里。
- `docs/architecture/config-and-models.md`：`qqBot` 形状与密钥覆盖层的按账号路径（` Where secrets really live`）。
- `docs/architecture/web-and-admin.md`：外部接入面板的账号列表契约。
- `docs/architecture/README.md`：只加一条症状 → 文档指针（"改配第二个号后老会话收不到回复"），不改 Topic Ownership 表（归属未变）。
