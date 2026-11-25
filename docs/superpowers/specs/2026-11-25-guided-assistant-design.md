# 新手引导、复核可读性与 OpenRefine/W3C 改造设计

日期：2026-11-25  
状态：待最终确认  
范围：前端交互重构、复核解释、在线结果查看、方法推荐、OpenRefine 运行时与 W3C 标准数据输出。  
交付拆分为两个阶段：先 UX 层，再 OpenRefine/W3C 层。

## 1. 背景

五个问题指向同一个根因：界面把引擎、阶段、Provider、证据码和产物直接摆给用户，没有从“我要完成什么”出发。

- 左侧有 8 个一级菜单，新用户默认落在数据源页，没有“从这里开始”。
- 实体复核卡片展示 `CANDIDATE / STRONG_IDENTIFIER_MISSING` 和原始 JSON，没有业务语言解释，也没有字段并排对比。
- L1 清洗结果、L2 簇文件、L3 契约模型只能下载；L4 结果已经在线。
- 路由工作台只写“执行建议”，没有说明用途，也没有把方法选择讲清楚。
- 后端已有按数据特征推荐 L1/L2/L4 的路由器，但没有把它翻译成“OpenRefine + W3C”或“Splink + DuckDB”。

用户进一步确认：

- 要真正接入 OpenRefine runtime 和操作配方，不只做“风格”。
- 助手可以自由文本，但模型只做意图分类和解释，不替用户决策。
- 助手在一次确认后可以自动跑完整条链路，但不能自动合并、发布或替人裁决。
- 菜单要减少，只保留核心、需要用户查看配置和交互的入口。
- OpenRefine/W3C 属于后续交付，但必须写进后续计划。

## 2. 目标

1. 新用户进入后 30 秒内知道第一步；助手可以把他带到对应页面并自动完成一次任务。
2. 一级菜单从 8 个减少到 4 个：数据 / 任务 / 复核 / 设置。助手常驻，不占菜单。
3. 实体复核默认用人话说明“为什么需要你判断”，并给出字段对比、模型建议和选择后果。
4. L1、L2、L3 的主要结果可以在线查看，不必先下载。
5. 按数据特征推荐规则清洗/OpenRefine + W3C，或 Splink + DuckDB，并说明理由。
6. 第二阶段接入真实 OpenRefine 运行时、内置操作配方、清洗前后 diff，以及 LinkML + JSON Schema + JSON-LD 标准数据输出。

## 3. 非目标与反目标

非目标：

- 第一交付不接入 OpenRefine 运行时。第一交付的方法卡只能写“规则清洗 + W3C 契约”，不能把现有 L1 冒充成 OpenRefine。
- 第一交付不引入一等公民 Task 表，也不做数据库迁移。任务页聚合现有 jobs、unification runs、contract versions。
- 第一交付不支持用户上传 OpenRefine 配方；只用内置、版本化、只读配方。
- 第一交付不做 RDF/OWL 输出；W3C 输出限定为 LinkML + JSON Schema + JSON-LD。
- 不做自动合并、自动发布契约、自动发布快照、自动替人做复核决定。
- 不做真实客户数据的模型评估。

反目标：

- 一个只会聊天、不能把用户带到正确页面并完成任务的助手。
- 一个悄悄切换引擎、不解释理由的方法推荐。
- 一个自动改了数据却不给 diff 的清洗流程。
- 一个把原始 JSON 藏起来、只给结论不给证据的复核页。
- 一个减少菜单却把高级功能藏到用户找不到的地方。

## 4. 最终形态

```
┌──────────────────────────────────────────────┬──────────────┐
│ 数据 / 任务 / 复核 / 设置                      │ 助手          │
│                                              │ 你想做什么？  │
│  当前页面内容                                 │ [清洗名单]    │
│                                              │ [匹配客户]    │
│                                              │ 建议：...     │
│                                              │ [开始]        │
└──────────────────────────────────────────────┴──────────────┘
```

