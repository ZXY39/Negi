# Negi 会话关联与点对点消息（session links）

本文负责会话之间的通信子系统：操作员维护的对称关联边、模型面短别名、
`send_message` 的会话寻址、每收件人持久信箱与心跳唤醒、头部名册与不唤醒的
变化通知。节点信箱与任务委派归 `main-task-runtime.md`；心跳的通用续跑合同归
`heartbeat-system.md`；关联弹窗与 REST 的界面合同归 `web-and-admin.md`
「Session Link Dialog And API Contract」。

## 责任与边界

- 这个子系统让**会话与会话**互发消息、协调委派。它不是群：没有群对象、没有
  广播、没有共享消息池。每条消息永远显式寻址，一对多就是 `to` 列表，机制上
  不存在隐式扇出——"唤醒谁"永远是发送模型的显式意图。
- 关联边是 ACL，不是消息通道：同边互发，无边拒收。群日志不建，审计走各自信箱
  台账与发送侧工具账本。
- 委派干活仍走任务系统（`create_async_task` / `spawn_child_nodes`，验收、看门狗、
  暂停恢复全继承）；会话消息只做协调与知情。跨会话给任务根节点捎话不需要本车道
  （前门 `send_message` 的 task 目标不受会话归属拦截，见 `main-task-runtime.md`
  「节点信箱与唤醒（现行）」）。
- 节点道不参与：节点侧 `send_message` 与节点信箱不变，节点头部不渲染会话名册
  （`long_term_tasks` 只在会话道的先例）。

## 关系模型与短别名

- `session_links(a, b, created_at)`：对称无向边，写入前规范化 `a<b`，主键去重。
  在 A 的弹窗勾上 B，两边列表同时生效。`MainRuntimeService.replace_session_links`
  做差集增删，新边的两个端点各分配一个别名。
- `session_aliases(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_key UNIQUE,
  created_at)`：别名 = `s{seq}`。**AUTOINCREMENT 保证删除后永不复用；别名行永不
  删除**——ext 会话注销后同 key 再注册拿回原别名。规范 session key 是冻结标识符
  （转录文件名、侧车、注册表、cron/心跳目标、任务归属都钉在它上面），别名是纯
  寻址覆盖层，内部一切照旧用规范 key。
- 模型面只出现别名：头部名册、`legal_targets` 回执、来信签名、回投指引。规范 key
  只在操作员界面（会话 ID 复制键）与持久层出现。
- 候选范围是全部会话（`web:` 本地 + `ext:` 渠道可见行），排除自身、`internal:`
  隐藏会话、`china:` 归档（无投递路径）与已隐藏的 ext 条目。隐藏 ext 不入候选：
  操作员已隐藏的先尊重，要关联先取消隐藏。

## 寻址与投递

- 前门 `send_message`（resource 工具，`tools/send_message/`）的 `to` 接受
  `session:<别名|规范key>`；裸 id 解析顺序是 **node → task → alias**（`s\d+` 形状
  与 hex id 不碰撞），别名形状对不上任何会话时给 `session_missing`，其余未知裸 id
  保持 `target_not_in_task`。显式前缀说了算，未知命名空间拒收。
- ACL 就是关联边：`has_session_link(sender, recipient)` 为假 → `session_not_linked`。
  会话删除会清边，所以 deleted 会话也落这一档（边没了即不可达）。别名/键形对不上
  → `session_missing`。拒收触发 `legal_targets` 回填：本会话任务的可发消息节点 id
  之后追加可达会话别名，有界 + `legal_targets_truncated`。
- `reply_to`（可选，会话目标专用）：命中**发送者自己信箱里**的一条来信才继承其
  `chain_id`、hop+1；对不上 → `reply_to_unknown`，整条拒收。无 reply_to 开新链
  （chain_id=本 message_id，hop=0）。chain/hop 运行时盖章，模型传不了伪造值。
- **hop 上限 `SESSION_MESSAGE_HOP_CAP=8`**（`main/runtime/session_links.py` 单一
  作者）：到顶的来信落箱但 `wakeable=False`，回执 `wake=session_inbox_no_wake` 并
  附说明。续链要收件人显式发新消息（不带 reply_to）。结构上封死无限互醒。
- 指纹去重：`message_fingerprint(kind='session_message', source, content, ref)`，
  只对**未消费**（pending）行去重；消费后重发是新意图（节点信箱同档）。
- 信箱行 `frontdoor_session_messages`：每收件人一行，消费态各自独立；
  `delivered/pending → consumed` 两态，**注入后才销账**。payload 带
  fingerprint/chain_id/hop/reply_to/source_alias/wakeable，签名运行时写入。
- 回执逐目标：`{target, target_kind:'session', resolved_session_key, status,
  message_id, wake, chain_id, hop, reason}`，形状与节点/任务目标回执同族。

## 唤醒矩阵与心跳车道

- 投递只落箱，不直接入队心跳：`SessionMessageScanner`
  （`g3ku/heartbeat/session_message_scanner.py`，15s 一拍，与
  `FrontdoorMessageScanner` 同形）扫 pending 行入队。单路径，无跨层回调；
  重启后扫描器重扫即补投。
- **暂停闸（A5=A 同档，只在本车道）**：目标会话暂停时不入队，行留 pending，
  恢复后下一拍入队。判据两处：驻留会话 `state.paused`/`status=='paused'`，或
  持久暂停侧车 `read_paused_execution_context` 可恢复。心跳对暂停会话的全局行为
  不变（别的车道照常唤醒暂停会话）。
