# Checkpoint & Save Reference

## Purpose

指导 G3KU Agent 在关键操作前创建结构化 checkpoint，确保上下文可恢复、工作不丢失、状态可追溯。

## When to Create Checkpoints

**Mandatory (always checkpoint before):**
- Stage transitions (`submit_next_stage`)
- Destructive operations (file deletion, bulk edits, repo resets)
- Submitting final results (`submit_final_result`)
- Spawning child nodes for complex parallel work
- Any operation that will reset or replace the active context

**Recommended (checkpoint when practical):**
- After completing a significant milestone within a stage
- Before switching to a substantially different task domain
- When tool output is unpredictably large
- Before extended operations that may exceed stage budget

## Checkpoint Structure

A checkpoint MUST include the following structured elements:

### Minimal Checkpoint
```
Checklist (mandatory fields):
- [ ] stage_goal captured
- [ ] completed_work summarized
- [ ] remaining_work listed
- [ ] key_artifacts referenced (paths or refs)
- [ ] key_decisions noted
- [ ] recovery_instructions defined
```

### Extended Checkpoint (for complex tasks)
In addition to minimal fields:
- [ ] current_file_states (what files were modified)
- [ ] pending_operations (what was about to happen)
- [ ] rollback_instructions (how to undo last operation)
- [ ] memory_log_entries (what was logged to L1/L2)
- [ ] child_node_context (what spawned children need to know)

## Workflow

### Step 1: Decide Scope
- Is this a stage transition checkpoint or an in-stage safety checkpoint?
- What is the minimum information needed to resume from this point?

### Step 2: Capture State
- Summarize what has been accomplished
- List exactly what remains to be done
- Reference all artifacts by path or content ref
- Note any non-obvious state (environment, tool results kept in memory)

### Step 3: Log to Memory
- Create memory entries for checkpoint key data
- Use clear tags: `checkpoint`, `<task_id>`, `<topic>`
- Include artifact references and file paths

### Step 4: Write Checkpoint Artifact
- For file-based checkpoints: write to `temp/checkpoints/<task_id>-<timestamp>.md`
- For context-internal checkpoints: include as a structured block in the next tool call
- Reference the checkpoint location in the stage transition summary

### Step 5: Verify Recoverability
- Ask: "Given ONLY this checkpoint and memory, could I resume this work?"
- If yes, proceed with the operation
- If no, add missing information and retry verification

## Rollback

If an operation corrupts or destroys needed context:

1. Stop immediately—do not compound the damage
2. Retrieve the most recent checkpoint
3. Compare current state with checkpoint state
4. Restore what is lost, or recreate from checkpoint instructions
5. Log the recovery action to memory for future reference

## Anti-Patterns

- Do NOT create empty or placeholder checkpoints
- Do NOT assume "I'll remember this" without logging it
- Do NOT skip checkpoints for operations you think are "safe"—safety is assessed by impact, not intent
- Do NOT store checkpoints only in context window—they must be persisted (memory or file)
- Do NOT overwrite older checkpoints until newer ones are verified

## Integration with Stage Transitions

When transitioning stages via `submit_next_stage`:
- The `completed_stage_summary` serves as the checkpoint narrative
- The `key_refs` field captures artifact references
- The new `stage_goal` defines recovery boundaries
- If stage budget was exhausted, the checkpoint MUST include what was NOT yet done
