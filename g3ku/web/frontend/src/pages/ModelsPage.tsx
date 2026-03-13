import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ModelCatalogPage } from '@/components/models/ModelCatalogPage'
import { ModelEditorDrawer } from '@/components/models/ModelEditorDrawer'
import { createModel, deleteModel, disableModel, enableModel, listModels, updateModel, updateRoleChain } from '@/lib/api/models'
import type { ManagedModel, ModelRoles } from '@/lib/types/models'
import { PageHeader } from '@/layout/PageHeader'
import { useLayoutStore } from '@/stores/layoutStore'

export function ModelsPage() {
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)
  const [editingItem, setEditingItem] = useState<ManagedModel | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const query = useQuery({ queryKey: ['models'], queryFn: listModels })

  const saveMutation = useMutation({
    mutationFn: async (payload: { item: ManagedModel; isCreate: boolean }) => {
      if (payload.isCreate) return createModel(payload.item)
      return updateModel(payload.item.key, payload.item)
    },
    onSuccess: async () => {
      setDrawerOpen(false)
      setEditingItem(null)
      showToast('模型配置已保存', 'success')
      await queryClient.invalidateQueries({ queryKey: ['models'] })
    },
  })

  const actionMutation = useMutation({
    mutationFn: async (payload: { type: 'enable' | 'disable' | 'delete'; key: string }) => {
      if (payload.type === 'enable') return enableModel(payload.key)
      if (payload.type === 'disable') return disableModel(payload.key)
      return deleteModel(payload.key)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['models'] })
    },
  })

  const roleMutation = useMutation({
    mutationFn: ({ scope, modelKeys }: { scope: keyof ModelRoles; modelKeys: string[] }) => updateRoleChain(scope, modelKeys),
    onSuccess: async () => {
      showToast('Role chain 已更新', 'success')
      await queryClient.invalidateQueries({ queryKey: ['models'] })
    },
  })

  return (
    <section className="page-section resource-page">
      <PageHeader title="模型管理" description="维护 catalog、启停状态与 ceo/execution/inspection role chain。" />
      <ModelCatalogPage
        items={query.data?.items || []}
        roles={(query.data?.roles || { ceo: [], execution: [], inspection: [] }) as ModelRoles}
        onCreate={() => { setEditingItem(null); setDrawerOpen(true) }}
        onEdit={(item) => { setEditingItem(item); setDrawerOpen(true) }}
        onEnable={(key) => actionMutation.mutate({ type: 'enable', key })}
        onDisable={(key) => actionMutation.mutate({ type: 'disable', key })}
        onDelete={(key) => actionMutation.mutate({ type: 'delete', key })}
        onSaveRoles={(scope, modelKeys) => roleMutation.mutate({ scope, modelKeys })}
      />
      <ModelEditorDrawer open={drawerOpen} item={editingItem} onClose={() => setDrawerOpen(false)} onSave={(item, isCreate) => saveMutation.mutate({ item, isCreate })} />
    </section>
  )
}
