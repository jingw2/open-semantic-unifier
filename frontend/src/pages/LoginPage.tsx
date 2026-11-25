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
      <div className="w-full max-w-sm bg-white border border-neutral-200 rounded-xl p-7">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl bg-brand-600 text-white font-semibold flex items-center justify-center text-lg">
            零
          </div>
          <div>
            <h1 className="text-lg font-semibold text-neutral-900">零点未来 · 企业语义统一平台</h1>
            <p className="text-xs text-neutral-500">登录以继续</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-neutral-600 mb-1.5">企业邮箱</label>
            <div className="relative">
              <Mail className="w-4 h-4 text-neutral-400 absolute left-3 top-3" />
              <input
                type="email"
                aria-label="企业邮箱"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="w-full pl-9 pr-3 py-2 text-sm border border-neutral-300 rounded-lg bg-white focus:outline-none focus:border-neutral-900 focus:ring-1 focus:ring-neutral-900/10"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-600 mb-1.5">密码</label>
            <div className="relative">
              <Lock className="w-4 h-4 text-neutral-400 absolute left-3 top-3" />
              <input
                type="password"
                aria-label="密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="w-full pl-9 pr-3 py-2 text-sm border border-neutral-300 rounded-lg bg-white focus:outline-none focus:border-neutral-900 focus:ring-1 focus:ring-neutral-900/10"
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
            className="w-full py-2 bg-neutral-900 hover:bg-neutral-800 text-white font-medium text-sm rounded-lg transition disabled:opacity-50"
          >
            {loading ? '正在验证身份...' : '登录工作台'}
          </button>
        </form>

        <div className="mt-6 pt-4 border-t border-neutral-100 flex items-start gap-2 text-xs leading-5 text-neutral-500">
          <ShieldCheck className="w-4 h-4 text-neutral-400 shrink-0 mt-0.5" />
          <span>
            私有化部署默认关闭公开注册。使用初始管理员账号登录后，可在“团队与权限”中创建成员。
          </span>
        </div>
      </div>
    </div>
  )
}
