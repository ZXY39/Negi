# 沙箱执行面 + 第三方信任分级 实现计划

日期：2026-10-26
最后更新：2026-10-27（第四轮拍板：**安装态驱动的档位模型**，见 D17–D21）
状态：**P0 完成**；**P1 的第一步（档位开关 + 安装态探测）完成**；
jail 本体（P1a 剩余）、安装器（P1b）、前端面板仍未开工。分支 `feat/sandbox-trust`。
取证基线：`main` @ `53fbf30f`。标 † 的是子代理读数，尚未逐行复核，动对应阶段前先复核。

> 工作树警告：本计划落笔时工作树**不干净**，已有他人/前序会话的未提交改动
> （`docs/architecture/README.md`、`docs/architecture/main-task-runtime.md`、
> `main/monitoring/log_service.py`、`main/runtime/chat_backend.py`、`main/runtime/node_runner.py`、
> `main/runtime/react_loop.py`、`main/runtime/task_actor_service.py`、`main/service/runtime_service.py`、
> 三个 tests、未跟踪的 `work/sticker-send-plan.md`）。
> 本计划只新增本文件，不碰这些路径。**P2 要动 `main/runtime/*`，动手前必须先确认那批改动的归属与行号漂移。**

## 一句话结论

沙箱不是"给工具加路径限制"，而是**新增一个执行面**：宿主只留控制面（模型调用、记忆、任务库、审批、审计），
数据面工具（`exec` / `filesystem_*` / `content_*` / 进程控制）全部经一条 fail-closed 的 seam 进
**每会话一个持久 jail**；jail **看不到宿主任何目录**，只有 `/workspace`（会话目录）与
`/shared/{inbox,outbox}`（人工导入导出桥）。放权放在 jail 里面（命令、装包、长任务、经代理联网都放开），
边界放在 jail 外面。

jail 的实现是**可插拔后端，不绑死容器**：Linux 用 bwrap、macOS 用 Seatbelt、Windows 用「专用 WSL2 发行版 +
内部 bwrap」，已装 Docker/Podman 的机器可选容器后端。**没有常驻守护进程、没有监听端口**——每次调用包一层 argv
（DeepSeek CVE-2026-82533 的直接教训：沙箱能打到自己的控制面，等于没有沙箱）。

安全收益的主体其实**不依赖任何后端**：P0（信任分级 + 记忆门 + 控制面拒绝 + **只读档**）单独上线就已经堵住
"群成员一句话写进全局记忆 / 调 `exec` / 派任务 / 读宿主文件"这四条今天活着的路径。jail 是 P1 才付的成本。

**项目默认不装沙箱**。会话档位只有两档意图——`restricted`（受限）与 `unrestricted`（全权限）——
装没装沙箱决定这两档意图落到哪个执行面：未安装时 `restricted` = **只读档**（宿主进程内、读根限定、禁写禁进程禁外网），
安装后 `restricted` = **沙箱档**（jail 内、能力放开）。所以**装/卸沙箱不需要迁移任何会话元数据**，
UI 标签跟着安装态换字（只读↔全权限 / 开启沙箱↔关闭沙箱），存的是同一个字段。

## 背景与问题（已核）

### 1. 今天群聊消息与操作员消息在运行时不可区分

- QQ 桥只把正文和附件交给 `/api/v1`，**不传发送者**：`_dispatch` 只取 `content` / `id` / attachments
  （`g3ku/qq_official/bridge.py:1263-1275`），`on_incoming` 签名里也没有 sender（`:648-655`），
  `ExternalApiClient.send_message` 只有 `text` / `attachments` / `idempotency_key`（`g3ku/qq_official/client.py:48-67`）。
- 服务端**支持** `sender` 与 `metadata`，但只写进 `UserInputMessage.metadata["external_sender"]`
  （`g3ku/runtime/api/external_v1.py:938-952`、`:842-859`），投影时不渲染 —— 模型看到的仍是一条裸 user 消息。
- 群/单聊的区分只活在 external_key 前缀里（`qq:group:` / `qq:c2c:` / `qq:guild:` / `qq:guilddm:`，
  `g3ku/qq_official/messages.py:32-33`），会话键是 hash（`ext:{bridge}:{sha1}`），
  运行时侧要靠注册表反查（`g3ku/runtime/external_sessions.py:203`）。
- botpy 群消息的 author **只有 `member_openid`，没有昵称**（`.venv/Lib/site-packages/botpy/message.py:238-258`），
  c2c 只有 `user_openid`（`:263-280`）。所以"群里谁是操作员"在协议层不可判定 → 群聊只能整会话按第三方处理。

### 2. 记忆是全局的，群聊内容会污染所有会话

- 复核窗口对**所有**可见会话生效，只排除 `internal:` 前缀与 silent 轮
  （`g3ku/runtime/session_agent.py:4123-4150`）；心跳/cron 内部轮也会把 assistant 文本送进去（`user_messages=[]`）。
- 复核载荷把用户原文逐条交给记忆 agent（`g3ku/agent/memory_agent_runtime.py:5370-5394`），
  批次里带 `session_key`（`:5927`）但消费侧**没有任何来源过滤**。
- `snapshot_text(**_)` 忽略 channel/chat_id（`g3ku/agent/memory_agent_runtime.py:2092-2095`）——
  记忆是工作区级全局的，注入在每个会话的 message index 1（`g3ku/runtime/frontdoor/message_builder.py:78-96`、`:1207-1218`）。
- `memory_update` 是常驻内置、恒可调用（`g3ku/runtime/tool_visibility.py:5-15`、
  `g3ku/runtime/frontdoor/_ceo_runtime_ops.py:4098-4100`），直接入队（`g3ku/agent/tools/memory_update.py:63-77`），
  **绕过复核窗口**。

### 3. 工具面默认全开，渠道审批档默认关

- 审批默认关（`g3ku/config/schema.py:775`）；关掉时 `_reviewable_tool_risk_map` 返回空
  （`main/service/runtime_service.py:10569-10602`、`main/governance/review_policy.py:56-61`），
  于是 `_approval_request_for_tool_calls` 不产生 review_items（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:5686-5734`）。
- "渠道会话需要审批的工具当跳打回"这条车道存在（`:5729-5730` 打标、`:8628-8629` 自动打回、
  `:8601-8607` 合成拒绝结果），但**前提是审批开着**。
- `exec` / `create_async_task` / `memory_update` / `memory_note` 都在常驻内置里，不需要 load 契约即可调用。
- `exec` 的守卫是命令形态层（deny 黑名单、路径策略、工作区监禁），且 `restrict_to_workspace` 默认 False
  （`g3ku/agent/tools/shell.py:134`、`:649-669`）；代码自己写明这不是 OS 沙箱（`:495-503`）。

### 4. 没有任何执行面隔离

- `exec` 直接宿主 subprocess（`g3ku/agent/tools/shell.py:304-320`、后台车道 `:808-825`），
  cwd 与 `TMP/TEMP/TMPDIR` 都锚在宿主数据根（`:855-907`、`g3ku/deployment/runtime_startup.py:28-64`）。
- `filesystem_*` / `content_*` 直接宿主 I/O，路径按宿主 workspace 解析
  （`g3ku/agent/tools/filesystem_mutation.py:323-390`、`g3ku/content/tool.py:23`）。
- 任务节点跑在 worker 进程里（`main/runtime/task_actor_service.py:537` → `main/runtime/node_runner.py:606`），
  worker 由宿主 `subprocess.Popen([sys.executable, "-m", "g3ku", "worker"])` 拉起（`g3ku/web/worker_control.py:141-161`），
  执行面按 `G3KU_TASK_RUNTIME_ROLE` 分（`g3ku/runtime/bootstrap_bridge.py:95`、`main/service/runtime_service.py:766`）。
- 仓库里**没有**任何 docker/podman/nsjail/bwrap 运行时调用；`Dockerfile` + `compose.yaml` 只是部署形态，
  两个服务共享同一批宿主卷（`compose.yaml:11-18`、`:36-43`，`Dockerfile:14-24`）。

### 5. 同类项目的实测教训（外部调研，2026-10-26）

- **DeepSeek Harness**：进程级文件效果沙箱（bwrap/Landlock/Seatbelt/Windows ACL），
  `ctx.sandbox.confine` 单一 seam、无后端即 `SandboxUnavailableError`（fail-closed）、上报 `full`/`partial`、
  区分 `denialSignatures`（沙箱正常拦截）与 `runnerFailureRules`（沙箱基础设施坏了）。
  **但网络与进程可见性被明确排除在沙箱语义外** → CVE-2026-82533（CVSS 9.4）：
  本地控制面 API 用 `Host` 头判"可信"，沙箱内一条 `curl` 就把自己切成 `danger-full-access`。
  另有公开报告：所有模式都能连本机特权 daemon（Docker daemon / ssh-agent）；browser 签名密钥在文件沙箱内可读。
- **Hermes Agent**：可插拔终端后端（local / docker / ssh / modal / daytona / vercel_sandbox）。
  `local` 明确是"受信操作员 shell"、不是沙箱；`docker` 是边界（`--cap-drop ALL`、`no-new-privileges`、
  `--pids-limit`、size-limited tmpfs、可选 `--user`）；大量集中式 env 剥离（provider key、gateway token、平台 secret）；
  egress 走 `iron-proxy`，沙箱只拿代理 token、真实 key 由代理注入；远端后端**自动** file sync + teardown sync-back。
  但 Docker 持久模式会 bind `/workspace`、`/root`，配置了 host_cwd 时还会把宿主 cwd 挂进去（Windows 盘符自动挂）。

→ 两条可直接抄的结论：**① 只围文件系统不够，必须同时切断网络与本机控制面；② 密钥不进沙箱，联网走持钥代理。**
→ 两条明确不抄的：**宿主 workspace 挂载**、**自动双向同步**（与"除非手动操作不同步"直接冲突）。

## 目标行为（验收口径）

1. 群聊会话里，模型**读不到也写不到**宿主 workspace、`.g3ku`、`memory/`、`sessions/`、`config.json`、`master.key`。
   - 沙箱档：由内核强制（最小根，不绑就不存在）。
   - 只读档（未安装时的默认）：由**读根限定**强制——所有带路径的工具调用都必须落在会话自己的读根内，
     越界返回 `sandbox_denied`；写类工具与 `exec` 直接不在名单里（P0-7）。
2. 群聊会话**不写**全局长期记忆：复核窗口不入队、`memory_update` 被拒、已排队/已停车的群来源批次不被消费。
3. 群聊会话上下文里**没有** `## 长期记忆` 块，也没有记忆修订增量通知（人设是否注入见 D7）。
4. 群聊里每条入站消息在模型可见投影里带来源标注（发送者 id + 第三方群聊），转录原文不变。
5. 群聊里控制面工具（记忆、任务、cron、模型配置、治理、`send_message`）一律被拒，
   拒绝走现有合成拒绝车道，模型拿到可读原因并能向群里解释。数据面按档位给：
   - 沙箱档：放开（命令、装包、写文件、长命令），但 `create_async_task` / `task_*` 仍拒绝（已拍板）。
   - 只读档：只给读类工具（`content_open` / `content_search` / `content_describe` / `filesystem_stat` /
     `view_message_history` / `search_stickers`），禁写、禁 `exec`、禁 `web_fetch`。
6. **未安装沙箱不再是"仅对话"**：群聊落**只读档**（默认），能读自己会话里的附件与产物、能查贴纸、能看本会话历史；
   读不到宿主任何东西、写不了、跑不了命令、联不了网。原 `conversation_only` 档**取消**。
7. 会话档位只有 `restricted` / `unrestricted` 两个值，**UI 标签随安装态换字**：
   未安装显示「只读 / 全权限」，已安装显示「开启沙箱 / 关闭沙箱」；装/卸沙箱**不改**任何会话元数据。
8. 沙箱页面按安装态换主动作：未安装显示「安装沙箱」（含确认页），已安装显示「卸载沙箱」。
   **文件管理（浏览/导入/导出/重置卷/包清单/磁盘占用）只在已安装时可用**；未安装时这些区块隐藏或禁用并给出原因。
9. 操作员能在网页上看到每个会话的档位与（已安装时）后端逐维度强制力；导入导出全程审计。
10. 沙箱不能改自己的策略：开关、审批、审计、记忆全在宿主，沙箱内无管理 API、无 Docker socket、无宿主端口可达。
11. 工具结果里能区分 `sandbox_denied`（策略拦截，模型该换路子）与 `sandbox_unavailable`（基础设施坏了，该告警）。
12. **owner 会话默认 `unrestricted`，行为与今天完全一致**（宿主执行 + 现有 guards + 记忆注入 + 可派任务）。
    只读/沙箱对它是可选项；owner 切 `restricted` 不需要口令。
13. third_party 会话切 `unrestricted`（未安装=全权限 / 已安装=关闭沙箱）：需**二次输入项目解锁口令** + 审计 +
    会话页常驻红色横幅；数据面回宿主，但控制面拒绝与记忆门**仍然生效**；`negi doctor` 可列出所有这类会话
    （残留风险见「风险登记」第 9 条）。**两种安装态下这道闸门一致**（D19）。
14. 受限档（只读或沙箱）**一律不能派异步任务**，owner 也一样——否则"只读"这个意图传不进 worker 节点的执行面，
    就成了口头约束而不是机制（D18）。

## 设计决策（本表是原始推荐；最终口径以下一节「拍板结果」为准）

| # | 决策 | 推荐默认 | 备选 |
|---|---|---|---|
| D1 | 群聊长期记忆 | **读写全断**（不注入、不可写） | 只断写保留读 / 按群独立记忆作用域（大改造，不做） |
| D2 | QQ 私聊默认信任 | **第三方**，`ownerOpenids` 白名单内才是 owner | 默认 owner（任何能私聊的人都有权限，不建议） |
| D3 | 群聊工具面 | **控制面一律拒绝**；数据面按档位：沙箱档放开（exec/装包/写文件），只读档只给读类工具；**受限档一律拒绝 `create_async_task` / `task_*`**（已拍板，D18） | 仅 `silent`/`time`（最保守，但群聊基本只能聊天；已被第四轮拍板的只读档取代） |
| D4 | 沙箱后端 | **可插拔**：Linux bwrap / macOS Seatbelt / Windows `wsl_bwrap`，容器（Podman/Docker）可选；**按需安装、不是必需依赖**（D12/D13 拍板，取代本行原始的"Docker/Podman 优先"） | microVM（P4）/ 远端沙箱服务（不做，数据出机） |
| D5 | 群聊能否关闭沙箱 | **允许关闭，需二次输入项目解锁口令 + 审计**；**关闭后数据面回宿主执行**（两轮拍板） | 只允许管理员在全局设置里关（更严，但与用户决定冲突） |
| D6 | 沙箱网络 | **默认 `none`**；owner 沙箱可开 egress 代理 + allowlist | 默认放开网络（等于给注入一条外传通道） |
| D7 | 群聊人设 | **默认不导入；用户在沙箱页面配置或导入**（已拍板）；长期记忆仍不注入 | 默认注入公开人设 |
| D8 | Windows 专属能力 | **沙箱内不支持**；需要则该会话不开沙箱（owner 手动决定） | Windows VM 后端（P4，重） |
| D9 | 沙箱粒度 | **每活跃会话一个持久容器**，任务复用同一容器 | 每任务一个容器（文件不连续、成本高） |
| D10 | 文件桥 | **只有 `/shared/{inbox,outbox}`，人工导入导出** | 自动 sync（Hermes 式，与需求冲突，不做） |

