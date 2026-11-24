# L3 契约统一实施计划

> **状态：** 计划，未实现。前置为实体统一 V0.1 验收完成（T8 仅剩 OSS-02 远端 CI 证据）。本计划通过 L3-01 前，不得对外声明支持 L3。

**目标：** 用户显式选择真实契约文件和已发布实体快照，生成不可变、可校验、可追溯的业务模型版本。禁止使用示例内容替代真实输入。

**Spec：** [验收基线](../../reference/acceptance-baseline.md) 的 L3-01、[实体统一计划](2026-11-23-open-semantic-unifier.md) 的 L3 边界。

## 现状与差距

- `backend/app/services/engines.py` 的 `execute_l3` 在未提供输入时写入 `DEFAULT_SCHEMASHEET_TSV` 和 `DEFAULT_BASE_SCHEMA_YAML`，并生成仿真 DuckDB 数据。
- 旧路径不读取用户选择的契约文件，不绑定实体快照，不保存结构化的模型版本。
- `ArtifactVersion` 只保存文件产物，无法表达字段类型、枚举、关系、引用和校验错误。
- L3-01 尚未执行，不能把旧页面的演示结果当作验收证据。

## Global Constraints

- 只消费 `DataSource` 中真实上传或选定的契约文件，以及已发布 `UnificationSnapshot`。缺输入返回 422，不回退到示例。
- 契约定稿版本不可变。修正生成新版本，发布指针单独原子切换；历史版本和旧导出保持不变。
- 模型必须记录 `entity_snapshot_id`、实体快照版本、契约文件哈希、编译器版本和输入引用。
- 字段类型、枚举、关系和引用必须可校验。错误逐条返回，整批不部分发布。
- 复用现有工作空间、项目角色和凭据脱敏。viewer 只读。
- 不在 L3 中实现指标计算；L4 另行计划。
- 编译器依赖 `linkml`、`schemasheets` 的版本必须固定并验证。当前审计把 `rfc3987` 标为 GPL-3.0-or-later，分发前需复核。
- 不在生产逻辑中写入任何 Golden 或演示答案。

## 拟定文件职责

| 文件 | 职责 |
| --- | --- |
| `backend/app/models/contracts.py` | 契约源文件、模型版本、发布指针、校验记录 |
| `backend/app/services/contracts/compiler.py` | 真实文件到 LinkML、SQL DDL、JSON Schema、ER 的编译适配 |
| `backend/app/services/contracts/validation.py` | 字段、类型、枚举、关系、引用校验 |
| `backend/app/services/contracts/releases.py` | 不可变版本、发布指针、导出、审计 |
| `backend/app/api/v1/contracts.py` | 项目内契约 API；复用既有 deps |
| `backend/migrations/versions/` | 新表和发布指针迁移 |
| `frontend/src/pages/ContractsPage.tsx` | 文件选择、校验错误、版本、发布、导出 |
| `backend/tests/test_contracts_*.py` | 事务/API 验收 |
| `tests/fixtures/contracts/` | 非示例的真实结构测试夹具，不含业务真值 |
| `docs/how-to/contracts.md` | 操作说明和限制 |

## 接口（暂定，编译器验证后定稿）

```python
compile_contract(source: ContractSource, snapshot: PublishedSnapshot) -> CompiledModel
validate_model(model: CompiledModel) -> ValidationReport
publish_contract(db, *, project_id: str, model_id: str, expected_current_version: int | None, actor_id: str) -> ContractRelease
```

`CompiledModel` 包含 entities、fields、types、enums、relations、keys、references、compiler_version、input_sha256、entity_snapshot_ref。`ValidationReport` 包含 accepted、issues、counts；issues 逐条含 path、code、message。

## T-L3-1 契约源和真实输入边界

**Files:** 新增 `models/contracts.py`、`services/contracts/contracts.py` 或数据类、`tests/test_contracts_ingest.py`；修改 `models/__init__.py`、`core/db.py`、迁移。

