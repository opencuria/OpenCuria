import React, { useEffect, useState } from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { RUNNERS } from "./ImageDefinitionsPage";
import {
  QEMU_RESOURCES,
  capCeiling,
  formatResource,
  hostCapacity,
  initialCap,
  qemuLimitsForm,
  qemuLimitsPayload,
  qemuLimitsValid,
  setQemuBound,
  type QemuLimitsForm,
  type QemuRange,
  type QemuResource,
} from "./runners";

const RESOURCES: QemuResource[] = ["vcpus", "memory", "disk"];
const BOUNDS: Array<[keyof QemuRange, string]> = [
  ["min", "Minimum"],
  ["default", "Default"],
  ["max", "Maximum"],
];

/** Per-workspace defaults and limits of a runner and optional totals (admins). */
export function RunnerLimitsPage({
  runnerId,
}: {
  runnerId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const runners = useResource(resources, RUNNERS, () => client.runners.list());
  const metrics = useResource(resources, `runner-metric:${runnerId}`, () =>
    client.runners.latestMetrics(runnerId),
  );
  const runner = runners.data?.find((entry) => entry.id === runnerId);
  const [form, setForm] = useState<QemuLimitsForm | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (runner && !form) setForm(qemuLimitsForm(runner));
  }, [runner, form]);

  const save = async () => {
    if (!form || !runner || !qemuLimitsValid(form)) return;
    setBusy(true);
    try {
      const saved = await client.runners.update(
        runner.id,
        qemuLimitsPayload(form),
      );
      runners.set((current) =>
        (current ?? []).map((entry) => (entry.id === saved.id ? saved : entry)),
      );
      showToast({
        title: "Runner updated",
        message: "QEMU limits and defaults were saved.",
        tone: "success",
      });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "Update failed",
        message: errorMessage(cause, "Failed to update runner limits."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const sections: NativeFormSection[] =
    form && runner
      ? [
          ...RESOURCES.map((resource): NativeFormSection => {
            const spec = QEMU_RESOURCES[resource];
            const range = form.ranges[resource];
            return {
              id: `range-${resource}`,
              title: `${spec.label} per workspace`,
              rows: BOUNDS.map(([bound, label]) => ({
                kind: "stepper" as const,
                id: `${resource}-${bound}`,
                label,
                value: range[bound],
                min: bound === "default" ? range.min : spec.floor,
                max: bound === "default" ? range.max : spec.ceiling,
                step: spec.step,
                format: (value: number) => formatResource(resource, value),
                onChange: (value: number) =>
                  setForm({
                    ...form,
                    ranges: {
                      ...form.ranges,
                      [resource]: setQemuBound(range, bound, value),
                    },
                  }),
              })),
            };
          }),
          ...RESOURCES.map((resource): NativeFormSection => {
            const spec = QEMU_RESOURCES[resource];
            const cap = form.caps[resource];
            const host = hostCapacity(resource, runner, metrics.data);
            return {
              id: `cap-${resource}`,
              title: `${spec.label} across active workspaces`,
              footer:
                [
                  resource === "disk"
                    ? "Stopped workspaces and retained disks also count."
                    : "",
                  host && resource !== "vcpus"
                    ? `Host: ${formatResource(resource, host)}.`
                    : "",
                ]
                  .filter(Boolean)
                  .join(" ") || undefined,
              rows: [
                {
                  kind: "toggle" as const,
                  id: `${resource}-unlimited`,
                  label: "Unlimited",
                  value: cap === null,
                  onChange: (unlimited: boolean) =>
                    setForm({
                      ...form,
                      caps: {
                        ...form.caps,
                        [resource]: unlimited
                          ? null
                          : initialCap(form.ranges[resource], resource),
                      },
                    }),
                },
                ...(cap === null
                  ? []
                  : [
                      {
                        kind: "stepper" as const,
                        id: `${resource}-cap`,
                        label: "Total",
                        value: cap,
                        min: spec.floor,
                        max: capCeiling(form, resource, host),
                        step: spec.step,
                        format: (value: number) =>
                          formatResource(resource, value),
                        onChange: (value: number) =>
                          setForm({
                            ...form,
                            caps: { ...form.caps, [resource]: value },
                          }),
                      },
                    ]),
              ],
            };
          }),
        ]
      : [];

  return (
    <SettingsPage
      title="QEMU limits"
      sections={sections}
      loading={!form && !runners.data && !runners.error}
      empty={{
        title: "Runner unavailable",
        description: runners.error ?? "This runner no longer exists.",
        symbol: "exclamationmark.triangle.fill",
      }}
      primaryAction={{
        title: "Save",
        onPress: () => void save(),
        busy,
        disabled: !form || !qemuLimitsValid(form) || busy,
        testID: "save-runner-limits",
      }}
      testID="settings-runner-limits"
    />
  );
}
