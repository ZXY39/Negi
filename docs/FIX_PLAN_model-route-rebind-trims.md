# Fix Plan: 节点模型路由的重绑收口（删阶段边界清绑、删全局 revision 比较、cooldown 整条车道删除）

> **Status: 待实现。** 目标分支 `feat/model-load-balancing`（独立 worktree `G3KU-Agent-lb`，HEAD `85be3f21`），是本功能的后续收口，不单独成立——它删的三条车道都只存在于该分支上。
>
> Origin: 操作者要求，2026-09-26。起点是「与失败无关的重绑触发是否多余」，中途三条裁定：
> ① **组成员变化不整组重绑**——只有绑定成员被移出组的那些节点才解绑，加成员不动任何既有绑定；
> ② **cooldown 是为不发生的场景写的特例，删**——「其他节点反复撞」是可接受的，换节点后请求就可能成功，额外耗时由操作者按日志自行修配置；
> ③ **penalty 只保留 429 一档**——不把惩罚判据扩到 401/403/5xx，即本计划不新增任何失败记忆。
>
> Scope: 节点模型路由的准入选择与失败观测（`model_load_balancer.py` / `react_loop.py` 的一处清绑 / `runtime_service.py` 的状态字段）。**只删不加**：不引入组级签名、不引入状态码分档、不加新配置项、不动打分权重。
>
> 判据（操作者定）：负载均衡的初衷是**优化模型调用的均匀度**，不是做故障处置；失败情景的处理复杂度**与原来的模型链保持一致**。落到本计划：均衡器只保留"配额桶到没到限流"这一个从旧链继承的判断（复用同一张 `429` 关键字表），其余失败一律不留记忆，链该怎么 fallback 还怎么 fallback。

---

## 0. 决定形状的测量

数据源：主树 `.g3ku/logs/console.log`（2026-09-17 03:22 → 09-26 17:01，约 9.5 天）与 `.g3ku/config.json` 的 catalog / roles。

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| M1 | cooldown 六条触发词在实盘的命中次数（`All configured API keys are disabled` / `Error code: 401` / `Error code: 403` / `Invalid API key` / `api_key` / `unknown model key`） | 配置类 **0 次**（前、后两项均为 0）；401 **78 次**、403 **1 次**；总计 79 次 ≈ **每天 8 次** | cooldown 的"配置坏了"场景 9.5 天零命中 ⇒ 删除 |
| M2 | 这 78 次 401 的出处 | 全部落在 `main.runtime.chat_backend:_log_model_chain_fallback:666` 的 FALLBACK 记录里（`model_ref: glm-5.2-5 → next_model_ref: glm-5.2-4`） | **换下一个配置已经是现状行为**：401 既不是 `APIKeyConfigurationError`、也不在内部错误名单（`g3ku/providers/fallback.py:51-59` 只有 sqlite/database 类），`should_fallback_model_error` 判 True ⇒ 组内换成员。cooldown 买到的只是"别的节点少撞一次快速失败" |
| M3 | 同一段日志里 `Error code: 429` | **2132 次** | 失败记忆只留 429 这一档（裁定 ③），它对应的才是本特性要解决的问题（链首 `deepseek-v4-flash-2` 独占 457/794 的归因） |
| M4 | `config refreshed` 次数与 reason | 162 次，其中 `admin_qq_bot_settings_update` 3、`ceo_frontdoor` 2、`prompt` 1 = **6 次（3.7%）与模型路由无关** | revision 来自 `config.json` 的 mtime（`g3ku/config/live_runtime.py:44`），任何一次无关保存都会清光**全部在飞节点**的绑定 ⇒ 删该比较 |

一处必须先讲清的机制（它是"删 :260 就够，不需要组级签名"的依据）：**精确车道已经存在**。`configure()` 在装载新组定义时，只解绑"绑定成员已被移出组"的节点，其余绑定原样保留（`main/runtime/model_load_balancer.py:206-210`，注释即"成员被移出组：解绑，让该节点下次准入重选。在飞请求不受影响"）。`select()` 里那条 `binding.config_revision != self._config_revision`（`:260`）是压在精确车道上面的第二道、且是**整组宽泛**的一道。所以本计划是删，不是换成更细的比较。

`maxRetryRounds` 与成员能力本来就不需要重绑：槽位预算每回合从 plan 现读（`main/runtime/chat_backend.py:1097`），窗口/多模态由每次请求的 `RouteCandidateFilters` 现场判定（`main/runtime/model_route.py:39-48`）。

