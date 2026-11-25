# Decision models

Decision models add a pluggable, typed suggestion to entity-pair review. They return `SAME`, `DIFFERENT`, or `UNKNOWN` with a confidence and a probability distribution. The workbench configures them under **模型 Provider** and enables them per scheme under **实体统一工作台**.

## Provider kinds

| Kind | Description | Default model |
| --- | --- | --- |
| `openai_compatible` | Any OpenAI-compatible chat endpoint, including local and private deployments. | User-configured. |
| `perplexity_decisions` | Perplexity Decisions choice protocol. | `pplx-decider-flash-latest`. |
| `typesafe_jev` | TypeSafe Jev choice protocol. | `jev-latest`. |

The Perplexity preset prefills `https://api.perplexity.ai` and `pplx-decider-flash-latest`; the TypeSafe preset prefills `https://api.typesafe.ai` and `jev-latest`. The decision providers answer the same choice shape, so the request and response mapping is shared; only the endpoint and model defaults differ.

## Provider configuration

Under **模型 Provider**, save a provider with:

- **Provider 类型** (`provider_kind`): one of the three kinds above.
- **发送字段** (`send_fields`, comma-separated): the record fields sent to the provider. A Perplexity or TypeSafe provider requires at least one field. An OpenAI-compatible provider with no fields sends every field in the scheme field map.
- **超时（秒）** (`timeout_seconds`): request timeout. Leave blank for the server default.
- **最大重试次数** (`max_retries`): bounded retries for retryable errors.
- **提示词版本** (`prompt_version`): the request template version recorded with every call.

The API key is Fernet-encrypted at rest and never returned in plaintext. Choose **编辑** on a saved provider to change its configuration. Leave the API key blank to keep the stored key.

## Data-flow disclosure

External calls happen only when a scheme enables decision models and selects a configured provider. The request body contains the configured send fields for the two candidate records plus the entity type, granularity, and candidate evidence. Evidence keeps the rule name and field name, and it removes the value, left, and right entries for a field outside the send fields. Credentials are excluded from logs, exports, audit rows, and traces.

A Perplexity or TypeSafe provider with no send fields is rejected when you save it. If such a provider exists from an older version, the run records a typed `missing_send_fields` error and never calls the provider.

## Test button

**测试连通性** on a configured provider sends one controlled Acme/Acme pair through the provider. For a decision provider it shows the typed result: verdict, confidence, model, and provider kind. It shows an error code and message when the provider fails. A success is only shown when the provider returned a typed suggestion.

## Review semantics

A decision model only produces a review suggestion. It never merges or separates entities on its own:

- Rule conflicts, `veto_auto_merge` constraints, human `cannot-link` decisions, and confirmed rulings stay authoritative over a model suggestion.
- A model `SAME` defaults to `REVIEW`, not an automatic merge; a `DIFFERENT` suggestion does not create a persistent rejection on its own.
- Timeout, bad JSON, missing credentials, and provider outages follow the scheme failure policy: `REVIEW` routes the pair to human review, `FAIL` fails the run. Retries are bounded.
- The review panel shows the model verdict badge, confidence, the probability distribution, provider kind and key, model and prompt versions, and latency. The UI only displays the server values; it does not make its own identity decision.

## Limitations

- Confidence is not a calibrated probability and is not used as an automatic merge threshold.
- Distribution and explanation may be absent from a provider; they are displayed as unknown rather than fabricated.
- Real Perplexity and TypeSafe smoke tests are BLOCKED until API keys are available; see [decision-model-providers](../reference/decision-model-providers.md) and [decision-model-acceptance](../reference/decision-model-acceptance.json).
- The independent holdout and evaluator are frozen. A real TypeSafe Jev run made 29 calls with 0 errors. See [decision-model-evaluation](../reference/decision-model-evaluation.md). A Perplexity real smoke remains BLOCKED without a key.