- 左侧一级菜单只有 4 个。助手是右侧常驻面板，首次登录默认展开，可折叠，移动端为抽屉。
- “任务”是新的一级页，聚合现有任务并提供新建入口。现有路由工作台、实体统一、契约统一、发布中心变成任务详情里的视图。
- “设置”收纳模型 Provider、团队、API 密钥、项目设置。
- 发布、回滚、导出属于任务结果的动作，放在任务详情，不放进设置。

## 5. 交付拆分

### 交付一：UX 层

- 4 菜单壳、任务聚合页、助手面板。
- 助手：确定性按钮 + 可选自由文本；模型只分类和解释；一次确认后自动跑 L1/L2/L4，L3 编译 DRAFT，实体匹配可运行但不裁决。
- 复核解释、自动分离解释区。
- L1/L2/L3 在线结果查看。
- 方法推荐先用“规则清洗 + W3C 契约”标签。
- 现有 Playwright 验收更新到新壳。

### 交付二：OpenRefine 与 W3C

- OpenRefine companion 服务、适配器、内置配方、清洗前后 diff。
- 配方自动选择、可见可换。
- 运行时不可用或数据过大时的降级。
- LinkML 契约 + JSON Schema + JSON-LD 实例导出。
- Docker 集成测试与文档。

## 6. 助手

### 6.1 交互

助手只做四件事：问目标、看数据、给方案、执行确认后的计划。

```
welcome
  你想完成什么？
  [清洗供应商名单] [跨系统匹配客户]
  [统一 GMV 口径] [生成数据契约]
  [解释当前页面]

planning
  已读取当前项目的数据。
  没有数据： [加载示例数据] [去上传]

plan
  建议方法：Splink + DuckDB
  理由：检测到 2 张表共 35,000 行，需要跨库匹配。
  接下来：
  ① 使用哪份数据  ② 运行  ③ 看结果
  [开始] [换一个方法]

conflict
  你选的是“清洗供应商名单”，但当前数据是 2 张共 35,000 行的大表，
  更适合 Splink + DuckDB。要按你的选择走 L1，还是改用推荐方法？
  [按我的选择走 L1] [改用 Splink + DuckDB]

running
  正在加载数据 → 创建任务 → 运行 → 读取结果
  [取消]

done
  已完成。清洗后 250 行，实体数 15。
  [查看结果] [去复核] [解释当前页面]
```

### 6.2 意图与模型边界

- 按钮任务不用模型。
- 自由文本先用确定性关键词匹配；未命中时调用后端助手接口，用工作区默认 Provider 做意图分类和解释。
- 模型只返回 `intent`、`confidence`、`explanation`、`suggested_task`，没有工具调用权，不能执行动作。
- 模型看到的是用户消息、当前页面、数据摘要（数量、字段名、方法标签），不包含记录字段值。
- Provider 未配置、调用失败或置信度低时，回退确定性按钮并请用户确认，不阻塞。
- 助手接口不写数据库，不新增审计表；只记录日志和耗时。

### 6.3 自动运行边界

一次确认后，助手可以：

- 选择现有数据源，或加载示例数据（按钮明示会新增一份）。
- 创建默认方案：实体类型来自数据，强标识取税号/GTIN/邮箱等可识别字段，候选规则取名称相似和精确标识，模型 Provider 使用当前默认值，失败策略 REVIEW。
- 提交并轮询 L1/L2/L4 任务，或实体统一运行。
- 为 L3 编译 DRAFT 版本，前提是已有契约源和实体快照。

助手不可以：

- 自动发布契约版本、自动发布快照、自动合并或分离实体、替人点复核。
- 静默覆盖用户已有方案或数据源。
- 在用户未确认前写入任何数据。

每一步显示状态，失败时停止并给出“去对应页面手动操作”的链接。用户可随时取消未开始的后续步骤。

### 6.4 任务冲突

用户选择的任务与数据画像推荐冲突时，展示两种选择，不静默覆盖。用户的选择优先。若用户选择的方法前置条件不满足（例如 L3 没有契约文件），助手说明缺什么并给出上传入口。

## 7. 复核

### 7.1 两层信息

默认层只给人话：

