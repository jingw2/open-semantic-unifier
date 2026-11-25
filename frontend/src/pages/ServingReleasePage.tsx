import { ArtifactDownload } from '../components/ArtifactAccess'
import React, { useEffect, useState } from 'react'
import { Rocket, KeyRound, Download, RotateCcw, Copy } from 'lucide-react'
import { apiFetch, ProjectItem } from '../lib/api'

interface Props {
  project: ProjectItem
}

export default function ServingReleasePage({ project }: Props) {
  const [artifacts, setArtifacts] = useState<any[]>([])
  const [keys, setKeys] = useState<any[]>([])
  const [keyName, setKeyName] = useState('生产调用 Key')
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
      <div className="bg-white border border-neutral-200 rounded-xl p-6 ">
        <h2 className="text-lg font-semibold text-neutral-900 flex items-center gap-2">
          <Rocket className="w-5 h-5 text-neutral-500" /> 发布中心
        </h2>
        <p className="text-xs text-neutral-500 mt-1 mb-4">
          每次运行会保存一个不可变版本（默认 DRAFT）。只有点击“发布到线上 API”才会切换对外生效版本，可随时回滚。
        </p>

        {releasable.length === 0 ? (
          <p className="text-xs text-neutral-500">还没有已生成的版本。先发布 L3 契约或运行 L4 指标。</p>
        ) : (
          <div className="overflow-x-auto border border-neutral-200 rounded-xl">
            <table className="w-full text-xs text-left">
              <thead className="bg-neutral-50 text-neutral-600">
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
                  <tr key={a.id} className="border-t border-neutral-100">
                    <td className="p-3 font-medium">{a.artifact_type === 'l3_contract' ? 'L3 校验契约' : 'L4 指标说明书'}</td>
                    <td className="p-3 font-mono font-medium text-brand-600">v{a.version_no}</td>
                    <td className="p-3">{a.filename}</td>
                    <td className="p-3">
                      <span
                        className={`px-2 py-0.5 rounded font-medium ${
                          a.release_status === 'PUBLISHED'
                            ? 'bg-green-50 text-green-700'
                            : a.release_status === 'ARCHIVED'
                              ? 'bg-neutral-100 text-neutral-500'
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
                          className="px-2.5 py-1 bg-brand-600 text-white rounded-md font-medium flex items-center gap-1"
                        >
                          {a.release_status === 'ARCHIVED' ? <RotateCcw className="w-3 h-3" /> : <Rocket className="w-3 h-3" />}
                          {a.release_status === 'ARCHIVED' ? '回滚到此版本' : '发布到线上 API'}
                        </button>
                      )}
                      <ArtifactDownload id={a.id} filename={a.filename} className="px-2.5 py-1 border border-neutral-200 rounded-md font-semibold flex items-center gap-1">
                        <Download className="w-3 h-3" /> 下载
                      </ArtifactDownload>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 项目 Serving API Key 管理 */}
      <div className="bg-white border border-neutral-200 rounded-xl p-6  space-y-4">
        <h3 className="text-sm font-semibold text-neutral-900 flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-neutral-500" /> 对外 Serving API Key
        </h3>
        <div className="flex gap-2">
          <input
            value={keyName}
            onChange={(e) => setKeyName(e.target.value)}
            className="flex-1 max-w-sm border border-neutral-200 rounded-lg px-3 py-2 text-xs"
            placeholder="输入密钥用途说明"
          />
          <button onClick={createKey} className="px-4 py-2 bg-brand-600 text-white rounded-lg text-xs font-medium">
            + 签发新 API Key
          </button>
        </div>

        {newKeyOnce && (
          <div className="p-3.5 bg-green-50 border border-green-200 rounded-xl text-xs text-green-900 flex items-center justify-between gap-3">
            <div>
              <div className="font-medium">新密钥已签发。只展示这一次，请立即复制保存。</div>
              <code className="block mt-1 font-mono font-medium text-green-700 break-all">{newKeyOnce}</code>
            </div>
            <button
              onClick={() => navigator.clipboard.writeText(newKeyOnce)}
              className="px-3 py-1.5 bg-white border border-green-300 rounded-lg font-medium flex items-center gap-1 shrink-0"
            >
              <Copy className="w-3.5 h-3.5" /> 复制
            </button>
          </div>
        )}

        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center justify-between border border-neutral-200 rounded-xl px-4 py-2.5 text-xs">
              <div>
                <span className="font-medium text-neutral-800">{k.name}</span>
                <span className="ml-3 font-mono text-neutral-500">{k.key_prefix}****</span>
                <span className={`ml-2 px-2 py-0.5 rounded font-medium ${k.is_active ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>
                  {k.is_active ? '生效中' : '已吊销'}
                </span>
              </div>
              {k.is_active && (
                <button onClick={() => revokeKey(k.id)} className="text-red-600 font-medium hover:underline">
                  吊销
                </button>
              )}
            </div>
          ))}
        </div>

        <pre className="bg-neutral-900 text-neutral-100 p-4 rounded-xl text-xs overflow-x-auto leading-relaxed">
{`# 1. 统一实体解析 API（必须传入要读取的已发布 snapshot_id）
curl -X POST http://localhost:8080/api/v1/serve/${project.slug}/unification/resolve \\
  -H "X-API-Key: <YOUR_KEY>" -H "Content-Type: application/json" \\
  -d '{"snapshot_id": "<SNAPSHOT_ID>", "source_system": "erp", "dataset": "suppliers", "source_record_id": "S-1001"}'

# 兼容旧版别名字典 API（不读取实体统一快照）
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
