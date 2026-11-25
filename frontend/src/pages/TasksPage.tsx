import React, { useEffect, useState } from 'react'
import { Download, GitMerge, ListChecks, Play, Plus, Rocket, Rows3 } from 'lucide-react'
import { loadTasks, TaskItem } from '../lib/tasks'
import { NewTaskKind, routeHash } from '../lib/router'

interface Props {
  projectId: string
  onRoute: (hash: string) => void
}

const NEW_TASKS: Array<{ kind: NewTaskKind; label: string; icon: React.ReactNode }> = [
  { kind: 'clean', label: '清洗字段', icon: <Rows3 className="h-4 w-4" /> },
  { kind: 'match', label: '跨系统匹配字段', icon: <Play className="h-4 w-4" /> },
  { kind: 'entity', label: '实体匹配字段', icon: <GitMerge className="h-4 w-4" /> },
  { kind: 'metrics', label: '统一指标口径', icon: <ListChecks className="h-4 w-4" /> },
  { kind: 'contract', label: '生成数据契约', icon: <Download className="h-4 w-4" /> },
]

function taskHash(task: TaskItem): string {
  if (task.detail.kind === 'job') return routeHash({ view: 'tasks-job', jobId: task.detail.jobId })
  if (task.detail.kind === 'run') return routeHash({ view: 'tasks-run', runId: task.detail.runId })
  return routeHash({ view: 'tasks-contract', versionId: task.detail.versionId })
}

export default function TasksPage({ projectId, onRoute }: Props) {
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    loadTasks(projectId)
      .then((items) => setTasks(items))
      .catch((caught) => setError(caught instanceof Error ? caught.message : '任务加载失败'))
      .finally(() => setLoading(false))
  }, [projectId])

  return (
    <div className="space-y-5">
      <header className="rounded-xl border border-neutral-200 bg-white p-6">
        <h1 className="text-xl font-semibold text-neutral-900">任务</h1>
        <p className="mt-1.5 text-sm text-neutral-500">从一次清洗、匹配或契约开始。已有任务会出现在下面。</p>
      </header>

      <div className="flex flex-wrap gap-2">
        {NEW_TASKS.map((task) => (
          <button
            key={task.kind}
            onClick={() => onRoute(routeHash({ view: 'tasks-new', kind: task.kind }))}
            className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-2 text-xs font-medium text-white hover:bg-neutral-800"
          >
            <Plus className="h-3.5 w-3.5" /> {task.label}
          </button>
        ))}
        <button
          onClick={() => onRoute('#/tasks/publish')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-3 py-2 text-xs font-medium text-neutral-700 hover:bg-neutral-50"
        >
          <Rocket className="h-3.5 w-3.5" /> 产物与发布
        </button>
      </div>

      <section className="rounded-xl border border-neutral-200 bg-white p-5">
        <h2 className="mb-3 text-sm font-semibold text-neutral-900">最近任务</h2>
        {loading ? (
          <p className="text-xs text-neutral-500">加载中...</p>
        ) : error ? (
          <p role="alert" className="text-xs text-red-600">{error}</p>
        ) : tasks.length === 0 ? (
          <p className="text-xs text-neutral-500">还没有任务。选一个上面的入口开始。</p>
        ) : (
          <div className="space-y-2">
            {tasks.map((task) => (
              <button
                key={`${task.type}:${task.id}`}
                onClick={() => onRoute(taskHash(task))}
                className="flex w-full items-center justify-between gap-3 rounded-lg border border-neutral-200 px-4 py-3 text-left hover:bg-neutral-50"
              >
                <div>
                  <div className="text-sm font-medium text-neutral-900">{task.title}</div>
                  <div className="mt-1 text-[11px] text-neutral-500">{task.type} · {task.createdAt ? new Date(task.createdAt).toLocaleString() : '—'}</div>
                </div>
                <span className="rounded-md bg-neutral-100 px-2 py-1 text-[10px] font-medium text-neutral-600">{task.status}</span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