- 标题：“这两条记录是不是同一实体？”
- `why`：“名称等弱证据相似，但缺少可核对的强标识，无法自动确认是否同一实体。”
- 字段对比表：字段名、左侧、右侧、匹配状态。
- 模型建议：保留 `模型建议 · SAME` 原文，旁边加“倾向同一实体 / 倾向不同实体 / 不确定”。保留置信度、分布、模型版本、Provider 类型和 Key、耗时。
- 建议文案：“建议合并 / 建议分离 / 模型不确定”。不预选按钮。拿不准时提示“保持分离更安全”。
- 选择后果一句。
- 按钮保留“确认同一实体”“保持分离”。

展开层：

- “查看原始证据”默认折叠，显示证据码和原始 JSON。
- 技术字段名、trace、模型原始分布都放在这里。

### 7.2 后端响应

扩展 `GET /projects/{id}/unification/reviews`：

```json
{
  "items": [ ...待人工复核... ],
  "auto_separated": [ ...系统自动保持分离的解释... ],
  "total": 1,
  "auto_separated_total": 3,
  "run_id": "..."
}
```

每个待复核 item 增加：

```json
{
  "left": {
    "record_ref": "erp:suppliers:S-1001",
    "source_system": "erp",
    "dataset": "suppliers",
    "source_record_id": "S-1001",
    "attributes": {"name": "Acme Ltd", "tax_id": "TEST-TAX-001"}
  },
  "right": { "...": "..." },
  "comparison": [
    {"field": "name", "left": "Acme Ltd", "right": "ACME, Ltd.", "status": "MATCH"},
    {"field": "tax_id", "left": "TEST-TAX-001", "right": null, "status": "MISSING"}
  ],
  "why": "名称等弱证据相似，但缺少可核对的强标识，无法自动确认是否同一实体。",
  "suggestion": "建议分离",
  "consequence": "确认同一实体后，两条来源记录会合并到一个实体；保持分离则保留为两个实体。"
}
```

- `record_ref` 统一为 `<source_system>:<dataset>:<source_record_id>`，同时返回三个原始字段。
- `attributes` 只带方案 `field_map` 的规范字段子集，键用规范名，值保留原始值；状态判断用方案 normalizers 归一化后比较。
- `status` 只取 `MATCH`、`DIFFERENT`、`MISSING`。
- `why` 由纯函数根据证据码生成，便于单测。
- `suggestion` 由确定性规则生成：SAME 且强标识匹配 -> 建议合并；UNKNOWN 或弱证据 -> 建议分离；DIFFERENT -> 建议分离。

`auto_separated` 每个 item 包含：

- 左/右记录摘要、`comparison`、`why`、证据码。
- 只包含有明确原因的 DISTINCT 配对：`CONFLICT_CONSTRAINT`、`STRONG_IDENTIFIER_CONFLICT`、`human_cannot_link`、`TRANSITIVE_CONFLICT`。
- 最多 20 条；超过时显示总数和“在高级筛选里查看”。
- 不包含普通 AUTO_MATCH 或无关配对。

### 7.3 复核接口范围

- `items` 仍然只返回 `action == "REVIEW"` 的配对。
- 强标识冲突等 `DISTINCT` 配对进入 `auto_separated`，不混入主队列。
- 冲突类 `why` 分支用纯函数单测；接口测试覆盖可达的 REVIEW 分支和 `auto_separated` 的筛选上限。

### 7.4 L2 复核队列

- 每条显示“模型认为可能是同一实体（95%）”，附模型理由。
- 显示左右文本、相似度、推入原因：“L2 灰色边界样本”。
- 保留“同意合并 / 拒绝”和映射规则沉淀说明。

## 8. 在线结果查看

新增 `ResultViewer` 弹窗。所有结果入口加“查看”。

| 结果 | 在线渲染 |
| --- | --- |
| `.csv` | 前 200 行表格，显示列名和总行数，底部“下载完整文件” |
| `.json` / `.yaml` | 格式化代码块 |
| `.html` | iframe，复用 `ArtifactPreview` |
| `.sql` / `.txt` | 代码块 |
| 契约模型 | 类、属性、枚举、关系四块；可切换 JSON |
| JSON Schema | 格式化 JSON |
| JSON-LD | `@context` 和实例表格 |

