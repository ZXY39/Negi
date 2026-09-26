# 执行节点与检验节点的全局模型负载均衡实现计划

- **状态**：已实现，见 §16。§13 是 Q1–Q6 裁定，§14 是取证与冲突清单；§4–§12 正文已按裁定改写，**§14.4 的冲突不再存在**。
- **编写日期**：2026-09-26
- **目标范围**：执行节点（`execution`）与检验/验收节点（`inspection`）的模型路由、配置、运行时、准入排队、管理面、测试和运维文档
- **核心目标**：把当前“按列表顺序逐个尝试”的模型链扩展为“有序 fallback 链 + 链内平级负载均衡组”，让同一个负载均衡组在多个任务、多个节点、执行/检验两条车道之间共享实时负载与上游限流观测，并在**准入层以节点为粒度**完成原子选择与绑定。

---

## 1. 需求结论与边界

### 1.1 需要实现的行为

1. **模型链仍然保留顺序**：链上的节点从前到后仍代表 fallback 优先级。
2. **新增负载均衡组（Load Balance Group）**：组内模型是平级候选，不再按配置顺序固定调用。
3. **一个模型链允许多个负载均衡组**：例如“主模型组 → 专用检验模型 → 应急模型组”。
4. **组内选择按综合负载，绑定粒度是节点**：新节点在组内选择当前综合 score（本地在飞 + 60 秒滚动请求速率 + 衰减 429 惩罚）最低的可用成员，并在该节点生命周期内粘滞复用，直到命中重绑条件。
5. **全局共享**：同一个组的状态以 `group_key` 为键，不能按任务、节点或会话隔离；同一组被多个任务引用时共享运行中请求、排队请求、滚动速率和短期故障状态。
6. **负载与限流观测按配额桶聚合**：同一 endpoint + 同一 API key 的多个 binding 合并为一个配额桶，RPM 与 429 惩罚按桶共享；不同 key 是否共享上游账户只允许 operator 显式声明 `quota_pool_key`，不按 provider 名称猜测。
7. **失败仍按链 fallback**：
   - 组内首选成员暂时不可用、容量已满、配置不可解析或 provider 失败时，先尝试该组内尚未尝试的其他成员；
   - 组内候选全部不可用或耗尽后，才进入模型链中的下一个 route entry；
   - 不把“组内成员顺序”当作优先级。
8. **组内有预算上限且有节拍**：成员使用 group 级 `max_retry_rounds`（默认 1，允许 1..3），不继承 catalog 的 `retry_count`；换成员之间沿用现有封顶指数退避，不得背靠背打满整组。
9. **不改变已有安全边界**：请求形状错误（结构化 400/422）、内部运行时错误、已经产生可见流式文本后的请求，不得被错误地透明拼接或无限 fallback。

### 1.2 第一阶段范围

第一阶段只把全局负载均衡接入 `execution` 和 `inspection`。CEO/frontdoor 与 memory 继续使用现有 ordered chain 语义，避免把会话固定模型、CEO 的缓存键和 frontdoor 的 retry contract 一次性混入节点路由改造。

但是，配置模型和 route 抽象应设计成通用的，后续可以在不改配置格式的情况下把 CEO/memory 也接入同一套 route engine。第一阶段若 `ceo` 或 `memory` 配置了负载均衡组，应给出明确的校验错误，而不是静默按字符串处理。

### 1.3 “全局”的准确含义

当前受支持的任务运行架构中，`main/service/runtime_service.py` 在 worker 进程内创建一个共享的 `ModelKeyConcurrencyController`；所有任务和节点都通过该 runtime service 使用它。实现应在这个同一实例上挂载全局负载均衡器，因此在一个 worker 进程内满足“跨任务、跨节点、跨 execution/inspection”的全局共享。

当前 Compose 拓扑由一个 `worker` 负责后台任务执行；web 进程不直接执行节点模型请求。如果未来运行多个 worker 副本，纯内存计数无法跨进程共享，需要第二阶段引入共享 lease/计数存储（例如专用 SQLite/Redis）。第一阶段必须在日志和文档中明确这个边界，不能把“单 worker 全局”误写成“多副本集群全局”。

---

## 2. 当前实现基线

### 2.1 配置目前只有扁平字符串链

相关位置：

- `g3ku/config/schema.py`
  - `RoleModelRoutingConfig` 的 `ceo/execution/inspection/memory` 当前是 `list[str]`。
  - `Config.get_role_model_keys()` 返回字符串列表。
  - `Config.get_scope_model_chain()` 按列表顺序生成 `ModelFallbackTarget`。
- `g3ku/config/loader.py`
  - `_managed_models_payload()` 和 `_runtime_config_payload()` 只序列化扁平的 `models.roles.*`。
- `g3ku/config/model_manager.py`
  - `_prepare_scope_route_update()` 只校验和保存 `model_keys`。

因此当前配置无法表达“一个链节点是 group，group 内有多个模型”。

### 2.2 目前存在两套模型 fallback 循环

1. `g3ku/providers/fallback.py` 的 `FallbackProvider`：
   - 接收 `model_chain: list[str]`；
   - 从 `model_index = 0` 开始，按顺序尝试模型；
   - 同一模型内处理 API key 轮换、retry_on、retry_count 和退避；
   - 模型耗尽后进入链下一个模型。
2. `main/runtime/chat_backend.py` 的 `ConfigChatBackend.chat()`：
   - 节点请求接收 `model_refs: list[str]`；
   - 自己维护 `model_index`、`tried_model_refs`、retry round、model boundary refresh 和 `ModelKeyConcurrencyController` permit；
   - 当前 `model_refs` 的列表顺序就是执行/检验节点的 fallback 顺序。

第一阶段要改造的是第二套加上它的**准入层**（2.5）：执行节点和检验节点都经过 `main/runtime`，而模型许可早在 `NodeTurnController` 泵里就按链首被预占了。只改 `ConfigChatBackend` 而不改准入，均衡器读到的 `running` 会全部虚挂在链首成员上。不要在 `FallbackProvider` 和 `ConfigChatBackend` 中分别复制一套负载均衡算法。

### 2.3 节点只在 NodeRunner 处区分 execution/inspection

相关位置：

- `main/runtime/node_runner.py`
  - `_model_refs_for()` 根据 `node.node_kind` 返回 `_execution_model_refs` 或 `_acceptance_model_refs`。
  - `_runtime_context()` 把扁平 `model_refs` 放进节点上下文。
  - spawn review、distribution decision 等控制回合也复用 inspection/execution refs。
- `main/service/runtime_service.py`
  - 从配置读取 execution/inspection refs 并注入 `NodeRunner`。
  - 已经创建并共享 `ModelKeyConcurrencyController`、`NodeTurnController`。

改造后应把这些 refs 替换为 route plan；同时保留一个“展开后的候选 model keys”视图供上下文窗口、多模态和诊断逻辑使用。

### 2.4 现有并发控制可以复用，但还不是负载均衡

`main/runtime/model_key_concurrency.py` 已经按 `model_ref + api_key_index` 维护 running/waiting 和 permit，`model_state()`（`:61-79`）已能返回 `key_indexes/per_key_limits/running/waiting`。它解决的是“单模型/单 key 的并发上限”，不是“组内哪个成员应该接收下一个节点”。

需要在其上增加组级原子选择/预占，不能采用以下非原子流程：

1. 先读取所有模型的负载；
2. 选出最低者；
3. 再单独 acquire permit。

否则大量并发节点会同时读到相同的最低值，随后全部打到同一模型，负载均衡会失效。

两个已存在的偏差必须一起处理：

- `try_acquire_first_available()`（`:81-94`）按 `key_indexes` 的**固定顺序**取第一个有容量的 key，key 轴同样有首项偏置；
- `_slot_has_capacity_locked()`（`:219-223`）在 `per_key_limit` 为 `None` 时恒返回可用。现网 14 条 catalog 全为 `None`，因此队列从不排队、`waiting` 恒为 0——**不能把“本地拿不到 permit”当默认预期，也不能反过来用 score 阈值伪造 busy**。

### 2.5 节点回合的模型许可发生在准入层，不在 chat 层

这是接入负载均衡最关键的现存耦合：

- `main/runtime/react_loop.py:749` 取 `current_model_refs[0]`（链首）作为 `model_ref` 调 `node_turn_controller.acquire_turn()`；
- `main/runtime/node_turn_controller.py:190` 在 pump 里对该 `model_ref` 调 `try_acquire_first_available()` **预占一颗 permit**，存进 `NodeTurnLease.initial_model_permit`；
- `main/runtime/chat_backend.py:889-906` 只有当链首 ref + key index + `attempt_number == 1` 同时命中时才消费这颗 permit；不命中就一路攥到 `chat()` 的 `finally`（`:1151-1153`）才释放。

后果：如果组选择只放在 chat 层，每个在飞回合都会给“组内第一个成员”挂一份 `running`，而请求实际打在别的成员上——按 running 打分的均衡器会系统性回避链首，且同一请求持有两颗 permit。因此首次选择必须上移到准入层（§13 Q1）。

同一 pump 还有一个排队副作用：`:190-196` 拿不到 permit 就 `break`，即队头成员满时会阻塞后面本可用的请求；改造后必须改成有界扫描（4.3）。

### 2.6 管理面目前只支持拖拽模型到 role chain

相关位置：

- `main/api/admin_rest.py`：`PUT /api/models/roles/{scope}`、`PUT /api/models/routes/batch`。
- `g3ku/llm_config/facade.py`：`get_routes()` / `set_route()`。
- `g3ku/web/frontend/api_client.js`：模型配置 API 调用。
- `g3ku/web/frontend/org_graph_llm.js`、`org_graph_app.js`：模型链编辑器。

现有前端把链渲染成模型数组，且文案默认暗示列表顺序就是优先级。需要增加 group 容器和“组内顺序不代表优先级”的明确提示。

---

## 3. 推荐配置模型

### 3.1 使用“中央 group 定义 + 链 route entry 引用”

不要把完整的组成员数组直接复制到每一个 role chain 条目中。推荐在 `models` 下增加全局 group 定义，链中只放引用：

```yaml
models:
  catalog:
    - key: openai_a
      # provider/binding fields...
    - key: openai_b
      # provider/binding fields...
    - key: backup_model
      # provider/binding fields...

  loadBalanceGroups:
    shared_execution_inspection:
      enabled: true
      maxRetryRounds: 1        # 4.4：允许 1..3，成员 catalog 的 retryCount 对 group route 不生效
      modelKeys:
        - openai_a
        - openai_b

  roles:
    execution:
      - type: load_balance
        groupKey: shared_execution_inspection
      - type: model
        modelKey: backup_model

    inspection:
      - type: load_balance
        groupKey: shared_execution_inspection
      - type: model
        modelKey: backup_model
```

