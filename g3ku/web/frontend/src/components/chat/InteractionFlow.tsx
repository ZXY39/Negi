import type { CeoToolEvent } from '@/lib/types/api'

export function InteractionFlow(props: { events: CeoToolEvent[] }) {
  if (!props.events.length) {
    return <div className="interaction-flow empty">等待工具执行…</div>
  }

  return (
    <ol className="interaction-flow">
      {props.events.map((event, index) => (
        <li key={`${event.tool_call_id || event.tool_name}_${index}`} className={`interaction-step ${event.status}`}>
          <div className="interaction-step-head">
            <strong>{event.tool_name}</strong>
            <span>{event.status}</span>
          </div>
          <p>{event.text || '无额外输出'}</p>
        </li>
      ))}
    </ol>
  )
}