接入位置：

- L1：`clean_csv`、`rules_json` 加“查看”。
- L2：`clusters_csv` 加“查看”；权重图和瀑布图保持现有预览。
- L3：版本历史每行加“查看模型”；第二交付加“查看 JSON-LD”。
- L4：指标产物加“查看”，结果行已在页面展示。
- 任务详情：所有产物统一入口。

实现约束：

- 读取现有 `/api/v1/artifacts/{id}/content`，前端按文件名解析。
- CSV 只做预览，支持引号转义；解析失败回退纯文本。
- 契约模型解析失败显示原始 JSON。
- 弹窗只读，下载仍是独立按钮。

## 9. 任务页与导航

### 9.1 一级菜单

```
数据    数据源、上传/连接、方法建议
任务    任务列表、新建任务、任务详情
复核    L2 复核、实体复核、自动分离解释
设置    Provider、团队、API 密钥、项目设置
```

助手常驻右侧，不占菜单。

### 9.2 任务聚合

第一版不新增 Task 表，前端聚合：

- `GET /projects/{id}/jobs`：L1/L2/L4 任务。
- `GET /projects/{id}/unification/runs`：实体统一运行。
- `GET /projects/{id}/contracts/versions`：契约版本。
- `GET /projects/{id}/artifacts`：发布与产物。

前端归一化为 `TaskItem`：

```ts
type TaskItem = {
  id: string
  type: 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY'
  title: string
  status: string
  createdAt: string
  reviewCount?: number
  detail: 'workbench' | 'contracts' | 'unification'
}
```

任务详情嵌入现有页面组件：

- L1/L2/L4 -> 路由工作台（改名为“处理方法”）。
- L3 -> 契约统一。
- ENTITY -> 实体统一工作台。
- 发布、导出、回滚留在详情页。

### 9.3 设置

- 模型 Provider。
- 团队与权限。
- 对外 Serving API Key。
- 项目设置（名称、默认方法偏好）。
- 发布动作不在这里。

### 9.4 兼容

- 旧一级入口移除后，现有 Playwright 验收改为从“任务”进入对应详情，或通过深链接进入。
- 现有页面组件保留，避免重写业务逻辑。
- 旧书签或外部链接通过路由参数落到对应任务详情。

## 10. 方法推荐与路由

### 10.1 方法对象

`recommend_pipeline` 增加：

```json
{
  "method": {
    "key": "rule_clean_w3c",
    "label": "规则清洗 + W3C 契约",
    "stage": "L1",
    "description": "先清洗小表，再用 LinkML 契约固化标准字段。",
    "reason": "检测到 1 张 250 行的小表，字段有空格和大小写噪声。",
    "available": true
  }
}
```

第一交付的 key：

| key | 触发条件 | 阶段 | 标签 |
| --- | --- | --- | --- |
| `metrics_yaml` | 指标 YAML，或用户指定 L4 | L4 | 指标 YAML |
| `contract_w3c` | Schemasheets/LinkML，或用户指定 L3 | L3 | LinkML W3C 契约 |
| `splink_duckdb` | 多表、总行数 ≥ 5 万、多维消歧键或有黑话 | L2 | Splink + DuckDB |
| `rule_clean_w3c` | 其他小表单表 | L1 | 规则清洗 + W3C 契约 |

第二交付：

- OpenRefine 运行时可用且数据在限制内时，`rule_clean_w3c` 升级为 `openrefine_w3c`，标签“OpenRefine + W3C 契约”。
- 运行时不可用或数据超过限制时，回到 `rule_clean_w3c`，并显示降级原因。
- 多表/大数据仍然走 `splink_duckdb`，不因为 OpenRefine 可用就改变路由。

### 10.2 展示位置

- 数据源卡片：“建议方法：Splink + DuckDB”。
- 处理方法页顶部方法卡：方法、理由、可展开的 `reasons`。
- 实体统一页顶部横幅：按用户选中或最新数据源的路由显示建议，可覆盖。
- 助手计划卡片。
- 任务详情标题旁。

