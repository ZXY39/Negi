# React Loop 分析文档

## 1. 文件概述

- **文件路径**: `main/runtime/react_loop.py`
- **文件大小**: 108 KB
- **总行数**: 2350 行
- **语言**: Python (asyncio)

## 2. 主要功能

`react_loop.py` 是 G3KU 系统的核心 ReAct (Reasoning + Acting) 循环引擎，负责：

1. **ReAct 循环执行**: 实现思考-行动循环，允许 AI 在多个迭代中调用工具并根据结果进行推理
2. **工具调用管理**: 解析模型输出的工具调用请求，支持并行执行多个工具
3. **消息管理与状态维护**: 管理对话上下文、工具调用状态、循环中断等
4. **阶段门控 (Stage Gating)**: 支持多阶段任务执行，每个阶段有独立的提示词配置
5. **熔断机制**: 实现 `RepeatedActionCircuitBreaker` 防止无限循环同一 action
6. **错误恢复与重试**: 完善的错误处理和恢复机制

## 3. 关键类定义

### 3.1 RepeatedActionCircuitBreaker

**位置**: 文件开头附近

**功能**: 熔断器，防止 AI 重复执行相同的 action 导致无限循环

**核心方法**:
- `__init__()`: 初始化熔断器参数（阈值、最大历史记录数）
- `should_break()`: 判断是否应该中断当前循环
- `record_action()`: 记录已执行的 action

**属性**:
- `max_history_size`: 最大历史记录数
- `threshold`: 触发熔断的重复次数阈值

### 3.2 ReActToolLoop

**位置**: 文件主体部分 (约 300+ 行开始)

**功能**: ReAct 循环的核心执行器，管理整个思考-行动流程

**初始化参数**:
```python
def __init__(
    self,
    llm: Any,                          # 大语言模型客户端
    tools: List[Tool],                 # 可用工具列表
    tool_loop_context: Any,            # 循环上下文
    tool_loop_settings: Any,           # 循环设置
    tool_executor: Callable,           # 工具执行器
    message_manager: Any,              # 消息管理器
    token_counter: Any,                # token 计数器
    default_headers: Optional[Dict],   # 默认请求头
    stage_settings: Optional[Dict]     # 阶段设置
):
```

## 4. 关键方法定义

### 4.1 核心公开方法

| 方法名 | 功能描述 |
|--------|----------|
| `run()` | ReAct 循环的主入口方法，协调整个思考-行动流程 |
| `_execute_tool_calls()` | 并行执行多个工具调用 |
| `_prepare_messages()` | 准备发送给模型的消息 |
| `_render_tool_message_content()` | 渲染工具返回的消息内容 |
| `_extract_tool_calls_from_xml_pseudo_content()` | 从模型输出中提取工具调用 |
| `_resume_pending_tool_turn_if_needed()` | 处理pending状态的工具调用 |
| `close()` | 清理资源 |

### 4.2 阶段门控方法 (Stage Gating)

| 方法名 | 功能描述 |
|--------|----------|
| `_stage_prompt_prefix()` | 获取当前阶段的提示词前缀 |
| `_stage_max_turns()` | 获取当前阶段的最大轮次限制 |
| `_stage_should_break()` | 判断当前阶段是否应该结束 |
| `_stage_should_continue()` | 判断是否应该继续到下一阶段 |
| `_stage_tool_whitelist()` | 获取当前阶段允许的工具白名单 |

### 4.3 错误处理方法

| 方法名 | 功能描述 |
|--------|----------|
| `_should_retry_on_error()` | 判断错误是否应该重试 |
| `_get_error_recovery_message()` | 获取错误恢复消息 |
| `_record_error_for_circuit_breaker()` | 记录错误用于熔断判断 |
| `_handle_specific_error()` | 处理特定类型的错误 |

### 4.4 XML 解析方法

| 方法名 | 功能描述 |
|--------|----------|
| `_try_parse_xml_tool_calls()` | 尝试解析 XML 格式的工具调用 |
| `_try_parse_json_tool_calls()` | 尝试解析 JSON 格式的工具调用 |
| `_extract_using_regex()` | 使用正则表达式提取工具调用 |
| `_extract_xml_tool_call()` | 解析单个 XML 工具调用块 |

