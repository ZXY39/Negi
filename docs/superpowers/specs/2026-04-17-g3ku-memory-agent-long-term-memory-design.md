# G3KU 记忆 Agent 长期记忆机制设计规格

> 状态：设计确认稿
> 日期：2026-04-17
> 语言：中文
> 适用范围：G3KU CEO/frontdoor 长期记忆机制重构

## 1. 背景

G3KU 现有长期记忆机制基于旧 memory runtime、结构化事实写入、检索投影和语义摘要链路实现。该实现对运行时有较强耦合，维护复杂度高，也不符合本次需求提出的目标：

- 使用专门的 memory agent 统一维护长期记忆
- 以单一 Markdown 文档作为长期记忆正文源
- 保持记忆正文极简、可人工检查、可直接注入上下文
- 将显式记忆、自主记忆、压缩前 flush、会话边界 flush 统一收口到异步队列和单一 memory agent

本设计以本次对话中后续补充要求为最终准绳。若与更早讨论版本冲突，以本规格为准。

## 2. 目标

本次重构必须实现以下目标：

- 将长期记忆的正文源统一收敛为 `memory/MEMORY.md`
- 引入单一 memory agent，作为唯一长期记忆正文维护者
- 引入单一异步 durable 队列，统一承载“写入记忆”和“删除记忆”请求
- 保证写入批处理，但不同处理类型不得混批
- 保证每个 fresh turn 看到的是该 turn 的冻结记忆快照，而非实时热更新内容
- 将 `MEMORY.md` 直接注入当前 turn 上下文
- 移除 `memory_search` 工具，新增 `memory_note` 工具用于按需加载详细笔记
- 将详细笔记放在 `memory/notes/` 目录，而不是内联写入 `MEMORY.md`
- 严格控制记忆正文体积，保证可读性与 prompt 成本

## 3. 非目标

本次设计明确不做以下事情：

- 不再使用旧的结构化 memory fact 作为长期记忆正文源
- 不再要求长期记忆依赖向量库、Qdrant、structured_current 等投影文件
- 不将运行时过程状态、工具处理中提示、压缩状态、pause/resume 控制信息写入长期记忆
- 不为长期记忆正文引入额外标题、编号、状态字段、canonical key、fact id 等技术字段
- 不引入多个 memory agent
- 不拆分多个物理队列分别处理写入与删除

## 4. 术语

### 4.1 长期记忆正文

指 `memory/MEMORY.md` 中保存的极简记忆块集合，是长期记忆唯一提交态正文源。

### 4.2 详细笔记

指 `memory/notes/` 目录下的 Markdown 文件，用于承载无法在 100 字内稳定表达的复杂背景信息。

### 4.3 记忆请求

指进入异步队列、等待 memory agent 处理的写入或删除请求。

### 4.4 冻结记忆快照

指某个 fresh turn 开始时读取的 `MEMORY.md` 内容快照。在该 turn 生命周期内保持不变，即使后台 memory agent 已更新文档，也不会热更新当前 turn。

### 4.5 决定源

记忆来源只允许两种短标签：

- `user`
- `self`

其中：

- `user` 表示用户明确要求记忆，或用户明确表达了稳定偏好、规则、默认值、约束
- `self` 表示系统自主归纳、自主复盘、自主压缩、自主清理、自主合并得到

## 5. 总体架构

新长期记忆机制由以下 5 个核心组成：

1. `memory/MEMORY.md`
   长期记忆唯一正文源
2. `memory/notes/`
   详细笔记目录
3. `memory/queue.jsonl`
   单一 durable 异步队列
4. `memory/ops.jsonl`
   已处理操作日志与幂等审计
5. 单一 memory worker + 单一 memory agent
   按批消费队列，统一修改 `MEMORY.md`

职责边界如下：

- `memory_write`：只提交写入请求，不直接改 `MEMORY.md`
- `memory_delete`：只提交删除请求，不直接改 `MEMORY.md`
- `memory_note`：只读加载 `notes/` 下的详细笔记
- memory worker：负责从单一队列取批、组装 agent 输入、执行验证、提交回写
- memory agent：负责做记忆归纳、重写、合并、删除、外置详细笔记

## 6. 文件结构

长期记忆目录固定为：

```text
memory/
├─ MEMORY.md
├─ queue.jsonl
├─ ops.jsonl
└─ notes/
   ├─ note_a1b2.md
   └─ note_x9k3.md
```

约束如下：

