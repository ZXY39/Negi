# 检查点与上下文保存

## 何时创建检查点

### 自动触发
- 每次调用 `submit_next_stage` 时（使用 `completed_stage_summary` 和 `key_refs`）
- 执行高风险操作前：
  - 批量修改或删除文件
  - 派生多个高成本子节点
  - 调用可能产生大量输出的工具
  - 重构核心代码或架构
- 进入临界区（>80% 上下文使用率）后每步操作前

### 手动触发
- 用户明确要求保存当前状态
- 检测到异常行为或连续失败

## 检查点内容结构

```yaml
checkpoint:
  timestamp: "ISO 8601"
  task_id: "task:xxx"
  node_id: "node:xxx"
  stage_id: "stage:xxx"
  stage_index: N
  goal: "当前阶段目标简述"
  completed_work:
    - "已完成工作项1"
    - "已完成工作项2"
  key_decisions:
    - "关键决策1及其理由"
    - "关键决策2及其理由"
  active_references:
    - {ref: "artifact:xxx", note: "引用说明"}
    - {path: "/abs/path", note: "文件说明"}
  pending_work:
    - "待完成项1"
    - "待完成项2"
  context_version: N
  next_action: "下一步行动建议"
```

## 与 G3KU Stage 机制的集成

`submit_next_stage` 是最正式的检查点载体：
- `completed_stage_summary` = 已完成工作总结
- `key_refs` = 关键引用列表
- `stage_goal` = 新阶段目标（相当于检查点后的下一步）

阶段切换天然提供了上下文裁剪的最佳时机：旧阶段内容可以被总结归档，新阶段从轻载开始。

## 保存策略

- **内存检查点**：通过 `submit_next_stage` 和 stage 机制保存在运行时
- **持久检查点**：关键决策和里程碑应通过 `memory` skill 写入长期记忆
- **文件检查点**：对于特别重要的状态，可写入 `.g3ku/checkpoints/` 目录

## 检查点版本管理

- 同一阶段内的多次检查点使用递增的 `context_version`
- 保留最近 3 个版本，超过后归档旧版本
- 阶段切换时重置版本计数
