# 渠道会话统一：记忆分域、受限模式、心跳与 @

状态：方案已确认，待开始实现（未改代码）
日期：2026-10-30
基线：main @ 1acd9ca0

## 已确认口径

1. owner 私聊与本地会话同权：同一份本地记忆、同一份人设、默认权限一致。
2. 所有外部会话共享同一份人设；记忆分为：
   - 本地记忆：仅 owner 会话可写；第三方私聊只读。
   - 群聊记忆：按群分离，群会话只写本群；群记忆块 = 本地记忆 + 本群记忆。
3. 未安装沙箱时的 `restricted` 档改为受限模式：每个会话独立工作目录，目录内可写文件；禁止 CLI / 进程 / 网络 / 任务派发；CLI 调用返回明确的拦截文案。受限模式适用于 owner 与第三方全部会话。
4. 群聊提示词增加会话类型、当前模式与限制；全权限模式不写模式段。切档后下一回合直接重印 system prompt / tools / 合同，并投一条“已切换到 xxx 模式”的运行时通知。
5. 群聊保留心跳；`cron` / `schedule_wake` 在受限/沙盒档可用；`create_async_task` 仍拒。
6. 60 秒主动消息节流：按会话计，cron / heartbeat / 任务通知 / @ 工具都算“主动发消息”，普通回合回复不算；不足 60 秒的任务延迟到窗口结束，最终唤醒时带“因不足 60 秒延迟”的说明；不再增加任务数量、最小间隔、max_runs 等额外上限。
7. @ 使用 `mention_member` 工具；目标只能是当前会话花名册里的 Master 或会话内编号；QQ 群用 markdown `<qqbot-at-user id="member_openid" />`。
8. 记忆页增加作用域选择：本地 / 各群，查看、编辑、删除都走对应记忆 agent 的队列。
9. 投递失败不做时间节流：每次失败都唤醒模型；失败唤醒是内部回合，不默认向渠道回复，模型应 `silent` 或只在被动窗口内用 `mention_member` @Master。
10. 受限模式增加**按会话**的“允许使用的工具列表”，完全可编辑：`web_fetch` 默认勾选；`exec` / `process_kill` / `create_async_task` / `skill-installer` 等默认不勾选但可勾选；`silent` / `time` / `load_*` 为运行时固定项。修改需项目解锁口令。受限模式下“命令行风险拦截设置”在生效工具包含 `exec` 时显示，不再无条件隐藏。

## 已核实现状（HEAD 1acd9ca0）

### 记忆与人设

- `MemoryManager` 是单实例（`g3ku/runtime/bootstrap_bridge.py::init_memory_runtime`），所有路径从 `workspace` 推导：一个 sqlite、一个 `MEMORY.md`、一个 notes/index、一个 queue/review_state、一个 worker（`g3ku/agent/memory_agent_runtime.py:2101`）。
- 读侧第三方隔离：`message_builder.py::_read_memory_snapshot_text`（记忆返回空串）、`adopt_persona`（改读 `<数据根>/sandbox/persona.md`）。
- 通知侧第三方隔离：`_ceo_runtime_ops.py::_frontdoor_memory_notice_provider` / `_frontdoor_persona_notice_provider` 返回 `None`。
- 写侧记忆门：`bootstrap_bridge.py::_memory_review_eligibility` 注入 `is_owner_session`，`MemoryManager.review_eligibility` 在 `record_turn_for_review`、`flush_review_window`、队列拆批、`memory_update` 四处判。
- 额外闸门：第三方只读白名单不含 `memory_update` / `memory_note`，`exposure_resolver` 会按 `tool_allowed` 摘掉。
- 群聊整会话按 `third_party_group`；名单只影响来源标注，不影响档位。
- 本地记忆管理页 `/api/memory/current` 只读 `main_service.memory_manager`；配置包记忆面只收集本地 store/mirror/notes/index（`config_bundle.py::_collect_memory_entries`），不扫描群目录。
- 人设 note 只有一个：`memory/notes/note_persona.md`，由本地 manager 持有。

### 档位与提示词

