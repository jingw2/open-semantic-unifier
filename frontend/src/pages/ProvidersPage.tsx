import React, { useEffect, useState } from 'react'
import { Bot, Plus, Trash2, Zap } from 'lucide-react'
import { apiFetch } from '../lib/api'

type ProviderTestResult = {
  status?: string
  message?: string
  verdict?: string
  confidence?: number | null
  model?: string
  provider_kind?: string
  prompt_version?: string
  code?: string
  retryable?: boolean
}

const PROVIDER_KIND_OPTIONS = [
  { value: 'openai_compatible', label: 'OpenAI 兼容（通用 LLM）' },
  { value: 'perplexity_decisions', label: 'Perplexity Decisions（决策模型）' },
  { value: 'typesafe_jev', label: 'TypeSafe Jev（决策模型）' },
]

export default function ProvidersPage() {
  const [presets, setPresets] = useState<any[]>([])
  const [configured, setConfigured] = useState<any[]>([])
  const [providerKey, setProviderKey] = useState('deepseek')
  const [name, setName] = useState('DeepSeek 生产默认')
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com/v1')
  const [model, setModel] = useState('deepseek-chat')
  const [apiKey, setApiKey] = useState('')
  const [isDefault, setIsDefault] = useState(true)
  const [providerKind, setProviderKind] = useState('openai_compatible')
  const [sendFields, setSendFields] = useState('')
  const [timeoutSeconds, setTimeoutSeconds] = useState('')
  const [maxRetries, setMaxRetries] = useState('0')
  const [promptVersion, setPromptVersion] = useState('entity-pair-choice-v1')
  const [editingId, setEditingId] = useState<string | null>(null)
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
    setProviderKind(p.provider_kind || 'openai_compatible')
  }

  const changeKind = (kind: string) => {
    setProviderKind(kind)
    if (kind === 'perplexity_decisions') {
      setBaseUrl('https://api.perplexity.ai')
      setModel('pplx-decider-flash-latest')
    } else if (kind === 'typesafe_jev') {
      setBaseUrl('https://api.typesafe.ai')
      setModel('jev-latest')
    }
  }

  const resetForm = () => {
    setEditingId(null)
    setApiKey('')
    setSendFields('')
  }

  const startEdit = (p: any) => {
    setEditingId(p.id)
    setProviderKey(p.provider_key)
    setName(p.name)
    setBaseUrl(p.base_url || '')
    setModel(p.default_model || '')
    setProviderKind(p.provider_kind || 'openai_compatible')
    setSendFields(Array.isArray(p.send_fields) ? p.send_fields.join(', ') : '')
    setTimeoutSeconds(p.timeout_seconds != null ? String(p.timeout_seconds) : '')
    setMaxRetries(String(p.max_retries ?? 0))
    setPromptVersion(p.prompt_version || 'entity-pair-choice-v1')
    setIsDefault(!!p.is_default)
    setApiKey('')
  }

  const saveProvider = async (e: React.FormEvent) => {
    e.preventDefault()
    await apiFetch(editingId ? `/api/v1/providers/${editingId}` : '/api/v1/providers', {
      method: editingId ? 'PUT' : 'POST',
      body: JSON.stringify({
        provider_key: providerKey,
        name,
        base_url: baseUrl,
        default_model: model,
        api_key: apiKey,
        is_custom: !presets.some((x) => x.provider_key === providerKey),
        is_default: isDefault,
        provider_kind: providerKind,
        send_fields: sendFields.split(',').map((s) => s.trim()).filter(Boolean),
        timeout_seconds: timeoutSeconds.trim() ? Number(timeoutSeconds) : null,
        max_retries: maxRetries.trim() ? Number(maxRetries) : 0,
        prompt_version: promptVersion.trim() || 'entity-pair-choice-v1',
      }),
    })
    resetForm()
    await load()
  }

  const testProvider = async (id: string) => {
    setTestMsg((prev) => ({ ...prev, [id]: '测试中...' }))
    try {
      const res = await apiFetch<ProviderTestResult>(`/api/v1/providers/${id}/test`, { method: 'POST' })
      if (res.verdict) {
        setTestMsg((prev) => ({ ...prev, [id]: `${res.provider_kind || ''} → ${res.verdict}（置信度 ${res.confidence ?? '—'}）· 模型 ${res.model || '—'}` }))
      } else if (res.status === 'error' || res.code) {
        setTestMsg((prev) => ({ ...prev, [id]: `错误 ${res.code || 'error'}：${res.message}` }))
      } else {
        setTestMsg((prev) => ({ ...prev, [id]: res.message || '无结果' }))
      }
    } catch (err) {
      setTestMsg((prev) => ({ ...prev, [id]: `错误：${err instanceof Error ? err.message : '测试失败'}` }))
    }
  }

  const removeProvider = async (id: string) => {
    await apiFetch(`/api/v1/providers/${id}`, { method: 'DELETE' })
    await load()
  }

  return (
    <div className="space-y-5">
      <div className="bg-white border border-neutral-200 rounded-xl p-6 ">
        <h2 className="text-lg font-semibold text-neutral-900 flex items-center gap-2">
          <Bot className="w-5 h-5 text-neutral-500" /> 模型 Provider
        </h2>
        <p className="text-xs text-neutral-500 mt-1 mb-4">
          配置 OpenAI 兼容模型或决策模型。凭据加密存储，接口只返回掩码。
        </p>

        <div className="flex flex-wrap gap-2 mb-4">
          {presets.map((p) => (
            <button
              key={p.provider_key}
              type="button"
              onClick={() => applyPreset(p)}
              className="px-3 py-1.5 bg-neutral-50 hover:bg-brand-50 border border-neutral-200 hover:border-brand-200 rounded-lg text-xs font-medium text-neutral-700"
            >
              {p.name}
            </button>
          ))}
        </div>

        <form onSubmit={saveProvider} className="grid grid-cols-1 md:grid-cols-2 gap-3 bg-neutral-50 p-4 rounded-xl border border-neutral-200">
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">Provider 标识（自定义可填如 siliconflow）</span>
            <input value={providerKey} onChange={(e) => setProviderKey(e.target.value)} required className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">显示名称</span>
            <input value={name} onChange={(e) => setName(e.target.value)} required className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">Provider 类型</span>
            <select value={providerKind} onChange={(e) => changeKind(e.target.value)} className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal">
              {PROVIDER_KIND_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">默认模型名称</span>
            <input value={model} onChange={(e) => setModel(e.target.value)} required className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">Base URL</span>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.deepseek.com/v1" className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">发送字段（决策模型必填）</span>
            <input value={sendFields} onChange={(e) => setSendFields(e.target.value)} required={providerKind !== 'openai_compatible'} placeholder="name, tax_id" className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">超时（秒）</span>
            <input type="number" min="0" step="0.5" value={timeoutSeconds} onChange={(e) => setTimeoutSeconds(e.target.value)} placeholder="留空使用服务端默认" className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600">
            <span className="block mb-1">最大重试次数</span>
            <input type="number" min="0" step="1" value={maxRetries} onChange={(e) => setMaxRetries(e.target.value)} className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600 md:col-span-2">
            <span className="block mb-1">提示词版本</span>
            <input value={promptVersion} onChange={(e) => setPromptVersion(e.target.value)} className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-mono" />
          </label>
          <label className="block text-xs font-semibold text-neutral-600 md:col-span-2">
            <span className="block mb-1">API Key（加密存储；编辑时留空保持原 Key）</span>
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-...（离线模式或 Ollama 可留空）" className="w-full border border-neutral-200 rounded-lg px-3 py-2 text-xs bg-white font-normal" />
          </label>
          <div className="md:col-span-2 flex flex-wrap items-center justify-between gap-2 pt-1">
            <label className="flex items-center gap-2 text-xs font-semibold text-neutral-700">
              <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /> 设为工作空间默认模型
            </label>
            <div className="flex items-center gap-2">
              {editingId && (
                <button type="button" onClick={resetForm} className="px-4 py-2 bg-neutral-100 text-neutral-600 rounded-lg text-xs font-medium">取消编辑</button>
              )}
              <button type="submit" className="px-4 py-2 bg-brand-600 text-white rounded-lg text-xs font-medium flex items-center gap-1.5">
                <Plus className="w-4 h-4" /> {editingId ? '更新 Provider' : '保存并加密入库'}
              </button>
            </div>
          </div>
        </form>
      </div>

      <div className="bg-white border border-neutral-200 rounded-xl p-6 ">
        <h3 className="text-sm font-semibold text-neutral-900 mb-3">已配置模型 Provider ({configured.length})</h3>
        {configured.length === 0 ? (
          <p className="text-xs text-neutral-400">尚未保存自定义 Provider，默认将使用免 Key 离线演示模式。</p>
        ) : (
          <div className="space-y-2.5">
            {configured.map((p) => (
              <div key={p.id} className="border border-neutral-200 rounded-xl p-4 flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
                <div>
                  <div className="text-sm font-medium text-neutral-900 flex items-center gap-2">
                    {p.name}
                    {p.is_default && <span className="px-2 py-0.5 bg-brand-50 text-brand-600 rounded text-xs">默认</span>}
                  </div>
                  <div className="text-xs text-neutral-500 font-mono mt-1">
                    {p.base_url || '(offline)'} · {p.default_model} · Key: {p.api_key_masked || '无'}
                  </div>
                  <div className="text-[11px] text-neutral-400 mt-1">
                    类型 {p.provider_kind || 'openai_compatible'} · 发送字段 {Array.isArray(p.send_fields) && p.send_fields.length ? p.send_fields.join(', ') : '—'} · 提示词版本 {p.prompt_version || '—'}
                  </div>
                  {['perplexity_decisions', 'typesafe_jev'].includes(p.provider_kind) && (!Array.isArray(p.send_fields) || p.send_fields.length === 0) && (
                    <div className="text-xs mt-1 font-semibold text-rose-600">缺少发送字段，运行会被拒绝。点“编辑”填写。</div>
                  )}
                  {testMsg[p.id] && <div className="text-xs mt-1 font-semibold">{testMsg[p.id]}</div>}
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => startEdit(p)} className="px-3 py-1.5 bg-neutral-50 text-neutral-600 rounded-lg text-xs font-medium">编辑</button>
                  <button onClick={() => testProvider(p.id)} className="px-3 py-1.5 bg-brand-50 text-brand-600 rounded-lg text-xs font-medium flex items-center gap-1">
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
