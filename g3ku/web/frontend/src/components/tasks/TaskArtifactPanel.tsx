import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { applyArtifact, getArtifact } from '@/lib/api/tasks'
import type { TaskArtifactRecord } from '@/lib/types/task'
import { useLayoutStore } from '@/stores/layoutStore'

export function TaskArtifactPanel(props: { taskId: string; artifacts: TaskArtifactRecord[] }) {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)

  const selectedArtifact = useMemo(
    () => props.artifacts.find((item) => item.artifact_id === selectedArtifactId) || props.artifacts[0] || null,
    [props.artifacts, selectedArtifactId],
  )

  const artifactQuery = useQuery({
    queryKey: ['artifact', props.taskId, selectedArtifact?.artifact_id],
    queryFn: () => getArtifact(props.taskId, selectedArtifact!.artifact_id),
    enabled: !!selectedArtifact,
  })

  const applyMutation = useMutation({
    mutationFn: (artifactId: string) => applyArtifact(props.taskId, artifactId),
    onSuccess: async () => {
      showToast('补丁已应用', 'success')
      await queryClient.invalidateQueries({ queryKey: ['artifacts', props.taskId] })
      await queryClient.invalidateQueries({ queryKey: ['taskDetail', props.taskId] })
    },
  })

  return (
    <section className="detail-panel artifact-panel">
      <div className="panel-header-row">
        <h3>Artifacts</h3>
        <span>{props.artifacts.length} items</span>
      </div>
      <div className="artifact-grid">
        <div className="artifact-list">
          {props.artifacts.length ? (
            props.artifacts.map((artifact) => (
              <button key={artifact.artifact_id} type="button" className={`artifact-row ${selectedArtifact?.artifact_id === artifact.artifact_id ? 'selected' : ''}`} onClick={() => setSelectedArtifactId(artifact.artifact_id)}>
                <strong>{artifact.title}</strong>
                <span>{artifact.kind}</span>
                <small>{artifact.preview_text || artifact.created_at}</small>
              </button>
            ))
          ) : (
            <div className="empty-state">暂无工件。</div>
          )}
        </div>
        <div className="artifact-detail">
          {selectedArtifact ? (
            <>
              <div className="panel-header-row">
                <div>
                  <strong>{selectedArtifact.title}</strong>
                  <p>{selectedArtifact.path}</p>
                </div>
                {selectedArtifact.kind === 'patch' ? (
                  <button type="button" className="primary-button" onClick={() => applyMutation.mutate(selectedArtifact.artifact_id)} disabled={applyMutation.isPending}>
                    应用补丁
                  </button>
                ) : null}
              </div>
              <pre>{artifactQuery.data?.content || selectedArtifact.preview_text || ''}</pre>
            </>
          ) : (
            <div className="empty-state">选择一个 artifact 查看详情。</div>
          )}
        </div>
      </div>
    </section>
  )
}