- 档位：`restricted` × 安装态 → 无 jail 时 `readonly`，有 jail 且 ready 时 `jailed`；`unrestricted` → `host_full`（`g3ku/sandbox/policy.py::resolve_tier`）。
- `JAIL_BACKENDS_AVAILABLE = False`，默认不发放 `jailed`；装好沙箱也只到只读档，直到打开 `G3KU_SANDBOX_ENABLE_JAIL` 并跑完验收。
- 只读档：第三方白名单（十来个工具），文件写类工具拒；owner 只读是黑名单；`fs_write_roots` 只是声明，没有消费方，写入侧靠 `_resolve_path` 的读根 ContextVar。
- 受限档的 `filesystem_write/edit` 会执行 `write_validation_commands_by_ext`（默认 `python -m py_compile`）——受限模式必须关掉，否则禁 CLI 被内部校验绕过。
- `build_base_prompt` 明确把 tier 排除在 cache stamp 外（`prompt_builder.py:59`），pinned contract 的 revision key 也刻意忽略 exec 策略与临时目录（`tool_contract.py:201`）；切档当前不重印前缀，只靠 `[RUNTIME_NOTICE]` 纠正。
- 渠道身份块 `channel_identity_block` 是静态隐私说明；沙箱声明块 `_sandbox_declaration_block` 只有 `jailed` / `readonly` 两档。

### 心跳 / 定时 / @

- 心跳对 `ext:` 会话本身有效：`heartbeat/session_service.py::_run_session` 不按信任级过滤，`task_notice_audience_ok` 把 `ext:` 算活受众。
- `cron` / `schedule_wake` 在 `TASK_TOOL_NAMES` 里，受限档一律拒；第三方只读白名单也不含它们。
- `cron` 的 `list` / `remove` 全局无会话过滤；`name` 是 `message[:30]`，群聊里会读到其他会话的提醒内容。
- `cron` 到点由 `CronService` claim 后 dispatch；一次性 `at` 任务 claim 后重启不会重放。节流必须在 claim 之前做，否则一次性任务会被误判为已执行。
- 外部会话注册表条目（`external_sessions.py:35`）有 `bridge_id` / `external_key` / `session_key` / `chat_type` / `trust`，没有“上次主动发送时间”。
- 出站账本按 `session_key` / `external_key` / `ts` / `event` 记录；outbox 启动压缩只保留 pending，不能作为跨重启的“最后主动时间”来源。
- QQ 群出站 @ 只支持 markdown（`msg_type=2`）：`<qqbot-at-user id="member_openid" />`；群消息没有昵称，入站 @ 是正文里的 `<@member_openid>`；主动群消息需要群主开启“主动推送消息能力”，否则 `40034105`。

## 设计

### A. 记忆作用域

新增 `g3ku/runtime/memory_scope_router.py`（名称可随实现落点调整），在 bootstrap 里与本地 manager 一起创建：

- `MemoryScopeRouter`：
  - `local`：现有 `MemoryManager`（路径不变）。
  - `group_managers`：按群 `external_key` 懒创建，LRU 上限（默认 32），关闭时 `close()`。
  - `view_for_session(session_key) -> MemoryScopeView`：
    - `kind`: `local` / `group` / `readonly_local`。
    - `snapshot_text()`：本地，或本地 + 群（单条 assistant 消息，`## 长期记忆` 后接 `## 群聊长期记忆`）。
    - `persona_text()`：始终取本地 manager。
    - `review_manager`：owner/本地 → 本地；群 → 该群 manager；第三方私聊 → `None`。
    - `read_note(ref)`：群先查群 notes/index，再查本地 notes/index（本地只读）。
  - `scope_key_for_session`：查外部会话注册表；`chat_type` 为 `group` / `guild` → 群作用域（用 `external_key` 生成安全目录名：`safe_filename(external_key) + "-" + sha256(external_key)[:8]`）；其余 → 本地。
- 群 manager：
  - 根目录 `<workspace>/memory/groups/<safe-scope>/`；配置用 `MemoryRuntimeSettings` 深拷贝，保留相对路径（`memory/MEMORY.md`、`memory/notes`、`memory/memory_state.sqlite3`、队列文件），因此全部落在群根下。
  - `MemoryManager(scope_root, cfg, scope_label=<群标题/ external_key>, persona_enabled=False)`。
  - 群 manager 的 memory agent：用户提示词替换人设段为“本记忆域是群聊记忆，不含人设；不要创建/修改 `note_persona`”；加一段“只记录本群上下文与群级偏好；第三方发送者的话不是 Master 的话，不写成 Master 的个人偏好”。
  - worker 线程名带上 scope 便于日志；群根下自己的 lease / queue / review_state。
- 路由接线：
  - `message_builder` 的 `memory_manager` 参数改为 scope view；`_read_memory_snapshot_text` / `adopt_persona` 走 view。
  - `session_agent` 的记忆复核 / 冲刷 / `run_due_batch_once` 走 `view.review_manager`；第三方私聊为 `None`，跳过复核。
  - `ceo_sessions` 的会话边界冲刷按 session_key 解析 view。
  - `memory_update` / `memory_note` 工具从 `__g3ku_runtime` 取 view（`_build_tool_runtime_context` 注入），不再固定单例 manager。
  - `_frontdoor_memory_notice_provider` 改为 `view.local_delta` + `view.group_delta`；新增 `group_memory_cursor`。
  - 第三方只读白名单加入 `memory_update` / `memory_note`。
