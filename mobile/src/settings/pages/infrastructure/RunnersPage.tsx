import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { tileColor } from "../../../ui/tileColors";
import { useTheme } from "../../../ui/theme";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";
import { RUNNERS } from "./ImageDefinitionsPage";
import { metricsSummary, runnerConnection } from "./runners";

/** Runner instances of the organization with their load (admins). */
export function RunnersPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const { theme } = useTheme();
  const runners = useResource(resources, RUNNERS, () => client.runners.list());
  const online = (runners.data ?? []).filter(
    (runner) => runner.status === "online",
  );
  const metricsKey = `runner-metrics:${online.map((runner) => runner.id).join(",")}`;
  const metrics = useResource(
    resources,
    metricsKey,
    async () =>
      Object.fromEntries(
        await Promise.all(
          online.map(async (runner) => [
            runner.id,
            await client.runners.latestMetrics(runner.id).catch(() => null),
          ]),
        ),
      ),
    { enabled: online.length > 0 },
  );
  usePolling(runners.reload, 10000, Boolean(runners.data));
  usePolling(metrics.reload, 60000, online.length > 0);

  const sections: NativeFormSection[] = runners.data?.length
    ? [
        {
          id: "runners",
          footer: "Runners execute workspaces and their AI coding agents.",
          rows: runners.data.map((runner) => {
            const sample = metrics.data?.[runner.id];
            return {
              kind: "item" as const,
              id: `runner-${runner.id}`,
              testID: `runner-${runner.id}`,
              title: runner.name || runner.id.slice(0, 8),
              subtitle: [
                runnerConnection(runner),
                sample ? metricsSummary(sample) : "",
              ]
                .filter(Boolean)
                .join(" · "),
              symbol: "server.rack" as const,
              tint: tileColor(
                runner.status === "online" ? "green" : "gray",
                theme,
              ),
              onPress: () => navigation.push(route("runner", runner.id)),
            };
          }),
        },
      ]
    : [];

  return (
    <SettingsPage
      title="Runners"
      sections={
        runners.error && !runners.data
          ? loadErrorSections(runners.error, () => void runners.reload())
          : sections
      }
      loading={!runners.data && !runners.error}
      onRefresh={async () => {
        await Promise.allSettled([runners.reload(), metrics.reload()]);
      }}
      empty={{
        title: "No runners registered",
        description:
          "Register a runner to start provisioning workspaces for your AI coding agents.",
        symbol: "server.rack",
      }}
      primaryAction={{
        title: "Register runner",
        symbol: "plus",
        onPress: () => navigation.push("runner-new"),
        testID: "register-runner",
      }}
      testID="settings-runners"
    />
  );
}