### 10.3 实体统一横幅

- 小表单表：“当前数据适合先清洗并生成 W3C 契约。”[去清洗] [仍然直接匹配]
- 多表/大数据：“当前数据适合 Splink + DuckDB 跨库匹配。”[去运行 L2] [直接匹配]
- 推荐是建议，不静默切换引擎。

## 11. 交付二：OpenRefine 运行时与 W3C

### 11.1 服务

- 在 `docker-compose.yml` 增加 `openrefine` 服务，使用固定版本的官方镜像，内部网络访问，不对外暴露端口。
- 数据卷持久化 OpenRefine 项目；后端通过 `OPENREFINE_URL` 访问。
- 本地无 Docker 时，适配器健康检查失败，自动降级到规则清洗。
- 版本固定并记录镜像 digest，遵循仓库现有容器固定策略。

### 11.2 适配器

新增 `backend/app/services/openrefine/client.py`：

- `health()`
- `create_project(name, file_bytes, format)`
- `apply_recipe(project_id, operations)`
- `export_csv(project_id)`
- `delete_project(project_id)`

所有调用有超时、重试上限和错误映射；凭据不出现在日志。

### 11.3 配方

- 内置配方放在 `backend/app/services/openrefine/recipes/`，每个配方一个 JSON 文件。
- 字段：`id`、`name`、`description`、`version`、`match`、`limits`、`operations`（OpenRefine 操作 JSON）。
- 第一版配方类别：去空格/大小写归一、标点清理、重复值合并、列拆分/合并、值替换、名称聚类。
- 配方只读、版本化；用户可以从目录里换，但不能上传或编辑。
- 选择规则：`select_recipe(profile, user_goal)` 按数据画像匹配；多个匹配时按优先级取一个，并显示“换配方”。

### 11.4 执行与 diff

1. 从数据副本或样本创建 OpenRefine 项目。
2. 应用配方。
3. 导出清洗后 CSV。
4. 计算 diff：变更行数、变更单元格数、前 20 个 before/after 示例。
5. 在结果页显示 diff；用户点“采用”才创建新的数据源或产物，“撤销”删除临时项目。

不覆盖原始数据源。每次执行记录配方 ID、版本、变更统计和操作人。

### 11.5 降级

- OpenRefine 健康检查失败：使用规则清洗，显示“OpenRefine 不可用，已降级为规则清洗”。
- 数据超过限制（单表 > 5 万行、文件 > 10 MiB、列 > 256）：不送 OpenRefine；小表单表降级规则清洗，多表/大数据改走 Splink + DuckDB。
- 配方执行失败：保留原始数据，显示错误码和“去手动处理”。

### 11.6 W3C 输出

第一交付已有 LinkML 契约、JSON Schema 导出。第二交付增加：

1. OpenRefine 清洗后的 CSV。
2. LinkML 契约编译和校验。
3. JSON Schema 导出（已有）。
4. JSON-LD 实例导出：
   - 从契约的类/槽位和清洗后列名推断映射。
   - 展示映射预览，允许用户改类名、ID 槽和属性槽。
   - 生成 `@context` 和实例 JSON-LD。
   - 无契约时提示先上传/编译契约。

RDF/OWL 不在第一版范围；如果客户明确要求，单独评估。

## 12. API 与数据变更

- `recommend_pipeline` 新增 `method` 对象。
- `profile_items` 新增 LinkML YAML 识别 `has_linkml_contract`。
- `GET /unification/reviews` 返回 `items` 和 `auto_separated`，item 增加 `left`、`right`、`comparison`、`why`、`suggestion`、`consequence`。
- 新增助手接口：`POST /projects/{id}/assistant/plan`。请求只含用户消息、当前页面和（后端自建的）数据摘要；响应只含意图、置信度、解释和建议任务；不执行动作。
- 新增 OpenRefine 适配器和配方注册表；环境变量 `OPENREFINE_URL`、`OPENREFINE_ENABLED`。
- 新增 JSON-LD 实例导出接口。
- 第一交付无数据库迁移；第二交付只加必要的配方/映射记录，优先用文件存储，避免迁移。

