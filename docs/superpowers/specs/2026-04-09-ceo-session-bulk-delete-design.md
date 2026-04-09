# CEO Session Bulk Delete Design

**Goal**

Adjust the CEO session panel so the main runtime branding is simplified to `G3KU`, the expanded session tab control becomes more compact, and operators can bulk-select and bulk-delete both local and channel sessions with the same inline confirmation pattern already used for single-session deletion.

**Scope**

- Update frontend branding strings that still say `G3ku Main Runtime` or `G3ku`.
- Update only the CEO session panel frontend in `g3ku/web/frontend/`.
- Reuse the existing inline confirm modal, session delete-check API, and per-session delete API.
- Keep the backend delete protocol unchanged.

**User-Confirmed Requirements**

- The application main name should be shown as `G3KU`.
- In the expanded CEO session panel, the gap between the `本地` and `渠道` segmented tabs should be smaller.
- A right-side control should appear next to the session tabs in expanded mode.
- That control starts as `多选` and switches to `取消` while bulk mode is active.
- When bulk mode is active, a bottom action bar appears under the current session list with `删除` and `全选`.
- `全选` only selects deletable sessions in the current tab.
- Session cards show top-left checkboxes while bulk mode is active.
- Deleting selected sessions must still use the existing second confirmation behavior, including the `清除关联任务` checkbox and the associated task list details.
- Channel sessions must support the same bulk-delete confirmation flow as local sessions.

**Design**

### 1. Branding

- Replace the HTML `<title>` text with `G3KU`.
- Replace the sidebar brand text with `G3KU`.
- Replace the CEO empty-state welcome copy so it references `G3KU` instead of the old main runtime name.

### 2. Expanded session header layout

- Keep the existing expand button and new-session button in the left control cluster.
- Keep the existing `本地 / 渠道` segmented control as the view switcher.
- Reduce the expanded-state segmented control gap from the current value to a tighter spacing so the two tabs visually read as one compact control.
- Add a new expanded-state bulk-mode toggle button on the right side of the segmented control row.
- The new button will share the segmented control height so the row reads as one aligned toolbar.
- In collapsed mode, this bulk-mode button is hidden.

### 3. Session bulk-selection mode

- Introduce a dedicated CEO-session bulk-selection state in the frontend store.
- Bulk-selection mode is only relevant in the expanded session panel.
- When bulk mode is inactive:
  - current behavior remains unchanged,
  - clicking a session card activates that session,
  - single-session action menus remain available for local sessions.
- When bulk mode is active:
  - session activation by clicking the card is disabled,
  - clicking a card toggles selection instead,
  - top-left checkboxes are rendered on each visible session card,
  - the single-session action menu is hidden to avoid conflicting controls,
  - a bottom action bar appears under the current session list.

### 4. Selection rules

- Selection is scoped to the currently visible CEO session tab.
- On the `本地` tab, selectable items are the currently visible local sessions.
- On the `渠道` tab, selectable items are the currently visible channel sessions.
- `全选` selects only selectable sessions in the current tab.
- Pressing `全选` again when all selectable sessions are already selected will clear the selection for the current tab.
- Changing tabs while bulk mode is active clears the previous tab selection so the active selection always matches the current list on screen.
- Leaving bulk mode clears all CEO bulk selections.

### 5. Bulk action bar

- Render a dedicated action row below the session list while bulk mode is active.
- The row contains:
  - `删除`
  - `全选`
- `删除` remains disabled until at least one selectable session is checked.
- `全选` reflects the current tab behavior only; it does not cross local/channel boundaries.

### 6. Delete confirmation behavior

- Bulk delete must reuse the existing inline confirm modal rather than browser-native confirm.
- Before opening the confirm modal, the frontend will call the existing session delete-check endpoint for each selected session.
- The frontend will aggregate the returned delete-check data into one combined confirmation payload.
- The confirmation copy will explicitly state that the selected session chat records will be cleared.
- For channel sessions, the wording will explicitly say channel chat history will be cleared.
- The confirmation keeps one checkbox:
  - `清除关联任务`
- The checkbox hint and details section will be generated from the union of the selected sessions' related task data.
- The task details block must list all associated task ids gathered from the selected sessions, de-duplicated by task id.

### 7. Delete execution

- After confirmation, the frontend will call the existing single-session delete API once per selected session.
- The same `delete_task_records` boolean will be passed to every selected session delete call.
- Deletes run sequentially so they continue to respect current single-session behavior and active-session switching behavior.
- After the batch completes, the UI refreshes the CEO session list and task hall as needed.
- If some deletes fail after others already succeeded, the frontend will keep the successful deletions, refresh the session list, and show a summary error toast instead of pretending the batch was atomic.

**Non-Goals**

- No new batch delete backend API.
- No change to the server-side semantics of a single CEO session delete.
- No change to how the server decides which related tasks are deletable.
- No new channel-specific delete confirmation component.

**Testing**

- Add a frontend unit test for the CEO bulk-selection state helpers:
  - current-tab `全选`
  - tab switch clearing selection
  - bulk toggle reset behavior
- Add a frontend unit test for aggregated delete-check formatting and task-id de-duplication.
- Add a static HTML/CSS test that verifies:
  - the `G3KU` branding strings exist,
  - the CEO session panel markup contains the bulk toggle and bulk action bar anchors,
  - the expanded segmented control CSS uses the tighter gap.
