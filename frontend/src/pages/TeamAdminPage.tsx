import React, { useEffect, useState } from 'react'
import { Users, UserPlus } from 'lucide-react'
import { apiFetch, UserInfo } from '../lib/api'

interface Props {
  currentUser: UserInfo
}

export default function TeamAdminPage({ currentUser }: Props) {
  const [members, setMembers] = useState<any[]>([])
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('Team@2026')
  const [role, setRole] = useState('editor')
  const [msg, setMsg] = useState('')

  const load = async () => {
    const list = await apiFetch<any[]>('/api/v1/auth/members')
    setMembers(list)
  }

  useEffect(() => {
    load()
  }, [])

  const createMember = async (e: React.FormEvent) => {
    e.preventDefault()
    setMsg('')
    try {
      await apiFetch('/api/v1/auth/members', {
        method: 'POST',
        body: JSON.stringify({ email, display_name: displayName, password, role }),
      })
      setEmail('')
      setDisplayName('')
      setMsg('✅ 团队成员创建成功')
      await load()
    } catch (err: any) {
      setMsg('❌ ' + err.message)
    }
  }

  const changeRole = async (memberId: string, newRole: string) => {
    await apiFetch(`/api/v1/auth/members/${memberId}`, {
      method: 'PATCH',
      body: JSON.stringify({ role: newRole }),
    })
    await load()
  }

  return (
    <div className="space-y-5">
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h2 className="text-lg font-extrabold text-slate-900 flex items-center gap-2">
          <Users className="w-5 h-5 text-brand-600" /> 团队与 RBAC 权限管理
        </h2>
        <p className="text-xs text-slate-500 mt-1 mb-4">
          私有化部署关闭自助注册，仅 Admin 管理员可创建成员与分配角色（Admin 全权 / Editor 运行与发布 / Viewer 只读）。
        </p>

        {currentUser.role === 'admin' && (
          <form onSubmit={createMember} className="grid grid-cols-1 md:grid-cols-4 gap-3 bg-slate-50 p-4 rounded-xl border border-slate-200 mb-5">
            <input value={email} onChange={(e) => setEmail(e.target.value)} required type="email" placeholder="成员邮箱" className="border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required placeholder="姓名" className="border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
            <input value={password} onChange={(e) => setPassword(e.target.value)} required placeholder="初始密码" className="border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white" />
            <div className="flex gap-2">
              <select value={role} onChange={(e) => setRole(e.target.value)} className="border border-slate-200 rounded-lg px-2 py-2 text-xs bg-white">
                <option value="editor">Editor</option>
                <option value="viewer">Viewer</option>
                <option value="admin">Admin</option>
              </select>
              <button type="submit" className="flex-1 bg-brand-600 text-white rounded-lg text-xs font-bold flex items-center justify-center gap-1">
                <UserPlus className="w-3.5 h-3.5" /> 创建
              </button>
            </div>
          </form>
        )}
        {msg && <div className="text-xs font-semibold mb-3">{msg}</div>}

        <div className="overflow-x-auto border border-slate-200 rounded-xl">
          <table className="w-full text-xs text-left">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="p-3">姓名</th>
                <th className="p-3">邮箱</th>
                <th className="p-3">角色</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.member_id} className="border-t border-slate-100">
                  <td className="p-3 font-bold">{m.display_name}</td>
                  <td className="p-3 text-slate-600">{m.email}</td>
                  <td className="p-3">
                    {currentUser.role === 'admin' ? (
                      <select
                        value={m.role}
                        onChange={(e) => changeRole(m.member_id, e.target.value)}
                        className="border border-slate-200 rounded-md px-2 py-1 text-xs font-bold"
                      >
                        <option value="admin">admin</option>
                        <option value="editor">editor</option>
                        <option value="viewer">viewer</option>
                      </select>
                    ) : (
                      <span className="px-2 py-0.5 bg-slate-100 rounded font-bold">{m.role}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