## 拍板结果（第一、二轮，2026-10-26；不再询问。第三轮见下节，**第四轮见「第四轮拍板」，档位模型以第四轮为准**）

- D1 群聊长期记忆：**读写全断**。
- D2 QQ 私聊默认：**第三方 + `owner_openids` 白名单**。
- D3 群聊工具面：**数据面宽 + 控制面拒绝**；**受限档（沙箱或只读）异步任务不可进行**（第四轮 D18 扩到 owner）。
- D4 后端：**Docker / Podman**，按需安装、不是必需依赖；**安装做到一键（运行时 + 镜像）**。
- D5 群聊沙箱：**可关闭，需二次输入项目解锁口令**；**关闭后数据面回到宿主执行**（控制面拒绝与记忆门仍生效）。
  第四轮 D19 把同一道闸门扩到未安装态的「全权限」切换，两态口径一致。
- D6 网络：**默认 `none`**，owner 沙箱可开 egress 代理。
- D7 群聊人设：**默认不导入**，用户在沙箱页面配置/导入（沙箱作用域人设，不做文件级复制；
  第四轮 D20：该配置区块**不依赖安装态**）。
- D8 Windows 专属能力：**沙箱内不支持**；需要则该会话不开沙箱或等 P4 Windows VM。
- D9 粒度：**每活跃会话一个持久容器**。
- D10 文件桥：**只有 `/shared/{inbox,outbox}`，人工导入导出**。
- D11 默认档位：**群聊/频道与未列名私聊默认 `restricted`，本地 `web:`/CLI 与白名单私聊默认 `unrestricted`**，
  权限面板可逐会话切换（第四轮细化为 D17 的两档意图模型）。
- UI：**所有会话的权限面板都可切换档位**；未安装时该面板显示「只读 / 全权限」并附一行「沙箱环境未安装 → 去安装」，
  已安装时同位置显示「开启沙箱 / 关闭沙箱」（第二轮 + 第四轮拍板合并口径）；**新增沙箱页面**
  （未安装＝安装，已安装＝卸载 + 检查 + 内容管理）。
- 交付顺序：**P0 先发**（零后端依赖）；P1a 沙箱本体、P1b 一键安装分开验收。

### 第三轮拍板（2026-10-26，后端选型）

- **D12 Windows 群聊后端**：默认 **`wsl_bwrap`**（专用 WSL2 发行版 + 内部每会话 bwrap jail），
  已装 Podman/Docker 的机器可选 `container` 后端。理由：Podman 在 Windows 上本来就是 WSL2 VM
  （官方口径"each Podman machine is backed by a virtualized WSLv2 distribution"），直接用 WSL2 少一层，
  且与 Linux 的 `os_native` 共用同一份 bwrap profile 实现。
  **关键前提**：光有一个发行版**不等于每会话隔离**（所有群会话共用一个文件系统＝群 A 能读群 B），
  所以发行版内部必须再做每会话 bwrap jail。
- **D13 `os_native` 范围**：Linux **bwrap**（必要，既是 Linux 默认后端也是 `wsl_bwrap` 的内核）+
  **Landlock 兜底**（只给写约束，判 `partial`，**不服务群聊**）+ macOS **Seatbelt**（零安装，但
  `sandbox-exec`/SBPL 已被 Apple 标记 deprecated，按"能用但不演进"对待）。
  **不做 Windows 原生受限令牌**：DSH 自认 `partial` 且"读取不受限"，实现要一整套 Win32 FFI，性价比最低；
  Windows 上 `os_native` 直接判不可用（安装态落 `not_installed`，`restricted` 会话解析为只读档）。
- **D14 `additional_writable_roots`**：必须支持额外可写根清单。Negi 的交付物常落在桌面/输出目录
  （`g3ku/resources/tool_settings.py:47-50` 的注释就是这个事实），owner 一开写隔离就会写不出交付物。
- **D15 沙箱与监管模式保持正交**：不把沙箱挂在 `ceo_frontdoor_regulatory_mode_enabled` 上。
  两条理由：① 监管模式是**全局**设置、不是会话级（`main/governance/review_policy.py:13`、
  `g3ku/runtime/frontdoor/_ceo_runtime_ops.py:5676-5684`），改成会话级会破坏"档位对所有会话生效、
  前门每跳重读"的既有契约；② 语义正交——监管模式管"要不要人批"，沙箱管"跑在哪"，
  耦合会造成"操作员为了少点审批而关掉监管模式，顺手把隔离也关了"。
  折中只做两件：权限面板里同时呈现两者并提示"建议切到受限档"；`negi doctor` 报"审批开着但档位是
  `unrestricted`"的会话。
  "按需开启"的诉求由 D11/D17 满足（owner 默认 `unrestricted`、面板手动切 `restricted`）。
- **D16 无常驻守护进程**：所有后端都用"每次调用包一层 argv"的方式执行，**不开 TCP 端口、不开 unix socket 服务**。
  文件工具经 jail 内的一次性 helper（`fsop`）执行，以获得内核级约束（而不是只靠路径策略）。

### 第四轮拍板（2026-10-27，安装态驱动的档位模型）

> 用户口径（原话）："项目默认不安装沙箱，此时群聊默认开启只读，其他会话都默认不开启只读(表现为和现在一样)，
> 不安装沙箱时，可切换只读和全权限模式，安装沙箱后变成开启沙箱和关闭沙箱，在沙箱界面变成卸载沙箱和安装沙箱，
> 安装沙箱后可管理文件等操作。"

- **D17 档位＝两档意图 × 安装态**（本轮的核心决定，取代原 `conversation_only`）：
  会话只存一个字段 `Session.metadata["sandbox"]["mode"] ∈ {restricted, unrestricted}`，
  **默认按信任派生**：`third_party_group` / `third_party_dm` → `restricted`；owner → `unrestricted`。
  实际执行面由安装态解析，**不入库**：

  | 安装态 | `restricted` 解析为 | `unrestricted` 解析为 |
  |---|---|---|
  | 未安装 / `partial`（该信任档必需维度不全） | **只读档 `readonly`**（宿主进程内，读根限定） | **宿主全权限 `host_full`** |
  | 已安装且 `ready` | **沙箱档 `jailed`**（jail 内） | **宿主全权限 `host_full`** |

  UI 标签跟着安装态换字（未安装「只读 / 全权限」，已安装「开启沙箱 / 关闭沙箱」），
  **装/卸沙箱不改任何会话元数据、不需要迁移**。原来的 `unavailable` 不再是档位，只是安装态的一个输入；
  **`conversation_only` 档取消**（未安装时群聊落只读档，不是仅对话）。
- **D18 只读档的强制内容（机制级，不是提示词约束）**：
  - **默认拒绝**：不在名单里的工具名一律拒（含 MCP / skill / 资源工具 / 将来新增的任何工具）。
    这条是只读档能当地板用的唯一理由——新工具不会自动获得宿主访问权。
  - 名单（third_party 只读档）：`silent`、`time`、`load_tool_context`、`load_skill_context`、
    `content_open`、`content_search`、`content_describe`、`filesystem_stat`、`view_message_history`、`search_stickers`。
  - **禁写**：`filesystem_write|edit|copy|move|delete|propose_patch` 全拒。
  - **禁进程**：`exec` / `process_kill` 全拒。理由：`exec` 现有的只读形态守卫
    （`g3ku/agent/tools/shell.py:512-555`）是**正则黑名单**，`os.system('rm …')` 这类写法就绕过去了；
    它是防误操作的形态层，不是边界（代码自己也这么写：`:495-503`）。third_party 档不给这个面子。
  - **禁外网**：`web_fetch` / `skill-installer` 全拒（没有 jail 就没有网络命名空间，只能按名字拒）。
  - **读根限定**：所有带路径的工具只能读会话自己的读根（见 P0-7），越界 `sandbox_denied`。
  - **owner 的只读档**放宽两处：允许 `exec` 但强制 `governed` 执行模式（于是形态守卫 + 路径策略 + 工作区监禁都生效，
    `g3ku/agent/tools/shell.py:256-290`），允许 `web_fetch`；写类工具与读根限定照旧。
    owner 本就受信，这一档对他是"防手滑"，不是安全边界。
  - **受限档一律不能派异步任务**（owner 也一样）：`create_async_task` / `task_*` / `send_message` 全拒。
    理由：任务节点跑在 worker 进程里（`main/runtime/node_runner.py:606`），只读意图传不进那条执行面，
    与其做一个"名义只读、节点里全权"的假档，不如直接不给派。
- **D19 口令闸门在两种安装态下一致**：third_party 会话从 `restricted` 切 `unrestricted`
  （未安装＝切"全权限"，已安装＝切"关闭沙箱"）都要**二次输入项目解锁口令** + 审计 + 常驻告警横幅 +
  `sandbox.disabled_by_operator = {at, by}`（不要求填理由）。owner 会话切换不需要口令。
- **D20 群聊人设配置不依赖安装态**：D7 说"群聊人设默认不导入，用户在沙箱页面配置"，但人设注入发生在**宿主前门**
  （`g3ku/runtime/frontdoor/message_builder.py` 的 `adopted_persona_text`），与 jail 无关。
  所以沙箱页面里的**人设配置区块在未安装时也可用**；只有依赖 jail 的区块（文件、包、磁盘、重置卷）才锁在安装态后面。

- **D21 Windows 发行版来源：现有发行版优先，专用 rootfs 回退**（第五轮增补，取代 D12 里
  "必须专用发行版"的隐含前提）。探测扫 `wsl -l -q` 的已安装发行版，**同时具备 `bubblewrap`
  与 `python3`** 即合格（后者跑 `fsop`）；专用发行版 `negi-sandbox` 排最前、命中即停，
  用户自己的发行版次之，最多探 4 个。隔离强度不因此下降：jail 的 mount namespace 里只有
  profile 绑进去的路径，发行版开着 automount 也看不见 `/mnt`；`/init` 与 WSL interop 的
  binfmt 用 `--unshare-pid` + 全新 `/proc` 挡在 jail 外（`wsl_interop` 作为读数留给 doctor，
  不是不合格项）。代价：发行版的版本与包集不可 pin，用户可能随手卸载或升级；专用 rootfs
  仍是"保证 `fsop` 一定在、包集最小"的唯一路子，所以保留为回退项，安装页三选一
  （现有发行版 / 专用 rootfs / 容器运行时）。

### 沙箱档位状态机（P0/P1 的唯一判据来源）

`g3ku/sandbox/policy.py` 必须把下面这张表实现成**单一函数**（入参：信任级 + `mode` + 安装态读数），
前门与节点两条车道都只读它，不各自判：

| 会话信任 | `mode` | 安装态 | 解析档位 | 数据面（exec/fs/content） | 控制面（记忆/任务/cron/配置） | 长期记忆 | 人设 |
|---|---|---|---|---|---|---|---|
| owner（本地/白名单私聊） | `unrestricted`（默认） | 任意 | `host_full` | 宿主（与今天完全一致） | 宿主，按现有 RBAC | 注入 | 注入 |
| owner | `restricted` | 未安装 / `partial` | `readonly` | 宿主进程内，**读根限定**；`exec` 强制 `governed`；**禁写** | 宿主，按现有 RBAC；**任务拒绝** | 注入 | 注入 |
| owner | `restricted` | `ready` | `jailed` | **沙箱**（能力放开） | 宿主，按现有 RBAC；**任务拒绝** | 注入 | 注入 |
| third_party（群聊/未列名私聊） | `restricted`（**群聊默认**） | 未安装 / `partial` | `readonly` | 宿主进程内，**读根限定**；**禁 `exec`、禁写、禁外网** | **全部拒绝**；**任务拒绝** | **不注入** | 默认不注入，沙箱页可配（D20） |
| third_party | `restricted` | `ready` | `jailed` | **沙箱**（能力放开，含 exec/装包/写文件） | **全部拒绝**；**任务拒绝** | **不注入** | 同上 |
| third_party | `unrestricted`（**口令解锁**） | 任意 | `host_full` | **宿主**（仅剩 exec 形态守卫） | **全部拒绝**；**任务拒绝** | **不注入** | 同上 |

规则：

- **`mode` 是意图，档位是解析结果**：安装态变了档位就变，`mode` 不动。安装沙箱 → 所有 `restricted` 会话
  从 `readonly` 升为 `jailed`（能力变强、隔离也变强）；卸载 → 从 `jailed` 降为 `readonly`（能力变弱、隔离变弱但仍在地板以上）。
  两个方向都**不需要写会话元数据**，也**不打断在飞回合**（下一回合边界生效）。
- **`partial` 等同于未安装**（对该信任档而言）：后端探测出 `partial`（third_party 必需维度 `fs_read`/`fs_write`/`network`
  任一项不是 `full`，维度表见 P1-2）→ 该档不给 `jailed`，落 `readonly`，界面标注缺哪几维。
  owner 档可以接受 `partial` 的 jail（例如 Seatbelt 缺 `process`/`resource`），同样标注。
- **`unrestricted` 对 third_party 只能由操作员用项目解锁口令逐会话打开**（D19），写会话元数据
  `sandbox.disabled_by_operator = {at, by}`（**不要求填理由**）+ 审计行 + **会话页常驻告警横幅**；
  `negi doctor` 列出所有这类会话。owner 切换不需要口令。
- **只读档约束的是数据面**（文件 / 进程 / 网络）。控制面对 owner 照旧按 RBAC（所以 owner 的"只读"仍能写长期记忆），
  对 third_party 本来就全拒——这一点要在 UI 文案里写清，避免"只读了怎么还能改记忆"的误解。
- 档位变化**只在回合边界生效**，不打断在飞回合；变化时转录落一条边界通知（路径根变了 / 任务不可用了 / 能力变了）。
- P0 阶段还没有 jail，所以 third_party 一律解析为 `readonly`：**能读自己会话的东西、能聊天、能查贴纸，
  但读不到宿主、写不了、跑不了命令、联不了网、碰不到控制面**。这就是 D3“两段式”的由来：
  P0 给只读地板，P1a 装好沙箱后同一批会话自动升到 jail 里放开数据面。

### 按推荐默认执行（如无异议不再询问）

- **异步任务范围**：**受限档（只读或沙箱）一律拒绝** `create_async_task` / `task_*` / `send_message`（D18）；
  `cron` / `schedule_wake` 对 owner 仍可唤醒本会话，但不得派生任务；third_party 档连 `cron` 也拒（控制面）。
- **沙箱页面管理范围**：安装/卸载、运行状态与逐维度 enforcement 等级、人设配置（**这三项不依赖安装态**，D20）；
  磁盘占用、已装包清单、`/workspace` 与 `/shared` 文件浏览（宿主侧直读卷）、导入/导出、重置卷
  （**这五项只在已安装时可用**，未安装时隐藏或禁用并给一行原因）。
