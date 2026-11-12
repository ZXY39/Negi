# 任务级模型配置（执行链 / 检验链单独覆盖）实现计划

状态：已实施（v2 全五批提交：71285008 P1 / 6c36550f P2 / d6c5de0d P3 / c3ba6755 P4 / 51e586b2 P5 验收脚本；真任务验收 20/20；cda207eb 按用户要求改两列并排）
日期：2026-11-12
靶子：任务详情页头部「活跃节点」胶囊左侧新增「模型配置」入口；点开显示本任务生效的执行 agent 模型链与检验 agent 模型链；默认跟随全局；**覆盖链与全局角色链完全同形——任意已保存模型与任意负载均衡组槽位可自由编排**；任务面板的修改只作用于本任务，**绝不反向同步全局**，全局配置（含组定义）唯一写点仍在模型页。

v2 修订（按用户反馈）：推翻 v1 的 D2「覆盖链只允许扁平 key、不含组」。覆盖链改为有序 route entry 列表（direct + load_balance），与 `models.roles.execution/inspection` 同形。由此牵出一处必须改的运行时落点：balancer 组注册从「仅全局链引用的组」扩为「整份配置的组」（§3.3，已核安全）。

## 1. 现状事实（已核证据）

路由与准入：
- 全局两条角色链 `execution` / `inspection`；worker 启动与配置刷新重建 route plan 并同步注册组：`main/service/runtime_service.py:9454-9461`、`15605-15653`（`_apply_model_route_plans` + `_load_balance_groups_from_plans`——**当前只注册全局两条链里出现的组**）。检验全局为空时继承执行 plan（`9459-9460`、`15622-15635`）。
- 节点级路由唯一选择点：`main/runtime/node_runner.py:3646-3647` `_model_routes_for(node)`；候选展开 `_model_refs_for` 3633-3644 从 plan 派生；react_loop 注入 `model_routes_supplier`（848-851），链轮/重试边界活解析（`main/runtime/react_loop.py:812-815`、`main/runtime/chat_backend.py:1242-1245`）。覆盖接进 `_model_routes_for` 即自动获得「下一个链轮边界生效、不热切在飞请求」合同（`docs/architecture/config-and-models.md:167-181`）。
- route plan 构造：`main/runtime/model_route.py:180-250` `build_model_route_plan(config, scope)`：从 `config.get_role_model_routes(scope)` 读有序 entry；组 entry 查 `config.get_load_balance_group`，**组不存在→跳过该 entry**（198-200），组禁用→保留 entry 但候选为空（226-233）；direct 禁用成员跳过（239-241）；回滚闸门 `mainRuntime.modelRouteLoadBalanceEnabled=false` 把组摊平成 direct（190-220）。
- 组注册安全性（v2 扩注册的判据）：漂移检查只查组表**自洽**（注册键==组自带键、成员不被两组认领，`main/runtime/model_load_balancer.py:169-190`），不与全局链比对；`configure()` 只解绑「绑定成员已被移出组」的节点（217-253），多注册未被引用的组不动任何既有绑定、不清观测。准入对每个组 entry 调 `balancer.serveability(group_key)`（`main/runtime/node_turn_controller.py:575-590`），组未注册即 `unknown_group` 整链跳过、节点冻在排队——**这就是任务链引用组必须扩注册的原因**。
- 绑定粒度是节点（`_bindings` 按 node_id），选择沿当前 plan 的 routes 走；plan 换组后旧绑定不被引用、节点终态清除——覆盖切换不需要额外清绑定代码（补测试锁定）。
- 组定义访问：`config.models.load_balance_groups`（`g3ku/config/schema.py:499`）、`get_load_balance_group`（1423）、`get_role_model_routes`（1415）。全局写侧校验口径：direct 成员须存在且启用、组引用须存在（`schema.py:1291-1338`）；组仅允许 execution/inspection 引用（1314-1322）。

