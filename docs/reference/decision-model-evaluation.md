# Decision model evaluation

Status: harness and holdout frozen. A real evaluation run remains **BLOCKED** until a Perplexity or TypeSafe credential exists. No model quality score is reported yet.

## Why this holdout

The evaluation uses a synthetic, authored holdout at `tests/holdout/v1`. It is not a split of the Golden set. The Golden set already guided rules, prompts, and fixtures, so a split would measure tuning, not generalization.

A public entity-resolution benchmark would need a schema and label mapping that this product does not have, and its records may already be in a hosted model's training data. That contamination cannot be ruled out. The authored holdout is frozen before any real run, so its records cannot influence prompt or threshold choices.

Every value is fictional. Names, addresses, phone numbers, emails, tax identifiers, and product identifiers use reserved or obviously synthetic forms.

## Files and size

| Domain | Entity type | Records | Labeled pairs |
| --- | --- | --- | --- |
| `vendor` | organization | 20 | 10 |
| `contact` | person | 20 | 10 |
| `catalog_item` | product variant | 20 | 10 |

Each domain has `records.json`, `scheme.json`, and `expected.json`. `manifest.json` records the version, freeze date, counts, and a SHA-256 for every file.

The scenarios cover a trusted identifier match, name-only variants, identifier conflicts, shared phones and emails, transliterations, missing identifiers, sparse records, attribute conflicts, and one pair that the candidate rules are expected to miss. Truth values are `SAME`, `DIFFERENT`, or `UNRESOLVED`.

## Leakage controls

- Truth, expected action, case id, and the review reason live only in `expected.json`. The matcher and the provider never read that file.
- The adapter receives candidate attributes, the configured send fields, and matcher evidence. It never receives a truth label.
- `validate_holdout` rejects a label key in `records.json` or `scheme.json`.
- `assert_no_golden_overlap` rejects any shared record ref, source key, case id, or attribute value with the Golden set.
- CI validates the manifest, runs the overlap check, and runs the evaluator against a controlled adapter. The controlled run proves the scoring path only, not model quality.
- The provider credential comes from the encrypted provider configuration or from `TYPESAFE_API_KEY` / `PERPLEXITY_API_KEY`. The report never contains it.

The holdout is frozen for evaluation. Do not use it to tune a prompt, threshold, rule, or model choice. A change to the fixture requires a new version and new manifest hashes.

## Run the evaluation

Validate the frozen fixture first. This needs no credential.

```bash
python3 scripts/evaluate_decision_models.py --validate-only
```

Fill the key in the workbench under **模型 Provider**: choose the **TypeSafe Jev** preset, enter the API key, set the send fields, and save. For the CLI, export the same key instead.

```bash
export TYPESAFE_API_KEY=...
python3 scripts/evaluate_decision_models.py \
  --provider-kind typesafe_jev \
  --model jev-latest \
  --send-fields name,tax_id,email,gtin,brand,model,voltage,pack_count \
  --output docs/reference/decision-model-evaluation-report.json
```

The command prints a `BLOCKED` report and exits 2 when no key exists. It exits 0 when the run completes and 1 when the provider fails enough calls that no usable report exists.

## Metrics

Each domain reports its own figures. The aggregate repeats them, and a failure in one domain stays visible.

- **Candidate recall.** Required candidate pairs that the candidate stage actually produced.
- **Same recall.** Truth `SAME` candidate pairs where the model answered `SAME`.
- **Different recall.** Truth `DIFFERENT` candidate pairs where the model answered `DIFFERENT`.
- **False SAME rate.** Truth `DIFFERENT` pairs where the model answered `SAME`. A false `SAME` adds review work. It never merges, because the product does not use a model score as an automatic merge.
- **Unknown rate.** Labeled candidate pairs where the model answered `UNKNOWN`.
- **Suggestion accuracy.** Correct `SAME` or `DIFFERENT` answers divided by labeled candidate pairs that received a verdict.
- **Review volume.** Pairs routed to human review by the deterministic action, including pairs the candidate stage missed.
- **False merges.** Deterministic `AUTO_MATCH` results on truth `DIFFERENT`. This figure must stay at zero.
- **Latency and cost.** Mean and p95 latency and reported cost per domain. Missing provider usage stays missing, not estimated.

`UNRESOLVED` pairs are reported but excluded from accuracy. Provider errors are reported separately and are not counted as wrong verdicts.

## Current result

No real provider run exists. DM-06 stays **BLOCKED**. The frozen fixture, the overlap check, and the scoring path are ready, and the controlled-adapter test fixes the expected metric shape. A real report lands in `docs/reference/decision-model-evaluation-report.json` after a run with a real credential.

## Limits

- The holdout is synthetic. It shows behavior on authored cases, not customer data.
- A hosted model's training data cannot be audited. The frozen set prevents our own tuning leakage, not all possible training contamination.
- Confidence is not a calibrated probability, and no automatic merge threshold uses it.
- A passing report does not change the V0.1 or L3 product status.
