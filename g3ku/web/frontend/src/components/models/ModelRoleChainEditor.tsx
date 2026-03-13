import { useEffect, useMemo, useState } from 'react'

import type { ManagedModel, ModelRoles } from '@/lib/types/models'

const SCOPES: Array<keyof ModelRoles> = ['ceo', 'execution', 'inspection']

export function ModelRoleChainEditor(props: {
  items: ManagedModel[]
  roles: ModelRoles
  onSave: (scope: keyof ModelRoles, modelKeys: string[]) => void
}) {
  const [localRoles, setLocalRoles] = useState<ModelRoles>(props.roles)
  const [dragState, setDragState] = useState<{ scope: keyof ModelRoles; index: number } | null>(null)
  const [selectedToAdd, setSelectedToAdd] = useState<Record<keyof ModelRoles, string>>({ ceo: '', execution: '', inspection: '' })

  useEffect(() => {
    setLocalRoles(props.roles)
  }, [props.roles])

  const enabledModels = useMemo(() => props.items.filter((item) => item.enabled), [props.items])

  function move(scope: keyof ModelRoles, fromIndex: number, toIndex: number) {
    setLocalRoles((state) => {
      const next = [...state[scope]]
      const [item] = next.splice(fromIndex, 1)
      next.splice(toIndex, 0, item)
      return { ...state, [scope]: next }
    })
  }

  return (
    <section className="role-chain-editor">
      <div className="panel-header-row">
        <h3>Role Chain</h3>
        <span>拖拽排序 + 手动增删</span>
      </div>
      <div className="role-columns">
        {SCOPES.map((scope) => (
          <div key={scope} className="role-column">
            <div className="panel-header-row">
              <strong>{scope}</strong>
              <button type="button" className="primary-button small" onClick={() => props.onSave(scope, localRoles[scope])}>保存</button>
            </div>
            <div className="role-add-row">
              <select value={selectedToAdd[scope]} onChange={(event) => setSelectedToAdd((state) => ({ ...state, [scope]: event.target.value }))}>
                <option value="">选择模型</option>
                {enabledModels.map((item) => <option key={item.key} value={item.key}>{item.key}</option>)}
              </select>
              <button
                type="button"
                className="secondary-button"
                onClick={() => {
                  const key = selectedToAdd[scope]
                  if (!key) return
                  setLocalRoles((state) => ({
                    ...state,
                    [scope]: state[scope].includes(key) ? state[scope] : [...state[scope], key],
                  }))
                }}
              >
                添加
              </button>
            </div>
            <div className="role-list">
              {localRoles[scope].map((key, index) => (
                <div
                  key={`${scope}_${key}`}
                  className="role-item"
                  draggable
                  onDragStart={() => setDragState({ scope, index })}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragState && dragState.scope === scope) move(scope, dragState.index, index)
                    setDragState(null)
                  }}
                >
                  <span>{key}</span>
                  <div className="role-item-actions">
                    <button type="button" className="icon-button" onClick={() => index > 0 && move(scope, index, index - 1)}>↑</button>
                    <button type="button" className="icon-button" onClick={() => index < localRoles[scope].length - 1 && move(scope, index, index + 1)}>↓</button>
                    <button type="button" className="icon-button" onClick={() => setLocalRoles((state) => ({ ...state, [scope]: state[scope].filter((item) => item !== key) }))}>×</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
