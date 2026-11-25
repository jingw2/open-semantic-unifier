import React from 'react'

export type ReviewPair = {
  left_source_revision_id: string
  right_source_revision_id: string
  candidate: boolean
  action: string
  evidence_codes?: string[]
  evidence?: Array<Record<string, unknown>>
  recommendation?: {
    verdict?: string
    is_same_entity?: boolean | null
    confidence?: number | null
    distribution?: Record<string, number> | null
    reason?: string | null
    model?: string
    provider_kind?: string
    provider_key?: string
    prompt_version?: string
    latency_ms?: number
    usage?: Record<string, unknown> | null
    cost_usd?: number | null
    trace_id?: string | null
  } | null
  model_error?: string | null
  why?: string
  suggestion?: string
  consequence?: string
  left?: { record_ref?: string; attributes?: Record<string, unknown> }
  right?: { record_ref?: string; attributes?: Record<string, unknown> }
  comparison?: Array<{ field: string; left: unknown; right: unknown; status: 'MATCH' | 'DIFFERENT' | 'MISSING' }>
}

export const VERDICT_ORDER = ['SAME', 'DIFFERENT', 'UNKNOWN'] as const

export function verdictLabel(recommendation: ReviewPair['recommendation']): string | null {
  if (!recommendation) return null
  if (recommendation.verdict) return recommendation.verdict
  if (recommendation.is_same_entity === true) return 'SAME'
  if (recommendation.is_same_entity === false) return 'DIFFERENT'
  return null
}

export function verdictTone(verdict: string): string {
  if (verdict === 'SAME') return 'bg-emerald-100 text-emerald-800'
  if (verdict === 'DIFFERENT') return 'bg-rose-100 text-rose-800'
  return 'bg-amber-100 text-amber-900'
}

const VERDICT_TEXT: Record<string, string> = {
  SAME: '倾向同一实体',
  DIFFERENT: '倾向不同实体',
  UNKNOWN: '不确定',
}

const STATUS_TEXT: Record<string, string> = {
  MATCH: '一致',
  DIFFERENT: '不同',
  MISSING: '未填写',
}

const STATUS_TONE: Record<string, string> = {
  MATCH: 'text-emerald-700',
  DIFFERENT: 'text-rose-700',
  MISSING: 'text-neutral-400',
}

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未填写'
  return String(value)
}

interface Props {
  pair: ReviewPair
  busy: boolean
  onDecide: (action: 'MERGE' | 'KEEP_DISTINCT') => void
}