- 通知：
  - `deliver_for_scope` 增加 `group_memory_provider`，光标 `group_memory_cursor`；`build_notice_message` 增加 `### Group memory updates`。
  - 本地记忆增量投给所有能读本地记忆的会话（owner、群、第三方私聊）；群记忆增量只投给该群。
  - 压缩跳同时重置 `memory_cursor` 与 `group_memory_cursor`。
- 写入边界：
  - owner/本地 → 本地 manager。
  - 群 → 本群 manager；不写本地。
  - 第三方私聊 → `memory_update` 返回可读拒绝（“只有操作员会话能修改本地记忆”），`review_manager=None`。
- 管理面：
  - 新增 `GET /api/memory/scopes`（本地 + 各群：scope key、标题、session_key、最后修订/更新时间）。
  - `GET /api/memory/current`、`update`、`delete`、`note` 接受 `scope` 参数；`internal:memory_admin` 仍作为发起键，目标 manager 由 scope 决定。
  - 记忆页加“本地 / 群聊”作用域选择；群列表来自外部会话注册表。
- 配置包：
  - 记忆面收集 `memory/groups/**` 的 mirror、sqlite、notes、index（排除 queue/ops/review_state、worker lock、`.bak`），逻辑键保持 `memory/groups/<scope>/...`；导入按记忆面整包替换，文档写明群记忆会随包整体替换。
- CLI：
  - `negi memory doctor` 默认扫描所有 scope；`memory` 其他命令加 `--scope`（默认 local）。

### B. 受限模式

- `g3ku/sandbox/policy.py`：
  - 新增 `TIER_LIMITED = "limited"`；`resolve_tier`：`restricted` 且无可用 jail → `limited`；`jailed` 仍按现有闸门；`unrestricted` → `host_full`。
  - `SessionPolicy`：`limited` 的生效工具集合见 G 节（默认集合 + per-session override；`web_fetch` 默认允许；`exec`/`process_kill`/`create_async_task`/`skill-installer` 默认不允许但可由 operator 勾选）。`send_message` / 节点控制类不在 CEO 目录；控制面仍拒。
  - owner `limited`：读根含工作区；写根只含会话工作目录；第三方 `limited`：读写根都限会话工作目录 + 上传 + temp。
  - `fs_write_roots` 补上真实消费：`filesystem_mutation` 在写/改/删/移动/复制前用 `runtime_write_roots` 做根校验；空清单保持不设限。
- 会话工作目录：
  - 新 helper `g3ku/runtime/frontdoor/session_workspace.py`：`<数据根>/session-workspaces/<safe-session>-<sha256[:8]>/`；惰性创建；无自动清理（与 `temp/ceo` 同纪律，文档写明）。
  - runtime context 增加 `session_workspace_dir`；工具契约增加 `session_workspace_dir:` 行；`filesystem_*` 在 `limited` 下把相对路径锚到该目录。
  - `filesystem_write/edit` 在 `limited` 下禁用 `write_validation_commands_by_ext` / `edit_validation_commands_by_ext`（内部命令也属于 CLI）。
  - `exec` / `process_kill` / `create_async_task` 默认不曝光；operator 在按会话工具列表勾选后曝光并按各自守卫执行（`exec` 强制 governed）。未勾选时若模型经残留契约调用，审批闸门返回受限模式专属文案：“本会话处于受限模式，该工具未在本会话允许列表中；可以读写会话工作目录内的文件”。`web_fetch` 默认曝光。
  - 会话工作目录纳入 `_is_allowed_temp_target` / managed-artifact 例外，避免 zip/日志被策略误拒；磁盘上限可先不做，文档标风险。
- UI / 文案：
  - `_TIER_LABELS` 增加 `limited: "受限"`；`_LABELS_NOT_INSTALLED` 的 `restricted` 从“只读”改“受限”；`sandbox.js` 的开关标题、确认文案、失败说明同步改。
  - `tasks_allowed` 只在 `host_full` 为真（现有），文案说明受限/沙盒都不派任务。

### C. 模式提示词与切换重印

- `channel_identity_block` 改为按 `tier` 生成：
  - 固定隐私段（现有）。
  - `limited`：本会话是群聊/外部渠道会话；受限模式；可读写本会话工作目录内的文件；默认不执行命令行、不派任务；`web_fetch` 默认可用；具体可用工具以 `tools[]` 为准。若 operator 在工具列表里勾选了 `exec`，补一句“命令行执行已由操作员显式允许，并按命令行风险拦截设置执行”。
  - `jailed`：本会话是群聊/外部渠道会话；沙盒模式；命令与文件操作在一次性沙盒内；只有 `/workspace` 与 `/shared` 可写；网络关闭；宿主路径不可见。
  - `host_full`：不写模式段。
