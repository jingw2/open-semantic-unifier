import React, { useEffect, useState } from 'react'
import { Database, FileUp, Sparkles, CheckCircle2, Plug } from 'lucide-react'
import { apiFetch, DataSourceItem } from '../lib/api'

interface Props {
  projectId: string
  onRouteReady: (dsIds: string[], profile: any, routing: any) => void
}

export default function DataSourcesPage({ projectId, onRouteReady }: Props) {
  const [tab, setTab] = useState<'file' | 'db'>('file')
  const [sources, setSources] = useState<DataSourceItem[]>([])
  const [goal, setGoal] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  // DB form state
  const [dbType, setDbType] = useState('duckdb')
  const [database, setDatabase] = useState('')
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState('3306')
  const [user, setUser] = useState('root')
  const [password, setPassword] = useState('')
  const [uri, setUri] = useState('')
  const [customSql, setCustomSql] = useState('')
  const [tables, setTables] = useState<string[]>([])
  const [selectedTable, setSelectedTable] = useState('')

  const refreshSources = async () => {
    const list = await apiFetch<DataSourceItem[]>(`/api/v1/projects/${projectId}/datasources`)
    setSources(list)
  }

  useEffect(() => {
    refreshSources()
  }, [projectId])

  const handleUpload = async (files: FileList | null) => {
    if (!files || !files.length) return
    setBusy(true)
    setMsg('正在上传至对象存储并执行智能体检...')
    try {
      const fd = new FormData()
      Array.from(files).forEach((f) => fd.append('files', f))
      if (goal) fd.append('user_goal', goal)
      const res = await apiFetch(`/api/v1/projects/${projectId}/datasources/upload`, { method: 'POST', body: fd })
      await refreshSources()
      onRouteReady(res.datasource_ids, res.profile, res.routing)
    } catch (e: any) {
      setMsg('❌ ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleDemo = async (stage: string) => {
    setBusy(true)
    setMsg(`正在生成并加载 ${stage} 高仿真示例数据...`)
    try {
      const res = await apiFetch(`/api/v1/projects/${projectId}/datasources/demo/${stage}`, { method: 'POST' })
      await refreshSources()
      onRouteReady(res.datasource_ids, res.profile, res.routing)
    } catch (e: any) {
      setMsg('❌ ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleTestDb = async () => {
    setMsg('正在测试数据库连接...')
    try {
      const res = await apiFetch<{ tables: string[] }>('/api/v1/db/test', {
        method: 'POST',
        body: JSON.stringify({ db_type: dbType, database, host, port: Number(port) || null, user, password, uri: uri || null }),
      })
      setTables(res.tables)
      setSelectedTable(res.tables[0] || '')
      setMsg(`✅ 连接成功，发现 ${res.tables.length} 张表/集合（体检仅安全抽样前 5,000 行）`)
    } catch (e: any) {
      setMsg('❌ ' + e.message)
    }
  }

  const handleConnectDb = async () => {
    setBusy(true)
    setMsg('正在加密保存连接并抽样前 5,000 行体检...')
    try {
      const res = await apiFetch(`/api/v1/projects/${projectId}/datasources/db`, {
        method: 'POST',
        body: JSON.stringify({
          db_type: dbType,
          database,
          host,
          port: Number(port) || null,
          user,
          password,
          uri: uri || null,
          table_name: selectedTable,
          custom_sql: customSql,
          user_goal: goal || null,
        }),
      })
      await refreshSources()
      onRouteReady(res.datasource_ids, res.profile, res.routing)
    } catch (e: any) {
      setMsg('❌ ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const isFileDb = dbType === 'duckdb' || dbType === 'sqlite'

  return (
    <div className="space-y-5">
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
          <div>
            <h2 className="text-lg font-extrabold text-slate-900">数据源接入中心</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              支持本地文件（CSV / XLSX / Parquet / TSV / YAML）与 5 大企业数据库安全抽样接入，自动触发 Smart Router 路由推荐
            </p>
          </div>
          <select
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            className="text-xs border border-slate-200 rounded-lg px-3 py-2 bg-slate-50 font-semibold"
          >
            <option value="">✨ 自动智能判断路线</option>
            <option value="L1">指定目标：L1 小表清洗对齐</option>
            <option value="L2">指定目标：L2 跨库大表 + AI 认亲</option>
            <option value="L3">指定目标：L3 源头契约防幻觉</option>
            <option value="L4">指定目标：L4 统一指标口径</option>
          </select>
        </div>

        <div className="flex gap-2 border-b border-slate-200 pb-3 mb-4">
          <button
            onClick={() => setTab('file')}
            className={`px-3.5 py-1.5 rounded-lg text-xs font-bold flex items-center gap-1.5 ${
              tab === 'file' ? 'bg-brand-50 text-brand-600' : 'text-slate-500 hover:bg-slate-100'
            }`}
          >
            <FileUp className="w-4 h-4" /> 上传文件 (CSV/XLSX/Parquet/TSV/YAML)
          </button>
          <button
            onClick={() => setTab('db')}
            className={`px-3.5 py-1.5 rounded-lg text-xs font-bold flex items-center gap-1.5 ${
              tab === 'db' ? 'bg-brand-50 text-brand-600' : 'text-slate-500 hover:bg-slate-100'
            }`}
          >
            <Database className="w-4 h-4" /> 直连数据库 (DuckDB/SQLite/MySQL/PG/MongoDB)
          </button>
        </div>

        {tab === 'file' ? (
          <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
            <label className="md:col-span-3 border-2 border-dashed border-slate-300 hover:border-brand-600 hover:bg-brand-50/40 rounded-xl p-8 flex flex-col items-center justify-center cursor-pointer transition">
              <input
                type="file"
                multiple
                accept=".csv,.xlsx,.xls,.parquet,.tsv,.yaml,.yml"
                className="hidden"
                onChange={(e) => handleUpload(e.target.files)}
              />
              <FileUp className="w-8 h-8 text-brand-600 mb-2" />
              <div className="text-sm font-bold text-slate-800">点击或拖拽上传 1 个或多个文件</div>
              <div className="text-xs text-slate-500 mt-1">支持 .csv · .xlsx · .parquet · .tsv · .yaml（上传 2 表自动路由 L2）</div>
            </label>

            <div className="md:col-span-2 bg-slate-50 border border-slate-200 rounded-xl p-4 flex flex-col justify-between">
              <div>
                <div className="text-xs font-bold text-slate-800 flex items-center gap-1.5">
                  <Sparkles className="w-4 h-4 text-brand-600" /> 一键加载内置高仿真示例
                </div>
                <p className="text-xs text-slate-500 mt-1">秒级体验 L1–L4 完整企业语义统一实战数据</p>
              </div>
              <div className="grid grid-cols-2 gap-2 mt-3">
                {(['L1', 'L2', 'L3', 'L4'] as const).map((st) => (
                  <button
                    key={st}
                    disabled={busy}
                    onClick={() => handleDemo(st)}
                    className="px-3 py-2 bg-white border border-brand-200 text-brand-600 hover:bg-brand-50 rounded-lg text-xs font-bold"
                  >
                    加载 {st} 示例
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1">数据库类型</label>
                <select
                  value={dbType}
                  onChange={(e) => {
                    setDbType(e.target.value)
                    setPort(e.target.value === 'postgresql' ? '5432' : e.target.value === 'mongodb' ? '27017' : '3306')
                  }}
                  className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs"
                >
                  <option value="duckdb">DuckDB（免驱动）</option>
                  <option value="sqlite">SQLite（免驱动）</option>
                  <option value="mysql">MySQL</option>
                  <option value="postgresql">PostgreSQL</option>
                  <option value="mongodb">MongoDB</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1">
                  {isFileDb ? '数据库文件路径' : '数据库名'}
                </label>
                <input
                  value={database}
                  onChange={(e) => setDatabase(e.target.value)}
                  placeholder={isFileDb ? '/path/to/data.duckdb' : 'erp_prod'}
                  className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs"
                />
              </div>
              {!isFileDb && (
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1">主机 Host</label>
                  <input value={host} onChange={(e) => setHost(e.target.value)} className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs" />
                </div>
              )}
              {!isFileDb && (
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1">端口</label>
                  <input value={port} onChange={(e) => setPort(e.target.value)} className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs" />
                </div>
              )}
              {!isFileDb && dbType !== 'mongodb' && (
                <>
                  <div>
                    <label className="block text-xs font-semibold text-slate-600 mb-1">用户名</label>
                    <input value={user} onChange={(e) => setUser(e.target.value)} className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs" />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-600 mb-1">密码（Fernet 加密存库）</label>
                    <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs" />
                  </div>
                </>
              )}
              {dbType === 'mongodb' && (
                <div className="md:col-span-2">
                  <label className="block text-xs font-semibold text-slate-600 mb-1">MongoDB URI（可选）</label>
                  <input value={uri} onChange={(e) => setUri(e.target.value)} placeholder="mongodb://127.0.0.1:27017/" className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs" />
                </div>
              )}
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">可选：自定义只读 SELECT 过滤语句（大表增量抽取推荐）</label>
              <input
                value={customSql}
                onChange={(e) => setCustomSql(e.target.value)}
                placeholder="例如 SELECT * FROM orders WHERE created_at >= '2026-01-01'"
                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs font-mono"
              />
            </div>

            <div className="flex items-center gap-3 flex-wrap pt-1">
              <button onClick={handleTestDb} className="px-3.5 py-2 bg-brand-50 text-brand-600 border border-brand-200 rounded-lg text-xs font-bold flex items-center gap-1.5">
                <Plug className="w-3.5 h-3.5" /> 测试连接并拉表
              </button>
              {tables.length > 0 && (
                <select value={selectedTable} onChange={(e) => setSelectedTable(e.target.value)} className="border border-slate-200 rounded-lg px-3 py-2 text-xs">
                  {tables.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              )}
              {(selectedTable || customSql) && (
                <button disabled={busy} onClick={handleConnectDb} className="px-4 py-2 bg-brand-600 text-white rounded-lg text-xs font-bold">
                  安全抽样 5,000 行并体检
                </button>
              )}
            </div>
          </div>
        )}

        {msg && <div className="mt-3 text-xs font-semibold text-slate-600">{msg}</div>}
      </div>

      {/* 已接入数据源清单 */}
      <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <h3 className="text-sm font-extrabold text-slate-900 mb-3">本项目已接入数据源 ({sources.length})</h3>
        {sources.length === 0 ? (
          <p className="text-xs text-slate-400">暂无数据源，请先上传文件、连接数据库或加载内置示例。</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {sources.map((s) => (
              <div key={s.id} className="border border-slate-200 rounded-xl p-4 flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-bold text-slate-900 flex items-center gap-2">
                    <CheckCircle2 className="w-4 h-4 text-green-600" /> {s.name}
                  </div>
                  <div className="text-xs text-slate-500 mt-1">
                    类型：{s.source_type} · 行数：{s.profile?.total_rows?.toLocaleString() ?? 0} · 推荐路线：
                    <span className="font-bold text-brand-600 ml-1">{s.routing?.recommended_stage || 'L1'}</span>
                  </div>
                </div>
                <button
                  onClick={() => onRouteReady([s.id], s.profile, s.routing)}
                  className="px-3 py-1.5 bg-brand-50 text-brand-600 rounded-lg text-xs font-bold shrink-0"
                >
                  进入工作台 →
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
