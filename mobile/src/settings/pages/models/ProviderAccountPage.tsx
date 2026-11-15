import React, { useEffect, useState } from "react";
import { Alert, Linking } from "react-native";
import * as Clipboard from "expo-clipboard";
import {
  DEFAULT_OPENROUTER_BASE_URL,
  providerAccountForm,
  providerAccountPayload,
  unsharePayload,
  type ProviderAccount,
  type ProviderAccountForm,
  type ProviderAccountInfo,
  type ProviderAccountInput,
} from "@opencuria/chat-core/providerAccounts";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { PROVIDER_ACCOUNTS } from "./catalog";
import { useChatGptLogin } from "./useChatGptLogin";

const KEEP_SECRET = "Leave blank to keep existing";

/** The credential rows of one provider's account form. */
function credentialRows(
  provider: ProviderAccountInfo["id"],
  form: ProviderAccountForm,
  account: ProviderAccount | null,
  edit: (next: Partial<ProviderAccountForm>) => void,
): NativeFormRow[] {
  const secret = (
    id: string,
    label: string,
    value: string,
    onChange: (value: string) => void,
    placeholder: string,
  ): NativeFormRow => ({
    kind: "text",
    id,
    label,
    value,
    secure: true,
    placeholder,
    onChange,
  });
  const savedKey = account?.api_key_hint
    ? `Saved key (${account.api_key_hint})`
    : null;
  switch (provider) {
    case "openrouter":
      return [
        secret(
          "api-key",
          "API key",
          form.apiKey,
          (apiKey) => edit({ apiKey }),
          savedKey ?? "sk-or-…",
        ),
        {
          kind: "text",
          id: "base-url",
          label: "Base URL",
          keyboard: "url",
          placeholder: DEFAULT_OPENROUTER_BASE_URL,
          value: form.baseUrl,
          onChange: (baseUrl) => edit({ baseUrl }),
        },
      ];
    case "openai-compatible":
      return [
        {
          kind: "text",
          id: "base-url",
          label: "Base URL",
          keyboard: "url",
          placeholder: "https://my-host:8000/v1",
          value: form.baseUrl,
          onChange: (baseUrl) => edit({ baseUrl }),
        },
        secret(
          "api-key",
          "API key (optional)",
          form.apiKey,
          (apiKey) => edit({ apiKey }),
          savedKey ?? "Optional API key",
        ),
        {
          kind: "text",
          id: "models",
          label: "Models",
          placeholder: "Model ids, one per line",
          multiline: true,
          monospace: true,
          lines: [3, 8],
          value: form.modelsText,
          onChange: (modelsText) => edit({ modelsText }),
        },
      ];
    case "amazon-bedrock":
      return [
        {
          kind: "segmented",
          id: "bedrock-auth",
          label: "Authentication",
          value: form.bedrockAuthMethod,
          options: [
            { value: "access_keys", label: "Access keys" },
            { value: "bearer", label: "Bearer token" },
          ],
          onChange: (value) =>
            edit({ bedrockAuthMethod: value as "access_keys" | "bearer" }),
        },
        ...(form.bedrockAuthMethod === "bearer"
          ? [
              secret(
                "bearer-token",
                "Bearer token",
                form.bearerToken,
                (bearerToken) => edit({ bearerToken }),
                account ? KEEP_SECRET : "Bearer token",
              ),
            ]
          : [
              {
                kind: "text" as const,
                id: "access-key-id",
                label: "Access key ID",
                placeholder: account ? KEEP_SECRET : "AKIA…",
                value: form.accessKeyId,
                onChange: (accessKeyId: string) => edit({ accessKeyId }),
              },
              secret(
                "secret-access-key",
                "Secret access key",
                form.secretAccessKey,
                (secretAccessKey) => edit({ secretAccessKey }),
                account ? KEEP_SECRET : "Secret access key",
              ),
              secret(
                "session-token",
                "Session token (optional)",
                form.sessionToken,
                (sessionToken) => edit({ sessionToken }),
                account ? KEEP_SECRET : "Session token (optional)",
              ),
            ]),
        {
          kind: "text",
          id: "region",
          label: "Region",
          placeholder: "us-east-1",
          value: form.region,
          onChange: (region) => edit({ region }),
        },
      ];
    case "claude-agent": {
      const keep =
        account?.connected && account.auth_type === form.claudeAuthType;
      return [
        {
          kind: "segmented",
          id: "claude-auth",
          label: "Anthropic authentication",
          value: form.claudeAuthType,
          options: [
            { value: "api_token", label: "API token" },
            { value: "subscription_token", label: "Subscription token" },
          ],
          onChange: (value) =>
            edit({
              claudeAuthType: value as "api_token" | "subscription_token",
            }),
        },
        secret(
          "claude-token",
          form.claudeAuthType === "subscription_token"
            ? "Anthropic subscription token"
            : "Anthropic API token",
          form.claudeToken,
          (claudeToken) => edit({ claudeToken }),
          keep
            ? "Leave blank to keep existing token"
            : "Paste your Anthropic token",
        ),
      ];
    }
    default:
      return [];
  }
}