语义如下：

- `execution` 的第一跳和 `inspection` 的第一跳都引用同一个 `groupKey`，因此共享同一个全局负载池。
- `backup_model` 是 group 之后的 fallback route entry，只有组内模型均不可用/耗尽时才尝试。
- 同一个 group 可以被多个任务、多个节点、多个 role chain 引用。
- group 内 `modelKeys` 的顺序仅用于展示和稳定序列化，不参与选择优先级。
- 负载与 429 统计按**配额桶**聚合而不是按 binding 聚合（4.1）。当 operator 知道两个不同 key 共享同一上游 RPM 账户时，在 catalog 条目上显式声明同一个 `quotaPoolKey`；不配置时只按运行时解析到的 endpoint + key 自动合并，且**不猜测**。

```yaml
models:
  catalog:
    - key: openai_a
      quotaPoolKey: gw_main_60rpm   # 可选；同池成员共享 RPM/429 惩罚
```

### 3.2 Pydantic 类型建议

在 `g3ku/config/schema.py` 增加以下概念（名称可微调，但语义保持一致）：

```python
class ModelRouteEntry(Base):
    type: Literal["model", "load_balance"]
    model_key: str | None = None
    group_key: str | None = None

class ModelLoadBalanceGroup(Base):
    enabled: bool = True
    max_retry_rounds: int = 1            # 校验范围 1..3，超上限直接报错，不夹断
    model_keys: list[str] = Field(default_factory=list)

class RoleModelRoutingConfig(Base):
    ceo: list[ModelRouteEntry | str] = Field(default_factory=list)
    execution: list[ModelRouteEntry | str] = Field(default_factory=list)
    inspection: list[ModelRouteEntry | str] = Field(default_factory=list)
    memory: list[ModelRouteEntry | str] = Field(default_factory=list)

class ModelsConfig(Base):
    catalog: list[ManagedModelConfig] = Field(default_factory=list)
    roles: RoleModelRoutingConfig = Field(default_factory=RoleModelRoutingConfig)
    load_balance_groups: dict[str, ModelLoadBalanceGroup] = Field(default_factory=dict)
```

`ManagedModelConfig` 增加可选 `quota_pool_key: str | None`。

实现时要遵守现有 `Base` 的 camelCase/snake_case 双向填充规则，并支持 `type/modelKey/groupKey/modelKeys/maxRetryRounds/quotaPoolKey` 与 snake_case 输入。

### 3.3 旧配置兼容

旧配置必须继续可读：

```yaml
models:
  roles:
    execution: [model_a, model_b]
```

加载时把字符串规范化为 direct model route entry：

```json
[
  {"type": "model", "modelKey": "model_a"},
  {"type": "model", "modelKey": "model_b"}
]
```

为了减少无意义的 config diff，可以采用以下保存策略：

- 如果一个 role 仍然只包含 direct model entry，序列化时保留旧的字符串数组格式；
- 只要出现 group entry，就序列化为 route object 数组；
- 运行时内部始终使用规范化后的 `ModelRouteEntry`，不得在业务循环里到处判断“字符串还是对象”。

### 3.4 校验规则

保存和启动时都校验。新结构与 legacy 结构的严格度不同（§13 Q5）：**显式 `route_entries` 一律报错，legacy 扁平 `list[str]` 保持现有静默去重**。

1. route entry 的 `type` 只能是 `model` 或 `load_balance`。
2. `model` 必须有 `model_key`，且不能有 `group_key`。
3. `load_balance` 必须有 `group_key`，且不能有 `model_key`。
4. group key 必须存在于 `models.load_balance_groups`。
5. group 必须至少有一个成员；**组内成员重复直接报错**，不得静默去重（会改变均衡权重并隐藏配置错误）。
6. group 成员必须引用存在且 enabled 的 `models.catalog` 条目。
7. **显式 route 中**：同一链重复引用相同 group 报错；重复放置同一 direct model 报错。对照现状——`g3ku/config/schema.py:353-365` 与 `g3ku/config/model_manager.py:321-326` 都是静默丢弃空项与重复项，legacy 路径必须保持不变，只在显式 route 路径升级为报错。
8. 同时传入 legacy chain 与显式 route 时，以显式 route 为准并走严格校验，不做静默合并。
9. `max_retry_rounds` 必须在 `1..3`，超上限直接校验失败，不得夹断成 3。
10. `quota_pool_key` 只允许是非空稳定字符串；同一 pool key 的成员数不参与容量乘法（4.1）。
11. `ceo`/`memory` 在第一阶段若出现 `load_balance`，返回明确错误，例如“负载均衡组当前仅支持 execution/inspection”。
12. 仍保留现有 required role、模型 key 唯一性、context window、memory chat capability 等校验。
13. group key 不得与 model key 混淆，建议使用单独命名空间或禁止同名。

---

## 4. 全局负载均衡算法

### 4.1 负载定义

“负载”是综合 score，不是单一本地在飞数（§13 Q2）。四项相加，权重常量集中在 `main/runtime/model_load_balancer.py` 顶部，不散落各处：

```text
score(member) =
      w_local * (running + waiting + reserved) / max(local_capacity, 1)
    + w_rpm   * rolling_requests_60s(quota_bucket) / max(bucket_capacity, 1)
    + w_429   * decayed_429_penalty(quota_bucket)
```

- `running`：已拿到 provider permit、正在发 provider 请求的数量；
- `waiting`：已进入该模型/key 队列等 permit 的数量；
- `reserved`：被 selector 原子预占、尚未进入 provider 请求的数量；
- `rolling_requests_60s`：该**配额桶**在最近 60 秒内的请求启动数，作为上游 RPM 代理；
- `decayed_429_penalty`：该配额桶近期 429 计数的衰减值，时间常数对齐上游分钟窗口，不永久累积。

口径必须按 §13 Q3 的两层分开：

```text
路由成员：model_key / binding（决定 provider、协议、参数、context window、multimodal、retry policy）
配额身份：quota_bucket = (endpoint fingerprint, api_key fingerprint) 或显式 quota_pool_key
```

- 同一 endpoint + 同一 API key 的多个 binding 合并进同一个配额桶，RPM 与 429 惩罚按桶共享，不得按成员数量虚增容量；
- fingerprint 只存在于内存，日志与管理面只允许输出“桶数量/成员归属桶序号”，不得输出 fingerprint 或密钥；
- **fingerprint 只能在已解锁密钥的 worker 进程内算**。实测：在 web/worker 之外用 `build_provider_from_model_key()` 解析现网 6 个 execution ref，全部 `keys=0`、`base_url` 为空（sha256 空串 `e3b0c442`），空值若参与合并会把 6 个 binding 塌成一个假桶；因此空值一律记 `unresolved`，永不互并，也不参与桶合并优化；
- “不同 key 共享同一上游账户”不做猜测，只允许 operator 显式配 `quota_pool_key`，不得按 provider 名称推断。

`local_capacity` 与 `busy` 的判据必须与 score 解耦。实测：现网 14 条 catalog 的 `singleApiKeyMaxConcurrency` 全为 `None`，`_slot_has_capacity_locked()`（`main/runtime/model_key_concurrency.py:219-223`）对 `None` 恒返回可用 ⇒ 本地永不排队、`waiting` 恒为 0、score 的分母退化。因此：

- `None` 只代表“本地没有显式并发上限”，不代表上游有无限 RPM；
- `busy` 只能由“实际拿不到 permit”或“全部候选在 cooldown/已试过”推导，**不得由 score 阈值推导**（见 4.3）。

给运维展示“组内使用比例”时仍记：

```text
usage_ratio(model) = (running + reserved) / max(sum(group running + reserved), 1)
```

`waiting` 用于拥塞判断但不算进已发送占比。不使用进程启动以来的累计调用数作为选择依据。展示 `usage_ratio` 时必须同时显示该成员所属配额桶，避免把同桶的两个 binding 读成两份独立容量。

### 4.2 选择规则

选择发生在**准入层**，绑定粒度是**节点**（§13 Q1 + 本文 14.2）。`NodeTurnController._pump()` 在同一把锁内向全局 balancer 请求候选成员并取得该成员/key 的 permit，然后创建带 `route_index / group_key / model_ref / key_index / permit` 的 `NodeTurnLease`。

节点首次准入（或命中重绑条件）时：

1. 读取该 group 的当前成员快照。
2. 用 request preflight 已得到的条件过滤成员：context window 能容纳当前估算、需要图像时 `image_multimodal_enabled` 为真、provider protocol 支持当前 request shape、本请求已尝试过的排除。现序已满足——`main/runtime/react_loop.py:743-746` 先判 preflight 失败并返回，`747-760` 才准入。
3. 排除 cooldown 中的成员。
4. 按 `score` 升序选择；同分按“该配额桶最久未被选中”打破平局，再按稳定 key 排序保证可测试性。
5. 在同一个锁/原子操作中写 `reserved` 并拿到对应 model/key permit；锁内不得构建 provider、不得网络 I/O、不得 `await`。
6. 记入亲和账本 `node_id → (route_index, group_key, model_ref, key_index, permit)`；该节点后续回合直接复用绑定成员，不重选。
7. 重绑触发条件（任一即重选）：阶段边界、preflight 过滤条件变化（多模态或 context 需求变了）、绑定成员进入 cooldown 或其配额桶 penalty 超阈、route plan/config revision 变化、节点结束（释放亲和）。
8. provider attempt 结束后，无论成功、异常或取消，都必须 exactly-once release lease。

绑定粒度取节点级而不是逐回合重选的代价依据见 14.2：换 model_key 等于换前缀缓存命名空间，断点之后的**整段存活上下文**都要重传，实测一跳的脱靶量中位 32,768 token。

不得用 `random.choice()` 作为唯一策略，也不得把配置数组第一项当 tie-breaker；那会重新引入固定顺序偏差。

### 4.3 并发与容量满时的行为

- 组内有可用成员时，按 4.1 的综合 score 选。
- 组内所有成员要么拿不到 permit、要么在 cooldown、要么本请求已试过，则该 group route 判 `busy`，按链顺序进入下一个 route entry。`busy` 只由这三类事实推导，不由 score 阈值推导（现网 `per_key_limit` 全 `None`，score 推不出 busy）。
- `_pump()` 不再“队头模型没有容量就 break”（现状：`main/runtime/node_turn_controller.py:190-196` 拿不到 permit 就跳出，且是先拿 permit 再 `popleft()` 队头）。改造后必须：
  - 对未冻结请求做**有界扫描**（第一版 `_MAX_SCAN = 8`），跳过当前不可用的请求，授予后面可用的 route；
  - **移除被授予的那一条请求**，不能移除队头；
  - 保留任务冻结、取消清理与整体 `gate_supplier` 语义；
  - 被跳过次数超过防饿死上界（第一版 4 次）后强制让该请求等待，避免优先级反转变成长尾。
