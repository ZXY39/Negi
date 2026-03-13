import type { SkillDetail } from '@/lib/types/resources'

export function SkillList(props: { items: SkillDetail[]; selectedSkillId: string | null; onSelect: (skillId: string) => void }) {
  if (!props.items.length) return <div className="empty-state">没有 Skill。</div>
  return (
    <div className="resource-list">
      {props.items.map((item) => (
        <button key={item.skill_id} type="button" className={`resource-row ${props.selectedSkillId === item.skill_id ? 'selected' : ''}`} onClick={() => props.onSelect(item.skill_id)}>
          <strong>{item.display_name}</strong>
          <span>{item.skill_id}</span>
          <small>{item.description || '无描述'}</small>
        </button>
      ))}
    </div>
  )
}
