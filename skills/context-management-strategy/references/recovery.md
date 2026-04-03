# Recovery Reference

## Purpose

指导 G3KU Agent 在上下文丢失或中断后，有条理地恢复工作状态，减少重复劳动，快速回到 productive 状态。

## Recovery Scenarios

| Scenario | Signal | Primary Recovery Path |
|----------|--------|----------------------|
| Context window reset | Context appears empty or unrelated | Memory search + checkpoint retrieval |
| Session interruption | Conversation breaks, reconnects | Memory search for last checkpoint |
| Stage transition loss | New stage but previous context not carried | Previous stage summary + key_refs |
| Tool error corruption | Tool output garbled or wrong | Rerun tool + compare with memory |
| Human handoff context | New human takes over | Full recovery package (this section) |

## Recovery Workflow

### Step 1: Diagnose
- What was I working on when context was lost?
- Search memory for: `checkpoint`, `task_id`, `stage_goal` keywords
- Identify the most recent checkpoint and its timestamp
- Determine what work was in-progress vs. completed

### Step 2: Reconstruct State
- Retrieve the latest checkpoint (from memory or file)
- Cross-reference with `key_refs` from the most recent stage transition
- Identify which artifacts still exist on disk (verify file paths)
- Note which tool results need to be rerun

### Step 3: Rebuild Context
- Load the stage goal and remaining work
- Load key artifact references (paths, refs, URLs)
- Load the immediate next action to take
- Load any constraints or rules that were active (completion_contract, execution_policy)

### Step 4: Verify Completeness
Can I answer these questions?
- What is the current stage goal?
- What has already been completed in this stage?
- What remains to be done?
- What is the very next action I should take?
- Are there any constraints I must respect?

If any answer is "I don't know," search memory again or retrieve from checkpoint.

### Step 5: Resume
- Execute the identified next action
- Create a new checkpoint noting the recovery point
- Continue with the stage plan

## Recovery Priority Order

When recovering, restore information in this order:

1. **Stage goal and completion contract** — without these, no direction
2. **Remaining work items** — what needs to be done next
3. **Key artifact paths** — where to find what was produced
4. **Active constraints** — rules that must not be violated
5. **Completed work summary** — what is already done (for context, not redo)
6. **Tool call history** — only if rerunning tools is needed

## What NOT to Recover

- Do NOT attempt to reconstruct every prior message verbatim
- Do NOT re-read every tool output that was already processed
- Do NOT redo work that was completed and verified
- Do NOT recover context that is now obsolete (old stage goals, superseded plans)

## Recovery Artifact Template

When creating a recovery package (for handoff or self-recovery):

```
## Recovery Package for <task_id>/<node_id>

**Current Stage**: <stage_number>
**Stage Goal**: <concise goal>
**Completed in this stage**: <summary>
**Remaining work**: <list>
**Key artifacts**:
  - <path or ref>: <description>
  - <path or ref>: <description>
**Next immediate action**: <specific action>
**Active constraints**: <list critical rules>
**Memory search terms**: <keywords to find more context>
**Known issues**: <anything currently broken or uncertain>
```

## Prevention (Better Than Recovery)

- Always create checkpoints before destructive operations
- Log to memory after completing significant work
- Keep stage summaries detailed enough for recovery
- Reference artifacts by absolute path, not relative assumptions
