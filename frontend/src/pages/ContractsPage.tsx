import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, Check, CircleAlert, FileText, History, LoaderCircle, Plus, RotateCcw, Send } from 'lucide-react'
import { apiFetch, clearAuth, DataSourceItem, downloadApi, getToken } from '../lib/api'
import ResultViewer, { ResultViewerSource } from '../components/ResultViewer'

type Tab = 'sources' | 'versions' | 'release'
type ContractVersionStatus = 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'

interface ContractSourceItem {
  id: string
  project_id: string
  data_source_id: string
  filename: string
  format: string
  sha256: string
  size_bytes: number
  created_by: string
  created_at: string
}

interface ContractVersionItem {
  id: string
  project_id: string
  version: number
  status: ContractVersionStatus
  source_id: string
  source_sha256: string
  entity_snapshot_id: string
  snapshot_version: number
  counts: { classes?: number; attributes?: number; enums?: number; relations?: number }
  compiler_versions: Record<string, string>
  created_by: string
  created_at: string
}

interface ContractReleaseState {
  project_id: string
  current_version: number
  current_version_id: string | null
}

interface ContractAuditItem {
  id: string
  project_id: string
  version_id: string
  action: 'PUBLISH' | 'ROLLBACK'
  actor_id: string
  reason: string
  from_version: number | null
  to_version: number
  trace_id: string | null
  created_at: string
}

interface SnapshotOption {
  snapshot_id: string
  version: number
  is_current: boolean
}

interface ValidationIssue {
  path: string
  code: string
  message: string
}

const emptyRelease: ContractReleaseState = { project_id: '', current_version: 0, current_version_id: null }

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试。'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isValidationIssue(value: unknown): value is ValidationIssue {
  return isRecord(value) && typeof value.path === 'string' && typeof value.code === 'string' && typeof value.message === 'string'
}

class ValidationError extends Error {
  issues: ValidationIssue[]

  constructor(issues: ValidationIssue[]) {
    super(`校验未通过：${issues.length} 个问题`)
    this.issues = issues
  }
}

/** POST JSON and keep structured `detail` (a validation issue array) instead of collapsing it to a string. */
async function postJson<T = any>(path: string, body: unknown): Promise<T> {
  const headers = new Headers()
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) })
  if (response.status === 401) {
    clearAuth()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  const data: unknown = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = isRecord(data) ? data.detail : undefined
    if (Array.isArray(detail) && detail.every(isValidationIssue)) {
      throw new ValidationError(detail)
    }
    throw new Error(typeof detail === 'string' && detail ? detail : `请求失败 (${response.status})`)
  }
  return data as T
}

function formatCounts(counts: ContractVersionItem['counts']): string {
  return `类 ${counts.classes ?? 0} · 属性 ${counts.attributes ?? 0} · 枚举 ${counts.enums ?? 0} · 关系 ${counts.relations ?? 0}`
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString()
}

const statusBadge: Record<ContractVersionStatus, string> = {
  DRAFT: 'bg-amber-100 text-amber-800',
  PUBLISHED: 'bg-emerald-100 text-emerald-800',
  ARCHIVED: 'bg-neutral-100 text-neutral-500',
}