任务与写入：
- `TaskRecord.metadata` 是扩展位（`main/models.py:382-402`）；快照 `task.model_dump()` 已带 metadata 进前端（`main/monitoring/query_service.py:902`）。
- web 进程写任务行先例：`main/monitoring/log_service.py:1926-1965` `update_task_control`（`_task_lock` + `store.update_task` mutator + 摘要 patch）；mutator 单写线程内重读再整行 upsert（`main/storage/sqlite_store.py:1001-1016`）。
- 任务 REST：`main/api/rest.py`（`/tasks/{task_id}` 系列）。命令车道（`runtime_service.py:2239-2369`）用于要 worker 动手的操作；本功能 worker 边界活读，不需要新命令。
- 辅助车道：spawn 送审评审用 `_acceptance_model_refs` 展开视图（`node_runner.py:4829`，task 在手，不过准入——维持现状）；重复预检在任务创建前（`runtime_service.py:5771`，保持全局）。

会话先例（语义/校验/前端交互照抄）：
- 存储归一 `g3ku/runtime/web_ceo_sessions.py:795-890`（空不落键、读侧容忍失效）；API 逐 key 点名 404/409 带可读 message（`g3ku/runtime/api/ceo_sessions.py:1223-1268`）；前端「草稿与全局逐位相同⇒发空载荷恢复跟随」（`ceo/model-panel.js:233-250`）、编辑中后台刷新不打回草稿（keepDraft，104-136）。

前端：
- 原生 JS IIFE。头部 `g3ku/web/frontend/org_graph.html:942-987`（活跃节点胶囊 958-962）；U 绑定 `org_graph_app.js:635` 附近；脚本装载 `org_graph.html:1808-1839` + 测试清单 `tests/resources/support/frontend_sources.js:34,54`；头部恒一行只压缩提示词宽（`org_graph.css:17244,18986`）。
- 模型页链编辑器：`models/role-view.js:211-269` `renderModelRoleEditors`（org_graph_llm.js:2435 用 renderRoutes 覆盖）；组槽位卡 `renderModelGroupChainTile(scopeKey, groupKey, index)`（role-view.js:69-86，读 `activeLoadBalanceGroups()`——**roleEditing 时切到组草稿**，任务面板不能进这个态）；链→route_entries 转换先例 `org_graph_app.js:9048`；`groupRefToken/isGroupRef/groupKeyFromRef`（9044-9046）；`normalizeModelRoleChain/activeLoadBalanceGroups`（models/role-store.js:113,137）；`S.modelCatalog` 含 catalog/roles/routeEntries/loadBalanceGroups（org_graph_llm.js:2449）。
- 模型页保存车道会**整份替换 `models.loadBalanceGroups`**（org_graph_app.js:9059-9061 注释、role-store.js cloneLoadBalanceGroups）——任务面板必须完全绕开这条车道，这是「不反向同步」的最大风险点。

## 2. 已定默认（可否决）

