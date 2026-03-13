import type { FormEvent } from 'react'
import { useState } from 'react'

export function CeoComposer(props: { disabled?: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState('')

  function submit(event: FormEvent) {
    event.preventDefault()
    const clean = text.trim()
    if (!clean) return
    props.onSend(clean)
    setText('')
  }

  return (
    <form className="chat-composer" onSubmit={submit}>
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="给 CEO 一条新的指令，例如：创建一个异步任务并持续监控结果。"
        rows={4}
      />
      <div className="chat-composer-footer">
        <span>保留工具执行流与最终回复</span>
        <button className="primary-button" type="submit" disabled={props.disabled || !text.trim()}>
          发送
        </button>
      </div>
    </form>
  )
}
