export type TaskStatus = 'in_progress' | 'success' | 'failed' | 'paused' | 'cancelled' | string

export type TaskListItem = {
  task_id: string
  title: string
  brief: string
  status: TaskStatus
  is_unread: boolean
  created_at: string
  updated_at: string
  max_depth?: number
}

export type TaskSummary = {
  total_tasks: number
  in_progress_tasks: number
  failed_tasks: number
  unread_tasks: number
  text: string
}

export type TaskTreeNode = {
  node_id: string
  parent_node_id: string | null
  depth: number
  status: TaskStatus
  title: string
  input: string
  output: string
  check_result: string
  updated_at: string
  children: TaskTreeNode[]
}

export type TaskNodeRecord = {
  node_id: string
  task_id: string
  parent_node_id: string | null
  root_node_id: string
  depth: number
  node_kind: string
  status: TaskStatus
  goal: string
  prompt: string
  input: string
  output: Array<{ seq: number; content: string; created_at: string; tool_calls: unknown[] }>
  check_result: string
  final_output: string
  can_spawn_children: boolean
  created_at: string
  updated_at: string
  finished_at: string | null
  failure_reason: string
  metadata: Record<string, unknown>
}

export type TaskRecord = {
  task_id: string
  session_id: string
  title: string
  user_request: string
  status: TaskStatus
  max_depth: number
  created_at: string
  updated_at: string
  finished_at: string | null
  final_output: string
  failure_reason: string
  is_unread: boolean
}

export type TaskProgress = {
  task_id: string
  task_status: TaskStatus
  tree_text: string
  root: TaskTreeNode | null
  nodes: TaskNodeRecord[]
  text: string
}

export type TaskDetailPayload = {
  task: TaskRecord
  progress: TaskProgress
}

export type TaskArtifactRecord = {
  artifact_id: string
  task_id: string
  node_id: string | null
  kind: string
  title: string
  path: string
  mime_type: string
  preview_text: string
  created_at: string
}
