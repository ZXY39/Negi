export function TaskToolbar(props: {
  scope: number
  searchText: string
  selectedCount: number
  multiSelectMode: boolean
  onScopeChange: (scope: 1 | 2 | 3 | 4) => void
  onSearchChange: (value: string) => void
  onToggleMultiSelect: () => void
  onPause: () => void
  onResume: () => void
  onCancel: () => void
}) {
  const scopes = [
    { value: 1 as const, label: '全部' },
    { value: 2 as const, label: '进行中' },
    { value: 3 as const, label: '失败' },
    { value: 4 as const, label: '未读' },
  ]

  return (
    <div className="task-toolbar">
      <div className="task-toolbar-filters">
        {scopes.map((scope) => (
          <button key={scope.value} type="button" className={`pill-button ${props.scope === scope.value ? 'active' : ''}`} onClick={() => props.onScopeChange(scope.value)}>
            {scope.label}
          </button>
        ))}
      </div>
      <input value={props.searchText} onChange={(event) => props.onSearchChange(event.target.value)} placeholder="搜索 title / task_id / brief" />
      <button type="button" className="secondary-button" onClick={props.onToggleMultiSelect}>
        {props.multiSelectMode ? '退出多选' : '多选'}
      </button>
      {props.multiSelectMode ? (
        <div className="task-toolbar-actions">
          <span>已选 {props.selectedCount}</span>
          <button type="button" className="secondary-button" onClick={props.onPause} disabled={!props.selectedCount}>
            Pause
          </button>
          <button type="button" className="secondary-button" onClick={props.onResume} disabled={!props.selectedCount}>
            Resume
          </button>
          <button type="button" className="secondary-button danger" onClick={props.onCancel} disabled={!props.selectedCount}>
            Cancel
          </button>
        </div>
      ) : null}
    </div>
  )
}
