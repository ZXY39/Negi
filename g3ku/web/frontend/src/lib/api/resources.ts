import { getJson, postJson, putJson } from '@/lib/api/client'
import type { SkillDetail, SkillFileItem, ToolFamily } from '@/lib/types/resources'

type SkillsResponse = { ok: true; items: SkillDetail[] }
type SkillDetailResponse = { ok: true; item: SkillDetail; files: SkillFileItem[] }
type SkillFilesResponse = { ok: true; items: SkillFileItem[] }
type SkillFileResponse = { ok: true; file_key: string; path: string; content: string }
type ToolsResponse = { ok: true; items: ToolFamily[] }
type ToolDetailResponse = { ok: true; item: ToolFamily }

export async function listSkills() {
  return getJson<SkillsResponse>('/resources/skills')
}

export async function getSkillDetail(skillId: string) {
  return getJson<SkillDetailResponse>(`/resources/skills/${encodeURIComponent(skillId)}`)
}

export async function listSkillFiles(skillId: string) {
  return getJson<SkillFilesResponse>(`/resources/skills/${encodeURIComponent(skillId)}/files`)
}

export async function readSkillFile(skillId: string, fileKey: string) {
  return getJson<SkillFileResponse>(`/resources/skills/${encodeURIComponent(skillId)}/files/${encodeURIComponent(fileKey)}`)
}

export async function updateSkillFile(skillId: string, fileKey: string, content: string) {
  return putJson<{ ok: true; item: { skill_id: string; file_key: string; path: string } }>(`/resources/skills/${encodeURIComponent(skillId)}/files/${encodeURIComponent(fileKey)}`, { content })
}

export async function updateSkillPolicy(skillId: string, payload: { enabled?: boolean; allowed_roles?: string[] }) {
  return putJson<{ ok: true; item: SkillDetail }>(`/resources/skills/${encodeURIComponent(skillId)}/policy`, payload)
}

export async function enableSkill(skillId: string) {
  return postJson<{ ok: true; item: SkillDetail }>(`/resources/skills/${encodeURIComponent(skillId)}/enable`)
}

export async function disableSkill(skillId: string) {
  return postJson<{ ok: true; item: SkillDetail }>(`/resources/skills/${encodeURIComponent(skillId)}/disable`)
}

export async function listTools() {
  return getJson<ToolsResponse>('/resources/tools')
}

export async function getToolDetail(toolId: string) {
  return getJson<ToolDetailResponse>(`/resources/tools/${encodeURIComponent(toolId)}`)
}

export async function updateToolPolicy(toolId: string, payload: { enabled?: boolean; actions?: Record<string, string[]> }) {
  return putJson<{ ok: true; item: ToolFamily }>(`/resources/tools/${encodeURIComponent(toolId)}/policy`, payload)
}

export async function enableTool(toolId: string) {
  return postJson<{ ok: true; item: ToolFamily }>(`/resources/tools/${encodeURIComponent(toolId)}/enable`)
}

export async function disableTool(toolId: string) {
  return postJson<{ ok: true; item: ToolFamily }>(`/resources/tools/${encodeURIComponent(toolId)}/disable`)
}

export async function reloadResources() {
  return postJson<{ ok: true; session_id: string; skills: number; tools: number }>('/resources/reload', {})
}
