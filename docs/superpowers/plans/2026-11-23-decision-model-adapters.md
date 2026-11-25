# 决策模型适配器实施计划

> **状态：** T-DM-1 至 T-DM-5、T-DM-7、T-DM-9 已实现并验收。T-DM-8 的独立留出集和评估工具已冻结（`tests/holdout/v1`），真实服务评估与烟测仍 BLOCKED：没有 Perplexity 或 TypeSafe 的真实凭据。证据见 [决策模型验收记录](../../reference/decision-model-acceptance.json) 和 [评估说明](../../reference/decision-model-evaluation.md)。

**目标：** 在实体配对复核中增加可插拔、类型化的决策模型建议。规则冲突、人工禁止合并和已确认裁决继续优先；模型建议默认只进入复核，不直接触发合并。

**Spec：** [实体统一计划](2026-11-23-open-semantic-unifier.md) 的“后续增补：决策模型作为可选判断方法”、[验收基线](../../reference/acceptance-baseline.md) 的 LLM-01、SEC-01、OBS-01、AUDIT-01。

## 现状与差距

- `MatchScheme` 已有 `model_judgment_enabled`、`model_provider_id`、`model_failure_policy`、`prompt_version`。
- `backend/app/services/llm_client.py` 支持 OpenAI 兼容接口，区分建议与错误，并写入 `unification.model_judge` span。
- `backend/app/services/unification/matching.py` 在确定性冲突和禁止关系之后调用模型判断。
- `LLMProvider` 只有名称、base URL、默认模型和加密凭据。没有提供商类型、固定模型版本、数据外发字段、选项分布或持久调用记录。
- 没有独立留出集评估，也没有 Jev、Perplexity Decision API 或兼容实现的适配器。
- 现有得分不是校准概率，不能作为自动决策阈值。

## Global Constraints

- 实现前逐个核实提供商官方文档：接口、鉴权、模型版本、请求/响应结构、数据处理、可用性、限流和用量。不得假设协议一致。
- 输入只包含候选对、配置的实体粒度、字段和可核验匹配证据。不得发送未配置字段。
- 输出为 `SAME`、`DIFFERENT` 或 `UNKNOWN` 的建议，可选选项分布、解释、模型版本和错误状态。缺失分布或解释标为未知，不得编造。
- 规则冲突、人工禁止合并和已确认裁决优先。模型建议默认进入 `REVIEW`；本阶段不增加自动合并阈值。
- 低置信、模型冲突、服务不可用和输出无效按配置送审或使运行失败。重试有上限。
- 记录提供商、固定模型版本、请求模板版本、耗时、错误和可用的用量。凭据不得进入日志、导出或 trace。
- 外部调用必须由用户显式配置；界面说明发送的字段和数据流向。本地或私有部署必须可用。
- 复用工作空间、项目角色和既有凭据加密。viewer 只读。
- 评估使用冻结 Golden 集和独立留出集，分别报告各场景动作、候选召回、错误合并、复核量、延迟和成本。分数不视为已校准概率。
- 缺真实服务或凭据时对应验收标 BLOCKED，不以固定响应代替连通证据。

## 拟定文件职责

| 文件 | 职责 |
| --- | --- |
| `backend/app/services/decision_models/contracts.py` | 类型化请求、建议、错误、能力和版本字段 |
| `backend/app/services/decision_models/adapters.py` | 提供商适配器协议和 OpenAI 兼容实现 |
| `backend/app/services/decision_models/providers/` | Jev、Perplexity 和本地兼容适配器；每个提供商独立映射 |
| `backend/app/services/llm_client.py` | 保留现有兼容入口，改为调用适配器边界 |
| `backend/app/models/unification.py` | 持久模型调用记录和提供商配置字段 |
| `backend/app/api/v1/providers_jobs.py` | 提供商类型、数据外发配置、连通测试 |
| `backend/app/api/v1/unification.py` | 受控配对测试和模型调用查询 |
| `frontend/src/pages/ProvidersPage.tsx`、`UnificationPage.tsx` | 配置、数据流向说明、复核证据 |
| `backend/tests/test_decision_models_*.py` | 契约、回退、优先级、审计、安全 |
| `tests/decision_models/` | Golden 对比和独立留出集；不进入生产匹配器 |

## 接口（暂定，提供商核实后定稿）

