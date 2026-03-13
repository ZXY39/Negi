import type { TaskStatus } from '@/lib/types/task'

export function statusLabel(status: TaskStatus) {
  switch (status) {
    case 'in_progress':
      return '进行中'
    case 'success':
      return '成功'
    case 'failed':
      return '失败'
    case 'paused':
      return '已暂停'
    case 'cancelled':
      return '已取消'
    default:
      return status || '未知'
  }
}

export function statusTone(status: TaskStatus) {
  switch (status) {
    case 'success':
      return 'success'
    case 'failed':
      return 'failed'
    case 'paused':
      return 'pending'
    case 'in_progress':
    default:
      return 'running'
  }
}
