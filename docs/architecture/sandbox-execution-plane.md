# 沙箱执行面（信任分级 / 只读档 / 读根限定）

本层回答一个问题：**这一轮是谁在要求运行时做事，它因此能触到哪些东西？**
会话本身不带答案——QQ 群里的陌生人和操作员本人在转录里长得一模一样——所以答案由
三样输入解析出来：会话信任级、会话档位意图、沙箱安装态。

边界划分沿用"控制面 / 数据面"：

| 面 | 内容 | 执行位置 |
|---|---|---|
| 数据面 | 命令、文件读写、内容读取、进程控制、网络抓取 | 宿主（`host_full`）/ 宿主进程内受限（`readonly`）/ jail（`jailed`，后续阶段） |
| 控制面 | 记忆写入、任务与节点、cron、模型配置、治理 | 恒在宿主，由档位判定准入 |

判据只有一个实现：`g3ku/sandbox/policy.py`（档位与工具面）与
`g3ku/runtime/session_trust.py`（信任级）。前门执行束、曝光层、节点执行道、
记忆门、QQ 桥都读这两份，不各自再判一次来源。

## 档位模型（意图 × 安装态）

会话只存**一档意图**（`Session.metadata["sandbox"]["mode"]`），实际执行面由**安装态**
解析，档位本身不入库：

| 安装态 | `restricted` | `unrestricted` |
|---|---|---|
| `not_installed` / `runtime_missing` / `partial`（该信任档必需维度不全） | `readonly` | `host_full` |
| `ready` | `jailed` | `host_full` |

- **默认意图按信任派生**：`third_party*` → `restricted`，`owner` → `unrestricted`。
  操作员本地会话因此保持既有行为（宿主执行 + 现有 guards + 记忆注入 + 可派任务）。
- **装/卸沙箱不改任何会话元数据**：意图不动，档位随安装态变；`restricted` 会话在
  安装后自动升 `jailed`，卸载后自动回 `readonly`。
- **`partial` 只服务 owner**：`third_party` 的必需维度（`fs_read` / `fs_write` / `network`）
  缺一维就不给它 jail——把"缺一维"当"全缺"或反过来，两种都是假安全。
- 档位变化**只在回合边界生效**，不打断在飞回合。
- 空会话键（无会话上下文的装配、CLI、检查实例）按 `host_full` 处理：外部渠道的每一轮
  都带 `ext:` 键，走不到这一支；按第三方处理会让所有无会话的装配路径失去工具。

本层当前只解析出 `readonly` 与 `host_full`；jail 后端作为后续阶段接入，
届时 `resolve_tier` 的 `jailed` 分支生效，工具面与读根判据不变。

## 信任分级

| 信任级 | 判据 | 默认意图 |
|---|---|---|
| `owner` | 非渠道会话（`web:` / `internal:` / CLI），或私聊发送者在白名单 | `unrestricted` |
| `third_party_dm` | `ext:` 私聊且发送者未列名、缺 sender、或注册表无法分类 | `restricted` |
| `third_party_group` | 多人会话（`group` / `guild`，按 `chat_type` 或 `external_key` 前缀） | `restricted` |

三条硬规则：

- **群聊整会话按第三方**：群消息只带 `member_openid`（无昵称），它与 c2c 的
  `user_openid` 属两套 id 空间，"群里谁是操作员"在协议层不可判定；即便发送者在白名单里，
  也不把群会话升级成 owner。
- **信任只降不升**：注册表条目记"见过的最受限信任级"，一条自称操作员的后继消息不能
  把先前的第三方判定抬回去。
- **失败关闭**：空 sender 永不等于操作员；未登记的 `ext:` 键与 `china:` 归档键按第三方。
  服务端只认 `chat_type` 与 sender 白名单派生信任，**不接受桥自报 `trust`**。

白名单是服务端配置：`qqBot.accounts.<appId>.ownerOpenids`（官方 QQ 适配器的
`qq-official-<appId>` 桥身份按 AppID 反查）与 `externalApi.tokens.<id>.trustedSenders`
（第三方桥）。配置项的落点见 `config-and-models.md`「external_api」与
`external-agent-api.md`「入站身份契约（`sender` / `chat_type`）」。

`/api/v1` 入站契约：`sender{id,name}` 与 `chat_type`（白名单 `group` / `dm` / `guild` /
`guild_dm`，非法值 400）；二者与 `external_key` 前缀冲突时**取更严的一档**并留 WARNING。
服务端把派生结果写进 `metadata["external_trust"]` 与 `metadata["chat_type"]`，桥不能覆盖。

