import { TaskCard } from '@/components/tasks/TaskCard'
import type { TaskListItem } from '@/lib/types/task'

export function TaskList(props: {
  items: TaskListItem[]
  selectedTaskIds: string[]
  multiSelect: boolean
  onOpenTask: (taskId: string) => void
  onToggleSelect: (taskId: string) => void
}) {
  if (!props.items.length) {
    return <div className="empty-state">当前筛选下没有任务。</div>
  }

  return (
    <div className="task-list-grid">
      {props.items.map((item) => (
        <TaskCard
          key={item.task_id}
          item={item}
          selected={props.selectedTaskIds.includes(item.task_id)}
          multiSelect={props.multiSelect}
          onClick={() => props.onOpenTask(item.task_id)}
          onToggleSelect={() => props.onToggleSelect(item.task_id)}
        />
      ))}
    </div>
  )
}