- 第一阶段不为 group 增加无限等待；`load_balance_wait_seconds` 不做。
- direct model route 保留现有 permit 等待/fallback 语义（`acquire_specific()` 会排队等）；只有 group route 使用“选择不到可用成员就向后 fallback”，避免改变现有单模型配置。

### 4.4 失败、组内预算与退避节拍

组内模型失败时要区分三类：

| 情况 | 组内动作 | 是否进入后续 route |
|---|---|---|
| 400/422 请求形状错误 | 当前请求内排除该成员，不消耗组预算 | 组内无候选后进入 |
| 429/5xx/timeout/连接错误等可 fallback provider 错误 | 消耗该成员的一轮 key pass；预算用尽后记本请求已试，换组内其他成员 | 组内全部耗尽后进入 |
| 配置/认证无法初始化、模型 disabled、无可用 key | 短期排除；写入按 config revision 失效的 cooldown | 立即按组内其他成员或后续 route |
| 内部运行时错误、数据库/状态错误 | 原样抛出 | 不隐藏为模型 fallback |
| 已经产生可见流式文本后失败 | 不能透明切换并拼接 | 按现有 contract 终止本次响应 |

**成员预算（§13 Q4）**：load-balance route 不继承成员 catalog 的 `retry_count`。

- group entry 提供 `max_retry_rounds`，默认 `1` 个完整 key pass，允许范围 `1..3`，超过上限直接配置校验失败；
- 成员的 catalog `retry_count` 只对 direct model route 继续生效；
- **不得复用现表达式**取默认：`main/runtime/chat_backend.py:857` 是 `normalized_retry_count(...) or DEFAULT_RETRYABLE_MODEL_ROUNDS`，而 `g3ku/providers/fallback.py:40-45` 注释确认“未配置”与“配 0”都落 10 轮。group 要的 `1` 必须写成显式成员级值，不能走 `or` 默认路径；
- 第一版只做 group 级统一预算，不做 per-member override。

**组内退避节拍**：换下一个成员前 `await model_retry_backoff_seconds(k)`，`k` = 本请求在本组内已消耗的 pass 数（1s 起、60s 封顶、±25% 抖动，`g3ku/providers/fallback.py:37-38,415-425`）。

- 现状是退避只发生在**同模型的轮之间**（`chat_backend.py:1068-1095`），跨模型前进（`1101-1128`）不插退避；
- 依据（14.3 实测）：6.6% 的跨模型回合扛 31.3% 的 provider 请求，均值 9.05 次/回合，今天的节拍全部来自退避。组预算从 10 轮收缩到 1..3 轮之后若不补节拍，这 1547 次请求会变成一次 pass 内背靠背打满 6 个成员，而对手是按分钟计的 RPM 窗口；
- 因此组内节拍是“把已有的节拍从模型维度挪到组维度”，不是新增机器护栏。

**cooldown 与惩罚**：429/超时的惩罚按**配额桶**累积并衰减，同桶成员共享；单次请求内的 `tried_model_refs` 永久排除已试成员。config revision 变化时重新评估 cooldown，避免修好配置后仍被旧状态挡住。

### 4.5 route chain 语义

示例：

```text
[LB(shared-A), direct(model-X), LB(shared-B), direct(model-Y)]
```

请求流程：

1. 准入时在 `shared-A` 内按 score 选最低负载成员并绑定该节点；
2. 该成员预算耗尽后，先释放旧成员 lease，再由同一个 node-turn lease 就地重绑 `shared-A` 内下一个未试成员（不得创建第二个 node-turn lease）；
3. `shared-A` 全部不可用/耗尽，才尝试 `model-X`；
4. `model-X` 耗尽后进入 `shared-B`；
5. 以此类推。

组内成员是平级的，`shared-A` 的成员配置顺序不代表优先级；route entry 的顺序才代表 fallback 优先级。`route_index` 由准入写入 lease 并随前进递增。

---

## 5. 运行时改造步骤

### 5.1 新增独立的 route/负载均衡模块

建议新增：

- `main/runtime/model_route.py`：运行时 route entry、resolved group、flattened candidate view 和 route snapshot 的轻量类型。
- `main/runtime/model_load_balancer.py`：全局 group state、配额桶 state、原子 selection、亲和账本、lease、cooldown/惩罚衰减、metrics 和 config refresh。

不要把 group 选择逻辑继续堆进 `chat_backend.py` 的 fallback 循环；`chat_backend.py` 负责请求生命周期和现有 retry contract，负载均衡器负责“该绑哪个成员”。

建议的核心接口：

```python
class ModelLoadBalancer:
    def configure(self, *, groups, config_revision) -> None: ...

    def select(
        self,
        *,
        node_id: str,
        route_index: int,
        group_key: str,
        preflight_filters: RouteCandidateFilters,
        excluded_model_keys: set[str],
    ) -> ModelRouteLease | None: ...

    def rebind(
        self,
        *,
        lease: ModelRouteLease,
        route_index: int,
        group_key: str | None,
        preflight_filters: RouteCandidateFilters,
        excluded_model_keys: set[str],
    ) -> ModelRouteLease | None: ...

    def release(self, lease: ModelRouteLease, *, outcome: str, error: str = "") -> None: ...

    def record_request_start(self, lease: ModelRouteLease) -> None: ...

    def record_outcome(self, lease: ModelRouteLease, *, status_code: int | None, error_text: str) -> None: ...

    def snapshot(self, *, group_key: str | None = None) -> dict[str, Any]: ...
```

约束：

- `select()`/`rebind()` 必须把 group selection、reservation 和底层 model/key permit 放在同一个同步临界区内；临界区内不得构建 provider、不得网络 I/O、不得 `await`。
- `release()` 要覆盖 provider build 在 permit 之后失败、async task 被取消、runtime shutdown、config refresh 四条路径，exactly-once。
- `record_request_start()` 是 60 秒滚动 RPM 的**唯一**上报点（实际发 provider 请求之前），避免准入层和 chat 层双计数。
- `record_outcome()` 只从结构化状态取码（`g3ku/providers/fallback.py:222-236` 的 `_error_status_code()`），RPM/TPM/RPS 维度归属尽力从文案识别、识别不到记 `unknown`；文案不得参与“是否可 fallback”的判定（`fallback.py:229-233` 已记录 sensenova 网关把 429 标成 `invalid_request_error`）。
- `ModelRouteLease` 字段：`group_key / model_ref / key_index / route_index / quota_bucket_index / score / running / waiting / reserved / rolling_rpm_60s / penalty / config_revision / permit`。
- 亲和账本 `node_id → lease` 由 balancer 持有，节点结束或重绑时释放；它不属于 `config.json`，是运行态。

### 5.2 扩展 model key controller

`main/runtime/model_key_concurrency.py` 的只读状态**已经够用**：`model_state()`（`:61-79`）已能按模型返回 `key_indexes / per_key_limits / running / waiting`。真正要补的只有四项：

- `reserved` 计数（与 `running` 分开，selection 与 permit 之间不留窗口）；
- `effective_capacity()` 聚合（仅在显式配了 `singleApiKeyMaxConcurrency` 时有意义，未配置返回“无限”，不得当成容量 1 参与 busy 判定）；
- **按当前 key 负载取 permit**：`try_acquire_first_available()`（`:81-94`）现在是“按 key_indexes 固定顺序取第一个有容量的 key”，同模型内也有固定顺序偏差，key 轴要和 model 轴一样按负载选；
- 让 balancer 能把“选成员”和“取该成员某把 key 的 permit”绑成一次原子操作。

保持现有 `ModelKeyPermitLease` 的释放兼容，旧 direct model path 不受影响。锁只保护计数和 lease 状态。

### 5.3 改造 `ConfigChatBackend`

`main/runtime/chat_backend.py` 的改造顺序：

1. `chat()` 新增 `model_routes`（route plan snapshot）参数，过渡期继续接受旧 `model_refs`。
2. `_resolved_model_refs()` 改为 `_resolved_model_routes()`；runtime config revision 变化时重新取 route snapshot。
3. 外层 `model_index` 改为 `route_index`。
4. **第一次 provider attempt 必须消费准入阶段已取得的 permit**：现有 `use_held_turn_permit` 判定在 `:889-895`，要求 ref + key index + `attempt_number == 1` 同时命中。因此准入选定成员后，本轮 key 顺序必须**旋转**成以该 `key_index` 开头（`g3ku/utils/api_keys.py:179-181` 的 `iter_api_key_retry_slots()` 接受自定义 `key_indexes`，天然支持），否则第一次 attempt 会二次 acquire 造成双 permit。
5. direct model route 走现有模型 retry/key rotation 逻辑，行为不变。
6. load-balance route 不再自行 acquire：成员与 permit 来自 lease；组内前进时调用 `balancer.rebind()` 复用同一个 node-turn lease，并先释放旧成员 lease。
7. 组内预算取 group entry 的 `max_retry_rounds`（4.4），不走 `:857` 的 `or DEFAULT_RETRYABLE_MODEL_ROUNDS` 默认路径。
8. 换成员前按 4.4 插入退避节拍；组内候选全部结束后才递增 `route_index`。
9. group 内维护 `tried_model_refs`，一个请求不能在同一个 group 内重复选择已耗尽成员。
10. 保留 `model_refs_resolver` 的热刷新语义，但刷新对象改为 route plan；已在飞的 attempt 不换成员，下一次 attempt/边界使用新 snapshot。链在重试途中被改写的重启路径（`:1041-1066`）必须同样适用于 route plan。
11. retry status 的 `model_refs` 字段改为同时提供 `route_entries`、`attempted_model_keys`、`selected_group_key`、`quota_bucket_index`；旧 `model_refs` 暂时继续发扁平候选列表以兼容旧 UI。

`g3ku/providers/fallback.py` 的 `FallbackProvider` 第一阶段不承担 execution/inspection 的组选择；后续若让 CEO/memory 接入，应调用同一个 route engine，而不是复制算法。

### 5.4 改造准入层、NodeRunner 与 ReAct loop

涉及：

- `main/runtime/node_turn_controller.py`
- `main/runtime/node_runner.py`
- `main/runtime/react_loop.py`

改造点：

