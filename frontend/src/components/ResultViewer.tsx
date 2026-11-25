import React, { useEffect, useState } from 'react'
import { Download, X } from 'lucide-react'
import { getToken } from '../lib/api'
import { parseCsv } from '../lib/csv'

export type ResultViewerSource =
  | { kind: 'artifact'; id: string; filename: string }
  | { kind: 'contract'; projectId: string; versionId: string; filename: string }

interface Props {
  source: ResultViewerSource | null
  onClose: () => void
}

function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function ContractModel({ model, raw, showJson, onToggleJson }: { model: any; raw: string; showJson: boolean; onToggleJson: () => void }) {
  if (showJson) {
    return <pre className="overflow-auto rounded-lg bg-neutral-950 p-4 font-mono text-[11px] leading-5 text-neutral-100">{raw}</pre>
  }
  const classes = model?.classes || {}
  const enums = model?.enums || {}
  const relations = model?.relations || {}
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button onClick={onToggleJson} className="rounded-md border border-neutral-200 px-2.5 py-1.5 text-[11px] text-neutral-600">查看 JSON</button>
      </div>
      {Object.entries(classes).map(([name, details]: [string, any]) => (
        <section key={name} className="rounded-lg border border-neutral-200">
          <div className="border-b border-neutral-100 bg-neutral-50 px-3 py-2 text-xs font-semibold text-neutral-800">{name}</div>
          <table className="w-full text-left text-[11px]">
            <thead className="text-neutral-500"><tr><th className="p-2">属性</th><th className="p-2">类型</th><th className="p-2">必填</th><th className="p-2">标识</th></tr></thead>
            <tbody>
              {Object.entries(details?.attributes || {}).map(([attribute, info]: [string, any]) => (
                <tr key={attribute} className="border-t border-neutral-100">
                  <td className="p-2 font-mono text-neutral-700">{attribute}</td>
                  <td className="p-2 text-neutral-600">{info?.range || 'string'}</td>
                  <td className="p-2 text-neutral-600">{info?.required ? '是' : '否'}</td>
                  <td className="p-2 text-neutral-600">{info?.identifier ? '是' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
      {Object.keys(enums).length > 0 && (
        <section className="rounded-lg border border-neutral-200 p-3 text-xs">
          <div className="font-semibold text-neutral-800">枚举</div>
          {Object.entries(enums).map(([name, details]: [string, any]) => (
            <div key={name} className="mt-1 text-neutral-600">{name}: {(details?.values || []).join(', ')}</div>
          ))}
        </section>
      )}
      {Object.keys(relations).length > 0 && (
        <section className="rounded-lg border border-neutral-200 p-3 text-xs">
          <div className="font-semibold text-neutral-800">关系</div>
          {Object.entries(relations).map(([name, targets]: [string, any]) => (
            <div key={name} className="mt-1 text-neutral-600">{name}: {Array.isArray(targets) ? targets.join(', ') : String(targets)}</div>
          ))}
        </section>
      )}
    </div>
  )
}

export default function ResultViewer({ source, onClose }: Props) {
  const [text, setText] = useState('')
  const [model, setModel] = useState<any>(null)
  const [error, setError] = useState('')
  const [showJson, setShowJson] = useState(false)

  useEffect(() => {
    if (!source) return
    setText('')
    setModel(null)
    setError('')
    setShowJson(false)
    const token = getToken()
    const url = source.kind === 'artifact'
      ? `/api/v1/artifacts/${source.id}/content`
      : `/api/v1/projects/${source.projectId}/contracts/versions/${source.versionId}/export?kind=model`
    fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(async (response) => {
        if (!response.ok) throw new Error(`无法读取结果 (${response.status})`)
        const value = await response.text()
        setText(value)
        if (source.kind === 'contract') setModel(JSON.parse(value))
      })
      .catch((caught) => setError(caught instanceof Error ? caught.message : '读取失败'))
  }, [source])

  if (!source) return null
  const extension = source.filename.toLowerCase().split('.').pop() || ''
  const csv = extension === 'csv' ? parseCsv(text) : null

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30 p-4">
      <div className="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-neutral-200 bg-white">
        <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-3">
          <div className="text-sm font-semibold text-neutral-900">{source.filename}</div>
          <div className="flex items-center gap-2">
            <button onClick={() => downloadText(source.filename, text)} className="inline-flex items-center gap-1 rounded-md border border-neutral-200 px-2.5 py-1.5 text-[11px] text-neutral-600">
              <Download className="h-3.5 w-3.5" /> 下载
            </button>
            <button onClick={onClose} aria-label="关闭" className="rounded-md border border-neutral-200 p-1.5 text-neutral-500"><X className="h-4 w-4" /></button>
          </div>
        </header>
        <div className="overflow-auto p-4">
          {error ? (
            <p role="alert" className="text-xs text-red-600">{error}</p>
          ) : source.kind === 'contract' ? (
            model ? <ContractModel model={model} raw={text} showJson={showJson} onToggleJson={() => setShowJson((value) => !value)} /> : <p className="text-xs text-neutral-500">加载中...</p>
          ) : csv ? (
            <table className="w-full text-left text-xs">
              <thead className="bg-neutral-50 text-[10px] text-neutral-500">
                <tr>{csv.headers.map((header) => <th key={header} className="p-2">{header}</th>)}</tr>
              </thead>
              <tbody>
                {csv.rows.map((row, index) => (
                  <tr key={index} className="border-t border-neutral-100">
                    {row.map((cell, cellIndex) => <td key={cellIndex} className="p-2 text-neutral-700">{cell}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <pre className="overflow-auto rounded-lg bg-neutral-950 p-4 font-mono text-[11px] leading-5 text-neutral-100">{text}</pre>
          )}
        </div>
      </div>
    </div>
  )
}
