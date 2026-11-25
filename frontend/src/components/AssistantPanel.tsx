import React, { useEffect, useRef, useState } from 'react'
import { ArrowRight, Bot, CheckCircle2, ChevronRight, LoaderCircle, Send } from 'lucide-react'
import { apiFetch, DataSourceItem, JobItem } from '../lib/api'
import { explainPage } from '../lib/pageHelp'

type AssistantTaskKey = 'clean' | 'match' | 'entity' | 'metrics' | 'contract' | 'review'
type AssistantStage = 'idle' | 'running' | 'done' | 'error'

type TaskDef = {
  key: AssistantTaskKey
  label: string
  method: string
  stage: 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY' | 'REVIEW'
  demo?: 'L1' | 'L2' | 'L4'
}

const TASKS: TaskDef[] = [
  { key: 'clean', label: '清洗供应商名单', method: '规则清洗 + W3C 契约', stage: 'L1', demo: 'L1' },
  { key: 'match', label: '跨系统匹配客户', method: 'Splink + DuckDB', stage: 'L2', demo: 'L2' },
  { key: 'entity', label: '实体匹配', method: '规则候选 + 模型复核', stage: 'ENTITY' },
  { key: 'metrics', label: '统一 GMV 口径', method: '指标 YAML', stage: 'L4', demo: 'L4' },
  { key: 'contract', label: '生成数据契约', method: 'LinkML W3C 契约', stage: 'L3' },
  { key: 'review', label: '查看待复核', method: '人工复核', stage: 'REVIEW' },
]

const PARAM_LABELS: Record<string, string> = {
  target_col: '目标列',
  amount_col: '金额列',
  name_col: '名称列',
  tax_col: '税号列',
  phone_col: '电话列',
  city_col: '城市列',
  metric_name: '指标',
  group_by: '分组',
}

type PlanOption = { label: string; message: string }
type PlanPayload = {
  intent: string
  confidence: number
  explanation: string
  source: 'model' | 'rules'
  suggested_task: string | null
  needs_clarification: boolean
  question: string | null
  options: PlanOption[]
  datasource_id: string | null
  params: Record<string, string> | null
  recipe_id: string | null
}

type ChatMessage =
  | { id: number; role: 'user'; content: string }
  | { id: number; role: 'assistant'; content: string; plan: PlanPayload | null }

interface Props {
  projectId: string
  currentPage: string
  onRoute: (hash: string) => void
}

function taskForIntent(intent: string | null | undefined): TaskDef | undefined {
  if (!intent) return undefined
  return TASKS.find((task) => task.key === intent)
}

