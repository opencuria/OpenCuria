import React from "react";
import { ApiKeyCreatedPage } from "./ApiKeyCreatedPage";
import { ApiKeyPage } from "./ApiKeyPage";
import { ApiKeysPage } from "./ApiKeysPage";
import { CredentialPage } from "./CredentialPage";
import { CredentialServicesPage } from "./CredentialServicesPage";
import { CredentialsPage } from "./CredentialsPage";
import { NewApiKeyPage } from "./NewApiKeyPage";
import { NewCredentialPage } from "./NewCredentialPage";
import { NewCredentialServicePage } from "./NewCredentialServicePage";
import { PluginEditorPage } from "./PluginEditorPage";
import {
  PluginMcpPage,
  PluginRequirementPage,
  PluginSkillPage,
} from "./PluginEntryPages";
import { PluginPage } from "./PluginPage";
import { PluginSkillViewPage } from "./PluginSkillViewPage";
import { PluginsPage } from "./PluginsPage";
import { PublicKeyPage } from "./PublicKeyPage";

/** "<first>:<rest>" of a route argument. */
function pair(argument: string): [string, string] {
  const index = argument.indexOf(":");
  return index < 0
    ? [argument, ""]
    : [argument.slice(0, index), argument.slice(index + 1)];
}

/**
 * Pages for Credentials, Credential services, Plugins and API keys.
 * Returns `undefined` for routes this group does not own.
 */
export function renderAccessRoute(
  page: string,
  argument: string,
): React.ReactNode | undefined {
  const [first, rest] = pair(argument);
  switch (page) {
    case "credentials":
      return <CredentialsPage />;
    case "credential":
      return argument ? <CredentialPage credentialId={argument} /> : undefined;
    case "credential-new":
      return <NewCredentialPage serviceId={argument || undefined} />;
    case "credential-public-key":
      return argument ? <PublicKeyPage credentialId={argument} /> : undefined;
    case "credential-services":
      return <CredentialServicesPage />;
    case "credential-service-new":
      return <NewCredentialServicePage />;
    case "plugins":
      return <PluginsPage />;
    case "plugin":
      return argument ? <PluginPage pluginId={argument} /> : undefined;
    case "plugin-skill-view":
      return first && rest ? (
        <PluginSkillViewPage pluginId={first} skillId={rest} />
      ) : undefined;
    case "plugin-editor":
      return argument ? <PluginEditorPage draftKey={argument} /> : undefined;
    case "plugin-skill":
      return first && rest ? (
        <PluginSkillPage draftKey={first} uid={rest} />
      ) : undefined;
    case "plugin-mcp":
      return first && rest ? (
        <PluginMcpPage draftKey={first} uid={rest} />
      ) : undefined;
    case "plugin-requirement":
      return first && rest ? (
        <PluginRequirementPage draftKey={first} uid={rest} />
      ) : undefined;
    case "api-keys":
      return <ApiKeysPage />;
    case "api-key":
      return argument ? <ApiKeyPage keyId={argument} /> : undefined;
    case "api-key-new":
      return <NewApiKeyPage />;
    case "api-key-created":
      return argument ? <ApiKeyCreatedPage keyId={argument} /> : undefined;
    default:
      return undefined;
  }
}