1. **`acquire_turn()` 改为接收 route plan**（或 route resolver），不再只接收展开后的链首 `model_ref`；调用点 `react_loop.py:749` 的 `primary_model_ref = str(current_model_refs[0] ...)` 是要消灭的那一行。
2. `NodeTurnLease` 新增 `route_index`、`group_key`，并把 `model_ref/key_index/initial_model_permit` 改为来源于 balancer 选择。
3. `_pump()` 按 4.3 改成有界扫描 + 精确移除被授予的请求。
4. `_model_refs_for()` 改为 `_model_routes_for()`。
5. `_runtime_context()` 仍可输出 `model_refs`，但该字段改为“所有 route 候选的去重展开列表”，并新增 `model_routes`、`model_route_mode`、`load_balance_group_keys`。
6. execution/inspection/spawn review/distribution decision 使用同一个 role route resolver，任何控制回合不得偷偷回落到“第一个候选”。
7. `react_loop` 的 `current_model_refs` 改为 route snapshot，`model_refs_supplier` 改为 route supplier。
8. `_execution_prompt_cache_key()`（`react_loop.py:3127-3133`，现把候选数组当模型身份）改为：route signature + **最终 selected model**。缓存诊断与实际请求归因按 selected model 生成，避免不同成员互相污染取证。
9. `image_multimodal_enabled`、context window、request timeout、reasoning/output defaults 等逻辑不能只看 route 第一个字符串，统一走候选解析层（5.5）。

### 5.5 preflight/context window/multimodal 兼容策略

负载均衡让“本次用哪个成员”在准入时才定，现有只读 `model_refs[0]` 的逻辑不再可靠：`chat_backend.py:108` 与 `:187`、`react_loop.py:5546` 都取链首。

推荐采用以下顺序：

1. 先根据请求特征得到候选过滤条件：context window 能容纳当前估算、是否需要图像多模态、provider protocol 是否支持当前 request shape。
2. 只在符合条件的 group 成员中做负载选择。
3. 如果没有任何成员符合条件，**先按 preflight 失败处理（交给压缩/收口）**，而不是按 busy fallback 到下一个 route entry——否则会把“装不下”误判成“忙”。
4. preflight 需要发送前定值时，使用候选集合的保守值（context window 取可用候选的最小值），并把最终 selected model 写入 actual-request diagnostics。实测代价为零：现网 14 条 catalog 的 `contextWindowTokens` 全为 390000，取最小值与取链首同值。
5. 绑定成员后，若该成员的实际窗口小于保守值，按现有 contract 拒绝该候选并重绑，不得中途放大。
6. 压缩/重试重建请求时沿用同一 route contract；压缩 helper 不得直接取 group 第一个成员。

这是模型路由改造中最容易漏掉的隐性耦合点，必须单独测试。

### 5.6 RuntimeService 注入与热刷新

在 `main/service/runtime_service.py`：

1. 创建与 `model_key_concurrency_controller`（`:571-577`）同生命周期的 `ModelLoadBalancer`，并把亲和账本挂在这里。
2. 注入 `NodeTurnController`、`ConfigChatBackend`、`ReActToolLoop`、`NodeRunner` 的调用链。
3. **进程边界必须写进日志与文档**：controller/balancer 只在 `execution_mode == 'worker'` 时创建（`:548`），而 `G3KU_TASK_RUNTIME_ROLE` 默认是 `embedded`（`g3ku/runtime/bootstrap_bridge.py:95`），web 进程设 `web`（`g3ku/web/main.py:29`）、worker 子进程设 `worker`（`g3ku/web/launcher.py:314`、`worker_control.py:147`）。balancer 在 controller 缺失时必须退化为 no-op（单成员直选、不预占），不能让 embedded/web 路径因为没有 balancer 而报错。
4. `ensure_runtime_config_current()` 刷新成功后调用 `balancer.configure()` 更新 group 定义、配额桶与 config revision；同处要修 `:5514`：inspection 缺省时继承**完整 execution route plan**，不是继承扁平字符串列表。
5. 对未变化的 group 保留 running/reserved/cooldown/亲和状态；成员变化的 group 只在没有 active lease 时清理旧成员，active lease 允许正常 release。
6. 配置刷新不得中断已在 provider 中的请求；新 route 只在下一次 model boundary 生效。
7. shutdown 时先阻止新 selection，再释放所有 lease 与亲和记录，确认 reservation 归零，避免重启后把旧负载算在某个模型上。

---

## 6. 配置保存与管理 API 改造

### 6.1 后端数据返回

扩展 `GET /api/models` 和相关模型配置 payload：

```json
{
  "catalog": [
    {"key": "openai_a", "enabled": true, "quota_pool_key": null}
  ],
  "roles": {
    "execution": ["openai_a", "openai_b"]
  },
  "routes": {
    "execution": [
      {"type": "load_balance", "group_key": "shared_exec"},
      {"type": "model", "model_key": "emergency"}
    ]
  },
  "load_balance_groups": {
    "shared_exec": {
      "enabled": true,
      "max_retry_rounds": 1,
      "model_keys": ["openai_a", "openai_b"]
    }
  },
  "load_balance_diagnostics": {
    "bucket_count": 2,
    "duplicate_bucket_count": 1,
    "unresolved_bucket_count": 0
  }
}
```

`roles` 保留扁平展开结果供旧客户端使用；新的模型链编辑器使用 `routes` 和 `load_balance_groups`。返回值必须明确：`roles.execution` 是候选展开视图，不再代表 fallback 顺序。

`load_balance_diagnostics` 只输出桶的数量与成员归属序号，**不输出 fingerprint、endpoint 或密钥**；`unresolved_bucket_count` 表示密钥在该进程不可解析（4.1 实测：web/worker 之外解析 6 个 execution ref 全部 `keys=0`），该值非 0 时前端必须显示“配额分布未知”，不得显示“无重复”。

### 6.2 保存接口

扩展现有：

- `PUT /api/models/roles/{scope}`（`main/api/admin_rest.py:1307`）
- `PUT /api/models/routes/batch`（`main/api/admin_rest.py:1293`）

新增字段（同时接受 camelCase）：

```json
{
  "route_entries": [
    {"type": "load_balance", "group_key": "shared_exec"},
    {"type": "model", "model_key": "emergency"}
  ],
  "load_balance_groups": {
    "shared_exec": {
      "enabled": true,
      "max_retry_rounds": 1,
      "model_keys": ["openai_a", "openai_b"]
    }
  }
}
```

保存必须是原子操作：route entry、group 定义和 `quota_pool_key` 要一起校验、一起写入、一起触发 runtime refresh。不能先保存 group 再保存 route，导致中间状态让 worker 读到悬空引用。

路由注册顺序是硬约束：`PUT /models/{model_key:path}`（`admin_rest.py:1328`）会把后注册的 `/models/...` 整段吞掉。任何新增的 PUT（例如组定义批量应用）必须注册在它**之前**，现有 `routes/batch` 与 `roles/{scope}` 就是靠顺序规避的。

`g3ku/config/loader.py` 的 `_runtime_config_payload()`（`:480-560`）是手写白名单，`load_balance_groups`、`quota_pool_key` 和 `models.roles` 的 route 形态必须逐字段加进去，否则 worker 侧读不到——现网 `_managed_models_payload()`（`:451-477`）就只序列化扁平 `roles`。

`g3ku/config/model_manager.py` 需要：

- 扩展 `_prepare_scope_route_update()`（`:310-374`）支持 route entries 规范化与 3.4 的双严格度校验（legacy 静默去重、显式 route 报错）；
- 增加 group 的 create/update/delete 或 batch apply，并校验 `max_retry_rounds` 范围；
- 删除/禁用模型时检查 group 成员和 route 引用，给出“仍被哪些 role/group 使用”的可读错误；
- 保持现有 `max_iterations` / `max_concurrency` 更新行为。

### 6.3 前端交互

涉及：

- `g3ku/web/frontend/api_client.js`
- `g3ku/web/frontend/org_graph_llm.js`
- `g3ku/web/frontend/org_graph_app.js`

推荐 UI 结构：

1. 左侧仍然显示可用模型 catalog。
2. 右侧链编辑器显示有序 route entries：
   - direct model：单行模型卡片；
   - load balance group：一个组卡片，组内显示成员。
3. 提供“新增负载均衡组”操作：设置 group key、勾选多个模型、设置 `max_retry_rounds`（1..3）。
4. 组内成员允许增删，但不提供“优先级排序”语义；界面明确显示“组内模型平级，运行时按综合负载选择，节点绑定后粘滞”。
5. 组卡片外部可拖拽排序，外部顺序表示 fallback 顺序。
6. 保存前显示预览：
   - route 顺序；
   - 每个 group 的成员与 `max_retry_rounds`；
   - 同一个 group 是否被 execution/inspection 共享；
   - 配额桶合并结果（只显示桶数与成员归属序号，以及“配额分布未知”的计数）。
7. 对旧 flat chain 做无损展示；用户不点击“转换为 group”时，继续使用现有行为。
8. 保存失败时显示后端校验错误，不能悄悄把 group 降级成第一个模型。
9. 任何把 role 显示成“当前模型：链首”的地方（含 `g3ku/cli/commands.py:792-794`）改为显示 route/group/候选，不得把链首标成实际执行模型（§13 Q6）。

第一阶段可不在模型配置页展示实时 running/waiting/RPM，但后端必须提供调试快照（7.3）或日志字段；实时状态面板作为后续小迭代。

---

## 7. 可观测性与运维诊断

### 7.1 每次选择至少记录这些字段

建议在现有 model chain trace/attempt diagnostics 中增加结构化字段：

- `route_kind`: `model` / `load_balance`
- `group_key`
- `route_index`
- `selected_model_key`
- `candidate_model_keys`
- `selection_reason`: `least_load` / `sticky_reuse` / `sticky_rebind` / `capacity_available` / `fallback_after_failure` / `busy_fallback`
- `sticky_rebind_reason`: `stage_boundary` / `filter_changed` / `cooldown` / `penalty_threshold` / `plan_changed` / `node_finished`
- `running_before`
- `waiting_before`
- `reserved_before`
- `local_capacity`（未显式配置时写 `unlimited`，不要写 1）
- `rolling_rpm_60s`
- `penalty_429_before` / `penalty_429_after` / `throttle_dimension`: `rpm` / `tpm` / `rps` / `token` / `unknown`
- `quota_bucket_index` / `bucket_member_count`
- `score` / 各分项（local、rolling rpm、penalty）
- `usage_ratio`
- `max_retry_rounds` / `group_passes_used`
- `config_revision`
- `task_id`、`node_id`、`actor_role`

不要只打印 provider model 名称；必须打绑定 key。现网实测：6 个 execution ref 只对应 2 个 provider model（`deepseek-flash` ×3、`sensenova-6.8-flash-lite` ×3）且同一个 endpoint，按 provider model 归因会把 3 个 binding 混成 1 个。桶只输出序号与成员数，不输出 fingerprint。

