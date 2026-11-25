import React, { useEffect, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { apiFetch, ProjectItem } from '../lib/api'
import { NewTaskKind } from '../lib/router'
import WorkbenchPage from './WorkbenchPage'
import UnificationPage from './UnificationPage'
import ContractsPage from './ContractsPage'

interface Props {
  project: ProjectItem
  canEdit: boolean
  activeDsIds: string[]
  activeProfile: any
  activeRouting: any
  jobId?: string
  runId?: string
  versionId?: string
  newKind?: NewTaskKind
  onBack: () => void
  onGoDataSources: () => void
  onGoContracts: () => void
}

function newStage(kind?: NewTaskKind): 'L1' | 'L2' | 'L4' | undefined {
  if (kind === 'clean') return 'L1'
  if (kind === 'match') return 'L2'
  if (kind === 'metrics') return 'L4'
  return undefined
}

export default function TaskDetailPage({
  project, canEdit, activeDsIds, activeProfile, activeRouting,
  jobId, runId, versionId, newKind, onBack, onGoDataSources, onGoContracts,
}: Props) {
  const [state, setState] = useState<'loading' | 'ready' | 'missing'>('ready')

  useEffect(() => {
    if (!jobId && !runId && !versionId) {
      setState('ready')
      return
    }
    const path = jobId
      ? `/api/v1/jobs/${jobId}`
      : runId
        ? `/api/v1/projects/${project.id}/unification/runs/${runId}`
        : `/api/v1/projects/${project.id}/contracts/versions/${versionId}`
    setState('loading')
    apiFetch(path).then(() => setState('ready')).catch(() => setState('missing'))
  }, [jobId, runId, versionId, project.id])

  const title = jobId ? '任务详情' : runId ? '实体统一详情' : versionId ? '契约详情' : '新建任务'

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between gap-3">
        <button onClick={onBack} className="inline-flex items-center gap-1.5 rounded-md border border-neutral-200 bg-white px-3 py-2 text-xs text-neutral-600 hover:bg-neutral-50">
          <ArrowLeft className="h-3.5 w-3.5" /> 返回任务
        </button>
        <h1 className="text-base font-semibold text-neutral-900">{title}</h1>
      </header>

      {state === 'loading' ? (
        <div className="rounded-xl border border-neutral-200 bg-white p-8 text-sm text-neutral-500">正在加载任务...</div>
      ) : state === 'missing' ? (
        <div className="rounded-xl border border-neutral-200 bg-white p-8 text-center">
          <h2 className="text-base font-semibold text-neutral-900">任务不存在</h2>
          <p className="mt-1 text-xs text-neutral-500">这条任务可能已被删除，或链接中的 id 不正确。</p>
          <button onClick={onBack} className="mt-4 rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white">返回任务</button>
        </div>
      ) : jobId || (newKind && newKind !== 'entity' && newKind !== 'contract') ? (
        <WorkbenchPage
          projectId={project.id}
          activeDsIds={activeDsIds}
          activeProfile={activeProfile}
          activeRouting={activeRouting}
          initialJobId={jobId}
          initialStage={jobId ? undefined : newStage(newKind)}
          onGoDataSources={onGoDataSources}
          onGoContracts={onGoContracts}
        />
      ) : runId || newKind === 'entity' ? (
        <UnificationPage projectId={project.id} canEdit={canEdit} initialRunId={runId} />
      ) : versionId || newKind === 'contract' ? (
        <ContractsPage projectId={project.id} canEdit={canEdit} initialVersionId={versionId} />
      ) : null}
    </div>
  )
}