- `MEMORY.md` 只保存记忆正文块，不允许写额外标题、统计信息、版本信息、索引信息
- `queue.jsonl` 和 `ops.jsonl` 可以保存技术元数据，但这些元数据不得出现在 `MEMORY.md`
- `notes/` 目录只保存详细笔记，不保存队列或审计信息

## 7. MEMORY.md 正文格式

### 7.1 顶层规则

`MEMORY.md` 中只允许由多个记忆块顺序组成，不允许任何其他文档级字段。

每个记忆块格式必须为：

```md
---
2026/4/17-self：
完成任务必须说明任务总耗时
```

或：

```md
---
2026/4/17-user：
创建文件默认格式要求，见 ref:note_a1b2
```

### 7.2 强制规则

- 每个记忆块必须以单独一行 `---` 开始
- 第二行必须为 `YYYY/M/D-source：`
- `source` 只允许 `user` 或 `self`
- 第三行必须为正文精炼句
- 正文精炼句长度必须 `<= 100` 字符
- 正文精炼句中允许出现短引用，如 `ref:note_a1b2`
- 不允许在正文块中出现其他字段名，如：
  - `created_at`
  - `category`
  - `canonical_key`
  - `detail`
  - `summary`
  - `metadata`
  - `status`

### 7.3 正文长度规则

正文精炼句长度上限固定为 `100` 字符。

处理策略如下：

- 若 memory agent 产出的正文超过 100 字符，视为不合格输出
- worker 必须拒绝直接写回
- worker 需要要求 memory agent 重新压缩改写
- 若重写后仍然超过 100 字符，则该批次失败并进入错误处理流程

### 7.4 正文整体体积规则

`MEMORY.md` 文件全文字符数上限固定为 `10000` 字符。

约束如下：

- 统计对象仅为 `MEMORY.md` 正文文件
- `notes/` 目录中的详细笔记不计入 10000 字符限制
- 当 `MEMORY.md` 接近或超过上限时，memory agent 必须优先：
  - 合并相近 `self` 记忆
  - 删除过时 `self` 记忆
  - 将复杂背景外移到 `notes/`
  - 压缩冗余表述
- `user` 来源记忆的保留优先级高于 `self`

## 8. 详细笔记与 notes 目录规则

### 8.1 使用条件

当出现以下任一情况时，允许创建 note 文件：

- 记忆信息密度高，无法在 100 字内稳定表达
- 一条记忆包含多条件、多约束、多引用
- 为了满足 100 字正文上限，需要把细节外移

### 8.2 note 引用规则

正文中只允许写短引用，不写长路径。推荐格式：

- `ref:note_a1b2`

正文示例：

```md
---
2026/4/17-self：
任务完成回复规则，见 ref:note_a1b2
```

对应 note 文件：

```text
memory/notes/note_a1b2.md
```

### 8.3 note 文件规则

- note 文件内容为普通 Markdown
- note 文件允许保存详细说明、示例、引用片段、复杂背景
- note 文件不作为长期记忆快照默认注入上下文
- note 文件只在显式调用 `memory_note` 时加载

### 8.4 note 清理

memory agent 本身不暴露通用删除工具。  
孤儿 note 清理由运行时非 agent 清理流程负责：

- 当某个 note 不再被任何记忆正文引用时，运行时可在后处理阶段删除该 note 文件
- 该清理不属于 `MEMORY.md` 正文协议的一部分

## 9. 工具契约

## 9.1 工具集合变更

本规格将长期记忆工具集合调整为：

- 保留：`memory_write`
- 保留：`memory_delete`
- 新增：`memory_note`
- 删除：`memory_search`

这意味着本次重构不再维持“长期记忆查询工具名不变”的旧约束。以本规格为准。

### 9.2 memory_write

职责：

- 接收显式写入记忆请求
- 将工具入参转换为“写入型记忆请求”
- durable 写入 `queue.jsonl`
- 可选尝试一次短超时 fast-path 处理

约束：

- `memory_write` 不直接写 `MEMORY.md`
- `memory_write` 只提交候选记忆，不自行决定最终正文格式
- memory agent 才是长期记忆正文唯一维护者

### 9.3 memory_delete

职责：

- 接收显式删除记忆请求
- durable 写入删除型请求到 `queue.jsonl`

删除定位方式：

- 因 `memory_search` 已被移除，删除不再依赖 search id
- `memory_delete` 的目标应来自当前 turn 可见的冻结记忆快照文本
- 请求中可以包含：
  - 当前可见的记忆正文块文本
  - 目标记忆的正文句子
  - 目标 note 引用 `ref:note_xxxx`