- 渠道会话不再叠加 `_sandbox_declaration_block`（避免与渠道块重复）；本地会话继续用 `_sandbox_declaration_block`。
- `build_base_prompt`：
  - cache stamp 加 `|{tier}`，切档即重建。
  - 更新 docstring：tier 现在进入 stamp，接受一次前缀重印。
- pinned contract：
  - `pinned_contract_revision_key` 增加 `sandbox_tier` 与**生效工具集合签名**（effective allowed tool names + hard-denied；按排序后的名字取摘要）；`frontdoor_pinned_contract_text` / `_frontdoor_pinned_contract` / 合同装配路都传 tier 与签名。
  - 合同渲染增加 `session_workspace_dir`（limited）或 `session_temp_dir`（现有）；exec 策略按档位。
  - 工具列表变更后下一回合重印；`### Session mode` 通知附“工具列表已更新”。
- 模式通知：
  - `deliver_for_scope` 增加 `mode_provider` 或复用 `contract_sections` 的 `session_mode` 节；首次对齐当前档，变化时投 `### Session mode`：“已切换到受限/沙盒/全权限模式：…限制…”。
  - `PATCH /api/ceo/sessions/{id}/sandbox` 保存后写审计（现有），下一回合按新 tier 重印；`effective_from: next_turn` 保留。
- `tools[]` 刷新：切档后 `session_policy` 改变 → `exposure_resolver` 重新过滤；`prompt_refresh_stamp` / 合同 revision 改变 → 系统提示与工具合同重印。实现后需要用一次真实切档验收确认 `tools[]` 与提示词同回合变化。

### D. 心跳与定时

- 工具策略拆分：
  - `SCHEDULE_TOOL_NAMES = {"cron", "schedule_wake"}`，受限/沙盒档放行；加入第三方只读白名单。
  - `TASK_DISPATCH_TOOL_NAMES = {"create_async_task", "send_message", task_*}`，保持拒绝。
- `cron` 安全：
  - 渠道会话的 `list` / `remove` 只作用于本会话 `payload.session_key` 的任务；web owner 保持全局。
  - 60 秒节流：
    - `ExternalSessionEntry` 增加 `last_proactive_at`；共享 drain 在发布 `outbound.created` 时更新（普通 `reply.final` 不更新）。
    - `CronService` 增加可注入的 `admit_proactive(job, now_ms)` 回调；web shell 用注册表实现：返回 `(allowed, delay_ms, reason)`。
    - `_on_timer` 在 `_claim_job` 之前调用；不足 60 秒时把 `next_run_at_ms` 推到上次主动时间 + 60s，写 `last_status="deferred"` / `last_error` / `delayed_from_ms` / `delayed_until_ms`，不 claim、不消耗一次性任务。
    - `_dispatch_claimed` 最终执行时把 `cron_delay_reason` / `cron_delayed_seconds` 放进 cron 事件 metadata；cron 提示词块渲染一行。
    - `CronJobState` / `jobs.json` 增加延迟字段，旧 store 缺字段按默认值读。
  - 不新增任务数量、最小间隔、max_runs 上限；保留现有 `max_runs` 语义（默认 1）。
  - 渠道会话的 cron 增删写审计事件（会话、任务 id、触发来源）；`list` / `remove` 的归属校验是安全边界，不是频率上限。
- `schedule_wake`：沿用一次性 `at` 任务 + 覆盖本会话待触发；节流在 dispatch 前统一生效；不再额外加最小秒数。
- 心跳：机制不改；`ext:` 会话可被 cron/self-wake 唤醒；`create_async_task` 仍拒，所以任务终态/失速心跳不会在群聊产生。
- 平台：群主动消息需群主开启“主动推送消息能力”；没权限时唤醒照跑，投递被桥判 `active_denied` / `undeliverable`，工具/日志如实记录。

### E. @ 工具

- 新工具 `g3ku/agent/tools/mention_member.py`：
  - 只在 `ext:` 群聊/频道会话可见；`target` 为 `Master` 或 `0002` 这类会话内编号；`message` 正文上限 1000 字。
  - 从会话转录的 user 行 + `external_sender` 元数据构建花名册；`Master` 取 `qq_owner_openids()` 与花名册交集；拒绝原始 openid、未知编号、跨会话目标。
  - Master 没在群里发过言时返回可读失败（提示先把群 member_openid 加入操作员名单）。
  - 通过 `publish_channel_notice` 走持久 outbox；`OutboundMessage.metadata["mentions"]`；drain 写进事件与 outbox；QQ 桥 `deliver(..., mentions=...)` 在群里用 `msg_type=2` + `<qqbot-at-user id="..."/>`，沿用被动参数与 pacer。
  - 同会话 60 秒最多 1 次、1 小时最多 10 次；写审计。
  - 结果返回“已提交”，不承诺送达；群没有主动消息权限时由桥判 `undeliverable`。
