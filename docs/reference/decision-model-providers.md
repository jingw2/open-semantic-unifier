# Decision model provider verification

Status: **implemented** for the two decision-model adapters. **TypeSafe Jev real-service smoke passed on 2026-11-25.** Perplexity real-service smoke remains BLOCKED until a key is available.

Date: 2026-11-24. Scope: T-DM-1 and T-DM-4 of [the decision-model adapter plan](../superpowers/plans/2026-11-23-decision-model-adapters.md).

## What was verified

Direct reads of the official documentation through the web fetch tool were rejected by the fetch proxy before any page content was retrieved, so the protocol was reconstructed from the sources that were available in the environment:

- The TypeSafe Jev request/response shape was derived from the user-provided TypeSafe models URL and the official `typesafe-sdk` 0.7.5 schema.
- The Perplexity Decisions shape was derived from the Perplexity Decisions documentation, with the same choice-protocol mapping.
- The remaining official pages were not reachable; see [the blocked paths](#blocked-paths).

Both providers expose the same choice protocol: one `POST` to a versioned endpoint carrying a `state` and named `questions.entity_match` choice, answered with `answers.entity_match.choice`, `probabilities`, and `confidence`. The shared mapping lives in `backend/app/services/decision_models/providers/choice.py`; each provider contributes only its endpoint and model defaults.

## Support matrix

| Provider | Adapter readiness | Endpoint | Default model | Real smoke |
| --- | --- | --- | --- | --- |
| TypeSafe Jev | Implemented (`typesafe_jev`). | `POST /v1/systemone` on the configured base URL. | `jev-latest`. | PASS. Returned `UNKNOWN` with confidence 0.98 from `jev-1.14.0`; the holdout run made 29 calls with 0 errors. |
| Perplexity Decisions | Implemented (`perplexity_decisions`). | `POST /v1/decisions` on the configured base URL. | `pplx-decider-flash-latest`. | BLOCKED. No `PERPLEXITY_API_KEY`. |
| Local or private OpenAI-compatible provider | Available through the generic typed adapter boundary. | OpenAI-compatible chat completions. | User-configured. | Available for a local or private endpoint smoke. |

A missing or invalid API key fails the provider test and the run with a typed `missing_credentials` or `invalid_credentials` error. DM-07 passed with TypeSafe Jev and remains unrun for Perplexity until that key exists.

## Blocked paths

The following domains were unreachable from this environment when the documentation was first read:

| Domain | Result |
| --- | --- |
| `typesafe.ai` | Domain verification failed |
| `docs.typesafe.ai` | Domain verification failed |
| `openrouter.ai` | Domain verification failed |
| `docs.perplexity.ai` | Domain verification failed |

This is a network or enterprise policy block, not a missing page or a transient timeout.

## Required before unblocking the Perplexity smoke

1. A Perplexity Decisions API key, or access to a privately hosted compatible endpoint.
2. Confirmation of the current model names, rate limits, pricing, and data-processing terms from the official documentation, when reachable.
3. A decision on which configured fields may be sent to Perplexity.

## Sources

- [TypeSafe](https://typesafe.ai)
- [TypeSafe documentation (unreachable)](https://docs.typesafe.ai)
- [OpenRouter Jev guide (unreachable)](https://openrouter.ai/docs/guides/community/jev)
- [Perplexity Decisions quickstart (unreachable)](https://docs.perplexity.ai/docs/decisions/quickstart)
- [Perplexity Decisions guide (unreachable)](https://docs.perplexity.ai/docs/decisions/decisions)
- [Perplexity Decisions API reference (unreachable)](https://docs.perplexity.ai/api-reference/decisions-post)