最终是否删除、删除哪一条，以 memory agent 对当前 `MEMORY.md` 的解析和匹配为准

### 9.4 memory_note

职责：

- 加载 `memory/notes/` 目录中的详细笔记
- 类似 `load_skill_context` 的按需展开语义

输入：

- `ref`

返回：

- 对应 note 文件正文
- 可包含简短元信息，如解析到的 note 文件名

约束：

- `memory_note` 只读，不允许写入
- 若 `ref` 无效或 note 文件不存在，应返回明确错误

## 10. 队列设计

### 10.1 单队列原则

系统只允许存在一个物理队列：

- `memory/queue.jsonl`

不得拆为：

- 写入队列
- 删除队列

### 10.2 单 worker / 单 agent 原则

系统只允许存在：

- 一个 memory worker
- 一个 memory agent

同一时刻只允许一个 memory worker 对 `MEMORY.md` 提交修改，保证串行一致性。

### 10.3 队列项语义

队列中的请求逻辑语义如下：

```text
要求：写入
决定源：自主记忆
【触发的上下文片段】
```

```text
要求：写入
决定源：自主记忆
【memory_write工具传入的内容】
```

```text
要求：写入
决定源：用户要求
【memory_write工具传入的内容】
```

```text
要求：删除
决定源：自主决定
【当前记忆快照中的目标记忆文本，或工具传入的删除目标】
```

### 10.4 队列文件中的技术字段

虽然 `MEMORY.md` 不允许保存技术元数据，但 `queue.jsonl` 中允许保存最小技术字段，用于恢复和幂等：

- `request_id`
- `created_at`
- `op`
- `decision_source`
- `trigger_source`
- `session_key`
- `payload_text`

这些字段只存在于队列和审计文件中，不得写入 `MEMORY.md`。

## 11. 批处理规则

### 11.1 批次字符上限

单批发送给 memory agent 的待处理请求总字符数上限固定为：

- `50000`

允许：

- 单次发送少于 50000 字符

不允许：

- 超过 50000 字符仍然继续拼批

### 11.2 同类同批规则

不同处理类型不得同批交给 memory agent。

即：

- `write` 与 `delete` 不得混批
- 只有相同处理类型才能在同一批中发送

### 11.3 取批规则

worker 从队首开始取批：

1. 读取队首请求类型
2. 只继续吸收后续连续的相同类型请求
3. 若累计字符数即将超过 50000，则停止取批
4. 若遇到不同类型请求，立即停止取批

示例：

```text
write
write
delete
write
```

则第一批只能取前两个 `write`，不得把后面的 `delete` 或第四条 `write` 混入当前批次。

### 11.4 最长等待时间

为避免队列长期等待凑批，默认配置：

- 批次最长等待时间：`3 秒`

含义：

- 即使未达到 50000 字符
- 只要队首请求等待时间达到 3 秒
- 也应立刻发起该批次处理

## 12. memory agent 输入与权限

### 12.1 memory agent 输入

每次调用 memory agent 时，输入必须包含：

1. 固定 system prompt
2. 当前批次待处理请求
3. `MEMORY.md` 路径
4. 当前 `MEMORY.md` 全文
5. `notes/` 目录约束说明

### 12.2 memory agent 固定职责

memory agent 的职责仅限于：

- 根据批次请求修改长期记忆正文
- 在必要时创建详细笔记
- 合并、压缩、替换、删除现有记忆
- 保证输出满足格式和体积约束

### 12.3 可用工具

memory agent 仅允许使用以下最小工具面：

- `content_open`
  - 读取 `MEMORY.md`
  - 读取已有 note 文件
- `filesystem_write`
  - 重写 `MEMORY.md`
  - 创建新的 note 文件
- `filesystem_edit`
  - 对 `MEMORY.md` 做精确编辑

不向 memory agent 暴露：

- 通用搜索工具
- 通用 exec
- 网络工具
- 任务工具
- 通用删除工具

## 13. 触发机制

### 13.1 显式写入触发

用户或模型调用 `memory_write` 后，运行时创建写入型请求并入队。

### 13.2 显式删除触发

用户或模型调用 `memory_delete` 后，运行时创建删除型请求并入队。

### 13.3 周期自主复盘触发

默认每 `10` 个可见用户 turn 触发一次自主复盘。

触发后：

- 运行时抽取最近相关上下文片段
- 形成 `self` 来源写入请求
- 入队等待 memory agent 处理

