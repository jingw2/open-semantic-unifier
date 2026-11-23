# Open Semantic Unifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付支持多场景、稳定身份、人工裁决和重复运行的实体统一首版，再推进实际契约与指标统一。

**Architecture:** 保留 FastAPI、SQLAlchemy、Celery、React 和对象存储。以项目内持久实体、来源修订、匹配方案、裁决、不可变快照为业务模型，现有引擎改为候选与证据提供者。Web 和 API 共用服务；Phoenix 保存执行追踪，业务数据库保存审计。

**Tech Stack:** Python、FastAPI、SQLAlchemy、PostgreSQL、Celery/Redis、S3/MinIO、Splink、React/TypeScript、OpenTelemetry/OpenInference、Phoenix。SQLite 支持快速开发，生产验收使用 Compose 实际服务。

**Spec:** [验收基线](../../reference/acceptance-baseline.md)、[Golden Set](../../../tests/golden/README.md)、[生命周期步骤](../../reference/golden-lifecycle.md)。

## Global Constraints

- 实体统一先正式发布，L3/L4 暂列实验能力。
- 供应商、客户、商品均通过配置接入，不向生产逻辑写入 Golden 答案。
- 来源键是 `(project_id, source_system, dataset, source_record_id)`。
- 缺失、空串不作为相等的身份标识。
- 人工禁止合并和配置的冲突约束优先于模糊分数及模型判断。
- 已发布快照不可变。修正、重新发布和回滚只改变显式版本或当前发布指针。
- 复用现有鉴权；新增 API 不绕过工作空间归属和角色校验。
- 不新增 CDC、双向写回、独立 SDK/CLI 或自研观测平台。
- 开始实现前记录实际依赖版本；新增依赖不使用未经验证的版本或 API。
- 本计划不执行提交、部署或产品实现；任务中的提交点是后续实施的可审查单元。

## Review Focus

1. 相同来源 ID 在不同系统/数据集中出现时不覆盖。T1 测 L21。
2. 匹配为空、冲突或人工拒绝时，不能通过传递聚类合并。T3/T4 测 L08、L19。
3. 并发裁决、重复任务或 Worker 中断不能产生重复业务效果。T4/T5 测 L10、L11、L17。
4. 拆分和回滚不能改变已发布历史的属性或成员。T4/T5 测 L07、L22。
5. 模型或追踪服务失败不能生成伪成功或删除业务证据。T6 测 L12、L14。

## 当前基线和证据边界

当前已有登录/RBAC、来源上传、引擎、复核、产物发布与 Serving API。权限漏洞已在工作树修复。上一轮在本机 Python 环境、临时副本中执行既有后端测试，5 项通过；这不覆盖本计划的新能力。新增 Golden 自检只验证数据和评分器，不验证当前应用。

已知待改：`MASTER_SUPPLIERS` 硬编码；L3/L4 忽略选定文件；任务存储偏向结果 JSON；拒绝和身份历史未建模；Compose 读取 `.env.example`；Makefile 依赖本机路径；缺 CI 和 LICENSE。

## 拟定文件职责

| 文件 | 职责 |
| --- | --- |
| `backend/app/models/unification.py` | 来源修订、实体、方案、裁决、快照和审计表；通过模型包导出 |
| `backend/alembic.ini`、`backend/migrations/` | 版本化迁移，替代用 create_all 表示升级 |
| `backend/app/services/unification/contracts.py` | 方案、输入、证据、动作和版本请求类型 |
| `backend/app/services/unification/ingest.py` | 来源键校验和修订生成 |
| `backend/app/services/unification/matching.py` | 标准化、候选、规则、冲突及引擎适配 |
| `backend/app/services/unification/identity.py` | 实体成员、合并、拆分、禁止关系及属性选择 |
| `backend/app/services/unification/releases.py` | 快照、变化、发布指针和回滚 |
| `backend/app/services/telemetry.py` | 追踪初始化、关联字段及导出配置 |
| `backend/app/api/v1/unification.py` | 新业务 API；复用既有 deps |
| `backend/app/workers/tasks.py` | 幂等任务编排，调用业务服务，跨 Worker 传递上下文 |
| `frontend/src/pages/UnificationPage.tsx` | 配置、匹配证据、复核与身份变化操作 |
| `backend/tests/test_unification_*.py` | 真实事务/API 验收 |
| `backend/tests/integration/test_unification_runtime.py` | Celery、Phoenix 和故障恢复 |
| `scripts/run_golden.py` | 从真实应用获取预测；不导入 oracle 生成结果 |
| `scripts/golden.py` | 已提供的静态自检及配对评分器 |
| `tests/e2e/unification.spec.ts` | 工作台与 API 的闭环验证 |

## T1 来源身份、持久实体和迁移

