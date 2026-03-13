import { create } from 'zustand'

type FilterScope = 1 | 2 | 3 | 4

type TaskUiStore = {
  selectedTaskIds: string[]
  multiSelectMode: boolean
  filterScope: FilterScope
  searchText: string
  selectedNodeId: string | null
  expandedNodeIds: Record<string, boolean>
  pan: { x: number; y: number }
  setSelectedTaskIds: (taskIds: string[]) => void
  toggleSelectedTaskId: (taskId: string) => void
  clearTaskSelection: () => void
  setMultiSelectMode: (value: boolean) => void
  setFilterScope: (value: FilterScope) => void
  setSearchText: (value: string) => void
  selectNode: (nodeId: string | null) => void
  toggleNodeExpanded: (nodeId: string) => void
  setExpandedNodeIds: (items: Record<string, boolean>) => void
  setPan: (x: number, y: number) => void
  resetTreeUi: () => void
}

export const useTaskUiStore = create<TaskUiStore>((set) => ({
  selectedTaskIds: [],
  multiSelectMode: false,
  filterScope: 1,
  searchText: '',
  selectedNodeId: null,
  expandedNodeIds: {},
  pan: { x: 0, y: 0 },
  setSelectedTaskIds: (taskIds) => set({ selectedTaskIds: taskIds }),
  toggleSelectedTaskId: (taskId) =>
    set((state) => ({
      selectedTaskIds: state.selectedTaskIds.includes(taskId)
        ? state.selectedTaskIds.filter((item) => item !== taskId)
        : [...state.selectedTaskIds, taskId],
    })),
  clearTaskSelection: () => set({ selectedTaskIds: [] }),
  setMultiSelectMode: (value) => set({ multiSelectMode: value }),
  setFilterScope: (value) => set({ filterScope: value }),
  setSearchText: (value) => set({ searchText: value }),
  selectNode: (nodeId) => set({ selectedNodeId: nodeId }),
  toggleNodeExpanded: (nodeId) =>
    set((state) => ({
      expandedNodeIds: {
        ...state.expandedNodeIds,
        [nodeId]: state.expandedNodeIds[nodeId] === false,
      },
    })),
  setExpandedNodeIds: (items) => set({ expandedNodeIds: items }),
  setPan: (x, y) => set({ pan: { x, y } }),
  resetTreeUi: () => set({ selectedNodeId: null, expandedNodeIds: {}, pan: { x: 0, y: 0 } }),
}))