## 只读档（`readonly`）

没有 jail 时，只读档是第三方会话的地板。它不是"给工具加路径限制"的泛称，
而是一份**默认拒绝的名单**：未登记的工具名一律不可调用。

| 面 | 组内工具 | third_party `readonly` | owner `readonly` |
|---|---|---|---|
| 读类数据面 | `content_open` / `content_search` / `content_describe` / `filesystem_stat` / `view_message_history` | 放行（受读根限定） | 放行（受读根限定） |
| 常驻信号 | `silent` / `time` / `load_tool_context` / `load_skill_context` | 放行 | 放行 |
| 贴纸目录 | `search_stickers` | 放行 | 放行 |
| 写类数据面 | `filesystem_write` / `edit` / `copy` / `move` / `delete` / `propose_patch` | 拒 | 拒 |
| 进程 / 网络 | `exec` / `process_kill` / `web_fetch` / `skill-installer` | 拒（见下） | `exec` 强制 `governed`，`web_fetch` 放行，其余拒 |
| 控制面 | 记忆、任务族、`send_message`、`cron`、`schedule_wake`、模型配置 | 拒 | 记忆照 RBAC，任务族拒 |
| 其他一切 | MCP 工具、skill 工具、资源工具、将来新增的工具 | **默认拒** | **默认拒** |

两档的强度不同，这点不能混：

- **third_party 只读是安全边界**，用白名单。新增资源工具要进这一档，必须显式登记到
  `READONLY_THIRD_PARTY_TOOL_NAMES` 并声明它读什么根；未声明一律按不可用。
- **owner 只读是防手滑**，用黑名单（写类 / 进程控制 / 任务族 / 会写盘的联网工具）。
  其余工具与设置照旧，`exec` 的档位被压回 `governed`（形态守卫 + 路径策略 + 工作区监禁
  三层同时生效），即便工具记录里的执行模式是 `full_access`。
- **`exec` 在 third_party 只读档整条拒绝**：它的只读形态守卫是正则黑名单
  （`g3ku/agent/tools/shell.py` 的 `_enforce_read_only_command`），`os.system('rm …')`
  这类写法能绕过去。它是防误操作的形态层，不是边界，因此不给第三方会话。
- 只读档约束的是**数据面**。控制面对 owner 照旧按 RBAC（所以 owner 的只读档仍能写长期记忆），
  对 third_party 本来就全拒。

**受限档一律不能派异步任务**：`create_async_task` / `task_*` / `send_message` / `schedule_wake`
在 `readonly` 与 `jailed` 下都拒，owner 也一样。任务节点跑在 worker 进程里，受限意图
传不进那条执行面；与其做一个"名义只读、节点里全权"的假档，不如直接不给派。

判定在两层同时生效，缺一层就是绕过：

1. **曝光层**：候选与 callable 名单在执行前收窄（`CeoExposureResolver`、
   `_frontdoor_callable_tool_names_for_state`）。模型看不到，就不会去调。
2. **执行层**：`_approval_request_for_tool_calls` 对名单外调用无条件生成打回项——
   不查治理开关、不查风险度，`all_pass` 也放不开。**收窄必须发生在常驻内置并进来之后**，
   先减后并等于没减。

## 只读档的读根限定

名单里的读类工具必须同时限定"能读哪"。根清单由前门写进
`runtime_context["fs_read_roots"]`：

| 档位 | 读根 |
|---|---|
| `third_party` `readonly` | 该会话的入站上传目录（web 上传目录 + `external-uploads/<会话>`）、会话 temp 目录 |
| `owner` `readonly` | 上列 + 宿主工作区 |
| `host_full` / `jailed` | 不下发（不设限 / jail 自己管） |

实现约定：

- 判定在 `Path.resolve()` **之后**做，symlink 指到根外与直接写绝对路径一样被拒。
- 运行时根**优先于**工具设置推出的根：只读档不能被 `restrict_to_workspace` 之类的宽松
  设置反向放宽。
- 工具实例在资源层共享，会话根不能写到实例上：执行闸门（前门
  `_execute_tool_call_with_raw_result`、节点 `_execute_tool_raw`）用 `ContextVar`
  在调用期 pin，`with` 退出即还原。ContextVar 按 asyncio 任务隔离，并发会话不串根。
- 统一实现是 `g3ku/utils/path_roots.py`：`normalize_roots` / `enforce_roots` /
  `runtime_read_roots` / `pinned_read_roots`。`content_*`、`filesystem_*`、
  `filesystem_stat`、`propose_patch` 都消费同一份。
