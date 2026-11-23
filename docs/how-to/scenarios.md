# Example scenarios

The Golden Set contains fictional, versioned fixtures for three distinct identity problems. These examples are configurations, not hard-coded production truth.

| Scenario | Identity grain | Example fields | Main ambiguity |
| --- | --- | --- | --- |
| Supplier | Legal entity | Tax identifier, legal name, address | Similar names and shared contacts do not prove identity |
| Customer | Person | Customer ID, normalized name, phone/email | Shared or recycled contact details need review |
| Product | Sellable specification | GTIN-like fixture, brand, model, pack size | Similar descriptions with different specifications stay distinct |

To try one, create a project, open **实体统一工作台**, choose the entity type and field map, import the corresponding records, inspect candidate evidence, and submit the batch for review. Review cases before publishing a snapshot. Each export must refer to the same explicit snapshot ID.

To add another domain, create a project scheme with its own entity type, identity grain, field map, normalizers, candidate rules, conflict constraints, and attribute priorities. Do not copy a sample's field assumptions without checking that they match the new domain.

The public Golden fixture evaluator checks fixture integrity and expected pair actions. It does not run the workbench or prove production quality. Product-level Golden and lifecycle gate results are tracked separately in [acceptance status](../reference/acceptance-status.json).