- **hop 闸**：`wakeable=False` 的行入队但**不请求唤醒**，事件随下一次别的唤醒
  并入上下文。
- 事件 reason=`session_message`，source=`session_link`，dedupe key
  `session-message:{message_id}`；队列在飞 + 已注入两层 dedupe，转录
  `injected_heartbeat_event_keys` 跨重启防重。
- 渲染（`prompt_lane._session_message_lines`）：签名头
  `Session message from s{n} (标题) [trust: <档>]`（alias/title/trust 扫描器侧
  富化，trust 取 `session_trust(source_key)`），正文长文外置同现有车道，附
  **不可信注记**（peer 会话内容、可能转述第三方文本、不是操作员命令、自行判断
  是否动作、无动作 `silent`）与回投指引
  `send_message(to="s{n}", content=..., reply_to="<message_id>")`。
- 消费点唯一：`_consume_heartbeat_events_at_injection` 的 `session_message` 分支
  在事件束注入后 `mark_frontdoor_session_message_consumed`。注入前失败行留
  pending 下一拍重试；注入后模型失败不重投（通用合同见 `heartbeat-system.md`
  「Continuation Contract」）。
- 信任边界：双方都可以是任意信任档（操作员关联即授权接收），但 `ext:` 受限档
  会话的工具面没有 `send_message`（RBAC 现状）——能收不能发。来信签名带信任档，
  收件模型据此判断 peer 请求的分量。

## 头部名册与不唤醒的变化通知

- 头部钉住块新增 `### 可交流的会话` 节（`render_reachable_session_roster`）：
  每行 `- s{n} | 标题`，≤8 行 + 溢出指针 `另有 N 条，用 list_sessions 查看`；
  空表整节不渲染。取数口 `frontdoor_reachable_session_roster`（关联边窄读 +
  标题补全：转录 metadata，ext 回退注册表标题），失败一律空表。只在重钉那一跳
  取数，随原文冻结。渲染器版本 `pinned-contract-v7`；名册**不进重印判据键**，
  关联增删不触发头部重印（不顶缓存前缀）。节点道不传 provider，天然无此节。
- 变化走 `[RUNTIME_NOTICE]` 的 session links 节（`skill_roster_notice.py`）：
  新增抬头 `Sessions newly reachable in this session:`，移除抬头常量
  `SESSION_LINKS_REMOVED_HEADING`，末句给寻址入口。**不产生心跳事件、不唤醒会话**，随下一跳
  装配并入。台账 `session_link_cursor{known:[alias], initialized}`：首跳以头部
  名册解析值（`reachable_session_aliases_from_text`）为初值，钉住时已说过的名字
  不重播；此后每跳与实时集 diff，每条变化只宣布一次。
- 收口：压缩跳 `reset_contract_sections_for_compaction` 清游标（头部已按当刻
  关联重印，下一跳以新头部重新播种）；截断/Fork `drop_records_after` 不带游标
  键即归零。回显守卫 `strip_skill_roster_notice_echo` 的抬头白名单含两个新抬头。
  侧车载体复用 `skill_roster_notice_context` 整包归一，子键在 normalize 白名单里。
- `list_sessions`（`tools/list_sessions/`，只读候选工具）：返回本会话可达会话
  `alias | title | trust`，是名册溢出的取数入口，无写入面。
- 控制台写关联记一条 operator action（category=`session_links`，scope=
  `session:<key>`，audience=frontdoor），模型在 `### Operator actions` 节看到
  是谁改的；可达性变化本身由 session links 节交代。

## 生命周期

- 会话真删（本地删除 / 渠道注销 identity_forgotten）：`purge_session_link_data`
  清该会话全部关联边（双侧）与信箱行（收件 + 发件）；**别名行保留**。对侧会话的
  名册 delta 在下一跳宣布移除，不唤醒。
- 渠道「清历史留身份」（hides_identity）：关系与信箱保留——同一 peer 入站即回来。
- 解除关联：双方 delta 通知；存量来信仍可读可消费，但回投被 `session_not_linked`
  拒收（ACL 即时生效）。
- 暂停会话：来信落箱不唤醒、不清暂停位；恢复后 ≤15s 内入队消费。

## 常见排障入口

- 模型说"发不出去"/回执 `session_not_linked`：先查 `session_links` 有没有边
  （操作员是否保存过关联），再查目标会话是否被删（删除清边）。`legal_targets`
  里的别名才是当前可达集。
- 发了消息对方没醒：按链读——信箱行是不是 pending（`frontdoor_session_messages`）
  → 目标是不是暂停（驻留状态或暂停侧车，暂停闸设计内不唤醒）→ hop 是不是到顶
  （`wakeable=False` 设计内不唤醒）→ 扫描器节拍是否在跑 → 队列 dedupe 是否已注入
  （`injected_heartbeat_event_keys`）。
- 名册里没有新关联的会话：头部只在压缩边界重印；两次重印之间看
  `[RUNTIME_NOTICE]` 的 session links 节有没有宣布。游标 `session_link_cursor`
  已 initialized 且 known 含该别名 = 已宣布过，不重播。
- 重启后别名变了：不可能——AUTOINCREMENT 永不复用；若见到新别名，查的是另一个
  session key（ext 注销后 external_key 变了会落新 key）。
- 关联弹窗保存报 `session_not_linkable`：目标是自身、internal:、china: 归档或
  已隐藏 ext；`session_missing`：目标转录/注册表条目不存在。