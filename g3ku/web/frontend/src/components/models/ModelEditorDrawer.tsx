import { useEffect, useState } from 'react'

import type { ManagedModel } from '@/lib/types/models'

const EMPTY_FORM: ManagedModel = {
  key: '',
  provider_model: '',
  api_key: '',
  enabled: true,
  api_base: '',
  extra_headers: null,
  max_tokens: 4096,
  temperature: 0.1,
  reasoning_effort: null,
  retry_on: ['network', '429', '5xx'],
  description: '',
  scopes: [],
}

export function ModelEditorDrawer(props: {
  open: boolean
  item: ManagedModel | null
  onClose: () => void
  onSave: (payload: ManagedModel, isCreate: boolean) => void
}) {
  const [form, setForm] = useState<ManagedModel>(EMPTY_FORM)

  useEffect(() => {
    setForm(props.item || EMPTY_FORM)
  }, [props.item])

  return (
    <aside className={`drawer ${props.open ? 'open' : ''}`}>
      <div className="drawer-header">
        <div>
          <h3>{props.item ? '编辑模型' : '新增模型'}</h3>
          <p>{props.item?.key || '创建新的 managed model'}</p>
        </div>
        <button type="button" className="icon-button" onClick={props.onClose}>×</button>
      </div>
      <div className="drawer-body model-form-grid">
        <label>Key<input value={form.key} disabled={!!props.item} onChange={(event) => setForm({ ...form, key: event.target.value })} /></label>
        <label>Provider Model<input value={form.provider_model} onChange={(event) => setForm({ ...form, provider_model: event.target.value })} /></label>
        <label>API Base<input value={form.api_base} onChange={(event) => setForm({ ...form, api_base: event.target.value })} /></label>
        <label>API Key<input value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} /></label>
        <label>Max Tokens<input type="number" value={form.max_tokens || 4096} onChange={(event) => setForm({ ...form, max_tokens: Number(event.target.value) })} /></label>
        <label>Temperature<input type="number" step="0.1" value={form.temperature || 0.1} onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })} /></label>
        <label>Reasoning<input value={form.reasoning_effort || ''} onChange={(event) => setForm({ ...form, reasoning_effort: event.target.value || null })} /></label>
        <label className="field-span-full">Description<textarea rows={4} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
        <div className="drawer-actions field-span-full">
          <button type="button" className="secondary-button" onClick={props.onClose}>取消</button>
          <button type="button" className="primary-button" onClick={() => props.onSave(form, !props.item)}>保存</button>
        </div>
      </div>
    </aside>
  )
}
