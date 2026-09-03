# Agent guide — Kerosene Rails

## Scope

This repository owns independent Bitcoin Core and LND adapters. It does not own
Auth, KFE, Vault, deployment manifests or secrets.

## Documentation

- Start at `docs/README.md` and keep cross-adapter facts in `docs/reference/`.
- Component-local READMEs may remain beside source when they document that
  component's build or runtime; link them from the portal.
- Keep API exposure and consumer contracts in the catalog, not in duplicate
  adapter prose.

## Safety and integration

- Preserve existing HTTP contracts during extraction.
- Never commit credentials, macaroons, TLS keys or environment files.

## Verification

Run adapter tests in isolated Python environments and update the API catalog for
route or authentication changes.
