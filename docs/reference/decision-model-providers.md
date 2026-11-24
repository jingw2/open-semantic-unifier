# Decision model provider verification

Status: **BLOCKED** for official verification. No provider-specific adapter code may be written until the official documentation is read and the missing conditions are resolved.

Date: 2026-11-24. Scope: T-DM-1 of [the decision-model adapter plan](../superpowers/plans/2026-11-23-decision-model-adapters.md).

## What was attempted

Direct reads of the official documentation were attempted through the web fetch tool. Every request was rejected by the fetch proxy before any page content was retrieved:

| Domain | Result |
| --- | --- |
| `typesafe.ai` | Domain verification failed |
| `docs.typesafe.ai` | Domain verification failed |
| `openrouter.ai` | Domain verification failed |
| `docs.perplexity.ai` | Domain verification failed |

This is a network or enterprise policy block, not a missing page or a transient timeout. Repeating the same URLs from this environment will fail the same way. A public search index returned summaries and URLs, but those summaries are not the official documents and are not evidence for an adapter contract.

## Support matrix

| Provider | Official docs verified | Adapter readiness | Missing conditions |
| --- | --- | --- | --- |
| TypeSafe Jev | No. `typesafe.ai`, `docs.typesafe.ai`, and the OpenRouter guide were unreachable. | BLOCKED. Do not implement a provider adapter. | Official docs access; API key; confirmed request/response schema; official data-policy URL; model version availability. |
| Perplexity Decisions | No. `docs.perplexity.ai` was unreachable. | BLOCKED. Do not implement a provider adapter. | Official docs access; API key; confirmed request/response schema; official data-policy URL; model version availability. |
| Local or private OpenAI-compatible provider | Yes. The existing repository path is already implemented and tested. | Available for the generic typed adapter boundary. | A real local or private model endpoint for a connectivity smoke. |

## Search-index leads, not verified

The following facts appeared in a public search result summary. They are leads for the verification pass. They are not an adapter contract.

### TypeSafe Jev

- Intended official entry points: `typesafe.ai`, `docs.typesafe.ai`, `console.typesafe.ai`.
- Candidate endpoints reported by the index: `POST https://api.typesafe.ai/v1/systemone` and `POST https://api.typesafe.ai/v1/decisions`. The index also reports an OpenRouter route and a chat-completions rejection.
- Candidate model names: `jev-1.13.0` and `jev-1.14`; context limits reported as roughly 32K/64K and 262K.
- Candidate primitives: `Choice`, `Noul`, `Score`, and `Box`.
- Candidate auth: `TYPESAFE_API_KEY` through the reported Python/JavaScript SDKs.
- Candidate response fields: probability distribution, confidence, usage, and cost.
- Candidate data policy: not found in the search summary.
- Reported caveat: confidence reflects decisiveness, not correctness. Calibration does not guarantee an individual answer.

### Perplexity Decisions

- Intended official pages: `https://docs.perplexity.ai/docs/decisions/quickstart`, `https://docs.perplexity.ai/docs/decisions/decisions`, and `https://docs.perplexity.ai/api-reference/decisions-post`.
- Candidate endpoint: `POST https://api.perplexity.ai/v1/decisions`.
- Candidate auth: `Authorization: Bearer <PERPLEXITY_API_KEY>`; an `x-api-key` header is reported as ignored.
- Candidate model names: `pplx-decider-latest`, `pplx-decider-flash-latest`, `pplx-decider-v1.2`, `pplx-decider-v1.1-27b`.
- Candidate request shape: `model`, `state`, and 1 to 128 named `questions`; question types `noul`, `choice`, `score`, and `locate`.
- Candidate limits: 10 requests per second per organization, roughly 262K input tokens, 32 MiB request body, `x-ratelimit-*` headers, and 429 with `Retry-After`.
- Candidate SDKs: Python `client.decisions.create` and Node `@perplexity-ai/perplexity_ai`.
- Candidate data policy: not found in the search summary.

## Decision

Do not write TypeSafe Jev or Perplexity provider-specific adapters yet. The official documentation was not read, so endpoint, auth, request schema, response schema, model naming, limits, pricing, and data terms are unconfirmed.

The provider-agnostic work can proceed separately under T-DM-2 and T-DM-3: typed `SAME` / `DIFFERENT` / `UNKNOWN` contracts, the disabled-versus-error distinction, and the adapter boundary. Those units do not need a provider-specific protocol. The existing OpenAI-compatible local or private path remains the only provider path with current evidence.

## Required before unblocking

1. Network or proxy access to the official documentation, or the official pages pasted or exported for review.
2. Official documentation for each provider: endpoint, auth, request and response schema, model versions, limits, pricing, and data-processing terms.
3. API keys for a real connectivity smoke. Without them, the real-service acceptance remains BLOCKED.
4. A decision on which provider is first, and whether external fields may be sent to it.

## Sources

- [TypeSafe](https://typesafe.ai)
- [TypeSafe documentation (unreachable)](https://docs.typesafe.ai)
- [OpenRouter Jev guide (unreachable)](https://openrouter.ai/docs/guides/community/jev)
- [Perplexity Decisions quickstart (unreachable)](https://docs.perplexity.ai/docs/decisions/quickstart)
- [Perplexity Decisions guide (unreachable)](https://docs.perplexity.ai/docs/decisions/decisions)
- [Perplexity Decisions API reference (unreachable)](https://docs.perplexity.ai/api-reference/decisions-post)
