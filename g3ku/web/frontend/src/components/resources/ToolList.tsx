import type { ToolFamily } from '@/lib/types/resources'

export function ToolList(props: { items: ToolFamily[]; selectedToolId: string | null; onSelect: (toolId: string) => void }) {
  if (!props.items.length) return <div className="empty-state">没有 Tool。</div>
  return (
    <div className="resource-list">
      {props.items.map((item) => (
        <button key={item.tool_id} type="button" className={`resource-row ${props.selectedToolId === item.tool_id ? 'selected' : ''}`} onClick={() => props.onSelect(item.tool_id)}>
          <strong>{item.display_name}</strong>
          <span>{item.tool_id}</span>
          <small>{item.description || '无描述'}</small>
        </button>
      ))}
    </div>
  )
}