- **中途开启沙箱**：只把该会话已有的入站附件复制进 `/shared/inbox`，不做宿主 workspace 快照；转录里落一条边界通知。
- **中途卸载沙箱**：`jailed` 会话回落 `readonly`，卷保留（可导出/重置）；转录里落一条边界通知
  （"沙箱已卸载，本会话回到只读，此前提到的 `/workspace` 路径不再可达"）。
- **卷保留**：切档位或删除会话时卷默认保留，删除需二次确认并提示先导出。
- **owner 切档**：不需要口令（owner 本就受信）；只有 third_party 会话切 `unrestricted` 需要口令（D19）。

### 信任分级（P0 的核心数据结构）

```
owner             本地 web:/CLI，或 ext: 且 sender ∈ ownerOpenids 的 c2c/guilddm
third_party_dm    ext: c2c/guilddm，sender 未列名
third_party_group ext: qq:group: / qq:guild:（整会话，与发送者无关）
```

规则：
- `effective_trust = min(session_trust, turn_trust)`，**只能降不能升**。
- 内部轮（heartbeat / cron / task terminal / shutdown_resume）**继承会话信任**，不因"不是用户轮"而升级。
- `prompt_batch` 批次里只要有一条第三方消息，整轮按第三方处理。
- 网页接管群会话（`/ws/ceo` 的渠道输入车道）：按 D5 保守处理为 `third_party_group`；
  若要给操作员留全权通道，走"显式 trusted group"配置，不靠回合来源升级。
- 信任判定**不接受桥自报的 `trust=owner`**：服务端按 `chat_type` + sender 白名单派生。

### 工具面拆分（P0/P1 共用）

| 面 | 工具 | 沙箱档执行位置 | third_party `jailed` | third_party `readonly`（未安装默认） | owner `readonly` |
|---|---|---|---|---|---|
| 数据面·进程/写 | `exec`、`filesystem_write`/`edit`/`copy`/`move`/`delete`/`propose_patch`、`process_kill`、依赖安装 | jail 内（**每次调用包一层 argv，无常驻守护进程**） | 放开 | **全拒** | `exec` 强制 `governed` 放行；写类与 `process_kill` **拒** |
| 数据面·读 | `content_open`、`content_search`、`content_describe`、`filesystem_stat`、`view_message_history` | jail 内（走 `fsop`） | 放开（jail 内根） | 放行，**读根限定**（P0-7） | 放行，**读根限定** |
| 数据面·网络 | `web_fetch`、`skill-installer` | 只能经 P3 的 egress 代理 | 拒（`network=none`） | **全拒** | `web_fetch` 放行；`skill-installer` 拒（会写 workspace） |
| 控制面 | `memory_update`、`memory_note`、`create_async_task`、`task_*`、`send_message`、`cron`、`schedule_wake`、`model_config`、治理/资源管理 | 宿主 | **全拒** | **全拒** | `memory_*` 按现有 RBAC 放行；任务/`send_message`/`cron` **拒**（D18） |
| 常驻信号 | `silent`、`time`、`load_tool_context`、`load_skill_context` | 宿主（无副作用） | 放开 | 放开 | 放开 |
| 其他一切 | MCP 工具、skill 提供的工具、资源工具、将来新增的任何工具 | —— | 按沙箱档数据面/控制面归类 | **默认拒绝** | **默认拒绝** |

**最后一行是只读档能当地板的唯一理由**：名单是白名单不是黑名单，新增工具不会自动获得宿主访问权（fail-closed）。
资源工具要进只读档，必须显式登记到 `g3ku/sandbox/policy.py` 的名单里并声明它读什么根。

理由：控制面工具不碰环境，碰的是宿主的库、调度与配置。把它们塞进沙箱要么复制宿主服务、要么开 RPC 回宿主，
反而扩大攻击面（DeepSeek 的 CVE 正是"沙箱能打到自己的控制面"）。**安全边界落在文件/命令/网络三类动作上，这三类全进 jail。**

文件工具为什么也要进 jail：`filesystem_*` / `content_*` 是**进程内 Python I/O**
（`g3ku/agent/tools/filesystem_mutation.py:323-390`、`g3ku/content/tool.py:23`），包 argv 管不到它们。
DSH 把这条列为自己的 gap（in-process tools 另走 fs provider），于是它的文件约束只是策略级。
Negi 要走**严的那条**：文件操作交给 jail 内的一次性 helper（`python -m g3ku.sandbox.fsop <op> <path>`），
读写都由内核强制。代价是每次文件操作多一次进程 spawn（可批处理），P1a 要实测这个开销。

## P0 信任分级 + 记忆门 + 控制面拒绝 + 只读档（不依赖任何沙箱后端，独立可上）

P0 是这次改造里**性价比最高的一段**：不引入任何新运行时，就把"群成员能写全局记忆 / 能调 exec / 能派任务 /
能读宿主文件"四条活路径堵掉，同时群聊还能正常聊天、读自己会话里的附件。

### P0-1 会话来源判定（新模块）

- 新增 `g3ku/runtime/session_trust.py`：
  - `resolve_session_trust(*, session_key, memory_chat_id, sender_id, chat_type) -> TrustLevel`
  - `is_multi_party_chat(chat_id_or_key) -> bool`（认 `qq:group:` / `qq:guild:`；其他桥按 `chat_type` 声明）
  - 判据顺序：显式 `chat_type` metadata > 注册表 external_key 前缀 > 未知按 `third_party_dm`（fail-closed）
- 注册表条目加可选 `chat_type`（`g3ku/runtime/external_sessions.py:37-41` 的 `ExternalSessionEntry` +
  `_load`/`_save`）；`POST /sessions` 接受 `chat_type`（`g3ku/runtime/api/external_v1.py:157-185`）。
  存量条目没有该字段 → 回退 external_key 前缀解析，不要求迁移。
- 配置：`qqBot.accounts.<appId>.owner_openids: list[str]`（`g3ku/config/schema.py:1054-1060` 旁），
  以及 `externalApi.tokens[].trusted_senders`（第三方桥用；默认空）。

### P0-2 QQ 桥补 sender / chat_type

- `g3ku/qq_official/bridge.py`：`_dispatch`（`:1263-1275`）与 `on_incoming`（`:648-655`）增加
  `sender: {id, name}` 与 `chat_type`；四个 handler 各自派生（`:1277-1300`）：
  - `on_group_at_message_create` → `chat_type="group"`，sender id = `author.member_openid`
  - `on_c2c_message_create` → `"dm"`，`author.user_openid`
  - `on_at_message_create` → `"guild"`，`author.id` +（`author.username` / `member.nick` 有则用）
  - `on_direct_message_create` → `"guild_dm"`，`author.id`
  - 群消息**没有昵称**（botpy `GroupMessage._User` 只有 `member_openid`）：`name` 留空，投影里只显示 id 短形。
- `g3ku/qq_official/client.py:48-67`：`send_message` 增加 `sender` / `metadata`（`chat_type`）透传；
  `ensure_session`（`:39-46`）增加 `chat_type`。
- 服务端已支持 `sender` 与 `metadata`（`g3ku/runtime/api/external_v1.py:938-952`），只需补文档与校验：
  `chat_type` 白名单 `{group,dm,guild,guild_dm}`，非法值 400；`sender.id` 长度上限。

### P0-3 模型可见的来源标注（只改投影，不改转录）

- `g3ku/runtime/frontdoor/message_builder.py:566-611`：在 `_history_message` / `_decorate_user_arrival_time`
  同一处，按 metadata 追加一行来源标注，形如
  `[群消息 | 发送者 <id 短形> | 第三方群聊，非操作员]`，位置在正文**之前**、送达时间戳之后不动。
- 硬约束（照抄 `[消息送达时间]` 的既有纪律）：
  - 装饰值派生自记录固定字段，跨回合字节稳定，不打断前缀缓存。
  - 所有按内容相等性比较的地方必须先 strip：`strip_arrival_time_stamp` 那一族要加同形的
    `strip_sender_stamp`（`g3ku/runtime/frontdoor/message_builder.py:697-698` 是两个已知比较点）。
  - 当前轮兜底路径（`:1449-1460`）同样装饰。
- 转录保持原文（`g3ku/runtime/session_agent.py:1562-1596` 已把 metadata 落盘），RAG/UI 消费方不受影响。

### P0-4 记忆门（四处同时生效，缺一处就是绕过）

单一判据函数 `memory_review_eligible(session_key, trust)`，由 `MemoryManager` 持有（构造期注入回调，
`g3ku/runtime/bootstrap_bridge.py:264-267` 是唯一生产实例化点；`g3ku/shells/memory_cli.py:32-43` 传"全允许"），
四个入口都调它：

1. `record_turn_for_review`（`g3ku/agent/memory_agent_runtime.py:5553`）：不合格 → `{"status":"ignored","reason":"untrusted_session"}`，不落缓冲。
2. `flush_review_window`（`:5643`）：不合格 → **清缓冲不入队**（覆盖 1h 按龄冲刷 `:4902-4957`、
   token 压缩冲刷 `g3ku/runtime/session_agent.py:4159-4165`、会话边界 `:5779-5793`）。
3. 队列消费侧：`run_due_batch_once` 处理批次前按 `item.session_key` 过滤（`:5927` 已把 session_key 带进批次），
   不合格批次写终态台账 `discarded`（reason `untrusted_session`），不进模型窗口、不停车。
4. `memory_update` 工具（`g3ku/agent/tools/memory_update.py:63-77`）：执行前查信任，不合格直接返回可读拒绝
   （不进队列）；同时在 P0-5 的工具面里也摘掉它（双保险）。

迁移：启动时对 `memory/review_state.json` 里所有不合格会话跑一次 `_pop_review_session_state`（`:5688-5692`），
并留一行 INFO 计数；`memory/failed.jsonl` 里群来源的停车记录**不自动删**，只在管理页标记来源、
且排除在"成功信号自动重排"之外（`queue.auto_requeue_on_success` 那条车道）。

### P0-5 记忆读侧（D1/D7）

- `g3ku/runtime/frontdoor/message_builder.py:78-96` `_read_memory_snapshot_text`：不合格会话返回 `""`
  （不注入 `## 长期记忆`）；`adopted_memory_snapshot_text`（`:114-119`）随之拿到空串，index 1 位置由人设块顶上。
- 记忆修订增量通知同样要压掉：`_ceo_runtime_ops.py:3814` 的 `memory_notice_delta` 车道对不合格会话直接跳过，
  否则"块是空的但增量还在投"会把记忆从侧门送进群聊上下文。
- `memory_write_hint_block`（`:1201-1218`）随 `memory_update` 从可见集摘除而自动消失。
- 人设（D7 已拍板）：`adopted_persona_text` 在 `third_party_group` 且未配置沙箱人设时返回空串；
  群聊默认不带人设，配置/导入入口在 P1 的沙箱页面（**未安装沙箱时也可配**，D20）。长期记忆仍不注入。
- `memory_note` 读 note：不合格会话拒绝（P0-6 的名单里）。

### P0-6 档位解析 + 控制面拒绝（untrusted 回合）

- **档位解析**（新函数，P0 就要有，P1 只是给它加 `jailed` 分支）：
  `g3ku/sandbox/policy.py::resolve(session_key, trust, mode, install_state) -> Tier`。
  P0 阶段 `install_state` 恒为 `not_installed`（没有后端探测代码，直接常量），于是只可能解析出
  `readonly` 或 `host_full`。`mode` 从 `Session.metadata["sandbox"]["mode"]` 读，缺省按信任派生
  （third_party → `restricted`，owner → `unrestricted`）；**P0 不提供切换 UI**，改元数据即可（P1-5 才做端点与面板）。
