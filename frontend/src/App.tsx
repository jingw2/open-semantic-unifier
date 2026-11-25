import React, { useEffect, useState } from 'react'
import { Bot, CheckSquare, Database, FileText, LogOut, Plus, Rocket, Route, Users, GitMerge } from 'lucide-react'
import { apiFetch, clearAuth, getStoredUser, getToken, ProjectItem, UserInfo } from './lib/api'
import LoginPage from './pages/LoginPage'
import DataSourcesPage from './pages/DataSourcesPage'
import WorkbenchPage from './pages/WorkbenchPage'
import ReviewQueuePage from './pages/ReviewQueuePage'
import ServingReleasePage from './pages/ServingReleasePage'
import ProvidersPage from './pages/ProvidersPage'
import TeamAdminPage from './pages/TeamAdminPage'
import UnificationPage from './pages/UnificationPage'
import ContractsPage from './pages/ContractsPage'

type NavKey = 'datasources' | 'workbench' | 'unification' | 'contracts' | 'reviews' | 'serving' | 'providers' | 'team'

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
    { key: 'workbench', label: '路由工作台', icon: <Route className="w-4 h-4" /> },
    { key: 'unification', label: '实体统一工作台', icon: <GitMerge className="w-4 h-4" /> },
    { key: 'contracts', label: '契约统一', icon: <FileText className="w-4 h-4" /> },
    { key: 'reviews', label: '人工复核队列', icon: <CheckSquare className="w-4 h-4" /> },
    { key: 'serving', label: '发布与对外 API', icon: <Rocket className="w-4 h-4" /> },
    { key: 'providers', label: '模型 Provider', icon: <Bot className="w-4 h-4" /> },
    { key: 'team', label: '团队与权限', icon: <Users className="w-4 h-4" /> },
  ]

  return (
    <div className="min-h-screen flex">
      {/* 左侧专业导航栏 */}
      <aside className="w-60 bg-[#fbfbfb] border-r border-neutral-200 flex flex-col justify-between p-3 shrink-0">
        <div>
          <div className="flex items-center gap-2.5 px-2 py-1.5 mb-5">
            <div className="w-7 h-7 rounded-md bg-neutral-900 text-white font-semibold flex items-center justify-center text-sm">
              零
            </div>
            <div>
              <div className="text-[13px] font-semibold text-neutral-900">零点未来</div>
              <div className="text-[11px] text-neutral-500">企业语义统一平台</div>
            </div>
          </div>

          <div className="mb-4 px-2">
            <div className="flex items-center justify-between text-[11px] font-medium text-neutral-500 mb-1.5">
              <span>当前项目</span>
              {user.role !== 'viewer' && (
                <button onClick={createProject} className="text-neutral-600 hover:text-neutral-900 flex items-center gap-0.5">
                  <Plus className="w-3 h-3" /> 新建
                </button>
              )}
            </div>
            <select
              value={activeProject?.id || ''}
              onChange={(e) => setActiveProject(projects.find((x) => x.id === e.target.value) || null)}
              className="w-full border border-neutral-300 rounded-md px-2 py-1.5 text-xs bg-white"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          <nav className="space-y-0.5">
            {navItems.map((item) => (
              <button
                key={item.key}
                onClick={() => setNav(item.key)}
                className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-md text-[13px] transition ${
                  nav === item.key ? 'bg-neutral-200/70 text-neutral-900 font-medium' : 'text-neutral-600 hover:bg-neutral-200/50'
                }`}
              >
                {item.icon} {item.label}
              </button>
            ))}
          </nav>
        </div>

        <div className="pt-3 border-t border-neutral-200 px-2">
          <div className="text-[13px] font-medium text-neutral-800 truncate">{user.display_name}</div>
          <div className="text-[11px] text-neutral-500 truncate">{user.email}</div>
          <div className="text-[11px] text-neutral-400 truncate">{user.role}</div>
          <button
            onClick={() => { clearAuth(); setUser(null) }}
            className="mt-2 w-full py-1.5 border border-neutral-200 rounded-md text-xs text-neutral-600 hover:bg-white hover:text-red-600 flex items-center justify-center gap-1"
          >
            <LogOut className="w-3.5 h-3.5" /> 退出登录
          </button>
        </div>
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 bg-[#f7f7f8] p-6 md:p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          {!activeProject ? (
            <div className="text-sm text-neutral-500">正在加载项目...</div>
          ) : nav === 'datasources' ? (
            <DataSourcesPage
              projectId={activeProject.id}
              onRouteReady={(dsIds, profile, routing) => {
                setActiveDsIds(dsIds)
                setActiveProfile(profile)
                setActiveRouting(routing)
                setNav(routing?.recommended_stage === 'L3' ? 'contracts' : 'workbench')
              }}
            />
          ) : nav === 'workbench' ? (
            <WorkbenchPage
              projectId={activeProject.id}
              activeDsIds={activeDsIds}
              activeProfile={activeProfile}
              activeRouting={activeRouting}
              onGoDataSources={() => setNav('datasources')}
              onGoContracts={() => setNav('contracts')}
            />
          ) : nav === 'unification' ? (
            <UnificationPage projectId={activeProject.id} canEdit={user.role !== 'viewer'} />
          ) : nav === 'contracts' ? (
            <ContractsPage projectId={activeProject.id} canEdit={user.role !== 'viewer'} />
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