### 4.5 工具调用相关方法

| 方法名 | 功能描述 |
|--------|----------|
| `_convert_tool_calls_for_executor()` | 转换工具调用格式给执行器 |
| `_should_parallelize_tool_calls()` | 判断是否应该并行执行 |
| `_sort_tool_calls_by_priority()` | 按优先级排序工具调用 |
| `_validate_tool_call_arguments()` | 验证工具调用参数 |
| `_apply_tool_whitelist()` | 应用工具白名单过滤 |

### 4.6 消息处理方法

| 方法名 | 功能描述 |
|--------|----------|
| `_add_to_messages()` | 添加消息到上下文 |
| `_get_system_prompt()` | 获取系统提示词 |
| `_should_truncate_messages()` | 判断是否需要截断消息 |
| `_truncate_messages_if_needed()` | 截断超长消息 |

### 4.7 状态管理方法

| 方法名 | 功能描述 |
|--------|----------|
| `_update_loop_state()` | 更新循环状态 |
| `_save_state()` | 保存当前状态 |
| `_load_state()` | 加载保存的状态 |
| `_get_current_turn()` | 获取当前轮次 |
| `_increment_turn()` | 增加轮次计数 |

## 5. 对外接口

### 5.1 类级别接口

```python
class ReActToolLoop:
    """ReAct 循环执行器"""
    
    async def run(
        self,
        system_prompt: str,
        user_message: str,
        extra_messages: Optional[List[Dict]] = None
    ) -> Tuple[str, List[Dict]]:
        """
        执行 ReAct 循环
        
        Returns:
            (final_response, conversation_messages)
        """
        
    async def close(self) -> None:
        """清理资源"""
```

### 5.2 数据结构接口

**Tool Call 格式**:
```python
{
    "id": str,                    # 唯一标识
    "name": str,                  # 工具名称
    "arguments": Dict,            # 工具参数
    "type": str                   # 工具类型
}
```

**Tool Result 格式**:
```python
{
    "id": str,                    # 对应 tool_call 的 id
    "name": str,                  # 工具名称
    "result": Any,                # 执行结果
    "error": Optional[str],       # 错误信息
    "is_error": bool              # 是否出错
}
```

### 5.3 配置接口

**ToolLoopSettings**:
- `max_turns`: 最大循环轮次
- `timeout`: 单次工具调用超时
- `retry_on_error`: 是否在错误时重试
- `parallel_tool_calls`: 是否并行执行工具
- `max_concurrent_tools`: 最大并发工具数

**StageSettings**:
- `max_turns`: 当前阶段最大轮次
- `tool_whitelist`: 允许的工具列表
- `prompt_prefix`: 阶段提示词前缀

## 6. ReAct 循环机制

### 6.1 循环流程图

```
┌─────────────────────────────────────────────────────────────────┐
│                        开始 ReAct 循环                          │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    1. 准备消息 (prepare_messages)               │
│    - 系统提示词 + 历史消息 + 用户消息                             │
│    - 阶段门控检查                                                │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    2. 调用 LLM (llm.chat)                       │
│    - 发送消息给大语言模型                                        │
│    - 获取模型响应                                                │
└─────────────────────────────────────────────────────────────────┐
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    3. 提取工具调用 (extract_tool_calls)         │
│    - 尝试 XML 解析                                              │
│    - 尝试 JSON 解析                                             │
│    - 正则表达式提取备选                                         │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────┴──────────────────┐
              │                                         │
              ▼                                         ▼
    ┌─────────────────────┐               ┌─────────────────────┐
    │   有工具调用请求     │               │  无工具调用（结束）  │
    └─────────────────────┘               └─────────────────────┘
              │                                         │
              ▼                                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    4. 验证工具调用                               │
│    - 参数验证                                                    │
│    - 工具白名单过滤                                              │
│    - 权限检查                                                    │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    5. 执行工具调用                               │
│    - 决定并行/串行执行                                           │
│    - 并行执行: asyncio.gather                                   │
│    - 串行执行: 逐个 await                                        │
│    - 收集结果                                                    │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    6. 处理工具结果                               │
│    - 渲染消息内容                                                │
│    - 错误处理                                                    │
│    - 熔断器记录                                                  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────┴──────────────────┐
              │                                         │
              ▼                                         ▼
    ┌─────────────────────┐               ┌─────────────────────┐
    │   未达到最大轮次     │               │  达到最大轮次       │
    │   + 未触发熔断       │               │  或触发熔断         │
    └─────────────────────┘               └─────────────────────┘
              │                                         │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
                    返回步骤 1: 准备下一轮消息
```

