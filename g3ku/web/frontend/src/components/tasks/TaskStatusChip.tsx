import { statusLabel, statusTone } from '@/lib/utils/status'
import type { TaskStatus } from '@/lib/types/task'

export function TaskStatusChip(props: { status: TaskStatus }) {
  return <span className={`status-chip ${statusTone(props.status)}`}>{statusLabel(props.status)}</span>
}
