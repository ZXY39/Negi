import { Clock3 } from 'lucide-react'

import { TaskStatusChip } from '@/components/tasks/TaskStatusChip'
import type { TaskListItem } from '@/lib/types/task'

export function TaskCard(props: {
  item: TaskListItem
  selected: boolean
  multiSelect: boolean
  onClick: () => void
  onToggleSelect: () => void
}) {
  const { item } = props
  return (
    <article className={`task-card ${props.selected ? 'selected' : ''}`}>
      <div className="task-card-head">
        {props.multiSelect ? <input type="checkbox" checked={props.selected} onChange={props.onToggleSelect} aria-label={`选择任务 ${item.task_id}`} /> : null}
        <div className="task-card-title-wrap" onClick={props.onClick} role="button" tabIndex={0}>
          <h3>{item.title || item.task_id}</h3>
          <p className="task-card-id">{item.task_id}</p>
        </div>
        <TaskStatusChip status={item.status} />
      </div>
      <button type="button" className="task-card-body" onClick={props.onClick}>
        <p>{item.brief || '暂无摘要'}</p>
        <div className="task-card-meta">
          <span className="meta-chip">
            <Clock3 size={14} />
            {item.updated_at ? new Date(item.updated_at).toLocaleString() : '无更新时间'}
          </span>
          {item.is_unread ? <span className="meta-chip unread">未读</span> : null}
        </div>
      </button>
    </article>
  )
}
