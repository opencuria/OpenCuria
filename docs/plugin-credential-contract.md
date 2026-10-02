# Workspace plugin and credential selection contract

Workspace creation accepts `plugin_ids` (default `[]`) and `credential_ids`
(default `[]`). Workspace PATCH accepts either field as optional: omitted means
unchanged; an explicit empty array clears that selection. Plugin activation is
independent of credential/service activation. A single credential per service
may be attached to a workspace and may satisfy multiple selected plugins.

Both REST (`POST /api/v1/workspaces/`, `PATCH /api/v1/workspaces/{id}/`) and
MCP (`create_workspace`, `update_workspace`) use these same selection rules.
REST returns the persisted `plugin_ids` in `WorkspaceOut`, `WorkspaceCreateOut`,
and `WorkspaceUpdateOut`; the corresponding MCP responses include both final
`plugin_ids` and `credential_ids`.

When a selected plugin has a missing required credential—including an OAuth
credential without a connected, endpoint-matching grant—the selection is
rejected with HTTP 409 and `code: "missing_plugin_credentials"`. The optional
`gaps` array contains only safe metadata objects with `plugin_id`,
`plugin_name`, `key`, `service_id`, `service_name`, and `credential_type`. MCP
returns the same `code` and `gaps` object. Optional requirements do not block.
The legacy plugin-write route/tool is removed; selection is only changed with
workspace create/update.

Workspace update response fields include
`credential_sync_status: "synced" | "pending" | "failed" | "not_required"`
and optional safe `credential_sync_detail`. The configuration transaction is
committed before ordinary credential injection. `credentials_present` means
ordinary env/file/SSH material was acknowledged as installed by the runner, not
merely selected in the database. Creation sends ordinary material with the
create task and records the runner's `credentials_present` acknowledgement;
updates preserve the last acknowledged value until a runner confirms an inject.
Failed or pending sync preserves that value; heartbeat reconciliation retries
when disk presence differs from selected ordinary credential material.
OAuth grants remain server-side; OAuth-only association changes need no runner
injection.
