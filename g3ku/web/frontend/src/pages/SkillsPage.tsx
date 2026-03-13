import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { SkillDetailDrawer } from '@/components/resources/SkillDetailDrawer'
import { SkillList } from '@/components/resources/SkillList'
import { listSkills } from '@/lib/api/resources'
import { PageHeader } from '@/layout/PageHeader'

export function SkillsPage() {
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null)
  const [searchText, setSearchText] = useState('')
  const query = useQuery({ queryKey: ['skills'], queryFn: listSkills })

  const items = useMemo(() => {
    const all = query.data?.items || []
    const needle = searchText.trim().toLowerCase()
    if (!needle) return all
    return all.filter((item) => [item.display_name, item.skill_id, item.description].join(' ').toLowerCase().includes(needle))
  }, [query.data?.items, searchText])

  return (
    <section className="page-section resource-page">
      <PageHeader title="Skill 管理" description="查看、编辑 skill 文件和 allowed_roles。" actions={<input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="搜索 Skill" />} />
      <SkillList items={items} selectedSkillId={selectedSkillId} onSelect={setSelectedSkillId} />
      <SkillDetailDrawer skillId={selectedSkillId} open={!!selectedSkillId} onClose={() => setSelectedSkillId(null)} />
    </section>
  )
}