export default function ContractsPage({ projectId, canEdit, initialVersionId }: { projectId: string; canEdit: boolean; initialVersionId?: string }) {
  const [tab, setTab] = useState<Tab>('sources')
  const [loaded, setLoaded] = useState(false)
  const [sources, setSources] = useState<ContractSourceItem[]>([])
  const [fileDataSources, setFileDataSources] = useState<DataSourceItem[]>([])
  const [snapshots, setSnapshots] = useState<SnapshotOption[]>([])
  const [versions, setVersions] = useState<ContractVersionItem[]>([])
  const [release, setRelease] = useState<ContractReleaseState>({ ...emptyRelease, project_id: projectId })
  const [audit, setAudit] = useState<ContractAuditItem[]>([])
  const [selectedDataSourceId, setSelectedDataSourceId] = useState('')
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [selectedSnapshotId, setSelectedSnapshotId] = useState('')
  const [allowedNewTypes, setAllowedNewTypes] = useState('')
  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [publishVersionId, setPublishVersionId] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [viewerSource, setViewerSource] = useState<ResultViewerSource | null>(null)
  const [jsonldVersion, setJsonldVersion] = useState<ContractVersionItem | null>(null)
  const [jsonldSourceId, setJsonldSourceId] = useState('')
  const [jsonldModel, setJsonldModel] = useState<any>(null)
  const [jsonldClass, setJsonldClass] = useState('')
  const [jsonldIdSlot, setJsonldIdSlot] = useState('')
  const [jsonldColumns, setJsonldColumns] = useState<Record<string, string>>({})
  const [jsonldError, setJsonldError] = useState('')
  const [jsonldBusy, setJsonldBusy] = useState(false)

  const base = `/api/v1/projects/${projectId}`

  const loadWorkspace = async () => {
    setError('')
    try {
      const [sourceList, versionPage, releaseState, auditPage, dsList, snapshotPage] = await Promise.all([
        apiFetch<ContractSourceItem[]>(`${base}/contracts/sources`),
        apiFetch<{ items: ContractVersionItem[] }>(`${base}/contracts/versions?limit=50`),
        apiFetch<ContractReleaseState>(`${base}/contracts/release`),
        apiFetch<{ items: ContractAuditItem[] }>(`${base}/contracts/audit?limit=50`),
        apiFetch<DataSourceItem[]>(`${base}/datasources`),
        apiFetch<{ items: SnapshotOption[] }>(`${base}/unification/snapshots?limit=50`),
      ])
      setSources(sourceList)
      setVersions(versionPage.items)
      setRelease(releaseState)
      setAudit(auditPage.items)
      setFileDataSources(dsList.filter((item) => item.source_type === 'file'))
      setSnapshots(snapshotPage.items)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setLoaded(true)
    }
  }

  useEffect(() => {
    setSources([])
    setVersions([])
    setAudit([])
    setRelease({ ...emptyRelease, project_id: projectId })
    setIssues([])
    setSelectedDataSourceId('')
    setSelectedSourceId('')
    setSelectedSnapshotId('')
    setPublishVersionId('')
    setJsonldVersion(null)
    setJsonldSourceId('')
    setJsonldModel(null)
    setJsonldError('')
    setLoaded(false)
    void loadWorkspace()
  }, [projectId])

  useEffect(() => {
    if (!selectedDataSourceId && fileDataSources.length > 0) setSelectedDataSourceId(fileDataSources[0].id)
  }, [fileDataSources, selectedDataSourceId])

  useEffect(() => {
    if (!selectedSourceId && sources.length > 0) setSelectedSourceId(sources[0].id)
  }, [sources, selectedSourceId])

  useEffect(() => {
    if (!selectedSnapshotId && snapshots.length > 0) setSelectedSnapshotId(snapshots[0].snapshot_id)
  }, [snapshots, selectedSnapshotId])

  useEffect(() => {
    if (!initialVersionId) return
    const found = versions.find((version) => version.id === initialVersionId)
    if (found) {
      setTab('versions')
      return
    }
    apiFetch<ContractVersionItem>(`${base}/contracts/versions/${initialVersionId}`)
      .then((version) => {
        setVersions((items) => [version, ...items.filter((item) => item.id !== version.id)])
        setTab('versions')
      })
      .catch(() => setError('任务不存在'))
  }, [initialVersionId, versions])

  const addSource = async () => {
    if (!selectedDataSourceId) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const created = await postJson<ContractSourceItem>(`${base}/contracts/sources`, { data_source_id: selectedDataSourceId })
      setNotice(`契约源已登记：${created.filename}（${created.format}）`)
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const createVersion = async () => {
    if (!selectedSourceId || !selectedSnapshotId) return
    setBusy(true)
    setError('')
    setNotice('')
    setIssues([])
    try {
      const created = await postJson<ContractVersionItem>(`${base}/contracts/versions`, {
        source_id: selectedSourceId,
        snapshot_id: selectedSnapshotId,
        allowed_new_entity_types: allowedNewTypes.split(',').map((value) => value.trim()).filter(Boolean),
      })
      setNotice(`模型版本 v${created.version} 已创建（${formatCounts(created.counts)}），状态为草稿。`)
      await loadWorkspace()
      setTab('versions')
    } catch (caught) {
      if (caught instanceof ValidationError) {
        setIssues(caught.issues)
      } else {
        setError(errorText(caught))
      }
    } finally {
      setBusy(false)
    }
  }

  const releaseAction = async (action: 'publish' | 'rollback') => {
    if (!publishVersionId || !reason.trim()) return
    const target = versions.find((item) => item.id === publishVersionId)
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await apiFetch(`${base}/contracts/versions/${publishVersionId}/${action}`, {
        method: 'POST',
        body: JSON.stringify({
          expected_current_version: release.current_version || 0,
          reason: reason.trim(),
        }),
      })
      setNotice(action === 'publish' ? `已发布版本 v${target?.version ?? '?'}。` : `已回滚到版本 v${target?.version ?? '?'}。`)
      setReason('')
      await loadWorkspace()
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  const exportVersion = async (version: ContractVersionItem, kind: 'model' | 'json_schema' | 'sql_ddl' | 'er_diagram') => {
    const extension = kind === 'sql_ddl' || kind === 'er_diagram' ? 'txt' : 'json'
    try {
      await downloadApi(`${base}/contracts/versions/${version.id}/export?kind=${kind}`, `contract-v${version.version}-${kind}.${extension}`)
    } catch (caught) {
      setError(errorText(caught))
    }
  }

  const applyJsonldMapping = (model: any, mapping: any) => {
    const classModel = model?.classes?.[mapping?.class_name]
    const columns: Record<string, string> = {}
    Object.keys(classModel?.slots || {}).forEach((slot) => {
      columns[slot] = mapping?.columns?.[slot] || ''
    })
    setJsonldClass(mapping?.class_name || '')
    setJsonldIdSlot(mapping?.id_slot || classModel?.id_slot || '')
    setJsonldColumns(columns)
  }

  const loadJsonldPreview = async (version: ContractVersionItem, sourceId: string) => {
    setJsonldBusy(true)
    setJsonldError('')
    try {
      const preview = await apiFetch<any>(`${base}/contracts/versions/${version.id}/jsonld/preview?data_source_id=${encodeURIComponent(sourceId)}`)
      setJsonldModel(preview.model)
      applyJsonldMapping(preview.model, preview.inferred_mapping)
    } catch (caught) {
      setJsonldError(errorText(caught))
      setJsonldModel(null)
    } finally {
      setJsonldBusy(false)
    }
  }

  const openJsonld = (version: ContractVersionItem) => {
    setJsonldVersion(version)
    setJsonldError('')
    setJsonldModel(null)
    const sourceId = jsonldSourceId || fileDataSources[0]?.id || ''
    setJsonldSourceId(sourceId)
    if (sourceId) void loadJsonldPreview(version, sourceId)
    else setJsonldError('先上传一份 CSV 数据源，再导出 JSON-LD 实例。')
  }

  const changeJsonldSource = (sourceId: string) => {
    setJsonldSourceId(sourceId)
    if (jsonldVersion) void loadJsonldPreview(jsonldVersion, sourceId)
  }

  const changeJsonldClass = (className: string) => {
    setJsonldClass(className)
    const classModel = jsonldModel?.classes?.[className]
    setJsonldIdSlot(classModel?.id_slot || '')
    const columns: Record<string, string> = {}
    Object.keys(classModel?.slots || {}).forEach((slot) => { columns[slot] = '' })
    setJsonldColumns(columns)
  }

  const generateJsonld = async () => {
    if (!jsonldVersion || !jsonldSourceId || !jsonldClass || !jsonldIdSlot) return
    setJsonldBusy(true)
    setJsonldError('')
    try {
      const payload = await apiFetch<any>(`${base}/contracts/versions/${jsonldVersion.id}/jsonld`, {
        method: 'POST',
        body: JSON.stringify({
          data_source_id: jsonldSourceId,
          class_name: jsonldClass,
          id_slot: jsonldIdSlot,
          columns: Object.fromEntries(Object.entries(jsonldColumns).filter(([, column]) => column)),
        }),
      })
      setViewerSource({ kind: 'jsonld', data: payload, filename: `contract-v${jsonldVersion.version}.jsonld` })
    } catch (caught) {
      setJsonldError(errorText(caught))
    } finally {
      setJsonldBusy(false)
    }
  }

  const selectedJsonldSource = fileDataSources.find((source) => source.id === jsonldSourceId)
  const jsonldColumnOptions: string[] = selectedJsonldSource?.profile?.tables?.[0]?.columns || []

  const tabs: Array<{ key: Tab; title: string; detail: string }> = [
    { key: 'sources', title: '契约源与模型版本', detail: `${sources.length} 个契约源` },
    { key: 'versions', title: '版本历史', detail: `${versions.length} 个版本` },
    { key: 'release', title: '发布与审计', detail: release.current_version ? `v${release.current_version}` : '尚未发布' },
  ]

  return (
    <div className="contracts-workspace space-y-5">
      <header className="rounded-xl border border-neutral-200 bg-white p-6">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <h1 className="text-xl font-semibold text-neutral-900">契约统一工作台</h1>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-neutral-500">
              选择契约文件与实体快照，生成不可变、可校验、可追溯的模型版本。发布指针独立切换，历史版本保持不变。
            </p>
          </div>
          <div className="flex flex-wrap gap-2 text-xs text-neutral-600">
            <span className="rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5">项目 <b className="ml-1 font-mono text-neutral-900">{projectId.slice(0, 8)}</b></span>
            <span className="rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5">契约源 <b className="ml-1 text-neutral-900">{sources.length}</b></span>
            <span className="rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5">模型版本 <b className="ml-1 text-neutral-900">{versions.length}</b></span>
            <span className="rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5">当前发布 <b className="ml-1 text-neutral-900">{release.current_version ? `v${release.current_version}` : '未发布'}</b></span>
          </div>
        </div>
      </header>

      {(error || notice) && (
        <div className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-sm ${error ? 'border-rose-200 bg-rose-50 text-rose-800' : 'border-emerald-200 bg-emerald-50 text-emerald-800'}`} role={error ? 'alert' : 'status'}>
          {error ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" /> : <Check className="mt-0.5 h-4 w-4 shrink-0" />}
          <span>{error || notice}</span>
        </div>
      )}

      <nav className="grid grid-cols-1 gap-2 rounded-xl border border-neutral-200 bg-white p-2 md:grid-cols-3" aria-label="契约流程">
        {tabs.map((item, index) => (
          <button key={item.key} onClick={() => setTab(item.key)} className={`rounded-xl px-4 py-3 text-left transition ${tab === item.key ? 'bg-neutral-950 text-white ' : 'text-neutral-600 hover:bg-neutral-50'}`}>
            <span className="flex items-center gap-2 text-sm font-medium"><span className={`font-mono text-[10px] text-neutral-400`}>{String(index + 1).padStart(2, '0')}</span>{item.title}</span>
            <span className={`mt-1 block pl-7 text-[11px] ${tab === item.key ? 'text-neutral-300' : 'text-neutral-400'}`}>{item.detail}</span>
          </button>
        ))}
      </nav>

      {!loaded ? (
        <div className="flex items-center justify-center rounded-xl border border-neutral-200 bg-white p-10 text-sm text-neutral-500"><LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> 正在加载契约工作台数据...</div>
      ) : tab === 'sources' && (
        <div className="grid items-start gap-5 xl:grid-cols-2">
          <section className="rounded-xl border border-neutral-200 bg-white p-5  md:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div><div className="text-[11px] text-neutral-500">契约源</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">登记契约源</h2><p className="mt-1 text-xs leading-5 text-neutral-500">从项目中的文件数据源选择契约文件（YAML / TSV / CSV），后端按内容哈希去重。</p></div>
              <Plus className="h-5 w-5 text-neutral-400" />
            </div>
            <label className="block text-xs font-semibold text-neutral-700">文件数据源
              <select aria-label="文件数据源" value={selectedDataSourceId} onChange={(event) => setSelectedDataSourceId(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2.5 text-sm">
                <option value="">选择文件数据源</option>
                {fileDataSources.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id.slice(0, 8)}</option>)}
              </select>
            </label>
            {fileDataSources.length === 0 && <p className="mt-2 text-[11px] text-neutral-400">本项目还没有文件数据源，请先在数据源中心上传契约文件。</p>}
            <button onClick={() => void addSource()} disabled={!canEdit || busy || !selectedDataSourceId} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50">
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} 添加契约源
            </button>
          </section>

          <section className="rounded-xl border border-neutral-200 bg-white p-5  md:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div><div className="text-[11px] text-neutral-500">编译与校验</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">创建模型版本</h2><p className="mt-1 text-xs leading-5 text-neutral-500">编译契约文件并对照实体快照校验，生成 DRAFT 版本。校验问题逐条返回。</p></div>
              <Send className="h-5 w-5 text-neutral-400" />
            </div>
            <div className="grid gap-4">
              <label className="block text-xs font-semibold text-neutral-700">契约源
                <select aria-label="契约源" value={selectedSourceId} onChange={(event) => setSelectedSourceId(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2.5 text-sm">
                  <option value="">选择契约源</option>
                  {sources.map((item) => <option key={item.id} value={item.id}>{item.filename} · {item.sha256.slice(0, 8)}</option>)}
                </select>
              </label>
              <label className="block text-xs font-semibold text-neutral-700">实体快照
                <select aria-label="实体快照" value={selectedSnapshotId} onChange={(event) => setSelectedSnapshotId(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2.5 text-sm">
                  <option value="">选择已发布快照</option>
                  {snapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>快照 v{item.version}{item.is_current ? ' · 当前' : ''}</option>)}
                </select>
              </label>
              <label className="block text-xs font-semibold text-neutral-700">允许的新实体类型（可选，逗号分隔）
                <input aria-label="允许的新实体类型" value={allowedNewTypes} onChange={(event) => setAllowedNewTypes(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 px-3 py-2.5 text-sm" placeholder="例如：Invoice, Payment" />
              </label>
            </div>
            {snapshots.length === 0 && <p className="mt-2 text-[11px] text-neutral-400">还没有已发布实体快照，请先在实体统一工作台发布快照。</p>}
            <button onClick={() => void createVersion()} disabled={!canEdit || busy || !selectedSourceId || !selectedSnapshotId} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-neutral-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50">
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />} 编译并校验
            </button>
            {issues.length > 0 && (
              <div className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3">
                <div className="text-xs font-medium text-rose-900">校验未通过，共 {issues.length} 个问题：</div>
                <ul className="mt-2 space-y-1.5">
                  {issues.map((issue, index) => (
                    <li key={index} className="text-xs leading-5 text-rose-800">
                      <span className="rounded bg-rose-100 px-1.5 py-0.5 font-mono text-[10px] font-medium">{issue.code}</span>
                      {issue.path && <span className="ml-2 font-mono text-[10px] text-rose-600">{issue.path}</span>}
                      <span className="ml-2">{issue.message}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <section className="rounded-xl border border-neutral-200 bg-white p-5  xl:col-span-2 md:p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div><div className="text-[11px] text-neutral-500">契约源</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">契约源清单</h2></div>
              <button onClick={() => void loadWorkspace()} className="rounded-lg border border-neutral-200 px-3 py-2 text-xs font-medium text-neutral-600 hover:bg-neutral-50">刷新</button>
            </div>
            {sources.length === 0 ? <div className="rounded-xl border border-dashed border-neutral-300 p-8 text-center text-sm text-neutral-500">还没有契约源。从文件数据源添加契约文件后显示在这里。</div> : (
              <div className="overflow-x-auto rounded-xl border border-neutral-200">
                <table className="w-full min-w-[760px] text-left text-xs">
                  <thead className="bg-neutral-50 text-[10px] tracking-wide text-neutral-500">
                    <tr><th className="p-3">文件</th><th className="p-3">格式</th><th className="p-3">SHA-256</th><th className="p-3">大小</th><th className="p-3">登记人</th><th className="p-3">登记时间</th></tr>
                  </thead>
                  <tbody>
                    {sources.map((item) => (
                      <tr key={item.id} className="border-t border-neutral-100 align-top">
                        <td className="p-3"><div className="font-mono text-[10px] font-medium text-neutral-900">{item.filename}</div></td>
                        <td className="p-3"><span className="rounded bg-neutral-100 px-2 py-1 font-mono text-[10px] text-neutral-600">{item.format}</span></td>
                        <td className="p-3 font-mono text-[10px] text-neutral-500">{item.sha256.slice(0, 12)}…</td>
                        <td className="p-3 text-neutral-600">{item.size_bytes.toLocaleString()} B</td>
                        <td className="p-3 text-neutral-600">{item.created_by}</td>
                        <td className="p-3 text-neutral-500">{formatTime(item.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}

      {tab === 'versions' && (
        <section className="rounded-xl border border-neutral-200 bg-white p-5  md:p-6">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div><div className="text-[11px] text-neutral-500">版本历史</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">模型版本历史</h2><p className="mt-1 text-xs leading-5 text-neutral-500">版本一经创建不可修改。修正会生成新版本，历史版本与导出保持不变。</p></div>
            <button onClick={() => void loadWorkspace()} className="rounded-lg border border-neutral-200 px-3 py-2 text-xs font-medium text-neutral-600 hover:bg-neutral-50">刷新</button>
          </div>
          {versions.length === 0 ? <div className="rounded-xl border border-dashed border-neutral-300 p-8 text-center text-sm text-neutral-500">还没有模型版本。选择契约源与实体快照后编译生成。</div> : (
            <div className="overflow-x-auto rounded-xl border border-neutral-200">
              <table className="w-full min-w-[980px] text-left text-xs">
                <thead className="bg-neutral-50 text-[10px] tracking-wide text-neutral-500">
                  <tr><th className="p-3">版本</th><th className="p-3">状态</th><th className="p-3">计数</th><th className="p-3">契约源</th><th className="p-3">实体快照</th><th className="p-3">编译器版本</th><th className="p-3">创建</th><th className="p-3">导出</th></tr>
                </thead>
                <tbody>
                  {versions.map((item) => (
                    <tr key={item.id} className="border-t border-neutral-100 align-top">
                      <td className="p-3"><div className="font-mono text-[10px] font-medium text-neutral-900">v{item.version}</div></td>
                      <td className="p-3"><span className={`rounded-md px-2 py-1 text-[10px] font-medium ${statusBadge[item.status]}`}>{item.status}</span></td>
                      <td className="p-3 text-neutral-600">{formatCounts(item.counts)}</td>
                      <td className="p-3 font-mono text-[10px] text-neutral-500">{item.source_id.slice(0, 10)}…</td>
                      <td className="p-3"><div className="font-mono text-[10px] text-neutral-500">{item.entity_snapshot_id.slice(0, 10)}…</div><div className="mt-1 text-[10px] text-neutral-400">快照 v{item.snapshot_version}</div></td>
                      <td className="p-3 max-w-[220px] font-mono text-[10px] leading-4 text-neutral-500">{Object.entries(item.compiler_versions).map(([name, version]) => `${name} ${version}`).join(' · ')}</td>
                      <td className="p-3"><div className="text-neutral-600">{item.created_by}</div><div className="mt-1 text-[10px] text-neutral-400">{formatTime(item.created_at)}</div></td>
                      <td className="p-3">
                        <div className="flex flex-wrap gap-1.5">
                          <button onClick={() => setViewerSource({ kind: 'contract', projectId, versionId: item.id, filename: `contract-v${item.version}-model.json` })} className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 px-2 py-1.5 text-[11px] font-medium text-neutral-700 hover:bg-neutral-50">
                            查看模型
                          </button>
                          <button onClick={() => openJsonld(item)} className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 px-2 py-1.5 text-[11px] font-medium text-neutral-700 hover:bg-neutral-50">
                            JSON-LD
                          </button>
                          {(['model', 'json_schema', 'sql_ddl', 'er_diagram'] as const).map((kind) => (
                            <button key={kind} onClick={() => void exportVersion(item, kind)} className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 px-2 py-1.5 text-[11px] font-medium text-neutral-700 hover:bg-neutral-50">
                              <ArrowDownToLine className="h-3 w-3" />{kind}
                            </button>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {jsonldVersion && (
            <div className="mt-5 rounded-xl border border-neutral-200 bg-neutral-50/60 p-4 md:p-5">
              <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-neutral-900">JSON-LD 实例 · v{jsonldVersion.version}</div>
                  <p className="mt-1 text-xs text-neutral-500">选择 CSV 数据源，把契约槽位映射到列，再生成 W3C JSON-LD。</p>
                </div>
                <button onClick={() => setJsonldVersion(null)} className="self-start rounded-lg border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-600">
                  关闭
                </button>
              </div>

              <div className="mt-4 grid gap-3 md:grid-cols-3">
                <label className="text-xs font-semibold text-neutral-700">数据源
                  <select aria-label="JSON-LD 数据源" value={jsonldSourceId} onChange={(event) => changeJsonldSource(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm">
                    <option value="">选择 CSV 数据源</option>
                    {fileDataSources.map((source) => <option key={source.id} value={source.id}>{source.name}{source.routing?.role === 'standard_roster' ? ' · 标准花名册' : ''}</option>)}
                  </select>
                </label>
                <label className="text-xs font-semibold text-neutral-700">类
                  <select aria-label="JSON-LD 类" value={jsonldClass} onChange={(event) => changeJsonldClass(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm" disabled={!jsonldModel}>
                    {Object.keys(jsonldModel?.classes || {}).map((name) => <option key={name} value={name}>{name}</option>)}
                  </select>
                </label>
                <label className="text-xs font-semibold text-neutral-700">ID 槽
                  <select aria-label="JSON-LD ID 槽" value={jsonldIdSlot} onChange={(event) => setJsonldIdSlot(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm" disabled={!jsonldClass}>
                    {Object.keys(jsonldModel?.classes?.[jsonldClass]?.slots || {}).map((name) => <option key={name} value={name}>{name}</option>)}
                  </select>
                </label>
              </div>

              {jsonldError && <p role="alert" className="mt-3 text-xs text-rose-700">{jsonldError}</p>}
              {jsonldBusy && <p className="mt-3 text-xs text-neutral-500">正在读取契约与数据…</p>}

              {jsonldModel && jsonldClass && (
                <div className="mt-4 overflow-hidden rounded-lg border border-neutral-200 bg-white">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-neutral-50 text-[10px] tracking-wide text-neutral-500">
                      <tr><th className="p-2.5">槽位</th><th className="p-2.5">类型</th><th className="p-2.5">必填</th><th className="p-2.5">CSV 列</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(jsonldModel.classes[jsonldClass].slots).map(([slot, info]: [string, any]) => (
                        <tr key={slot} className="border-t border-neutral-100">
                          <td className="p-2.5 font-mono text-neutral-800">{slot}{slot === jsonldIdSlot ? '（ID）' : ''}</td>
                          <td className="p-2.5 text-neutral-500">{info.range}</td>
                          <td className="p-2.5 text-neutral-500">{info.required ? '是' : '—'}</td>
                          <td className="p-2.5">
                            <select
                              aria-label={`${slot} 列`}
                              value={jsonldColumns[slot] || ''}
                              onChange={(event) => setJsonldColumns((current) => ({ ...current, [slot]: event.target.value }))}
                              className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-xs"
                            >
                              <option value="">不映射</option>
                              {jsonldColumnOptions.map((column) => <option key={column} value={column}>{column}</option>)}
                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="mt-4 flex flex-wrap items-center gap-3">
                <button
                  disabled={jsonldBusy || !jsonldClass || !jsonldIdSlot || !jsonldColumns[jsonldIdSlot]}
                  onClick={() => void generateJsonld()}
                  className="rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white disabled:opacity-40"
                >
                  生成预览
                </button>
                <span className="text-[11px] text-neutral-500">最多生成 5,000 条实例；ID 槽必须映射到一列。</span>
              </div>
            </div>
          )}
        </section>
      )}

      {tab === 'release' && (
        <div className="grid items-start gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
          <section className="rounded-xl border border-neutral-200 bg-white p-5 md:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div><div className="text-[11px] text-neutral-500">发布指针</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">发布控制</h2><p className="mt-1 text-xs leading-5 text-neutral-500">发布指针单独切换并写入审计。期望版本过期会返回 409 冲突，不会覆盖他人发布。</p></div>
              <History className="h-5 w-5 text-neutral-400" />
            </div>
            <div className="rounded-lg bg-neutral-50 p-3 text-xs text-neutral-600">当前发布：<b className="text-neutral-900">{release.current_version ? `v${release.current_version}` : '尚未发布'}</b>{release.current_version_id && <span className="ml-2 font-mono text-[10px] text-neutral-400">{release.current_version_id.slice(0, 10)}…</span>}</div>
            <label className="mt-4 block text-xs font-semibold text-neutral-700">目标版本
              <select aria-label="目标版本" value={publishVersionId} onChange={(event) => setPublishVersionId(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2.5 text-sm">
                <option value="">选择版本</option>
                {versions.map((item) => <option key={item.id} value={item.id}>v{item.version} · {item.status}</option>)}
              </select>
            </label>
            <label className="mt-3 block text-xs font-semibold text-neutral-700">发布原因（必填）
              <input aria-label="发布原因" value={reason} onChange={(event) => setReason(event.target.value)} className="mt-1.5 w-full rounded-lg border border-neutral-300 px-3 py-2.5 text-sm" placeholder="例如：新增任务实体契约，通过校验" />
            </label>
            {canEdit && (
              <div className="mt-4 flex flex-wrap gap-2">
                <button onClick={() => void releaseAction('publish')} disabled={busy || !publishVersionId || !reason.trim()} className="inline-flex items-center gap-2 rounded-lg bg-neutral-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50">
                  {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} 发布版本
                </button>
                <button onClick={() => void releaseAction('rollback')} disabled={busy || !publishVersionId || !reason.trim() || !release.current_version} className="inline-flex items-center gap-2 rounded-lg border border-neutral-300 px-4 py-2.5 text-sm font-medium text-neutral-700 hover:bg-neutral-50 disabled:cursor-not-allowed disabled:opacity-40">
                  <RotateCcw className="h-4 w-4" /> 回滚到所选版本
                </button>
              </div>
            )}
            {!canEdit && <p className="mt-4 text-[11px] text-neutral-400">查看者只读，无法发布或回滚。</p>}
          </section>
          <section className="min-w-0 rounded-xl border border-neutral-200 bg-white p-5 md:p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div><div className="text-[11px] text-neutral-500">审计</div><h2 className="mt-1 text-lg font-semibold text-neutral-900">发布审计</h2></div>
              <button onClick={() => void loadWorkspace()} className="rounded-lg border border-neutral-200 px-3 py-2 text-xs font-medium text-neutral-600 hover:bg-neutral-50">刷新</button>
            </div>
            {audit.length === 0 ? <div className="rounded-xl border border-dashed border-neutral-300 p-8 text-center text-sm text-neutral-500">还没有发布或回滚记录。</div> : (
              <div className="overflow-x-auto rounded-xl border border-neutral-200">
                <table className="w-full min-w-[720px] text-left text-xs">
                  <thead className="bg-neutral-50 text-[10px] tracking-wide text-neutral-500">
                    <tr><th className="p-3">动作</th><th className="p-3">版本切换</th><th className="p-3">操作人</th><th className="p-3">原因</th><th className="p-3">trace_id</th><th className="p-3">时间</th></tr>
                  </thead>
                  <tbody>
                    {audit.map((item) => (
                      <tr key={item.id} className="border-t border-neutral-100 align-top">
                        <td className="p-3"><span className={`rounded-md px-2 py-1 text-[10px] font-medium ${item.action === 'PUBLISH' ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-900'}`}>{item.action}</span></td>
                        <td className="p-3 font-mono text-[10px] text-neutral-600">{item.from_version ?? '∅'} → v{item.to_version}</td>
                        <td className="p-3 text-neutral-600">{item.actor_id}</td>
                        <td className="p-3 max-w-[240px] text-neutral-600">{item.reason}</td>
                        <td className="p-3 font-mono text-[10px] text-neutral-400">{item.trace_id ? item.trace_id.slice(0, 10) : '—'}</td>
                        <td className="p-3 text-neutral-500">{formatTime(item.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}
      <ResultViewer source={viewerSource} onClose={() => setViewerSource(null)} />
    </div>
  )
}
