import React, { useEffect, useState } from 'react'
import { CheckSquare, Database, ListChecks, LogOut, Plus, Settings as SettingsIcon } from 'lucide-react'
import { apiFetch, clearAuth, getStoredUser, getToken, ProjectItem, UserInfo } from './lib/api'
import { parseRoute, Route, routeNav } from './lib/router'
import LoginPage from './pages/LoginPage'
import DataSourcesPage from './pages/DataSourcesPage'
import TasksPage from './pages/TasksPage'
import TaskDetailPage from './pages/TaskDetailPage'
import SettingsPage from './pages/SettingsPage'
import ReviewHomePage from './pages/ReviewHomePage'
import ServingReleasePage from './pages/ServingReleasePage'

type NavKey = 'data' | 'tasks' | 'reviews' | 'settings'

function initialRoute(): Route {
  return parseRoute(window.location.hash) || { view: 'data' }
}

export default function App() {
  const [user, setUser] = useState<UserInfo | null>(() => (getToken() ? getStoredUser() : null))
  const [route, setRoute] = useState<Route>(initialRoute)
  const [projects, setProjects] = useState<ProjectItem[]>([])
  const [activeProject, setActiveProject] = useState<ProjectItem | null>(null)

  const [activeDsIds, setActiveDsIds] = useState<string[]>([])
  const [activeProfile, setActiveProfile] = useState<any>(null)
  const [activeRouting, setActiveRouting] = useState<any>(null)

  const navigate = (hash: string) => {
    if (window.location.hash === hash) {
      setRoute(parseRoute(hash) || { view: 'data' })
    } else {
      window.location.hash = hash
    }
  }

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash) || { view: 'data' })
    window.addEventListener('hashchange', onHash)
    if (!window.location.hash) window.location.hash = '#/data'
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

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
    { key: 'data', label: '数据', icon: <Database className="h-4 w-4" /> },
    { key: 'tasks', label: '任务', icon: <ListChecks className="h-4 w-4" /> },
    { key: 'reviews', label: '复核', icon: <CheckSquare className="h-4 w-4" /> },
    { key: 'settings', label: '设置', icon: <SettingsIcon className="h-4 w-4" /> },
  ]

  const onNav = (key: NavKey) => {
    if (key === 'data') navigate('#/data')
    else if (key === 'tasks') navigate('#/tasks')
    else if (key === 'reviews') navigate('#/reviews')
    else navigate('#/settings/providers')
  }

  const onRouteReady = (dsIds: string[], profile: any, routing: any) => {
    setActiveDsIds(dsIds)
    setActiveProfile(profile)
    setActiveRouting(routing)
    const stage = routing?.method?.stage || routing?.recommended_stage
    if (stage === 'L3') navigate('#/tasks/new/contract')
    else if (stage === 'L2') navigate('#/tasks/new/match')
    else if (stage === 'L4') navigate('#/tasks/new/metrics')
    else navigate('#/tasks/new/clean')
  }

  const nav = routeNav(route)

  const renderContent = () => {
    if (!activeProject) return <div className="text-sm text-neutral-500">正在加载项目...</div>
    switch (route.view) {
      case 'data':
        return <DataSourcesPage projectId={activeProject.id} onRouteReady={onRouteReady} />
      case 'tasks':
        return <TasksPage projectId={activeProject.id} onRoute={navigate} />
      case 'tasks-new':
        return (
          <TaskDetailPage
            project={activeProject}
            canEdit={user.role !== 'viewer'}
            activeDsIds={activeDsIds}
            activeProfile={activeProfile}
            activeRouting={activeRouting}
            newKind={route.kind}
            onBack={() => navigate('#/tasks')}
            onGoDataSources={() => navigate('#/data')}
            onGoContracts={() => navigate('#/tasks/new/contract')}
          />
        )
      case 'tasks-job':
        return (
          <TaskDetailPage
            project={activeProject}
            canEdit={user.role !== 'viewer'}
            activeDsIds={activeDsIds}
            activeProfile={activeProfile}
            activeRouting={activeRouting}
            jobId={route.jobId}
            onBack={() => navigate('#/tasks')}
            onGoDataSources={() => navigate('#/data')}
            onGoContracts={() => navigate('#/tasks/new/contract')}
          />
        )
      case 'tasks-run':
        return (
          <TaskDetailPage
            project={activeProject}
            canEdit={user.role !== 'viewer'}
            activeDsIds={activeDsIds}
            activeProfile={activeProfile}
            activeRouting={activeRouting}
            runId={route.runId}
            onBack={() => navigate('#/tasks')}
            onGoDataSources={() => navigate('#/data')}
            onGoContracts={() => navigate('#/tasks/new/contract')}
          />
        )
      case 'tasks-contract':
        return (
          <TaskDetailPage
            project={activeProject}
            canEdit={user.role !== 'viewer'}
            activeDsIds={activeDsIds}
            activeProfile={activeProfile}
            activeRouting={activeRouting}
            versionId={route.versionId}
            onBack={() => navigate('#/tasks')}
            onGoDataSources={() => navigate('#/data')}
            onGoContracts={() => navigate('#/tasks/new/contract')}
          />
        )
      case 'tasks-publish':
        return <ServingReleasePage project={activeProject} />
      case 'reviews':
        return <ReviewHomePage projectId={activeProject.id} onRoute={navigate} />
      case 'settings':
        return (
          <SettingsPage
            project={activeProject}
            currentUser={user}
            canEdit={user.role !== 'viewer'}
            initialTab={route.tab}
          />
        )
    }
  }

  return (
    <div className="min-h-screen flex">
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
                onClick={() => onNav(item.key)}
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

      <main className="flex-1 bg-[#f7f7f8] p-6 md:p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">{renderContent()}</div>
      </main>
    </div>
  )
}