- D1 覆盖粒度：执行 / 检验两条独立车道，各自「跟随全局」或一条显式有序 route entry 链；不做固定单模型模式（单模型＝长度 1 的 direct 链）。
- D2（v2 修订）链形状：**与全局角色链同形的有序 route entry 列表**——`{type:"model", model_key}` 与 `{type:"load_balance", group_key}` 任意混排、任意顺序，可选任意已保存模型与任意已存在的负载均衡组。组**定义**（成员、轮数、启停）仍是全局资源，唯一写点在模型页；任务面板只引用、只读展示组定义，不提供组的增删改。
- D3（v2 修订）存储：`TaskRecord.metadata.model_config = {"execution_route_entries": [...], "inspection_route_entries": [...]}`，entry 为 snake_case 对象（同上形状）。空列表＝跟随全局；写侧摘空键、两键皆空摘整键；读侧丢弃畸形 entry、保序、容忍失效引用。
- D4 失效降级（解析时，不改存储）：direct 成员禁用/删除→跳过；组被删除→跳过该 entry（builder 现行为）；组禁用→保留槽位但候选为空（与全局一致，准入按组耗尽前进）；整条链解析为空→回退该车道全局链。面板对失效成员/组画警示徽标。
- D5 检验继承：检验「跟随全局」且全局检验链为空时，继承**本任务生效的执行链**（含组），与全局继承同构。
- D6 写入路径：web REST 经 `log_service` mutator 落任务行（暂停旗标先例），worker 离线/任务暂停可保存；worker 下一链轮边界活读生效。**PATCH 只写 task.metadata，不触碰 config.json / models.roles / models.loadBalanceGroups，不触发 refresh_runtime_config。**
- D7 面板形态：头部胶囊按钮「模型配置」（有覆盖加小圆点），右侧抽屉（复用 `detail-drawer` 壳）。两节上下排：执行链 / 检验链；每节「跟随全局 / 单独配置」切换 + 链编辑器（模型与组槽位混排拖拽、移除、添加选择器分「模型 / 负载均衡组」两栏）；底部一个「保存」整窗落盘。组槽位卡显示组名 + 成员展开 + 启停徽标 + 「组定义在模型页维护」提示，不可就地编辑。无声音。
- D8 范围：只做任务详情页入口；创建时不带配置；重复预检保持全局。
- D9（v2 新增，硬保证）不反向同步：任务面板自有草稿态 `S.taskModelConfig`，**绝不进入 `S.modelCatalog.roleEditing`、绝不写 roleDrafts/loadBalanceGroupDrafts、绝不调用模型页保存/组弹窗写动作**；交互 handler 全部 `data-task-model-*` 命名空间，与模型页 `data-scope`/`data-group-edit` handler 零交叠。保存只发任务 PATCH。

## 3. 数据与 API 合同

### 3.1 metadata 归一（main/models.py 新增）

```python
TASK_MODEL_CONFIG_KEY = 'model_config'

def normalize_task_model_config(raw) -> dict:
    # {"execution_route_entries": [...], "inspection_route_entries": [...]}
    # entry 只认 {type:model, model_key} / {type:load_balance, group_key}；
    # 畸形/空 key 丢弃，保序；读侧不校验存在性（写入口校验）
```

写侧（log_service）非空才落键，两键皆空摘整键；mutator bump `updated_at`。

### 3.2 REST（main/api/rest.py 新增）

- `GET /api/tasks/{task_id}/model-config`
  → `{ok, task_id, execution: {follows_global, route_entries}, inspection: {follows_global, route_entries}}`（只回存储态；生效链展示由前端用 `S.modelCatalog.routeEntries` + 覆盖合成）。
- `PATCH /api/tasks/{task_id}/model-config`
  body `{execution_route_entries?: [...], inspection_route_entries?: [...]}`（缺席＝该车道不动；空数组＝恢复跟随）。
  校验镜像全局写侧（schema.py:1291-1338）：direct→模型存在 404 `model_key_not_found` / 启用 409 `model_key_disabled`；组引用→组存在 404 `group_key_not_found`（组禁用允许保存，与全局 builder 行为一致，前端标徽标）；entry 形状非法 400。detail 带 `{code, model_key|group_key, message}`，文案口径照会话端点。
  成功→落盘 + 任务摘要 patch + 回 GET 同形。任务不存在 404。
- 服务层：`MainRuntimeService.get_task_model_config_payload` / `update_task_model_config`（经 `log_service.update_task_model_config`，照 `update_task_control` 结构）。**这两个方法不调用任何 config 写入口。**

### 3.3 运行时解析（worker）——v2 核心改动

