import type { ReactNode } from 'react'

export function PageHeader(props: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">控制台</p>
        <h2>{props.title}</h2>
        {props.description ? <p className="page-description">{props.description}</p> : null}
      </div>
      {props.actions ? <div className="page-actions">{props.actions}</div> : null}
    </header>
  )
}
