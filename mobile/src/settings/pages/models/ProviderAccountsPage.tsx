import React, { useState } from "react";
import type { ProviderAccountInfo } from "@opencuria/chat-core/providerAccounts";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { PROVIDER_ACCOUNTS } from "./catalog";

/**
 * The accounts of one provider: tap a connected account to use it for new
 * responses, edit your own (admins also shared ones), or add another.
 */
export function ProviderAccountsPage({
  provider,
}: {
  provider: ProviderAccountInfo;
}): React.JSX.Element {
  const { client, resources, onCatalogChanged } = useSettings();
  const navigation = useSettingsNavigation();
  const accounts = useResource(resources, PROVIDER_ACCOUNTS, () =>
    client.providerAccounts.list(),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const own = (accounts.data ?? []).filter(
    (account) => account.provider === provider.id,
  );

  const select = async (accountId: string | null) => {
    setBusy(accountId ?? "none");
    try {
      await client.providerAccounts.select(provider.id, accountId);
      await accounts.reload();
      onCatalogChanged();
    } catch (cause) {
      showToast({
        title: "Failed to change active account",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const sections: NativeFormSection[] = own.length
    ? [
        {
          id: "accounts",
          footer: "New responses use the selected account.",
          rows: own.map((account) => ({
            kind: "item" as const,
            id: `account-${account.id}`,
            testID: `account-${account.id}`,
            title: account.label,
            subtitle: [
              account.scope === "organization"
                ? "Organization shared"
                : "Personal",
              account.connected ? "" : "Not connected",
            ]
              .filter(Boolean)
              .join(" · "),
            subtitleTone: account.connected ? "muted" : "warning",
            selected: account.is_active,
            busy: busy === account.id,
            disabled: Boolean(busy),
            onPress:
              account.connected && !account.is_active
                ? () => void select(account.id)
                : undefined,
            onInfo: account.can_manage
              ? () =>
                  navigation.push(
                    route("provider-account", provider.id, account.id),
                  )
              : undefined,
            infoLabel: `Edit ${account.label}`,
          })),
        },
        {
          id: "none",
          rows: [
            {
              kind: "button",
              id: "use-none",
              title: "Use no account",
              disabled:
                Boolean(busy) || !own.some((account) => account.is_active),
              busy: busy === "none",
              onPress: () => void select(null),
            },
          ],
        },
      ]
    : [];

  return (
    <SettingsPage
      title={provider.name}
      sections={
        accounts.error && !accounts.data
          ? loadErrorSections(accounts.error, () => void accounts.reload())
          : sections
      }
      loading={!accounts.data && !accounts.error}
      onRefresh={accounts.reload}
      empty={{
        title: "No accounts yet",
        description: provider.description,
        symbol: "person",
      }}
      primaryAction={{
        title: "Add account",
        symbol: "plus",
        onPress: () =>
          navigation.push(route("provider-account", provider.id, "new")),
        testID: "add-provider-account",
      }}
      testID="settings-provider-accounts"
    />
  );
}
