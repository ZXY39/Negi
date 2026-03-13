import { create } from 'zustand'

type ToastState = {
  message: string
  tone: 'info' | 'success' | 'error'
}

type LayoutStore = {
  sidebarCollapsed: boolean
  toast: ToastState | null
  setSidebarCollapsed: (value: boolean) => void
  toggleSidebar: () => void
  showToast: (message: string, tone?: ToastState['tone']) => void
  clearToast: () => void
}

export const useLayoutStore = create<LayoutStore>((set) => ({
  sidebarCollapsed: false,
  toast: null,
  setSidebarCollapsed: (value) => set({ sidebarCollapsed: value }),
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  showToast: (message, tone = 'info') => set({ toast: { message, tone } }),
  clearToast: () => set({ toast: null }),
}))