### 7.2 失败日志

组内 fallback 应能回答以下问题：

1. 为什么没有选最常用的模型？
2. 选中成员时它的 running/waiting/reserved/rolling RPM/惩罚是多少？
3. 是因为 busy、disabled、cooldown、配置错误还是 provider error 跳过？
4. 下一次进入的是同组其他成员，还是链上的下一个 route？
5. 最终失败时，哪些 route entry 和哪些候选已经耗尽？
6. 这个节点这次生命周期内换过几次绑定成员、每次为什么换（`sticky_rebind_reason`）？

建议增加统一日志锚点：

- `Model route selected`
- `Model load-balance candidate skipped`
- `Model load-balance group exhausted`
- `Model route fallback`
- `Model route lease released`
- `Model node binding rebound`

### 7.3 运维状态接口（可选但推荐）

增加只读管理员接口，例如：

```text
GET /api/models/load-balance/status?group_key=shared_exec
```

返回每个成员的：

- running / waiting / reserved
- 配额桶归属序号与桶内成员数（`unresolved` 单独计数）
- rolling 60 秒请求数、429 计数按维度、惩罚衰减值与半衰期
- local capacity（或 `unlimited`）
- current load score / usage ratio
- cooldown until/reason
- last selected at、当前绑定到该成员的节点列表
- current config revision

该接口不能暴露 API key、endpoint fingerprint 或 provider secret。若暂时不做接口，至少保证 worker 日志可通过 group key 和 model key 复原选择过程。

---

## 8. 测试计划

### 8.1 配置与迁移测试

新增或扩展 `tests/` 中的配置测试：

1. 旧 `list[str]` role chain 能加载、运行和原样保存。
2. route object 能用 camelCase 和 snake_case 加载。
3. group 缺失、空 group、未知模型、disabled 模型、未知 group key 被拒绝。
4. **严格度分叉**：显式 `route_entries` 中的重复 direct model、重复 group 引用、组内重复成员各自报错；legacy 扁平链仍走现有静默去重（对照 `schema.py:353-365`、`model_manager.py:321-326`），两者不得互相污染。
5. 同时传 legacy chain 与显式 route 时以显式 route 为准并走严格校验。
6. `max_retry_rounds` 取 0、4、非整数时校验失败；取 1..3 通过；不得夹断。
7. `quota_pool_key` 与自动 fingerprint 都能把两个成员归到同一桶；空 key/空 endpoint 参与 fingerprint 时**必须产出 `unresolved`，不得互并**（用假空值构造回归）。
8. execution/inspection 可以引用同一个 group；运行时解析得到同一 `group_key`。
9. ceo/memory 第一阶段配置 group 时返回明确错误。
10. 删除/禁用仍被 group 或 route 引用的模型给出可操作错误。
11. 保存 group + route + quota 字段是原子的，失败时旧配置不被部分覆盖。
12. runtime payload 能透传 `load_balance_groups`、route 形态与 `quota_pool_key`（`loader.py:480-560` 白名单漏字段要测到）；旧安装没有 group 字段时默认值通过。

### 8.2 负载均衡器单元测试

新增 `tests/resources/test_model_load_balancer.py`（或同等命名）：

1. 两个等容量模型在并发**节点**下不会固定命中第一个。
2. 3 个等容量模型、N 个新节点的绑定分布接近均匀；偏差阈值写成显式常量。注意：粒度是节点绑定，不是单请求，测试要按“N 个节点各自首次准入”构造。
3. 同一配额桶的两个成员共享 RPM 计数与 429 惩罚：桶内一个成员被打爆后，另一成员不得被当作空闲容量优先选中。
4. 不同显式 `singleApiKeyMaxConcurrency` 的成员按归一化 score 选择，而不是只比 raw running。
5. **现网态专项**：`per_key_limit` 全为 `None` 时 `waiting` 恒 0、`busy` 不成立，选择必须由 rolling RPM 与 429 惩罚区分；构造全 None 场景断言不会误判 busy。
6. 相同 score 的 tie-breaker 不永远固定为配置第一项，也不用 `random.choice()`。
7. 原子 reservation 能防止并发协程同时把同一最低负载成员选满；锁内不做 provider 构建与 `await`。
8. lease 在成功、异常、取消、provider build 失败、preflight 失败、shutdown 六条路径上 exactly-once release。
9. busy 的 group 按 chain fallback，不死等；`_pump()` 有界扫描跳过不可用请求时**移除的是被授予的那一条**，且被跳过 4 次后进入等待（防饿死）。
10. 同一个 group 被 execution 和 inspection、多个 task 同时使用时共享状态。
11. 亲和账本：同一节点连续多回合复用同一成员（不重选、不换缓存命名空间）；阶段边界、过滤条件变化、绑定成员 cooldown、plan/config revision 变化四种触发才重绑；节点结束释放。
12. config revision 更新后新成员可见，旧 active lease 可正常释放。
13. cooldown/429 惩罚按衰减时间在预期内恢复；配置修复后不被旧 cooldown 永久阻塞；惩罚不永久累积。
14. 模型 key 轴也按负载取 permit（`try_acquire_first_available()` 现有固定 key 顺序偏差不回归）。

### 8.3 fallback 与 provider 行为测试

扩展 `tests/resources/test_chat_backend_observability.py`、`test_model_queue_runtime.py`、重试相关测试：

1. group 首选失败后选择同组其他成员，不按配置数组顺序走。
2. 同组全部失败后进入链上的下一个 direct model/group。
3. **组内退避节拍**：成员之间插入 `model_retry_backoff_seconds(k)`，断言 6 成员组在 429 风暴下不会背靠背打完 6 个成员（防止退化成 burst）。
4. **预算来源**：成员的 catalog `retry_count=9999999` 对 group route 不生效；group route 用 `max_retry_rounds`，且不走 `chat_backend.py:857` 的 `or DEFAULT_RETRYABLE_MODEL_ROUNDS` 默认；同成员在 direct route 上仍按 catalog `retry_count` 行为不回归。
5. **第一次 attempt 消费准入 permit**：断言不产生第二颗 permit；并覆盖“准入选中的 key_index 不是 key 顺序第一个”时 key 序列被旋转（`api_keys.py:179-181`）。
6. 400/422 不在同一成员上无意义轮 key，能按约定进入候选/链 fallback。
7. 内部 runtime error 不被吞掉，不伪装成 exhausted model chain。
8. 有可见流式文本后不切换成员拼接结果。
9. runtime config 在退避或 model boundary 变化时，下一次绑定使用新 route，但飞行中请求不被中断；链在重试途中被改写的重启路径（`chat_backend.py:1041-1066`）对 route plan 同样成立。
10. retry status、attempts、actual request diagnostics 记录实际 selected model 与 quota bucket，而不是只记 group 第一个成员。

### 8.4 节点与上下文测试

1. execution node 和 inspection node 使用正确的 route plan。
2. spawn review、distribution decision 不意外退回旧的第一模型。
3. context window 过滤会排除不适配当前请求的 group 成员；**没有任何成员适配时按 preflight 失败处理，不得走 busy fallback**。
4. multimodal 请求不会选择不支持图像的模型。
5. prompt cache key 与 actual request artifact、token usage ledger 按最终 selected model 归因；`react_loop.py:3127-3133` 现把候选数组当模型身份的行为要改掉。
6. **缓存命名空间不抖动**：一个节点在其生命周期内重绑次数与 `sticky_rebind_reason` 可被断言，正常情况下长节点只绑 1 个成员（对齐现网 99.8% 的观测）。
7. 同一任务多个节点并发时，分布由全局 group state 决定，不是每个节点各自从零开始。
8. inspection 缺省时继承完整 execution route plan（含 group entry），而不是继承扁平字符串列表（`runtime_service.py:5514`）。
9. `execution_mode` 为 `embedded`/`web`（无 controller）时 balancer 退化为 no-op，节点仍能正常跑，不抛错。
10. 完成 smoke subset：

```text
python -m pytest tests/resources/test_resource_runtime_smoke.py -q
```

### 8.5 API 与前端测试

1. role route API 兼容旧 `model_keys` payload。
2. 新 `route_entries` + `load_balance_groups`（含 `max_retry_rounds`）保存和读取一致。
3. batch save 失败时不产生部分更新；新增 PUT 路由必须注册在 `admin_rest.py:1328` 的 `{model_key:path}` 之前，加一条“路由顺序”回归。
4. 前端 group 卡片、外部 fallback 顺序、组内平级提示、粘滞说明、旧 flat chain 兼容渲染通过。
5. 配额诊断只输出桶数/归属序号；断言响应体与前端渲染均不含 fingerprint、endpoint 或 key；`unresolved` 非 0 时显示“配额分布未知”而不是“无重复”。
6. API client 对 camelCase/snake_case 响应都能处理。
7. 删除/禁用一个 group 成员的错误提示可读。

### 8.6 质量门禁

实现完成后按仓库要求运行：

```text
python -m ruff check .
python -m pytest tests/resources/test_resource_runtime_smoke.py -q
python -m pytest <模型配置、模型队列、chat backend、node runtime 相关测试> -q
```

若增加前端测试，执行仓库当前约定的 JavaScript 测试命令；最终结果中列出实际执行的命令和结果。

---

## 9. 分阶段实施顺序

### Phase 0：基线与契约冻结

- 把本文 14 节的实测基线固化成可断言的常量（429 归因占比、跨模型回合 6.6% 扛 31.3% 请求、长节点单模型粘滞、每跳 cache_hit 中位 32,768 token）。
- 补充当前 flat chain 的行为测试：准入按链首预占 permit、退避只在同模型轮之间、`retry_count=0 → 10 轮`。
- 明确 `group_key`、route entry JSON、busy 判据、配额桶口径。
- 先不改前端，避免 schema 和 UI 同时漂移。

**完成标准**：有一组能证明旧 ordered fallback 与旧准入行为的回归测试；配置样例已由 §13 裁定确认。

### Phase 1：配置 schema、迁移和管理后端

- 增加 route entry/group schema（含 `max_retry_rounds`、`quota_pool_key`）与双严格度校验。
- 兼容旧字符串链。
- 更新 loader（含 `_runtime_config_payload` 白名单）、runtime payload、ModelManager、LLM facade、admin API（注意路由注册顺序）。
- 暂时让 runtime 仍把 route plan 展开成旧 refs，保证配置链路先闭环。

**完成标准**：新配置可保存/读取，旧配置无 diff 迁移，管理 API 能返回 route/group/诊断桶计数，全部配置测试通过。

