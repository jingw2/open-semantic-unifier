import React from 'react'
import { GitMerge } from 'lucide-react'
import ReviewQueuePage from './ReviewQueuePage'

interface Props {
  projectId: string
  onRoute: (hash: string) => void
}

export default function ReviewHomePage({ projectId, onRoute }: Props) {
  return (
    <div className="space-y-5">
      <header className="rounded-xl border border-neutral-200 bg-white p-6">
        <h1 className="text-xl font-semibold text-neutral-900">复核</h1>
        <p className="mt-1.5 text-sm text-neutral-500">L2 灰色样本和实体配对都在这里处理。</p>
      </header>

      <section className="flex flex-col gap-3 rounded-xl border border-neutral-200 bg-white p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <GitMerge className="mt-0.5 h-5 w-5 text-neutral-400" />
          <div>
            <div className="text-sm font-semibold text-neutral-900">实体配对</div>
            <p className="mt-1 text-xs text-neutral-500">查看为什么需要判断，对比两侧字段，然后确认同一实体或保持分离。</p>
          </div>
        </div>
        <button
          onClick={() => onRoute('#/tasks/new/entity')}
          className="shrink-0 rounded-lg bg-neutral-900 px-4 py-2 text-xs font-medium text-white hover:bg-neutral-800"
        >
          去实体工作台
        </button>
      </section>

      <ReviewQueuePage projectId={projectId} />
    </div>
  )
}