(a) `main/runtime/model_route.py`：
- 抽出 entry 解析段为 `build_model_route_plan_from_entries(config, entries, *, revision) -> ModelRoutePlan`；`build_model_route_plan` 改为读 scope entries 后委托它（行为零变化，含回滚闸门与组降级）。
- 新增 `resolve_config_load_balance_groups(config) -> dict[str, ResolvedLoadBalanceGroup]`：遍历 `config.models.load_balance_groups` 整份，用既有 `_member_view_for` 解析成员视图。

(b) `runtime_service`：
- `_apply_model_route_plans` 的组注册改用 (a) 的整份配置组表（替换 `_load_balance_groups_from_plans`），启动与刷新同一处落地——「plan 引用的组必须在 balancer」不变量加强为「配置里的组全部在 balancer」，任务链引用任何已存在组都不会 `unknown_group`。漂移检查与重绑逻辑零改动（已核：只查自洽、只解绑被移出成员）。
- 注入 `node_runner.task_model_routes_resolver = (task_id, node_kind) -> ModelRoutePlan | None`：
  1. `store.get_task(task_id)` 读 metadata（与 `_pause_requested` node_runner.py:8539 同一热路径读法）；
  2. 归一覆盖；该车道无覆盖→None（回退全局 plan）；
  3. 检验无覆盖且全局检验为空→取本任务执行车道生效 plan（D5）；
  4. 有覆盖→`build_model_route_plan_from_entries`（组/direct 降级与回滚闸门自动同口径）；解析为空→None；
  5. 缓存 `{(task_id, kind, revision, entries签名): plan}`；配置刷新 bump revision 自然失效；只缓存 plan 不缓存任务行。

(c) `node_runner._model_routes_for(node)`：先问 resolver，None 回退现逻辑；`_model_refs_for` / `_model_route_entries_payload` / `_has_load_balance_routes` / `_load_balance_group_keys` 全部经它派生，无需另改。spawn 送审评审（4829）取本任务检验生效 plan 的 `candidate_model_keys`（组摊平），无覆盖走现逻辑；该车道仍不过准入（维持现状）。

(d) 准入/绑定/组观测代码零改动：覆盖 plan 与全局 plan 同类型，准入沿 plan routes 走 serveability/lease；节点粘性绑定按 node_id，覆盖切换后旧组绑定不被引用、终态清除（测试锁定）。心跳 `model_route_groups` 与 `/api/models/load-balance/status` 读数将包含未被全局链引用的已配置组——这是预期变化，文档注明。

## 4. 前端落点

- `org_graph.html`：958 行活跃节点胶囊前插入 `<button id="task-model-config-btn" class="task-detail-pill task-model-config-btn">`（图标 `sliders-horizontal` + 「模型配置」+ 覆盖圆点）；抽屉复用 `detail-drawer` 壳（同错误日志抽屉 1012 起）：标题「模型配置」、副标题「仅作用于本任务 · 组定义在模型页维护」、两节链编辑器、底部保存。
- 新文件 `g3ku/web/frontend/tasks/model-config-panel.js`：
  - 状态 `S.taskModelConfig`：taskId / open / loaded / saving / error / 两车道各自 followsGlobal + draftRefs（模型 key 或 `group:<key>` 记号，与模型页草稿同形）+ editing / dragFrom；切任务 reset。
  - 打开：GET model-config；跟随态画全局 `routeEntries[lane]`（组槽位用只读组卡：组名 + 成员 chips + 启停徽标）；覆盖态画可编辑链。
  - 编辑器：复用视觉类 `model-chain-slide / is-group-tile / model-chain-list / policy-chip` 与显示件 `modelDisplayTitle / modelRefItem / groupRefToken / isGroupRef / groupKeyFromRef`；**markup 属性全部 `data-task-model-*` 命名空间**，handler 自写（拖拽排序、移除、添加），不挂模型页的 `data-scope` / `data-group-edit` / `data-model-chain-action`。组卡读 **committed** `S.modelCatalog.loadBalanceGroups`（不经 `activeLoadBalanceGroups()` 的 roleEditing 分支），无编辑/删除按钮。
  - 添加选择器：两栏「模型」（目录搜索，禁用标灰不可选）/「负载均衡组」（已配置组列表 + 成员预览，禁用组标徽标但可选——与全局口径一致）；点击或拖入链尾。
  - 保存：refs→route_entries（照 org_graph_app.js:9048 转换口径）；草稿与全局链逐 entry 相同或为空⇒该车道发空数组恢复跟随；PATCH 两车道一次发；成功 toast + 圆点更新；编辑中收到摘要 patch 只刷服务端态不打回草稿（keepDraft）。**保存路径不调用 client_api/models.js 的任何写方法。**
  - 失效徽标：目录/组表查不到的成员标「已失效，发送时跳过」。