### Phase 2：全局负载均衡核心

- 新增 `ModelLoadBalancer`、`ModelRouteLease` 与亲和账本。
- 实现配额桶 fingerprint（含 `unresolved` 保护）与共享惩罚。
- 扩展 model key controller：`reserved`、按负载取 key permit、容量聚合。
- 实现 60 秒滚动 RPM 与 429 衰减惩罚，权重常量集中一处。
- 编写并发、fairness、release、cooldown、亲和测试；先用最小 fake provider 验证分布，不接真实 provider。

**完成标准**：相同容量 3 成员组上，N 个并发节点的**首次绑定**分布达到预设阈值；同桶成员不被当作两份容量；没有 lease 泄漏；无逐请求重绑。

### Phase 3：execution/inspection runtime 接入（含准入层）

- `NodeTurnController.acquire_turn()` 改接 route plan，`_pump()` 改为有界扫描 + 精确移除 + balancer 原子选择（§13 Q1 的落点）。
- NodeRunner/ReAct loop 改为 route plan；消灭 `react_loop.py:749` 的链首取法和 `:3127` 的候选数组当身份。
- ConfigChatBackend 消费准入 permit（含 key 顺序旋转）、组内预算与成员间退避节拍。
- RuntimeService 创建并热刷新共享 balancer；`embedded/web` 无 controller 时退化 no-op；inspection 缺省继承完整 route plan。
- 更新 preflight/context window/multimodal/timeout/cache/actual-request diagnostics，以及 `cli/commands.py:792-794` 的显示。

**完成标准**：多个任务同时产生 execution/inspection 节点时共享同一 group 负载与配额桶；失败、重试、取消、热刷新无回归；长节点每生命周期重绑次数为 0（除触发条件），`cache_hit_tokens` 不因接入而下滑。

### Phase 4：前端模型链编辑器

- API client 改造。
- role chain UI 增加 group 容器、`max_retry_rounds` 输入、粘滞与配额桶计数说明。
- 保留旧 flat chain 编辑体验。
- 增加解释性文案和保存前预览。

**完成标准**：用户可以不编辑配置文件，直接创建 group、选择成员、设置 fallback 顺序并保存；刷新页面后结构不丢失。

### Phase 5：观测、压测和灰度

- 增加 route selection/fallback/binding 日志和 status snapshot。
- 用 fake provider 做 10/50/100 节点并发压测。
- 再用可控的 429/timeout/disabled/capacity-full 场景验证 fallback。
- feature flag 落点必须是配置字段并进 `_runtime_config_payload` 白名单（不是环境变量），字段名 `models.loadBalanceGroups.<key>.enabled` 已足够表达开关，另加 `mainRuntime.modelRouteLoadBalance.enabled` 作为 execution/inspection 各自可关的总闸；默认只对显式包含 group 的 route 开启。

**完成标准**：均匀性、fallback 正确性、吞吐、错误可诊断性达到验收指标，且可以一键回退到旧 flat chain。

---

## 10. 回滚与兼容策略

1. 旧字符串链永远保留解析能力。
2. feature flag 关闭时，route plan 中的 direct model entry 继续走原 ordered chain；含 group 的配置在回滚路径上按“展开候选并保持配置顺序”走**旧准入行为**（即按链首预占 permit），不留半新半旧的准入态。
3. runtime 只在下一次 provider attempt 边界使用新配置，不强杀飞行中的请求。
4. 每次 route selection 都记录 config revision，出现异常时可以判断是新配置还是旧请求。
5. 不改变 `models.catalog[].key`，不按 provider model 名称重新生成 key，避免 token ledger、绑定和会话固定模型失效。
6. 不把 running/cooldown/滚动 RPM/惩罚/亲和绑定持久化进 `config.json`；它们是运行态。
7. 回滚必须能单独关掉准入层选择（保留 chat 层 fallback），否则第一版任何准入改动出错都只能整块回退。
8. 如果将来需要多 worker 全局均衡，新增共享 lease 层时必须保持本地 `ModelLoadBalancer` 接口不变，避免再次改动 NodeRunner/ChatBackend/NodeTurnController 的调用契约。

---

## 11. 实现完成后必须更新的架构文档

这次功能会改变配置结构、runtime 路由和运维诊断，因此正式实现时必须调用并遵循 `skills/g3ku-architecture-maintenance/SKILL.md`，至少更新：

1. `docs/architecture/config-and-models.md`
   - 新的 route/group 配置来源；
   - legacy flat chain 迁移；
   - group key 与模型 key 的解析关系；
   - role chain 与 group 的 fallback 语义。
2. `docs/architecture/runtime-overview.md`
   - NodeRunner → ReAct loop → **NodeTurnController 准入选择/绑定** → ChatBackend → global balancer → provider 的新路径；
   - 节点级粘滞绑定的生命周期与重绑触发条件；
   - 跨任务共享的运行态（含配额桶与亲和账本，均为进程内存态）；
   - lease、busy fallback、组内退避节拍、失败分类和 config refresh 行为；
   - `execution_mode` 为 `embedded`/`web` 时 balancer 退化为 no-op 的边界。
3. `docs/architecture/web-and-admin.md`
   - route/group API contract；
   - 模型链编辑器中外部顺序与组内平级语义；
   - 管理员诊断接口（若实现）。
4. `docs/architecture/context-and-cache-troubleshooting.md`
   - group-aware preflight、最终 selected model、prompt cache/actual-request 取证规则。
5. `docs/architecture/operations-and-maintenance.md`
   - 如何查看 group 负载、区分 busy fallback 与 provider failure、如何回滚到 flat chain。
6. `docs/architecture/README.md`
   - 只有在新增独立架构主题文档或改变阅读导航时更新；如果仍由上述现有文档承载，则不需要无意义地改目录。

正式代码实现完成后，最终摘要必须明确列出实际更新的架构文档；若某项没有更新，要说明原因。

---

## 12. 验收场景

### 场景 A：同容量三成员在节点维度摊开

- `shared_exec` 包含 A/B/C，三个成员都可用、容量相同。
- 同时启动多个任务，生成大量 execution 和 inspection 节点。
- 观察一个滑动窗口内**每个成员绑定的节点数**（`running + reserved`），不能持续固定绑在 A；稳定后节点级分布应接近 1/3。
- 附加约束：单个节点在其生命周期内不因负载重选而换成员（见场景 G），否则均摊达标但缓存代价爆炸。

### 场景 B：某成员暂时不可用

- A 返回 429 或 timeout，B/C 正常。
- 新请求先在组内跳过 cooldown/惩罚超阈的 A，在 B/C 中选择综合 score 更低者。
- A/B/C 都耗尽后，进入链上的 direct fallback D。
- 组内换成员之间必须观察到退避间隔（不是背靠背 3 连发）。

### 场景 C：组容量全部占满

- **前置条件**：成员必须显式配置 `singleApiKeyMaxConcurrency`。现网实测 14 条 catalog 全为 `None`，本地永不排队，因此该场景在本机不可自然复现，只能在 fake provider + 显式限额的测试里跑（8.2.5、8.2.9）。
- group 内所有成员达到 key concurrency 上限。
- 请求不无限阻塞在组内，而是按约定进入后续 fallback route。
- 后续 route 成功后，原 group lease/counter 不得泄漏。

### 场景 D：execution 与 inspection 共享池

- execution 和 inspection 都引用 `shared_exec_inspect`。
- execution 正在大量使用 A 后，新 inspection 请求应优先选择 B/C 中综合 score 更低者。
- 两条车道不能各自把 A 当作“本车道的第一模型”。
- 现网对照：execution 与 inspection 当前是同一批 6 个 key、顺序正好相反，本场景达标即吸收掉这个手工反向技巧。

### 场景 E：配置热刷新

- 运行中把 group 增加模型 D 或禁用模型 B。
- 已在飞的请求完成后释放旧 lease；下一次绑定选择看到新成员/禁用状态。
- retry boundary 不因配置刷新产生重复请求或死循环。
- 刷新后 cooldown/惩罚按 config revision 重估，不出现修好配置仍被旧状态挡住。

### 场景 F：回滚

- 将 group route 展开为旧 flat `model_keys` 或关闭 feature flag。
- 新请求恢复现有 ordered fallback **与旧的链首预占准入行为**，不留半新半旧准入态。
- 旧任务不丢失，不修改模型 binding key 和 token 记录。

### 场景 G：缓存命名空间不抖动（接入负载均衡的硬约束）

- 构造一个 ≥16 回合的长节点，跑完整生命周期。
- 断言其 `delta_usage_by_model` 只出现 1 个 model_key（除非命中重绑条件），且每跳 `cache_hit_tokens` 中位数不低于接入前基线（现网长节点 32,768 token/跳、命中率 68.9%）。
- 人为触发一次 cooldown 重绑后，必须能在日志里看到 `sticky_rebind_reason=cooldown`，且该跳之后缓存命中率恢复。
- 这条场景的目的是把“逐回合重选”明确挡在实现外：换成员等于换前缀缓存命名空间，断点之后的整段存活上下文都要重传。

---

## 结论

推荐把这项需求实现为：

> **有序 route chain 负责 fallback，中央命名的 load-balance group 负责链内平级选择，共享的 runtime balancer 负责跨任务/跨节点的实时负载与上游限流观测，准入层负责把节点原子地绑到某个成员上。**

关键成功点不是简单把模型列表 `shuffle()`，而是三件事同时成立：

1. “选择成员”和“取得并发 permit”合并成准入层的一次原子操作，并且第一次 provider attempt 消费的就是这颗 permit；
2. 负载口径覆盖上游观测（60 秒滚动请求数 + 衰减 429 惩罚），并按配额桶而不是按 binding 聚合；
3. 绑定粒度是节点，使均衡效果不再依赖链首顺序，也不去抖掉已经拿到的前缀缓存命中。

这样才能在大量节点同时请求模型时真正避免固定顺序热点，同时保留现有 retry、API key 轮换、错误分类、上下文 preflight 和可回滚能力。

---

## 13. 评审裁定记录（2026-09-26）

以下裁定覆盖实现阶段的歧义点，后续实现不再按旧计划中的模糊表述自行猜测。

### Q1：负载均衡选择层 —— 采用 A，选择上移到准入层

模型选择必须发生在 `NodeTurnController` 的准入/排队阶段，而不是只放在 `chat_backend` 的 provider attempt 循环里。

具体约束：

