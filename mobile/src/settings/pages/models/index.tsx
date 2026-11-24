import React from "react";
import { providerAccountInfo } from "@opencuria/chat-core/providerAccounts";
import { AgentsPage } from "./AgentsPage";
import { ProviderAccountPage } from "./ProviderAccountPage";
import { ProviderAccountsPage } from "./ProviderAccountsPage";
import { ProvidersPage } from "./ProvidersPage";

/**
 * Pages for Provider & Models and Agents.
 * Returns `undefined` for routes this group does not own.
 */
export function renderModelsRoute(
  page: string,
  argument: string,
): React.ReactNode | undefined {
  switch (page) {
    case "providers":
      return <ProvidersPage />;
    case "provider": {
      const provider = providerAccountInfo(argument);
      return provider ? (
        <ProviderAccountsPage provider={provider} />
      ) : undefined;
    }
    case "provider-account": {
      const [id, ...account] = argument.split(":");
      const provider = providerAccountInfo(id);
      const accountId = account.join(":");
      return provider && accountId ? (
        <ProviderAccountPage
          provider={provider}
          accountId={accountId === "new" ? null : accountId}
        />
      ) : undefined;
    }
    case "agents":
      return <AgentsPage />;
    default:
      return undefined;
  }
}