- `_approval_request_for_tool_calls`（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:5686-5734`）：
  untrusted 回合时，**名单外的每个调用**都进 `review_items`（不查 governance 是否开启、不查风险度）。
  名单取自「工具面拆分」表的 third_party `readonly` 列（**白名单，未列出的一律拒**）：
  `silent`、`time`、`load_tool_context`、`load_skill_context`、`content_open`、`content_search`、
  `content_describe`、`filesystem_stat`、`view_message_history`、`search_stickers`。
  P1 装好沙箱后，`jailed` 档才把数据面·进程/写工具放回名单（它们此时进 jail，不进宿主）。
  `channel_session` 标记已按会话键判出（`:5729-5730`），于是自动走 `_auto_reject_channel_review`（`:8628-8629`）
  的合成拒绝车道（`:8601-8607`）—— **零新机制，复用既有拒绝形状**。
- 防御纵深：`_frontdoor_executable_tool_names`（`:3459-3496`）与 `_frontdoor_callable_tool_names_for_state`
  （`:4072-4109`）在**常驻内置并进来之后**再减去 untrusted 拒绝集（顺序很关键：先减后并等于没减）。
  同时 `exposure_resolver.resolve_for_actor`（`g3ku/runtime/frontdoor/exposure_resolver.py:33-50`）按同一份名单收窄
  `tool_names`，让尾块 callable 与执行准入同源。
  **注意**：`content_open` / `content_search` / `filesystem_stat` 是资源工具、`exec` / `memory_update` 是常驻内置，
  两条来源都要过同一份名单，否则"从可见集摘掉但仍可调用"就是绕过。
- 拒绝文案（给模型看的英文）要说明三件事：这是第三方群聊、这类动作需要操作员在私聊里发起、不要重试同名工具。
  只读档另给一句能力说明（可读什么、不能写/不能跑命令），否则模型会反复试 `filesystem_write`。
- 优先级：信任策略 **高于** 全局审批档；`all_pass` 不能把它放开（写进 `tool-and-skill-system.md` 的档位契约）。

### P0-7 只读档的读根限定（没有 jail 时唯一的读边界）

只读档让 `content_*` / `filesystem_stat` 留在名单里，就**必须**同时限定它们能读哪。今天它们能读整个宿主：
`content_open` 的描述明写"content ref or **absolute file path**"，`allowed_dir` 只在
`settings.restrict_to_workspace` 为真时才生效，而它默认不生效
（`g3ku/content/tool.py:484`、`g3ku/agent/tools/filesystem_stat.py:249`、
`g3ku/agent/tools/filesystem_mutation.py:1733`）。所以群聊在只读档下可以
`content_open("/…/.g3ku/master.key")` 再把内容发群里 —— **这条必须在 P0 一起堵，否则只读档比 `conversation_only` 更危险。**

好消息是**钩子已经存在且形状统一**：所有带路径的工具都走同一个 `allowed_dir` 检查
（`g3ku/content/navigation.py:1690-1708` 与 `:1710-1723`、`g3ku/agent/tools/filesystem.py:11-22`、
`g3ku/agent/tools/filesystem_mutation.py:84-99`、`g3ku/agent/tools/filesystem_stat.py:235-238`），
都是 `resolved.relative_to(allowed_dir)`，且都在 `.resolve()` **之后**判——symlink 指出去会被拒（这条要写进验收）。

要做三件事：

1. **`allowed_dir` 从单值改成多根清单**，并加一条**每会话**来源：
   `runtime_context["fs_read_roots"]`（只读档）/ `["fs_write_roots"]`（沙箱档用）。
   优先级：`runtime_context` 里的档位根 **高于** `settings.restrict_to_workspace` 推出来的 workspace 根
   （只读档必须比 workspace 更窄，不能反被 workspace 放宽）。
2. **只读档的读根清单**（third_party）：
   - 该会话的入站上传目录（web 上传落盘车道 `g3ku/runtime/api/websocket_ceo.py:370-424` †、
     渠道附件抽取车道 †）；
   - 该会话的 temp 目录；
   - **不含**：宿主 workspace、项目根、`.g3ku`、`memory/`、`sessions/`、其他会话的任何目录、用户主目录。
   owner 只读档另加：宿主 workspace（他要的就是"别写坏我的项目"，不是"别读"）。
3. **artifact ref 的作用域**：`content_open(ref="artifact:…")` 走的是 `main.storage.artifact_store` 的 id 查找，
   **不经过 `allowed_dir`**（`g3ku/content/navigation.py:1628-1666`、`:993`）。只读档必须限制为
   **本会话产生的 / 投递给本会话的** artifact（`view_message_history` 返回的就是这一类）。
   实现落点与"artifact 是否已有会话归属字段"† 要在动手前核实；若没有归属字段，
   **P0 的处置是 third_party 只读档不给 `view_message_history`**（宁可少一个能力，不开一个枚举口子）。

**验收口径**：群聊只读档下 `content_open` / `content_search` / `content_describe` / `filesystem_stat`
对 `.g3ku/master.key`、`config.json`、`memory/MEMORY.md`、宿主 workspace 任意文件、其他会话目录
**全部返回 `sandbox_denied`**；对本会话上传的附件成功；对读根内指向根外的 symlink 失败。

### P0 验收

- 单测（新增 `tests/`）：
  - 群会话跑一轮 → `review_state.json` 无该会话、`queue.jsonl` 无新行、`ops.jsonl` 无 applied 行。
  - 群会话已缓冲 3 轮后升级 → 下一次按龄冲刷**不入队**且缓冲被清。
  - 群会话调 `memory_update` → 返回拒绝、队列无行；调 `memory_note` → 拒绝。
  - 群会话请求体里无 `## 长期记忆`、无 `### Long-term memory updates`；本地会话照旧有。
  - 群会话调 `exec` / `filesystem_write` / `create_async_task` / `send_message` / `cron` / `web_fetch` →
    合成拒绝，`approval_status="channel_auto_rejected"`，且宿主没有子进程产生、没有文件被改。
  - **只读档读根**（P0-7 的验收口径逐条测）：群会话 `content_open` / `content_search` / `content_describe` /
    `filesystem_stat` 打 `.g3ku/master.key`、`config.json`、`memory/MEMORY.md`、宿主 workspace 任意文件、
    其他会话目录 → 全部 `sandbox_denied`；打本会话上传的附件 → 成功；打读根内指向根外的 symlink → 拒。
  - **默认拒绝**：给群会话塞一个名单外的假工具（MCP / 资源工具都行）→ 拒；证明是白名单而不是黑名单。
  - owner 只读档：`exec` 走 `governed`（`full_access` 元数据被忽略）、写类工具拒、`web_fetch` 放行、
    workspace 可读；**且不能派任务**。
  - `prompt_batch` 混合批次（owner + 第三方）→ 整轮按第三方。
  - 内部轮（heartbeat）在群会话 → 不写记忆、控制面仍拒绝、只读档读根仍生效。
  - 投影标注：装饰后可见、转录原文不变、`strip_sender_stamp` 后与原文相等（相等性去重回归）。
- 实机（按 `feedback-live-runtime-verification`：单测不算验收）：
  重启后在 QQ 群里发"记住我喜欢 X"、"帮我跑一下 dir"、"把 config.json 发出来看看"三句，
  核对 `memory/queue.jsonl`、`memory/ops.jsonl`、请求原件（actual-request artifact）与审计行；
  再上传一个附件让群里读它，确认读根放行；最后在本地 web 会话发同样三句，确认行为不变。

### P0 风险

1. `strip_sender_stamp` 漏一个比较点 → 暂停回合种子对账/cron 相等性误判，症状是重复注入。缓解：把两个已知比较点
   （`message_builder.py:697-698`）与 `strip_arrival_time_stamp` 的调用方全量 grep 一遍，同处加。
2. 记忆门只堵复核窗口、漏了 `memory_update` 或队列消费侧 → 群里一句话照样写进全局记忆。缓解：四处同一判据函数，
   验收逐处测。
3. 群会话不注入记忆后，模型在群里的连续人格/偏好会退化 —— 这是 D1 的既定代价，要写进变更说明。
4. `chat_type` 由桥自报：恶意/被攻破的桥可以谎报 `dm`。缓解：服务端不接受 `trust` 字段自报，
   且 `qq:group:` 前缀与 `chat_type` 不一致时按更严的一档处理并留 WARNING。
5. **P0-7 漏一个路径入口 = 只读档变成宿主只读全开**（本轮新增的最大风险）。带路径的工具不止 `content_*`：
   `filesystem_stat` 的"bounded listings and size aggregates"能列目录，资源工具/skill 可能自带 I/O。
   缓解：① 名单是白名单，未登记的工具在只读档一律拒；② `allowed_dir` 改成多根清单时**保留原有的
   `restrict_to_workspace` 语义**，不要让新代码路径把 `allowed_dir` 置空；③ 验收必须点名具体敏感文件
   （`master.key` / `config.json` / `MEMORY.md`），不能只测"能读附件"。
6. **artifact ref 绕过读根**：`allowed_dir` 管不到 `artifact:` 查找（P0-7 第 3 条）。
   缓解：没有会话归属字段就不给 `view_message_history`；有了也要断言跨会话 ref 读不到。
7. **只读档比 `conversation_only` 多给了能力**，回归面更大（读根解析、附件落盘位置、artifact 作用域）。
   缓解：这三件事都在 P0 内闭环、逐条有验收；宁可 P0 慢一点，也不要"只读档先发、读根后补"——
   那中间态是**净负收益**（今天群聊读不到宿主文件不是因为策略，是因为没人告诉它路径）。

## P1（= P1a）沙箱执行面 MVP（可插拔后端，不绑死容器）

### P1-1 新模块 `g3ku/sandbox/`

| 文件 | 职责 |
|---|---|
| `policy.py` | 信任级 + `mode` + 安装态 → 档位（`host_full` / `readonly` / `jailed`）；后端选择链；工具面分类表与**只读档白名单**；读根/写根（`fs_read_roots` / `fs_write_roots` / `additional_writable_roots`）解析 |
| `backend_base.py` | 后端契约：`probe()` / `enforcement`（**逐维度**）/ `create` / `run(argv,cwd,env,timeout)` / `fsop(op,…)` / `stop` / `destroy` / `import_files` / `export_files` |
| `profiles.py` | bwrap argv profile 与 macOS SBPL profile 的**唯一**生成处（最小根、隐藏 `$HOME`/`.g3ku`、`--unshare-net/pid/ipc`、seccomp、可写根、拒读子路径） |
| `backend_bwrap.py` | Linux，也是 `wsl_bwrap` 的内核：探测 bwrap 与非特权 userns、profile 装配、denial / runner-failure 两类 stderr 签名 |
| `backend_landlock.py` | Linux 兜底：只有写约束，`network` 维度缺失 → 不服务 third_party |
| `backend_seatbelt.py` | macOS：`sandbox-exec -p <SBPL>`；`deny file-read*` 敏感子路径 + `deny network*` |
| `backend_wsl.py` | Windows：专用发行版生命周期（`wsl --import` / `--terminate` / `--unregister`、关 automount）+ 把每次调用委派给发行版内的 bwrap |
| `backend_container.py` | 可选：Docker/Podman CLI（`run`/`exec`/`stop`/`rm`、cap-drop、no-new-privileges、cgroup 限制） |
| `fsop.py` | jail 内一次性 helper：read/write/edit/list/stat/search；**结构化参数，不拼 shell** |
| `manager.py` | `SandboxManager`：每会话一个 jail、目录布局、lease/引用计数、idle 回收、并发上限、状态读数、崩溃重建 |
| `router.py` | `ToolRouter`：数据面工具 → 后端代理实例；控制面 → 宿主；硬校验兜底 |
| `bridge_files.py` | `/shared/{inbox,outbox}` 导入导出：realpath + 根校验、拒 symlink、大小上限、tar 流、审计 |
| `errors.py` | `SandboxUnavailableError` / `SandboxDeniedError` / `SandboxPartialEnforcementError` |
| ~~`client.py` / `sandboxd`~~ | **不做**（D16）：无常驻守护、无 RPC、无监听端口 |

后端选择链（按平台，第一个 `probe()` 通过且满足该信任档的维度要求者胜出；全不胜出 = 安装态 `not_installed`，
`restricted` 会话落**只读档**，不是"仅对话"）：

- **Linux**：`bwrap` → `container`（若已装）→ `landlock`（只够 owner 档）→ 无后端
- **macOS**：`seatbelt` → `container`（若已装）→ 无后端
- **Windows**：`wsl_bwrap` → `container`（若已装）→ 无后端（**不做原生受限令牌**，D13）

### P1-2 隔离契约（后端无关，逐条可测）

无论哪个后端，下面每条都必须成立。**强制力按维度上报**，不是一个笼统的 `full`/`partial`：

| 维度 | 要求 | third_party 是否必需 |
|---|---|---|
| `fs_read` | 敏感子路径（宿主 workspace、`.g3ku`、`memory/`、`sessions/`、`$HOME`）**拒读**，不是只读 | **必需 full** |
| `fs_write` | 只允许 `/workspace`、`/shared`、`/tmp` + `additional_writable_roots`（D14；third_party 档恒空） | **必需 full** |
| `network` | 默认 `none`；放开只能经 P3 的 egress 代理 | **必需 full** |
| `process` | 私有 PID namespace / 独立进程空间 | 推荐，缺失可放行但界面标注 |
| `resource` | pids / 内存 / CPU / 磁盘配额 | 推荐，缺失可放行但界面标注 |

其他硬约束：

- **可见性**：jail 里只有 `/workspace`、`/shared`、只读系统根（`/usr`,`/bin`,`/lib` 或镜像 rootfs）、`/tmp`。
  宿主 workspace、`.g3ku`、`memory/`、`sessions/`、`skills/`、`tools/`、`externaltools/` 一律不挂不绑。
- **凭据**：不注入任何 `G3KU_*` 密钥、模型 key、master key、QQ AppSecret、`externalApi` token；
  只给 `PATH`/`HOME`/`TMPDIR`/`LANG` 与 `SANDBOX_ID`。照抄 Hermes 的集中式剥离清单思路。
- **控制面不可达**：jail 内不能访问宿主 web/internal API、MCP 端口、容器运行时 socket、ssh-agent。
  这条既靠 `network=none` + 不挂 socket，也靠 D16"不设守护进程"。
- **失败分类**：区分"策略拦截"（denial 签名）与"沙箱自己坏了"（runner-failure 签名 + 退出码），
  抄 DSH 的 `denialSignatures` / `runnerFailureRules` 两分；两者在工具结果里分别是
  `sandbox_denied` 与 `sandbox_unavailable`。
- **fail-closed**：无可用后端 → `SandboxUnavailableError`，绝不静默无隔离透传。

各后端能拿到哪几维（这决定它能服务哪个信任档）：

| 维度 | bwrap（Linux/WSL） | Seatbelt（macOS） | Landlock | 容器（Podman/Docker） | Win 受限令牌 |
|---|---|---|---|---|---|
| `fs_read` | full（最小根，不绑就不存在） | full（`deny file-read*`） | **缺** | full | **缺（读不受限）** |
| `fs_write` | full | full | full | full | partial（硬链接/AppContainer ACL 缺口） |
| `network` | full（`--unshare-net`） | full（`deny network*`） | **缺** | full（`--network none`） | **缺** |
| `process` | full（`--unshare-pid`） | 缺 | 缺 | full | 缺 |
| `resource` | 需 cgroup v2，缺则 partial | 缺 | 缺 | full | 缺 |
| **可服务 third_party** | ✅ | ✅（`process`/`resource` 标注为缺） | ❌ | ✅ | ❌ |

→ 结论：**macOS Seatbelt 可以服务群聊**（fs+network 两维 full），但要在界面标注"无进程/资源隔离"；
**Landlock 与 Windows 受限令牌不能服务群聊**，只够 owner 档。
"不能服务群聊"在第四轮拍板后的含义是：**群聊落只读档**（宿主进程内、读根限定），不是"群聊只能聊天"。

### P1-3 工具路由（两个车道各一个 choke point）

- 前门：`_registered_tools_for_state`（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:3967-3977`）与
  `_frontdoor_execution_bundle`（`:6575-6603`）里，把数据面工具实例换成 `router` 产出的代理实例；
  代理实现同一个 `Tool` 契约（`g3ku/agent/tools/base.py:145`），schema/描述不变 → **`tools[]` 字节不变，不动前缀缓存**。
  真正的执行仍走 `_execute_tool_call_with_raw_result`（`g3ku/runtime/frontdoor/_ceo_support.py:704-780`），
  在那里加一道硬校验：数据面工具若拿到的是宿主实例而 `sandbox_id` 非空 → 直接 `SandboxDeniedError`（防装配路漏改）。
- 节点：`main/runtime/react_loop.py:5172-5270` 的 `_execute_tool_raw` 是唯一 `tool.execute(**kwargs)` 出口，
  同一道硬校验加在这里；`runtime_context` 由 `main/runtime/node_runner.py:3309-3350` 注入 `sandbox_id`。
- `runtime_context` 新增键：`sandbox_id`、`sandbox_policy`、`trust_level`、`sandbox_paths`（锚点映射）、
  **`fs_read_roots` / `fs_write_roots`**（只读档靠这两个键生效，`sandbox_id` 为空时它们就是唯一的路径边界）。
  前门侧写在 `_build_tool_runtime_context`（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:3362-3434`）。
- **只读档不产生代理实例**：工具仍是宿主实例，边界来自 ① P0-6 的名单（谁根本不能调）
  ② `fs_read_roots`（能调的读哪些根）。P1-3 的硬校验因此要写成两条：
  数据面工具拿到宿主实例而 `sandbox_id` 非空 → `SandboxDeniedError`；
  带路径的工具在任何档位下都必须消费 `fs_read_roots`/`fs_write_roots`，缺省**不放宽**（fail-closed）。
- 文件工具走 jail 内的一次性 helper（D16）：`filesystem_*` / `content_*` 的代理实例把操作翻成
  `python -m g3ku.sandbox.fsop <op>` 的**结构化参数**，由后端包一层 profile 执行；不拼 shell、不透传裸字符串路径。
  每次文件操作多一次 spawn，P1a 要实测单跳开销，并留"同一跳内多个文件操作合并成一次 helper 调用"的批处理出口。

### P1-4 路径锚点

- `expand_path_anchor_tokens` 增加沙箱档：`{workspace}` → `/workspace`、`{temp}` → `/workspace/tmp`、
  `{data_root}` → 不提供（沙箱内没有数据根概念）。