- [ ] 先写失败测试：未选择文件返回 422；选择不存在或跨项目的 `DataSource` 返回 404；空文件、错误扩展名、超限文件返回明确错误且不落库。
- [ ] 实现契约源记录：项目、文件对象键、格式、SHA-256、大小、上传者、时间。允许格式先限定为 LinkML YAML、schemasheet TSV/CSV。
- [ ] 同项目同一哈希重复上传复用记录；内容变化生成新源记录，不覆盖历史。
- [ ] 拒绝示例回退。`execute_l3` 的 `DEFAULT_SCHEMASHEET_TSV` 路径在新 API 不可达。
- [ ] 验证：focused ingest 测试通过；迁移在空库和既有库上升级通过。

## T-L3-2 编译器适配和版本固定

**Files:** `services/contracts/compiler.py`、`backend/Dockerfile`、`backend/requirements-lock.txt`、`tests/test_contracts_compiler.py`。

- [ ] 记录实际可用的 `sheets2linkml`、`gen-sqlddl`、`gen-json-schema`、`gen-erdiagram` 命令和版本；测试启动时检查命令存在，缺失返回明确错误。
- [ ] 先写失败测试：真实 YAML 和 TSV 夹具生成非空 LinkML YAML、SQL DDL、JSON Schema、ER；不包含演示实体名。
- [ ] 编译在临时目录执行，限制输入大小和超时；命令失败、超时、空输出均返回 422/500 的明确错误，不写版本。
- [ ] 记录编译器版本和输入哈希。评测 `rfc3987` 等依赖的许可证义务，必要时隔离 LinkML 编译器进程。
- [ ] 删除新路径对 `DEFAULT_BASE_SCHEMA_YAML` 的依赖；旧路径的去留由 T-L3-7 统一切换。
- [ ] 验证：编译器测试通过；产物结构与夹具字段一致。

## T-L3-3 模型结构和校验

**Files:** `services/contracts/validation.py`、`tests/test_contracts_validation.py`。

- [ ] 先写失败测试：未知字段类型、重复字段、空枚举、枚举越界、关系目标实体不存在、引用字段不存在、键不唯一、循环引用分别返回指定错误码。
- [ ] 类型映射限定为实际支持的集合；不支持的类型明确拒绝，不猜测。
- [ ] 校验实体快照中的实体类型是否存在；契约中的实体映射到快照实体 ID 或显式的新类型，冲突返回 422。
- [ ] 校验通过后生成 `CompiledModel` 的规范 JSON；字段顺序稳定，便于哈希和 diff。
- [ ] 验证：校验测试逐条通过；同一输入两次生成相同哈希。

## T-L3-4 版本、发布和导出

**Files:** `models/contracts.py`、`services/contracts/releases.py`、`api/v1/contracts.py`、`tests/test_contracts_releases.py`。

- [ ] 先写失败测试：发布后模型行不可变；新版本不改变旧版本；发布指针原子切换；过期 `expected_current_version` 返回 409；viewer 发布返回 403。
- [ ] 实现项目内发布指针和审计记录，含 actor、time、reason、版本、trace 引用。
- [ ] 导出四类产物：模型 JSON、JSON Schema、SQL DDL、ER 图。导出必须指明 `version`，内容来自该版本。
- [ ] 同一版本重复发布幂等；回滚只切换指针并生成审计，不修改历史版本。
- [ ] 验证：发布/回滚测试通过；同一版本导出字节稳定。

## T-L3-5 API 和权限

**Files:** `api/v1/contracts.py`、`api/v1/__init__.py`、`main.py`、`tests/test_contracts_api.py`。

- [ ] 设计并测试 `/projects/{project_id}/contracts`、`/contracts/{id}`、`/contracts/{id}/publish`、`/contracts/{id}/export`。分页稳定，列表不返回裸文件内容。
- [ ] 匿名 401；跨空间 404；viewer 只读；editor/admin 可上传、校验、发布。
- [ ] 错误响应包含 path、code、message；不返回凭据或内部路径。
- [ ] 验证：API 权限和错误测试通过；既有权限回归通过。

## T-L3-6 工作台

**Files:** `frontend/src/pages/ContractsPage.tsx`、`App.tsx`、`lib/api.ts`、`frontend/tests/`。

- [ ] 页面展示文件选择、格式、哈希、校验错误、实体快照版本、模型版本、发布状态和四类导出。
- [ ] 用户能在真实浏览器中完成选择文件、校验、修正、发布和下载；错误可见且不丢失已保存草稿。
- [ ] 不新增前端独立业务判定；所有结果来自 API。
- [ ] 验证：Chromium 流程通过；生产构建通过。

