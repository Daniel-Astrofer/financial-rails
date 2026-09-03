<!--
Kerosene documentation metadata
status: review-required
audience: internal
owner: rails
source_of_truth: rails
last_reviewed: 2026-09-03
-->

# Kerosene Rails

Owner: adapters between Kerosene services and external payment rails.

- `bitcoin_core_backend`: Bitcoin Core HTTP facade; 17 tests.
- `lightning_flask`: LND HTTP facade; 32 tests.

Each adapter is an independent Python process. Deploy owns image and runtime
configuration; Core consumes the adapter contract and must not embed this
source.

Pending: define immutable images in `kerosene-deploy`, add contract tests with
KFE and create the GitHub remote. mTLS is intentionally outside this migration.

Documentation map: [portal](../README.md) · [status](../STATUS.md) ·
[API catalog](../reference/API_CATALOG.md) · [quickstart](../QUICKSTART.md).
