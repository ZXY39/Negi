# Dream 可读化：服务端 dream_scope 增强 + 前端结构化渲染

## 背景

记忆管理页抽屉对 `op=dream` 的行把队列载荷原样投成「原始请求内容」，而 dream 载荷是运行时
生成的整理地址（`cluster_key / memory_ids / note_refs / reason / base_revision`），不是自然
语言，读起来是一串 id 与枚举，可读性差。write/delete 行是指令所以可读，dream 行是「整理哪一簇」
的地址簿。真正喂给记忆 agent 的完整行卡不在载荷里（按 id 从 sqlite 展开），落在
`request_artifact_paths`。

目标：dream 行在队列/已处理/失败三个读面显示「用户可读的整理范围」，原始 JSON 折叠保留作取证。
**只改显示层，不动存储**：`payload_text` 是重放/审计载体（失败重试靠它回队），不能删。

## 方案（已确认：服务端增强 + 前端结构化渲染）

id→正文/行号/退场/缺失 的解析放服务端（sqlite 与两棵 note 树的唯一权威），三个分页口共用一处；
前端只渲染结构化数据。纯前端映射会对已退场行开天窗（`/memory/current` 不含它们），故不采用。

### 服务端 `g3ku/agent/memory_agent_runtime.py`

新增 `MemoryManager.dream_scope_view(payload_text) -> dict | None`：
- 解析 JSON；非 dict 或无 `cluster_key` 返回 None；任何异常吞掉返回 None（读面永不因畸形载荷挂）。
- 返回 `{cluster_key, reason, base_revision, memories[], notes[]}`。
- `memories[]` 逐 id 查 `repo.list_memories()`（含退场行）：
  `{memory_id, line(当前常驻行号,退场/缺失为 None), body(memory_body 全文), minimal_memory,
    note_ref, retired_into(consolidated_into), missing(库里查无), updated_at}`。
  行号用 `resident_lines()` 的 id→序号映射。
- `notes[]` 逐 ref：`{ref, kind(ordinary/category/unknown), exists}`，用
  `_ordinary_note_names()/_category_note_names()` 判 kind、`_note_path` try 判 exists。

三个投影口对 dream 附加 `dream_scope`（用**截断前**的原始载荷计算）：
- `_project_queue_row_for_read`：`op=="dream"` 时从 `row["payload_text"]` 算。
- `_project_processed_row_for_read`：`op/source_op=="dream"` 时从 `payload_texts[0]` 算。
- `_project_failed_record_for_read`：`op=="dream"` 时从 `items[0]["payload_text"]` 算，挂记录级。

REST 层（`main/api/admin_rest.py`）不改：字段随 manager 投影自然流出。

### 前端 `g3ku/web/frontend/memory/view.js`

- 新增 `memoryDreamReasonLabel(reason)`：recent_revision_family→最近修订族、
  doctor_findings→医生问题族、note_round_robin→note 轮转，其余原样。
- 新增 `memoryDreamTriggerLabel(item)`：trigger_source/trigger_sources 含 dream_cli→手动整理、
  dream_scheduler→自动调度。
- 新增 `renderMemoryDreamScope(scope, item)`：头部（触发 + 选择依据），记忆行列表
  （行号或「已退场→分类」/「已删除」+ 正文全文 + note 归属 chip），note 列表（复用
  `renderMemoryNoteOwnershipChip`/`renderMemoryNoteRefChip`，可点开 note 窗）。
- `openMemoryDetailPreview`：dream 行把 `dream_scope` 与原始 blocks 一并放进 preview；
  `renderMemoryDetailPreview` 对 dream 把主区标题改「整理范围」，渲染结构化 scope，
  原始 blocks 收进 `<details>原始载荷（取证）</details>`。次区（无变更原因/变更内容/错误历史）不动。
- 队列卡 `renderMemoryQueueCard`：dream 行正文预览改「整理簇：N 条记忆 / M 份笔记 · <reason>」。

### 测试

- 后端：`tests/resources/test_memory_dream_mode.py` 增用例，断言 `dream_scope_view` 与三个分页口
  对 dream 行带 `dream_scope`，覆盖常驻/退场/缺失行与 ordinary/category note。
- 前端：`tests/resources/test_memory_management_frontend.py` 增 node 用例，导出
  `renderMemoryDreamScope`/`memoryDreamReasonLabel`，断言结构化输出含行号/退场标记/note chip，
  且不含裸 JSON 首屏。

### 文档

`docs/architecture/web-and-admin.md`「Memory Management Page And Admin Contract」就地补一条：
dream 行读侧 `dream_scope` 增强 + 抽屉结构化「整理范围」+ 原始载荷折叠 + 队列卡预览。
不改 README（无新子系统/新 owning doc）。

## 验证与提交

- `python -m ruff check .`；跑 `test_memory_dream_mode.py`、`test_memory_management_frontend.py`、
  `test_resource_runtime_smoke.py`。
- 分批提交：后端+后端测试一个 commit；前端+前端测试+文档一个 commit。英文提交信息，临时身份。
- 重启服务（graceful reap + 原方式后台拉起），用 `/api/memory/processed` 验证 dream 行带 dream_scope。

## 不做

- 不删/不改 `payload_text` 存储；不动重放/审计/取全文车道。
- 不改 processed/failed 卡片芯片逻辑（结论已由 chips/次区表达）。
- 不新增 REST 路由。