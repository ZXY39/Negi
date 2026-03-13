import type { ChatMessage, PendingTurn } from '@/stores/ceoStore'

import { InteractionFlow } from '@/components/chat/InteractionFlow'

export function CeoChatFeed(props: { messages: ChatMessage[]; pendingTurns: PendingTurn[] }) {
  return (
    <div className="chat-feed">
      {props.messages.map((message) => (
        <article key={message.id} className={`chat-bubble ${message.role}`}>
          <header>
            <span>{message.role === 'user' ? '你' : 'CEO'}</span>
            <time>{new Date(message.createdAt).toLocaleTimeString()}</time>
          </header>
          <p>{message.text}</p>
        </article>
      ))}
      {props.pendingTurns.map((turn) => (
        <article key={turn.turnId} className="chat-turn-pending">
          <header>
            <span>处理中</span>
            <time>{new Date(turn.createdAt).toLocaleTimeString()}</time>
          </header>
          <p className="pending-user-text">{turn.text}</p>
          <InteractionFlow events={turn.toolEvents} />
        </article>
      ))}
    </div>
  )
}
