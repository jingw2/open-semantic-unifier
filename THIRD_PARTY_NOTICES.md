# Third-party software notices

The project installs Python and JavaScript dependencies listed in `backend/requirements.txt`, `backend/requirements-observability.txt`, and `frontend/package-lock.json`. Their copyright and license terms remain with their respective authors. Runtime containers (PostgreSQL, Redis, MinIO, Phoenix, Python, Node.js, and Nginx) also carry separate license terms. This repository references pinned images but does not relicense them.

## Audit status

`docs/reference/license-audit-summary.json` records the 2026-11-23 audit. It covers 182 Python packages, 184 npm packages, and the pinned container images. Every Python and npm package now has an identifiable license. The repository license is MIT, which permits commercial use.

The audit flags these obligations:

- `rfc3987` 1.3.8 uses GPL-3.0-or-later. It is transitive through LinkML. A combined binary distribution may require GPL compliance.
- `psycopg2-binary` 2.9.13 uses LGPL with exceptions.
- `certifi`, `fqdn`, and `tqdm` use MPL-2.0 terms.
- The Compose deployment uses `bitnamilegacy/minio:latest` for MinIO 2025.5.24. MinIO uses AGPL-3.0. The official `minio/minio` image is no longer anonymously pullable, and the Bitnami legacy namespace may not receive updates. Review obligations before redistributing a modified image or offering it as a hosted service, and plan a maintained replacement.
- The Phoenix image uses Elastic License 2.0, which limits providing it to others as a managed service. Phoenix is optional and self-hosted.
- Redis 7.4.11 uses RSALv2 or SSPLv1, not the earlier BSD terms.

The audit records package metadata and upstream license files. It is not a legal opinion.

This notice is an inventory, not a substitute for checking the full resolved transitive dependency tree when creating a binary or hosted distribution. CI validates installation and tests but does not generate a machine-verified SBOM.
