import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { ToolDetailDrawer } from '@/components/resources/ToolDetailDrawer'
import { ToolList } from '@/components/resources/ToolList'
import { listTools } from '@/lib/api/resources'
import { PageHeader } from '@/layout/PageHeader'

export function ToolsPage() {
  const [selectedToolId, setSelectedToolId] = useState<string | null>(null)
  const [searchText, setSearchText] = useState('')
  const query = useQuery({ queryKey: ['tools'], queryFn: listTools })

  const items = useMemo(() => {
    const all = query.data?.items || []
    const needle = searchText.trim().toLowerCase()
    if (!needle) return all
    return all.filter((item) => [item.display_name, item.tool_id, item.description].join(' ').toLowerCase().includes(needle))
  }, [query.data?.items, searchText])

  return (
    <section className="page-section resource-page">
      <PageHeader title="Tool 管理" description="编辑 action 级 allowed_roles，支持 reload resources。" actions={<input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="搜索 Tool" />} />
      <ToolList items={items} selectedToolId={selectedToolId} onSelect={setSelectedToolId} />
      <ToolDetailDrawer toolId={selectedToolId} open={!!selectedToolId} onClose={() => setSelectedToolId(null)} />
    </section>
  )
}
