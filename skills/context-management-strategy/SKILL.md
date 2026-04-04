# Context Management Strategy for G3KU

**Version**: 1.0.0

## Purpose

为 G3KU Agent 提供统一的上下文管理策略，涵盖上下文保存、恢复、预算控制、范围标注、上下文驱动开发和跨阶段连续性。本 skill 不替代 long-term memory system，而是在 context window 层面提供操作纪律。

## Core Principles

1. **Context is finite, memory is persistent**: 上下文窗口有限，长期记忆持久。能用 memory 存储的，不占 context window。
2. **Save before destroy**: 任何可能丢失上下文的操作前，必须先保存 checkpoint。
3. **Trim purposefully**: 裁剪上下文时保留完整执行轨迹的关键节点，而非盲目截断。
4. **Scope before diving**: 开始复杂任务前，先设定范围标注，避免上下文无限制膨胀。
5. **Recover with evidence**: 恢复上下文时，不仅恢复摘要，还需保留关键证据引用。
6. **Continuity is designed**: 跨阶段连续性是有意设计的产物，不是自然发生的。

## Quick Reference

| Scenario | Action | Reference |
|----------|--------|-----------|
| 上下文快满，需要释放空间 | Budget & Trim | `references/budget-and-trimming.md` |
| 即将做销毁性操作 | Checkpoint & Save | `references/checkpoint-and-save.md` |
| 上下文已丢失，需要重建 | Recovery | `references/recovery.md` |
| 多阶段任务的连续性管理 | Continuity | `references/continuity.md` |
| 与记忆/任务系统协作 | Integration | `references/integration.md` |
| 候选 skill 对比与取舍依据 | Comparison | `references/candidate-comparison.md` |

## Skill Boundaries

### This skill covers:
- Context window budget thresholds and trimming strategies
- Checkpoint creation before destructive operations
- Structured context recovery after loss
- Scope tagging to prevent context sprawl
- Phase continuity design for multi-stage tasks
- Integration with G3KU memory and task systems

### This skill does NOT cover:
- Long-term memory storage and retrieval (see L1/L2 memory system)
- Task scheduling and cron management
- External tool execution strategies
- Code review or implementation standards

## When to Apply

**Always apply** when:
- Context window usage exceeds 70%
- Switching between substantially different task domains
- About to perform stage transition or checkpoint operation
- Recovering from context loss or interruption
- Starting a multi-phase complex task

**Consider applying** when:
- Working on tasks with more than 3 phases
- Using tools that generate large output
- Switching between execution and acceptance modes

## Usage

1. Read this SKILL.md for principles and quick decisions.
2. Navigate to the specific `references/` file for detailed workflow.
3. Follow the workflow steps and produce required artifacts.
4. Log memory entries for checkpoints and recovery points.
5. At stage transition, summarize what was retained and what was discarded.

## Maintenance

- This skill is maintained by G3KU Team.
- Structural changes require review.
- Minor tuning (wording, examples, formatting) is allowed without review.
- See DESIGN.md for the design rationale behind this skill.