## 13. 验收标准

### 交付一

- 新用户登录后助手默认展开；选“清洗供应商名单”，确认一次后自动完成 L1，并在线看到清洗表和变更摘要；从登录到看到结果不超过 2 次点击。
- 新用户能独立完成一次实体复核：看懂 `why`、字段对比、建议和后果，并做出决定；技术细节默认折叠。
- 自动分离解释区可展开，最多 20 条，带原因。
- L1、L2、L3 结果可在线查看，不需要下载。
- 4 个一级菜单可用；旧页面通过任务详情进入；现有 Playwright 验收更新后全部通过。
- 自由文本：“帮我把客户名单去重”在配置了 DeepSeek 时路由到 L2；Provider 不可用时回退确定性引导；请求不含记录字段值。

### 交付二

- OpenRefine 服务启动后，一个小表执行内置配方，显示 before/after diff；采用后生成新数据源，原数据不变。
- 运行时不可用时自动降级并提示；大数据自动走 Splink + DuckDB。
- LinkML 契约、JSON Schema、JSON-LD 实例均可在线查看和导出。
- Docker 集成测试通过；现有验收不回归。

## 14. 实施阶段

### 交付一

1. 4 菜单壳、任务聚合页、助手面板（确定性）、助手 LLM 接口、自动运行 L1/L2/L4、进度与结果；更新 Playwright。
2. 复核解释、自动分离解释区、L2 文案。
3. 在线结果查看器，接入 L1/L2/L3。
4. 方法标签、实体统一横幅、L3/实体任务详情。
5. Playwright 全量验收、真实数据回归、文档。

### 交付二

6. OpenRefine 服务、适配器、配方注册表、假服务测试。
7. diff 预览、采用/撤销、降级、路由接入。
8. JSON-LD 实例导出、映射预览、契约查看器。
9. Docker 集成测试、UI 收尾、文档。

每阶段单独测试、单独提交。按 pstack 的 feature / multi-phase 流程执行。

## 15. 风险与回滚

- 4 菜单重构可能破坏现有深链接和验收。保留旧页面组件，更新路由和 Playwright，提供深链接兼容。
- 助手自动运行会创建数据源和任务。一次确认、可取消、不覆盖已有数据、不做破坏性动作。
- 自由文本意图可能误判。低置信度回退按钮确认，模型不执行动作。
- OpenRefine API 不是稳定公开协议。固定版本、适配器隔离、假服务测试、健康检查降级。
- OpenRefine 内存和文件限制。设置行数/字节/列数上限，超限走规则清洗或 Splink+DuckDB。
- JSON-LD 映射可能歧义。自动推断 + 预览 + 手工修改，不静默导出。
- 任务页聚合多个来源，状态口径可能不一致。前端只做展示，不改变各业务状态机。

## 16. 已定取舍

- 两个交付：先 UX，再 OpenRefine/W3C。第二交付的需求已列入本设计和后续计划。
- 一级菜单 4 个：数据 / 任务 / 复核 / 设置；助手常驻，不占菜单。
- 任务页 v1 聚合现有 jobs/runs/contract versions，不新增 Task 表。
- 发布、回滚、导出留在任务详情；设置只放 Provider、团队、API 密钥、项目设置。
- 助手混合：按钮为主，自由文本可选；模型只分类和解释，使用默认 Provider，不送记录值。
- 一次确认后自动运行；边界是不发布、不合并、不替人裁决。
- 复核保留 `模型建议 · SAME` 等原文；技术细节折叠。`unification` 验收改为展开证据或断言 `why`。
- 复核接口返回 `items` + `auto_separated`；冲突类 `why` 只做纯函数单测。
- 任务与数据推荐冲突时展示两种选择，用户优先。
- 第一交付的方法标签是“规则清洗 + W3C 契约”，不冒充 OpenRefine。
- 第二交付接入 OpenRefine companion 服务、内置只读配方、diff 预览、降级，以及 LinkML + JSON Schema + JSON-LD 输出；RDF/OWL 不在第一版。