### 13.4 压缩前 flush

在上下文压缩即将丢弃原始对话近场信息前，运行时必须先生成一批 `self` 来源写入请求并入队。

目标：

- 给长期记忆一次从即将丢失的原始上下文中提炼 durable facts 的机会

### 13.5 会话边界 flush

在以下边界前，运行时应执行会话边界 flush：

- 新会话切换
- 会话清空
- 会话删除
- 进程正常关闭
- channel clear

注意：

- 手动 pause 不视为会话结束
- pause 不触发 session-boundary flush

## 14. 冻结快照与上下文注入

### 14.1 注入策略

`MEMORY.md` 的内容会直接加载到当前 turn 上下文中。

但为兼容 G3KU 现有 prompt cache 与 turn 级状态设计，本规格要求：

- `MEMORY.md` 在每个 fresh turn 开始时读取一次
- 形成该 turn 的冻结记忆快照
- 该快照属于当前 turn 的上下文注入内容
- 不在当前 turn 中途热更新

### 14.2 与 notes 的关系

- `MEMORY.md` 默认直接进入当前 turn 上下文
- `notes/` 不默认注入
- 只有在显式调用 `memory_note(ref=...)` 时才读取对应 note

### 14.3 与缓存稳定性的关系

长期记忆快照应作为每个 turn 的冻结输入处理，不应在同一 turn 内因为后台 memory agent 写盘而变化。  
这保证：

- 当前 turn 行为稳定
- 不出现同一轮 prompt 漂移
- 不出现模型前后看到不同记忆正文的问题

## 15. 写入、合并、删除规则

### 15.1 写入规则

- `user` 来源记忆优先级高于 `self`
- 新记忆优先压缩为 100 字以内精炼句
- 若无法压缩，应外移为 note，并在正文中保留短 ref
- 不要机械重复追加相近内容
- 若新记忆是旧记忆的更准确版本，应替换旧记忆

### 15.2 合并规则

- 同主题、同语义、同来源的相近记忆允许合并
- 合并后正文仍不得超过 100 字
- 若合并后仍过长，应外移细节到 note

### 15.3 删除规则

- 仅删除明显过时、冲突、冗余、或已被新记忆完全覆盖的内容
- `self` 不应删除用户明确要求保留的核心规则，除非后续用户明确推翻
- 删除优先通过“以新替旧”实现，而不是盲删

## 16. 验证与失败处理

### 16.1 写回前验证

worker 在提交 `MEMORY.md` 前必须执行验证：

- 文件只包含合法记忆块
- 每块格式合法
- 每块正文长度 `<= 100`
- 全文长度 `<= 10000`
- note 引用格式合法

### 16.2 不合格输出处理

若 memory agent 输出不合格：

1. worker 拒绝写回
2. worker 发起一次修复轮
3. 若仍失败，则本批次失败并记录到 `ops.jsonl`

### 16.3 幂等与审计

每条请求必须带稳定 `request_id`。  
`ops.jsonl` 用于记录：

- 请求是否处理成功
- 是否已重试
- 是否已落盘
- 最终失败原因

## 17. 迁移边界

本次重构完成后，长期记忆主路径应满足：

- `MEMORY.md` 成为唯一长期记忆正文源
- 旧 RAG memory runtime 不再作为长期记忆 source of truth
- 旧 memory search 路径退出长期记忆主协议

旧系统的兼容与迁移不属于本规格的正文协议，但实施时需要提供：

- 旧记忆内容导入策略
- 旧提示词与测试用例清理策略
- 旧 memory runtime 下线路径

## 18. 成功标准

当以下条件全部满足时，本设计视为完成：

- `MEMORY.md` 成为唯一长期记忆正文源
- memory agent 成为唯一正文维护者
- 只有单队列、单 worker、单 agent
- `write` 和 `delete` 请求不混批
- 单条正文严格不超过 100 字
- `MEMORY.md` 全文严格不超过 10000 字
- `memory_search` 被移除
- `memory_note` 可按 `ref` 成功加载 note
- `notes/` 正常承载复杂细节
- 每个 fresh turn 使用冻结记忆快照
- 显式写入、自主复盘、压缩前 flush、会话边界 flush 全部走统一异步队列

## 19. 实施备注

本规格是设计文档，不代表代码已完成。  
进入实施阶段前，应基于本规格再产出独立实现计划，明确：

- 模块划分
- 迁移步骤
- 测试矩阵
- 文档更新范围
