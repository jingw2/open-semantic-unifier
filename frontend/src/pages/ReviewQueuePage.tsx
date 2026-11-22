import React, { useEffect, useState } from 'react'
import { Check, X, BookMarked } from 'lucide-react'
import { apiFetch } from '../lib/api'

interface Props {
  projectId: string
}

export default function ReviewQueuePage({ projectId }: Props) {
  const [reviews, setReviews] = useState<any[]>([])
  const [mappings, setMappings] = useState<any[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const res = await apiFetch<{ reviews: any[]; mappings: any[] }>(`/api/v1/projects/${projectId}/reviews`)
      setReviews(res.reviews)
      setMappings(res.mappings)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [projectId])

  const decide = async (id: string, decision: 'APPROVED' | 'REJECTED') => {
    await apiFetch(`/api/v1/reviews/${id}/decide`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    })
    await load()
  }

  return (
    <div className="space-y-5">
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-lg font-extrabold text-slate-900">人工复核队列（Human-in-the-Loop）</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              L1 低置信度项与 L2 灰色边界样本（附 LLM 终审建议）将进入此处；点“同意合并”自动沉淀为项目级可复用映射字典
            </p>
          </div>
          <button onClick={load} className="px-3 py-1.5 border border-slate-200 rounded-lg text-xs font-bold">
            刷新队列
          </button>
        </div>

        {loading ? (
          <p className="text-xs text-slate-400">加载中...</p>
        ) : reviews.length === 0 ? (
          <p className="text-xs text-slate-400">暂无待复核样本，先去智能工作台运行一次 L1 或 L2 即可生成。</p>
        ) : (
          <div className="space-y-3">
            {reviews.map((r) => (
              <div key={r.id} className="border border-slate-200 rounded-xl p-4 flex flex-col md:flex-row justify-between gap-4">
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <span className="px-2 py-0.5 bg-brand-50 text-brand-600 rounded text-xs font-bold">{r.stage}</span>
                    <span className="text-xs font-bold text-slate-700">{r.case_type}</span>
                    <span className="text-xs text-slate-400">置信度 {(r.score * 100).toFixed(0)}%</span>
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-bold ${
                        r.decision === 'APPROVED'
                          ? 'bg-green-50 text-green-700'
                          : r.decision === 'REJECTED'
                            ? 'bg-red-50 text-red-600'
                            : 'bg-amber-50 text-amber-700'
                      }`}
                    >
                      {r.decision}
                    </span>
                  </div>
                  <div className="text-sm font-semibold text-slate-900">
                    左：<span className="text-brand-700">{r.left_text}</span> <span className="mx-2 text-slate-400">↔</span> 右：
                    <span className="text-slate-800">{r.right_text}</span>
                  </div>
                  {r.llm_suggestion?.reason && (
                    <div className="text-xs text-slate-500">🤖 建议理由：{r.llm_suggestion.reason}</div>
                  )}
                </div>

                {r.decision === 'PENDING' && (
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => decide(r.id, 'APPROVED')}
                      className="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white rounded-lg text-xs font-bold flex items-center gap-1"
                    >
                      <Check className="w-3.5 h-3.5" /> 同意合并并沉淀
                    </button>
                    <button
                      onClick={() => decide(r.id, 'REJECTED')}
                      className="px-3 py-1.5 bg-red-50 hover:bg-red-100 text-red-600 border border-red-200 rounded-lg text-xs font-bold flex items-center gap-1"
                    >
                      <X className="w-3.5 h-3.5" /> 拒绝合并
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 已沉淀映射字典 */}
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h3 className="text-sm font-extrabold text-slate-900 flex items-center gap-2 mb-3">
          <BookMarked className="w-4 h-4 text-brand-600" /> 已沉淀的可复用映射字典 ({mappings.length})
        </h3>
        {mappings.length === 0 ? (
          <p className="text-xs text-slate-400">暂无人工沉淀记录。</p>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-xl">
            <table className="w-full text-xs text-left">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="p-3">原始别名 / 黑话</th>
                  <th className="p-3">标准编码</th>
                  <th className="p-3">标准全称</th>
                  <th className="p-3">来源</th>
                </tr>
              </thead>
              <tbody>
                {mappings.map((m) => (
                  <tr key={m.id} className="border-t border-slate-100">
                    <td className="p-3 font-semibold">{m.raw_alias}</td>
                    <td className="p-3 font-mono text-brand-600 font-bold">{m.canonical_id}</td>
                    <td className="p-3">{m.canonical_name}</td>
                    <td className="p-3"><span className="px-2 py-0.5 bg-green-50 text-green-700 rounded font-bold">{m.source}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