- 越界一律 `PermissionError`，工具层翻成 `sandbox_denied` 形态的可见拒绝；模型应改道找
  操作员上传文件，而不是重试同位路径。

## 控制面拒绝

名单外调用走**既有的合成拒绝车道**（渠道会话没有审批界面那条），不新开中断：

1. `_approval_request_for_tool_calls` 产出 `review_items`，策略项带
   `reason="trust_policy"`，审批项带 `reason="approval"`；整批带 `auto_reject`。
2. `_graph_review_tool_calls` 见 `auto_reject` 即调 `_auto_reject_channel_review`：
   可执行的那半照常执行，被拒的那半按原顺序生成合成结果。
3. 两类拒绝的回执文案不同——策略拒绝说明"这是第三方会话，该由操作员在自己的会话里发起，
   不要重试"；审批拒绝说明"渠道会话没有审批面"。
4. `approval_status`：渠道会话 `channel_auto_rejected`，纯策略打回 `policy_auto_rejected`。

## 记忆门

全局长期记忆是工作区级的，注入在每个会话的 index 1，所以群聊内容一旦写进去，
操作员之后的每一次对话都会被污染。判定同样只有一处：
`MemoryManager.review_eligibility(session_key)`，生产实例由 bootstrap 注入
"`is_owner_session`"，CLI 与检查实例不注入（全允许）。判据自身抛错按**不可信**处理。

四个入口，缺一处就是绕过：

| 入口 | 不合格时的行为 |
|---|---|
| `record_turn_for_review` | 不入缓冲，返回 `ignored` / `untrusted_session` |
| `flush_review_window`（按龄 / token 压缩 / 会话边界三条冲刷道共用一个口） | 清缓冲、不入队，返回 `discarded` |
| 队列消费（`run_due_batch_once`） | 按 `session_key` 拆批：不合格条目终态化 `discarded`，可信条目照常处理 |
| `memory_update` 工具 | 直接返回可读拒绝（`ok=false`），不入队 |

另外两条配套：

- **启动清扫**：管理器启动时把 `review_state.json` 里不合格会话的缓冲摘掉——闸门只管新写入，
  升级前已缓冲的轮次仍会在下一次按龄冲刷时进全局记忆。
- **成功信号自动重排**排除整批不可信的停车记录；混合批次照常重排，消费侧再拆。

读侧同样按同一判据：第三方会话的长期记忆快照返回空串（不是"读失败"，所以冻结值是稳定的
空），记忆修订增量与人设增量都不投。人设默认空，配置入口在沙箱页面（不依赖安装态）。

## 来源标注

第三方消息在模型可见投影里带一行前缀：

```
[消息来源] 群消息 | 发送者 member_op… | 第三方，非操作员
```

- 只装饰**第三方**消息；操作员的消息不盖（噪声，且会让存量 `owner` 会话的稳定前缀无谓变一次）。
- 装饰值派生自记录里的固定字段（`external_trust` / `external_sender` / `chat_type`），
  跨回合字节稳定，不打断 provider 前缀缓存。
- **转录保持原文**：RAG、网页回放、导出都吃 raw content。
- 所有按内容相等性的比较先 `strip_sender_stamp`（与 `strip_arrival_time_stamp` 同一纪律）：
  `_cron_prompt_equal`、暂停回合种子去重两处是已知比较点。
- 缺 `external_trust` 的历史记录不装饰——无法判定其来源时宁可不标。

实现：`g3ku/runtime/sender_stamp.py`（渲染与剥离）、
`g3ku/runtime/frontdoor/message_builder.py`（历史道与当前轮兜底道同一作者）。

## Debugging Entry Points

- 群里让它"记住 X"后全局记忆变了 → 先看 `memory/ops.jsonl` 的 `session_key` 与
  `memory/review_state.json`；四处闸门任一漏改都会表现成"只有某一条车道漏了"。
- 群里让它读 `config.json` / `master.key` → 看该会话的 `external_trust` 是否为第三方、
  工具是否落在只读白名单里、`fs_read_roots` 是否下发（前门 `_sandbox_read_roots`）。
- 群聊工具"看不到但不报错" → 曝光层收窄；"看得到但被拒" → 执行层打回，两者文案不同。
- 群聊本来能用的工具突然没了 → 查注册表条目的 `trust`（粘性，只降不升）。
- 装了/卸了沙箱但会话档位没变 → 安装态是全局读数、档位在回合边界才生效。
