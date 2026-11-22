import React, { useEffect, useState } from 'react'
import { Rocket, KeyRound, Download, RotateCcw, Copy } from 'lucide-react'
import { apiFetch, ProjectItem } from '../lib/api'

interface Props {
  project: ProjectItem
}

export default function ServingReleasePage({ project }: Props) {
  const [artifacts, setArtifacts] = useState<any[]>([])
  const [keys, setKeys] = useState<any[]>([])
  const [keyName, setKeyName] = useState('生产 AI Agent 调用 Key')
  const [newKeyOnce, setNewKeyOnce] = useState('')

  const load = async () => {
    const [a, k] = await Promise.all([
      apiFetch<any[]>(`/api/v1/projects/${project.id}/artifacts`),
      apiFetch<any[]>(`/api/v1/projects/${project.id}/api-keys`),
    ])
    setArtifacts(a)
    setKeys(k)
  }

  useEffect(() => {
    load()
  }, [project.id])

  const publish = async (artId: string) => {
    await apiFetch(`/api/v1/artifacts/${artId}/publish`, { method: 'POST' })
    await load()
  }

  const createKey = async () => {
    if (!keyName.trim()) return
    const res = await apiFetch<{ api_key_once: string }>(`/api/v1/projects/${project.id}/api-keys`, {
      method: 'POST',
      body: JSON.stringify({ name: keyName }),
    })
    setNewKeyOnce(res.api_key_once)
    await load()
  }

  const revokeKey = async (id: string) => {
    await apiFetch(`/api/v1/api-keys/${id}`, { method: 'DELETE' })
    await load()
  }

  const releasable = artifacts.filter((a) => a.artifact_type === 'l3_contract' || a.artifact_type === 'l4_metrics')

  return (
    <div className="space-y-5">
      {/* 草稿 / 发布 / 回滚版本中心 */}
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h2 className="text-lg font-extrabold text-slate-900 flex items-center gap-2">
          <Rocket className="w-5 h-5 text-brand-600" /> 契约与指标发布中心（草稿 / 线上发布 / 一键回滚）
        </h2>
        <p className="text-xs text-slate-500 mt-1 mb-4">
          每次运行 L3 或 L4 都会保存为不可变版本号（默认 DRAFT）；只有点击“发布到线上 API”才会切换对外 Serving API 生效版本，可随时回滚历史版本。
        </p>

        {releasable.length === 0 ? (
          <p className="text-xs text-slate-400">尚未生成 L3 契约或 L4 指标版本，请先去智能工作台运行 L3 / L4。</p>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-xl">
            <table className="w-full text-xs text-left">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="p-3">类型</th>
                  <th className="p-3">版本</th>
                  <th className="p-3">文件名</th>
                  <th className="p-3">发布状态</th>
                  <th className="p-3">操作</th>
                </tr>
              </thead>
              <tbody>
                {releasable.map((a) => (
                  <tr key={a.id} className="border-t border-slate-100">
                    <td className="p-3 font-bold">{a.artifact_type === 'l3_contract' ? 'L3 校验契约' : 'L4 指标说明书'}</td>
                    <td className="p-3 font-mono font-bold text-brand-600">v{a.version_no}</td>
                    <td className="p-3">{a.filename}</td>
                    <td className="p-3">
                      <span
                        className={`px-2 py-0.5 rounded font-bold ${
                          a.release_status === 'PUBLISHED'
                            ? 'bg-green-50 text-green-700'
                            : a.release_status === 'ARCHIVED'
                              ? 'bg-slate-100 text-slate-500'
                              : 'bg-amber-50 text-amber-700'
                        }`}
                      >
                        {a.release_status}
                      </span>
                    </td>
                    <td className="p-3 flex items-center gap-2">
                      {a.release_status !== 'PUBLISHED' && (
                        <button
                          onClick={() => publish(a.id)}
                          className="px-2.5 py-1 bg-brand-600 text-white rounded-md font-bold flex items-center gap-1"
                        >
                          {a.release_status === 'ARCHIVED' ? <RotateCcw className="w-3 h-3" /> : <Rocket className="w-3 h-3" />}
                          {a.release_status === 'ARCHIVED' ? '回滚到此版本' : '发布到线上 API'}
                        </button>
                      )}
                      <a href={`/api/v1/artifacts/${a.id}/content`} className="px-2.5 py-1 border border-slate-200 rounded-md font-semibold flex items-center gap-1">
                        <Download className="w-3 h-3" /> 下载
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 项目 Serving API Key 管理 */}
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm space-y-4">
        <h3 className="text-sm font-extrabold text-slate-900 flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-brand-600" /> 对外标准 Serving API Key（供外部系统与 AI Agent 调用）
        </h3>
        <div className="flex gap-2">
          <input
            value={keyName}
            onChange={(e) => setKeyName(e.target.value)}
            className="flex-1 max-w-sm border border-slate-200 rounded-lg px-3 py-2 text-xs"
            placeholder="输入密钥用途说明"
          />
          <button onClick={createKey} className="px-4 py-2 bg-brand-600 text-white rounded-lg text-xs font-bold">
            + 签发新 API Key
          </button>
        </div>

        {newKeyOnce && (
          <div className="p-3.5 bg-green-50 border border-green-200 rounded-xl text-xs text-green-900 flex items-center justify-between gap-3">
            <div>
              <div className="font-bold">✅ 新密钥已签发（仅展示这一次，请立即复制保存）：</div>
              <code className="block mt-1 font-mono font-bold text-green-700 break-all">{newKeyOnce}</code>
            </div>
            <button
              onClick={() => navigator.clipboard.writeText(newKeyOnce)}
              className="px-3 py-1.5 bg-white border border-green-300 rounded-lg font-bold flex items-center gap-1 shrink-0"
            >
              <Copy className="w-3.5 h-3.5" /> 复制
            </button>
          </div>
        )}

        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center justify-between border border-slate-200 rounded-xl px-4 py-2.5 text-xs">
              <div>
                <span className="font-bold text-slate-800">{k.name}</span>
                <span className="ml-3 font-mono text-slate-500">{k.key_prefix}****</span>
                <span className={`ml-2 px-2 py-0.5 rounded font-bold ${k.is_active ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>
                  {k.is_active ? '生效中' : '已吊销'}
                </span>
              </div>
              {k.is_active && (
                <button onClick={() => revokeKey(k.id)} className="text-red-600 font-bold hover:underline">
                  吊销
                </button>
              )}
            </div>
          ))}
        </div>

        <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl text-xs overflow-x-auto leading-relaxed">
{`# 1. 标准实体对齐 API（优先命中人工沉淀字典）
curl -X POST http://localhost:8080/api/v1/serve/${project.slug}/reconcile \\
  -H "X-API-Key: <YOUR_KEY>" -H "Content-Type: application/json" \\
  -d '{"query": "星云制造科技"}'

# 2. L3 LinkML 契约校验闸门 API（按当前 PUBLISHED 版本）
curl -X POST http://localhost:8080/api/v1/serve/${project.slug}/validate \\
  -H "X-API-Key: <YOUR_KEY>" -H "Content-Type: application/json" \\
  -d '{"entity_name": "ShipmentOrder", "payload": {"shipment_id":"S1","order_id":"O1","carrier_code":"SUP-1003","delivery_status":"IN_TRANSIT"}}'

# 3. L4 指标语义层标准查询 API（按当前 PUBLISHED 版本）
curl -X POST http://localhost:8080/api/v1/serve/${project.slug}/metrics/query \\
  -H "X-API-Key: <YOUR_KEY>" -H "Content-Type: application/json" \\
  -d '{"metric_name": "net_gmv", "group_by": "order_channel"}'`}
        </pre>
      </div>
    </div>
  )
}