**Files:** 创建 models/unification.py、services/unification/contracts.py、ingest.py、alembic.ini、migrations/、backend/tests/conftest.py 和 backend/tests/test_unification_ingest.py；修改 models/__init__.py、core/db.py，调整 backend/tests/test_platform.py 的数据库初始化。

**Interfaces:** `SourceKey(project_id, source_system, dataset, source_record_id)`；`ingest_batch(db, *, project_id: str, records: list[SourceRecordInput], mode: IngestMode, input_version: str) -> IngestResult`。`IngestResult` 含 input_version、source_revision_ids 和逐条校验错误。`Entity.id` 使用现有 UUID hex 风格，不用名称派生；领域枚举区分 REVIEW/DISTINCT/AUTO_MATCH。

- [ ] 先实现失败测试 `test_source_key_is_namespaced`、`test_rerun_keeps_ids`、`test_reordered_input_keeps_identity`、`test_missing_or_conflicting_key_rejects_whole_batch`。断言 L01/L02/L16/L21。
- [ ] 执行 `python -m pytest backend/tests/test_unification_ingest.py -q`，确认因缺功能失败。
- [ ] 将既有测试导入时创建/删除相对数据库的行为改为临时目录 fixture；新旧测试都不能删除用户数据库或互相污染。添加唯一约束和来源修订；新输入先整批校验再持久化。空表返回明确的 422；探索模式用生成的单次来源键并标注限制。
- [ ] 添加新库和已有库的迁移测试，禁止删除旧业务表或重置数据；重跑上述测试及既有权限回归。
- [ ] 以“持久来源和实体身份”为独立审查/提交单元。对应 INGEST-01/02、ID-01、DEPLOY-02 的迁移部分。

## T2 方案、标准目录和属性策略

**Files:** contracts.py、identity.py、api/v1/unification.py；新增 tests/test_unification_schemes.py。

**Interfaces:** `MatchScheme` 含 entity_type、identity_granularity、field_map、normalizers、strong_identifiers、conflict_constraints、candidate_rules、attribute_policies、revision；`select_attributes(members: list[SourceRevision], scheme: MatchScheme, overrides: list[AttributeOverride]) -> CanonicalAttributes` 返回值与来源引用。

- [ ] 写 `test_three_domains_use_only_configuration`、`test_user_catalog_has_no_demo_dependency`、`test_attribute_priority_and_override_keep_provenance`、`test_invalid_field_map_is_rejected`。断言 L09 和 CONFIG-01。
- [ ] 执行 `python -m pytest backend/tests/test_unification_schemes.py -q`，确认失败。
- [ ] 版本化方案与标准目录。将 MASTER_SUPPLIERS 移至显式 demo 数据路径。上传或创建真实项目时不自动注入 demo。
- [ ] 实现字段映射与属性优先级，冲突保留备选和原因。重跑测试后独立审查。对应 CONFIG-01、ATTR-01、INGEST-02。

## T3 候选与安全匹配

**Files:** matching.py、engines.py、新增 tests/test_unification_matching.py 和 scripts/run_golden.py。

**Interfaces:** `match_records(records: list[SourceRevision], scheme: MatchScheme, constraints: list[CannotLink], judge: ModelJudge | None) -> MatchResult`。`MatchResult` 含实际候选、配对动作、规则证据和建议，不直接发布快照。`ModelJudge` 的异常与模型建议显式区分。

- [ ] 写 `test_null_identifier_never_matches`、`test_spec_conflict_vetoes_identifier`、`test_shared_contact_is_review`、`test_unknown_does_not_auto_merge`，使用 records/scheme，不在匹配代码读取 expected。
- [ ] 执行 `python -m pytest backend/tests/test_unification_matching.py -q`，确认失败。
- [ ] 适配现有清洗/Splink 以输出候选及证据。配置的小型 Golden 全配对模式用于验证规则，生产采用候选规则，不承诺任意规模全配对。
- [ ] 实现真实运行适配器 `python scripts/run_golden.py --base-url URL --output PATH`，凭据经环境变量读取。适配器只在关联输出时读取 case 引用；不得复制 truth/action。
- [ ] 用 `python scripts/golden.py evaluate PATH` 核验真实结果。此时如身份成员尚未落地，标 BLOCKED，不伪造 assignments；在 T4 完成后重跑。对应 MATCH-01/02、GOLD-01。

## T4 持久裁决、合并拆分及冲突

**Files:** identity.py、unification.py、models/unification.py、tests/test_unification_identity.py、tests/test_unification_lifecycle.py。

**Interfaces:** `apply_decision(db, *, project_id: str, decision: DecisionInput, expected_revision: int, actor_id: str) -> DecisionResult`；`merge_entities(db, *, target_id: str, source_ids: list[str], actor_id: str, expected_revision: int) -> IdentityChange`；`undo_identity_change(db, *, change_id: str, expected_revision: int, actor_id: str) -> IdentityChange`。所有操作原子记录 audit。

