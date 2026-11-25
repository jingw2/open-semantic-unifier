import { ArtifactDownload, ArtifactPreview } from '../components/ArtifactAccess'
import React, { useEffect, useState } from 'react'
import { Play, Download, CheckCircle2, AlertTriangle } from 'lucide-react'
import { apiFetch, JobItem } from '../lib/api'

type WorkbenchStage = 'L1' | 'L2' | 'L4'

function normalizeStage(value: unknown): WorkbenchStage {
  return value === 'L2' || value === 'L4' ? value : 'L1'
}

interface Props {
  projectId: string
  activeDsIds: string[]
  activeProfile: any
  activeRouting: any
  onGoDataSources: () => void
  onGoContracts: () => void
}

export default function WorkbenchPage({ projectId, activeDsIds, activeProfile, activeRouting, onGoDataSources, onGoContracts }: Props) {
  const [stage, setStage] = useState<WorkbenchStage>(normalizeStage(activeRouting?.recommended_stage))
  const [providers, setProviders] = useState<any[]>([])
  const [providerId, setProviderId] = useState('')
  const [metricName, setMetricName] = useState('net_gmv')
  const [enableHybrid, setEnableHybrid] = useState(true)

  const [job, setJob] = useState<JobItem | null>(null)
  const [running, setRunning] = useState(false)
  const needsDataSource = stage === 'L1' && activeDsIds.length === 0

  useEffect(() => {
    if (activeRouting?.recommended_stage) {
      setStage(normalizeStage(activeRouting.recommended_stage))
    }
  }, [activeRouting])

  useEffect(() => {
    apiFetch<{ configured: any[] }>('/api/v1/providers').then((d) => {
      setProviders(d.configured)
      const def = d.configured.find((x) => x.is_default)
      if (def) setProviderId(def.id)
    })
  }, [])

  const submitJob = async () => {
    setRunning(true)
    try {
      const sp = activeRouting?.suggested_params || {}
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
          datasource_ids: activeDsIds,
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

  return (
    <div className="space-y-5">
      <div className="bg-white border border-neutral-200 rounded-xl p-5 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <div className="text-xs font-medium text-neutral-500">执行建议</div>
          <h2 className="text-lg font-semibold text-neutral-900 mt-1">
            当前阶段：{stage} {activeRouting?.meta?.title ? `· ${activeRouting.meta.short}` : ''}
          </h2>
          {activeRouting?.reasons ? (
            <ul className="list-disc pl-5 text-xs text-neutral-600 mt-1.5 space-y-0.5">
              {activeRouting.reasons.map((r: string, i: number) => (
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
          <button
            disabled={running || needsDataSource}
            onClick={submitJob}
            className="px-4 py-2 bg-neutral-900 hover:bg-neutral-800 text-white font-medium text-sm rounded-lg flex items-center gap-2 disabled:opacity-40"
          >
            <Play className="w-4 h-4" /> {running ? '运行中…' : `运行 ${stage}`}
          </button>
          {needsDataSource && <span className="text-[11px] text-neutral-500">先在数据源加载示例或上传文件</span>}
        </div>
      </div>

      {activeRouting?.recommended_stage === 'L3' && (
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
      {(activeProfile || stage === 'L2' || stage === 'L4') && (
      <div className="bg-white border border-neutral-200 rounded-xl p-5 flex flex-wrap items-center gap-4 text-xs">
        {activeProfile && (
          <span className="px-2.5 py-1 bg-neutral-100 rounded-lg font-semibold text-neutral-700">
            已接入 {activeProfile.file_count} 份数据 · {activeProfile.total_rows?.toLocaleString()} 行
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
                <ArtifactDownload
                  key={a.id}
                  id={a.id} filename={a.filename}
                  className="px-3 py-1.5 bg-brand-50 text-brand-600 rounded-lg text-xs font-medium flex items-center gap-1"
                >
                  <Download className="w-3.5 h-3.5" /> v{a.version} {a.filename}
                </ArtifactDownload>
              ))}
            </div>
          </div>

          <div className="w-full h-2 bg-neutral-100 rounded-full overflow-hidden">
            <div className="h-full bg-brand-600 transition-all duration-300" style={{ width: `${job.progress}%` }} />
          </div>

          {/* L1 结果 */}
          {job.status === 'SUCCESS' && job.stage === 'L1' && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="p-4 bg-neutral-50 rounded-xl border border-neutral-200"><div className="text-2xl font-semibold text-brand-600">{job.result.total_rows}</div><div className="text-xs text-neutral-500">总行数</div></div>
              <div className="p-4 bg-red-50 rounded-xl border border-red-200"><div className="text-2xl font-semibold text-red-600">{job.result.before_unique} 种</div><div className="text-xs text-red-700">清洗前马甲裂变</div></div>
              <div className="p-4 bg-green-50 rounded-xl border border-green-200"><div className="text-2xl font-semibold text-green-600">{job.result.after_unique} 家</div><div className="text-xs text-green-700">对齐后标准实体</div></div>
              <div className="p-4 bg-brand-50 rounded-xl border border-brand-200"><div className="text-2xl font-semibold text-brand-600">{job.result.review_queued} 条</div><div className="text-xs text-brand-700">已推入复核队列</div></div>
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
    </div>
  )
}