- 入站：桥/投影把 `<@member_openid>` 归一成 `@Master` / `@第三方 0002` / `@某成员`，避免 openid 进入模型上下文；`mentions` 字段可选透传。
- 提示词：渠道块加“需要提醒/征求 Master 时用 `mention_member`；不要手写 openid，不要连续 @；用完后可调 `silent`”。
- 文档：`external-agent-api.md` 出站事件加 `mentions`；QQ 适配器节加群 markdown @ 与主动消息权限。

### F. 投递失败回传（群主动消息权限）

现状：桥在 `_classify_send_failure` 认出 `active_denied` / `passive_expired` / `quota_exhausted`，但只写日志、把 outbox 按 `undeliverable` 销账；模型和操作员都不知道这条推送没送达。

设计：

- `client.ack_outbox(..., status="undeliverable")` 的请求体增加 `kind`（失败分类）、`shape`（`passive` / `active`）、`passive_window_open`、`detail`、`text_preview`；桥在 pump 的失败分支把这些带上。送达时也带 `shape`，用于区分主动成功与被动成功。
- 服务端 ack 路由：
  - 在外部会话条目上记录 `active_message_state`（`denied` / `quota` / 空）、`active_message_denied_at`、`last_delivery_failure_notice_at`、`last_delivery_failure_kind`。
  - `active_denied` / `quota_exhausted` / `passive_expired` 时**每一条失败都入一条** `delivery_failure` 心跳事件；不做 30 分钟时间节流。去重只按 `outbox_id`（同一条消息的重复 ack / 重放副本不重复唤醒），不同消息的失败各自唤醒。
  - 主动形状送达成功（`shape=active` + `delivered`）清掉 `active_message_state`；被动送达不改变能力状态。
- 心跳提示词新增 `delivery_failure` 分支：
  - 说明哪条消息因什么被拒（群未开启主动消息权限 / 被动窗口过期 / 日额度用尽）。
  - 不重试原推送。
  - 若 `passive_window_open=true`，可用 `mention_member` @Master，请群主在机器人资料页开启“主动推送消息能力”；若 Master 不是群主，说明需要群主操作。
  - 若没有被动窗口，`silent` 等 Master 下次发言再说明；不要回复一条同样会被拒的主动消息。
- `mention_member` 判据：会话 `active_message_state=denied` 且最近 5 分钟没有入站消息时，直接返回可读拒绝（“当前没有被动窗口，@ 也会被拒”）；有被动窗口才放行尝试。成功/失败都由同一条 ack 回传更新状态。
- 可选兜底（不在默认范围）：若操作员有 c2c 会话，加一个 `notify_operator` 走单聊主动消息；单聊主动发送在桥侧实测可用，但它不是群内 @。

注意：心跳失败回传本身是内部回合，**不默认向渠道发送任何回复**。模型的正文本轮只留在网页转录里；要触达群里只能走 `mention_member`（且要有被动窗口）。这样即使模型没有 `silent`，也不会因为“回复失败通知”再产生一条必然失败、又触发新通知的主动消息，避免自激循环。每次真实失败仍会各自唤醒，符合“每次失败都提醒模型”的要求。

### G. 受限模式下的按会话工具列表（完全可编辑）

- 存储：`g3ku/sandbox/state.py` 增加 per-session `allowed_tools`（JSON，随现有 session state 落盘；删除会话时一并清理）。未设置 = 使用受限模式默认集合；用户保存后存显式集合；对话框提供“恢复默认”。
- 默认集合（新会话 / 未自定义）：
  - 文件读写与读类：`filesystem_write` / `edit` / `copy` / `move` / `delete` / `propose_patch` / `filesystem_stat` / `content_open` / `content_search` / `content_describe` / `view_message_history`（写限会话工作目录）。
  - 记忆与调度：`memory_update` / `memory_note` / `cron` / `schedule_wake` / `mention_member`。
  - 网络：`web_fetch` **默认勾选**（按你的要求）。
  - 表情包：`show_sticker_category` 默认勾选；`register_stickers` 默认不勾选。
  - 危险工具默认不勾选但**可选**：`exec` / `process_kill` / `create_async_task` / `skill-installer`。勾选时对话框显示后果说明。
