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
