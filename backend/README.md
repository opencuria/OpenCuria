# Backend

The Django backend owns the control plane, agent harness, plugin policy, and
server-side credentials. See the root [source setup](../README.md)
for installation, migrations, and the single-worker ASGI startup command.

## Operations

- [Managed desktop resources](../docs/managed-desktop-resources.md): stdio plugin
  declarations, guarded migration/manual adoption, authenticated viewer intents,
  and coordinated rollout with backend, runner, and webapp.
- [MCP OAuth setup and operations](../docs/mcp-oauth.md).
- [Lifecycle recovery](../docs/image-lifecycle-implementation.md#recovery-hardening-operator-workflow).

## Codex harness

See the [Codex guide](../docs/codex.md) for supported ChatGPT/OpenRouter accounts,
permission semantics, trust boundaries, recovery and reproducible runtime tests;
the [engine README](apps/harness/engines/codex/README.md) maps internal contracts.
Deploy matching backend and runner versions. Install `requirements.txt` (including
the mandatory `pypdf==6.21.1` isolated PDF parser), then run migrations, including
`0037_provider_refresh_lease`, before starting ASGI. ChatGPT refresh is centralized
for native and Codex engines under database leases; no refresh token is sent to
the workspace. Preserve database/encryption keys for encrypted checkpoints.

Optional pinned runtime setup, from the repository root on Linux:

```bash
python3 scripts/fetch-codex-test-runtime.py
(cd backend && .venv/bin/pytest -q apps/harness/tests/test_codex_core*.py)
(cd runner && .venv/bin/pytest -q tests/test_codex_artifacts.py)
```

Artifacts live in ignored `.opencuria/codex-test-runtime/`. Tests use a local fake
Responses endpoint, not real provider credentials or paid inference. Real-runtime
cases skip if artifacts are absent; other contract tests still run. The runner's
real-package installer test currently targets x86_64.