- 运行时基础工具固定可用、不可取消：`silent` / `time` / `load_tool_context(_v2)` / `load_skill_context(_v2)`。它们不是安全边界，而是 agent 循环与工具/技能 hydrated 的必需项；关掉会让会话无法工作，所以在列表里显示为“运行时固定”。
- 策略：`SessionPolicy` 只保留 `allowed_tools_override`，**没有硬禁清单**：
  - 生效集合 = `override if set else LIMITED_TOOL_NAMES`，再与 CEO 角色可见工具求交。
  - 勾选 `exec` / `process_kill` 即真正放行；`exec` 在 `limited` 档强制 `exec_requires_governed=True`，命令行形态守卫、路径策略与“命令行风险拦截设置”的全局治理继续生效。
  - 渠道会话的审批闸门：显式勾选的工具不再走“渠道会话一律 auto_reject”的审批打回（群里没有审批面）；`exec` 由命令行治理接管，其他勾选工具按自身守卫执行。未勾选的工具仍按 `policy.tool_allowed` 拒绝。
  - `create_async_task` / `skill-installer` / `register_stickers` 可勾选，但对话框明确写：它们工作在会话工作目录之外，任务 worker / 工具安装目录 / 全局表情包注册表不受本会话写根约束。
  - 只有 `limited` 档使用这份 override；`jailed` / `host_full` 不读它（后续需要再加）。
- API：
  - `GET /api/ceo/sessions/{id}/sandbox/tools` 返回 `{items: [{tool_id, display_name, risk_level, allowed, default_allowed, fixed, warning}]}`；目录取 CEO 可见工具目录（不是全量节点工具），`fixed=true` 表示运行时基础工具。
  - `PATCH /api/ceo/sessions/{id}/sandbox/tools` body `{allowed_tools: [...], unlock_password}`；必须校验项目解锁口令（与 mode 切换同一 `_verify_unlock_password`），忽略 `fixed` 项，写 session state + 审计 `sandbox_tools_changed`（session、added/removed、tier、是否包含 exec）。
  - 返回 `effective_from: "next_turn"` 与 `exec_allowed`；下一回合 `tools[]`、system prompt、pinned contract 按新集合重印（生效工具签名进合同 revision key / prompt stamp）。
- 前端（`org_graph.html` / `org_graph_ceo_governance.js`）：
  - 沙箱开关下方：
    - `restricted` 时显示「允许使用的工具列表」按钮。
    - 「命令行风险拦截设置」在 `unrestricted` 时始终显示；在 `restricted` 时**仅当生效工具包含 `exec` 才显示**。两者可以同时出现。
  - 点击工具列表按钮弹出现有 `openConfirm` 风格的对话框：复选框、风险标签、`fixed` 项置为已勾选且不可改、危险工具给出后果说明、提供“恢复默认”；保存时要求项目解锁口令。
  - 保存后刷新 `exec_allowed`：勾选 `exec` 立刻显示「命令行风险拦截设置」；取消后隐藏。成功后 toast + 刷新 session 状态。
  - 受限模式的切档确认文案补一句：“可在‘允许使用的工具列表’里调整本会话可用的工具；包含命令行执行时可继续配置命令行风险拦截”。
  - 模式切换/工具列表变更后，`tools[]` 与提示词在下一回合重印；`### Session mode` 通知附一句“工具列表已更新”。
- 测试：
  - 默认集合包含 `web_fetch`、不含 `exec`；勾选 `exec` 后曝光层出现 exec、`exec_allowed=true`、前端显示命令行设置；取消后消失；`process_kill` 跟随勾选；`create_async_task` / 安装器 / 贴纸注册勾选后可用并给出警告；固定基础工具不可取消；口令错误/缺失拒绝；非受限档读取返回空/只读；删除会话清理 override；工具列表变更触发 contract/prompt 重印；渠道会话显式勾选的 exec 不被 `auto_reject` 打回、但受 governed exec 约束。
- 文档：`sandbox-execution-plane.md` 受限档与工具清单节、`web-and-admin.md` 权限面板交互与 exec 设置可见性、`tool-and-skill-system.md` 曝光层与 governed exec、`README.md` 群聊章节。

## 阶段与落点

### P1 受限模式（先做，影响面局部）

