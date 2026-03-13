import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { disableSkill, enableSkill, getSkillDetail, readSkillFile, updateSkillFile, updateSkillPolicy } from '@/lib/api/resources'
import type { ActorRole } from '@/lib/types/resources'
import { useLayoutStore } from '@/stores/layoutStore'

const ROLES: ActorRole[] = ['ceo', 'execution', 'inspection']

export function SkillDetailDrawer(props: { skillId: string | null; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)
  const [selectedFileKey, setSelectedFileKey] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [allowedRoles, setAllowedRoles] = useState<ActorRole[]>([])

  const detailQuery = useQuery({
    queryKey: ['skillDetail', props.skillId],
    queryFn: () => getSkillDetail(props.skillId!),
    enabled: props.open && !!props.skillId,
  })

  const fileQuery = useQuery({
    queryKey: ['skillFile', props.skillId, selectedFileKey],
    queryFn: () => readSkillFile(props.skillId!, selectedFileKey!),
    enabled: props.open && !!props.skillId && !!selectedFileKey,
  })

  useEffect(() => {
    const files = detailQuery.data?.files || []
    setSelectedFileKey(files[0]?.file_key || null)
    setAllowedRoles((detailQuery.data?.item.allowed_roles || []) as ActorRole[])
    setDrafts({})
  }, [detailQuery.data?.files, detailQuery.data?.item.allowed_roles, props.skillId])

  useEffect(() => {
    if (selectedFileKey && fileQuery.data?.content !== undefined) {
      setDrafts((state) => (state[selectedFileKey] === undefined ? { ...state, [selectedFileKey]: fileQuery.data.content } : state))
    }
  }, [fileQuery.data?.content, selectedFileKey])

  const files = detailQuery.data?.files || []
  const currentContent = selectedFileKey ? drafts[selectedFileKey] || '' : ''

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!props.skillId) return
      for (const [fileKey, content] of Object.entries(drafts)) {
        await updateSkillFile(props.skillId, fileKey, content)
      }
      await updateSkillPolicy(props.skillId, { enabled: detailQuery.data?.item.enabled, allowed_roles: allowedRoles })
    },
    onSuccess: async () => {
      showToast('Skill 已保存', 'success')
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
      await queryClient.invalidateQueries({ queryKey: ['skillDetail', props.skillId] })
    },
  })

  const toggleEnabledMutation = useMutation({
    mutationFn: async () => {
      if (!props.skillId) return
      if (detailQuery.data?.item.enabled) return disableSkill(props.skillId)
      return enableSkill(props.skillId)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
      await queryClient.invalidateQueries({ queryKey: ['skillDetail', props.skillId] })
    },
  })

  return (
    <aside className={`drawer ${props.open ? 'open' : ''}`}>
      <div className="drawer-header">
        <div>
          <h3>{detailQuery.data?.item.display_name || 'Skill 详情'}</h3>
          <p>{detailQuery.data?.item.skill_id || '未选择 Skill'}</p>
        </div>
        <button type="button" className="icon-button" onClick={props.onClose}>×</button>
      </div>
      {props.skillId && detailQuery.data ? (
        <div className="drawer-body">
          <p>{detailQuery.data.item.description || '无描述'}</p>
          <div className="checkbox-grid">
            {ROLES.map((role) => (
              <label key={role}>
                <input
                  type="checkbox"
                  checked={allowedRoles.includes(role)}
                  onChange={(event) =>
                    setAllowedRoles((state) =>
                      event.target.checked ? [...state, role] : state.filter((item) => item !== role),
                    )
                  }
                />
                {role}
              </label>
            ))}
          </div>
          <div className="resource-files-layout">
            <div className="resource-file-tabs">
              {files.map((file) => (
                <button key={file.file_key} type="button" className={selectedFileKey === file.file_key ? 'active' : ''} onClick={() => setSelectedFileKey(file.file_key)}>
                  {file.file_key}
                </button>
              ))}
            </div>
            <textarea value={currentContent} onChange={(event) => selectedFileKey && setDrafts((state) => ({ ...state, [selectedFileKey]: event.target.value }))} rows={18} />
          </div>
          <div className="drawer-actions">
            <button type="button" className="secondary-button" onClick={() => toggleEnabledMutation.mutate()}>
              {detailQuery.data.item.enabled ? '禁用' : '启用'}
            </button>
            <button type="button" className="primary-button" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
              保存
            </button>
          </div>
        </div>
      ) : (
        <div className="drawer-body"><div className="empty-state">选择一个 Skill 查看详情。</div></div>
      )}
    </aside>
  )
}
