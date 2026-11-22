import React, { useState } from 'react'
import { Lock, Mail, ShieldCheck } from 'lucide-react'
import { apiFetch, setAuth, UserInfo } from '../lib/api'

interface Props {
  onSuccess: (user: UserInfo) => void
}

export default function LoginPage({ onSuccess }: Props) {
  const [email, setEmail] = useState('admin@lingdianweilai.ai')
  const [password, setPassword] = useState('Admin@2026')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await apiFetch<{ access_token: string; user: UserInfo }>('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      })
      setAuth(res.access_token, res.user)
      onSuccess(res.user)
    } catch (err: any) {
      setError(err.message || '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-2xl shadow-lg p-8">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl bg-brand-600 text-white font-extrabold flex items-center justify-center text-lg">
            零
          </div>
          <div>
            <h1 className="text-lg font-extrabold text-slate-900">零点未来 · 企业语义统一平台</h1>
            <p className="text-xs text-slate-500">Semantic Unifier Platform · 私有化团队版</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">企业邮箱</label>
            <div className="relative">
              <Mail className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="w-full pl-9 pr-3 py-2.5 text-sm border border-slate-200 rounded-xl focus:outline-none focus:border-brand-600"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">密码</label>
            <div className="relative">
              <Lock className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="w-full pl-9 pr-3 py-2.5 text-sm border border-slate-200 rounded-xl focus:outline-none focus:border-brand-600"
              />
            </div>
          </div>

          {error && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-bold text-sm rounded-xl transition disabled:opacity-50"
          >
            {loading ? '正在验证身份...' : '登录工作台'}
          </button>
        </form>

        <div className="mt-6 pt-4 border-t border-slate-100 flex items-start gap-2 text-xs text-slate-500">
          <ShieldCheck className="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
          <span>
            私有化部署默认关闭公开自助注册。首次启动可使用 <code className="bg-slate-100 px-1 rounded">.env</code> 中配置的初始超管账号登录，并在“团队与权限”中创建成员。
          </span>
        </div>
      </div>
    </div>
  )
}
