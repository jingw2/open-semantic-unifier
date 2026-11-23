import React, { useEffect, useMemo, useState } from 'react'
import { ArrowDownToLine, ArrowRight, Check, CircleAlert, GitBranch, LoaderCircle, Play, RotateCcw, Save, ScanSearch, ShieldCheck, Sparkles } from 'lucide-react'
import { apiFetch, downloadApi, UnificationRun, UnificationScheme, UnificationSnapshot } from '../lib/api'

type Tab = 'configure' | 'run' | 'review' | 'release'
type BatchRecord = {
  source_system: string
  dataset: string
  source_record_id?: string
  attributes: Record<string, unknown>
}
type ReviewPair = {
  left_source_revision_id: string
  right_source_revision_id: string
  candidate: boolean
  action: string
  evidence_codes?: string[]
  evidence?: Array<Record<string, unknown>>
  recommendation?: { reason?: string; is_same_entity?: boolean; confidence?: number } | null
  model_error?: string | null
}
type SnapshotEntities = {
  snapshot_id: string
  version: number
  fields: string[]
  items: Array<{
    entity_id: string
    entity_type: string
    status: string
    attributes: Record<string, unknown>
    members: Array<{ source_key: string[]; source_revision_id: string | null }>
    conflicts: Array<{ field: string; values: unknown[] }>
  }>
  total: number
}
type IdentityChange = {
  change_id: string
  revision: number
  change_type: string
  actor_id: string
  reason: string
  payload?: Record<string, unknown>
  created_at: string
  trace_id: string | null
}

const starterScheme: UnificationScheme = {
  revision: 1,
  entity_type: 'supplier',
  identity_granularity: 'legal_entity',
  field_map: { name: 'name', tax_id: 'tax_id' },
  normalizers: ['unicode_nfkc', 'trim', 'casefold', 'strip_punctuation'],
  strong_identifiers: ['tax_id'],
  conflict_constraints: [{ field: 'tax_id', action: 'veto_auto_merge' }],
  candidate_rules: [{ kind: 'exact', fields: ['tax_id'] }, { kind: 'name_similarity', field: 'name', threshold: 0.82 }],
  attribute_policies: {},
  model_judgment_enabled: false,
  model_provider_id: null,
  model_failure_policy: 'REVIEW',
  prompt_version: 'entity-pair-v1',
}

const starterRecords = JSON.stringify([
  { source_system: 'erp', dataset: 'suppliers', source_record_id: 'S-1001', attributes: { name: 'Acme Ltd', tax_id: 'CN-91310000A' } },
  { source_system: 'crm', dataset: 'partners', source_record_id: 'P-77', attributes: { name: 'ACME, Ltd.', tax_id: 'CN-91310000A' } },
  { source_system: 'crm', dataset: 'partners', source_record_id: 'P-78', attributes: { name: 'Acme Trading', tax_id: 'CN-91310000B' } },
], null, 2)

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试。'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isConflictConstraint(value: unknown): value is { field: string; action: 'veto_auto_merge' } {
  return isRecord(value) && typeof value.field === 'string' && value.action === 'veto_auto_merge'
}

function parseBatch(text: string, mode: 'sync' | 'exploration'): BatchRecord[] {
  const value: unknown = JSON.parse(text)
  if (!Array.isArray(value) || value.length === 0) throw new Error('批次必须是至少包含一条记录的 JSON 数组。')
  return value.map((item, index) => {
    if (!isRecord(item)) throw new Error(`第 ${index + 1} 条记录必须是对象。`)
    const row = item
    if (typeof row.source_system !== 'string' || !row.source_system.trim()
      || typeof row.dataset !== 'string' || !row.dataset.trim()
      || (mode === 'sync' && (typeof row.source_record_id !== 'string' || !row.source_record_id.trim()))
      || (row.source_record_id !== undefined && typeof row.source_record_id !== 'string')
      || !isRecord(row.attributes)) {
      throw new Error(`第 ${index + 1} 条记录需要 source_system、dataset、source_record_id 和 attributes。`)
    }
    return {
      source_system: row.source_system,
      dataset: row.dataset,
      source_record_id: row.source_record_id,
      attributes: row.attributes,
    }
  })
}