- `acquire_turn()` 接收 route plan 或等价的 route resolver，不再只接收展开后的链首 `model_ref`。
- `NodeTurnController._pump()` 在同一个原子操作中向全局 balancer 请求候选成员并拿到该成员/API key 的 permit，然后创建带有 `route_index / group_key / model_ref / key_index` 的 `NodeTurnLease`。
- `chat_backend` 第一次 provider attempt 必须消费这颗已取得的 permit，禁止再次为同一个模型/key acquire，避免双 permit 和链首虚高。
- 同一次 `chat()` 内发生模型 fallback 时，先释放旧成员 lease，再由同一个 node-turn lease 重新绑定下一个候选；不得创建第二个独立 node-turn lease。
- 所有异常、取消、provider build 失败、preflight 失败和 shutdown 路径都必须 exactly-once release。
- `_pump()` 不能继续“队头模型没有容量就 break”。它需要对未冻结请求做有界扫描/轮转，跳过当前不可用的队头请求，授予后续可用 route；同时保留任务冻结、取消清理和整体 gate 语义。
- 组内选择、reserved 预占、底层 model/key permit 必须原子完成；锁内不能执行 provider 构建、网络 I/O 或 `await`。

因此不采用 B，也不接受 C 的链首虚高行为。

### Q2：负载口径 —— 采用 B；本地在飞数只作为基础项

不要把本地 `running` 数作为唯一负载。第一版的选择分数必须同时考虑本地状态和上游观测：

```text
score = local_inflight_component
      + rolling_request_rate_component
      + recent_429_penalty
      + cooldown_penalty
```

至少维护以下内存态指标：

- 以 API key/配额身份为粒度的最近 60 秒请求启动数（RPM 代理）；
- 429 总数及按原因分类的近期计数；
- 429 penalty 的衰减时间；
- 当前 running/reserved/waiting；
- 成功、失败、超时和 cooldown 的时间戳。

选择规则：

- cooldown 中的候选直接跳过；
- 其余候选按归一化综合 score 选择，而不是只按 raw running；
- 429 penalty 使用衰减值，不能永久污染模型；
- provider 返回明确的 RPM/TPM/RPS 限流信息时，记录限流维度，至少先把 RPM 作为第一版的主要观测维度；
- `singleApiKeyMaxConcurrency` 仍作为可选的本地并发容量约束，但不能作为启用负载均衡的前提；`None` 不再意味着该模型在上游速率层面无限容量。

暂不采用 C 作为前置条件。显式 per-key 限额可以作为后续容量提示或配置覆盖，但不能替代真实的滚动请求速率和 429 反馈。

### Q3：组粒度与多 key binding 的关系 —— 保留两层模型，不砍 Phase 2/3

负载均衡组和一个 binding 内的多 API key 解决的是两个不同轴：

- **binding 内多 key**：同一个 binding 内的 key 轮换、单 key 并发和该 binding 的请求容灾；
- **load-balance group**：多个 binding/上游模型/协议之间的平级选择，以及 execution 与 inspection 共享一个全局池。

因此即使确认 6 个 binding 实际使用的是 6 把不同 key，也不能取消 group；如果确认它们是同一把 key 的复制，也不能简单把 Phase 2/3 整块删除。

运行时必须引入“路由成员”和“配额身份”两层：

- 路由成员仍然是 binding/model key，因为它决定 provider、模型参数、协议、context window、multimodal 能力和 retry policy；
- 负载与 429 统计至少按 `quota_identity + api_key_slot` 聚合，而不能盲目把每个 binding 当成独立配额；
- 对运行时解析到的完全相同 endpoint + API key，使用仅存在于内存的稳定 fingerprint 做重复配额桶合并；日志和 API 不得输出 fingerprint 或密钥；
- 对“不同 key 但同一上游账户共享 RPM”的情况，增加可选的 operator-facing `quota_pool_key`/等价字段，不能凭 provider 名称猜测；
- 如果多个 group 成员落在同一配额桶，选择器应共享该桶的 RPM/429 penalty，必要时把它们视为同一 quota cohort，不能按成员数量虚增容量。

Q3-① 不需要用户先解密确认才能开始实现：实现应在运行时安全识别完全重复的 key，并提供仅显示“重复配额桶数量/成员数量”的诊断。上线前可用该诊断确认现有 6 个 binding 的真实分布。

Q3-③ 的方向确认正确：execution 与 inspection 共用同一 group 后，人工把两条链反向排列的做法应被全局共享状态吸收，不再依赖手工错开顺序。

### Q4：组内 retry_count —— 采用 A，但第一版必须有硬上限

load-balance route 不直接继承成员的 `retry_count`。否则 `9999999` 会让某个成员长期占住选择和退避周期，破坏组内平级 fallback。

第一版规则：

- load-balance group entry 提供 group 默认的 `max_retry_rounds`；
- 可选地提供按成员 model key 的 override；
- 第一版允许范围为 `1..3`，默认 `1` 个完整 key pass；超过上限直接配置校验失败；
- 成员的 catalog `retry_count` 继续只对 direct model route 生效；
- group 内一次完整 key pass 后，如果仍是可 fallback 错误且预算耗尽，就切换到同组其他未尝试成员；
- retry/backoff 与 429 penalty/cooldown 共同作用，但不能因为成员的历史 `retry_count` 很大而跳过同组候选；
- 如果未来确实需要“坚持某个 group 成员”，应增加显式的 group policy，而不是重新读取无限大的 catalog retry_count。

因此不采用 B 的“一律一次就换成员”作为唯一策略，也不采用 C 的“先手工修改 9999/9999999”。direct route 可以保留现有高 retry 配置，group route 必须使用独立且有上限的预算。

### Q5：重复项 —— 新结构严格报错，legacy flat chain 保持兼容

- 旧的 `model_keys: list[str]` 输入继续保留现有静默去重，避免破坏老配置和旧 API 客户端；
- 新的显式 `route_entries` 中，重复 direct model、重复 group 引用和同一 group 内重复成员均返回可读的 400 校验错误；
- 同一模型通过 legacy flat chain 与显式 route 同时传入时，以显式 route 为准并走严格校验，不做静默合并；
- group 重复成员不能依赖 schema 层静默清理，因为它会改变均衡权重并隐藏配置错误。

### Q6：链首隐式依赖 —— 全部纳入 Phase 3，不保留运行时链首语义

以下路径必须在 route-aware runtime 接入阶段一起改造：

- `runtime_service.py:5514`：inspection 缺省时继承完整 execution route plan，而不是继承展开后的字符串数组；
- `chat_backend.py` context-window 解析与 `react_loop.py` preflight：使用 route-aware candidate resolver；在模型尚未最终选择时采用候选集合的安全边界，最终请求记录 selected model；
- `chat_backend.py:708-721`：实际 provider attempt 使用 selected model 自己的 timeout；预选阶段不能读取链首作为决定；
- `cli/commands.py:792-794`：改为打印 route/group 语义和候选展开结果，不能把链首标作“实际执行模型”；
- retry status、attempts、actual-request diagnostics、token usage、context/multimodal 判定和 prompt cache 相关 helper 都必须记录/使用最终 selected model 或 route signature。

只允许在以下两个兼容场景保留“展开后第一个”：

1. legacy flat chain 且没有 group 的旧配置；
2. 明确标记为“默认候选/展示回退”的纯显示字段。

它不能再参与准入、负载统计、context window 决策、timeout 决策、fallback 或 actual-request 归因。

---

## 14. 二轮取证（2026-09-26，只列事实与由此暴露的计划内部冲突）

数据源：`.g3ku/main-runtime/runtime.sqlite3` 表 `task_model_calls`（`created_at >= 2026-09-24`，2588 回合 / 4943 次 provider 请求；按节点长度分组的那张表窗口为 `>= 2026-09-20`），`.g3ku/main-runtime/managed-worker.log` 末 165,805 行，`.g3ku/config.json` 与 `.g3ku/llm-config/*` 当场用 `load_config()` 读出。

### 14.1 裁定前提核验结果

| 裁定条目 | 核验 | 证据 |
|---|---|---|
| Q1「首次选择必须在 request preflight 之后」 | **现序已满足**，不需要挪代码 | react_loop.py:743-746 先判 preflight 失败并返回，747-760 才 `acquire_turn()` |
| Q1「fallback 不得创建第二个 node-turn lease」 | 可行，有可变先例 | `NodeTurnLease` 非 frozen（node_turn_controller.py:18），chat_backend.py:906 已在改 `held_turn_lease.initial_model_permit` |
| Q1「pump 改为有界轮转」 | **必须换移除方式** | 现 pump 是先 `permit` 后 `self._queue.popleft()`（node_turn_controller.py:190-196），轮转后必须移除"被授予的那一条"，不能移除队头 |
| Q2「识别 RPM/TPM/RPS 维度」 | 状态码可信，**文案维度不可信** | `is_request_shape_error` 的注释已记录 sensenova 网关把 429 标成 `invalid_request_error`（fallback.py:229-233）；`_error_status_code()` 只读结构化状态（fallback.py:222-236） |
| Q3「内存 fingerprint 合并重复配额桶」 | **不能从配置扫描侧算** | 在 web/worker 进程外用 `build_provider_from_model_key()` 实测 6 个 ref 全部 `keys=0`、`base_url` 为空（sha256 空串 `e3b0c442`）→ 6 个 binding 会塌进同一个"空 key"桶，产出假阳性重复结论 |
| Q4「group 默认 1 个完整 key pass」 | **不能复用现表达式** | chat_backend.py:857 `normalized_retry_count(...) or DEFAULT_RETRYABLE_MODEL_ROUNDS`，且 fallback.py:40 注释确认 `retry_count=0 → 10 轮`。"未配置"和"配 0"都落默认，group 必须写显式成员级值才能拿到 1 |

### 14.2 新发现：节点级模型粘滞是现状，逐回合重选会动它

现网一个节点在其生命周期内几乎不换模型：

| 节点回合数 | 节点数 | 用过的不同 model_key | 落在主用模型上的请求占比 |
|---|---|---|---|
| 6–15 | 125 | 122 个节点只用 1 个 | 99.8% |
| 16–40 | 145 | 143 个节点只用 1 个 | 99.8% |
| 41+ | 77 | 72 个节点只用 1 个 | 92.8% |

同窗口内 `cache_hit_tokens` 为 109,188,864，未命中输入 52,879,833，**缓存命中占全部输入 token 的 67.4%**。

口径修正（按“断点之后存活的尾部”算，不按切换次数算）：换 `model_key` 等于换前缀缓存命名空间，断点之后的**整段存活上下文**都要重传，所以单次切换的代价 = 该跳本可命中的缓存量。按节点长度 ≥16 回合的 48 个长节点、2166 跳实测：每跳未命中输入中位 8,821 token，每跳 `cache_hit` 中位 32,768 token（p90 106,496），命中率 68.9%。**脱靶一跳的代价就是中位 32,768 token。**

