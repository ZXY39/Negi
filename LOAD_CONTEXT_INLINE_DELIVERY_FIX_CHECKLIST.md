# Load Context Inline Delivery Fix Checklist

## Problem

`load_tool_context`、`load_skill_context` 以及同类“直接加载正文/上下文”的工具，当前已经在返回值里提供了完整正文和分层摘要，例如 `content`、`l0`、`l1`、`path`。但在后续运行时里，这些大体积 tool result 仍会被统一的内容外置逻辑再次包装成 `content_ref` / artifact 摘要，导致 agent 拿到的不是正文，而是“先去 `content.search/open` 再读”的二次跳转提示。

这会破坏这类工具的核心语义：

- 这类工具的目标就是“把正文直接加载进当前 agent 回合”
- agent 不应该为它们额外执行一次 `open`
- generic tool-result externalization 不应该覆盖 direct-load tool 的语义

## Current Hotspots

- `main/service/runtime_service.py`
  - `load_skill_context_v2`
  - `load_tool_context_v2`
  - 已经返回完整 `content/l0/l1/path` 载荷，本身不是问题根源
- `g3ku/content/navigation.py`
  - `_should_keep_inline_tool_result`
  - `maybe_externalize_text`
  - `externalize_for_message`
  - 当前只对白名单类型的 tool result 保持内联，`load_*_context` 不在其中
- `g3ku/runtime/tool_bridge.py`
  - `_externalize_tool_result`
  - CEO/runtime tool bridge 会把 tool 输出统一送进 externalization
- `g3ku/runtime/frontdoor/ceo_runner.py`
  - `_externalize_tool_result`
  - frontdoor CEO 路径存在同样的二次外置风险
- `main/runtime/react_loop.py`
  - `_execute_tool`
  - `_externalize_message_content`
  - 主 ReAct 回路同样会把 tool 输出送进同一套外置逻辑

## Fix Checklist

### 1. 明确 direct-load tool contract

- [ ] 把 `load_tool_context`、`load_skill_context` 这类工具定义为“正文直达 agent”的 direct-load tool，而不是普通大结果工具。
- [ ] 明确规则：如果工具的语义是“加载正文供当前回合立即消费”，generic externalization 不能把它改写成 `content_ref`。
- [ ] 不要只用硬编码工具名作为唯一判断条件；需要同时支持“等直接加载内容的工具”。

### 2. 在内容外置层增加统一豁免判定

- [ ] 在 `g3ku/content/navigation.py` 增加一个集中判定，识别“应保持内联的 direct-load tool result”。
- [ ] 优先基于 payload 语义识别，而不是散落在多个运行时入口里各写一份特判。
- [ ] 建议识别信号至少覆盖以下特征中的一组：
  - `ok=true`
  - 存在 `content` 正文
  - 存在 `level/l0/l1`
  - `uri` 为 `g3ku://skill/...` 或 `g3ku://resource/tool/...`
- [ ] 让这个判定在 `INLINE_CHAR_LIMIT` / `INLINE_LINE_LIMIT` 检查之前生效，避免正文因为够长而被再次外置。
- [ ] 保持已有例外规则不退化，例如 `filesystem.open` / `content.open` 的 excerpt 仍可继续内联。

### 3. 保持外置策略的边界清晰

- [ ] 不要顺手把所有大 JSON tool result 都改成内联；修复范围应限于 direct-load 语义。
- [ ] `exec` 长 stdout、超长搜索结果、日志类输出、patch artifact、显式 `content_ref` 输出仍应继续走外置机制。
- [ ] 如果工具本来就返回 `content_ref` 或 artifact 引用，保持原行为，不做“反外置”处理。

### 4. 统一三个运行时入口的行为

- [ ] 验证 `g3ku/runtime/tool_bridge.py` 走到 `externalize_for_message(...)` 时，会命中新的 direct-load 内联规则。
- [ ] 验证 `g3ku/runtime/frontdoor/ceo_runner.py` 走到 `externalize_for_message(...)` 时，也会命中同一规则。
- [ ] 验证 `main/runtime/react_loop.py` 里的工具执行路径也复用同一规则，不出现行为漂移。
- [ ] 避免在三个入口里分别写硬编码豁免名单；应让它们共享 `g3ku/content/navigation.py` 的集中判断。

### 5. 回归测试补齐

- [ ] 在 `tests/resources/test_resource_runtime_smoke.py` 增加用例：
  - 大体积 `load_skill_context_v2` 风格 payload 经过 `externalize_for_message(...)` 后仍保持内联
  - 大体积 `load_tool_context_v2` 风格 payload 经过 `externalize_for_message(...)` 后仍保持内联
- [ ] 断言结果不是 `content_ref` envelope，且原始 `content/l0/l1/path` 仍可直接被解析。
- [ ] 为 `ToolExecutionBridge` 增加回归测试，确认 `execute_named_tool(...)` 对 direct-load tool 返回的是原始正文载荷，而不是 artifact 摘要。
- [ ] 为 `ReActToolLoop` 增加回归测试，确认 `_execute_tool(...)` 后写回的 `tool_message.content` 仍是 direct-load payload。
- [ ] 保留现有正向保护测试：
  - `filesystem.open` 小/中等 excerpt 仍保持内联
  - `exec` 长 stdout 仍会 externalize
  - 普通超长 tool result 不会因为这次修复而全部绕过外置

### 6. 运行后验收

- [ ] 使用一个超过 `INLINE_CHAR_LIMIT` 的 skill/tool body 做复现样本，先确认当前版本会被改写为 `content_ref`。
- [ ] 修复后再次执行 `load_skill_context(...)` / `load_tool_context(...)`，确认 agent 下一轮直接拿到正文 JSON。
- [ ] 确认 agent 不再被迫追加 `content.open` / `filesystem.open` 来读取这类工具结果。
- [ ] 确认最近工具结果摘要、会话历史压缩、前端 artifact 展示没有因为内联正文而出现异常。

## Suggested Implementation Direction

- 首选方案：在 `g3ku/content/navigation.py` 新增一个类似 `_should_keep_inline_direct_load_tool_result(...)` 的集中 helper，并让 `_should_keep_inline_tool_result(...)` 调用它。
- 不建议方案：在 `tool_bridge.py`、`ceo_runner.py`、`react_loop.py` 分别对 `load_tool_context` / `load_skill_context` 写散落特判。

## Acceptance Criteria

- `load_tool_context` / `load_skill_context` 返回的完整正文会直接进入 agent 当前回合
- 不再出现“工具刚加载了正文，但 agent 还要额外 open artifact”的重复读取
- 普通大结果的 externalization 策略保持原有行为
- 三条运行时路径的表现一致
