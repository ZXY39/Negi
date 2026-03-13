import { deleteJson, getJson, postJson, putJson } from '@/lib/api/client'
import type { ManagedModel, ModelRoles } from '@/lib/types/models'

type ModelsResponse = { ok: true; items: ManagedModel[]; roles: ModelRoles }
type ModelResponse = { ok: true; item: ManagedModel }

export async function listModels() {
  return getJson<ModelsResponse>('/models')
}

export async function getModel(modelKey: string) {
  return getJson<ModelResponse>(`/models/${encodeURIComponent(modelKey)}`)
}

export async function createModel(payload: Partial<ManagedModel> & { key: string; provider_model: string; api_key: string; api_base: string }) {
  return postJson<ModelResponse>('/models', payload)
}

export async function updateModel(modelKey: string, payload: Partial<ManagedModel>) {
  return putJson<ModelResponse>(`/models/${encodeURIComponent(modelKey)}`, payload)
}

export async function enableModel(modelKey: string) {
  return postJson<ModelResponse>(`/models/${encodeURIComponent(modelKey)}/enable`)
}

export async function disableModel(modelKey: string) {
  return postJson<ModelResponse>(`/models/${encodeURIComponent(modelKey)}/disable`)
}

export async function deleteModel(modelKey: string) {
  return deleteJson<{ ok: true; item: { key: string; deleted: true } }>(`/models/${encodeURIComponent(modelKey)}`)
}

export async function updateRoleChain(scope: keyof ModelRoles, modelKeys: string[]) {
  return putJson<{ ok: true; scope: string; roles: { scope: string; model_keys: string[] }; all_roles: ModelRoles }>(`/models/roles/${scope}`, { model_keys: modelKeys })
}
