# Provider accounts

**Settings → Provider & Models → Manage** lists named accounts for each native
provider and Claude Agent. Members can create and manage personal accounts in the
current organization. Only organization admins can create or manage shared
accounts. Even admins cannot inspect another member's personal accounts.

Each member independently selects one active account per provider, or **Use no
account**. The first available account is selected automatically; additional
accounts never replace a selection or an explicit empty choice. A deleted or
unshared selection becomes empty, not an automatic fallback to another account.
The stored selection distinguishes an absent choice, explicit none, and a revoked
choice; choosing an account or none explicitly clears its revocation marker.

The selected accounts are captured at the start of each answer. Changing the
selection affects the next message, including follow-ups in an existing chat,
not an answer already running. Subagents, grounding, compaction, session titles
and summaries inherit the answer's account snapshot. Claude Agent resumes the
conversation transcript while resolving the current account for each new turn.

Deleting an account or withdrawing its organization share requests cancellation
of dependent answers and their background work. The owning worker performs the
existing provider/engine cleanup. Post-commit notifications wake local workers;
a database check (every 0.5 seconds while active) also handles other workers.
Cancellation is not a refund or a recall of a request already accepted by a
provider. No response is retried using a different account. After unsharing, the
acting admin becomes the account's personal owner; the UI requires confirmation.

## API and storage

- `GET /api/v1/provider-config/accounts/`: safe metadata for visible accounts.
- `POST /api/v1/provider-config/accounts/{provider}/`: create a named account.
- `PUT` / `DELETE /api/v1/provider-config/accounts/{provider}/{id}/`: manage the
  exact account. ChatGPT accounts are created using the device OAuth endpoints.
- `PUT /api/v1/provider-config/selection/{provider}/` with `account_id` (UUID or
  `null`): select an account or none. The choice is scoped to the caller and org.
- MCP tools `list_provider_accounts`, `save_provider_account`,
  `delete_provider_account`, `select_provider_account` use the same service.

ChatGPT device flows return a `flow_id`; status/cancel must address that flow.
Flows are durable, user/org-scoped, expire after ten minutes, and use fenced
polling claims so concurrent tabs cannot consume each other's authorizations.

Secrets live in the existing encrypted Credential vault under internal
`provider_secret` services. These are excluded from generic credential catalogs,
public CRUD, workspace attachments and plugin credential resolution. Tokens and
keys are never returned by account endpoints. Native credentials remain
backend-only; Claude tokens are supplied narrowly to the managed CLI process as
documented in [Claude Agent](claude-agent.md).

Apply the normal Django migration chain before starting the updated backend.
Existing native connections retain their IDs and org-wide availability. Existing
Claude connections keep their IDs and personal scope; historical session links
remain, but no longer determine future-turn authorization. Migration of encrypted
secrets requires the same configured encryption key as the old installation.

## Verification

`e2e/run-provider-accounts.sh` starts a disposable database and loopback-only model
server and tests the real API/settings/chat flows with member/admin browsers.
It never contacts a production provider or needs real credentials. See
[focused E2E instructions](../e2e/README.focused.md).