- [ ] 写 L04–L09、L17 对应测试；特别是 `test_cannot_link_blocks_transitive_merge`、`test_stale_decision_returns_409`、`test_redirect_cycle_rejected`。
- [ ] 执行 `python -m pytest backend/tests/test_unification_identity.py backend/tests/test_unification_lifecycle.py -q`，确认失败。
- [ ] 增加 cannot-link、乐观锁及引用完整性。合并前检查整个成员集合的冲突，不能只检查当前边。
- [ ] 固定拆分策略：优先恢复此次合并前被退役的实体 ID；如被后续有效操作占用或依赖，返回 409，要求显式选择拆分成员并分配新 ID，保留谱系。补两种路径测试。
- [ ] 再跑三场景真实 Golden，完成全部配对闸门。以“可撤销、可复用裁决”为审查单元。对应 ID-02/03、HITL-01/02/03、MATCH-03、ATTR-01。

## T5 运行版本、幂等与原子发布

**Files:** releases.py、tasks.py、models/unification.py；tests/test_unification_releases.py、integration/test_unification_runtime.py。

**Interfaces:** `submit_run(db, *, project_id: str, input_version: str, scheme_revision: str, idempotency_key: str, payload_hash: str) -> Run`；`publish_snapshot(db, *, run_id: str, expected_current_version: str | None, actor_id: str) -> SnapshotRef`；`rollback_snapshot(db, *, project_id: str, snapshot_id: str, actor_id: str) -> SnapshotRef`；`diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDelta`。

- [x] 写 `test_same_key_different_payload_409`、`test_concurrent_submit_has_one_effect`、`test_worker_crash_never_publishes_partial_result`、`test_reader_never_sees_mixed_snapshot`、`test_rollback_keeps_history`，断言 L03/L10/L11/L18/L22。
- [x] 执行目标测试确认失败；Celery 项必须使用真实服务，不能以同步运行替代。
- [x] 使用数据库唯一约束、尝试记录、结果暂存和事务发布指针实现幂等。明确有限重试及终止失败状态。
- [x] 验证旧版本成员与属性不可变，delta 区分新增、变更、冲突。重跑身份、生命周期及权限测试。对应 RUN-01/02、OPS-01/02、VER-01/02。

## T6 模型错误与 Phoenix 追踪

**Files:** telemetry.py、llm_client.py、tasks.py、模型配置类型；integration/test_unification_runtime.py；docker-compose.yml、.env.example。

**Interfaces:** `configure_tracing(config: TraceConfig) -> None`；`judge_pair(request: JudgeRequest, config: ProviderConfig) -> JudgeResult`；`JudgeResult` 是建议或明确错误，不能以 None 混淆未启用与失败。trace 关联 project_id/run_id/input_version/scheme_revision/prompt_version，不记录 API Key。

- [x] 写 `test_timeout_and_invalid_json_follow_fallback`、`test_model_cannot_override_spec_veto`、`test_worker_trace_continues`、`test_collector_down_does_not_block_run`、`test_audit_survives_trace_deletion`，断言 L12–L14/L19。
- [x] 以受控模型服务执行确定性故障，确认失败；真实模型连通另记证据，不用固定响应证明模型质量。
- [x] 通过当前官方接口集成 OpenTelemetry/OpenInference；核实依赖和 Phoenix 版本后固定。配置独立 Phoenix 服务和 OTLP endpoint；导出失败与业务失败分开。
- [x] 查询 Phoenix 的实际 span；检查 API/Worker 上下文、提示词版本、请求/响应、延迟、重试和错误。未知 token/费用保持未知；验证凭据不出现在 trace。
- [x] 完成一条用户选定本地/私有模型烟测。缺服务时标 BLOCKED。对应 LLM-01、OBS-01/02、AUDIT-01、SEC-01。

## T7 工作台、API 与导出闭环

**Files:** unification.py、main.py、frontend/src/pages/UnificationPage.tsx、App.tsx、lib/api.ts、tests/e2e/unification.spec.ts；tests/test_unification_api.py。

**API contracts:** 项目内 `/unification/schemes`、`/catalog`、`/batches`、`/runs`、`/reviews`、`/entities`、`/identity-changes`、`/snapshots`；成员查询始终带版本。POST runs 接收 Idempotency-Key；裁决带 expected_revision；实体/映射/裁决/delta 导出必须指明 snapshot_id。分页稳定，API 不返回裸凭据。Serving 复用项目 Key 并绑定明确发布版本。

