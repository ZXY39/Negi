import type { TaskNodeRecord, TaskRecord } from '@/lib/types/task'

export function TaskNodeDetail(props: { task: TaskRecord; node: TaskNodeRecord | null }) {
  if (!props.node) {
    return (
      <section className="detail-panel">
        <h3>任务摘要</h3>
        <dl className="detail-grid">
          <div><dt>标题</dt><dd>{props.task.title}</dd></div>
          <div><dt>状态</dt><dd>{props.task.status}</dd></div>
          <div><dt>创建时间</dt><dd>{props.task.created_at || '-'}</dd></div>
          <div><dt>更新时间</dt><dd>{props.task.updated_at || '-'}</dd></div>
          <div><dt>完成时间</dt><dd>{props.task.finished_at || '-'}</dd></div>
          <div><dt>请求</dt><dd>{props.task.user_request || '-'}</dd></div>
          <div><dt>最终输出</dt><dd>{props.task.final_output || '-'}</dd></div>
          <div><dt>失败原因</dt><dd>{props.task.failure_reason || '-'}</dd></div>
        </dl>
      </section>
    )
  }

  return (
    <section className="detail-panel">
      <h3>节点详情</h3>
      <dl className="detail-grid">
        <div><dt>标题</dt><dd>{props.node.goal || props.node.node_id}</dd></div>
        <div><dt>状态</dt><dd>{props.node.status}</dd></div>
        <div><dt>输入</dt><dd>{props.node.input || '-'}</dd></div>
        <div><dt>输出</dt><dd>{props.node.final_output || props.node.output.map((entry) => entry.content).join('\n\n') || '-'}</dd></div>
        <div><dt>校验结果</dt><dd>{props.node.check_result || '-'}</dd></div>
        <div><dt>更新时间</dt><dd>{props.node.updated_at || '-'}</dd></div>
      </dl>
    </section>
  )
}
