import React, { useEffect, useState } from 'react'
import { ArrowRight, Bot, CheckCircle2, ChevronRight, LoaderCircle, Send } from 'lucide-react'
import { apiFetch, DataSourceItem, JobItem } from '../lib/api'
import { explainPage } from '../lib/pageHelp'

type AssistantTaskKey = 'clean' | 'match' | 'entity' | 'metrics' | 'contract'
type AssistantStage = 'welcome' | 'plan' | 'running' | 'done' | 'error'

type TaskDef = {
  key: AssistantTaskKey
  label: string
  method: string
  stage: 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY'
  demo?: 'L1' | 'L2' | 'L4'
}

const TASKS: TaskDef[] = [
  { key: 'clean', label: '清洗供应商名单', method: '规则清洗 + W3C 契约', stage: 'L1', demo: 'L1' },
  { key: 'match', label: '跨系统匹配客户', method: 'Splink + DuckDB', stage: 'L2', demo: 'L2' },
  { key: 'entity', label: '实体匹配', method: '规则候选 + 模型复核', stage: 'ENTITY' },
  { key: 'metrics', label: '统一 GMV 口径', method: '指标 YAML', stage: 'L4', demo: 'L4' },
  { key: 'contract', label: '生成数据契约', method: 'LinkML W3C 契约', stage: 'L3' },
]

interface Props {
  projectId: string
  currentPage: string
  onRoute: (hash: string) => void
}

function taskForIntent(intent: string): TaskDef | undefined {
  return TASKS.find((task) => task.key === intent)
}

