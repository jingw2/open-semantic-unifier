# OpenRefine cleaning and W3C JSON-LD

Open Semantic Unifier can run a small, built-in OpenRefine recipe against one uploaded table, show a before/after diff, and only create a new data source after you adopt the result. Contract versions can then export LinkML, JSON Schema, and JSON-LD instances from that cleaned CSV.

## Runtime

OpenRefine is an internal Compose sidecar. It does not publish a port. OpenRefine has no official Docker image, so the repository builds `openrefine/Dockerfile` from the official GitHub release tarball and verifies the release SHA-256. The base image is pinned by digest.

```bash
docker compose up -d --build openrefine backend worker
```

The backend reads `OPENREFINE_URL` (default `http://openrefine:3333`) and `OPENREFINE_ENABLED` (Compose sets it to `true`). If the sidecar is unreachable, the workbench reports `OpenRefine 不可用，已降级为规则清洗` and offers the existing rule-cleaning path.

## Limits and fallback

OpenRefine runs only when the selected source is one table with at most:

- 50,000 rows
- 10 MiB
- 256 columns

Anything larger or multi-table is never sent to OpenRefine. Small single tables fall back to rule cleaning; multi-table or larger data stays on Splink + DuckDB.

## Preview, adopt, cancel

1. Select a file data source and open 处理方法.
2. In the `字段设置` card, pick a recipe and map its fields to the source columns. The field selects start from the detected columns, and the per-column recipe offers a checkbox group of all columns.
3. Choose `OpenRefine 预览`. The backend creates a temporary OpenRefine project and applies the selected built-in recipe with your field mapping.
4. The diff card shows changed rows, changed cells, row counts, column changes, and up to 20 before/after examples. The cleaned CSV is stored as a DRAFT artifact and can be viewed online.
5. `采用并生成新数据源` copies the cleaned CSV into a new DataSource. The original source and object are unchanged.
6. `撤销` deletes the temporary OpenRefine project. An adopted preview cannot be cancelled; adopting twice returns a conflict.

Every preview is recorded on the existing Job row with the recipe id, recipe version, operator, field mapping, selected columns, and diff statistics. No migration is required.

## Built-in recipes

Recipes are read-only, versioned JSON files under `backend/app/services/openrefine/recipes/`:

- 公司名称归一
- 空格归一
- 标点归一
- 去掉公司法律后缀
- 拆分联系方式
- 合并姓名字段
- 按列归一所选字段

The first six are field recipes: each declares required placeholders such as `name_col` or `contact_col`, and the workbench maps them to the source columns. `按列归一所选字段` is a per-column recipe: it repeats one trim-and-collapse operation over every selected column, defaulting to columns whose names match the built-in column terms (name, company, supplier, customer, 公司, 名称, 供应商, 客户).

Selection is based on the data profile. The workbench shows the selected recipe; the preview shows its name, version, and diff.

## JSON-LD export

In 契约统一工作台, open a model version and choose `JSON-LD`:

1. Pick a CSV data source.
2. Pick a class and its ID slot.
3. Map contract slots to CSV columns. The inferrer uses exact, case-insensitive name matching; unmatched slots stay empty.
4. `生成预览` validates the mapping and shows `@context` and `@graph` online.
5. `下载` saves the same document as `.jsonld`.

The ID slot must map to a column. Duplicate column mappings, unknown classes or slots, and a missing contract return a clear 404/422. The source CSV must be at most 50 MiB; at most 5,000 instances are generated per request and `truncated` reports whether more rows exist. Instance IDs are percent-encoded into `urn:osu:...` IRIs; rows without an ID get a distinct `urn:osu:autogen:...` identifier.

RDF/OWL is out of scope. W3C output is LinkML YAML, JSON Schema, and JSON-LD only.

## Standard roster for L1 alignment

L1 can align against an uploaded standard roster instead of the built-in demo catalog:

1. Upload a CSV or TSV with at least a name column. A column like `company_name`, `name`, `公司名称`, or `名称` is detected automatically; an ID column (`supplier_id`, `编号`, ...) is used when present, otherwise IDs are generated.
2. In 数据源中心, choose `设为标准花名册` on the CSV/TSV card. The backend auto-detects the id, name, alias, tax, and phone columns and records them under `routing_json.role == "standard_roster"` with a `routing_json.roster` field map. No database migration is involved.
3. Adjust the mapping in the inline editor (`ID 列`, `名称列`, `别名列`, `税号列`, `电话列`) and `保存字段`, or `取消花名册` to clear the role.
4. Run an L1 job. The latest roster in the project supplies the alignment catalog; alias columns, tax ids, and phone numbers all become aliases (split on `;；,，、|`), and the workbench result states `对齐来源：标准花名册 <source name>`. Without a roster, L1 keeps the demo-catalog behavior for demo sources and reports `演示花名册`.

Alias, tax, and phone values are split on `;；,，、|`, deduplicated, and exclude the canonical name. Rows without a name are skipped, and a roster with no usable rows fails the job rather than silently falling back.

## Verification

Fast tests use a controlled fake OpenRefine server:

```bash
python3 -m pytest backend/tests/test_openrefine_client.py backend/tests/test_openrefine_recipes.py \
  backend/tests/test_openrefine_api.py backend/tests/test_contract_jsonld.py -q
```

The real Compose proof is opt-in and uses an isolated project, ports, and volumes:

```bash
RUN_COMPOSE_RELEASE_TESTS=true python3 -m pytest backend/tests/integration/test_compose_openrefine.py -q
```

It uploads a dirty CSV, runs a real recipe, checks the diff, adopts the result, cancels a second preview, and verifies the temporary OpenRefine projects are gone.
