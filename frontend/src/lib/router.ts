export type TaskType = 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY'
export type NewTaskKind = 'clean' | 'match' | 'metrics' | 'contract' | 'entity'
export type SettingsTab = 'providers' | 'team' | 'api-keys'

export type Route =
  | { view: 'data' }
  | { view: 'tasks' }
  | { view: 'tasks-new'; kind: NewTaskKind }
  | { view: 'tasks-job'; jobId: string }
  | { view: 'tasks-run'; runId: string }
  | { view: 'tasks-contract'; versionId: string }
  | { view: 'tasks-publish' }
  | { view: 'reviews' }
  | { view: 'settings'; tab: SettingsTab }

const NEW_KINDS = new Set<NewTaskKind>(['clean', 'match', 'metrics', 'contract', 'entity'])
const SETTINGS_TABS = new Set<SettingsTab>(['providers', 'team', 'api-keys'])

export function parseRoute(hash: string): Route | null {
  const raw = (hash || '').replace(/^#\/?/, '')
  const parts = raw.split('/').filter(Boolean)
  if (parts.length === 0) return { view: 'data' }
  if (parts[0] === 'data' && parts.length === 1) return { view: 'data' }
  if (parts[0] === 'reviews' && parts.length === 1) return { view: 'reviews' }
  if (parts[0] === 'tasks') {
    if (parts.length === 1) return { view: 'tasks' }
    if (parts[1] === 'publish' && parts.length === 2) return { view: 'tasks-publish' }
    if (parts[1] === 'new' && parts.length === 3 && NEW_KINDS.has(parts[2] as NewTaskKind)) {
      return { view: 'tasks-new', kind: parts[2] as NewTaskKind }
    }
    if (parts[1] === 'job' && parts.length === 3) return { view: 'tasks-job', jobId: parts[2] }
    if (parts[1] === 'run' && parts.length === 3) return { view: 'tasks-run', runId: parts[2] }
    if (parts[1] === 'contract' && parts.length === 3) return { view: 'tasks-contract', versionId: parts[2] }
  }
  if (parts[0] === 'settings') {
    const tab = (parts[1] || 'providers') as SettingsTab
    if (SETTINGS_TABS.has(tab) && parts.length <= 2) return { view: 'settings', tab }
  }
  return null
}

export function routeHash(route: Route): string {
  switch (route.view) {
    case 'data': return '#/data'
    case 'tasks': return '#/tasks'
    case 'tasks-new': return `#/tasks/new/${route.kind}`
    case 'tasks-job': return `#/tasks/job/${route.jobId}`
    case 'tasks-run': return `#/tasks/run/${route.runId}`
    case 'tasks-contract': return `#/tasks/contract/${route.versionId}`
    case 'tasks-publish': return '#/tasks/publish'
    case 'reviews': return '#/reviews'
    case 'settings': return `#/settings/${route.tab}`
  }
}

export function routeNav(route: Route): 'data' | 'tasks' | 'reviews' | 'settings' {
  if (route.view === 'data') return 'data'
  if (route.view === 'reviews') return 'reviews'
  if (route.view === 'settings') return 'settings'
  return 'tasks'
}
