import { ArtifactDownload } from '../components/ArtifactAccess'
import React, { useEffect, useState } from 'react'
import { Rocket, Download, RotateCcw } from 'lucide-react'
import { apiFetch, ProjectItem } from '../lib/api'
import ResultViewer, { ResultViewerSource } from '../components/ResultViewer'

interface Props {
  project: ProjectItem
}

export default function ServingReleasePage({ project }: Props) {
  const [artifacts, setArtifacts] = useState<any[]>([])
  const [viewerSource, setViewerSource] = useState<ResultViewerSource | null>(null)
  const load = async () => {
    setArtifacts(await apiFetch<any[]>(`/api/v1/projects/${project.id}/artifacts`))
  }

  useEffect(() => {
    load()
  }, [project.id])

  const publish = async (artId: string) => {
    await apiFetch(`/api/v1/artifacts/${artId}/publish`, { method: 'POST' })
    await load()
  }


  const releasable = artifacts.filter((a) => a.artifact_type === 'l3_contract' || a.artifact_type === 'l4_metrics')

  return (
    <div className="space-y-5">
      {/* 草稿 / 发布 / 回滚版本中心 */}
      <div className="bg-white border border-neutral-200 rounded-xl p-6 ">
        <h2 className="text-lg font-semibold text-neutral-900 flex items-center gap-2">
          <Rocket className="w-5 h-5 text-neutral-500" /> 发布中心
        </h2>
        <p className="text-xs text-neutral-500 mt-1 mb-4">
          每次运行会保存一个不可变版本（默认 DRAFT）。只有点击“发布到线上 API”才会切换对外生效版本，可随时回滚。
        </p>

        {releasable.length === 0 ? (
          <p className="text-xs text-neutral-500">还没有已生成的版本。先发布 L3 契约或运行 L4 指标。</p>
        ) : (
          <div className="overflow-x-auto border border-neutral-200 rounded-xl">
            <table className="w-full text-xs text-left">
              <thead className="bg-neutral-50 text-neutral-600">
                <tr>
                  <th className="p-3">类型</th>
                  <th className="p-3">版本</th>
                  <th className="p-3">文件名</th>
                  <th className="p-3">发布状态</th>
                  <th className="p-3">操作</th>
                </tr>
              </thead>
              <tbody>
                {releasable.map((a) => (
                  <tr key={a.id} className="border-t border-neutral-100">
                    <td className="p-3 font-medium">{a.artifact_type === 'l3_contract' ? 'L3 校验契约' : 'L4 指标说明书'}</td>
                    <td className="p-3 font-mono font-medium text-brand-600">v{a.version_no}</td>
                    <td className="p-3">{a.filename}</td>
                    <td className="p-3">
                      <span
                        className={`px-2 py-0.5 rounded font-medium ${
                          a.release_status === 'PUBLISHED'
                            ? 'bg-green-50 text-green-700'
                            : a.release_status === 'ARCHIVED'
                              ? 'bg-neutral-100 text-neutral-500'
                              : 'bg-amber-50 text-amber-700'
                        }`}
                      >
                        {a.release_status}
                      </span>
                    </td>
                    <td className="p-3 flex items-center gap-2">
                      {a.release_status !== 'PUBLISHED' && (
                        <button
                          onClick={() => publish(a.id)}
                          className="px-2.5 py-1 bg-brand-600 text-white rounded-md font-medium flex items-center gap-1"
                        >
                          {a.release_status === 'ARCHIVED' ? <RotateCcw className="w-3 h-3" /> : <Rocket className="w-3 h-3" />}
                          {a.release_status === 'ARCHIVED' ? '回滚到此版本' : '发布到线上 API'}
                        </button>
                      )}
                      <div className="flex flex-wrap gap-1">
                        <button onClick={() => setViewerSource({ kind: 'artifact', id: a.id, filename: a.filename })} className="px-2.5 py-1 border border-neutral-200 rounded-md font-medium flex items-center gap-1">
                          查看
                        </button>
                        <ArtifactDownload id={a.id} filename={a.filename} className="px-2.5 py-1 border border-neutral-200 rounded-md font-semibold flex items-center gap-1">
                          <Download className="w-3 h-3" /> 下载
                        </ArtifactDownload>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ResultViewer source={viewerSource} onClose={() => setViewerSource(null)} />
    </div>
  )
}