- [x] 写 `test_foreign_project_resources_hidden`、`test_viewer_cannot_mutate`、`test_export_matches_snapshot`，以及 UI 的配置、复核、撤销、发布、下载流程，断言 L15/L20。
- [x] 执行 API 与 UI 测试确认失败；通过真实浏览器观察表单与结果，不只检查构建。
- [x] 接入统一服务，UI 展示候选依据、冲突、未知、变化、版本和 trace 链接；不重建独立的前端业务判定。
- [x] 执行后端测试、`npm --prefix frontend run build` 和 UI 测试；对同一快照比较四类输出。对应 API-01、SEC-01。

## T8 开源首版发布验收

**Files:** docker-compose.yml、Dockerfiles、.env.example、Makefile、README.md、LICENSE、CONTRIBUTING.md、docs/how-to/、.github/workflows/ci.yml、tests/integration/test_compose_release.py。

**Interfaces:** CI 先跑 Golden 自检和评分器测试，再跑数据库/API、运行服务及 UI 验收。完整报告按 baseline_id 汇总，不以被跳过的测试计入通过。

- [ ] 写 `.env` 覆盖、生效密码、迁移、重启持久化、异步发布和下载的 Compose 验收；在未修复配置时确认失败。
- [ ] 修复 `.env` 读取及本机路径依赖；构建使用已验证的依赖和镜像版本，前端使用 lockfile。检查权属/依赖授权后选择允许商业使用的许可证，不预先假定全部依赖兼容。
- [ ] 添加安装、三个案例、API、限制、贡献和反馈文档；纠正 README 过度承诺；列出各数据库连接器实际验证状态。
- [ ] 在干净环境完整执行，并重启验证持久化。报告 V0.1 全部闸门与 L01–L22；任何必选项 BLOCKED/FAIL 都不能称为正式首版通过。
- [ ] 发布只在后续用户授权时执行。对应 DEPLOY-01/02、OSS-01/02、GOLD-02 及全部 V0.1 汇总。

## 后续 L3/L4 的交付边界

实体首版通过后，分别写两份详细实施计划，不提前锁死未验证的契约编译器和指标引擎接口。

1. L3 消费真实实体快照和用户选中的契约文件，生成版本化业务模型，校验字段、枚举、关系及引用；禁止示例内容替代真实输入。通过 L3-01 后才正式声明支持。
2. L4 绑定 L3 业务模型及实体映射快照，明确粒度、时间、过滤、币种和关联基数。使用独立手算结果检验去重、退款、重复关联放大和历史版本复查。通过 L4-01、LINEAGE-01 后才正式声明支持。

## 后续增补：决策模型作为可选判断方法

在实体统一既有模型判断接口上，增加可插拔的类型化决策模型适配器，候选包括 TypeSafe Jev 和 Perplexity Decision API，以及可私有部署的兼容实现。现有 OpenAI 兼容模型判断继续可用。各提供商的接口、鉴权、模型版本、数据处理和可用性必须在实现前依据官方文档分别验证，不能假设协议一致。

- 输入为候选对、已配置的实体粒度、字段和可核验的匹配证据；输出为 SAME / DIFFERENT / UNKNOWN 的建议、可用的选项分布、模型版本和错误状态。缺失分布或解释必须标记未知，不得编造。
- 规则冲突、人工禁止合并和已确认的裁决继续优先；模型建议默认只进入复核，不直接触发合并。低置信、模型冲突、服务不可用和输出无效均按明确配置送审或使运行失败。
- 记录提供商、固定模型版本、请求模板版本、耗时、错误和可用的用量；凭据不得进入日志、导出或 trace。外部调用须由用户显式配置，明确发送字段和数据流向；允许本地或私有部署替代。
- 使用冻结 Golden 集和独立留出集分别报告各场景动作、候选召回、错误合并、复核量、延迟和成本；分数不能视为已校准概率。若未来考虑自动决策，先制定独立阈值、校准和错误合并验收，再经人工确认范围。
- 依次完成设计与实现计划、适配器和错误回退测试、真实服务连通烟测、Golden 对比及 GPT-6.1-Sol 审查。无真实服务时标 BLOCKED；该增补不改变当前 T8 未完成的发布验收状态。

## 实施规则与进度

本文件当前是计划，不代表任务已实现。每个 T 单元按“失败证据 → 最小实现 → 实际业务验证 → 审查”的顺序推进；完成后更新 checkbox 并附证据。不得在实现过程中同步改 oracle 以掩盖失败。数据修订单独说明业务理由。

先执行 T1，再执行 T2–T8 的依赖顺序。T3 的真实成员评分依赖 T4；T6 的跨 Worker 验收依赖 T5；T7 的 UI 闭环依赖前面服务。无需为了并行提前制造空接口或兼容层。

本轮已自检需求覆盖：范围和验收表全部映射到 T1–T8 或后续 L3/L4。尚未启动产品实现。
