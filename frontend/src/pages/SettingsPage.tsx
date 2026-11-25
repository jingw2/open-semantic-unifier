import React, { useState } from 'react'
import { Bot, KeyRound, Users } from 'lucide-react'
import { ProjectItem, UserInfo } from '../lib/api'
import { SettingsTab } from '../lib/router'
import ProvidersPage from './ProvidersPage'
import TeamAdminPage from './TeamAdminPage'
import ApiKeysPanel from '../components/ApiKeysPanel'

interface Props {
  project: ProjectItem
  currentUser: UserInfo
  canEdit: boolean
  initialTab?: SettingsTab
}

const TABS: Array<{ key: SettingsTab; label: string; icon: React.ReactNode }> = [
  { key: 'providers', label: '模型 Provider', icon: <Bot className="h-4 w-4" /> },
  { key: 'team', label: '团队', icon: <Users className="h-4 w-4" /> },
  { key: 'api-keys', label: 'API Key', icon: <KeyRound className="h-4 w-4" /> },
]

export default function SettingsPage({ project, currentUser, canEdit, initialTab }: Props) {
  const [tab, setTab] = useState<SettingsTab>(initialTab || 'providers')

  return (
    <div className="space-y-5">
      <header className="rounded-xl border border-neutral-200 bg-white p-6">
        <h1 className="text-xl font-semibold text-neutral-900">设置</h1>
        <p className="mt-1.5 text-sm text-neutral-500">模型、团队和对外密钥。</p>
      </header>

      <nav className="flex gap-1 rounded-xl border border-neutral-200 bg-white p-1">
        {TABS.map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key)}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium ${
              tab === item.key ? 'bg-neutral-200/70 text-neutral-900' : 'text-neutral-600 hover:bg-neutral-50'
            }`}
          >
            {item.icon} {item.label}
          </button>
        ))}
      </nav>

      {tab === 'providers' ? (
        <ProvidersPage />
      ) : tab === 'team' ? (
        <TeamAdminPage currentUser={currentUser} />
      ) : (
        <section className="rounded-xl border border-neutral-200 bg-white p-6">
          {canEdit ? <ApiKeysPanel projectId={project.id} /> : <p className="text-xs text-neutral-500">查看者只读，无法管理 API Key。</p>}
        </section>
      )}
    </div>
  )
}
