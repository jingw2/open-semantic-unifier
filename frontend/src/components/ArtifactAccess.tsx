import React, { useEffect, useState } from 'react'
import { getToken } from '../lib/api'

async function fetchArtifact(id: string): Promise<Blob> {
  const response = await fetch(`/api/v1/artifacts/${id}/content`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  })
  if (!response.ok) throw new Error(`无法读取产物 (${response.status})`)
  return response.blob()
}

export function ArtifactDownload({ id, filename, children, className }: {
  id: string; filename: string; children: React.ReactNode; className?: string
}) {
  const download = async () => {
    try {
      const url = URL.createObjectURL(await fetchArtifact(id))
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      alert(error instanceof Error ? error.message : '下载失败')
    }
  }
  return <button type="button" className={className} onClick={download}>{children}</button>
}

export function ArtifactPreview({ id, className }: { id: string; className?: string }) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let disposed = false
    let objectUrl = ''
    setUrl('')
    setError('')
    fetchArtifact(id).then(blob => {
      if (disposed) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch(error => {
      if (!disposed) setError(error instanceof Error ? error.message : '预览失败')
    })
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [id])
  if (error) return <p role="alert">{error}</p>
  return url ? <iframe title="产物预览" src={url} sandbox="allow-scripts" className={className} /> : <p>加载预览中…</p>
}