- `g3ku/sandbox/policy.py`：`TIER_LIMITED`、`resolve_tier`、`SessionPolicy`（默认集合 + per-session override；无硬禁清单；运行时固定项；`exec_allowed` / `exec_requires_governed`）。
- `g3ku/runtime/frontdoor/session_workspace.py`（新）；`_ceo_runtime_ops.py` runtime context / 读根；`message_builder` 合同行；审批闸门对显式勾选工具的渠道会话放行（exec 走 governed）。
- `g3ku/agent/tools/filesystem_mutation.py`：写根校验、受限模式关闭 validation 命令、相对路径锚到会话工作目录。
- `g3ku/sandbox/state.py`：per-session `allowed_tools` 存取与清理；`g3ku/runtime/api/sandbox.py`：`GET/PATCH /api/ceo/sessions/{id}/sandbox/tools`、口令校验、审计、`exec_allowed` 返回。
- `org_graph.html` / `org_graph_ceo_governance.js` / `sandbox.js`：tier 标签、文案、`tasks_allowed`；受限时显示「允许使用的工具列表」；「命令行风险拦截设置」在 unrestricted 或受限且 `exec_allowed` 时显示；工具列表对话框（复选框、风险标签、固定项、危险工具说明、恢复默认、口令确认）。
- 测试：limited 档文件读写边界、exec 默认不可用/勾选后可用且 governed、web_fetch 默认允许、validation 禁用、owner/第三方读根差异、工具列表默认/override/固定项/口令/审计/会话删除清理、渠道会话显式勾选工具不被 auto_reject、工具列表变更触发重印、旧 readonly 用例迁移。
- 文档：`sandbox-execution-plane.md` 档位表/只读档节/工具列表、`web-and-admin.md` 权限面板交互、`operations-and-maintenance.md` 会话工作目录、`tool-and-skill-system.md` 路径锚与曝光层。

### P2 模式提示与切换重印

- `prompt_builder.py`：tier 进 cache stamp；渠道块按 tier；渠道会话不叠加 base sandbox 块。
- `tool_contract.py`：revision key 加 tier；`session_workspace_dir` 渲染。
- `skill_roster_notice.py` / `_ceo_runtime_ops.py`：`### Session mode` 通知与 cursor。
- `api/sandbox.py`：PATCH 后审计/重印生效；测试切档后 system prompt、tools[]、合同、通知同回合变化。
- 文档：`context-and-cache-troubleshooting.md` 前缀重印说明、`sandbox-execution-plane.md` 模式提示。

### P3 心跳与定时

- `policy.py` 拆分调度/派发工具；`cron.py` 会话归属过滤；`CronService` 节流回调与延迟字段；`external_sessions.py` `last_proactive_at`；drain 更新；`runtime_dispatch.py` 延迟 metadata；cron 提示词块。
- 测试：cron/schedule_wake 只读可用、create_async_task 仍拒、list/remove 不越会话、60s 延迟/最终唤醒原因、一次性任务不丢、跨重启节流。
- 文档：`heartbeat-system.md` 群聊唤醒与节流、`tool-and-skill-system.md` cron 合同、`external-agent-api.md` 主动消息权限。

### P4 @ 工具与投递失败回传

- 新工具 + 前门装配 + outbox `mentions` + QQ 桥 markdown 路径 + 入站 `<@...>` 归一 + 限流/审计。
- 投递失败回传：桥 ack 带 `kind` / `shape` / `passive_window_open` / `text_preview`；外部会话条目记录 `active_message_state`；ack 路由按 `outbox_id` 去重后**每次失败都入** `delivery_failure` 心跳事件（无时间冷却）；失败回合内部可见、不默认向渠道回复；心跳提示词新增失败分支；`mention_member` 按被动窗口判据放行或拒绝。
- 测试：目标解析、未知目标拒绝、markdown 形状、outbox 重放、限流、入站归一；active_denied ack → 每次失败都产生心跳事件（同 outbox_id 不重复）、主动成功清状态、失败回合不向渠道发送默认回复、无被动窗口时 mention 拒绝。
- 文档：`external-agent-api.md`（出站 mentions、投递失败回传）、`sandbox-execution-plane.md` 只读白名单、`web-and-admin.md` 操作员 openid 说明、`heartbeat-system.md` 投递失败事件、`README.md` 群聊章节。

### P5 记忆分域

- `memory_scope_router.py`（新）+ bootstrap 接线；`message_builder` / `session_agent` / `_ceo_runtime_ops` / `ceo_sessions` 路由；`MemoryManager` scope/persona 开关 + memory agent 提示词；通知双 cursor；管理页 scope；配置包群目录；CLI。
- 测试：群记忆隔离、组合块、双通知、第三方私聊只读、群写入不碰本地、群 agent 不写人设、管理页 scope、配置包往返、worker 上限/关闭。
- 文档：`runtime-overview.md` Memory Runtime State、`sandbox-execution-plane.md` 记忆门删除、`web-and-admin.md` 记忆页 scope、`config-and-models.md` 配置包群记忆、`operations-and-maintenance.md` 群记忆目录。

## 验收

静态/单测：

