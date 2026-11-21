import React from "react";
import type {
  NativeFormItemRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadFailure, refreshError } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";
import { RUNNERS } from "./ImageDefinitionsPage";
import { metricsSummary, runnerConnection } from "./runners";

/** Runner instances of the organization with their load (admins). */
export function RunnersPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
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
          rows: runners.data.map((runner): NativeFormItemRow => {
            const sample = metrics.data?.[runner.id];
            return {
              kind: "item",
              id: `runner-${runner.id}`,
              testID: `runner-${runner.id}`,
              title: runner.name || runner.id.slice(0, 8),
              subtitle: [
                runnerConnection(runner),
                sample ? metricsSummary(sample) : "",
              ]
                .filter(Boolean)
                .join(" · "),
              symbol: "server.rack",
              tile: true,
              status: runner.status === "online" ? "positive" : "neutral",
              onPress: () => navigation.push(route("runner", runner.id)),
            };
          }),
        },
      ]
    : [];
  const failure = runners.data ? undefined : loadFailure("runners", runners);
  const register = {
    title: "Register runner",
    symbol: "plus" as const,
    onPress: () => navigation.push("runner-new"),
  };

  return (
    <SettingsPage
      title="Runners"
      sections={
        failure ? [] : [...refreshError("runners", runners), ...sections]
      }
      loading={!runners.data && !runners.error}
      onRefresh={async () => {
        await Promise.allSettled([runners.reload(), metrics.reload()]);
      }}
      empty={
        failure ?? {
          title: "No runners yet",
          description:
            "Register a runner to start provisioning workspaces for your AI coding agents.",
          symbol: "server.rack",
          action: { ...register, testID: "empty-register-runner" },
        }
      }
      primaryAction={{ ...register, testID: "register-runner" }}
      testID="settings-runners"
    />
  );
}
