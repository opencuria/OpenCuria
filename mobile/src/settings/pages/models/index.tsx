import React from "react";
import { providerAccountInfo } from "@opencuria/chat-core/providerAccounts";
import { AgentsPage } from "./AgentsPage";
import { ProviderAccountPage } from "./ProviderAccountPage";
import { ProviderAccountsPage } from "./ProviderAccountsPage";
import { ProvidersPage } from "./ProvidersPage";
import { SkillPage } from "./SkillPage";
import { SkillsPage } from "./SkillsPage";

/**
 * Pages for Provider & Models, Agents and Skills.
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
    case "skills":
      return <SkillsPage />;
    case "skill":
      return argument ? (
        <SkillPage skillId={argument === "new" ? null : argument} />
      ) : undefined;
    default:
      return undefined;
  }
}
