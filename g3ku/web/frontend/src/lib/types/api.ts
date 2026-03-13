export type ApiEnvelope<T = unknown> = {
  channel: string
  session_id: string
  task_id?: string | null
  seq?: number
  type: string
  data: T
  created_at?: string
}

export type CeoToolEvent = {
  status: 'running' | 'success' | 'error'
  tool_name: string
  text: string
  timestamp?: string
  tool_call_id?: string
  is_error?: boolean
}

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'
