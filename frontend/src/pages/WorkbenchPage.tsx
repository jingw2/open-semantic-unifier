import { ArtifactDownload, ArtifactPreview } from '../components/ArtifactAccess'
import ResultViewer, { ResultViewerSource } from '../components/ResultViewer'
import React, { useEffect, useState } from 'react'
import { Play, Download, CheckCircle2, AlertTriangle } from 'lucide-react'
import { ApiError, apiFetch, DataSourceItem, JobItem } from '../lib/api'

type WorkbenchStage = 'L1' | 'L2' | 'L4'

interface OpenRefinePreview {
  preview_id: string
  engine: string
  recipe: { id: string; name: string; version: string; description: string }
  diff: {
    changed_rows: number
    changed_cells: number
    before_rows: number
    after_rows: number
    columns_added: string[]
    columns_removed: string[]
    structure_changed: boolean
    examples: { row: number; column: string; before: string; after: string }[]
  }
  artifact_id: string
}

function normalizeStage(value: unknown): WorkbenchStage {
  return value === 'L2' || value === 'L4' ? value : 'L1'
}

interface Props {
  projectId: string
  activeDsIds: string[]
  activeProfile: any
  activeRouting: any
  initialJobId?: string
  initialStage?: WorkbenchStage
  onGoDataSources: () => void
  onGoContracts: () => void
}