- `org_graph_app.js`：U 绑定 + 按钮/抽屉开合（照错误日志抽屉绑定位置）。
- `org_graph.css`：胶囊与抽屉样式写进本文件（加载中的样式文件）；头部恒一行，新胶囊吃固定宽、提示词吃剩余宽；<=900px 折成图标 + tooltip。抽屉内链列表滚动条按主题契约自绘。
- 装载注册：`org_graph.html` tasks 段（1813 附近）+ `tests/resources/support/frontend_sources.js` tasks bundle。
- `client_api` task 文件：`getTaskModelConfig` / `updateTaskModelConfig`。

## 5. 实施阶段（每批 commit，身份 `-c user.name=ZXY39 -c user.email=1045273950@qq.com`）

- P1 后端存储 + API：models.py 归一；log_service.update_task_model_config；runtime_service 两个 payload 方法；rest.py GET/PATCH；单测（归一、entry 形状 400、模型 404/409、组 404、禁用组可存、空数组恢复跟随、mutator 不丢 metadata 其他键、**保存前后 config.json 与 models.roles/loadBalanceGroups 字节不变**）。
- P2 运行时：model_route.py 抽 builder + from_entries + resolve_config_load_balance_groups；注册改整份配置组表；resolver 注入与缓存；_model_routes_for 接入；spawn 评审 refs 接入；单测（覆盖含组且该组不在全局链→准入正常授予不冻队列；未知组 entry 跳过；禁用组槽位耗尽前进；direct 失效摘除剩空回退；D5 继承含组；回滚闸门摊平；revision 失效；覆盖切换后旧绑定不被引用；执行覆盖不影响检验跟随；全局刷新后任务覆盖链组成员视图更新）。
- P3 前端：按钮 + 抽屉 + 面板模块（组 aware 编辑器）+ API client + 装载注册 + CSS；vm 单测（跟随态渲染组卡、覆盖态保存载荷 refs→entries、相等回退、圆点、切任务 reset、**不触碰 roleDrafts/组草稿**）；1280/900/600 三档实测头部与抽屉截图核对。
- P4 文档（`negi-architecture-maintenance` skill 判定后执行）：
  - `config-and-models.md` 新叶子「任务级模型链覆盖」：entry 同形、组只引用不定义、不反向同步、D4/D5、生效时机指针；「角色路由」节补一句「组注册来自整份配置」；「会话级模型链与固定模型」加指针。
  - `runtime-overview.md`「节点模型路由与准入绑定」：resolver 选择点 + 组注册口径变化 + 心跳读数含未引用组。
  - `web-and-admin.md` 新叶子「Task Model Config Panel Contract」（入口、抽屉、组只读、命名空间 handler、保存写点、keepDraft、失效徽标、不反向同步）；「Model Config Page And Admin Contract」与「Task Hall Action Contract」各加指针。
  - `README.md`：Topic Ownership 补归属；Debugging Entry Points 加一条（「任务链引用组后节点冻在排队 / 任务改了链仍走全局 / 任务面板改完全局变了」→ 三篇指针）。
  - `python scripts/check_architecture_docs.py` 过。