export default function AssistantPanel({ projectId, currentPage, onRoute }: Props) {
  const [open, setOpen] = useState(true)
  const [stage, setStage] = useState<AssistantStage>('welcome')
  const [task, setTask] = useState<TaskDef | null>(null)
  const [datasources, setDatasources] = useState<DataSourceItem[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [routing, setRouting] = useState<any>(null)
  const [explanation, setExplanation] = useState('')
  const [steps, setSteps] = useState<string[]>([])
  const [error, setError] = useState('')
  const [resultRoute, setResultRoute] = useState('')
  const [message, setMessage] = useState('')
  const [sourceNote, setSourceNote] = useState('')
  const [providerId, setProviderId] = useState('')

  useEffect(() => {
    if (!projectId) return
    apiFetch<DataSourceItem[]>(`/api/v1/projects/${projectId}/datasources`)
      .then((items) => {
        setDatasources(items)
        if (!selectedId && items[0]) setSelectedId(items[0].id)
      })
      .catch(() => undefined)
    apiFetch<{ configured: any[] }>('/api/v1/providers')
      .then((data) => {
        const chat = data.configured.find((p) => p.provider_kind === 'openai_compatible' && p.is_default)
          || data.configured.find((p) => p.provider_kind === 'openai_compatible')
        if (chat) setProviderId(chat.id)
      })
      .catch(() => undefined)
  }, [projectId])

  const selectTask = (next: TaskDef) => {
    setTask(next)
    setStage('plan')
    setError('')
    setResultRoute('')
    setExplanation('')
    const source = datasources.find((item) => item.id === selectedId) || datasources[0]
    setSelectedId(source?.id || '')
    setRouting(source?.routing || null)
    setSteps(['① 选数据', '② 运行', '③ 看结果'])
  }

  const sendText = async () => {
    if (!message.trim()) return
    try {
      const plan = await apiFetch<{ intent: string; explanation: string; source: string }>(
        `/api/v1/projects/${projectId}/assistant/plan`,
        { method: 'POST', body: JSON.stringify({ message: message.trim(), page: currentPage }) },
      )
      setSourceNote(plan.source === 'rules' ? '本地规则' : '')
      setExplanation(plan.explanation)
      const next = taskForIntent(plan.intent)
      if (next) selectTask(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '助手请求失败')
      setStage('error')
    }
  }

  const run = async () => {
    if (!task) return
    setStage('running')
    setError('')
    setSteps(['① 选数据', '② 运行', '③ 看结果'])
    try {
      let dsIds = selectedId ? [selectedId] : []
      let currentRouting = routing
      if (task.demo && !selectedId) {
        const demo = await apiFetch<{ datasource_ids: string[]; profile: any; routing: any }>(
          `/api/v1/projects/${projectId}/datasources/demo/${task.demo}`,
          { method: 'POST' },
        )
        dsIds = demo.datasource_ids
        currentRouting = demo.routing
        setSteps((items) => [...items, '已加载示例数据'])
      }

      if (task.stage === 'ENTITY') {
        const demo = await apiFetch<{ run_id: string }>(`/api/v1/projects/${projectId}/unification/demo`, { method: 'POST' })
        let detail: any = null
        for (let attempt = 0; attempt < 120; attempt += 1) {
          detail = await apiFetch(`/api/v1/projects/${projectId}/unification/runs/${demo.run_id}`)
          if (detail.status === 'SUCCEEDED' || detail.status === 'FAILED') break
          await new Promise((resolve) => setTimeout(resolve, 500))
        }
        if (detail?.status !== 'SUCCEEDED') throw new Error(detail?.error_message || '实体运行失败')
        setResultRoute(`#/tasks/run/${demo.run_id}`)
        setStage('done')
        return
      }

      if (task.stage === 'L3') {
        const [sources, snapshots] = await Promise.all([
          apiFetch<any[]>(`/api/v1/projects/${projectId}/contracts/sources`),
          apiFetch<{ items: any[] }>(`/api/v1/projects/${projectId}/unification/snapshots?limit=1&offset=0`),
        ])
        if (!sources.length || !snapshots.items?.length) {
          throw new Error('还没有契约源或实体快照，请先去契约统一工作台准备。')
        }
        const version = await apiFetch<{ id: string }>(`/api/v1/projects/${projectId}/contracts/versions`, {
          method: 'POST',
          body: JSON.stringify({ source_id: sources[0].id, snapshot_id: snapshots.items[0].snapshot_id, allowed_new_entity_types: [] }),
        })
        setResultRoute(`#/tasks/contract/${version.id}`)
        setStage('done')
        return
      }

      const sp = currentRouting?.suggested_params || {}
      const params = task.stage === 'L1'
        ? { target_col: sp.name_col, amount_col: sp.amount_col }
        : task.stage === 'L2'
          ? { name_col: sp.name_col, tax_col: sp.tax_col, phone_col: sp.phone_col, city_col: sp.city_col, enable_hybrid_llm: true }
          : { metric_name: 'net_gmv', group_by: 'order_channel' }
      const created = await apiFetch<JobItem>(`/api/v1/projects/${projectId}/jobs`, {
        method: 'POST',
        body: JSON.stringify({ stage: task.stage, datasource_ids: dsIds, provider_id: providerId || null, params }),
      })
      let current = created
      for (let attempt = 0; attempt < 240; attempt += 1) {
        if (current.status === 'SUCCESS' || current.status === 'FAILED') break
        await new Promise((resolve) => setTimeout(resolve, 500))
        current = await apiFetch<JobItem>(`/api/v1/jobs/${created.id}`)
      }
      if (current.status !== 'SUCCESS') throw new Error(current.error_message || '任务运行失败')
      setResultRoute(`#/tasks/job/${created.id}`)
      setStage('done')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '运行失败')
      setStage('error')
    }
  }

  const manualRoute = task?.stage === 'L3' ? '#/tasks/new/contract'
    : task?.stage === 'ENTITY' ? '#/tasks/new/entity' : '#/data'
  const methodLabel = routing?.method?.label || task?.method || ''

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="fixed bottom-5 right-5 z-20 rounded-full bg-neutral-900 px-4 py-3 text-xs font-medium text-white shadow-lg">
        助手
      </button>
    )
  }

  return (
    <aside className="hidden w-80 shrink-0 border-l border-neutral-200 bg-white p-4 lg:flex lg:flex-col">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <Bot className="h-4 w-4 text-neutral-500" /> 助手
        </div>
        <button onClick={() => setOpen(false)} className="text-[11px] text-neutral-500 hover:text-neutral-900">收起</button>
      </div>

      <p className="mt-3 text-xs leading-5 text-neutral-500">你想完成什么？</p>
      <div className="mt-2 grid grid-cols-1 gap-1.5">
        {TASKS.map((item) => (
          <button
            key={item.key}
            onClick={() => selectTask(item)}
            className={`flex items-center justify-between rounded-md border px-3 py-2 text-left text-xs ${
              task?.key === item.key ? 'border-neutral-900 bg-neutral-50 text-neutral-900' : 'border-neutral-200 text-neutral-700 hover:bg-neutral-50'
            }`}
          >
            {item.label} <ChevronRight className="h-3.5 w-3.5" />
          </button>
        ))}
      </div>

      <div className="mt-3 flex gap-1.5">
        <input
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') void sendText() }}
          placeholder="也可以直接说，例如：帮我把客户名单去重"
          className="min-w-0 flex-1 rounded-md border border-neutral-300 px-2.5 py-2 text-xs"
        />
        <button onClick={() => void sendText()} className="rounded-md bg-neutral-900 px-2.5 text-white" aria-label="发送">
          <Send className="h-3.5 w-3.5" />
        </button>
      </div>
      {sourceNote && <p className="mt-1 text-[10px] text-neutral-400">{sourceNote}</p>}
      {explanation && <p className="mt-2 rounded-md bg-neutral-50 p-2 text-[11px] leading-5 text-neutral-600">{explanation}</p>}

      {task && stage !== 'welcome' && (
        <div className="mt-3 rounded-lg border border-neutral-200 p-3">
          <div className="text-xs font-semibold text-neutral-900">{task.label}</div>
          <div className="mt-1 text-[11px] text-neutral-500">建议方法：{methodLabel}</div>
          {routing?.method?.reason && <p className="mt-1 text-[11px] leading-5 text-neutral-500">{routing.method.reason}</p>}
          {datasources.length > 0 && (
            <select
              value={selectedId}
              onChange={(event) => {
                setSelectedId(event.target.value)
                const source = datasources.find((item) => item.id === event.target.value)
                setRouting(source?.routing || null)
              }}
              className="mt-2 w-full rounded-md border border-neutral-300 px-2 py-1.5 text-[11px]"
            >
              {datasources.map((source) => (
                <option key={source.id} value={source.id}>{source.name}</option>
              ))}
            </select>
          )}
          <ol className="mt-2 space-y-1 text-[11px] text-neutral-500">
            {steps.map((step) => <li key={step}>{step}</li>)}
          </ol>
          {stage === 'plan' && (
            <button onClick={() => void run()} className="mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-neutral-900 px-3 py-2 text-xs font-medium text-white">
              开始 <ArrowRight className="h-3.5 w-3.5" />
            </button>
          )}
          {stage === 'running' && <p className="mt-2 flex items-center gap-1.5 text-[11px] text-neutral-500"><LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 正在执行...</p>}
          {stage === 'done' && (
            <div className="mt-2 space-y-1.5">
              <p className="flex items-center gap-1.5 text-[11px] font-medium text-green-700"><CheckCircle2 className="h-3.5 w-3.5" /> 已完成</p>
              <button onClick={() => onRoute(resultRoute)} className="w-full rounded-md bg-neutral-900 px-3 py-2 text-xs font-medium text-white">查看结果</button>
              {task.stage === 'ENTITY' && <button onClick={() => onRoute(resultRoute)} className="w-full rounded-md border border-neutral-300 px-3 py-2 text-xs font-medium text-neutral-700">去复核</button>}
            </div>
          )}
          {stage === 'error' && (
            <div className="mt-2 space-y-1.5">
              <p role="alert" className="text-[11px] text-red-600">{error}</p>
              <button onClick={() => onRoute(manualRoute)} className="w-full rounded-md border border-neutral-300 px-3 py-2 text-xs font-medium text-neutral-700">去手动处理</button>
            </div>
          )}
        </div>
      )}

      <button onClick={() => setExplanation(explainPage(currentPage))} className="mt-auto pt-3 text-left text-[11px] text-neutral-500 underline">
        解释当前页面
      </button>
    </aside>
  )
}
