# Budget & Trimming Reference

## Purpose

指导 G3KU Agent 在上下文窗口接近容量时，有策略地预算和裁剪上下文，保留关键信息，释放空间继续工作。

## Trigger Conditions

- Context window usage exceeds 70%
- Tool output is expected to be large (e.g., file listing, search results)
- Multiple phases of work remaining in current stage

## Budget Thresholds

| Level | Usage | Action |
|-------|-------|--------|
| Green | < 50% | Normal operation |
| Yellow | 50-70% | Begin summarizing completed work for memory |
| Orange | 70-85% | Create checkpoint; trim completed sections |
| Red | 85-95% | Aggressive trimming; save all critical artifacts |
| Critical | > 95% | Emergency save; minimal context for immediate next step |

## Trimming Strategy (Priority Order)

### What to TRIM (keep for later in memory):
1. Already-completed tool outputs and their raw data
2. Intermediate reasoning steps for resolved sub-problems
3. Duplicate or near-duplicate information
4. Outdated context from earlier phases that is no longer relevant
5. Verbose error messages (keep only the essential error)

### What to PRESERVE in context:
1. Current stage goal and remaining work items
2. Active tool call results needed for next steps
3. Key decisions made and their rationale
4. Artifacts/file paths that are inputs to remaining work
5. Structured plans or checklists guiding current work

## Workflow

### Step 1: Assess
- Estimate current context usage (visual or from system hints)
- List what is currently in context vs. what is in memory

### Step 2: Checkpoint
- Create a checkpoint artifact with: current state, completed work, remaining work
- Log key information to long-term memory before trimming
- Note what will be lost and where to recover it

### Step 3: Trim
- Remove lowest-priority items first (see TRIM list above)
- Replace verbose sections with concise summaries
- Keep structured data (lists, tables, paths) over prose

### Step 4: Verify
- Confirm stage goal is still clear after trimming
- Confirm next immediate action is unambiguous
- If not, restore from checkpoint and be more conservative

### Step 5: Continue
- Proceed with highest-value next action
- Monitor context usage more frequently

## Anti-Patterns (Do Not Do)

- Do NOT trim away the stage goal or completion contract
- Do NOT trim without first checkpointing
- Do NOT trim active work-in-progress that is needed for the next step
- Do NOT assume memory can perfectly replace deleted context—verify recoverability
- Do NOT trim error context that explains WHY something failed (only trim the verbose output)

## Memory Integration

After trimming, log the following to memory:
- What was trimmed (general categories, not item-by-item)
- Checkpoint artifact reference
- Where to find details that were trimmed
- Any dependencies that were in context and are now memory-only