---

## 1. P1 — 删阶段边界清绑

现状：节点进入新执行阶段时主动清绑，下一次准入重新按负载选择。

| 删除点 | 位置 |
| --- | --- |
| 清绑调用（`forget_route_binding`）与 `previous_stage_boundary_key` 的比较块 | `main/runtime/react_loop.py:785-789` |
| 局部变量 `previous_stage_boundary_key = ''` 初始化 | `main/runtime/react_loop.py:272` |
| `_execution_stage_boundary_key`（确认无其他读者后整体删） | `main/runtime/react_loop.py:7332` |

不动的：`rebind_turn(rebind=True)` 这个入口**保留**——组内换成员走的是同一个入口（`main/runtime/chat_backend.py:917`）。`forget_route_binding` 本身保留，清绑点只剩节点终态（`main/runtime/node_runner.py:7136`、`:7183`）。

## 2. P2 — 删绑定上的全局 revision 比较

| 删除点 | 位置 | 备注 |
| --- | --- | --- |
| `binding.config_revision != self._config_revision → plan_changed` | `main/runtime/model_load_balancer.py:260` | 精确车道在 `:206-210`，见 §0 |
| `select()` 里的 `binding.model_key not in group.candidate_model_keys → plan_changed` | `:262` **保留** | 它是"plan 比 balancer 状态新"时的兜底，且是逐节点判定，不误伤 |

`_NodeBinding.config_revision`（`:144`）**保留**，但降级为纯展示字段：快照里仍报"这条绑定是在哪个 revision 下建立的"（`:484`），不参与任何判定。

## 3. P3 — cooldown 整条车道删除

判据、状态位、消费点一并删，不留开关。

| 删除点 | 位置 |
| --- | --- |
| `_looks_unavailable` 六关键词表 | `main/runtime/model_load_balancer.py:103-114` |
| `record_outcome` 里贴冷却的分支（含 `consecutive_unavailable` 递增与 `COOLDOWN_SECONDS_DEFAULT * 连击` 封顶逻辑） | `:384-397`；常量 `:47-48` |
| `_MemberState.cooldown_until` / `cooldown_reason` / `consecutive_unavailable` | `:134-135` 及其声明块 |
| `_in_cooldown`（含过期时顺手清零的副作用） | `:580-587`，消费点 `:321` |
| `_binding_blocked_reason` 的 `cooldown` 分支 | `:326-328` |
| `configure()` 里 revision 前进时撕条子 | `:211-215`（连同 `:189-191` 的 docstring 相应改写） |
| 快照里的 `cooldown_until_monotonic` / `cooldown_reason` | `:456-457` |
| 状态接口透传的 `cooldown_reason` | `main/service/runtime_service.py:10485` |

`record_outcome` 删完之后剩下的就是限流那一支（`:375-382`：给配额桶记 `penalty_events` + `throttle_events`，半衰 60s 衰减）。这一支的判据**改回复用旧链的关键字表**，不再自带一套文本匹配：

```python
# 现在（自带三份文本标记，与旧链的 429 preset 重复）
return "error code: 429" in lowered or "ratelimiterror" in lowered or "rate limit" in lowered

# 改为（与 is_retryable_model_error 同一张表，g3ku/utils/retry_keywords.py:33-38、:72-84）
return any(token in lowered for token in expand_retry_keywords(["429"]))
```

`status_code == 429` 那条短路保留（旧链也优先看状态码）。preset 展开是 `429 / rate limit / too many requests / quota`，所以误判口径与改造前的"要不要重试"判断**完全一致**——同一个错误文本在旧链里判可重试，在新均衡器里判"这个桶到配额了"，不再出现两套说法。两点必须写清：① 惩罚不看成员自己的 `retryOn`（成员配 `retryOn: []` 也照样记 429 惩罚），因为惩罚描述的是上游事实，不是"这条车道该不该重试"；现网 14 条 catalog 全含 `429`，所以眼下无差别。② `classify_throttle_dimension` 的 rpm/tpm/rps/token 分档（`:77-93`）**只进观测、不参与任何判定**，保留不影响行为；若按"失败面一律对齐旧链"推到极致也可以删掉它，代价只是状态接口少一列归因。

