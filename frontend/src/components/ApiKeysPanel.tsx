import React, { useEffect, useState } from 'react'
import { Copy, KeyRound } from 'lucide-react'
import { apiFetch } from '../lib/api'

export default function ApiKeysPanel({ projectId }: { projectId: string }) {
  const [keys, setKeys] = useState<any[]>([])
  const [keyName, setKeyName] = useState('生产调用 Key')
  const [newKeyOnce, setNewKeyOnce] = useState('')

  const load = async () => {
    setKeys(await apiFetch<any[]>(`/api/v1/projects/${projectId}/api-keys`))
  }

  useEffect(() => {
    void load()
  }, [projectId])

  const createKey = async () => {
    if (!keyName.trim()) return
    const res = await apiFetch<{ api_key_once: string }>(`/api/v1/projects/${projectId}/api-keys`, {
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

  return (
    <div className="space-y-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
        <KeyRound className="h-4 w-4 text-neutral-500" /> 对外 Serving API Key
      </h3>
      <div className="flex gap-2">
        <input
          value={keyName}
          onChange={(event) => setKeyName(event.target.value)}
          className="w-full max-w-sm rounded-lg border border-neutral-300 px-3 py-2 text-xs"
          placeholder="输入密钥用途说明"
        />
        <button onClick={() => void createKey()} className="rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white">
          + 签发新 API Key
        </button>
      </div>

      {newKeyOnce && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-green-200 bg-green-50 p-3.5 text-xs text-green-900">
          <div>
            <div className="font-medium">新密钥已签发。只展示这一次，请立即复制保存。</div>
            <code className="mt-1 block break-all font-mono font-medium text-green-700">{newKeyOnce}</code>
          </div>
          <button
            onClick={() => navigator.clipboard.writeText(newKeyOnce)}
            className="flex shrink-0 items-center gap-1 rounded-lg border border-green-300 bg-white px-3 py-1.5 font-medium"
          >
            <Copy className="h-3.5 w-3.5" /> 复制
          </button>
        </div>
      )}

      <div className="space-y-2">
        {keys.map((key) => (
          <div key={key.id} className="flex items-center justify-between rounded-xl border border-neutral-200 px-4 py-2.5 text-xs">
            <div>
              <span className="font-medium text-neutral-800">{key.name}</span>
              <span className="ml-3 font-mono text-neutral-500">{key.key_prefix}****</span>
              <span className={`ml-2 rounded px-2 py-0.5 font-medium ${key.is_active ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>
                {key.is_active ? '生效中' : '已吊销'}
              </span>
            </div>
            {key.is_active && (
              <button onClick={() => void revokeKey(key.id)} className="font-medium text-red-600 hover:underline">
                吊销
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
