# Decision model evaluation

Status: the holdout and evaluator are frozen, and a real TypeSafe Jev run exists. The latest report is [decision-model-evaluation-report.json](decision-model-evaluation-report.json).

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

Fill the key in the workbench under **模型 Provider**: choose the **TypeSafe Jev** preset, enter the API key, set the send fields, and save. The evaluator can read that saved provider by id. It decrypts the key in process and never writes it to the report.

```bash
python3 scripts/evaluate_decision_models.py \
  --provider-id <saved-provider-id> \
  --send-fields name,tax_id,email,gtin,brand,model,voltage,pack_count \
  --output docs/reference/decision-model-evaluation-report.json
```

For a provider that is not saved in the workbench, pass `--provider-kind`, `--model`, `--base-url`, and export `TYPESAFE_API_KEY` or `PERPLEXITY_API_KEY`.

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

The latest TypeSafe Jev run made 29 real calls with 0 errors. It used the frozen holdout 1.0.0 and the send fields listed in the report.

| Metric | Result |
| --- | --- |
| Candidate recall | 96.7% (29 of 30) |
| SAME recall | 66.7% (12 of 18) |
| DIFFERENT recall | 66.7% (6 of 9) |
| False SAME | 0 |
| UNKNOWN rate | 33.3% (9 of 27 judged pairs) |
| Suggestion accuracy | 66.7% (18 of 27) |
| Review volume | 13 pairs |
| False merges | 0 |
| Mean latency | 598.75 ms |

Per-domain accuracy was 55.6% for vendor, 44.4% for contact, and 100% for catalog item. The domain table stays in the report so the weak domains are visible. The full report, including every domain's counts and latency percentiles, is [decision-model-evaluation-report.json](decision-model-evaluation-report.json).

The model is conservative. It never suggested SAME on a DIFFERENT pair, and it answered UNKNOWN on about a third of judged pairs. Do not read the accuracy as a calibrated probability. A hosted model can vary between runs, so repeat the run before treating small differences as signal.

## Limits

- The holdout is synthetic. It shows behavior on authored cases, not customer data.
- A hosted model can return different verdicts between runs. Repeat a run before treating small differences as signal.
- A hosted model's training data cannot be audited. The frozen set prevents our own tuning leakage, not all possible training contamination.
- Confidence is not a calibrated probability, and no automatic merge threshold uses it.
- A passing report does not change the V0.1 or L3 product status.