```python
class DecisionVerdict(StrEnum):
    SAME = "SAME"
    DIFFERENT = "DIFFERENT"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True)
class DecisionRequest:
    entity_type: str
    identity_granularity: str
    fields: Mapping[str, object]
    evidence: tuple[Mapping[str, object], ...]
    prompt_version: str

@dataclass(frozen=True)
class DecisionSuggestion:
    verdict: DecisionVerdict
    confidence: float | None
    distribution: Mapping[str, float] | None
    explanation: str | None
    provider_kind: str
    model_version: str
    prompt_version: str
    latency_ms: float
    usage: Mapping[str, object] | None

@dataclass(frozen=True)
class DecisionError:
    code: str
    message: str
    retryable: bool

DecisionOutcome = DecisionSuggestion | DecisionError | None  # None = 未启用
```

`judge_pair` 返回 `DecisionOutcome`。`None` 只表示未启用，失败必须返回 `DecisionError`，不得用 `None` 混淆。

## T-DM-1 提供商核实

**Files:** `docs/reference/decision-model-providers.md`（新增）；无产品代码。

- [ ] 对 TypeSafe Jev、Perplexity Decision API 和候选本地兼容实现，分别记录官方文档链接、鉴权方式、固定模型版本、请求/响应字段、超时/限流、数据处理条款、可用性和用量返回。
- [ ] 核实哪些提供商允许发送候选对字段，哪些需要私有部署；记录不能支持的能力。
- [ ] 产出适配器支持矩阵，明确首发支持范围。缺文档或缺凭据的提供商标 BLOCKED，不猜协议。
- [ ] 评审通过前不写提供商适配器代码。

## T-DM-2 类型化判断契约

**Files:** `services/decision_models/contracts.py`、`tests/test_decision_models_contracts.py`。

- [ ] 先写失败测试：建议、错误、未启用三种状态可区分；`UNKNOWN` 与 `DIFFERENT` 不混淆；缺失分布和解释保持未知。
- [ ] 校验置信度范围、分布键、模型版本和提示词版本；非法输出返回明确错误码。
- [ ] 保持现有 `JudgeResult` 调用方兼容，或在同一改动中迁移全部调用方并删除旧类型。
- [ ] 验证：契约测试通过；现有模型错误和回退测试继续通过。

## T-DM-3 适配器边界和兼容实现

**Files:** `services/decision_models/adapters.py`、`services/llm_client.py`、`tests/test_decision_models_adapters.py`。

- [ ] 先写失败测试：协议适配器的能力查询；OpenAI 兼容实现超时、坏 JSON、缺凭据、正常建议；错误不吞掉。
- [ ] 适配器只负责协议映射，不决定复核或合并策略。策略留在 `matching.py`。
- [ ] 请求只发送配置字段；记录发送字段名，不记录凭据。
- [ ] 现有 `unification.model_judge` span 和错误码保持可查询。
- [ ] 验证：适配器测试通过；现有 `test_unification_llm.py` 不回归。

## T-DM-4 外部提供商适配器

**Files:** `services/decision_models/providers/`、`tests/test_decision_models_providers.py`。

- [ ] 只为 T-DM-1 已核实的提供商实现适配器。每个适配器独立映射请求和响应，不共享未经验证的字段假设。
- [ ] 先写失败测试：受控 HTTP 服务返回有效建议、`UNKNOWN`、非法枚举、缺分布、超时、401、429；映射为类型化结果或错误。
- [ ] 固定模型版本和请求模板版本；不把用户可选模型当作已校准版本。
- [ ] 记录用量和错误码；提供商不返回用量时标未知，不估算费用。
- [ ] 验证：受控 HTTP 测试通过；真实服务烟测在 T-DM-8 执行。

## T-DM-5 持久记录和观测

**Files:** `models/unification.py`、迁移、`services/unification/releases.py`、`telemetry.py`、`tests/test_decision_models_audit.py`。

- [ ] 先写失败测试：每次模型调用持久记录项目、运行、配对、提供商、模型版本、提示词版本、建议/错误、耗时、用量、trace 引用。
- [ ] trace 删除后记录仍可查；凭据不出现在记录、日志、导出或 span。
- [ ] 为模型调用写 span 属性：提供商、模型版本、提示词版本、错误码、耗时。请求/响应内容按数据外发配置记录或省略。
- [ ] 验证：审计和脱敏测试通过；Phoenix 查询能看到调用 span，业务审计不依赖远端 trace。

