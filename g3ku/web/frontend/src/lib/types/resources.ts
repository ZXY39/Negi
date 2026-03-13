export type ActorRole = 'ceo' | 'execution' | 'inspection'
export type RiskLevel = 'low' | 'medium' | 'high'

export type SkillDetail = {
  skill_id: string
  resource_name?: string | null
  display_name: string
  description: string
  enabled: boolean
  available: boolean
  allowed_roles: ActorRole[]
  editable_files: string[]
  risk_level: RiskLevel
  source_path: string
  skill_doc_path: string
  manifest_path?: string | null
  openai_yaml_path?: string | null
  metadata: Record<string, unknown>
}

export type SkillFileItem = {
  file_key: string
  path: string
}

export type ToolAction = {
  action_id: string
  label: string
  risk_level: RiskLevel
  destructive: boolean
  allowed_roles: ActorRole[]
  executor_names: string[]
}

export type ToolFamily = {
  tool_id: string
  display_name: string
  description: string
  enabled: boolean
  available: boolean
  source_path: string
  actions: ToolAction[]
  metadata: Record<string, unknown>
}