export default function AssistantPanel({ projectId, currentPage, onRoute }: Props) {
  const [open, setOpen] = useState(true)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [stage, setStage] = useState<AssistantStage>('idle')
  const [activeTask, setActiveTask] = useState<TaskDef | null>(null)
  const [datasources, setDatasources] = useState<DataSourceItem[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [routing, setRouting] = useState<any>(null)
  const [error, setError] = useState('')
  const [resultRoute, setResultRoute] = useState('')
  const [message, setMessage] = useState('')
  const [providerId, setProviderId] = useState('')
  const [thinking, setThinking] = useState(false)
  const idRef = useRef(0)
  const bottomRef = useRef<HTMLDivElement>(null)

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

  const pushUser = (content: string) => {
    idRef.current += 1
    setMessages((prev) => [...prev, { id: idRef.current, role: 'user', content }])
  }

  const pushAssistant = (content: string, plan: PlanPayload | null) => {
    idRef.current += 1
    setMessages((prev) => [...prev, { id: idRef.current, role: 'assistant', content, plan }])
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, thinking, stage])

  const requestPlan = (text: string, history: { role: 'user' | 'assistant'; content: string }[]) =>
    apiFetch<PlanPayload>(`/api/v1/projects/${projectId}/assistant/plan`, {
      method: 'POST',
      body: JSON.stringify({ message: text, page: currentPage, history }),
    })

  const submit = async (text: string) => {
    const history = messages.slice(-6).map((item) => ({ role: item.role, content: item.content }))
    pushUser(text)
    setStage('idle')
    setError('')
    setResultRoute('')
    setActiveTask(null)
    setThinking(true)
    try {
      const plan = await requestPlan(text, history)
      if (plan.datasource_id) {
        const source = datasources.find((item) => item.id === plan.datasource_id)
        if (source) {
          setSelectedId(source.id)
          setRouting(source.routing || null)
        }
      }
      pushAssistant(plan.explanation || '', plan)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '助手请求失败')
      setStage('error')
    } finally {
      setThinking(false)
    }
  }

  const sendText = () => {
    const text = message.trim()
    if (!text) return
    setMessage('')
    void submit(text)
  }

  const sendOption = (optionMessage: string) => {
    void submit(optionMessage)
  }

  const selectTask = (next: TaskDef) => {
    const source = datasources.find((item) => item.id === selectedId) || datasources[0]
    if (source) {
      setSelectedId(source.id)
      setRouting(source.routing || null)
    }
    const plan: PlanPayload = {
      intent: next.key,
      confidence: 1,
      explanation: `已选择：${next.label}。`,
      source: 'rules',
      suggested_task: next.key,
      needs_clarification: false,
      question: null,
      options: [],
      datasource_id: null,
      params: null,
      recipe_id: null,
    }
    setActiveTask(next)
    setStage('idle')
    setError('')
    setResultRoute('')
    pushAssistant(plan.explanation, plan)
  }

  const explainCurrentPage = async () => {
    const history = messages.slice(-6).map((item) => ({ role: item.role, content: item.content }))
    setStage('idle')
    setError('')
    setResultRoute('')
    setActiveTask(null)
    setThinking(true)
    try {
      const plan = await requestPlan(`请解释当前页面：${currentPage}`, history)
      if (plan.source === 'model') {
        pushAssistant(plan.explanation || explainPage(currentPage), null)
      } else {
        pushAssistant(explainPage(currentPage), null)
      }
    } catch {
      pushAssistant(explainPage(currentPage), null)
    } finally {
      setThinking(false)
    }
  }

  const run = async (plan: PlanPayload) => {
    const task = taskForIntent(plan.suggested_task || plan.intent)
    if (!task) return
    setActiveTask(task)
    setStage('running')
    setError('')
    setResultRoute('')
    try {
      let dsIds = selectedId ? [selectedId] : (plan.datasource_id ? [plan.datasource_id] : [])
      const source = datasources.find((item) => item.id === (selectedId || plan.datasource_id))
      let currentRouting = source?.routing || routing
      if (task.demo && !dsIds.length) {
        const demo = await apiFetch<{ datasource_ids: string[]; profile: any; routing: any }>(
          `/api/v1/projects/${projectId}/datasources/demo/${task.demo}`,
          { method: 'POST' },
        )
        dsIds = demo.datasource_ids
        currentRouting = demo.routing
      }

      if (task.stage === 'REVIEW') {
        setResultRoute('#/review')
        setStage('done')
        return
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
      const params = plan.params && Object.keys(plan.params).length > 0
        ? plan.params
        : task.stage === 'L1'
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

  const manualRoute = activeTask?.stage === 'L3' ? '#/tasks/new/contract'
    : activeTask?.stage === 'ENTITY' ? '#/tasks/new/entity'
      : activeTask?.stage === 'REVIEW' ? '#/review' : '#/data'

  const lastPlanIndex = (() => {
    for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
      const item = messages[idx]
      if (item.role === 'assistant' && item.plan && taskForIntent(item.plan.suggested_task || item.plan.intent)) {
        return idx
      }
    }
    return -1
  })()

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="fixed bottom-5 right-5 z-20 rounded-full bg-neutral-900 px-4 py-3 text-xs font-medium text-white shadow-lg">
        助手
      </button>
    )
  }

  return (
    <aside className="hidden w-[360px] shrink-0 border-l border-neutral-200 bg-white lg:flex lg:flex-col">
      <div className="flex items-center justify-between border-b border-neutral-100 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <Bot className="h-4 w-4 text-neutral-500" /> 助手
        </div>
        <button onClick={() => setOpen(false)} className="text-[11px] text-neutral-500 hover:text-neutral-900">收起</button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {messages.length === 0 && (
          <p className="text-xs leading-5 text-neutral-500">你想完成什么？试试下面的快捷任务，或直接输入一句话。</p>
        )}
        <div className="space-y-3">
          {messages.map((msg, idx) => {
            if (msg.role === 'user') {
              return (
                <div key={msg.id} className="flex justify-end">
                  <div className="max-w-[85%] rounded-lg bg-neutral-900 px-3 py-2 text-xs leading-5 text-white">{msg.content}</div>
                </div>
              )
            }
            const plan = msg.plan
            const planTask = plan ? taskForIntent(plan.suggested_task || plan.intent) : undefined
            const isLastPlan = idx === lastPlanIndex
            return (
              <div key={msg.id} className="flex justify-start">
                <div className="max-w-[85%] space-y-2">
                  {msg.content && (
                    <div className="rounded-lg bg-neutral-50 px-3 py-2 text-xs leading-5 text-neutral-700">{msg.content}</div>
                  )}
                  {plan?.needs_clarification && plan.question && (
                    <div className="rounded-lg border border-neutral-200 bg-white p-2.5">
                      <p className="text-xs leading-5 text-neutral-700">{plan.question}</p>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {plan.options.map((option) => (
                          <button
                            key={option.message}
                            onClick={() => sendOption(option.message)}
                            className="rounded-md border border-neutral-300 px-2 py-1 text-xs text-neutral-700 hover:bg-neutral-50"
                          >
                            {option.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {plan && planTask && (
                    <div className="rounded-lg border border-neutral-200 bg-white p-3">
                      <div className="text-xs font-semibold text-neutral-900">{planTask.label}</div>
                      <div className="mt-1 text-[11px] text-neutral-500">
                        建议方法：{routing?.method?.label || planTask.method}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
                        <span className="rounded border border-neutral-200 bg-neutral-50 px-1.5 py-0.5">
                          {plan.source === 'model' ? '模型理解' : '本地规则'}
                        </span>
                        {!providerId && plan.source === 'rules' && (
                          <button onClick={() => onRoute('#/settings')} className="text-neutral-600 underline">
                            配置模型
                          </button>
                        )}
                      </div>
                      {isLastPlan && datasources.length > 0 ? (
                        <div className="mt-2">
                          <div className="text-[11px] text-neutral-500">数据源：{datasources.find((item) => item.id === selectedId)?.name || '未选择'}</div>
                        <label className="mt-1 block text-[11px] text-neutral-500">
                          <select
                            value={selectedId}
                            onChange={(event) => {
                              setSelectedId(event.target.value)
                              const source = datasources.find((item) => item.id === event.target.value)
                              setRouting(source?.routing || null)
                            }}
                            className="mt-1 w-full rounded-md border border-neutral-300 px-2 py-1.5 text-xs text-neutral-800"
                          >
                            {datasources.map((source) => (
                              <option key={source.id} value={source.id}>{source.name}</option>
                            ))}
                          </select>
                        </label>
                        </div>
                      ) : (() => {
                        const source = datasources.find((item) => item.id === (plan.datasource_id || selectedId))
                        return source ? <div className="mt-1 text-[11px] text-neutral-500">数据源：{source.name}</div> : null
                      })()}
                      {plan.params && Object.keys(plan.params).length > 0 && (
                        <div className="mt-2 space-y-0.5 border-t border-neutral-100 pt-2">
                          {Object.entries(plan.params).map(([key, value]) => (
                            <div key={key} className="text-[11px] text-neutral-600">{PARAM_LABELS[key] || key}：{value}</div>
                          ))}
                        </div>
                      )}
                      <ol className="mt-2 space-y-1 text-[11px] text-neutral-500">
                        {['① 选数据', '② 运行', '③ 看结果'].map((step) => <li key={step}>{step}</li>)}
                      </ol>
                      {isLastPlan && stage === 'idle' && !plan.needs_clarification && (
                        <button onClick={() => void run(plan)} className="mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-neutral-900 px-3 py-2 text-xs font-medium text-white">
                          开始 <ArrowRight className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {isLastPlan && stage === 'running' && !plan.needs_clarification && (
                        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-neutral-500"><LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 正在执行...</p>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {thinking && (
            <div className="flex justify-start">
              <div className="rounded-lg bg-neutral-50 px-3 py-2 text-xs text-neutral-500">正在理解…</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {stage === 'done' && (
          <div className="mt-3 space-y-1.5">
            <p className="flex items-center gap-1.5 text-[11px] font-medium text-green-700"><CheckCircle2 className="h-3.5 w-3.5" /> 已完成</p>
            <button onClick={() => onRoute(resultRoute)} className="w-full rounded-md bg-neutral-900 px-3 py-2 text-xs font-medium text-white">查看结果</button>
            {activeTask?.stage === 'ENTITY' && <button onClick={() => onRoute(resultRoute)} className="w-full rounded-md border border-neutral-300 px-3 py-2 text-xs font-medium text-neutral-700">去复核</button>}
          </div>
        )}
        {stage === 'error' && (
          <div className="mt-3 space-y-1.5">
            <p role="alert" className="text-[11px] text-red-600">{error}</p>
            <button onClick={() => onRoute(manualRoute)} className="w-full rounded-md border border-neutral-300 px-3 py-2 text-xs font-medium text-neutral-700">去手动处理</button>
          </div>
        )}
      </div>

      <div className="border-t border-neutral-100 px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {TASKS.map((item) => (
            <button
              key={item.key}
              onClick={() => selectTask(item)}
              className="flex items-center gap-1 rounded-md border border-neutral-200 px-2 py-1 text-[11px] text-neutral-700 hover:bg-neutral-50"
            >
              {item.label} <ChevronRight className="h-3 w-3" />
            </button>
          ))}
        </div>
        <div className="mt-2 flex gap-1.5">
          <input
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') sendText() }}
            placeholder="也可以直接说，例如：帮我把客户名单去重"
            className="min-w-0 flex-1 rounded-md border border-neutral-300 px-2.5 py-2 text-xs"
          />
          <button onClick={sendText} className="rounded-md bg-neutral-900 px-2.5 text-white" aria-label="发送">
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>
        <button onClick={() => void explainCurrentPage()} className="mt-2 text-left text-[11px] text-neutral-500 underline">
          解释当前页面
        </button>
      </div>
    </aside>
  )
}
