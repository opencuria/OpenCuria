import React, { useState } from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { serviceSubtitle } from "./credentialServices";
import { ORGANIZATION_SERVICES } from "./resources";

/**
 * Credential services of the organization (admins): which services accept
 * new credentials, and organization-owned services of their own.
 */
export function CredentialServicesPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useResource(resources, ORGANIZATION_SERVICES, () =>
    client.credentialServices.list(),
  );
  const [busy, setBusy] = useState<string | null>(null);

  const toggle = async (id: string, active: boolean) => {
    setBusy(id);
    try {
      const saved = await client.credentialServices.setActive(id, active);
      services.set((current) =>
        (current ?? []).map((entry) => (entry.id === saved.id ? saved : entry)),
      );
      // The service list of new credentials changes too.
      resources.invalidate("credential-services:available");
    } catch (cause) {
      showToast({
        title: "Failed to toggle service activation",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const sections: NativeFormSection[] = services.data?.length
    ? [
        {
          id: "services",
          footer:
            "Activation controls availability for new credentials; existing credentials keep working. It is independent of plugin activation.",
          rows: services.data.map((service) => ({
            kind: "toggle" as const,
            id: `service-${service.id}`,
            testID: `service-${service.id}`,
            label: service.name,
            subtitle: serviceSubtitle(service),
            value: service.is_active,
            disabled: busy === service.id,
            onChange: (active: boolean) => void toggle(service.id, active),
          })),
        },
      ]
    : [];

  return (
    <SettingsPage
      title="Credential services"
      sections={
        services.error && !services.data
          ? loadErrorSections(services.error, () => void services.reload())
          : sections
      }
      loading={!services.data && !services.error}
      onRefresh={services.reload}
      empty={{
        title: "No credential services",
        description:
          "Create an organization service or activate a global service.",
        symbol: "checkmark.shield",
      }}
      primaryAction={{
        title: "New service",
        symbol: "plus",
        onPress: () => navigation.push("credential-service-new"),
        testID: "new-credential-service",
      }}
      testID="settings-credential-services"
    />
  );
}