## T-L3-7 切换和删除演示回退

**Files:** `services/engines.py`、`workers/tasks.py`、`api/v1/providers_jobs.py`、`frontend` 旧 L3 入口、文档。

- [ ] 把旧 L3 工作台入口迁移到新契约页面；旧 `/jobs` L3 路径返回明确的弃用错误或转发新服务，不保留双写。
- [ ] 删除 `DEFAULT_SCHEMASHEET_TSV`、`DEFAULT_BASE_SCHEMA_YAML` 在新 L3 路径的使用；如果旧测试仍依赖演示，改为显式 fixture，不进入产品路径。
- [ ] 更新安装、场景和 API 文档，标记 L3 为实验能力，说明支持的文件格式和限制。
- [ ] 验证：全后端测试、前端构建、旧入口回归通过。

## T-L3-8 L3-01 验收

**Files:** `backend/tests/test_contracts_acceptance.py`、真实夹具、验收报告。

- [ ] 使用非示例的 LinkML/TSV 契约文件和已发布实体快照，完成上传、校验、发布、导出。
- [ ] 断言字段类型、枚举、关系和引用进入模型；错误输入被逐条拒绝；历史版本和导出不变。
- [ ] 运行全后端、前端构建、迁移检查和 Golden 自检，记录命令与输出。
- [ ] 报告 L3-01 为 PASS 前，确认没有示例内容进入生产路径，且依赖许可证复核已完成。
- [ ] 独立审查后更新验收状态。缺真实契约定稿时，L3-01 标 BLOCKED，不伪造通过。

## 测试矩阵

| 层级 | 覆盖 |
| --- | --- |
| 单元 | 格式检测、哈希、类型映射、枚举、引用、循环、错误码 |
| 事务/API | 原子校验、不可变版本、发布指针、权限、导出 |
| 真实服务 | 真实文件编译、PostgreSQL 迁移、浏览器流程 |
| 契约 | L3-01 端到端，非示例输入 |

## 本轮决策（2026-11-24）

- 首发格式：LinkML YAML、schemasheet TSV、CSV 都支持；CSV 转换为 TSV 后编译。
- 实体映射：class 名必须匹配已发布快照的 entity_type，或列入 `allowed_new_entity_types`；显式映射表留待后续。
- LinkML 依赖：编译器在子进程执行；`rfc3987` 等 GPL 依赖已在 `license-audit-summary.json` 标记，分发前仍需复核。
- 旧 L3 入口：旧作业、示例数据源和前端入口退役；新入口是契约统一工作台。旧 `execute_l3` 仅作为 L4 演示种子保留。
- 关系与引用：LinkML 1.12.1 的 `gen-sqlddl` 不接受自定义 `references` 字段；关系使用 class range，未定义目标报 `UNKNOWN_RANGE`。

## 实施状态（2026-11-24）

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| T-L3-1 契约源 | 完成 | `9b4168f`；11 tests；真实 HTTP 上传、选择、哈希复用 |
| T-L3-2 编译 | 完成 | `4699dd8`；19 tests；YAML/TSV/CSV 真实编译 |
| T-L3-3 校验 | 完成 | `00cc7e4`、`55ede8f`；21 tests；canonical JSON |
| T-L3-4 版本/发布/导出 | 完成 | `50f9bf1`；19 release tests；真实 uvicorn 冒烟 |
| T-L3-5 API/权限 | 完成，契约源列表未分页 | contracts router；版本和审计分页；viewer 403 |
| T-L3-6 工作台 | 完成，viewer 只读未跑浏览器 | `aa46d97`；Chromium happy path 和校验错误；生产构建 |
| T-L3-7 切换 | 完成 | `aa46d97`；旧 L3 作业/示例/UI 退役；L4 演示种子保留 |
| T-L3-8 L3-01 | BLOCKED | 合成 supplier contract 端到端通过；缺客户真实契约 |

其他偏差：
- 契约产物存 DB JSON，单产物上限 10 MiB。
- 契约源列表未分页；版本和审计已分页。
- 工作台 viewer 只读由 `canEdit` 禁用控件；后端 403 已测，浏览器流程只覆盖 editor。

证据：`docs/reference/l3-acceptance-report.json`；全后端 220 passed / 6 skipped；前端生产构建；两个 Chromium 流程。