export default function WorkbenchPage({ projectId, activeDsIds, activeProfile, activeRouting, initialJobId, initialStage, onGoDataSources, onGoContracts }: Props) {
  const [stage, setStage] = useState<WorkbenchStage>(initialStage || normalizeStage(activeRouting?.recommended_stage))
  const [providers, setProviders] = useState<any[]>([])
  const [providerId, setProviderId] = useState('')
  const [metricName, setMetricName] = useState('net_gmv')
  const [enableHybrid, setEnableHybrid] = useState(true)
  const [localDsIds, setLocalDsIds] = useState<string[]>(activeDsIds)
  const [localProfile, setLocalProfile] = useState<any>(activeProfile)
  const [effectiveRouting, setEffectiveRouting] = useState<any>(activeRouting)
  const [loadError, setLoadError] = useState('')
  const [viewerSource, setViewerSource] = useState<ResultViewerSource | null>(null)
  const [preview, setPreview] = useState<OpenRefinePreview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [previewFallback, setPreviewFallback] = useState(false)
  const [adoptedName, setAdoptedName] = useState('')
  const [recipes, setRecipes] = useState<any[]>([])
  const [recipeId, setRecipeId] = useState('')
  const [fieldMap, setFieldMap] = useState<Record<string, string>>({})
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])

  const [job, setJob] = useState<JobItem | null>(null)
  const [running, setRunning] = useState(false)
  const dsIds = localDsIds.length ? localDsIds : activeDsIds
  const profile = localProfile || activeProfile
  const routing = effectiveRouting || activeRouting
  const needsDataSource = stage === 'L1' && dsIds.length === 0
  const isOpenRefine = stage === 'L1' && routing?.method?.key === 'openrefine_w3c'
  const currentRecipe = recipes.find((recipe) => recipe.id === recipeId) || recipes[0]

  const applyRecipe = (recipe: any) => {
    if (!recipe) return
    setRecipeId(recipe.id)
    if (recipe.mode === 'per_column') {
      setSelectedColumns(recipe.default_columns || [])
      setFieldMap({})
    } else {
      const map: Record<string, string> = {}
      ;(recipe.fields || []).forEach((field: any) => {
        if (field.detected) map[field.key] = field.detected
      })
      setFieldMap(map)
      setSelectedColumns([])
    }
  }

  useEffect(() => {
    setLocalDsIds(activeDsIds)
  }, [activeDsIds])

  useEffect(() => {
    setLocalProfile(activeProfile)
  }, [activeProfile])

  useEffect(() => {
    setEffectiveRouting(activeRouting)
  }, [activeRouting])

  useEffect(() => {
    if (initialStage) {
      setStage(initialStage)
      return
    }
    if (activeRouting?.recommended_stage) {
      setStage(normalizeStage(activeRouting.recommended_stage))
    }
  }, [activeRouting, initialStage])

  useEffect(() => {
    if (!initialJobId) return
    apiFetch<JobItem>(`/api/v1/jobs/${initialJobId}`)
      .then((loaded) => {
        setJob(loaded)
        setStage(normalizeStage(loaded.stage))
      })
      .catch((caught) => setLoadError(caught instanceof Error ? caught.message : '任务加载失败'))
  }, [initialJobId])

  useEffect(() => {
    if (routing) return
    apiFetch<DataSourceItem[]>(`/api/v1/projects/${projectId}/datasources`)
      .then((sources) => {
        const latest = sources.find((source) => source.routing && Object.keys(source.routing).length > 0)
        if (!latest) return
        setEffectiveRouting(latest.routing)
        setLocalProfile(latest.profile)
        setLocalDsIds([latest.id])
      })
      .catch(() => undefined)
  }, [projectId, routing])

  useEffect(() => {
    setPreview(null)
    setPreviewError('')
    setPreviewFallback(false)
    setAdoptedName('')
  }, [stage, dsIds.join(',')])

  useEffect(() => {
    apiFetch<{ configured: any[] }>('/api/v1/providers').then((d) => {
      setProviders(d.configured)
      const def = d.configured.find((x) => x.is_default)
      if (def) setProviderId(def.id)
    })
  }, [])

  useEffect(() => {
    if (!isOpenRefine || !dsIds.length) return
    apiFetch<{ items: any[]; selected_id: string | null }>(
      `/api/v1/projects/${projectId}/openrefine/recipes?data_source_id=${dsIds[0]}`,
    )
      .then((data) => {
        setRecipes(data.items || [])
        const chosen = (data.items || []).find((recipe) => recipe.id === data.selected_id) || (data.items || [])[0]
        applyRecipe(chosen)
      })
      .catch(() => setRecipes([]))
  }, [isOpenRefine, dsIds.join(','), projectId])

  const submitJob = async () => {
    setRunning(true)
    try {
      const sp = routing?.suggested_params || {}
      const params: Record<string, any> =
        stage === 'L1'
          ? { target_col: sp.name_col, amount_col: sp.amount_col }
          : stage === 'L2'
            ? { name_col: sp.name_col, tax_col: sp.tax_col, phone_col: sp.phone_col, city_col: sp.city_col, enable_hybrid_llm: enableHybrid }
            : stage === 'L4'
              ? { metric_name: metricName, group_by: 'order_channel' }
              : {}

      const created = await apiFetch<JobItem>(`/api/v1/projects/${projectId}/jobs`, {
        method: 'POST',
        body: JSON.stringify({
          stage,
          datasource_ids: dsIds,
          provider_id: providerId || null,
          params,
        }),
      })
      setJob(created)

      // 使用带登录凭据的请求获取异步任务进度
      if (created.status !== 'SUCCESS' && created.status !== 'FAILED') {
        let current = created
        while (current.status !== 'SUCCESS' && current.status !== 'FAILED') {
          await new Promise(resolve => setTimeout(resolve, 500))
          current = await apiFetch<JobItem>(`/api/v1/jobs/${created.id}`)
          setJob(current)
        }
        setRunning(false)
      } else {
        setRunning(false)
      }
    } catch (e: any) {
      alert('任务提交失败：' + e.message)
      setRunning(false)
    }
  }

  const runOpenRefinePreview = async () => {
    if (!dsIds.length) return
    setPreviewBusy(true)
    setPreviewError('')
    setPreviewFallback(false)
    setAdoptedName('')
    try {
      const loaded = await apiFetch<OpenRefinePreview>(`/api/v1/projects/${projectId}/openrefine/previews`, {
        method: 'POST',
        body: JSON.stringify({
          data_source_id: dsIds[0],
          recipe_id: recipeId || null,
          field_map: fieldMap,
          columns: selectedColumns,
        }),
      })
      setPreview(loaded)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 503 && caught.detail?.fallback === 'rule_clean') {
        setPreviewFallback(true)
        setPreviewError('OpenRefine 不可用，已降级为规则清洗。')
      } else {
        setPreviewError(caught instanceof Error ? caught.message : 'OpenRefine 预览失败')
      }
    } finally {
      setPreviewBusy(false)
    }
  }

  const adoptPreview = async () => {
    if (!preview) return
    setPreviewBusy(true)
    try {
      const adopted = await apiFetch<{ data_source_id: string; name: string }>(
        `/api/v1/projects/${projectId}/openrefine/previews/${preview.preview_id}/adopt`,
        { method: 'POST', body: JSON.stringify({}) },
      )
      setPreview(null)
      setAdoptedName(adopted.name)
    } catch (caught) {
      setPreviewError(caught instanceof Error ? caught.message : '采用失败')
    } finally {
      setPreviewBusy(false)
    }
  }

  const cancelPreview = async () => {
    if (!preview) return
    setPreviewBusy(true)
    try {
      await apiFetch(`/api/v1/projects/${projectId}/openrefine/previews/${preview.preview_id}`, { method: 'DELETE' })
      setPreview(null)
      setPreviewError('')
    } catch (caught) {
      setPreviewError(caught instanceof Error ? caught.message : '撤销失败')
    } finally {
      setPreviewBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold text-neutral-900">处理方法</h1>
        <p className="mt-1.5 text-sm text-neutral-500">根据数据特征选择清洗、跨库匹配或指标统一，然后运行。</p>
      </header>

      <div className="bg-white border border-neutral-200 rounded-xl p-5 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <div className="text-xs font-medium text-neutral-500">执行建议</div>
          <h2 className="text-lg font-semibold text-neutral-900 mt-1">
            当前阶段：{stage} {routing?.meta?.title ? `· ${routing.meta.short}` : ''}
          </h2>
          {loadError && <p role="alert" className="mt-2 text-xs text-red-600">{loadError}</p>}
          {routing?.method && (
            <div className="mt-2 rounded-md border border-neutral-200 bg-neutral-50 p-2.5 text-xs">
              <div className="font-medium text-neutral-800">建议方法：{routing.method.label}</div>
              {routing.method.reason && <p className="mt-1 text-neutral-500">{routing.method.reason}</p>}
            </div>
          )}
          {routing?.reasons ? (
            <ul className="list-disc pl-5 text-xs text-neutral-600 mt-1.5 space-y-0.5">
              {routing.reasons.map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-neutral-500 mt-1">
              L2 和 L4 可以直接使用示例数据。L1 需要先在{' '}
              <button onClick={onGoDataSources} className="text-neutral-900 underline font-medium">
                数据源
              </button>{' '}
              加载示例或上传文件。
            </p>
          )}
        </div>

        <div className="flex flex-col items-end gap-2 shrink-0">
          <div className="flex gap-1.5">
            {(['L1', 'L2', 'L4'] as const).map((s) => (
              <button
                key={s}
                onClick={() => setStage(s)}
                className={`px-3 py-1 rounded-lg text-xs font-medium border ${
                  stage === s ? 'bg-brand-600 text-white border-brand-600' : 'bg-white text-neutral-600 border-neutral-200'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            {isOpenRefine && (
              <button
                disabled={previewBusy || needsDataSource}
                onClick={runOpenRefinePreview}
                className="px-4 py-2 border border-neutral-300 bg-white hover:bg-neutral-50 text-neutral-900 font-medium text-sm rounded-lg disabled:opacity-40"
              >
                {previewBusy ? '正在清洗…' : 'OpenRefine 预览'}
              </button>
            )}
            <button
              disabled={running || needsDataSource}
              onClick={submitJob}
              className="px-4 py-2 bg-neutral-900 hover:bg-neutral-800 text-white font-medium text-sm rounded-lg flex items-center gap-2 disabled:opacity-40"
            >
              <Play className="w-4 h-4" /> {running ? '运行中…' : (isOpenRefine ? '运行规则清洗' : `运行 ${stage}`)}
            </button>
          </div>
          {needsDataSource && <span className="text-[11px] text-neutral-500">先在数据源加载示例或上传文件</span>}
        </div>
      </div>

      {isOpenRefine && dsIds.length > 0 && currentRecipe && (
        <div className="bg-white border border-neutral-200 rounded-xl p-5 space-y-3">
          <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <div className="text-sm font-semibold text-neutral-900">字段设置</div>
              <p className="mt-0.5 text-xs text-neutral-500">选择配方并映射数据字段。</p>
            </div>
            <select
              aria-label="配方"
              value={recipeId}
              onChange={(e) => applyRecipe(recipes.find((recipe) => recipe.id === e.target.value))}
              className="border border-neutral-300 rounded-md px-2.5 py-1.5 bg-white text-xs"
            >
              {recipes.map((recipe) => (
                <option key={recipe.id} value={recipe.id}>{recipe.name}</option>
              ))}
            </select>
          </div>

          {currentRecipe.mode === 'per_column' ? (
            <div className="flex flex-wrap gap-3">
              {(currentRecipe.columns || []).map((column: string) => (
                <label key={column} className="flex items-center gap-1.5 text-xs text-neutral-700">
                  <input
                    type="checkbox"
                    checked={selectedColumns.includes(column)}
                    onChange={(e) =>
                      setSelectedColumns(e.target.checked
                        ? [...selectedColumns, column]
                        : selectedColumns.filter((item) => item !== column))
                    }
                  />
                  {column}
                </label>
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {(currentRecipe.fields || []).map((field: any) => (
                <label key={field.key} className="flex flex-col gap-1 text-xs">
                  <span className="font-medium text-neutral-700">{field.label}</span>
                  <select
                    value={fieldMap[field.key] || ''}
                    onChange={(e) => setFieldMap({ ...fieldMap, [field.key]: e.target.value })}
                    className="border border-neutral-300 rounded-md px-2.5 py-1.5 bg-white"
                  >
                    <option value="">未选择</option>
                    {(field.options || []).map((option: string) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          )}
        </div>
      )}

      {(preview || previewError || adoptedName) && (
        <div className="bg-white border border-neutral-200 rounded-xl p-5 space-y-4">
          {adoptedName ? (
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="text-sm font-semibold text-neutral-900">已生成新数据源：{adoptedName}</div>
                <p className="mt-1 text-xs text-neutral-500">原数据源没有变化。新数据源可在数据页继续查看或用于匹配。</p>
              </div>
              <button onClick={onGoDataSources} className="rounded-lg border border-neutral-300 px-4 py-2 text-xs font-medium text-neutral-800">
                查看数据源
              </button>
            </div>
          ) : preview ? (
            <>
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-neutral-900">OpenRefine 清洗预览 · {preview.recipe.name}</div>
                  <p className="mt-1 text-xs text-neutral-500">{preview.recipe.description}（配方 v{preview.recipe.version}）</p>
                </div>
                <div className="flex gap-2">
                  <button
                    disabled={previewBusy}
                    onClick={adoptPreview}
                    className="rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white disabled:opacity-40"
                  >
                    采用并生成新数据源
                  </button>
                  <button
                    disabled={previewBusy}
                    onClick={cancelPreview}
                    className="rounded-lg border border-neutral-300 px-4 py-2 text-xs font-medium text-neutral-700 disabled:opacity-40"
                  >
                    撤销
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
                  <div className="text-lg font-semibold text-neutral-900">{preview.diff.changed_rows}</div>
                  <div className="text-[11px] text-neutral-500">变更行</div>
                </div>
                <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
                  <div className="text-lg font-semibold text-neutral-900">{preview.diff.changed_cells}</div>
                  <div className="text-[11px] text-neutral-500">变更单元格</div>
                </div>
                <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
                  <div className="text-lg font-semibold text-neutral-900">{preview.diff.before_rows} → {preview.diff.after_rows}</div>
                  <div className="text-[11px] text-neutral-500">行数</div>
                </div>
                <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
                  <div className="text-lg font-semibold text-neutral-900">
                    {(preview.diff.columns_added.length + preview.diff.columns_removed.length) || '—'}
                  </div>
                  <div className="text-[11px] text-neutral-500">列变化</div>
                </div>
              </div>

              {preview.diff.examples.length > 0 ? (
                <div className="overflow-hidden rounded-lg border border-neutral-200">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-neutral-50 text-neutral-500">
                      <tr><th className="p-2.5">行</th><th className="p-2.5">字段</th><th className="p-2.5">清洗前</th><th className="p-2.5">清洗后</th></tr>
                    </thead>
                    <tbody>
                      {preview.diff.examples.map((example, index) => (
                        <tr key={`${example.row}-${example.column}-${index}`} className="border-t border-neutral-100">
                          <td className="p-2.5 text-neutral-500">{example.row}</td>
                          <td className="p-2.5 font-medium text-neutral-800">{example.column}</td>
                          <td className="p-2.5 text-neutral-500 break-all">{example.before || '（空）'}</td>
                          <td className="p-2.5 text-neutral-900 break-all">{example.after || '（空）'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-neutral-500">这个配方没有产生需要变更的单元格。</p>
              )}

              <div className="flex flex-wrap items-center gap-3 text-xs">
                <button
                  onClick={() => setViewerSource({ kind: 'artifact', id: preview.artifact_id, filename: `${preview.recipe.id}.csv` })}
                  className="rounded-lg border border-neutral-200 px-3 py-1.5 font-medium text-neutral-600"
                >
                  查看清洗后的 CSV
                </button>
                {preview.diff.columns_added.length > 0 && <span className="text-neutral-500">新增列：{preview.diff.columns_added.join('、')}</span>}
                {preview.diff.columns_removed.length > 0 && <span className="text-neutral-500">删除列：{preview.diff.columns_removed.join('、')}</span>}
              </div>
            </>
          ) : (
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <p role="alert" className="text-sm text-red-600">{previewError}</p>
              {previewFallback && (
                <button onClick={submitJob} className="rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white">
                  运行规则清洗
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {routing?.recommended_stage === 'L3' && (
        <div className="flex flex-col gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 md:flex-row md:items-center md:justify-between">
          <div>
            <b>L3 契约统一已迁移到契约统一工作台。</b>
            <span className="ml-1">请选择真实契约文件和已发布实体快照生成版本；旧 L3 作业入口已停用。</span>
          </div>
          <button onClick={onGoContracts} className="shrink-0 rounded-lg bg-amber-700 px-4 py-2 text-xs font-medium text-white hover:bg-amber-800">
            前往契约统一
          </button>
        </div>
      )}

      {/* 阶段参数微调 */}
      {(profile || stage === 'L2' || stage === 'L4') && (
      <div className="bg-white border border-neutral-200 rounded-xl p-5 flex flex-wrap items-center gap-4 text-xs">
        {profile && (
          <span className="px-2.5 py-1 bg-neutral-100 rounded-lg font-semibold text-neutral-700">
            已接入 {profile.file_count} 份数据 · {profile.total_rows?.toLocaleString()} 行
          </span>
        )}
        {stage === 'L2' && (
          <>
            <label className="flex items-center gap-1.5 font-semibold text-neutral-700">
              <input type="checkbox" checked={enableHybrid} onChange={(e) => setEnableHybrid(e.target.checked)} />
              灰色地带交给 LLM 终审并进入复核
            </label>
            <select value={providerId} onChange={(e) => setProviderId(e.target.value)} className="border border-neutral-300 rounded-md px-2.5 py-1.5 bg-white">
              <option value="">离线模式</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.default_model})</option>
              ))}
            </select>
          </>
        )}
        {stage === 'L4' && (
          <select value={metricName} onChange={(e) => setMetricName(e.target.value)} className="border border-neutral-200 rounded-lg px-2.5 py-1.5 font-semibold">
            <option value="net_gmv">net_gmv（净成交额 · 剔除异常单）</option>
            <option value="signed_fulfillment_rate">signed_fulfillment_rate（签收履约率）</option>
            <option value="settled_revenue">settled_revenue（财务实收核销额）</option>
          </select>
        )}
      </div>
      )}

      {/* 实时任务进度条与结果看板 */}
      {job && (
        <div className="bg-white border border-neutral-200 rounded-xl p-6  space-y-5">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-2">
              {job.status === 'SUCCESS' ? (
                <CheckCircle2 className="w-5 h-5 text-green-600" />
              ) : job.status === 'FAILED' ? (
                <AlertTriangle className="w-5 h-5 text-red-600" />
              ) : (
                <div className="w-4 h-4 rounded-full border-2 border-brand-600 border-t-transparent animate-spin" />
              )}
              <span className="font-semibold text-sm">{job.stage} 任务状态：{job.status}</span>
              <span className="text-xs text-neutral-500">· {job.stage_message}</span>
            </div>
            <div className="flex gap-2 flex-wrap">
              {(job.result?.artifacts || []).map((a: any) => (
                <div key={a.id} className="flex items-center gap-1">
                  <ArtifactDownload
                    id={a.id} filename={a.filename}
                    className="px-3 py-1.5 bg-brand-50 text-brand-600 rounded-lg text-xs font-medium flex items-center gap-1"
                  >
                    <Download className="w-3.5 h-3.5" /> v{a.version} {a.filename}
                  </ArtifactDownload>
                  <button onClick={() => setViewerSource({ kind: 'artifact', id: a.id, filename: a.filename })} className="rounded-lg border border-neutral-200 px-3 py-1.5 text-xs font-medium text-neutral-600">
                    查看
                  </button>
                </div>
              ))}
            </div>
          </div>

          <div className="w-full h-2 bg-neutral-100 rounded-full overflow-hidden">
            <div className="h-full bg-brand-600 transition-all duration-300" style={{ width: `${job.progress}%` }} />
          </div>

          {/* L1 结果 */}
          {job.status === 'SUCCESS' && job.stage === 'L1' && (
            <div className="space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="p-4 bg-neutral-50 rounded-xl border border-neutral-200"><div className="text-2xl font-semibold text-brand-600">{job.result.total_rows}</div><div className="text-xs text-neutral-500">总行数</div></div>
                <div className="p-4 bg-red-50 rounded-xl border border-red-200"><div className="text-2xl font-semibold text-red-600">{job.result.before_unique} 种</div><div className="text-xs text-red-700">清洗前马甲裂变</div></div>
                <div className="p-4 bg-green-50 rounded-xl border border-green-200"><div className="text-2xl font-semibold text-green-600">{job.result.after_unique} 家</div><div className="text-xs text-green-700">对齐后标准实体</div></div>
                <div className="p-4 bg-brand-50 rounded-xl border border-brand-200"><div className="text-2xl font-semibold text-brand-600">{job.result.review_queued} 条</div><div className="text-xs text-brand-700">已推入复核队列</div></div>
              </div>
              {(job.result?.reference_catalog?.kind === 'roster' || job.result?.reference_catalog?.kind === 'demo') && (
                <p className="text-xs text-neutral-500">
                  对齐来源：{job.result.reference_catalog.kind === 'roster'
                    ? `标准花名册 ${job.result.reference_catalog.name}`
                    : '演示花名册'}
                </p>
              )}
            </div>
          )}

          {/* L2 结果 */}
          {job.status === 'SUCCESS' && job.stage === 'L2' && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="p-4 bg-neutral-50 rounded-xl border border-neutral-200"><div className="text-2xl font-semibold text-brand-600">{job.result.elapsed_sec}s</div><div className="text-xs text-neutral-500">执行耗时</div></div>
                <div className="p-4 bg-green-50 rounded-xl border border-green-200"><div className="text-2xl font-semibold text-green-600">{job.result.reduction_pct}%</div><div className="text-xs text-green-700">Blocking 压缩率</div></div>
                <div className="p-4 bg-neutral-50 rounded-xl border border-neutral-200"><div className="text-2xl font-semibold text-neutral-900">{job.result.unique_clusters?.toLocaleString()}</div><div className="text-xs text-neutral-500">归并唯一实体</div></div>
                <div className="p-4 bg-brand-50 rounded-xl border border-brand-200"><div className="text-2xl font-semibold text-brand-600">{job.result.token_cost_saved_pct}%</div><div className="text-xs text-brand-700">漏斗节省 Token</div></div>
              </div>
              {job.result.weights_artifact_id && (
                <ArtifactPreview id={job.result.weights_artifact_id} className="w-full h-80 border border-neutral-200 rounded-xl" />
              )}
              {(job.result.waterfall_artifacts || []).map((wf: any) => (
                <div key={wf.id} className="border border-neutral-200 rounded-xl p-3">
                  <div className="text-xs font-medium text-neutral-800 mb-2">{wf.title}</div>
                  <ArtifactPreview id={wf.id} className="w-full h-96 border-0" />
                </div>
              ))}
            </div>
          )}

          {/* L4 结果 */}
          {job.status === 'SUCCESS' && job.stage === 'L4' && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="border border-red-200 bg-red-50/50 rounded-xl p-4">
                  <div className="text-xs font-semibold text-red-600 mb-2">未统一口径：各部门 GMV 不一致</div>
                  {(job.result.conflict_rows || []).map((r: any, idx: number) => (
                    <div key={idx} className="flex justify-between text-xs py-1.5 border-b border-red-100 last:border-0">
                      <span>{r['部门口径']}</span>
                      <span className="font-semibold text-red-600">¥{Number(r['算出金额']).toLocaleString()}</span>
                    </div>
                  ))}
                </div>
                <div className="border border-green-200 bg-green-50/50 rounded-xl p-4">
                  <div className="text-xs font-semibold text-green-700 mb-2">统一口径结果：{job.result.selected_metric}</div>
                  {(job.result.result_rows || []).map((r: any, idx: number) => (
                    <div key={idx} className="flex justify-between text-xs py-1.5 border-b border-green-100 last:border-0">
                      <span>{r.order_channel || '全公司'}</span>
                      <span className="font-semibold text-green-700">{Number(r[job.result.selected_metric]).toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              </div>
              <pre className="bg-neutral-900 text-neutral-100 p-4 rounded-xl text-xs overflow-x-auto">{job.result.compiled_sql}</pre>
            </div>
          )}
        </div>
      )}
      <ResultViewer source={viewerSource} onClose={() => setViewerSource(null)} />
    </div>
  )
}