## T-DM-6 优先级和回退

**Files:** `services/unification/matching.py`、`tests/test_decision_models_precedence.py`。

- [ ] 先写失败测试：规格冲突和人工 cannot-link 在模型返回 `SAME` 且高置信时仍为 `DISTINCT`；模型建议不能绕过传递冲突。
- [ ] 模型 `SAME` 默认映射为 `REVIEW`；`DIFFERENT` 不能自动形成持久拒绝，除非配置允许并经过确认路径。
- [ ] 超时、坏 JSON、缺凭据、提供商不可用按 `REVIEW` 或 `FAIL` 策略处理；失败有上限并使用既有 attempt 限制。
- [ ] 模型结果与确定性证据并存，复核界面显示两者。
- [ ] 验证：优先级测试通过；L19 和现有 cannot-link 测试不回归。

## T-DM-7 配置、API 和界面

**Files:** `models/entities.py`、`api/v1/providers_jobs.py`、`api/v1/unification.py`、`frontend/src/pages/ProvidersPage.tsx`、`UnificationPage.tsx`、`tests/test_decision_models_api.py`。

- [ ] 提供商配置增加类型、固定模型版本、提示词版本、发送字段、超时、重试和失败策略。凭据继续加密存储。
- [ ] 受控配对测试 API 返回建议或明确错误，不发布快照。
- [ ] 界面展示数据流向、发送字段、提供商核实状态、模型版本和建议依据；未知分布显示为未知。
- [ ] 匿名 401、跨空间 404、viewer 只读；配置变更写审计。
- [ ] 验证：API 和浏览器流程通过；生产构建通过。

## T-DM-8 评估和真实烟测

**Files:** `tests/decision_models/`、`docs/reference/decision-model-evaluation.md`。

- [ ] 在冻结 Golden 集和独立留出集上分别运行，报告每场景动作、候选召回、错误合并、复核量、延迟和成本。
- [ ] 至少对一个已支持提供商执行真实连通烟测，记录 provider、model version、prompt version、请求字段和结果。缺服务或凭据时标 BLOCKED。
- [ ] 确认模型建议没有绕过 Golden 的硬冲突和 cannot-link 断言。
- [ ] 不根据分数推断校准概率，不设置自动合并阈值。
- [ ] 验证：评估报告可复现；失败项不被平均值掩盖。

## T-DM-9 验收和审查

**Files:** `docs/reference/decision-model-acceptance.json` 或现有验收报告新章节。

- [ ] DM-01 契约：建议、未知、错误和未启用可区分。
- [ ] DM-02 优先级：规则、冲突、人工裁决和 cannot-link 覆盖模型建议。
- [ ] DM-03 失败回退：超时、坏 JSON、缺凭据和不可用按配置处理，重试有上限。
- [ ] DM-04 安全：凭据不进入日志、导出或 trace；外发字段显式配置。
- [ ] DM-05 观测：模型调用可定位，审计不依赖远端 trace。
- [ ] DM-06 评估：Golden 和独立留出集报告，分数不作为校准概率。
- [ ] DM-07 真实烟测：至少一个真实提供商，否则 BLOCKED。
- [ ] 独立审查通过后更新验收状态；不改变实体 V0.1 的 OSS-02 状态。

## Review Focus

1. 模型 `SAME` 高置信不能覆盖硬冲突、人工拒绝或传递冲突。
2. `UNKNOWN`、`DIFFERENT` 和错误状态不能混淆；缺失分布不能编造。
3. 外部调用只发送显式配置字段；凭据不出现在任何可导出位置。
4. 真实服务失败不能产生伪成功或删除业务证据；重试有上限。
5. Golden 和留出集评估不能只报平均值；错误合并和复核量必须分别可见。

## 未决问题

- 首发支持哪些提供商？Jev 和 Perplexity Decision API 的当前官方接口是否可用，是否需要企业授权？
- 外部提供商的请求/响应内容是否允许写入 Phoenix？默认记录字段名还是值？
- “独立留出集”由谁提供、如何冻结，是否需要真实用户授权数据？
- 模型调用记录保留期和删除策略是否与现有审计策略一致？
