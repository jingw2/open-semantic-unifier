# 零点未来 · 企业语义统一平台（Semantic Unifier Platform）

私有化 / 团队自部署的企业级语义统一全栈平台，将 **L1 小表清洗对齐、L2 Splink 5.0 + LLM 混合实体解析、L3 LinkML 源头契约与 AI 防幻觉、L4 指标语义层** 整合为完整生产闭环。

## 核心特性
- **私有化账号与 RBAC 权限**：首次启动自动初始化超管（关闭公开注册），支持 Admin 管理团队成员与 `Admin / Editor / Viewer` 角色，Fernet 对称加密存储数据库密码与大模型 API Key。
- **多源数据接入 + 安全抽样**：支持上传 `CSV / XLSX / Parquet / TSV / YAML` 与直连 `DuckDB / SQLite / MySQL / PostgreSQL / MongoDB`；大表先 5,000 行安全抽样体检，执行任务时异步抽取落盘 Parquet 进 MinIO（支持自定义 SQL 过滤）。
- **智能路由（Smart Router）**：根据数据画像与业务目标自动推荐 L1–L4 路线并给出大白话理由。
- **人工复核与规则沉淀（Human-in-the-Loop）**：L1 低分项与 L2 灰色边界样本进入复核队列，确认后自动沉淀为项目级可复用映射字典。
- **草稿 / 发布双态版本 + 对外 Serving API**：L3 契约与 L4 指标支持版本号管理、一键发布到线上与一键回滚，签发项目级 `X-API-Key` 供外部系统与 AI Agent 直接调用 `/api/v1/serve/{project_slug}/reconcile|validate|metrics/query`。

## 一键 Docker Compose 私有化部署
```bash
cp .env.example .env
docker compose up -d --build
```
- Web 工作台：`http://localhost:8080`
- 默认超管：`admin@lingdianweilai.ai` / `Admin@2026`（请在 `.env` 中修改）
- MinIO 控制台：`http://localhost:9001`
- 后端 Swagger 文档：`http://localhost:8000/docs`