export default function EntityReviewCard({ pair, busy, onDecide }: Props) {
  const verdict = verdictLabel(pair.recommendation)
  const distribution = pair.recommendation?.distribution || {}
  const hasDistribution = Object.keys(distribution).length > 0
  const leftRef = pair.left?.record_ref || pair.left_source_revision_id.slice(0, 10)
  const rightRef = pair.right?.record_ref || pair.right_source_revision_id.slice(0, 10)

  return (
    <article className="rounded-xl border border-neutral-200 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-neutral-900">这两条记录是不是同一实体？</h3>
          <div className="mt-1 font-mono text-[10px] text-neutral-500">{leftRef} ↔ {rightRef}</div>
        </div>
        {onDecide && (
          <div className="flex gap-2">
            <button disabled={busy} onClick={() => onDecide('MERGE')} className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-medium text-white hover:bg-emerald-800 disabled:opacity-50">确认同一实体</button>
            <button disabled={busy} onClick={() => onDecide('KEEP_DISTINCT')} className="rounded-lg border border-neutral-300 px-3 py-2 text-xs font-medium text-neutral-700 hover:bg-neutral-50 disabled:opacity-50">保持分离</button>
          </div>
        )}
      </div>

      {pair.why && <p className="mt-3 rounded-lg bg-neutral-50 p-3 text-xs leading-5 text-neutral-700">{pair.why}</p>}

      {pair.comparison && pair.comparison.length > 0 && (
        <div className="mt-3 overflow-x-auto rounded-lg border border-neutral-200">
          <table className="w-full text-left text-xs">
            <thead className="bg-neutral-50 text-[10px] text-neutral-500">
              <tr><th className="p-2">字段</th><th className="p-2">左侧</th><th className="p-2">右侧</th><th className="p-2">对比</th></tr>
            </thead>
            <tbody>
              {pair.comparison.map((row) => (
                <tr key={row.field} className="border-t border-neutral-100">
                  <td className="p-2 font-mono text-[10px] text-neutral-600">{row.field}</td>
                  <td className="p-2 text-neutral-700">{display(row.left)}</td>
                  <td className="p-2 text-neutral-700">{display(row.right)}</td>
                  <td className={`p-2 text-[11px] font-medium ${STATUS_TONE[row.status] || ''}`}>{STATUS_TEXT[row.status] || row.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pair.recommendation && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {verdict && <span className={`rounded-md px-2 py-1 text-[10px] font-medium ${verdictTone(verdict)}`}>模型建议 · {verdict}</span>}
            {verdict && <span className="text-[11px] text-neutral-500">{VERDICT_TEXT[verdict] || ''}</span>}
            {typeof pair.recommendation.confidence === 'number' && <span className="text-[11px] font-semibold text-neutral-600">置信度 {(pair.recommendation.confidence * 100).toFixed(0)}%</span>}
            {pair.recommendation.model && <span className="font-mono text-[10px] text-neutral-400">模型 {pair.recommendation.model}</span>}
          </div>
          {hasDistribution && (
            <div className="flex flex-wrap items-center gap-3 text-[10px] text-neutral-500">
              {VERDICT_ORDER.map((key) => {
                const value = distribution[key]
                return (
                  <div key={key} className="flex items-center gap-1.5">
                    <span className="w-14 font-mono">{key}</span>
                    <div className="h-2 w-24 overflow-hidden rounded bg-neutral-100">
                      <div className="h-full rounded bg-neutral-600" style={{ width: `${Math.round((value ?? 0) * 100)}%` }} />
                    </div>
                    <span className="font-mono">{typeof value === 'number' ? `${(value * 100).toFixed(0)}%` : '—'}</span>
                  </div>
                )
              })}
            </div>
          )}
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-neutral-400">
            {pair.recommendation.provider_kind && <span>类型 {pair.recommendation.provider_kind}</span>}
            {pair.recommendation.provider_key && <span>Provider {pair.recommendation.provider_key}</span>}
            {pair.recommendation.prompt_version && <span>提示词 {pair.recommendation.prompt_version}</span>}
            {typeof pair.recommendation.latency_ms === 'number' && <span>耗时 {pair.recommendation.latency_ms}ms</span>}
          </div>
          {pair.recommendation.reason && <p className="border-l-2 border-neutral-300 pl-3 text-xs text-neutral-600">模型理由：{pair.recommendation.reason}</p>}
        </div>
      )}
      {pair.model_error && <p className="mt-2 text-xs text-rose-700">模型未给出建议：{pair.model_error}</p>}

      {(pair.suggestion || pair.consequence) && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
          {pair.suggestion && <span className="font-medium text-neutral-800">{pair.suggestion}</span>}
          {pair.consequence && <span className="text-neutral-500">{pair.consequence}</span>}
        </div>
      )}

      {(pair.evidence_codes?.length || pair.evidence?.length) ? (
        <details className="mt-3 rounded-lg border border-neutral-200 p-3">
          <summary className="cursor-pointer text-[11px] font-medium text-neutral-600">查看原始证据</summary>
          {pair.evidence_codes && <div className="mt-2 flex flex-wrap gap-1.5">{pair.evidence_codes.map((code) => <span key={code} className="rounded bg-neutral-100 px-2 py-1 font-mono text-[10px] text-neutral-600">{code}</span>)}</div>}
          {pair.evidence && <div className="mt-2 grid gap-2 md:grid-cols-2">{pair.evidence.map((evidence, index) => <pre key={index} className="overflow-x-auto rounded-lg bg-neutral-950 p-3 font-mono text-[10px] leading-4 text-neutral-100">{JSON.stringify(evidence, null, 2)}</pre>)}</div>}
        </details>
      ) : null}
    </article>
  )
}