- `python -m ruff check .`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- 相关：`tests/test_sandbox_trust_p0.py`、`tests/test_sandbox_mode_state.py`、`tests/resources/test_memory_current_admin.py`、`tests/resources/test_frontdoor_persona_block.py`、`tests/resources/test_memory_revision_notice_lane.py`、`tests/resources/test_qq_official_bridge.py`、`tests/resources/test_external_turns_contract.py`、新增 group memory / limited / cron throttle / mention 用例。

真实运行验收（重启后）：

1. 网页本地会话写记忆 → 群会话能读到；群会话写记忆 → 本地会话看不到，群内可见；本地记忆不被群写。
2. 群里收到记忆/人设通知；本地记忆更新时群也能收到增量；群记忆更新只有该群收到。
3. 群会话默认受限模式：能写会话工作目录内的文件；`web_fetch` 默认可用；`exec` 默认不可用并返回拦截文案；不能派任务；切到沙盒/全权限后提示词、tools、合同同回合变化，并收到模式切换通知。
4. 受限模式下权限面板显示「允许使用的工具列表」；默认不显示「命令行风险拦截设置」。勾选 `exec` 保存后，下一回合 `tools[]` 出现 exec，命令行设置按钮同时出现，exec 按 governed 执行；取消后两者一起消失。勾选/取消其他工具、固定基础项不可改、口令错误拒绝保存、恢复默认，均按 G 节验收。
5. 群里 `schedule_wake` / `cron` 可用；60 秒内的第二次主动推送被延迟，最终唤醒带延迟原因；`cron list` 只看本群；`create_async_task` 仍拒。
6. `mention_member("Master")` 在 Master 已发言且群有主动权限时发出 markdown @；未知编号/原始 openid 被拒；没权限时日志有 `active_denied`/`undeliverable`，不假报成功。
7. 两条不同的主动消息都被平台拒绝 → 两次失败各自唤醒模型；同一条消息的重复 ack 只唤醒一次；失败回合默认不向渠道发消息；有被动窗口时 `mention_member` 可发，没有时返回拒绝。
8. 记忆页切换本地/群作用域可查看、编辑、删除；配置包导出/导入保留群记忆。

## 风险与未决

- 群 manager 多实例：每群一个 worker + sqlite 连接；默认 LRU 32，超出关闭最久未用；需要观察线程/内存。
- 组合记忆块：本地 + 群各自保留现有 document cap，合并后可能到数万字；先按“本地在前、群在后、按各自 cap”实现，真实上下文成本在验收里量。
- 群聊能看到本地记忆是产品要求，但隐私只靠提示词约束；本地记忆里不要放不可对群暴露的凭据类内容，文档写明。
- 群记忆 agent 看不到本地记忆，可能产生重复条目；先接受，后续可给群 agent 只读本地摘要（不在本计划）。
- 模式切换会让 provider 前缀缓存整段失效一次；这是“立即替换提示词”的代价，已确认接受。
- 会话工作目录无自动清理，会随会话累积；磁盘上限/清理策略另开。
- 定时任务 60 秒节流按会话；如果有大量群任务同时到点，会串行延迟，延迟说明只在最终唤醒时出现。
- `JAIL_BACKENDS_AVAILABLE=False`：当前装好沙箱也不会进“沙盒模式”，需要另行启用 `G3KU_SANDBOX_ENABLE_JAIL` 并跑验收；模式提示按实际 tier 写。
- 群主动消息权限是平台能力，不由本仓库决定；@ 和定时提醒都可能被拒。
- 第三方私聊只读本地记忆；如果未来要给私聊独立记忆域，复用群作用域机制即可。
- 配置包记忆面整包替换会把目标机已有群记忆一起替换；文档必须写明。
- 每次投递失败都唤醒模型：失败越频繁、模型调用越多；失败回合内部可见且不默认向渠道回复，去重只按 `outbox_id`。如果模型反复在失败回合里产生新的主动消息，仍可能形成新的失败；提示词要求 `silent` 或在被动窗口内 @，需要实盘观察。
- 按会话工具列表完全可编辑：勾选 `exec` / `process_kill` / `create_async_task` / `skill-installer` / `register_stickers` 会真正扩大该会话的能力面；对话框给后果说明、保存要口令、审计记录。`exec` 强制 governed，但任务 worker / 工具安装目录 / 全局贴纸注册表不受会话工作目录约束。
- `web_fetch` 默认勾选意味着群会话默认拥有一个外发通道；如果某个群不需要，operator 在工具列表里取消。关掉后该会话不能联网。
- 工具列表 override 只作用于 `limited` 档；切到 `jailed`/`host_full` 时忽略，切回 `limited` 时重新生效；删除会话要清理 override。
- 工具列表变更会触发一次 system prompt / pinned contract 重印，和模式切换一样有一次前缀 cache miss。
