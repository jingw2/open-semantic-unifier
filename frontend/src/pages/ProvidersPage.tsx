import React, { useEffect, useState } from 'react'
import { Bot, Plus, Trash2, Zap } from 'lucide-react'
import { apiFetch } from '../lib/api'

export default function ProvidersPage() {
  const [presets, setPresets] = useState<any[]>([])
  const [configured, setConfigured] = useState<any[]>([])
  const [providerKey, setProviderKey] = useState('deepseek')
  const [name, setName] = useState('DeepSeek 生产默认')
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com/v1')
  const [model, setModel] = useState('deepseek-chat')
  const [apiKey, setApiKey] = useState('')
  const [isDefault, setIsDefault] = useState(true)
  const [testMsg, setTestMsg] = useState<Record<string, string>>({})

  const load = async () => {
    const res = await apiFetch<{ builtin_presets: any[]; configured: any[] }>('/api/v1/providers')
    setPresets(res.builtin_presets)
    setConfigured(res.configured)
  }

  useEffect(() => {
    load()
  }, [])

  const applyPreset = (p: any) => {
    setProviderKey(p.provider_key)
    setName(p.name)
    setBaseUrl(p.base_url)
    setModel(p.default_model)
  }

  const saveProvider = async (e: React.FormEvent) => {
    e.preventDefault()
    await apiFetch('/api/v1/providers', {
      method: 'POST',
      body: JSON.stringify({
        provider_key: providerKey,
        name,
        base_url: baseUrl,
        default_model: model,
        api_key: apiKey,
        is_custom: !presets.some((x) => x.provider_key === providerKey),
        is_default: isDefault,
      }),
    })
    setApiKey('')
    await load()
  }

  const testProvider = async (id: string) => {
    setTestMsg((prev) => ({ ...prev, [id]: '测试中...' }))
    const res = await apiFetch<{ status: string; message: string }>(`/api/v1/providers/${id}/test`, { method: 'POST' })
    setTestMsg((prev) => ({ ...prev, [id]: (res.status === 'ok' ? '✅ ' : '❌ ') + res.message }))
  }

  const removeProvider = async (id: string) => {
    await apiFetch(`/api/v1/providers/${id}`, { method: 'DELETE' })
    await load()
  }

  return (
    <div className="space-y-5">
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h2 className="text-lg font-extrabold text-slate-900 flex items-center gap-2">
          <Bot className="w-5 h-5 text-brand-600" /> 多模型 Provider 中心（支持自定义新增 + Fernet 加密存库）
        </h2>
        <p className="text-xs text-slate-500 mt-1 mb-4">
          点击下方主流预设一键填充，或直接输入任意 OpenAI 兼容地址（SiliconFlow / vLLM / OneAPI / Azure）新增自定义 Provider。
        </p>

        <div className="flex flex-wrap gap-2 mb-4">
          {presets.map((p) => (
            <button
              key={p.provider_key}
              type="button"
              onClick={() => applyPreset(p)}
              className="px-3 py-1.5 bg-slate-50 hover:bg-brand-50 border border-slate-200 hover:border-brand-200 rounded-lg text-xs font-bold text-slate-700"
            >
              {p.name}
            </button>
          ))}
        </div>

        <form onSubmit={saveProvider} className="grid grid-cols-1 md:grid-cols-2 gap-3 bg-slate-50 p-4 rounded-xl border border-slate-200">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">Provider 标识（自定义可填如 siliconflow）</label>
            <input value={providerKey} onChange={(e) => setProviderKey(e.target.value)} required className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">显示名称</label>
            <input value={name} onChange={(e) => setName(e.target.value)} required className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">Base URL</label>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.deepseek.com/v1" className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">默认模型名称</label>
            <input value={model} onChange={(e) => setModel(e.target.value)} required className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </div>
          <div className="md:col-span-2">
            <label className="block text-xs font-semibold text-slate-600 mb-1">API Key（服务端 Fernet 加密存储，接口永不明文返回）</label>
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-...（离线模式或 Ollama 可留空）" className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
          </div>
          <div className="md:col-span-2 flex items-center justify-between pt-1">
            <label className="flex items-center gap-2 text-xs font-semibold text-slate-700">
              <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /> 设为工作空间默认模型
            </label>
            <button type="submit" className="px-4 py-2 bg-brand-600 text-white rounded-lg text-xs font-bold flex items-center gap-1.5">
              <Plus className="w-4 h-4" /> 保存并加密入库
            </button>
          </div>
        </form>
      </div>

      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h3 className="text-sm font-extrabold text-slate-900 mb-3">已配置模型 Provider ({configured.length})</h3>
        {configured.length === 0 ? (
          <p className="text-xs text-slate-400">尚未保存自定义 Provider，默认将使用免 Key 离线演示模式。</p>
        ) : (
          <div className="space-y-2.5">
            {configured.map((p) => (
              <div key={p.id} className="border border-slate-200 rounded-xl p-4 flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
                <div>
                  <div className="text-sm font-bold text-slate-900 flex items-center gap-2">
                    {p.name}
                    {p.is_default && <span className="px-2 py-0.5 bg-brand-50 text-brand-600 rounded text-xs">默认</span>}
                  </div>
                  <div className="text-xs text-slate-500 font-mono mt-1">
                    {p.base_url || '(offline)'} · {p.default_model} · Key: {p.api_key_masked || '无'}
                  </div>
                  {testMsg[p.id] && <div className="text-xs mt-1 font-semibold">{testMsg[p.id]}</div>}
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => testProvider(p.id)} className="px-3 py-1.5 bg-brand-50 text-brand-600 rounded-lg text-xs font-bold flex items-center gap-1">
                    <Zap className="w-3.5 h-3.5" /> 测试连通性
                  </button>
                  <button onClick={() => removeProvider(p.id)} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg">
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