### 6.2 并行工具执行

`_execute_tool_calls()` 方法实现了并行执行：

```python
async def _execute_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolResult]:
    if self._should_parallelize_tool_calls(tool_calls):
        # 并行执行
        tasks = [self._execute_single_tool_call(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    else:
        # 串行执行
        results = []
        for tc in tool_calls:
            result = await self._execute_single_tool_call(tc)
            results.append(result)
    return results
```

### 6.3 阶段门控 (Stage Gating)

每个阶段可以配置：
- `max_turns`: 当前阶段最大允许的循环轮次
- `tool_whitelist`: 允许使用的工具列表
- `prompt_prefix`: 额外的提示词前缀

阶段转换逻辑在 `_stage_should_continue()` 中实现。

### 6.4 熔断机制

`RepeatedActionCircuitBreaker` 防止无限循环：

1. 记录每个 action 的执行历史
2. 当同一 action 连续执行次数超过阈值时触发熔断
3. 熔断后返回错误信息，终止循环

### 6.5 工具调用格式

ReActLoop 支持多种工具调用格式：

**XML 格式** (首选):
```xml
<tool_call>
<tool name="filesystem">
<param name="action">list</param>
<param name="path">/tmp</param>
</tool>
</tool_call>
```

**JSON 格式**:
```json
{
  "tool_call": {
    "name": "filesystem",
    "arguments": {"action": "list", "path": "/tmp"}
  }
}
```

## 7. 完整方法列表 (50+ 方法)

### 异步方法 (async def) - 44个

核心循环: `run`, `_execute_tool_calls`, `_execute_single_tool_call`, `close`

消息处理: `_prepare_messages`, `_add_to_messages`, `_get_system_prompt`, `_truncate_messages_if_needed`

工具调用: `_extract_tool_calls_from_xml_pseudo_content`, `_try_parse_xml_tool_calls`, `_try_parse_json_tool_calls`, `_convert_tool_calls_for_executor`, `_validate_tool_call_arguments`, `_apply_tool_whitelist`

渲染: `_render_tool_message_content`, `_render_tool_result_as_content`

阶段控制: `_stage_prompt_prefix`, `_stage_max_turns`, `_stage_should_break`, `_stage_should_continue`, `_stage_tool_whitelist`

错误处理: `_should_retry_on_error`, `_get_error_recovery_message`, `_handle_specific_error`, `_record_error_for_circuit_breaker`

状态: `_update_loop_state`, `_get_current_turn`, `_increment_turn`, `_save_state`, `_load_state`

工具相关: `_should_parallelize_tool_calls`, `_sort_tool_calls_by_priority`, `_resume_pending_tool_turn_if_needed`

XML/正则: `_try_parse_xml_tool_calls`, `_extract_using_regex`, `_extract_xml_tool_call`

### 同步方法 (def) - 10+个

工具解析: `_parse_json_tool_call_arguments`, `_parse_xml_tool_call_arguments`

工具查询: `_get_tool_by_name`, `_get_tools_schema`

验证: `_validate_tool_name`

其他: 多个辅助方法和属性访问器

## 8. 总结

`react_loop.py` 是 G3KU 系统的核心组件，实现了完整的 ReAct (Reasoning + Acting) 循环机制：

1. **功能完整**: 支持单轮/多轮对话、并行工具执行、阶段门控、熔断保护
2. **接口清晰**: 公开方法少而精，依赖注入便于测试
3. **健壮性强**: 完善的错误处理、重试机制、状态恢复
4. **可扩展性好**: 多层抽象便于添加新功能（如新的工具调用格式）
5. **性能优化**: 支持并行执行、消息截断等优化手段

该模块是理解 G3KU 系统架构的关键入口点。