由此：Q1 若实现为“每一次准入都按当轮负载重选成员”，一个 41+ 回合的节点会在一生中被分到多个成员上，每次切换都要重传整段存活上下文；67.4% / 68.9% 是全窗口聚合占比，长节点若每跳都脱靶，等价于把这 104,129,280 个 cached token 变成 fresh input。此点 §13 未覆盖，需在 Q1 下补一条绑定粒度裁定（见 14.5 选项，已由 §15 裁 A 节点级粘滞）。

上面“每跳脱靶 = 该跳 cache_hit 量”是实测；“逐回合重绑会把全部 cached token 变 fresh”是投影，不是实测——现网还没有逐回合重绑的样本。前提假设：provider/网关的前缀缓存按 model 维度分桶；若网关注到跨模型共享缓存，实际代价会低于该投影，上线前用场景 G 的实测复核。

### 14.3 新发现：请求量集中在失败尾，且预算收缩会去掉退避节拍

- 2417 个单模型回合平均 1.41 次请求，占请求总量 68.7%；171 个跨模型回合（6.6%）平均 **9.05 次请求，占总量 31.3%**。
- 全体回合请求数分布：1 次 2131 个、2–4 次 131 个、5–9 次 275 个、≥10 次 51 个。
- 多模型回合的节拍来自退避：`model_retry_backoff_seconds()` 为 1s 起、60s 封顶、±25% 抖动（fallback.py:37-38,415-425），且退避只发生在**同模型轮之间**（chat_backend.py:1068-1095）；跨模型前进（1101-1128）**不插退避**。
- 因此 Q4-A 把 `max_retry_rounds` 收到 1..3 后，这 171 个回合的 1547 次请求会从"被退避摊开"变成"一次 pass 内背靠背打完 6 个成员"，而后者的成因是每分钟 RPM 窗口。

Q4 落地时必须显式回答"组内成员之间是否插退避"，否则组预算收缩会砍掉唯一在抑制 RPM 风暴的机制。

### 14.4 正文与 §13 的冲突清单（已于 2026-09-26 全部改写进正文，保留作变更记录）

| 正文位置 | 与哪条裁定冲突 | 冲突内容 |
|---|---|---|
| §4.1 负载定义 / load_score 公式 | Q2 | 只含本地 running/waiting/reserved，无滚动速率项与 429 衰减惩罚 |
| §4.2 选择规则开头"每次新 provider attempt 进入一个 load-balance route 时" | Q1 | 选择时机写在 chat_backend 的 attempt 层，不是准入层 |
| §4.4 表第二行"先按该模型的 `retry_on/retry_count` 处理" | Q4 | 裁定为成员级 `max_retry_rounds`(1..3) 覆盖 catalog |
| §4.4 末段 cooldown 只按"跨请求反复失败" | Q2 | 裁定 cooldown/惩罚来自衰减的 429 观测，非单纯失败计数 |
| §5.1 `try_acquire()` 签名（无 route_index、无成员级预算）与 §5.3 步骤 4-5 的重复 acquire | Q1/Q4 | 双 permit 正是要禁止的行为 |
| §5.4 改造点清单 | Q1/Q6 | 未列 `acquire_turn()`/`NodeTurnLease`/`_pump()` 三个必改点 |
| §6.3 第 4、6 条"组卡片不排序、保存前预览" | Q3 | 未提配额桶合并后的"重复桶数量"诊断与 `quota_pool_key` 字段 |
| §7.1 字段清单 | Q2/Q3 | 缺 `rolling_rpm_60s`、`recent_429_by_dimension`、`penalty_before/after`、`quota_bucket_digest_count`（不得输出 digest 本身，见 14.1 Q3 行） |
| §8.2 第 2、3、5、7 条与 §12 场景 A/C | Q2/Q3 | 均匀性阈值、等容量分母、busy 判据都建立在"binding 即独立配额"这一被裁定否决的假设上 |
| §9 Phase 1/2/3 完成标准 | Q1/Q4 | 完成标准仍按"chat_backend 内选择 + 成员继承 catalog retry_count" |
| §10 第 2 条 feature flag 关闭时的回滚 | Q1 | 只说 route 按原序走，未说准入层如何退回"链首预占"旧路径 |

### 14.5 追加两条待裁（已由 §15 裁定）

1. **绑定粒度**：A. 节点级粘滞 + 阶段边界/candidate-filter 变化/成员 cooldown 时才重绑（建议：新增 `node_id → 成员` 亲和账本，准入读它，Q1 的原子选择与跨节点摊匀都保留，节点内换模型次数与今天一致）｜B. 逐回合重选（均衡粒度最细，代价见 14.2）｜C. 粘滞 + 每 N 回合重估。
2. **组预算结构**：`max_retry_rounds` 是"整组连续 pass 数（pass 之间沿用现有 1→60s 退避，成员之间背靠背）"(A)｜"每成员 pass 数，成员之间插退避"(B，直接回答 14.3)｜"纯 pass 数不设退避"(C)。

---

## 15. 追加裁定（2026-09-26，按 14.5 的推荐落定）

### 15.1 绑定粒度 —— A，节点级粘滞

- 亲和账本 `node_id → (route_index, group_key, model_ref, key_index, permit)` 由 `ModelLoadBalancer` 持有，属运行态，不进 `config.json`。
- 同一节点后续回合复用绑定成员，**不重选**；重绑只由四类触发：阶段边界、preflight 过滤条件变化、绑定成员进入 cooldown 或其配额桶惩罚超阈、route plan/config revision 变化。节点结束释放。
- 落地的硬约束写进场景 G 与 8.2.11、8.4.6：长节点在一生中绑定的成员数应为 1（除非命中触发条件）。
- 均衡的粒度因此是“节点之间摊”，不是“回合之间抖”；现网峰值 1 分钟内 8 个不同节点在发请求（`nodeDispatchConcurrency` execution 8 / inspection 4），6 成员组的绑定离散度足够消掉链首 457/794 的 429 归因集中度。

### 15.2 组预算结构 —— B，每成员 pass + 成员之间插退避

- `max_retry_rounds` 语义为“每个成员允许的完整 key pass 数”，默认 1、允许 1..3、超上限报错。
- 换下一个成员前 `await model_retry_backoff_seconds(k)`，`k` 为本请求在本组已消耗的 pass 数（1s 起、60s 封顶、±25% 抖动）。
- 定性说明：这不是新增机器护栏，而是把今天只在“同模型轮之间”存在的退避节拍（`chat_backend.py:1068-1095`）搬到“组内成员之间”；跨模型前进今天没有节拍（`:1101-1128`），而 14.3 实测表明请求量集中在失败尾（6.6% 的回合扛 31.3% 的请求，均值 9.05 次/回合），去掉节拍会把它变成背靠背 burst。
- §13 Q4 里“可选地提供按成员 model key 的 override”第一版**不实现**，只做 group 级统一预算；需要时再按显式 group policy 增补，不得回头读 catalog 的无限 `retry_count`。

### 15.3 其余按 §13 原样执行

Q1 选择上移准入层、Q2 综合负载含滚动 RPM 与 429 衰减、Q3 两层配额模型与内存 fingerprint（空值记 `unresolved` 不互并）、Q5 双严格度校验、Q6 链首依赖全部纳入 Phase 3，均已改写进 §3–§12。

---

## 16. 实施状态（2026-09-26，分支 `feat/model-load-balancing`）

Phase 0–5 全部落码，架构文档已入册。分支上的 10 个提交：`474b00f0`(Phase 0 基线) → `e73b0c82`+`ffb5d0cd`(Phase 1 配置) → `2ddd6c33`(准入) → `fe6c7fb6`(chat 链路) → `813dca93`/`4b65d1d5`/`02cfe4be`(重试状态、观测、前端) → `1a18d2c6`(docs) → `a66746a5`(本节)。

已按裁定落地的关键选择：

- 首次选择在准入层（`acquire_turn(route_plan=…, filters=…)`），第一次 attempt 消费准入 permit，key 轮序按选中 key 旋转；fallback 复用同一个 `NodeTurnLease`（`rebind_turn`），不产生第二个回合权。
- pump 改有界扫描（8）+ 精确移除被授予的请求 + 防饿死屏障（被越过 4 次后独占本轮）；旧的「严格 FIFO 队头阻塞」测试按新契约改写，现状行为另存于 Phase 0 基线文件。
- 负载 = 本地归一化在飞 + 配额桶 60 秒滚动请求数 + 衰减 429 惩罚；桶按 endpoint+key 指纹或显式 `quotaPoolKey` 合并，解析不到密钥材料时各自 `unresolved` 且绝不互并。
- 组预算 `max_retry_rounds`（1..3，不继承 catalog `retry_count`），成员之间插入退避节拍。
- 节点级粘滞绑定 + 四类重绑触发 + 节点终态清除。
- 回滚闸门 `mainRuntime.modelRouteLoadBalanceEnabled`：关掉即把含组的链按配置顺序摊平成 direct 候选。
- 运行态经 worker 心跳上报（`model_route_groups`）+ `GET /api/models/load-balance/status`；只输出计数与桶序号。

验证口径（分支上实测）：`test_resource_runtime_smoke` 99 passed/5 xfailed；route 系列新测试 72 + 69 + 179 三批全绿；宽回归批 551 passed；JS `node --test` 447 项中与本改动无关的存量红 1 项（主树同样红）；ruff 与主树逐文件对齐且 `chat_backend.py` 少一条。

尚未完成、需要授权或另开一轮的事项：

1. **实盘验收（§12 场景 A–G）**。代码要生效必须重启托管 worker；这会打断他正在跑的任务，因此未自行执行。验收时优先看三条：链首 457/794 的 429 归因是否被摊开、长节点每生命周期重绑次数是否为 0、每跳 `cache_hit_tokens` 是否不低于 32,768 中位基线。
2. **异构窗口的组**：现网 14 条 catalog 窗口全等，成员窗口不同时的保守过滤只由单测覆盖（8.4.3 的 `no_candidate` 路径）。
3. **前端视觉核验**：模型链编辑器的组卡片只过了 JS 契约测试与 `node --check`，没有在浏览器里看过——分支不是当前 18790 端口上运行的那份代码，再起一个 web 会踢掉在跑的实例。
4. 架构文档体积：`runtime-overview` / `context-and-cache-troubleshooting` / `operations-and-maintenance` 本轮改动前就已超出 README 的参考带宽（199/82/60 KB 对 88/71/31 KB 上限），本轮各自再加 1–4 KB。是抬参考值、还是按规则 6 的阶梯做拆分/搬移，留给你定，我没有代删他人契约。
