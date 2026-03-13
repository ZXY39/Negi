import type { ManagedModel, ModelRoles } from '@/lib/types/models'
import { ModelRoleChainEditor } from '@/components/models/ModelRoleChainEditor'

export function ModelCatalogPage(props: {
  items: ManagedModel[]
  roles: ModelRoles
  onCreate: () => void
  onEdit: (model: ManagedModel) => void
  onEnable: (modelKey: string) => void
  onDisable: (modelKey: string) => void
  onDelete: (modelKey: string) => void
  onSaveRoles: (scope: keyof ModelRoles, modelKeys: string[]) => void
}) {
  return (
    <div className="models-layout">
      <section className="models-catalog">
        <div className="panel-header-row">
          <h3>模型目录</h3>
          <button type="button" className="primary-button" onClick={props.onCreate}>新增模型</button>
        </div>
        <div className="model-list">
          {props.items.map((item) => (
            <article key={item.key} className="model-card">
              <div className="panel-header-row">
                <div>
                  <strong>{item.key}</strong>
                  <p>{item.provider_model}</p>
                </div>
                <span className={`status-chip ${item.enabled ? 'success' : 'pending'}`}>{item.enabled ? '启用' : '禁用'}</span>
              </div>
              <p>{item.description || '无描述'}</p>
              <div className="meta-row"><span>{item.api_base}</span><span>{item.scopes.join(', ') || '未分配 scope'}</span></div>
              <div className="drawer-actions">
                <button type="button" className="secondary-button" onClick={() => props.onEdit(item)}>编辑</button>
                <button type="button" className="secondary-button" onClick={() => (item.enabled ? props.onDisable(item.key) : props.onEnable(item.key))}>
                  {item.enabled ? '禁用' : '启用'}
                </button>
                <button type="button" className="secondary-button danger" onClick={() => props.onDelete(item.key)}>删除</button>
              </div>
            </article>
          ))}
        </div>
      </section>
      <ModelRoleChainEditor items={props.items} roles={props.roles} onSave={props.onSaveRoles} />
    </div>
  )
}
