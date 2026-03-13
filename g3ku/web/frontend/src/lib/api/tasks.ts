import { getJson, postJson } from '@/lib/api/client'
import type { TaskArtifactRecord, TaskDetailPayload, TaskListItem, TaskSummary } from '@/lib/types/task'

type SummaryResponse = { ok: true } & TaskSummary

type TasksResponse = {
  ok: true
  scope: number
  items: TaskListItem[]
}

type TaskDetailResponse = {
  ok: true
} & TaskDetailPayload

type ArtifactsResponse = {
  ok: true
  items: TaskArtifactRecord[]
}

type ArtifactDetailResponse = {
  ok: true
  artifact: TaskArtifactRecord
  content: string
}

export async function createTask(payload: { task: string; title?: string; max_depth?: number }) {
  return postJson<{ ok: true; task: TaskDetailPayload['task'] }>('/tasks', payload)
}

export async function getTaskSummary(sessionId = 'web:shared') {
  return getJson<SummaryResponse>(`/tasks/summary?session_id=${encodeURIComponent(sessionId)}`)
}

export async function getTasks(scope = 1, sessionId = 'web:shared') {
  return getJson<TasksResponse>(`/tasks?scope=${scope}&session_id=${encodeURIComponent(sessionId)}`)
}

export async function getTaskDetail(taskId: string, markRead = false) {
  return getJson<TaskDetailResponse>(`/tasks/${encodeURIComponent(taskId)}?mark_read=${markRead ? 'true' : 'false'}`)
}

export async function pauseTask(taskId: string) {
  return postJson<{ ok: true; task: TaskDetailPayload['task'] }>(`/tasks/${encodeURIComponent(taskId)}/pause`)
}

export async function resumeTask(taskId: string) {
  return postJson<{ ok: true; task: TaskDetailPayload['task'] }>(`/tasks/${encodeURIComponent(taskId)}/resume`)
}

export async function cancelTask(taskId: string) {
  return postJson<{ ok: true; task: TaskDetailPayload['task'] }>(`/tasks/${encodeURIComponent(taskId)}/cancel`)
}

export async function getArtifacts(taskId: string) {
  return getJson<ArtifactsResponse>(`/tasks/${encodeURIComponent(taskId)}/artifacts`)
}

export async function getArtifact(taskId: string, artifactId: string) {
  return getJson<ArtifactDetailResponse>(`/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}`)
}

export async function applyArtifact(taskId: string, artifactId: string) {
  return postJson<{ ok: true; artifact_id: string; path: string; applied: true }>(`/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}/apply`)
}