- P5 验收：ruff；smoke + 相关测试（test_model_route_chat_backend.py、新测试、前端 vm）；重启本机 web（work/start-web-logged.cmd，18790，D:\NEGI）；真任务端到端：默认跟随→执行链覆盖为「direct + 一个全局链未引用的组」→下一回合台账 `selected_model_key` 命中该组成员且 `route_admission_*` 正常→检验链单独覆盖（含组）→spawn 送审用覆盖链→恢复全局→**核对 config.json 字节未变**→其他任务/会话零影响。留 `work/live_task_model_config_acceptance.py`。

## 6. 验收条件

1. 「模型配置」按钮在活跃节点胶囊左侧，头部恒一行，三档宽度不溢出；覆盖态圆点可见。
2. 点开见两条生效链（组槽位画组卡 + 成员）；默认两车道「跟随全局」，全局链/组定义变更后面板与实跑同步跟随。
3. 覆盖链可任意混排已保存模型与已存在组；**引用一个全局链都没用到的组，节点照常授予成员、不冻队列**（台账可证）。
4. 单独改执行只影响本任务执行节点；检验跟随不变；改检验只影响检验节点与 spawn 送审；其他任务/会话零影响。
5. 保存后下一链轮边界生效，在飞请求不热切；worker 离线可保存。
6. **任务面板任何操作（含保存、拖拽、切跟随）前后，config.json 的 models.roles 与 models.loadBalanceGroups 字节不变**；组的增删改只能在模型页发生。
7. 失效成员/组按 D4 降级，面板画徽标；草稿与全局相同⇒自动恢复跟随；空数组/缺席键语义正确；metadata 其他键不被破坏。
8. ruff + smoke + 相关测试全绿；架构文档过 check 脚本。

## 7. 风险与边界

- R1 跨进程整行读改写：与暂停旗标先例同一竞态窗口（log_service.py:1947），低频操作员动作，沿用先例；实测咬到再升级 json_set 单语句。
- R2 热路径读：resolver 每次解析读一次任务行（_pause_requested 同量级）；plan 缓存挡重复构造；不在 resolver 里解析密钥。
- R3（v2 替换）组注册扩为整份配置：漂移检查只查自洽、configure 只解绑被移出成员（已核 169-253），多注册未引用组无副作用；心跳组扫描成本随已配置组数线性增长（实盘个位数组，可忽略）。回滚闸门 false 时覆盖链与全局链一起摊平，行为同口径。
- R4 缓存键必须含 revision + entries 签名两者，缺一会「改了不生效」或「刷新不跟随」。
- R5（v2 新增）前端写串道：模型页保存会整份替换组字典——任务面板若误挂模型页 handler 或误进 roleEditing 就会反向污染全局。判据：handler 命名空间隔离 + 保存只走任务 PATCH + P1/P3 各有一条「全局字节不变」测试。
- R6 头部宽度：新胶囊挤占提示词宽是设计内；1280/900/600 实测不换行不遮返回键。
- R7 文档叶子 ≤25KB，超则细分（README 规则 6）。

## 8. 交接段（上下文耗尽时从这里续）

- 本计划是唯一权威靶子（v2）；先读本文件再动代码。已完成阶段看 git log（task-model-config 前缀）。
- 关键锚点：路由选择 node_runner.py:3646；全局刷新与组注册 runtime_service.py:9454 / 15605；builder model_route.py:180；balancer configure/drift model_load_balancer.py:169-253；准入 serveability node_turn_controller.py:575；全局校验口径 schema.py:1291-1338；会话 API 先例 ceo_sessions.py:1230；写任务先例 log_service.py:1926；头部 org_graph.html:958；组卡 role-view.js:69；装载清单 frontend_sources.js:54。
- 本机验收：端口 18790、数据根 D:\NEGI、重启 work/start-web-logged.cmd；git 身份未配置，提交临时 `-c`。
- 共享工作树：只 add 自己的文件；禁 git stash；他人失败用例如实单列。