`LEASE_OUTCOME_UNAVAILABLE`（`main/runtime/model_route.py:20`）在 `release()` 里的唯一作用就是把错误文本喂给 cooldown（`:408-409`）：删这两行后，该 outcome 退化为纯调用方标记，**保留常量与传入点**，因为它仍是日志/释放原因的可读标签，删掉反而要改一串调用方。

前端与管理面对象没读 `cooldown`（`grep` 过 `g3ku/web/frontend/*` 与 `main/api/admin_rest.py`），所以删除不破坏任何界面。

## 4. 三档各自的误伤（不推荐任何一档，逐条列）

| 改动 | 代价 | 实盘依据 |
| --- | --- | --- |
| P1 删阶段边界清绑 | 绑定成员整个阶段都坏时，不再因为"阶段换了"白得一次重选机会 | `penalty_threshold` / `capacity` / `filter_changed` / 成员被移出组，四条都在下一次准入接管，不是漏网 |
| P2 删 revision 比较 | 只改成员的 `retryOn`/`retryCount`/密钥序号时不重绑 | 这些本来就不改变"该成员能否服务本节点"；M4 的 6 次无关保存反而每次都清了全部绑定 |
| P3 删 cooldown | ① 401/403 反复撞：每个新节点各付一次快速失败请求；② 若某成员真的密钥全禁用，`APIKeyConfigurationError` **不会**被链绕过（`main/runtime/chat_backend.py:1248-1250`、`:1083` 直接上抛），删掉 cooldown 后每个撞上的节点都各自失败一次 | ① 78 次/9.5 天，均每天 8 次，且 M2 已证链自己绕；② 该类错误 9.5 天 **0 次**，且真发生时操作者本来就要收到失败回合并去修配置 |

## 5. 测试

- `tests/resources/test_model_load_balancer.py`：删 401 进冷却、连击递增封顶、revision 撕条子、`_in_cooldown` 过期清零这几条用例（`:308`、`:471` 两处 `LEASE_OUTCOME_UNAVAILABLE` 的冷却断言）；新增一条负向断言——**同一成员连续 release 401 之后，下一次 `select` 仍按分数选中它**（钉住"不留失败记忆"是设计而非疏漏，防止后来者把它当 bug 补回去）。`:324-325` 那条用 `rebind_reason="stage_boundary"` 的断言换成 `"fallback_after_failure"`，因为 P1 删的是 react_loop 的调用点，`rebind_turn` 入口不变。
- `tests/resources/test_model_route_admission.py`：若有按 revision 变更触发重绑的用例，改为按"成员被移出组"触发。
- `tests/resources/test_model_route_runtime_wiring.py`：删阶段边界清绑的用例。
- `tests/resources/test_model_route_chat_backend.py`：`:19` 一带若断言 cooldown 相关行为需同步；组内换成员与退避节拍不受影响。

## 6. 架构文档（实现轮跑 `g3ku-architecture-maintenance`）

- `docs/architecture/runtime-overview.md`「节点模型路由与准入绑定」（`:261-268`）：重绑触发从"四类"改写为"四条事实"，删掉冷却表述；`:268` 那句"组 busy 只由事实推导：拿不到 permit、**全部在冷却**、或本请求全部试过"必须重写——冷却不再存在，busy 的构成只剩 permit/资格/已试。
- `docs/architecture/config-and-models.md:139`：配置刷新与重载那段若提到清冷却，删。
- `docs/architecture/operations-and-maintenance.md:363-364`：排障指引补一句判据——**401/坏密钥不留任何运行态记忆**，要看就 grep FALLBACK 行（`_log_model_chain_fallback`），别去状态接口找 `cooldown_reason`（该字段本次删除）。
- `docs/architecture/context-and-cache-troubleshooting.md:223`：粘滞绑定那段，把"四类触发"跟着改写。

## 7. 验证口径

`python -m pytest tests/resources/test_model_load_balancer.py tests/resources/test_model_route_admission.py tests/resources/test_model_route_chat_backend.py tests/resources/test_model_route_runtime_wiring.py -q`，加 `test_resource_runtime_smoke.py` 与既有 route 三批；`ruff` 逐文件与主树对齐；`node --test` 不受影响（无前端改动）。

实盘判据（需重启 worker，另行授权）：一次无关配置保存（例如改 prompt）之后，`GET /api/models/load-balance/status` 里 `node_bindings` 的数量不应下降；日志里不应再出现 `cooldown` 相关 reason。
