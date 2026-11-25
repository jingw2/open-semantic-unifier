import { apiFetch, JobItem } from './api'

export type TaskType = 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY'

export type TaskItem = {
  id: string
  type: TaskType
  title: string
  status: string
  createdAt: string
  detail:
    | { kind: 'job'; jobId: string; stage: 'L1' | 'L2' | 'L4' }
    | { kind: 'run'; runId: string }
    | { kind: 'contract'; versionId: string }
}

const JOB_TITLES: Record<string, string> = {
  L1: 'L1 小表清洗',
  L2: 'L2 跨库匹配',
  L4: 'L4 指标统一',
}

export async function loadTasks(projectId: string): Promise<TaskItem[]> {
  const [jobs, runs, versions] = await Promise.all([
    apiFetch<JobItem[]>(`/api/v1/projects/${projectId}/jobs`),
    apiFetch<{ items: any[] }>(`/api/v1/projects/${projectId}/unification/runs?limit=50&offset=0`),
    apiFetch<{ items: any[] }>(`/api/v1/projects/${projectId}/contracts/versions?limit=50`),
  ])
  const items: TaskItem[] = []
  for (const job of jobs) {
    if (job.stage !== 'L1' && job.stage !== 'L2' && job.stage !== 'L4') continue
    items.push({
      id: job.id,
      type: job.stage as TaskType,
      title: JOB_TITLES[job.stage] || job.stage,
      status: job.status,
      createdAt: job.created_at || '',
      detail: { kind: 'job', jobId: job.id, stage: job.stage as 'L1' | 'L2' | 'L4' },
    })
  }
  for (const run of runs.items || []) {
    items.push({
      id: run.run_id,
      type: 'ENTITY',
      title: `实体统一 · ${run.input_version}`,
      status: run.status,
      createdAt: run.created_at || '',
      detail: { kind: 'run', runId: run.run_id },
    })
  }
  for (const version of versions.items || []) {
    items.push({
      id: version.id,
      type: 'L3',
      title: `契约 v${version.version}`,
      status: version.status,
      createdAt: version.created_at || '',
      detail: { kind: 'contract', versionId: version.id },
    })
  }
  return items.sort((left, right) => (right.createdAt || '').localeCompare(left.createdAt || ''))
}
