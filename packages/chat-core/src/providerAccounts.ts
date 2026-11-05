/**
 * Model provider accounts shared by the web and native settings: provider
 * metadata, the editable account form, its payload and the ChatGPT device
 * login outcomes. Secrets are never prefilled; blank secrets keep the
 * stored values on update.
 */

/** Providers with named accounts; `claude-agent` runs the Claude engine. */
export type ProviderAccountProvider =
  | "openrouter"
  | "chatgpt"
  | "amazon-bedrock"
  | "openai-compatible"
  | "claude-agent";

/** Account metadata visible to the caller; secrets are never included. */
export interface ProviderAccount {
  id: string;
  provider: ProviderAccountProvider;
  label: string;
  scope: "personal" | "organization";
  connected: boolean;
  is_active: boolean;
  can_manage: boolean;
  base_url?: string;
  api_key_hint?: string;
  account_id?: string;
  region?: string;
  auth_method?: string;
  models?: string[];
  auth_type?: "api_token" | "subscription_token" | string;
}

/** Create/update body; empty secrets keep the stored values on update. */
export interface ProviderAccountInput {
  label: string;
  shared: boolean;
  api_key?: string;
  base_url?: string;
  /** Manual model ids (OpenAI-compatible only); `[]` clears the list. */
  models?: string[];
  auth_method?: string;
  region?: string;
  access_key_id?: string;
  secret_access_key?: string;
  session_token?: string;
  bearer_token?: string;
  token?: string;
  auth_type?: "api_token" | "subscription_token";
}

export interface ChatGptOAuthStatus {
  status:
    | "pending"
    | "connected"
    | "expired"
    | "denied"
    | "cancelled"
    | "no_flow";
  account_id?: string;
}

export interface ProviderAccountInfo {
  id: ProviderAccountProvider;
  name: string;
  /** Shown on the provider row while it has no accounts. */
  description: string;
  /** Catalog `provider` of this provider's models, for model counts. */
  catalogProvider: string;
}

/** Providers in settings order. */
export const PROVIDER_ACCOUNT_PROVIDERS: ProviderAccountInfo[] = [
  {
    id: "openrouter",
    name: "OpenRouter",
    description: "Route requests through OpenRouter with your API key.",
    catalogProvider: "openrouter",
  },
  {
    id: "chatgpt",
    name: "OpenAI ChatGPT",
    description: "Use your ChatGPT Plus or Pro subscription via device login.",
    catalogProvider: "chatgpt",
  },
  {
    id: "amazon-bedrock",
    name: "Amazon Bedrock",
    description:
      "Connect AWS credentials or a bearer token for Bedrock models.",
    catalogProvider: "amazon-bedrock",
  },
  {
    id: "openai-compatible",
    name: "OpenAI Compatible",
    description:
      "Custom OpenAI-compatible endpoint for self-hosted models (e.g. HuggingFace UI-TARS).",
    catalogProvider: "openai-compatible",
  },
  {
    id: "claude-agent",
    name: "Claude Agent",
    description:
      "Run the separate Claude Agent harness with an Anthropic account.",
    catalogProvider: "claude",
  },
];

export const DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";
export const DEFAULT_BEDROCK_REGION = "us-east-1";

/** Metadata of one provider, if it is known. */
export function providerAccountInfo(
  id: string | null | undefined,
): ProviderAccountInfo | undefined {
  return PROVIDER_ACCOUNT_PROVIDERS.find((entry) => entry.id === id);
}

/**
 * How a provider row describes its accounts: the description without
 * accounts, "2 accounts · None selected", or the active account and scope.
 */
export function providerAccountsSummary(
  provider: ProviderAccountInfo,
  accounts: Array<Pick<ProviderAccount, "label" | "scope" | "is_active">>,
): string {
  if (!accounts.length) return provider.description;
  const active = accounts.find((account) => account.is_active);
  if (!active)
    return `${accounts.length} ${accounts.length === 1 ? "account" : "accounts"} · None selected`;
  return `${active.label} · ${active.scope === "organization" ? "Shared" : "Personal"}`;
}

/** Distinct catalog models of a provider across the engine catalogs. */
export function providerModelCount(
  provider: ProviderAccountInfo,
  catalogs: Array<Array<{ id: string; provider?: string }>>,
): number {
  const ids = new Set<string>();
  for (const catalog of catalogs)
    for (const model of catalog)
      if (model.provider === provider.catalogProvider) ids.add(model.id);
  return ids.size;
}

