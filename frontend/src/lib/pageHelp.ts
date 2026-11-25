const HELP: Record<string, string> = {
  data: '数据页用于上传文件或连接数据库。系统会给出一条执行建议，右侧助手可以替你直接开始。',
  tasks: '任务页汇总清洗、匹配、契约和实体统一任务。点一个入口就能开始，已有任务会出现在下面。',
  reviews: '复核页处理 L2 灰色样本和实体配对。每条会说明为什么需要判断，并给出字段对比。',
  settings: '设置页管理模型 Provider、团队和对外 API Key。',
  'tasks-new': '这是新建任务页。助手可以替你选数据、跑任务并带你去看结果。',
  'tasks-job': '这是任务详情。运行结果和产物都在这里，右侧助手可以解释下一步。',
  'tasks-run': '这是实体统一运行详情。复核项会说明为什么需要判断。',
  'tasks-contract': '这是契约版本详情。可以查看模型、导出或发布。',
  'tasks-publish': '这是产物与发布中心。发布和回滚都在这里。',
}

export function explainPage(key: string): string {
  return HELP[key] || HELP.tasks
}
