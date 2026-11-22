import React, { useEffect, useState } from 'react'
import { Bot, CheckSquare, Database, LogOut, Plus, Rocket, Sparkles, Users } from 'lucide-react'
import { apiFetch, clearAuth, getStoredUser, getToken, ProjectItem, UserInfo } from './lib/api'
import LoginPage from './pages/LoginPage'
import DataSourcesPage from './pages/DataSourcesPage'
import WorkbenchPage from './pages/WorkbenchPage'
import ReviewQueuePage from './pages/ReviewQueuePage'
import ServingReleasePage from './pages/ServingReleasePage'
import ProvidersPage from './pages/ProvidersPage'
import TeamAdminPage from './pages/TeamAdminPage'

type NavKey = 'datasources' | 'workbench' | 'reviews' | 'serving' | 'providers' | 'team'

export default function App() {
  const [user, setUser] = useState<UserInfo | null>(() => (getToken() ? getStoredUser() : null))
  const [nav, setNav] = useState<NavKey>('datasources')
  const [projects, setProjects] = useState<ProjectItem[]>([])
  const [activeProject, setActiveProject] = useState<ProjectItem | null>(null)

  const [activeDsIds, setActiveDsIds] = useState<string[]>([])
  const [activeProfile, setActiveProfile] = useState<any>(null)
  const [activeRouting, setActiveRouting] = useState<any>(null)

  const loadProjects = async () => {
    const list = await apiFetch<ProjectItem[]>('/api/v1/projects')
    setProjects(list)
    if (list.length && !activeProject) setActiveProject(list[0])
  }

  useEffect(() => {
    if (user) loadProjects()
  }, [user])

  if (!user) {
    return <LoginPage onSuccess={(u) => setUser(u)} />
  }

  const createProject = async () => {
    const name = window.prompt('请输入新项目名称（例如：供应链主数据治理）')
    if (!name || !name.trim()) return
    const p = await apiFetch<ProjectItem>('/api/v1/projects', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim() }),
    })
    setProjects((prev) => [p, ...prev])
    setActiveProject(p)
  }

  const navItems: { key: NavKey; label: string; icon: React.ReactNode }[] = [
    { key: 'datasources', label: '数据源中心', icon: <Database className="w-4 h-4" /> },
    { key: 'workbench', label: '智能路由工作台', icon: <Sparkles className="w-4 h-4" /> },
    { key: 'reviews', label: '人工复核队列', icon: <CheckSquare className="w-4 h-4" /> },
    { key: 'serving', label: '发布与对外 API', icon: <Rocket className="w-4 h-4" /> },
    { key: 'providers', label: '模型 Provider', icon: <Bot className="w-4 h-4" /> },
    { key: 'team', label: '团队与权限', icon: <Users className="w-4 h-4" /> },
  ]

  return (
    <div className="min-h-screen flex">
      {/* 左侧专业导航栏 */}
      <aside className="w-64 bg-white border-r border-slate-200 flex flex-col justify-between p-4 shrink-0">
        <div>
          <div className="flex items-center gap-2.5 px-2 py-2 mb-4">
            <div className="w-8 h-8 rounded-lg bg-brand-600 text-white font-extrabold flex items-center justify-center">
              零
            </div>
            <div>
              <div className="text-sm font-extrabold text-slate-900">零点未来</div>
              <div className="text-[11px] text-slate-500">企业语义统一平台 v1.0</div>
            </div>
          </div>

          {/* 项目切换器 */}
          <div className="mb-4 px-1">
            <div className="flex items-center justify-between text-[11px] font-bold text-slate-400 mb-1">
              <span>当前项目</span>
              {user.role !== 'viewer' && (
                <button onClick={createProject} className="text-brand-600 hover:underline flex items-center gap-0.5">
                  <Plus className="w-3 h-3" /> 新建
                </button>
              )}
            </div>
            <select
              value={activeProject?.id || ''}
              onChange={(e) => setActiveProject(projects.find((x) => x.id === e.target.value) || null)}
              className="w-full border border-slate-200 rounded-lg px-2.5 py-2 text-xs font-bold bg-slate-50"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          <nav className="space-y-1">
            {navItems.map((item) => (
              <button
                key={item.key}
                onClick={() => setNav(item.key)}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-xs font-bold transition ${
                  nav === item.key ? 'bg-brand-50 text-brand-600' : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {item.icon} {item.label}
              </button>
            ))}
          </nav>
        </div>

        <div className="pt-4 border-t border-slate-100 px-2">
          <div className="text-xs font-bold text-slate-800">{user.display_name}</div>
          <div className="text-[11px] text-slate-500 truncate">{user.email} · {user.role}</div>
          <button
            onClick={() => { clearAuth(); setUser(null) }}
            className="mt-2.5 w-full py-1.5 border border-slate-200 rounded-lg text-xs font-semibold text-slate-600 hover:text-red-600 hover:border-red-200 flex items-center justify-center gap-1"
          >
            <LogOut className="w-3.5 h-3.5" /> 退出登录
          </button>
        </div>
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 p-6 md:p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          {!activeProject ? (
            <div className="text-sm text-slate-500">正在加载项目...</div>
          ) : nav === 'datasources' ? (
            <DataSourcesPage
              projectId={activeProject.id}
              onRouteReady={(dsIds, profile, routing) => {
                setActiveDsIds(dsIds)
                setActiveProfile(profile)
                setActiveRouting(routing)
                setNav('workbench')
              }}
            />
          ) : nav === 'workbench' ? (
            <WorkbenchPage
              projectId={activeProject.id}
              activeDsIds={activeDsIds}
              activeProfile={activeProfile}
              activeRouting={activeRouting}
              onGoDataSources={() => setNav('datasources')}
            />
          ) : nav === 'reviews' ? (
            <ReviewQueuePage projectId={activeProject.id} />
          ) : nav === 'serving' ? (
            <ServingReleasePage project={activeProject} />
          ) : nav === 'providers' ? (
            <ProvidersPage />
          ) : (
            <TeamAdminPage currentUser={user} />
          )}
        </div>
      </main>
    </div>
  )
}