- **只读档的锚点**：third_party 只读档下 `{workspace}` / `{data_root}` **一律不展开**（展开了也是
  `sandbox_denied`，只会教模型去撞墙），`{temp}` → 该会话 temp 目录；owner 只读档保留 `{workspace}`
  （它的读根含 workspace）。锚点表因此是**档位相关**的，不能只按"是否沙箱"二分。
- `task_temp_dir` / `session_temp_dir` 在沙箱档下返回沙箱内路径；宿主侧的真实卷路径只出现在管理面与审计里，
  **绝不进模型可见文本**（工具结果、错误文案、运行时契约尾块都要过一道 host-path 抹除）。
- 头部钉住块加一段稳定的**档位环境声明**：沙箱档是 `## Sandbox Environment`（根是 `/workspace`、交换区是 `/shared`、
  没有宿主文件系统、联网状态）；只读档是 `## Read-only Session`（可读哪些根、不能写文件、不能执行命令、
  需要产出请让操作员安装沙箱）。两者都进稳定前缀，只在重印边界变，不逐轮重铺；
  **切档（含装/卸沙箱）会改这一段**，所以那一次前缀 miss 是预期内的（见「副作用」第 8 条）。

### P1-5 会话级开关与 UI

- `Session.metadata["sandbox"] = {mode, backend, profile_digest, network, additional_writable_roots, created_at, disabled_by_operator?}`；
  **`mode` 只有 `restricted` / `unrestricted` 两个值**（D17），安装态**不入会话元数据**（它是全局读数）。
  归一化挂在 `normalize_ceo_metadata`（`g3ku/runtime/web_ceo_sessions.py:791-835`，未知键本来就会保留）；
  非法 `mode` 值按信任派生的默认处理并留 WARNING。
- 端点（照 `model-selection` 的形状，渠道会话可设 †）：
  - `GET /api/ceo/sessions/{id}/sandbox` — 返回 `{mode, resolved_tier, install_state, backend, enforcement{}, ui_labels{}}`；
    **`ui_labels` 由服务端按安装态给出**（未安装 `{restricted:"只读", unrestricted:"全权限"}`，
    已安装 `{restricted:"开启沙箱", unrestricted:"关闭沙箱"}`），前端不自己判安装态，避免两处口径漂移。
  - `PATCH /api/ceo/sessions/{id}/sandbox` — body `{mode, unlock_password?}`；
    third_party 会话切 `unrestricted` 时 `unlock_password` 必填（= **项目解锁口令**，服务端校验 + 限流 + 审计），
    并落 `sandbox.disabled_by_operator = {at, by}`（不要求填理由）；owner 会话不需要口令。
    **切档只在回合边界生效**，返回体带 `effective_from: "next_turn"`，前端别显示成"已立即生效"。
  - 以下四个**只在 `install_state ∈ {ready, partial}` 时可用**，否则 409 + `sandbox_not_installed`：
    - `GET /api/ceo/sessions/{id}/sandbox/files?area=workspace|inbox|outbox&path=` — 列表
    - `POST /api/ceo/sessions/{id}/sandbox/import`（multipart，复用 `websocket_ceo.py:592-605` 的上传车道 †）
    - `GET /api/ceo/sessions/{id}/sandbox/export?path=`（复用 `FileResponse` 形状，`websocket_ceo.py:770-782`）
    - `POST /api/ceo/sessions/{id}/sandbox/reset` — 销毁卷重建（二次确认）
  - 沙箱页：`GET /api/sandbox/status`（运行时/镜像/**安装态**/enforcement/磁盘占用）、
    `POST /api/sandbox/install`（二次确认后开始下载安装）、`POST /api/sandbox/uninstall`、
    `GET /api/sandbox/packages`（需已安装）、`GET/PUT /api/sandbox/persona`（**不依赖安装态**，D20）、
    `GET/PUT /api/sandbox/settings`（照 `admin_rest.py:1828-1849` † 的 governance-mode 形状）
- 前端（已拍板 + 第四轮细化）：
  - **档位开关放进现有的权限/风险面板**，不是新按钮——"所有会话下方的权限按钮点开都可额外选择"。
    **标签随安装态换字**：未安装「只读 / 全权限」，已安装「开启沙箱 / 关闭沙箱」（文案取 `ui_labels`）。
    未安装时该面板另附一行「沙箱环境未安装 → 去安装」，点击进沙箱页的安装确认流程（第二轮拍板的入口保留）。
  - **与监管模式同面板但正交呈现**（D15）：两条轴各一个开关，不互为条件；监管模式开着而档位是 `unrestricted` 时
    给一行提示"建议切到受限档"，`negi doctor` 也列这类会话。
  - 面板显示**当前解析档位**（只读 / 沙箱 / 宿主全权限）+（已安装时）**后端与逐维度强制力**
    （`fs_read`/`fs_write`/`network`/`process`/`resource`），缺的维度显式标注。
  - third_party 会话处 `unrestricted` 时，**会话页常驻告警横幅**（已拍板；不要求填理由），
    未安装与已安装两种状态下横幅文案不同（"该群可在宿主上执行命令" / "该群已关闭沙箱"）。
  - **新增独立沙箱页面**：主动作按安装态换（未安装＝「安装沙箱」，已安装＝「卸载沙箱」）；
    状态检查、人设配置常驻；**文件/包/磁盘/重置卷只在已安装时可用**，未安装时隐藏或禁用并给一行原因（D20）。
  - 落点参照现有 composer 面板 †（`g3ku/web/frontend/org_graph.html:234-308`、`ceo/model-panel.js:104-190`、
    `org_graph_ceo_governance.js:411`）。
- 渠道会话今天很多编辑口是 `channel_session_readonly`（`g3ku/runtime/api/ceo_sessions.py:926-941`），
  档位开关要像 `model-selection`（`:996-1080`）一样单独放行，并写审计。

### P1-6 附件与出站文件

- 入站：web/QQ 附件在提交回合前由 `SandboxManager` 复制进 `/shared/inbox`（用户显式输入，算手动通道）；
  模型可见文本里给沙箱内路径，不给宿主路径、不给签名 URL（沙箱内不可达）。
- 出站：模型要发文件必须写到 `/shared/outbox`；渠道出站附件抽取（`external_events.py` 那条车道 †）
  在沙箱档下只认 outbox 里的文件，宿主绝对路径一律不提取。
- `ceo_media` 签名 URL 增加沙箱卷解析分支（`g3ku/runtime/api/ceo_media.py:304-325` 的 `verify_original_token` 一侧 †），
  让网页能预览 outbox 文件；token 作用域限定在该会话的沙箱卷。
- **只读档（P0 就要有，不等 P1）**：入站附件仍落在今天的会话上传目录（`websocket_ceo.py:370-424` †），
  该目录**就是读根之一**（P0-7）；没有 `/workspace`、没有 outbox，所以只读档**产不出出站文件**——
  出站附件抽取车道在只读档下没有新来源，行为不变。模型可见文本给的是它自己能读到的那条路径，
  越界路径一律不给（否则模型会反复 `content_open` 一个注定 `sandbox_denied` 的路径）。

### P1-7 按需安装、CLI 与运维（已拍板：沙箱不是必需依赖）

- **安装态**，权限面板与沙箱页面读同一份读数（**全局一份，进程内缓存，不逐会话存**）：
  - `not_installed`：该平台需要的东西没装 → 面板显示「沙箱环境未安装」+ 档位标签为「只读 / 全权限」，
    点击进安装确认。
  - `runtime_missing`：装了一半（WSL2 在但发行版没导入、bwrap 缺、非特权 userns 被禁、cgroup 不可用）→ 给逐项自检读数；
    对档位解析等同 `not_installed`。
  - `ready`：third_party 必需维度全 `full` → `restricted` 解析为 `jailed`。
  - `partial`：可用但缺维度 → **third_party 档不给 `jailed`，落 `readonly`**；owner 档可用并标注缺哪几维。
- **“安装”在每个平台含义不同**（D12/D13 的直接后果）：

  | 平台 | 要装什么 | 体量 |
  |---|---|---|
  | Linux | `bubblewrap` 包（+ 可选 cgroup v2 委托） | 约 100 KB 级 |
  | macOS | 无需安装（Seatbelt 系统自带），只做探测 | 0 |
  | Windows | WSL2 + **一个合格发行版**（现有发行版需有 `bwrap` + `python3`；否则导入专用 rootfs tar，`wsl --import`，关 automount） | rootfs 数百 MB（复用现有发行版时 0） |
  | 可选（任意平台） | Podman / Docker + 沙箱镜像（pin digest） | 镜像数百 MB～GB |

- **安装确认页必须列出**：平台检测结果、要装什么、来源 URL 与校验和、体积、安装落点、预计磁盘占用、
  是否需要提权/重启、卸载方式；容器后端另需显示授权提示（Docker Desktop 商业授权，故默认推荐 Podman）。
  用户二次确认后才开始下载；进度可见、可取消、失败可重试。
- `negi sandbox install|status|list|reset|export|import|logs|uninstall`；`negi doctor` 增加沙箱体检
  （后端可用性、逐维度强制力、rootfs/镜像校验和匹配、目录占用、残留 jail/容器、
  **只读档读根是否被正确限定**，以及 D15 的"审批开着但档位是 `unrestricted`"清单
  与 D19 的"third_party 处 `unrestricted`"清单）。
- 数据根新增 `sandboxes/<sandbox_id>/{workspace,shared/{inbox,outbox}}`
  （`g3ku/deployment/data_root.py:102-115` 的 `data_work_path` 一侧），随会话删除一起回收（默认先导出提示）。
- **未安装时的行为**：`restricted` 会话落**只读档**（宿主进程内、读根限定、禁写禁进程禁外网）；
  `unrestricted` 会话就是今天的行为。**绝不因为"没装沙箱"而给 third_party 宿主全权限**。
- **卸载时**：安装态回到 `not_installed`，所有 `restricted` 会话在下一回合边界从 `jailed` 降为 `readonly`
  （转录落边界通知）；卷默认保留，`negi doctor` 报占用；`unrestricted` 的 third_party 会话不受影响
  （它本来就在宿主上，告警横幅继续挂着）。

### P1 验收

- **逐后端跑同一套断言**（bwrap / seatbelt / wsl_bwrap / container 各一遍）：
  - `exec pwd` → `/workspace`；`ls /` 看不到宿主目录树；`ls ~` 不是宿主 home。
  - **读隔离**：`cat` 宿主 `memory/MEMORY.md`、`.g3ku/master.key`、`config.json` 全部失败
    （注意：不是"读到只读副本"——DSH 的 `read-only` 语义在这里不够，必须真的读不到）。
  - **写隔离**：往 `/workspace` 外写失败；`additional_writable_roots` 里的路径可写（owner 档）。
  - **网络**：`curl` 失败（`network=none`）。
  - **控制面不可达**：jail 内访问宿主 web/internal API、MCP 端口、容器运行时 socket、ssh-agent 全部失败。
  - **凭据**：`env` 里没有任何 `G3KU_*` / 模型 key / master key / QQ AppSecret。
  - **无常驻进程**：jail 内没有 Negi 守护进程；宿主上没有为沙箱监听的端口（`netstat` 断言）。
- **文件工具走 `fsop`**：`filesystem_write` / `content_open` 在 jail 内生效；对 jail 外路径返回 `sandbox_denied`；
  symlink 指向 jail 外时被拒；实测单跳开销与批处理收益，读数写进本节。
- 宿主侧：jail 里创建的文件只出现在 `sandboxes/<id>/workspace`；UI 能列出、能下载；上传后模型能 `content_open` 到。
- 生命周期：idle 回收后再调用能自动重建；宿主重启后目录不变、会话可续。
- 后端缺失：Linux 无 bwrap 且无容器运行时 / Windows 无 WSL2 → 群聊落**只读档**（不是仅对话），
  `exec` / `filesystem_write` 返回 `sandbox_denied`（名单外），且**没有任何宿主子进程产生**；
  `content_open` 读本会话附件仍成功、读宿主文件仍 `sandbox_denied`（P0-7 的断言在无后端时同样成立）。
- 维度缺失：Landlock 兜底（缺 `network`）→ 群聊落只读档、owner 可用并标注；
  Seatbelt（缺 `process`/`resource`）→ 群聊可用但界面标注缺哪两维。
- **安装态切换（第四轮拍板的核心，必须实测）**：
  - 群会话在 `readonly` 下跑一轮 → 安装沙箱 → **不改会话元数据**，下一回合自动变 `jailed`，
    `exec pwd` = `/workspace`，转录里有边界通知；`mode` 字段仍是 `restricted`。
  - 反向：卸载 → 同一会话自动回 `readonly`，`exec` 变 `sandbox_denied`，卷保留，转录有边界通知。
  - `ui_labels` 随安装态换字（只读/全权限 ↔ 开启沙箱/关闭沙箱），前端不做二次判断。
  - 文件管理端点在 `not_installed` 下返回 409 `sandbox_not_installed`；人设端点在 `not_installed` 下**仍可用**（D20）。
  - owner 会话默认 `unrestricted`，安装前后行为**完全一致**（回归断言：装沙箱不改变 owner 的任何工具可用性）。
- 工具结果分类：策略拦截 → `sandbox_denied`；后端崩 → `sandbox_unavailable`；两者文案不同、审计分开。
- 缓存：开关沙箱前后，同一会话的 `tools[]` 字节不变（`actual_tool_schema_hash` 不变）；
  头部沙箱声明只在重印边界变。

### P1 风险

1. **profile 写松就是假安全**：bwrap argv / SBPL profile 是安全关键代码，一条 bind 写错就把宿主放进去了。
   缓解：profile 只在 `profiles.py` 一处生成；每个后端一套 e2e 断言（照 DSH 的 `bwrap.e2e` / `landlock.e2e` /
   `seatbelt.e2e` 做法）；断言必须点名具体目标（"读不到 `master.key`"），不能只测"能跑命令"。
2. **非特权 userns 被禁**：Debian/Ubuntu 可能 `kernel.unprivileged_userns_clone=0` 或 AppArmor 限制 → bwrap 不可用。
   缓解：探测报具体原因与修法；退回容器后端；都不可用则落**只读档**。
3. **`fsop` 每次 spawn 的开销**：文件密集型任务会变慢。缓解：同跳批处理、实测读数；
   必要时给 owner 档一个"进程内 + 路径策略"的降级档，**明确标注为策略级、永不服务 third_party**。
4. **rootfs / 镜像供应链**：Windows 的 WSL rootfs tar 与容器镜像都由我们分发。
   缓解：pin 版本 + sha256、离线导入、不从第三方站拉、`negi doctor` 校验和比对。
5. **路径锚点漂移**：模型上下文里混着旧宿主路径与新沙箱路径 → 反复"文件不存在"。
   缓解：切换档位时在转录里落一条边界通知；头部声明常驻；工具错误文案给出正确根。
6. **现有 skill/tool 假设宿主路径或 Windows 命令** → jail 内批量失效。
   缓解：P1a 先只保证固定内置数据面工具；资源工具逐个过锚点契约，坏的在沙箱档下标 `unavailable_in_sandbox`。