export default function UnificationPage({ projectId, canEdit }: { projectId: string; canEdit: boolean }) {
  const [tab, setTab] = useState<Tab>('configure')
  const [schemeKey, setSchemeKey] = useState('suppliers')
  const [scheme, setScheme] = useState<UnificationScheme>(starterScheme)
  const [fieldMapDraft, setFieldMapDraft] = useState(JSON.stringify(starterScheme.field_map, null, 2))
  const [candidateRulesDraft, setCandidateRulesDraft] = useState(JSON.stringify(starterScheme.candidate_rules, null, 2))
  const [conflictConstraintsDraft, setConflictConstraintsDraft] = useState(JSON.stringify(starterScheme.conflict_constraints, null, 2))
  const [schemes, setSchemes] = useState<UnificationScheme[]>([])
  const [recordsText, setRecordsText] = useState(starterRecords)
  const [inputVersion, setInputVersion] = useState(`input-${new Date().toISOString().slice(0, 10)}`)
  const [ingestMode, setIngestMode] = useState<'sync' | 'exploration'>('sync')
  const [schemeSaved, setSchemeSaved] = useState(false)
  const [batchImported, setBatchImported] = useState(false)
  const [runs, setRuns] = useState<UnificationRun[]>([])
  const [activeRun, setActiveRun] = useState<UnificationRun | null>(null)
  const [reviewPairs, setReviewPairs] = useState<ReviewPair[]>([])
  const [snapshots, setSnapshots] = useState<UnificationSnapshot[]>([])
  const [currentSnapshotId, setCurrentSnapshotId] = useState<string | null>(null)
  const [currentSnapshotVersion, setCurrentSnapshotVersion] = useState(0)
  const [snapshotEntities, setSnapshotEntities] = useState<SnapshotEntities | null>(null)
  const [overrideFields, setOverrideFields] = useState<Record<string, string>>({})
  const [identityChanges, setIdentityChanges] = useState<IdentityChange[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const base = `/api/v1/projects/${projectId}/unification`
  const latestIdentityRevision = identityChanges[0]?.revision ?? 1
  const phoenixUiUrl = (import.meta.env.VITE_PHOENIX_UI_URL || 'http://localhost:6006').replace(/\/$/, '')
  const phoenixProjectId = import.meta.env.VITE_PHOENIX_PROJECT_ID
  const succeededRuns = useMemo(() => runs.filter((run) => run.status === 'SUCCEEDED'), [runs])

  const loadWorkspace = async () => {
    setError('')
    try {
      const [schemeList, runPage, snapshotPage, changePage] = await Promise.all([
        apiFetch<UnificationScheme[]>(`${base}/schemes`),
        apiFetch<{ items: UnificationRun[] }>(`${base}/runs?limit=20&offset=0`),
        apiFetch<{ items: UnificationSnapshot[]; current_snapshot_id: string | null; current_version: number }>(`${base}/snapshots?limit=20&offset=0`),
        apiFetch<{ items: IdentityChange[] }>(`${base}/identity-changes?limit=50&offset=0`),
      ])
      setSchemes(schemeList)
      setRuns(runPage.items)
      setSnapshots(snapshotPage.items)
      setCurrentSnapshotId(snapshotPage.current_snapshot_id)
      setCurrentSnapshotVersion(snapshotPage.current_version)
      setIdentityChanges(changePage.items)
      const found = schemeList.find((item) => item.scheme_key === schemeKey)
      if (found) {
        setScheme(found)
        setFieldMapDraft(JSON.stringify(found.field_map, null, 2))
        setCandidateRulesDraft(JSON.stringify(found.candidate_rules, null, 2))
        setConflictConstraintsDraft(JSON.stringify(found.conflict_constraints, null, 2))
        setSchemeSaved(true)
      }
      if (!activeRun && runPage.items[0]) setActiveRun(runPage.items[0])
    } catch (caught) {
      setError(errorText(caught))
    }
  }

  useEffect(() => {
    setSchemes([])
    setRuns([])
    setSnapshots([])
    setIdentityChanges([])
    setActiveRun(null)
    setSnapshotEntities(null)
    setSchemeSaved(false)
    setBatchImported(false)
    void loadWorkspace()
  }, [projectId])

  useEffect(() => {
    if (!activeRun || activeRun.status !== 'SUCCEEDED') {
      setReviewPairs([])
      return
    }
    apiFetch<{ items: ReviewPair[] }>(`${base}/reviews?run_id=${encodeURIComponent(activeRun.run_id)}&limit=100&offset=0`)
      .then((result) => setReviewPairs(result.items))
      .catch((caught: unknown) => setError(errorText(caught)))
  }, [activeRun?.run_id, activeRun?.status, projectId])

  useEffect(() => {
    if (!currentSnapshotId) {
      setSnapshotEntities(null)
      return
    }
    apiFetch<SnapshotEntities>(`${base}/entities?snapshot_id=${encodeURIComponent(currentSnapshotId)}&limit=100&offset=0`)
      .then(setSnapshotEntities)
      .catch((caught: unknown) => setError(errorText(caught)))
  }, [currentSnapshotId, projectId])

  const updateScheme = (update: Partial<UnificationScheme>) => {
    setScheme((current) => ({ ...current, ...update }))
    if (update.field_map) setFieldMapDraft(JSON.stringify(update.field_map, null, 2))
    if (update.candidate_rules) setCandidateRulesDraft(JSON.stringify(update.candidate_rules, null, 2))
    if (update.conflict_constraints) setConflictConstraintsDraft(JSON.stringify(update.conflict_constraints, null, 2))
    setSchemeSaved(false)
  }

  const saveScheme = async () => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const fieldMapInput: unknown = JSON.parse(fieldMapDraft)
      if (!isRecord(fieldMapInput) || !Object.values(fieldMapInput).every((value) => typeof value === 'string')) {
        throw new Error('标准字段映射必须是字符串到字符串的 JSON 对象。')
      }
      const fieldMap: Record<string, string> = {}
      for (const [canonical, source] of Object.entries(fieldMapInput)) {
        if (typeof source === 'string') fieldMap[canonical] = source
      }
      const candidateRules: unknown = JSON.parse(candidateRulesDraft)
      if (!Array.isArray(candidateRules) || !candidateRules.every(isRecord)) {
        throw new Error('候选规则必须是 JSON 对象数组。')
      }
      const conflictConstraints: unknown = JSON.parse(conflictConstraintsDraft)
      if (!Array.isArray(conflictConstraints) || !conflictConstraints.every(isConflictConstraint)) {
        throw new Error('冲突约束必须是包含 field 和 veto_auto_merge action 的 JSON 数组。')
      }
      const schemePayload = { ...scheme }
      schemePayload.field_map = fieldMap
      schemePayload.candidate_rules = candidateRules
      schemePayload.conflict_constraints = conflictConstraints
      delete schemePayload.scheme_key
      const saved = await apiFetch<UnificationScheme>(`${base}/schemes/${encodeURIComponent(schemeKey)}`, {
        method: 'PUT', body: JSON.stringify(schemePayload),
      })
      setScheme({ ...saved, scheme_key: schemeKey })
      setSchemes((items) => [saved, ...items.filter((item) => item.scheme_key !== schemeKey)])
      setSchemeSaved(true)
      setNotice(`方案已保存为第 ${saved.revision} 版。`)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const importBatch = async () => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const records = parseBatch(recordsText, ingestMode)
      const result = await apiFetch<{ created_records: number; created_revisions: number }>(`${base}/batches`, {
        method: 'POST',
        body: JSON.stringify({ input_version: inputVersion, mode: ingestMode, records }),
      })
      setBatchImported(true)
      setNotice(`批次已接收：${result.created_records} 条新来源记录，${result.created_revisions} 条新修订。`)
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const runMatching = async () => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const submitted = await apiFetch<{ run_id: string; status: UnificationRun['status'] }>(`${base}/runs`, {
        method: 'POST',
        headers: { 'Idempotency-Key': `${projectId}-${inputVersion}-${Date.now()}` },
        body: JSON.stringify({ scheme_key: schemeKey, scheme_revision: scheme.revision, input_version: inputVersion }),
      })
      let current = await apiFetch<UnificationRun>(`${base}/runs/${submitted.run_id}`)
      for (let attempt = 0; (current.status === 'PENDING' || current.status === 'RUNNING') && attempt < 120; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        current = await apiFetch<UnificationRun>(`${base}/runs/${submitted.run_id}`)
      }
      setActiveRun(current)
      if (current.status === 'SUCCEEDED') {
        setNotice(`运行完成：共检查 ${current.result?.pairs?.length ?? 0} 组配对。`)
        setTab('review')
      } else {
        setError(current.error_message || (current.status === 'FAILED' ? '运行失败，请检查方案和输入。' : '运行仍在队列中，可在运行记录中查看进度。'))
        setTab('run')
      }
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const chooseRun = async (runId: string) => {
    const selected = await apiFetch<UnificationRun>(`${base}/runs/${runId}`)
    setActiveRun(selected)
    setTab('run')
  }

  const decideReview = async (pair: ReviewPair, action: 'MERGE' | 'KEEP_DISTINCT') => {
    const reason = window.prompt(action === 'MERGE' ? '记录合并原因' : '记录保持分离的原因')
    if (!reason?.trim() || !activeRun) return
    setBusy(true)
    setError('')
    try {
      await apiFetch(`${base}/reviews`, {
        method: 'POST',
        body: JSON.stringify({
          run_id: activeRun.run_id,
          left_source_revision_id: pair.left_source_revision_id,
          right_source_revision_id: pair.right_source_revision_id,
          action,
          reason: reason.trim(),
          expected_revision: latestIdentityRevision,
        }),
      })
      setNotice(action === 'MERGE' ? '已记录合并裁决。' : '已记录保持分离的裁决。')
      await loadWorkspace()
      const remaining = await apiFetch<{ items: ReviewPair[] }>(`${base}/reviews?run_id=${encodeURIComponent(activeRun.run_id)}&limit=100&offset=0`)
      setReviewPairs(remaining.items)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const publish = async () => {
    if (!activeRun || activeRun.status !== 'SUCCEEDED') return
    setBusy(true)
    setError('')
    try {
      const snapshot = await apiFetch<{ snapshot_id: string; version: number }>(`${base}/snapshots`, {
        method: 'POST',
        body: JSON.stringify({ run_id: activeRun.run_id, expected_current_version: currentSnapshotVersion }),
      })
      setNotice(`已发布快照 v${snapshot.version}。`)
      setCurrentSnapshotId(snapshot.snapshot_id)
      setCurrentSnapshotVersion(snapshot.version)
      setTab('release')
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const rollback = async (snapshot: UnificationSnapshot) => {
    if (!window.confirm(`将快照 v${snapshot.version} 的内容恢复为一个新版本？`)) return
    const reason = window.prompt('记录回滚原因')
    if (!reason?.trim()) return
    setBusy(true)
    setError('')
    try {
      const restored = await apiFetch<{ snapshot_id: string; version: number }>(`${base}/snapshots/${snapshot.snapshot_id}/rollback`, { method: 'POST', body: JSON.stringify({ reason: reason.trim() }) })
      setNotice(`已创建回滚快照 v${restored.version}。`)
      setCurrentSnapshotId(restored.snapshot_id)
      setCurrentSnapshotVersion(restored.version)
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const overrideAttribute = async (entityId: string) => {
    const field = overrideFields[entityId] || snapshotEntities?.fields[0]
    if (!field || !snapshotEntities || snapshotEntities.snapshot_id !== currentSnapshotId) return
    setBusy(true)
    setError('')
    try {
      const preview = await apiFetch<{ identity_revision: number; member_revisions: [string, string][]; members: unknown[] }>(
        `${base}/entities/${entityId}/attribute-overrides/preview?snapshot_id=${encodeURIComponent(snapshotEntities.snapshot_id)}&field=${encodeURIComponent(field)}`,
      )
      if (!window.confirm(`请核对当前来源成员和属性：\n${JSON.stringify(preview.members, null, 2)}`)) return
      const valueText = window.prompt('新的属性值（JSON，例如 "新名称"）')
      if (valueText === null) return
      let value: unknown
      try { value = JSON.parse(valueText) } catch { setError('属性值必须是有效 JSON。'); return }
      const reason = window.prompt('记录覆盖原因')
      if (!reason?.trim()) return
      await apiFetch(`${base}/entities/${entityId}/attribute-overrides`, {
        method: 'POST', body: JSON.stringify({ snapshot_id: snapshotEntities.snapshot_id, field, value, reason: reason.trim(), expected_revision: preview.identity_revision, member_revisions: preview.member_revisions }),
      })
      setNotice('属性覆盖已记录，请重新运行并发布新快照。')
      await loadWorkspace()
    } catch (caught) { setError(errorText(caught)) } finally { setBusy(false) }
  }

  const undoChange = async (change: IdentityChange) => {
    const reason = window.prompt('撤销原因')
    if (!reason?.trim()) return
    setBusy(true)
    setError('')
    try {
      await apiFetch(`${base}/identity-changes/${change.change_id}/undo`, {
        method: 'POST', body: JSON.stringify({ expected_revision: latestIdentityRevision, reason: reason.trim() }),
      })
      setNotice('已撤销该身份变更，新的身份版本已写入审计记录。')
      await loadWorkspace()
      if (activeRun) {
        const remaining = await apiFetch<{ items: ReviewPair[] }>(`${base}/reviews?run_id=${encodeURIComponent(activeRun.run_id)}&limit=100&offset=0`)
        setReviewPairs(remaining.items)
      }
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const downloadSnapshot = async (kind: 'entities' | 'mappings' | 'decisions' | 'delta') => {
    if (!currentSnapshotId) return
    const snapshot = snapshots.find((item) => item.snapshot_id === currentSnapshotId)
    const filename = `${projectId}-snapshot-${snapshot?.version ?? currentSnapshotVersion}-${kind}.json`
    try {
      await downloadApi(`${base}/snapshots/${currentSnapshotId}/export?kind=${kind}`, filename)
    } catch (caught) {
      setError(errorText(caught))
    }
  }

  const tabs: Array<{ key: Tab; title: string; detail: string }> = [
    { key: 'configure', title: '方案与输入', detail: '映射 / 来源批次' },
    { key: 'run', title: '运行记录', detail: `${runs.length} 次` },
    { key: 'review', title: '人工复核', detail: `${reviewPairs.length} 待处理` },
    { key: 'release', title: '快照与导出', detail: currentSnapshotVersion ? `v${currentSnapshotVersion}` : '尚未发布' },
  ]

  return (
    <div className="unification-workspace space-y-5">
      <header className="overflow-hidden rounded-2xl bg-slate-950 text-white shadow-sm">
        <div className="grid gap-6 p-6 md:grid-cols-[1.25fr_0.75fr] md:p-8">
          <div className="flex flex-col justify-between gap-6">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-bold tracking-[0.14em] text-cyan-300">
                <Sparkles className="h-4 w-4" /> PROJECT IDENTITY GRAPH
              </div>
              <h1 className="text-2xl font-extrabold tracking-tight md:text-3xl">实体统一工作台</h1>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-300">
                用可版本化的方案连接不同来源；每条合并建议都保留证据，发布内容固定在一个快照版本中。
              </p>
            </div>
            <div className="flex flex-wrap gap-2 text-xs">
              <span className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-slate-300">项目 <b className="ml-1 font-mono text-white">{projectId.slice(0, 8)}</b></span>
              <span className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-slate-300">身份版本 <b className="ml-1 text-white">r{latestIdentityRevision}</b></span>
              <span className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-slate-300">当前快照 <b className="ml-1 text-cyan-300">{currentSnapshotVersion ? `v${currentSnapshotVersion}` : '未发布'}</b></span>
            </div>
          </div>
          <div className="flex min-h-36 items-center justify-center rounded-xl border border-white/10 bg-slate-900/80 p-4" aria-label="来源记录汇入统一实体示意图">
            <div className="grid w-full grid-cols-[1fr_42px_1fr] items-center gap-2">
              <div className="space-y-3">
                <div className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-[11px] text-slate-200"><span className="mr-2 text-cyan-300">ERP</span> S-1001</div>
                <div className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-[11px] text-slate-200"><span className="mr-2 text-violet-300">CRM</span> P-77</div>
              </div>
              <svg viewBox="0 0 42 72" className="h-[72px] w-[42px]" aria-hidden="true">
                <path d="M1 12 C22 12 18 36 40 36" fill="none" stroke="#22d3ee" strokeWidth="2" />
                <path d="M1 60 C22 60 18 36 40 36" fill="none" stroke="#a78bfa" strokeWidth="2" />
                <circle cx="40" cy="36" r="3" fill="#5eead4" />
              </svg>
              <div className="rounded-xl border border-cyan-400/40 bg-cyan-400/10 px-3 py-4">
                <div className="mb-1 text-[10px] font-bold uppercase tracking-widest text-cyan-300">Canonical</div>
                <div className="text-sm font-bold text-white">统一实体</div>
                <div className="mt-1 font-mono text-[10px] text-slate-400">source → entity</div>
              </div>
            </div>
          </div>
        </div>
      </header>

      {(error || notice) && (
        <div className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-sm ${error ? 'border-rose-200 bg-rose-50 text-rose-800' : 'border-emerald-200 bg-emerald-50 text-emerald-800'}`} role={error ? 'alert' : 'status'}>
          {error ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" /> : <Check className="mt-0.5 h-4 w-4 shrink-0" />}
          <span>{error || notice}</span>
        </div>
      )}

      <nav className="grid grid-cols-2 gap-2 rounded-2xl border border-slate-200 bg-white p-2 md:grid-cols-4" aria-label="统一流程">
        {tabs.map((item, index) => (
          <button key={item.key} onClick={() => setTab(item.key)} className={`rounded-xl px-4 py-3 text-left transition ${tab === item.key ? 'bg-slate-950 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-50'}`}>
            <span className="flex items-center gap-2 text-sm font-bold"><span className={`font-mono text-[10px] ${tab === item.key ? 'text-cyan-300' : 'text-slate-400'}`}>{String(index + 1).padStart(2, '0')}</span>{item.title}</span>
            <span className={`mt-1 block pl-7 text-[11px] ${tab === item.key ? 'text-slate-300' : 'text-slate-400'}`}>{item.detail}</span>
          </button>
        ))}
      </nav>

      {tab === 'configure' && (
        <div className="grid items-start gap-5 xl:grid-cols-[0.9fr_1.1fr]">
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div><div className="text-[11px] font-bold uppercase tracking-widest text-cyan-700">Match scheme</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">定义身份规则</h2><p className="mt-1 text-xs leading-5 text-slate-500">同一方案可以复用在供应商、客户、商品或自定义实体上。</p></div>
              <ShieldCheck className="h-5 w-5 text-cyan-700" />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="text-xs font-semibold text-slate-700">方案名称
                <input value={schemeKey} onChange={(event) => { setSchemeKey(event.target.value); setSchemeSaved(false) }} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm" />
              </label>
              <label className="text-xs font-semibold text-slate-700">实体类型
                <select value={scheme.entity_type} onChange={(event) => updateScheme({ entity_type: event.target.value })} className="mt-1.5 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm">
                  <option value="supplier">供应商</option><option value="customer">客户</option><option value="product">商品</option><option value="custom">自定义</option>
                </select>
              </label>
              <label className="text-xs font-semibold text-slate-700 sm:col-span-2">身份粒度
                <input value={scheme.identity_granularity} onChange={(event) => updateScheme({ identity_granularity: event.target.value })} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm" placeholder="legal_entity / person / sellable_spec" />
              </label>
              <label className="text-xs font-semibold text-slate-700 sm:col-span-2">标准字段 → 来源字段（JSON）
                <textarea value={fieldMapDraft} onChange={(event) => {
                  const draft = event.target.value
                  setFieldMapDraft(draft)
                  setSchemeSaved(false)
                  try {
                    const parsed: unknown = JSON.parse(draft)
                    if (isRecord(parsed) && Object.values(parsed).every((value) => typeof value === 'string')) {
                      const fieldMap: Record<string, string> = {}
                      for (const [canonical, source] of Object.entries(parsed)) {
                        if (typeof source === 'string') fieldMap[canonical] = source
                      }
                      setScheme((current) => ({ ...current, field_map: fieldMap }))
                    }
                  } catch { /* keep incomplete JSON in the editor until it is valid */ }
                }} className="mt-1.5 min-h-28 w-full rounded-lg border border-slate-300 px-3 py-2.5 font-mono text-xs leading-5" />
              </label>
              <label className="text-xs font-semibold text-slate-700 sm:col-span-2">强标识字段（逗号分隔）
                <input value={scheme.strong_identifiers.join(', ')} onChange={(event) => updateScheme({ strong_identifiers: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm" />
              </label>
              <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900 sm:col-span-2">
                强标识缺失或冲突会进入保守判断；名称相似和模型建议不会绕过硬冲突或人工“保持分离”裁决。
              </div>
              <details className="rounded-xl border border-slate-200 p-3 sm:col-span-2">
                <summary className="cursor-pointer text-xs font-bold text-slate-800">高级匹配设置：候选规则与冲突约束</summary>
                <div className="mt-4 grid gap-4 md:grid-cols-2">
                  <label className="text-xs font-semibold text-slate-700">候选召回规则（JSON 数组）
                    <textarea aria-label="候选召回规则" value={candidateRulesDraft} onChange={(event) => {
                      const draft = event.target.value
                      setCandidateRulesDraft(draft)
                      setSchemeSaved(false)
                      try {
                        const parsed: unknown = JSON.parse(draft)
                        if (Array.isArray(parsed) && parsed.every(isRecord)) setScheme((current) => ({ ...current, candidate_rules: parsed }))
                      } catch { /* keep incomplete JSON in the editor until it is valid */ }
                    }} className="mt-1.5 min-h-32 w-full rounded-lg border border-slate-300 px-3 py-2.5 font-mono text-[11px] leading-5" spellCheck={false} />
                  </label>
                  <label className="text-xs font-semibold text-slate-700">冲突约束（JSON 数组）
                    <textarea aria-label="冲突约束" value={conflictConstraintsDraft} onChange={(event) => {
                      const draft = event.target.value
                      setConflictConstraintsDraft(draft)
                      setSchemeSaved(false)
                      try {
                        const parsed: unknown = JSON.parse(draft)
                        if (Array.isArray(parsed) && parsed.every(isConflictConstraint)) {
                          setScheme((current) => ({ ...current, conflict_constraints: parsed }))
                        }
                      } catch { /* keep incomplete JSON in the editor until it is valid */ }
                    }} className="mt-1.5 min-h-32 w-full rounded-lg border border-slate-300 px-3 py-2.5 font-mono text-[11px] leading-5" spellCheck={false} />
                  </label>
                </div>
              </details>
            </div>
            <button onClick={() => void saveScheme()} disabled={!canEdit || busy || !schemeKey.trim()} className="mt-5 inline-flex items-center gap-2 rounded-lg bg-slate-950 px-4 py-2.5 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50">
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} 保存方案
            </button>
            {schemeSaved && <span className="ml-3 text-xs font-semibold text-emerald-700">方案 v{scheme.revision} 已保存</span>}
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div><div className="text-[11px] font-bold uppercase tracking-widest text-violet-700">Source batch</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">导入来源记录</h2><p className="mt-1 text-xs leading-5 text-slate-500">通过批量 API 录入稳定来源键与来源属性；示例仅用于演示结构。</p></div>
              <GitBranch className="h-5 w-5 text-violet-700" />
            </div>
            <div className="mb-3 grid gap-3 sm:grid-cols-[1fr_1fr_1.2fr]">
              <label className="text-xs font-semibold text-slate-700">输入版本
                <input value={inputVersion} onChange={(event) => { setInputVersion(event.target.value); setBatchImported(false) }} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 font-mono text-xs" />
              </label>
              <label className="text-xs font-semibold text-slate-700">导入模式
                <select value={ingestMode} onChange={(event) => { setIngestMode(event.target.value === 'exploration' ? 'exploration' : 'sync'); setBatchImported(false) }} className="mt-1.5 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-xs">
                  <option value="sync">持续同步（需要来源 ID）</option><option value="exploration">探索（可自动生成临时 ID）</option>
                </select>
              </label>
              <div className="rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-5 text-slate-600">来源键：<code className="text-slate-900">项目 + 系统 + 数据集 + 来源记录 ID</code></div>
            </div>
            <label className="text-xs font-semibold text-slate-700">批次 JSON
              <textarea aria-label="批次 JSON" value={recordsText} onChange={(event) => { setRecordsText(event.target.value); setBatchImported(false) }} className="mt-1.5 min-h-[18rem] w-full rounded-lg border border-slate-300 px-3 py-3 font-mono text-[11px] leading-5 text-slate-800" spellCheck={false} />
            </label>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button onClick={() => void importBatch()} disabled={!canEdit || busy || !schemeSaved || !inputVersion.trim()} className="inline-flex items-center gap-2 rounded-lg border border-violet-300 bg-violet-50 px-4 py-2.5 text-sm font-bold text-violet-900 hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50">
                {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <ArrowDownToLine className="h-4 w-4" />} 导入批次
              </button>
              {batchImported && <span className="text-xs font-semibold text-emerald-700">已接收，可提交运行</span>}
              {!schemeSaved && <span className="text-[11px] text-slate-400">先保存方案，再导入批次</span>}
            </div>
          </section>
          <div className="flex justify-end xl:col-span-2">
            <button onClick={() => setTab('run')} className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-bold text-slate-600 hover:bg-white">继续到运行记录 <ArrowRight className="h-4 w-4" /></button>
          </div>
        </div>
      )}

      {tab === 'run' && (
        <div className="grid items-start gap-5 xl:grid-cols-[0.7fr_1.3fr]">
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
            <div className="text-[11px] font-bold uppercase tracking-widest text-cyan-700">Execution</div>
            <h2 className="mt-1 text-lg font-extrabold text-slate-950">运行当前输入</h2>
            <p className="mt-2 text-xs leading-5 text-slate-500">任务绑定输入版本与方案修订。相同幂等键只会创建一个逻辑运行。</p>
            <div className="mt-4 space-y-2 rounded-xl bg-slate-50 p-3 text-xs text-slate-600">
              <div className="flex justify-between gap-2"><span>方案</span><b className="text-slate-900">{schemeKey} · v{scheme.revision}</b></div>
              <div className="flex justify-between gap-2"><span>输入</span><b className="font-mono text-slate-900">{inputVersion}</b></div>
              <div className="flex justify-between gap-2"><span>批次</span><b className={batchImported ? 'text-emerald-700' : 'text-amber-700'}>{batchImported ? '已导入' : '当前浏览器会话未确认'}</b></div>
            </div>
            <button onClick={() => void runMatching()} disabled={!canEdit || busy || !schemeSaved || !batchImported} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-cyan-700 px-4 py-2.5 text-sm font-bold text-white hover:bg-cyan-800 disabled:cursor-not-allowed disabled:opacity-50">
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />} 提交运行
            </button>
            {!schemeSaved && <p className="mt-2 text-[11px] text-slate-400">请先保存方案。</p>}
            {schemeSaved && !batchImported && <p className="mt-2 text-[11px] text-slate-400">请先导入一个输入批次。</p>}
          </section>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div><div className="text-[11px] font-bold uppercase tracking-widest text-slate-400">Run ledger</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">执行记录</h2></div>
              <button onClick={() => void loadWorkspace()} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600 hover:bg-slate-50">刷新</button>
            </div>
            {runs.length === 0 ? <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">还没有执行记录。保存方案并导入批次后即可开始。</div> : (
              <div className="space-y-2">
                {runs.map((run) => (
                  <button key={run.run_id} onClick={() => void chooseRun(run.run_id)} className={`flex w-full flex-col gap-2 rounded-xl border p-3 text-left md:flex-row md:items-center md:justify-between ${activeRun?.run_id === run.run_id ? 'border-cyan-400 bg-cyan-50/50' : 'border-slate-200 hover:bg-slate-50'}`}>
                    <div><div className="font-mono text-xs font-bold text-slate-900">{run.run_id.slice(0, 12)} · {run.input_version}</div><div className="mt-1 text-[11px] text-slate-500">方案修订 {run.scheme_revision.slice(0, 8)} · {new Date(run.created_at).toLocaleString()}</div></div>
                    <span className={`w-fit rounded-md px-2 py-1 text-[10px] font-bold ${run.status === 'SUCCEEDED' ? 'bg-emerald-100 text-emerald-800' : run.status === 'FAILED' ? 'bg-rose-100 text-rose-800' : 'bg-amber-100 text-amber-800'}`}>{run.status}</span>
                  </button>
                ))}
              </div>
            )}
            {activeRun && <div className="mt-4 border-t border-slate-100 pt-4">
              <p className="mb-2 text-xs text-slate-500">执行尝试：{activeRun.attempt_count} 次{activeRun.last_interruption && ` · 最近中断：${activeRun.last_interruption}`}</p>
              {activeRun.status === 'FAILED' && <p className="text-sm text-rose-700">{activeRun.error_message || '运行失败'}</p>}
              {activeRun.status === 'SUCCEEDED' && <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-lg bg-slate-50 p-3"><div className="text-[10px] font-bold uppercase text-slate-400">配对数</div><div className="mt-1 text-xl font-extrabold text-slate-950">{activeRun.result?.pairs?.length ?? 0}</div></div>
                <div className="rounded-lg bg-amber-50 p-3"><div className="text-[10px] font-bold uppercase text-amber-700">待复核</div><div className="mt-1 text-xl font-extrabold text-amber-950">{reviewPairs.length}</div></div>
                <div className="rounded-lg bg-cyan-50 p-3"><div className="text-[10px] font-bold uppercase text-cyan-700">实体数</div><div className="mt-1 text-xl font-extrabold text-cyan-950">{Object.keys(activeRun.result?.entities || {}).length}</div></div>
              </div>}
              {activeRun.trace_id && <div className="mt-3 flex flex-wrap items-center gap-2 font-mono text-[10px] text-slate-400"><span>trace_id: {activeRun.trace_id}</span><a href={phoenixProjectId ? `${phoenixUiUrl}/projects/${encodeURIComponent(phoenixProjectId)}/traces/${activeRun.trace_id}` : phoenixUiUrl} target="_blank" rel="noreferrer" className="rounded border border-slate-200 px-2 py-1 font-sans font-bold text-cyan-800 hover:bg-cyan-50">在 Phoenix 中打开</a></div>}
            </div>}
          </section>
          <div className="flex justify-end xl:col-span-2"><button onClick={() => setTab('review')} className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-bold text-slate-600 hover:bg-white">查看待复核项 <ArrowRight className="h-4 w-4" /></button></div>
        </div>
      )}

      {tab === 'review' && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
            <div><div className="text-[11px] font-bold uppercase tracking-widest text-amber-700">Human decisions</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">复核候选配对</h2><p className="mt-1 text-xs leading-5 text-slate-500">每项决定都带原因和身份修订号；过期修订会返回冲突，避免覆盖其他人的裁决。</p></div>
            <select aria-label="选择运行" value={activeRun?.run_id || ''} onChange={(event) => void chooseRun(event.target.value)} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs">
              <option value="">选择运行记录</option>{runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.input_version} · {run.status}</option>)}
            </select>
          </div>
          {!activeRun || activeRun.status !== 'SUCCEEDED' ? <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">选择一条已完成的运行，查看其待复核配对。</div> : reviewPairs.length === 0 ? <div className="rounded-xl border border-dashed border-emerald-200 bg-emerald-50 p-8 text-center text-sm text-emerald-800">该运行没有待复核配对。</div> : (
            <div className="space-y-3">
              {reviewPairs.map((pair) => <article key={`${pair.left_source_revision_id}:${pair.right_source_revision_id}`} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2"><span className="rounded-md bg-amber-100 px-2 py-1 text-[10px] font-bold text-amber-900">REVIEW</span><span className="font-mono text-[10px] text-slate-500">{pair.left_source_revision_id.slice(0, 10)} ↔ {pair.right_source_revision_id.slice(0, 10)}</span><span className="text-[10px] text-slate-400">{pair.candidate ? '候选已召回' : '未召回'}</span></div>
                  {canEdit && <div className="flex gap-2"><button disabled={busy} onClick={() => void decideReview(pair, 'MERGE')} className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-800 disabled:opacity-50">确认同一实体</button><button disabled={busy} onClick={() => void decideReview(pair, 'KEEP_DISTINCT')} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-50">保持分离</button></div>}
                </div>
                {pair.evidence_codes && <div className="mt-3 flex flex-wrap gap-1.5">{pair.evidence_codes.map((code) => <span key={code} className="rounded bg-slate-100 px-2 py-1 font-mono text-[10px] text-slate-600">{code}</span>)}</div>}
                {pair.evidence && <div className="mt-3 grid gap-2 md:grid-cols-2">{pair.evidence.map((evidence, index) => <pre key={index} className="overflow-x-auto rounded-lg bg-slate-950 p-3 font-mono text-[10px] leading-4 text-cyan-100">{JSON.stringify(evidence, null, 2)}</pre>)}</div>}
                {pair.recommendation?.reason && <p className="mt-3 border-l-2 border-violet-400 pl-3 text-xs text-slate-600">模型建议：{pair.recommendation.reason}（仍需人工或规则裁定）</p>}
                {pair.model_error && <p className="mt-3 text-xs text-rose-700">模型未给出建议：{pair.model_error}</p>}
              </article>)}
            </div>
          )}
          <div className="mt-8 border-t border-slate-100 pt-5">
            <div className="mb-3 flex items-center gap-2"><ScanSearch className="h-4 w-4 text-slate-500" /><h3 className="text-sm font-bold text-slate-900">身份变更与撤销</h3><span className="font-mono text-[10px] text-slate-400">r{latestIdentityRevision}</span></div>
            {identityChanges.length === 0 ? <p className="text-xs text-slate-500">暂无身份变更记录。</p> : <div className="space-y-2">
              {identityChanges.map((change) => <div key={change.change_id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2.5">
                <div><span className="mr-2 rounded bg-slate-100 px-2 py-1 font-mono text-[10px]">r{change.revision}</span><b className="text-xs text-slate-800">{change.change_type}</b><span className="ml-2 text-xs text-slate-500">{change.reason}</span><div className="mt-1 text-[10px] text-slate-400">{change.actor_id} · {new Date(change.created_at).toLocaleString()}</div></div>
                {canEdit && ['MERGE', 'DECISION', 'SPLIT'].includes(change.change_type) && !identityChanges.some((item) => item.change_type === 'UNDO' && item.payload?.undoes === change.change_id) && <button onClick={() => void undoChange(change)} disabled={busy} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1.5 text-[11px] font-bold text-slate-600 hover:bg-slate-50 disabled:opacity-50"><RotateCcw className="h-3 w-3" />撤销</button>}
              </div>)}
            </div>}
          </div>
        </section>
      )}

      {tab === 'release' && (
        <div className="space-y-5">
          <div className="grid items-start gap-5 xl:grid-cols-[0.7fr_1.3fr]">
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
              <div className="text-[11px] font-bold uppercase tracking-widest text-emerald-700">Immutable snapshot</div>
              <h2 className="mt-1 text-lg font-extrabold text-slate-950">发布当前运行</h2>
              <p className="mt-2 text-xs leading-5 text-slate-500">发布会固定输入、方案、实体成员、规范属性和冲突证据。重新运行会生成新的候选快照，不覆盖已发布内容。</p>
              <label className="mt-4 block text-xs font-semibold text-slate-700">已完成的运行
                <select value={activeRun?.run_id || ''} onChange={(event) => void chooseRun(event.target.value)} className="mt-1.5 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-xs">
                  <option value="">选择已成功运行</option>{succeededRuns.map((run) => <option key={run.run_id} value={run.run_id}>{run.input_version} · {run.run_id.slice(0, 10)}</option>)}
                </select>
              </label>
              <button onClick={() => void publish()} disabled={!canEdit || busy || activeRun?.status !== 'SUCCEEDED'} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-4 py-2.5 text-sm font-bold text-white hover:bg-emerald-800 disabled:cursor-not-allowed disabled:opacity-50">{busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} 发布快照</button>
              <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">当前发布：<b className="text-slate-950">{currentSnapshotVersion ? `快照 v${currentSnapshotVersion}` : '尚无快照'}</b></div>
            </section>
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
              <div className="mb-4 flex items-center justify-between gap-3"><div><div className="text-[11px] font-bold uppercase tracking-widest text-slate-400">Snapshot history</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">快照历史</h2></div><button onClick={() => void loadWorkspace()} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600">刷新</button></div>
              {snapshots.length === 0 ? <div className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-xs text-slate-500">发布成功的快照会显示在这里。</div> : <div className="space-y-2">
                {snapshots.map((snapshot) => <div key={snapshot.snapshot_id} className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border p-3 ${snapshot.is_current ? 'border-emerald-300 bg-emerald-50/60' : 'border-slate-200'}`}>
                  <div><div className="flex items-center gap-2"><b className="text-sm text-slate-950">快照 v{snapshot.version}</b>{snapshot.is_current && <span className="rounded bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800">当前</span>}</div><div className="mt-1 font-mono text-[10px] text-slate-400">{snapshot.snapshot_id.slice(0, 14)} · {snapshot.run_id ? `run ${snapshot.run_id.slice(0, 8)}` : snapshot.rollback_of_id ? '回滚版本' : '快照'}</div></div>
                  {canEdit && <button onClick={() => void rollback(snapshot)} disabled={busy || snapshot.is_current} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1.5 text-[11px] font-bold text-slate-600 hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"><RotateCcw className="h-3 w-3" />从此版本回滚</button>}
                </div>)}
              </div>}
            </section>
          </div>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:p-6">
            <div className="mb-4 flex flex-wrap items-end justify-between gap-3"><div><div className="text-[11px] font-bold uppercase tracking-widest text-cyan-700">Versioned entities</div><h2 className="mt-1 text-lg font-extrabold text-slate-950">发布实体预览</h2><p className="mt-1 text-xs text-slate-500">读取必须指定快照；历史版本不会随当前发布切换而改写。</p></div>
              {currentSnapshotId && <div className="flex flex-wrap gap-2">{(['entities', 'mappings', 'decisions', 'delta'] as const).map((kind) => <button key={kind} onClick={() => void downloadSnapshot(kind)} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-2 text-[11px] font-bold text-slate-700 hover:bg-slate-50"><ArrowDownToLine className="h-3 w-3" />{kind}</button>)}</div>}
            </div>
            {!snapshotEntities ? <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-xs text-slate-500">当前没有可读取的已发布快照。</div> : snapshotEntities.items.length === 0 ? <p className="text-xs text-slate-500">该快照未包含实体。</p> : <div className="overflow-x-auto rounded-xl border border-slate-200"><table className="w-full min-w-[760px] text-left text-xs"><thead className="bg-slate-50 text-[10px] uppercase tracking-wide text-slate-500"><tr><th className="p-3">实体 ID / 类型</th><th className="p-3">规范属性</th><th className="p-3">来源成员</th><th className="p-3">冲突</th></tr></thead><tbody>{snapshotEntities.items.map((entity) => <tr key={entity.entity_id} className="border-t border-slate-100 align-top"><td className="p-3"><div className="font-mono text-[10px] font-bold text-slate-900">{entity.entity_id}</div><div className="mt-1 text-[10px] text-slate-500">{entity.entity_type} · {entity.status}</div></td><td className="p-3"><pre className="max-w-sm overflow-x-auto font-mono text-[10px] leading-4 text-slate-700">{JSON.stringify(entity.attributes, null, 2)}</pre>{canEdit && snapshotEntities.fields.length > 0 && <div className="mt-2 flex gap-2"><select aria-label={`选择实体 ${entity.entity_id} 的字段`} value={overrideFields[entity.entity_id] || snapshotEntities.fields[0]} onChange={(event) => setOverrideFields((current) => ({ ...current, [entity.entity_id]: event.target.value }))} className="rounded border border-slate-300 px-2 py-1">{snapshotEntities.fields.map((field) => <option key={field} value={field}>{field}</option>)}</select><button type="button" disabled={busy || snapshotEntities.snapshot_id !== currentSnapshotId} onClick={() => void overrideAttribute(entity.entity_id)} className="rounded border border-slate-300 px-2 py-1 text-xs font-bold text-slate-700 disabled:opacity-50">覆盖属性</button></div>}</td><td className="p-3"><div className="space-y-1">{entity.members.map((member) => <div key={member.source_revision_id} className="font-mono text-[10px] text-slate-600">{member.source_key.slice(1).join(' / ')}</div>)}</div></td><td className="p-3">{entity.conflicts.length ? <span className="rounded bg-amber-100 px-2 py-1 text-[10px] font-bold text-amber-900">{entity.conflicts.length} 项</span> : <span className="text-[10px] text-slate-400">无</span>}</td></tr>)}</tbody></table></div>}
            {currentSnapshotId && <p className="mt-3 font-mono text-[10px] text-slate-400">snapshot_id: {currentSnapshotId}</p>}
          </section>
        </div>
      )}
    </div>
  )
}
