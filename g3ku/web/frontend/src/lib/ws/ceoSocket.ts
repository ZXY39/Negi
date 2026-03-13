import type { ApiEnvelope } from '@/lib/types/api'

export function connectCeoSocket(options: {
  sessionId?: string
  onOpen?: () => void
  onClose?: () => void
  onError?: () => void
  onMessage: (payload: ApiEnvelope) => void
}) {
  const sessionId = options.sessionId || 'web:shared'
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const socket = new WebSocket(`${protocol}://${window.location.host}/api/ws/ceo?session_id=${encodeURIComponent(sessionId)}`)

  socket.addEventListener('open', () => options.onOpen?.())
  socket.addEventListener('close', () => options.onClose?.())
  socket.addEventListener('error', () => options.onError?.())
  socket.addEventListener('message', (event) => {
    try {
      options.onMessage(JSON.parse(event.data) as ApiEnvelope)
    } catch {
      // ignore malformed event
    }
  })

  return {
    send(text: string) {
      socket.send(JSON.stringify({ type: 'client.user_message', text }))
    },
    close() {
      socket.close()
    },
    raw: socket,
  }
}
