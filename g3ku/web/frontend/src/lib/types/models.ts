export type ManagedModel = {
  key: string
  provider_model: string
  api_key: string
  enabled: boolean
  api_base: string
  extra_headers?: Record<string, string> | null
  max_tokens?: number | null
  temperature?: number | null
  reasoning_effort?: string | null
  retry_on: string[]
  description: string
  scopes: string[]
}

export type ModelRoles = {
  ceo: string[]
  execution: string[]
  inspection: string[]
}
