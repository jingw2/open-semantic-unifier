# Contributing

Issues and pull requests are welcome. Before proposing a product behavior change, read the [acceptance baseline](docs/reference/acceptance-baseline.md) and identify the user-visible behavior and evidence affected.

## Development checks

```bash
python -m pip install -r backend/requirements.txt
python -m pytest backend/tests -q
npm --prefix frontend ci
npm --prefix frontend run build
python3 scripts/golden.py validate
python3 -m unittest discover -s tests -p 'test_golden_contract.py' -v
```

Do not modify Golden expected outcomes merely to make a change pass. If test data or an acceptance rule needs to change, explain the domain reason and update its versioned documentation and evidence together. Keep credentials, customer data, and generated `.env` files out of commits.

## Pull requests

- Describe the user task and changed observable behavior.
- List the tests and real services exercised; mark unavailable services as untested.
- Include migration and rollback notes for schema changes.
- Update user documentation and acceptance evidence when behavior changes.
- Do not claim L3/L4 production support or full V0.1 acceptance without the corresponding gates.

Report defects and feature requests through the repository's GitHub Issues page. Please include the version, deployment type, redacted logs, and a minimal reproduction; never include API keys or personal data.