/** Model ids from one-per-line text: trimmed, without blanks or duplicates. */
export function parseModelList(text: string): string[] {
  return [
    ...new Set(
      text
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ];
}

/** The editable fields of every provider; only the provider's own are sent. */
export interface ProviderAccountForm {
  label: string;
  shared: boolean;
  /** OpenRouter and OpenAI-compatible. */
  apiKey: string;
  baseUrl: string;
  /** OpenAI-compatible model ids, one per line. */
  modelsText: string;
  bedrockAuthMethod: "access_keys" | "bearer";
  region: string;
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
  bearerToken: string;
  claudeAuthType: "api_token" | "subscription_token";
  claudeToken: string;
}

/** A blank form for a new account, or an existing account's non-secret fields. */
export function providerAccountForm(
  provider: ProviderAccountProvider,
  account: Pick<
    ProviderAccount,
    | "label"
    | "scope"
    | "base_url"
    | "models"
    | "auth_method"
    | "region"
    | "auth_type"
  > | null = null,
): ProviderAccountForm {
  return {
    label: account?.label ?? "",
    shared: account?.scope === "organization",
    apiKey: "",
    baseUrl:
      account?.base_url ||
      (provider === "openrouter" ? DEFAULT_OPENROUTER_BASE_URL : ""),
    modelsText: (account?.models ?? []).join("\n"),
    bedrockAuthMethod:
      account?.auth_method === "bearer" ? "bearer" : "access_keys",
    region: account?.region || DEFAULT_BEDROCK_REGION,
    accessKeyId: "",
    secretAccessKey: "",
    sessionToken: "",
    bearerToken: "",
    claudeAuthType:
      account?.auth_type === "subscription_token"
        ? "subscription_token"
        : "api_token",
    claudeToken: "",
  };
}

/**
 * The create or update body for a provider's form, or the reason it cannot
 * be saved. Only admins can share; `account` is the account being edited.
 */
export function providerAccountPayload(
  provider: ProviderAccountProvider,
  form: ProviderAccountForm,
  options: {
    isAdmin: boolean;
    account?: Pick<ProviderAccount, "connected" | "auth_type"> | null;
  },
): { payload: ProviderAccountInput } | { error: string } {
  const base: ProviderAccountInput = {
    label: form.label.trim(),
    shared: options.isAdmin && form.shared,
  };
  if (!base.label) return { error: "Account name is required." };
  switch (provider) {
    case "openrouter":
      return {
        payload: {
          ...base,
          api_key: form.apiKey,
          base_url: form.baseUrl.trim() || DEFAULT_OPENROUTER_BASE_URL,
        },
      };
    case "openai-compatible":
      if (!form.baseUrl.trim()) return { error: "Base URL is required." };
      return {
        payload: {
          ...base,
          api_key: form.apiKey,
          base_url: form.baseUrl.trim(),
          models: parseModelList(form.modelsText),
        },
      };
    case "amazon-bedrock": {
      const region = form.region.trim() || DEFAULT_BEDROCK_REGION;
      return {
        payload:
          form.bedrockAuthMethod === "bearer"
            ? {
                ...base,
                auth_method: "bearer",
                region,
                bearer_token: form.bearerToken,
              }
            : {
                ...base,
                auth_method: "access_keys",
                region,
                access_key_id: form.accessKeyId,
                secret_access_key: form.secretAccessKey,
                session_token: form.sessionToken,
              },
      };
    }
    case "claude-agent": {
      const token = form.claudeToken.trim();
      const account = options.account;
      if (!account?.connected && !token)
        return { error: "Enter an Anthropic token to connect this account." };
      if (
        account?.connected &&
        account.auth_type !== form.claudeAuthType &&
        !token
      )
        return {
          error: "Enter a token when changing the authentication type.",
        };
      return { payload: { ...base, auth_type: form.claudeAuthType, token } };
    }
    case "chatgpt":
      return { payload: base };
  }
}

/**
 * Unsharing moves an organization account to the acting admin: the body
 * keeps the settings but sends no secrets, so the stored ones are kept.
 */
export function unsharePayload(
  provider: ProviderAccountProvider,
  payload: ProviderAccountInput,
): ProviderAccountInput {
  const next: ProviderAccountInput = { ...payload, shared: false };
  if (provider === "openrouter" || provider === "openai-compatible")
    next.api_key = "";
  if (provider === "amazon-bedrock") {
    delete next.access_key_id;
    delete next.secret_access_key;
    delete next.session_token;
    delete next.bearer_token;
  }
  if (provider === "claude-agent") next.token = "";
  return next;
}

/**
 * What a ChatGPT device-login status means: keep polling, connected, back
 * to the Connect button (cancelled), or a message to show.
 */
export function chatGptOAuthOutcome(
  status: ChatGptOAuthStatus["status"],
): "pending" | "connected" | "cancelled" | { error: string } {
  switch (status) {
    case "pending":
    case "connected":
    case "cancelled":
      return status;
    case "denied":
      return { error: "Authorization was denied. Try again when ready." };
    default:
      return {
        error: "The authorization code expired. Start a new connection.",
      };
  }
}
