import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { disableTool, enableTool, getToolDetail, reloadResources, updateToolPolicy } from '@/lib/api/resources'
import type { ActorRole } from '@/lib/types/resources'
import { useLayoutStore } from '@/stores/layoutStore'

const ROLES: ActorRole[] = ['ceo', 'execution', 'inspection']

export function ToolDetailDrawer(props: { toolId: string | null; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)
  const detailQuery = useQuery({ queryKey: ['toolDetail', props.toolId], queryFn: () => getToolDetail(props.toolId!), enabled: props.open && !!props.toolId })
  const [actions, setActions] = useState<Record<string, string[]>>({})

  useEffect(() => {
    const next: Record<string, string[]> = {}
    for (const action of detailQuery.data?.item.actions || []) {
      next[action.action_id] = [...action.allowed_roles]
    }
    setActions(next)
  }, [detailQuery.data?.item.actions, props.toolId])

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!props.toolId) return
      await updateToolPolicy(props.toolId, { enabled: detailQuery.data?.item.enabled, actions })
    },
    onSuccess: async () => {
      showToast('Tool policy 已更新', 'success')
      await queryClient.invalidateQueries({ queryKey: ['tools'] })
      await queryClient.invalidateQueries({ queryKey: ['toolDetail', props.toolId] })
    },
  })

  const enabledMutation = useMutation({
    mutationFn: async () => {
      if (!props.toolId) return
      if (detailQuery.data?.item.enabled) return disableTool(props.toolId)
      return enableTool(props.toolId)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['tools'] })
      await queryClient.invalidateQueries({ queryKey: ['toolDetail', props.toolId] })
    },
  })

  const reloadMutation = useMutation({
    mutationFn: () => reloadResources(),
    onSuccess: async () => {
      showToast('资源已重新加载', 'success')
      await queryClient.invalidateQueries({ queryKey: ['tools'] })
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
    },
  })

  return (
    <aside className={`drawer ${props.open ? 'open' : ''}`}>
      <div className="drawer-header">
        <div>
          <h3>{detailQuery.data?.item.display_name || 'Tool 详情'}</h3>
          <p>{detailQuery.data?.item.tool_id || '未选择 Tool'}</p>
        </div>
        <button type="button" className="icon-button" onClick={props.onClose}>×</button>
      </div>
      {props.toolId && detailQuery.data ? (
        <div className="drawer-body">
          <p>{detailQuery.data.item.description || '无描述'}</p>
          <div className="tool-action-list">
            {detailQuery.data.item.actions.map((action) => (
              <section key={action.action_id} className="tool-action-card">
                <div className="panel-header-row">
                  <div>
                    <strong>{action.label}</strong>
                    <p>{action.action_id}</p>
                  </div>
                  <span>{action.risk_level}</span>
                </div>
                <div className="checkbox-grid">
                  {ROLES.map((role) => (
                    <label key={role}>
                      <input
                        type="checkbox"
                        checked={(actions[action.action_id] || []).includes(role)}
                        onChange={(event) =>
                          setActions((state) => {
                            const current = state[action.action_id] || []
                            return {
                              ...state,
                              [action.action_id]: event.target.checked
                                ? [...current, role]
                                : current.filter((item) => item !== role),
                            }
                          })
                        }
                      />
                      {role}
                    </label>
                  ))}
                </div>
              </section>
            ))}
          </div>
          <div className="drawer-actions">
            <button type="button" className="secondary-button" onClick={() => reloadMutation.mutate()}>
              Reload
            </button>
            <button type="button" className="secondary-button" onClick={() => enabledMutation.mutate()}>
              {detailQuery.data.item.enabled ? '禁用' : '启用'}
            </button>
            <button type="button" className="primary-button" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
              保存
            </button>
          </div>
        </div>
      ) : (
        <div className="drawer-body"><div className="empty-state">选择一个 Tool 查看详情。</div></div>
      )}
    </aside>
  )
}