7. **macOS `sandbox-exec` 已被 Apple deprecated**：仍在（Chromium/curl 在用），但按"能用不演进"处理，
   容器后端作为 macOS 的备选路。
8. **Windows 文件桥性能**：`\\wsl$\` 跨 9P 慢。缓解：导入导出走 `wsl.exe` + tar 流，不逐文件复制。
9. **每会话 jail 的并发上限**：群多时 bwrap 进程数与 WSL 内存会顶上来。缓解：idle 回收、`max_concurrent`、
   排队与可读状态；**绝不为省资源让两个会话共用一个 jail**（跨群共享＝跨群泄露）。

## P1b 一键安装（已拍板，独立交付，排在 P1a 之后）

P1a = 上面 P1-1..P1-7（假定后端依赖已在，手动装也能用）；P1b 才是“点一下就装好”。
拆开的原因：P1b 要在**用户机器上做提权安装**，是整个计划里最容易炸的一块，不该和沙箱本体互相阻塞。

### 每个平台装什么（D12/D13 的直接结果）

| 平台 | 默认后端 | 一键安装内容 | 提权 | 可能重启 |
|---|---|---|---|---|
| Linux | `bwrap` | `bubblewrap` 包（发行版包管理器）；可选 cgroup v2 委托 | sudo/pkexec | 否 |
| macOS | `seatbelt` | **无需安装**，只跑探测与自检 | 否 | 否 |
| Windows | `wsl_bwrap` | ① WSL2（`wsl --install --no-distribution`）② 我们的专用发行版 rootfs tar（`wsl --import` + `/etc/wsl.conf` 关 automount）③ 发行版内的 `bubblewrap` | UAC | **可能要** |
| 任意（可选） | `container` | Podman（推荐）或 Docker + 沙箱镜像（pin digest） | 视运行时 | 视运行时 |

**为什么 Windows 不默认装 Podman**：Podman 在 Windows 上本来就是 WSL2 VM
（官方口径 “each Podman machine is backed by a virtualized WSLv2 distribution”），
所以「WSL2 + 我们的发行版」比「WSL2 + podman machine + CoreOS 镜像 + 沙箱镜像」少两层，
而且与 Linux 的 bwrap 共用同一份 profile 实现。已装 Podman/Docker 的机器仍可选 `container` 后端。

**Windows 专用发行版的内容**（我们自己维护，是新增的供应链责任）：最小 rootfs + `bubblewrap` +
Python（跑 `fsop`）+ 常用 CLI（git/curl/tar/压缩工具）+ profile 脚本。以 tar 分发、带 sha256、可离线导入；
版本与宿主 Negi 绑定，升级时**提示重建而不自动重建**（避免动用户数据）。

### 安装流程（状态机）

```
detecting → awaiting_confirm → downloading → installing → (needs_reboot) → verifying → ready | partial | failed
```

- **确认页必须列出**：要装什么、来源 URL 与校验和、体积、安装落点、是否需管理员、是否可能重启、卸载方式；
  容器后端另需授权提示（Docker Desktop 商业授权，故默认推荐 Podman）。用户二次确认后才开始。
- **提权**：Windows UAC / Linux pkexec / macOS 不需要；**绝不静默提权**，提权失败给可读原因。
- **重启**：WSL2 或虚拟化启用可能要求重启；状态落 `needs_reboot` 并持久化安装意图，重启后自动续跑，
  且不与 `g3ku/update_apply.py` 的重启车道打架。
- **校验**：下载物必须校验官方签名 / sha256；不从第三方站拉；校验不过一律拒绝安装。
- **自检**：装完跑一次真实的最小 jail（`network=none`、拒读 `master.key`、只写 `/workspace`），
  按逐维度结果落 `ready` / `partial`；`partial` 时 third_party 档拒绝启用。
- **失败模式要给读数**：BIOS 虚拟化未开、WSL2 缺失、Hyper-V 冲突、非特权 userns 被禁、杀软/组策略拦截、
  磁盘不足、网络不可达。每种都有对应自检行与建议，不许只报“安装失败”。
- **离线/内网**：支持从本地 tar 导入（WSL rootfs / 容器镜像）；下载源可配置镜像站。
- **卸载**：删发行版/镜像/容器/会话目录；**WSL2 与容器运行时默认不卸载**（可能影响用户其他工作），要卸单独问。

### P1b 验收

- 干净 Windows 装机（无 WSL2）→ 一键装完 → 群聊会话自动 `ready`，`exec pwd` = `/workspace`，
  且 jail 内 `ls /mnt/c` 不存在（automount 已关）。
- BIOS 虚拟化关闭 → 明确报“虚拟化未启用”，不是静默失败或无限转圈。
- 安装中途重启 → 重启后续跑，不重复下载、不留半截状态。
- 拒绝 UAC → 回到 `runtime_missing`，界面给指引，项目其他功能不受影响。
- 干净 Linux 装机 → 一键装 `bubblewrap` → 群聊 `ready`；人为禁掉非特权 userns → 报具体原因并退回**只读档**。
- macOS → 无需安装即 `ready`，界面标注缺 `process` / `resource` 两维。
- 卸载 → 回到 `not_installed`，会话目录按提示先导出。

### P1b 风险

1. **提权安装是新的攻击面**：下载源、校验、签名必须钉死；安装日志留证。
2. **我们要维护一个 WSL rootfs**：新增的供应链责任（构建、签名、更新、CVE 跟踪）。
   缓解：最小化包集、构建可复现、发布带 sha256、`negi doctor` 校验；**不要用大而全的桌面发行版**。
3. **企业环境**：组策略/EDR 可能拦 WSL 或包管理器；必须能优雅降级为**只读档**，不能把项目搞挂。
4. **WSL 版本差异**：`wsl --import` 参数、automount 配置、systemd/cgroup 支持在不同 WSL 版本上不一致，
   必须按验收矩阵实测，不能只照最新文档写。
5. **重启与续跑**：跨重启的安装意图要持久化，且不能和 `update_apply.py` 的重启车道打架。
6. **磁盘**：WSL 发行版 vhdx + 每会话目录；确认页给预估占用，`negi doctor` 给实测占用。

## P2 任务运行时进沙箱（本次不做，已拍板：受限档一律不派异步任务）

> 本节保留为未来参考，不在本次交付范围内。当前口径：沙箱档会话里 `create_async_task` / `task_*`
> 一律拒绝（P0-6 的控制面名单已覆盖），所以不存在“任务在沙箱里跑”这条路径。
> 真要支持，先解决下面的 lease/并发/交付物三件事，再单独开计划。

- `main/runtime/node_runner.py:3309-3350` 的 `_runtime_context` 注入 `sandbox_id`；
  `_task_temp_dir`（`:3411-3430`）在沙箱档下返回沙箱内路径，宿主卷路径只留在管理面。
- 任务 lease：节点在跑时 `SandboxManager` 持引用计数，禁止 idle 回收；沙箱崩 → 任务按现有
  可恢复暂停落 `pause_reason`，恢复时用同一卷重启容器（复用既有 pause/resume 与工具重放声明）。
- 交付物默认在 `/workspace/deliverables`，发送前必须进 `/shared/outbox`；验收节点读沙箱内路径。
- 群聊档：`create_async_task` 仍拒绝（D3），所以 P2 主要服务 owner 沙箱与"显式 trusted group"。
- 验收：owner 沙箱里派一个带 `exec` + 文件产出的任务 → 节点工具全在沙箱内执行、交付物可导出、
  暂停/恢复后卷内容连续、宿主 workspace 零改动。
- 风险：任务与前台回合并发写同一卷（需要沙箱内文件锁或按 task 子目录隔离）；
  长任务顶住 idle 回收导致容器常驻（配额与告警）。
- **动手前必须先处理工作树里 `main/runtime/*` 的未提交改动**（见抬头警告）。

## P3 egress 代理 + 配额 + 硬化

- egress 代理（照 Hermes `iron-proxy` 思路）：jail 只拿 scoped token，真实密钥由代理注入；
  按域名 + 方法 + 路径 allowlist 放行；全量审计；`no_proxy` 只留 loopback。
- 代理是**新的控制面**，必须窄：只做 HTTP(S) 转发，**无管理 API、无策略修改口**；
  只在 jail 自己的网络命名空间里可达（bwrap 用 `--unshare-net` + 专用 netns 里的代理地址，容器用专用 bridge），
  宿主端口一律不暴露 —— 否则重演 DeepSeek 的 CVE。这条与 D16"无常驻守护进程"不冲突：
  代理是数据通道，不是能被 jail 调用来改策略的控制通道。
- 配额：CPU/内存/pids/磁盘/网络字节/工具调用数；超限的处置分档（限流 → 拒绝 → 停 jail）。
  bwrap 后端靠 cgroup v2（拿不到则 `resource` 维度记 `partial`），容器后端靠现成 flag。
- 审计事件：`sandbox_created/started/stopped/destroyed/reset/imported/exported/denied/unavailable/policy_changed`。
- `negi doctor` 与网页状态页给出逐维度强制力、目录占用、残留 jail/容器、rootfs/镜像校验和漂移，
  以及 D15 的"审批开着但档位是 `unrestricted`"清单。

## P4 更重的后端（不在本次范围，只留接口）

> OS 原生档（bwrap / Seatbelt / Landlock 兜底）已按第三轮拍板**提到 P1a**；
> Windows 原生受限令牌档**明确不做**（D13）。这里保留的是"比 P1a 更重"的几条路。

### 调研依据：DSH 的 OS 原生档实测口径（`packages/sandbox/sandbox-local`，2026-10-26）

| 平台 | 原语 | DSH 自评 | 依赖 | Negi 采纳 |
|---|---|---|---|---|
| Linux | bwrap（只读宿主根 + 新 `/dev` + 私有 PID ns 的 `/proc` + 可写 bind + 临时 `/tmp`），失败退 Landlock | `full` / `full` | bwrap 包 + 非特权 userns；Landlock 需内核 ≥5.13 与随包 native launcher | ✅ P1a（profile 写严：最小根、拒读敏感路径、`--unshare-net`） |
| macOS | Seatbelt（`sandbox-exec -p <SBPL>`） | `full` | 系统自带 | ✅ P1a（加 `deny file-read*` / `deny network*`；注意已被 Apple deprecated） |
| Windows | ACL 受限令牌（`dsh-sandbox-windows-acl`：token/acl/grant/path-boundary/spawn） | **`partial`**：硬链接、**读不受限**、AppContainer ACL 边界 | 系统 API，无需安装 | ❌ 不做（读不隔离，且要一整套 Win32 FFI） |

三条直接抄的做法：无后端即 `SANDBOX_UNAVAILABLE` fail-closed；逐次上报强制力；区分
`denialSignatures`（策略拦截）与 `runnerFailureRules`（沙箱自己坏了）；功能性探测（真跑一次 `... -- true`）。
一条明确**不抄**的语义：DSH 的 `read-only` 只保证"不写"，宿主根仍可读 —— Negi 的 `fs_read` 维度必须真的拒读。

### P4 保留的几条路

- **microVM**（Kata / Firecracker）：要防容器逃逸 / 内核 0-day 时才上；Linux 专属，重。
- **Windows VM 后端**（Hyper-V）：要在沙箱里跑 Windows GUI / Office / COM 时才上（D8 的出路）。
- **远端沙箱服务**（Modal / Daytona / Vercel 类）：隔离由对端提供、本机免安装，但数据出机，默认不做。
- **WSL Containers（`wslc`）**：Hermes 社区插件走的路。实测缺口写得很清楚——`wslc` 拒绝
  `--cap-drop`、`--security-opt`、`--pids-limit`、`--init`、`--storage-opt`，磁盘配额报而不生效，
  所以该插件刻意保留危险命令审批。对 Negi：可作 `container` 后端的一个变体，`resource` 维度判 `partial`；
  由于 third_party 的必需维度只有 `fs_read`/`fs_write`/`network`，它**仍可服务群聊**，界面标注缺资源隔离即可
  —— 这正是"逐维度强制力"比一个笼统 `partial` 好用的地方。

## 性能预算与副作用

### 常开成本（P0，装不装沙箱都在）

| 项 | 量级 | 说明 |
|---|---|---|
| 信任判定 | µs/回合 | 注册表在内存里，只做前缀解析 |
| 档位解析 | µs/回合 | P0 阶段安装态是常量（没有后端探测代码），只有一次信任级 + `mode` 查表 |
| 后端探测（P1 才有） | **一次/进程** | 照 DSH：探测结论缓存到 provider 生命周期，不得每跳重探 |
| 记忆门 | **负成本** | 群聊不入队 → 省掉记忆 agent 的模型调用与批次字节 |
| 记忆不注入 | **负成本** | 群聊请求体少一整块 `## 长期记忆`（活库常驻 13–16 KB 量级）；index 1 不再随记忆修订抖动 → 缓存更稳 |
| 来源标注 | +20~40 字符/条用户消息 | 进可复用前缀，稳态近乎免费；**上线后每个存量 ext 会话第一次请求 miss 一次前缀缓存**（历史字节变了），一次性 |
| 控制面拒绝 | µs/回合 | 只在 untrusted 回合多一次集合差 |
| **只读档读根限定** | µs/次路径解析 | 就是既有的 `relative_to(allowed_dir)` 检查（`g3ku/content/navigation.py:1701-1707`），从单值改多根清单后多几次 `relative_to`；**没有新进程、没有新 I/O** |
| **只读档可见工具变少** | **负成本** | 群聊 `tools[]` 与契约尾块都变短（写类/exec/任务/治理工具全不进候选池） |

**硬要求**：`policy.resolve()` 与后端 `probe()` 必须是进程内缓存的纯内存判定。任何"每回合读注册表文件"
或"每跳跑一次 `bwrap … -- true`"的实现都会把 µs 级变成几十毫秒级 —— 这类实现直接否决，不是优化项。

### 沙箱档成本（只有解析到 `jailed` 时才付；只读档与宿主全权限档都不付）

| 项 | 今天 | 沙箱档（估算，**必须实测**） | 缓解 |
|---|---|---|---|
| `exec` 每次调用 | 1 次 subprocess | bwrap：+namespace/mount 建立（数十 ms）；`wsl_bwrap`：+`wsl.exe` 跨发行版一跳（约 0.1–0.3 s）；容器：+`podman exec`（约 0.05–0.2 s） | **jail 内常驻 shell**：一条长活的 wrapped shell 承接多次命令，摊薄 namespace/interop 成本 |
| `filesystem_*` / `content_*` 每次调用 | 进程内 I/O（亚毫秒） | 走 `fsop` helper → 一次 wrapped subprocess（Linux 数十 ms；Windows 一跳 0.1–0.3 s） | **同跳批处理**（一次 helper 做多个 op）+ 常驻 helper；大文件一次流式读完 |
| 冷启动 | 无 | bwrap 无；WSL 发行版闲置后首启数秒；容器 1–3 s | idle 阈值可调；lease 保护在跑任务 |
| 常驻内存 | 无 | WSL2 VM / podman machine 常驻（可用 `.wslconfig` 限）；**bwrap 近乎零** | 只有 Windows/容器档付；Linux bwrap 不付 |
| 磁盘 | 无 | 每会话目录 + WSL vhdx（只增不自动缩）/ 镜像 | 配额、`reset`、doctor 报实测占用 |
| 导入导出 | 无 | 复制成本；`\\wsl$\` 跨 9P 对大量小文件很慢 | tar 流，不逐文件复制 |
| 网络 | 宿主直连 | `none`（失败即返回，无等待）；开 egress 代理则多一跳 | allowlist 越窄越快 |

**实测门槛（P1a 验收必须给读数，不许拍数字）**：

- 单跳 `exec`（命令为 `true`）在四个后端上的 p50/p95；
- 单次 `filesystem_write` + `content_open`：宿主档 vs `fsop` 档的 p50/p95；
- 一个"20 次 exec + 30 次文件操作"的脚本回合：宿主档 vs 各沙箱档的总墙钟；
- 常驻 shell/helper 打开前后的差值。

若 `wsl_bwrap` 的单跳开销让脚本回合慢到不可用（例如 >3× 宿主），**P1a 必须先做常驻 shell 再上线**，
不是先发再优化。

### 副作用（非性能）

1. **受限档会话不能派异步任务**（D18）—— 只读档与沙箱档都一样，**也包括 owner 本地会话**。要任务就得切 `unrestricted`。
2. **路径语义变了**：沙箱档模型看到 `/workspace`；只读档模型看到的仍是宿主路径，但**只有读根内的能读**，
   越界返回 `sandbox_denied`。后者比"文件不存在"更容易让模型反复重试，错误文案必须说清
   "这个路径不在本会话可读范围内，需要文件请让操作员上传"。切换档位时落一条边界通知。
3. **依赖宿主路径或 Windows 命令的 skill 在受限档失效**：逐个过锚点契约，坏的按档标
   `unavailable_in_sandbox` / `unavailable_in_readonly`。
4. **只读档产不出文件**：群聊在未安装沙箱时不能写任何交付物（沙箱档则必须走 `/shared/outbox`）。
   这条要写进 UI 文案与安装确认页，否则操作员会以为"群聊坏了"——它其实是在告诉你该装沙箱了。
5. **群聊不记忆、默认无人设**：语气与连续性会变；人设要在沙箱页手动导入（**未安装也能配**，D20）。
6. **后台进程受 idle 回收影响**（沙箱档）：jail 内 `background: true` 起的进程在回收时消失；lease 只保护任务，
   不保护"用户随手起的后台服务"。这条要写进文档与工具回执。
7. **升级耦合**：rootfs/镜像版本与宿主 Negi 绑定；升级后旧 jail 仍是旧版本，直到 `reset`（要有版本漂移读数）。
8. **缓存**：切换档位会改头部声明与 `tools[]` → 该会话一次前缀 miss。**装/卸沙箱是一次全量事件**：
   所有 `restricted` 会话的名单都变（只读档名单 ≠ 沙箱档名单），于是一次性全部 miss 一遍。
   这是安装的隐性成本，要写进安装确认页；稳态下 `tools[]` 字节不变（已列入验收）。
9. **并发上限**：群多时 jail 数顶到 `max_concurrent` → 排队；刻意的，不为省资源共用 jail。
10. **新增供应链责任**：我们分发的 WSL rootfs 要构建、签名、跟 CVE。
11. **操作员摩擦**：导入导出、"文件去哪了"、权限面板与沙箱页两处入口。
12. **白名单维护成本**（只读档的直接后果）：新增工具默认在受限档不可用。这是刻意的 fail-closed，
    但"我加了个工具，群聊里怎么没有"会成为常见困惑。缓解：资源工具在 `resource.yaml` 里加一个显式声明位
    （例如 `exposure.readonly_tier: true|false`），`negi doctor` 列出所有**未声明档位归属**的工具；
    未声明一律按 `false` 处理。

### 各档位下，哪些代价消失

| 代价 | 未安装 · 只读档（群聊默认） | 未安装 · 全权限（owner 默认） | 已安装 · 沙箱档 | 已安装 · third_party 口令关沙箱 |
|---|---|---|---|---|
| jail spawn / `fsop` 开销 | 消失 | 消失 | **付** | 消失 |
| WSL VM / 容器常驻内存 | 消失 | 消失 | **付** | 消失 |
| 磁盘（会话目录 / vhdx / 镜像） | 消失 | 消失 | **付**（可 `reset`） | 卷保留 |
| 冷启动 / idle 回收 | 消失 | 消失 | **付** | 消失 |
| 导入导出摩擦 | 消失（也没有文件可导） | 消失 | **付** | 消失 |
| 任务不可用 | **禁**（D18） | 可用（与今天一致） | **禁** | **禁**（控制面拒绝与档位无关） |
| P0 常开成本 | **不消失**（µs 级；群聊侧净省） | 不消失 | 不消失 | 不消失 |
| 群聊工具能力 | 读类工具 + 聊天（读根限定） | —— | **最全**（jail 内放开） | 回到宿主（风险登记第 9 条） |
| 群聊记忆 / 人设 | 仍禁写、仍不注入 | —— | 仍禁写、仍不注入 | 仍禁写、仍不注入 |
| 宿主文件泄露风险 | 读根限定挡住（P0-7） | owner 自负 | jail 挡住 | **只剩 exec 形态守卫** |

一句话：**装不装沙箱只影响 P1 的成本与群聊的能力上限；P0 的信任分级、记忆门、只读档读根限定是常开的，
而且对群聊是净省 token。** "不安装"对群聊的代价不是性能也不是"只能聊天"，是**产不出文件、跑不了命令**。

## 迁移与兼容

- 存量群会话：升级后首次入站即标 `third_party_group`，`mode` 缺省派生为 `restricted`；
  未安装沙箱 → 落**只读档**，转录里落一条边界通知（"本会话已切到只读档：可以读你上传的附件，
  不能写文件、不能执行命令，宿主目录不可读；需要产出文件请让操作员安装沙箱"）。
  之后操作员装了沙箱 → 同一批会话自动升 `jailed`，再落一条边界通知（根变成 `/workspace`）。
- 存量 owner 会话：`mode` 缺省 `unrestricted` → **行为与升级前完全一致**，不落边界通知、不改任何元数据。
  升级不写会话文件（避免一次性触碰全库），`mode` 只在操作员第一次切档时才落盘。
- 存量记忆：已经写进全局记忆的群来源内容**无法按来源识别**（`source` 只有 `user`/`self`）。
  处置：提供 `negi memory audit --since <date>` 对照 `ops.jsonl` / queue artifact 的 `session_key` 出候选清单，
  人工裁决；不做自动删除。
- 存量任务：不迁移；群聊档从此不能派新任务，在跑的旧任务照旧在宿主跑完（审计里标出来）。
- Negi 自己跑在容器里（`compose.yaml`）：容器内通常拿不到非特权 userns → bwrap 不可用；
  不挂容器运行时 socket → `container` 后端也不可用；Windows 的 `wsl_bwrap` 不适用。
  结论：**容器化部署时 third_party 档落只读档**（宿主＝容器内，读根限定照样生效），
  除非实测确认该环境能跑 bwrap（`--privileged` 或显式开 userns 才行，而那会削弱外层隔离，默认不做）。
  这一条在第四轮拍板后**明显变好**：原来的口径是容器化部署下群聊只能聊天。
- 无 WSL2 的 Windows 装机 / 无 bwrap 且无容器运行时的 Linux 装机：同上，fail-closed 到只读档；
  安装器与 `negi doctor` 明确报"群聊处只读档，安装沙箱后可放开数据面"。

## 文档更新（按 AGENTS.md，必须做）

- 新增 `docs/architecture/sandbox-execution-plane.md`：**档位模型（两档意图 × 安装态 → `readonly`/`jailed`/`host_full`）**、
  边界定义、后端矩阵与选择链、**逐维度强制力**（`fs_read`/`fs_write`/`network`/`process`/`resource`）、
  **只读档的白名单与读根清单**（含"未登记工具默认拒绝"）、路径锚点沙箱档、`/shared` 桥、
  生命周期与 lease、fail-closed 规则、`sandbox_denied` vs `sandbox_unavailable` 错误分类、
  安装/卸载对既有会话的影响、运维入口。
- `docs/architecture/README.md`：Reading Order / Topic Guide / Topic Ownership / Debugging Entry Points 各加一行。
- `runtime-overview.md`：记忆复核资格（信任分级）、记忆注入的会话档、runtime_context 新键
  （`trust_level` / `sandbox_policy` / `fs_read_roots` / `fs_write_roots`）、**档位只在回合边界生效**这条时序契约。
- `external-agent-api.md`：`sender` / `chat_type` 契约、群聊默认档、附件进出沙箱的形状。
- `tool-and-skill-system.md`：数据面/控制面拆分、信任策略与审批档**正交**（D15）、常驻内置在 untrusted 档的摘除、
  **只读档白名单与"未登记工具默认拒绝"契约**、资源工具新增的 `exposure.readonly_tier` 声明位。
- `config-and-models.md`：`sandbox` 配置段（后端选择链、`additional_writable_roots`、配额）、
  `qqBot.accounts.<appId>.owner_openids`。
- `web-and-admin.md`：权限面板里的**档位开关（标签随安装态换字，`ui_labels` 由服务端给）**与逐维度读数、
  沙箱页面（安装 ↔ 卸载、哪些区块锁在安装态后面）、`GET/PATCH /api/ceo/sessions/{id}/sandbox` 契约、
  文件导入导出契约与 `sandbox_not_installed` 的 409、渠道会话可设项的变化、常驻告警横幅。
- `operations-and-maintenance.md`：各平台安装内容（bubblewrap / WSL rootfs / 镜像）、**卸载语义与卷保留**、
  目录布局与配额、残留 jail 清理、排障入口（含"非特权 userns 被禁"这类环境故障，
  以及"群聊为什么读不到某个文件"这类只读档读根排障）。
- 收尾跑 `python scripts/check_architecture_docs.py`（体例、指针、25KB 叶节、时态）。

## 不做

- **不用路径限制冒充"沙箱"**：`restrict_to_workspace` 那类策略是**进程内策略级**保证，强度低于内核边界，
  界面与文档都不能把它叫沙箱。第四轮拍板后它确实承担了一个明确职责——只读档的读根限定（P0-7）
  是未安装时群聊的读边界——但因此有三条硬约束：① 这一档只配"只读"这个名字（不给写、不给 `exec`、不给网络，
  因为没有 jail 就没有第二道机制兜住它们）；② 一旦有 jail 就被 jail 取代，`mode` 不变、档位自动升级；
  ③ 文档必须写明它是策略级，不许表述成"隔离"。
- **不挂宿主 workspace**：包括"只读挂一下项目目录"这种折中；要文件就走导入。
  （只读档的读根是**会话自己的目录**，不是宿主 workspace；owner 只读档才加 workspace。）
- **不做自动双向同步**（Hermes 的 sync/sync-back）：与"除非手动操作不同步"直接冲突。
- **不把密钥放进沙箱**：包括"临时给一次 token"；联网一律经持钥代理。
- **不在沙箱里放控制面**：策略、审批、审计、记忆、模型配置全在宿主。
- **不做每工具/每任务一个 jail**：成本高、文件不连续；粒度是会话。
- **不做常驻守护进程 / RPC 服务**（D16）：没有 `sandboxd`、没有监听端口、没有 unix socket 服务；
  每次调用包一层 argv。少一个能被 jail 反打的控制面。
- **不做 Windows 原生受限令牌后端**（D13）：DSH 自认 `partial` 且读不受限，Win32 FFI 成本最高、收益最低。
- **不把沙箱挂在监管模式上**（D15）：两条轴正交，只做同面板呈现 + 提示 + doctor 告警。
- **不抄 DSH 的 `read-only` 语义**：它的 read-only 只保证不写、宿主根仍可读；Negi 的只读档**既禁写也限定读根**，
  沙箱档的 `fs_read` 维度必须真的拒读。
- **不做 `conversation_only` 档**（第四轮取消）：未安装时群聊落只读档，不是只能聊天。
- **受限档不做异步任务**（D18）：`create_async_task` / `task_*` / `send_message` 在只读档与沙箱档一律拒绝；
  P2 移出本次范围。
- **不把沙箱做成必需依赖**（已拍板）：未安装即只读档，不给 third_party 宿主全权限，也不阻塞项目启动。
- **不把安装态写进会话元数据**（D17）：`mode` 是意图、安装态是全局读数，档位每回合解析；
  装/卸沙箱不触碰任何会话文件。
- **不做按群独立记忆作用域**：那是记忆库加 scope 维度的大改造，本次只做"群聊不碰全局记忆"。
- **v1 不做 microVM / Windows VM / 远端沙箱**：留后端接口。

## 风险登记（跨阶段）

1. **jail 逃逸 / 内核 0-day**：bwrap 与容器都共享宿主内核（Windows 上隔着 WSL2 的 VM，稍好）。
   沙箱不是绝对边界；要"绝对"只能 microVM + 人审，功能进一步受限。
2. **提示注入的文本层**：沙箱管动作不管回复；群聊回复仍然公开，上下文里已有的数据仍可被复述。
   缓解靠 D1/D7（不注入记忆）+ 内容策略，不靠沙箱。
3. **egress allowlist 成为外传通道**：允许的域名照样能带数据出去。缓解：方法/路径级 allowlist + 审计 + 默认 none。
4. **操作员配置错误**：关沙箱、挂宿主卷、放宽 egress 都能一键破防。缓解：D5 闸门、危险操作二次确认、审计、
   `negi doctor` 报"当前档位与推荐档位的差异"。
5. **供应链**：容器镜像、**我们自己分发的 WSL rootfs tar**、以及 jail 内装的包。
   缓解：pin 版本 + sha256、离线导入、非 root、不在 jail 里给 sudo、`negi doctor` 校验和比对。
6. **功能回归面大**：路径锚点、附件、媒体签名 URL、skill 兼容、temp dir 全都要过一遍。
   缓解：P0 先上（零后端依赖、收益最大），P1a 只做数据面 + 前台回合，任务已被拍板移出范围。
7. **磁盘与成本**：每会话一个目录 + 可能的镜像/发行版。缓解：配额、idle 回收、`reset`、占用读数进管理面。
8. **与在飞改动的冲突**：工作树已有 `main/runtime/*` 未提交改动；P2 前必须先对齐归属（抬头警告）。
9. **群聊切 `unrestricted` = 宿主执行（已拍板，本计划残留风险最高的一项）**：
   未安装时这一档在 UI 上叫「全权限」，已安装时叫「关闭沙箱」，**风险完全一样**（D19 的口令闸门因此对两态一致）。
   口令解锁后该群的 `exec` / `filesystem_*` / `content_*` 直接跑在宿主上，只剩 exec 自身的形态守卫
   （deny 黑名单、路径策略、工作区监禁，且 `restrict_to_workspace` 默认 False，
   `g3ku/agent/tools/shell.py:134`、`:649-669`）。提示注入可以驱动它读 workspace / `.g3ku` / `memory/`，
   再把内容写进群聊回复——记忆门挡得住"写进长期记忆"，挡不住"当场读出来发群里"。
   **注意：这一档刻意不套用只读档的读根限定**（P0-7 只服务 `restricted`），因为"全权限"这个标签
   必须名副其实，否则操作员以为自己给了读权限、实际给了半个——那比明说更危险。
   **已拍板的缓解：只做 a** —— 会话页常驻告警横幅 + 审计行 + `negi doctor` 列出所有这类会话；
   **切换时不要求填理由**。
   未采纳（记录在案，日后若出事故可回头加）：
   - b. `disabled_by_operator` N 天后自动回到 `restricted`；
   - c. 该档强制会话级 `restrict_to_workspace=true` + `content_*` 只读沙箱卷与 `/shared`。
   → 净结果：口令解锁后，该群对宿主文件与命令的访问**只剩 exec 自身的形态守卫**，没有第二道机制。
   这条必须写进变更说明与架构文档，不能只留在计划里。
10. **一键安装**：提权、重启、虚拟化未开、userns 被禁、杀软/组策略拦截、Docker Desktop 商业授权、磁盘占用。
    详见 P1b 风险；这也是为什么 P1b 排在 P1a 之后、且失败必须优雅降级为**只读档**。
11. **后端矩阵的测试成本**：4 个后端 × 2 条车道（前门/节点）× 5 个强制力维度。
    缓解：断言写成**后端无关的一套**（P1 验收那份清单），每个后端只跑同一套；
    profile 生成收在 `profiles.py` 一处；CI 里至少 bwrap 与 container 两个后端跑真 jail（macOS/Windows 靠手动矩阵）。
12. **`fsop` 的进程内 → 子进程改写**：`filesystem_*` / `content_*` 的语义（行号、编辑校验、外置信封、
    二进制探测）要在 helper 里等价重现，否则沙箱档与宿主档行为分叉。
    缓解：helper 复用同一批纯函数，只把 I/O 层换掉；两档跑同一套工具契约测试。
13. **只读档的读根限定是"未安装时的唯一读边界"（第四轮新增，风险等级仅次于第 9 条）**：
    没有 jail，所以群聊读不到宿主文件这件事**完全靠 P0-7 的进程内检查**。三个已知缺口：
    ① 带路径的工具不止 `content_*` / `filesystem_stat`，资源工具与 skill 可能自带 I/O；
    ② `artifact:` ref 走 store 的 id 查找，绕过 `allowed_dir`；
    ③ 将来若加"压缩包解压"这类落盘能力，zip 里的 symlink 会把读根指出去。
    缓解：白名单默认拒绝（未登记工具在受限档一律不可用）、artifact 无会话归属就不给 `view_message_history`、
    验收点名具体敏感文件、任何新增落盘能力都必须过一遍读根断言。
    **诚实口径**：只读档是**策略级**保证，不是内核级；它比今天的"群聊能读整个宿主"强得多，
    但比 jail 弱，所以界面与文档都不能把它叫沙箱。

## 交接

### 进度（截至 2026-10-27）

- **P0 全部落地**，分支 `feat/sandbox-trust`，commit `f23cba39`：
  - 新增 `g3ku/runtime/session_trust.py`、`g3ku/runtime/sender_stamp.py`、
    `g3ku/sandbox/{__init__,policy}.py`、`g3ku/utils/path_roots.py`；
  - 改动 `external_sessions` / `external_v1` / `external_auth` / `qq_official/{bridge,client}` /
    `config/{schema,loader}` / `message_builder` / `_ceo_runtime_ops` / `_ceo_support` /
    `exposure_resolver` / `bootstrap_bridge` / `memory_agent_runtime` / `memory_update` /
    `shell` / `content/{navigation,tool}` / `filesystem*` / `react_loop`；
  - 测试 `tests/test_sandbox_trust_p0.py`（29 条）；两条既有 approval 用例按新载荷形状更新。
- **实机验证（隔离 worktree，端口 18791，独立 data root，未触碰真实项目）**：
  启动无回归；`chat_type` 非法值 400；群会话 + 白名单发送者仍判 `third_party_group`；
  owner 私聊判 `owner`；同一私聊见过陌生人后降为 `third_party_dm` 且后续 owner 消息不回升；
  群会话谎报 `dm` 时 `chat_type` 保持 `group`；`_memory_review_eligibility` 对三种第三方会话
  返回 False、对 `web:` 返回 True，档位分别为 `readonly` / `host_full`，`exec` 只在
  `host_full` 可用。
- **未做**：真实模型回合下的工具拒绝与记忆门实测（需要项目解锁口令与可用的模型 key，
  本轮环境无法解密 secret overlay）；P1a/P1b/P3。

- **P1 的第一块已落地**（commit `c999dc36`）：档位意图存储（`<数据根>/sandbox/session-modes.json`
  + 进程内缓存）、`probe.py` 按平台探测与逐维度强制力、`JAIL_BACKENDS_AVAILABLE` fail-closed 闸门、
  `GET/PATCH /api/ceo/sessions/{id}/sandbox` 与安装态端点、第三方切档的口令闸门（只读校验）。
  **实机验证（隔离 worktree / 18791）**：`/api/sandbox/status` 如实报 `runtime_missing`
  （`wsl_distro_missing`，本机 WSL2 无发行版）与 `ui_labels`；owner PATCH 到 `restricted` 得
  `readonly`；第三方 PATCH 到 `unrestricted` 无口令 403、错口令 403、对口令 200 并落
  `operator_unlocks`；`session-modes.json` 内容与预期一致。
- **P1a 剩余（jail 本体）**：`backend_base` / `profiles` / `backend_bwrap` / `backend_seatbelt` /
  `backend_wsl` / `backend_container` / `fsop` / `manager` / `router` / `bridge_files`，
  以及把数据面工具换成代理实例的装配改动；落地后把 `policy.JAIL_BACKENDS_AVAILABLE` 置真。
  **本机无法验收**（无 WSL 发行版、无 Docker/Podman），必须在一台装好后端的机器上跑
  P1 验收那一套断言，再打开闸门。
- **P1b**：安装器（`POST /api/sandbox/install` 当前恒为 `install_available: false`）。
- **前端**：权限面板里的档位开关与沙箱页面尚未接这批端点（`ui_labels` 已由服务端给出）。

- **已逐行复核**（本计划引用的行号都亲自读过）：
  `g3ku/runtime/session_agent.py:1099-1116,1562-1596,4070-4179,4470-4529`、
  `g3ku/agent/memory_agent_runtime.py:1974-2095,4836-4900,5359-5400,5530-5800,5890-5960,6400-6470`、
  `g3ku/runtime/frontdoor/message_builder.py:50-130,566-646,1180-1270,1435-1545`、
  `g3ku/runtime/frontdoor/_ceo_runtime_ops.py:3362-3500,3955-4130,5535-5800,6575-6660,8580-8700`、
  `g3ku/runtime/frontdoor/_ceo_support.py:660-780`、`g3ku/runtime/frontdoor/exposure_resolver.py` 全文、
  `g3ku/runtime/frontdoor/state_models.py:204-280`、`g3ku/runtime/tool_visibility.py:1-80`、
  `g3ku/runtime/api/external_v1.py:97-190,800-980`、`g3ku/runtime/api/external_turns.py:180-290`、
  `g3ku/runtime/api/external_sessions.py`（= `g3ku/runtime/external_sessions.py:1-120,203-250`）、
  `g3ku/runtime/api/ceo_sessions.py:920-1090`、`g3ku/runtime/api/websocket_ceo.py:585-615,760-790`、
  `g3ku/runtime/api/ceo_media.py:295-326`、`g3ku/runtime/session_keys.py:1-200`、
  `g3ku/runtime/web_ceo_sessions.py:50-80,725-847`、`g3ku/runtime/manager.py:1-130`、
  `g3ku/runtime/engine.py:1-120`、`g3ku/runtime/bootstrap_bridge.py:228-276`（grep 级）、
  `g3ku/agent/tools/shell.py:118-170,222-390,469-510,505-567,569-770,787-1020`、
  `g3ku/agent/tools/memory_update.py` 全文、`g3ku/agent/tools/base.py:120-175`、
  `g3ku/session/manager.py:81-330`、`g3ku/deployment/data_root.py:70-130`、
  `g3ku/deployment/runtime_startup.py:1-95`、`g3ku/config/schema.py:775,1054-1090`、
  `g3ku/web/worker_control.py:110-185`、`g3ku/qq_official/{bridge.py:640-780,1240-1360,client.py:1-140,messages.py:1-70}`、
  `main/governance/{review_policy.py 全文,action_mapper.py:1-80}`、
  `main/service/runtime_service.py:10520-10610`（+ `execution_mode` grep）、
  `main/runtime/react_loop.py:5172-5270`（+ 执行入口 grep）、`main/runtime/node_runner.py:3309-3480`（grep 级）、
  `Dockerfile`、`compose.yaml`、`.venv/Lib/site-packages/botpy/message.py:1-290`。
- **第四轮新增复核**（2026-10-27，为只读档读的边界）：
  `g3ku/agent/tools/shell.py:242-300`（`execution_mode != full_access` 时才套路径策略 + 工作区监禁 + 形态守卫）、
  `:480-567`（`_resolve_execution_mode`、`_enforce_read_only_command` 的**正则黑名单**与 `_readonly_error` 文案）、
  `g3ku/content/navigation.py:1620-1725`（`_resolve_workspace_path` / `_resolve_ref_workspace_path` 的
  `resolve()` → `relative_to(allowed_dir)` 顺序、`artifact:` 走 store 查找）、
  `g3ku/content/tool.py:470-490`（`allowed_dir = runtime.workspace if settings.restrict_to_workspace else None`）、
  `g3ku/agent/tools/filesystem_stat.py:230-255`、`g3ku/agent/tools/filesystem_mutation.py:80-100,1540-1570,1725-1740`、
  `g3ku/agent/tools/filesystem.py:1-60`、`g3ku/agent/tools/propose_patch.py:35-120`、
  `main/runtime/recovery_check.py:30-90`（写类工具名的权威清单）、
  `tools/*/resource.yaml`（全量目录清单 + `content_*`/`filesystem_stat`/`view_message_history`/`web_fetch`/
  `send_message`/`cron`/`process_kill`/`skill-installer`/`search_stickers` 的 risk/permissions/exposure 读数）。
- **标 † 待复核**（子代理读数，动对应阶段前逐行核）：
  前端落点 `g3ku/web/frontend/org_graph.html:234-308`、`ceo/model-panel.js:104-190`、
  `org_graph_ceo_governance.js:411/441/985`、`client_api/ceo.js:85-114/143-151`、
  `org_graph_app.js:803-846/3398-3415`；
  `main/api/admin_rest.py:225-250/1828-1849/1905-1926`；`main/governance/store.py:81-86/290-310`；
  `g3ku/runtime/web_ceo_sessions.py:1721-1727/2196-2203`（目录字段）；
  `g3ku/runtime/api/websocket_ceo.py:370-424/735-767`（上传落盘与路径守卫）；
  `g3ku/runtime/api/ceo_media.py:101-130/246-301`（token 与附件抽取）；
  `g3ku/runtime/external_events.py`（出站附件抽取车道）。
  **P0-7 必须先核的两件**：① `main/storage/artifact_store.py` 是否已有会话/任务归属字段
  （决定 third_party 只读档能不能给 `view_message_history`，见 P0-7 第 3 条）；
  ② 会话入站上传目录与 temp 目录的**确切解析函数**（读根要从同一处取，不能各写一遍）。
- **外部调研**（2026-10-26，只读，未落任何第三方代码）：
  DeepSeek Harness `docs/subsystems/sandbox.zh.md`、`docs/subsystems/permission-presets.md`、
  `packages/sandbox/{README.md,sandbox-local/README.md,sandbox-local/src/index.ts,sandbox-policy/README.md}`、
  `.agents/notes/archived/feature/2026-07-06-sandbox.md`（gap：无网络限制、in-process tools 另走 fs provider）、
  `.agents/notes/implemented/architecture/{2026-09-11-sandboxed-node-ptc-runtime,2026-10-08-persistent-ptc-sandbox}.md`、
  CVE-2026-82533 报道与 discussions #4503 / #8678；
  Hermes Agent `tools/environments/{base,docker,local,local_env_policy,file_sync,ssh,docker_egress}.py`、
  `tools/terminal_tool_config.py`、`agent/terminal_env_{provider,registry}.py`、
  `plugin-catalog/hermes-wslc-sandbox.yaml`（wslc 的能力缺口）、
  `website/docs/developer-guide/terminal-environment-plugin.md`；
  Podman 官方安装文档（Windows 上每个 podman machine 由 WSLv2 发行版承载）。
  **未克隆任何第三方仓库到本机**（按 `feedback-cleanup-cloned-repos`，全程只走 raw/API 读取）。
  另需澄清两处易误认：Hermes 的 `apps/desktop/electron/windows-sandbox-fallback.ts` 是 **Electron/Chromium 自己的
  渲染进程沙箱**恢复逻辑，`nix/sandbox.nix` 是开发用 nix-shell 包装，**都与 agent 工具沙箱无关**。
- **第一步**：只做 P0（七个子项：P0-1 来源判定、P0-2 桥补 sender、P0-3 投影标注、P0-4 记忆门、
  P0-5 记忆读侧、P0-6 档位解析 + 控制面拒绝、**P0-7 只读档读根限定**），
  每项可独立验收、**不引入任何沙箱后端**（不需要 bwrap / WSL / 容器）。P0 上线后再评估 P1a 的后端成本。
  **P0-6 与 P0-7 必须同批上线**：只给读类工具而不限定读根，等于把宿主文件系统开给群聊（见 P0 风险 5/7）。
- **已决**：四轮拍板全部落地（见「拍板结果」「第三轮拍板」「第四轮拍板」）。P0 的工具名单按
  `third_party + restricted + not_installed → readonly` 档执行，**P0 现在可以开工**。
- **无未决的用户决策项**：
  - 风险登记第 9 条的 b（N 天自动回弹）与 c（会话级 `restrict_to_workspace` + `content_*` 只读沙箱卷）
    已由用户明确**不采纳**，只做 a（常驻告警 + 审计 + doctor 清单）。这条残留风险要写进变更说明与架构文档。
  - 第四轮我按推荐默认定的四条（可否决，不再单独询问）：
    **D18** 受限档一律禁 `exec`（third_party）/ 禁写 / 禁网 / 禁任务，owner 只读档放宽 `exec`=`governed` 与 `web_fetch`；
    **D19** 口令闸门在未安装（全权限）与已安装（关闭沙箱）两态一致；
    **D20** 人设配置不锁在安装态后面；
    **只读档名单**含 `search_stickers` 与 `view_message_history`（后者取决于 artifact 归属字段的核实结果）。
- **待核实的实现事实（不是决策）**：`main/storage/artifact_store.py` 的会话归属、会话上传/temp 目录的解析函数。
  两项都在 P0-7 动手前核，结果只影响名单里少不少一个工具，不影响档位模型。
- **P1a / P1b 拆分**：P1a 假定后端依赖已在（手动装也能用），P1b 才做一键安装；两者分别验收，互不阻塞。
