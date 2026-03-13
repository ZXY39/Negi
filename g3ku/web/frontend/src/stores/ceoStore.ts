import { type QueryClient } from '@tanstack/react-query'
import { create } from 'zustand'

import { connectCeoSocket } from '@/lib/ws/ceoSocket'
import type { CeoToolEvent, ConnectionState } from '@/lib/types/api'

type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  text: string
  createdAt: string
}

type PendingTurn = {
  turnId: string
  text: string
  toolEvents: CeoToolEvent[]
  createdAt: string
}

type SocketHandle = ReturnType<typeof connectCeoSocket> | null

let socketHandle: SocketHandle = null
let pendingMessages: string[] = []

function now() {
  return new Date().toISOString()
}

function nextId(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

type CeoStore = {
  messages: ChatMessage[]
  pendingTurns: PendingTurn[]
  connectionState: ConnectionState
  errorText: string
  connect: (queryClient?: QueryClient) => void
  disconnect: () => void
  send: (text: string, queryClient?: QueryClient) => void
  appendToolEvent: (event: CeoToolEvent) => void
  finalizeTurn: (text: string) => void
  setConnectionState: (state: ConnectionState) => void
}

export const useCeoStore = create<CeoStore>((set, get) => ({
  messages: [],
  pendingTurns: [],
  connectionState: 'idle',
  errorText: '',
  connect: (queryClient) => {
    if (socketHandle) return
    set({ connectionState: 'connecting', errorText: '' })
    socketHandle = connectCeoSocket({
      sessionId: 'web:shared',
      onOpen: () => {
        set({ connectionState: 'open' })
        for (const message of pendingMessages) {
          socketHandle?.send(message)
        }
        pendingMessages = []
      },
      onClose: () => {
        socketHandle = null
        set({ connectionState: 'closed' })
      },
      onError: () => set({ connectionState: 'error', errorText: 'CEO websocket error' }),
      onMessage: (payload) => {
        switch (payload.type) {
          case 'hello':
            return
          case 'ceo.agent.tool':
            get().appendToolEvent(payload.data as CeoToolEvent)
            return
          case 'ceo.reply.final':
            get().finalizeTurn(String((payload.data as { text?: string })?.text || ''))
            return
          case 'task.summary.changed':
            void queryClient?.invalidateQueries({ queryKey: ['tasks'] })
            void queryClient?.invalidateQueries({ queryKey: ['taskSummary'] })
            return
          case 'task.artifact.applied': {
            const taskId = String((payload.data as { task_id?: string })?.task_id || '')
            void queryClient?.invalidateQueries({ queryKey: ['tasks'] })
            void queryClient?.invalidateQueries({ queryKey: ['taskSummary'] })
            if (taskId) {
              void queryClient?.invalidateQueries({ queryKey: ['taskDetail', taskId] })
              void queryClient?.invalidateQueries({ queryKey: ['artifacts', taskId] })
            }
            return
          }
          case 'error':
            set({ errorText: String((payload.data as { code?: string })?.code || 'unknown_error') })
            return
          default:
            return
        }
      },
    })
  },
  disconnect: () => {
    socketHandle?.close()
    socketHandle = null
    pendingMessages = []
    set({ connectionState: 'closed' })
  },
  send: (text, queryClient) => {
    const clean = text.trim()
    if (!clean) return
    if (!socketHandle) get().connect(queryClient)
    const createdAt = now()
    set((state) => ({
      messages: [...state.messages, { id: nextId('user'), role: 'user', text: clean, createdAt }],
      pendingTurns: [...state.pendingTurns, { turnId: nextId('turn'), text: clean, toolEvents: [], createdAt }],
    }))
    if (socketHandle?.raw.readyState === WebSocket.OPEN) {
      socketHandle.send(clean)
    } else {
      pendingMessages.push(clean)
    }
  },
  appendToolEvent: (event) =>
    set((state) => {
      if (!state.pendingTurns.length) return state
      const pendingTurns = [...state.pendingTurns]
      const target = pendingTurns[pendingTurns.length - 1]
      pendingTurns[pendingTurns.length - 1] = { ...target, toolEvents: [...target.toolEvents, event] }
      return { ...state, pendingTurns }
    }),
  finalizeTurn: (text) =>
    set((state) => {
      const pendingTurns = [...state.pendingTurns]
      if (pendingTurns.length) pendingTurns.shift()
      const messages = text
        ? [...state.messages, { id: nextId('assistant'), role: 'assistant', text, createdAt: now() }]
        : state.messages
      return { ...state, pendingTurns, messages }
    }),
  setConnectionState: (state) => set({ connectionState: state }),
}))

export type { ChatMessage, PendingTurn }