/** Footer of the credential section. */
function credentialFooter(
  provider: ProviderAccountInfo["id"],
): string | undefined {
  switch (provider) {
    case "openrouter":
    case "openai-compatible":
      return "Leave the API key blank to keep the saved key.";
    case "claude-agent":
      return "Token usage follows Anthropic's terms.";
    default:
      return undefined;
  }
}

/**
 * Adds or edits a provider account. Secrets are never shown; blank secret
 * fields keep the stored ones. ChatGPT connects with OpenAI's device login.
 */
export function ProviderAccountPage({
  provider,
  accountId,
}: {
  provider: ProviderAccountInfo;
  /** `null` adds an account. */
  accountId: string | null;
}): React.JSX.Element {
  const { client, resources, isAdmin, onCatalogChanged } = useSettings();
  const navigation = useSettingsNavigation();
  const accounts = useResource(resources, PROVIDER_ACCOUNTS, () =>
    client.providerAccounts.list(),
  );
  const account =
    accountId === null
      ? null
      : ((accounts.data ?? []).find((entry) => entry.id === accountId) ?? null);
  const [form, setForm] = useState<ProviderAccountForm | null>(
    accountId === null ? providerAccountForm(provider.id) : null,
  );
  useEffect(() => {
    if (account && !form) setForm(providerAccountForm(provider.id, account));
  }, [account, form, provider.id]);
  const [busy, setBusy] = useState<string | null>(null);

  const finish = async (title: string) => {
    await accounts.reload();
    onCatalogChanged();
    showToast({ title, tone: "success" });
    navigation.pop();
  };
  const login = useChatGptLogin(client, () => void finish("ChatGPT connected"));

  const run = async (
    id: string,
    action: () => Promise<void>,
    failure: string,
  ) => {
    setBusy(id);
    try {
      await action();
    } catch (cause) {
      showToast({
        title: failure,
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const payload = (): ProviderAccountInput | null => {
    if (!form) return null;
    const result = providerAccountPayload(provider.id, form, {
      isAdmin,
      account,
    });
    if ("error" in result) {
      showToast({ title: result.error, tone: "error" });
      return null;
    }
    return result.payload;
  };

  const confirmUnshare = (body: ProviderAccountInput) =>
    Alert.alert(
      "Unshare account?",
      "Unsharing transfers this account to you and immediately interrupts other members' responses using it. They will not switch to another account.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Unshare",
          style: "destructive",
          onPress: () =>
            void run(
              "unshare",
              async () => {
                await client.providerAccounts.update(
                  provider.id,
                  account!.id,
                  unsharePayload(provider.id, body),
                );
                await finish("Account unshared");
              },
              "Failed to unshare account",
            ),
        },
      ],
    );

  const save = () => {
    const body = payload();
    if (!body) return;
    if (
      account &&
      account.scope === "organization" &&
      !body.shared &&
      isAdmin
    ) {
      confirmUnshare(body);
      return;
    }
    void run(
      "save",
      async () => {
        if (account)
          await client.providerAccounts.update(provider.id, account.id, body);
        else await client.providerAccounts.create(provider.id, body);
        await finish(account ? "Account saved" : "Account added");
      },
      "Failed to save account",
    );
  };

  const remove = () =>
    Alert.alert(
      `Delete ${account?.label ?? "account"}?`,
      "Deleting this account immediately interrupts responses using it. They will not switch to another account.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: () =>
            void run(
              "delete",
              async () => {
                await client.providerAccounts.delete(provider.id, account!.id);
                await finish("Account deleted");
              },
              "Failed to delete account",
            ),
        },
      ],
    );

  const connectChatGpt = () => {
    if (!form?.label.trim()) {
      showToast({ title: "Account name is required.", tone: "error" });
      return;
    }
    void login.start({
      label: form.label.trim(),
      shared: isAdmin && form.shared,
      ...(account ? { account_id: account.id } : {}),
    });
  };

  const edit = (next: Partial<ProviderAccountForm>) =>
    setForm((current) => (current ? { ...current, ...next } : current));
  const chatgpt = provider.id === "chatgpt";
  const manageable = !account || account.can_manage;

  const sections: NativeFormSection[] = form
    ? [
        {
          id: "account",
          footer:
            !isAdmin && account?.scope !== "organization"
              ? "Only you can use this account."
              : undefined,
          rows: [
            {
              kind: "text",
              id: "label",
              label: "Account name",
              placeholder: "e.g. Personal or Production",
              value: form.label,
              autoFocus: !account,
              onChange: (label) => edit({ label }),
            },
            ...(isAdmin
              ? [
                  {
                    kind: "toggle" as const,
                    id: "shared",
                    label: "Share with this organization",
                    value: form.shared,
                    onChange: (shared: boolean) => edit({ shared }),
                  },
                ]
              : []),
          ],
        },
        ...(chatgpt
          ? [
              {
                id: "chatgpt",
                title: "ChatGPT",
                footer: login.error
                  ? login.error
                  : login.pending
                    ? "Enter this code at OpenAI. This page updates when you are signed in."
                    : "Use your ChatGPT Plus or Pro subscription. You authorize with OpenAI's device login.",
                footerTone: login.error
                  ? ("danger" as const)
                  : ("muted" as const),
                rows: login.pending
                  ? [
                      {
                        kind: "value" as const,
                        id: "user-code",
                        label: "Code",
                        value: login.userCode,
                        monospace: true,
                      },
                      {
                        kind: "button" as const,
                        id: "copy-code",
                        title: "Copy code",
                        symbol: "doc.on.doc" as const,
                        onPress: () => {
                          void Clipboard.setStringAsync(login.userCode);
                          showToast({ title: "Code copied", tone: "success" });
                        },
                      },
                      {
                        kind: "button" as const,
                        id: "open-authorization",
                        title: "Open authorization page",
                        symbol: "arrow.up.right.square" as const,
                        onPress: () =>
                          void Linking.openURL(login.verificationUrl),
                      },
                      {
                        kind: "button" as const,
                        id: "waiting",
                        title: "Waiting for authorization…",
                        busy: true,
                        onPress: () => undefined,
                      },
                      {
                        kind: "button" as const,
                        id: "cancel-login",
                        title: "Cancel",
                        onPress: login.cancel,
                      },
                    ]
                  : [
                      {
                        kind: "button" as const,
                        id: "connect-chatgpt",
                        testID: "connect-chatgpt",
                        title: account
                          ? "Reconnect with ChatGPT"
                          : "Connect with ChatGPT",
                        symbol: "link" as const,
                        busy: login.starting,
                        disabled: !form.label.trim() || Boolean(busy),
                        onPress: connectChatGpt,
                      },
                    ],
              },
            ]
          : [
              {
                id: "credentials",
                title: "Credentials",
                footer: credentialFooter(provider.id),
                rows: credentialRows(provider.id, form, account, edit),
              },
            ]),
        ...(account && account.can_manage
          ? [
              {
                id: "danger",
                rows: [
                  ...(isAdmin && account.scope === "organization"
                    ? [
                        {
                          kind: "button" as const,
                          id: "unshare",
                          title: "Unshare",
                          symbol: "person" as const,
                          busy: busy === "unshare",
                          disabled: Boolean(busy),
                          onPress: () => {
                            const body = payload();
                            if (body) confirmUnshare(body);
                          },
                        },
                      ]
                    : []),
                  {
                    kind: "button" as const,
                    id: "delete",
                    title: "Delete account",
                    symbol: "trash" as const,
                    destructive: true,
                    busy: busy === "delete",
                    disabled: Boolean(busy),
                    onPress: remove,
                  },
                ],
              },
            ]
          : []),
      ]
    : [];

  return (
    <SettingsPage
      title={account ? account.label : `New ${provider.name} account`}
      sections={sections}
      loading={!form && !accounts.data && !accounts.error}
      empty={{
        title: "Account unavailable",
        description: accounts.error ?? "This account no longer exists.",
        symbol: "exclamationmark.triangle.fill",
      }}
      primaryAction={
        manageable && !(chatgpt && !account)
          ? {
              title: account ? "Save" : "Add",
              onPress: save,
              busy: busy === "save",
              disabled: !form?.label.trim() || Boolean(busy) || login.pending,
              testID: "save-provider-account",
            }
          : undefined
      }
      testID="settings-provider-account"
    />
  );
}
