import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { CeoChatFeed } from '@/components/chat/CeoChatFeed'
import { CeoComposer } from '@/components/chat/CeoComposer'
import { PageHeader } from '@/layout/PageHeader'
import { useCeoStore } from '@/stores/ceoStore'

export function CeoPage() {
  const queryClient = useQueryClient()
  const messages = useCeoStore((state) => state.messages)
  const pendingTurns = useCeoStore((state) => state.pendingTurns)
  const connectionState = useCeoStore((state) => state.connectionState)
  const errorText = useCeoStore((state) => state.errorText)
  const connect = useCeoStore((state) => state.connect)
  const disconnect = useCeoStore((state) => state.disconnect)
  const send = useCeoStore((state) => state.send)

  useEffect(() => {
    connect(queryClient)
    return () => disconnect()
  }, [connect, disconnect, queryClient])

  return (
    <section className="page-section chat-page">
      <PageHeader
        title="CEO 会话"
        description="通过 CEO websocket 直接驱动主前门，并实时查看工具执行流。"
        actions={<span className={`connection-pill ${connectionState}`}>{connectionState}</span>}
      />
      {errorText ? <div className="inline-banner error">{errorText}</div> : null}
      <CeoChatFeed messages={messages} pendingTurns={pendingTurns} />
      <CeoComposer disabled={connectionState === 'connecting'} onSend={(text) => send(text, queryClient)} />
    </section>
  )
}
