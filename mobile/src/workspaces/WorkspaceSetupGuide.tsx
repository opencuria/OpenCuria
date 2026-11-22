import React, { useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { Check } from "lucide-react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import {
  currentWorkspaceSetupStep,
  workspaceSetupSteps,
  type WorkspaceSetupStep,
  type WorkspaceSetupStepId,
} from "@opencuria/chat-core/workspaceSetup";
import { Button } from "../ui/primitives";
import { useTheme } from "../ui/theme";
import { useForegroundReload } from "../settings/useForegroundReload";
import { usePolling } from "../settings/usePolling";
import { CreateWorkspaceSheet } from "./CreateWorkspaceSheet";
import { useWorkspaceCatalog } from "./useWorkspaceCatalog";

/** Settings page behind each step; the workspace step opens the create sheet. */
const STEP_SETTINGS: Partial<Record<WorkspaceSetupStepId, string>> = {
  runner: "runners",
  image: "image-definitions",
  provider: "providers",
};

export interface WorkspaceSetupGuideProps {
  client: OpenCuriaClient;
  organizationId: string | null;
  isAdmin: boolean;
  /** See `isModelProviderConnected` in chat-core. */
  providerConnected: boolean;
  /** Changes when setup may have happened elsewhere, e.g. Settings closed. */
  refreshKey: number;
  onOpenSettings: (path: string[]) => void;
  /** The server accepted a new workspace; reload the workspace list. */
  onCreated: () => Promise<void>;
}

/**
 * First-run guide in place of the empty chat while the user has no
 * workspace, as on the web home screen. Steps and their state come from
 * chat-core, so both clients lead through the same setup.
 */
export function WorkspaceSetupGuide({
  client,
  organizationId,
  isAdmin,
  providerConnected,
  refreshKey,
  onOpenSettings,
  onCreated,
}: WorkspaceSetupGuideProps): React.JSX.Element {
  const { theme } = useTheme();
  const catalog = useWorkspaceCatalog(client, true);
  const [creating, setCreating] = useState(false);
  // Runners connect and images build elsewhere; keep the state current.
  usePolling(catalog.reload, 15_000);
  useForegroundReload(catalog.reload);
  const { reload } = catalog;
  useEffect(() => {
    if (refreshKey) void reload();
  }, [refreshKey, reload]);

  const checked =
    catalog.ready || catalog.error !== null || catalog.imageError !== null;
  const steps = workspaceSetupSteps({
    isAdmin,
    runners: catalog.runners,
    definitions: catalog.definitions,
    buildsByDefinition: catalog.buildsByDefinition,
    capturedImages: catalog.capturedImages,
    imagesUnknown: catalog.imageError !== null,
    providerConnected,
  });
  const current = currentWorkspaceSetupStep(steps)?.id;
  const done = steps.filter((step) => step.done).length;

  const run = (step: WorkspaceSetupStep) => {
    const page = STEP_SETTINGS[step.id];
    if (page) onOpenSettings([page]);
    else setCreating(true);
  };

  return (
    <View
      style={[
        styles.card,
        { backgroundColor: theme.card, borderColor: theme.separator },
      ]}
      testID="workspace-setup"
    >
      <View style={styles.header}>
        <View style={styles.headerText}>
          <Text style={[styles.title, { color: theme.text }]}>
            Set up your first workspace
          </Text>
          <Text style={[styles.detail, { color: theme.mutedText }]}>
            Agents work in isolated VMs with a terminal, desktop and Git.
          </Text>
        </View>
        {checked ? (
          <Text
            style={[styles.progress, { color: theme.mutedText }]}
            testID="workspace-setup-progress"
          >
            {done} of {steps.length}
          </Text>
        ) : null}
      </View>
      {steps.map((step, index) => {
        const next = step.id === current;
        return (
          <View
            key={step.id}
            style={[styles.row, { borderTopColor: theme.separator }]}
            accessibilityLabel={`${step.title}${step.done ? ", done" : ""}`}
            testID={`workspace-setup-step-${step.id}`}
          >
            <View
              style={[
                styles.badge,
                step.done
                  ? { backgroundColor: theme.surfaceMuted }
                  : {
                      borderWidth: next && checked ? 1.5 : 1,
                      borderColor:
                        next && checked ? theme.primary : theme.separator,
                    },
              ]}
            >
              {step.done ? (
                <Check size={14} strokeWidth={2.5} color={theme.success} />
              ) : (
                <Text
                  style={[
                    styles.badgeText,
                    { color: next && checked ? theme.primary : theme.mutedText },
                  ]}
                >
                  {index + 1}
                </Text>
              )}
            </View>
            <View style={styles.rowText}>
              <Text
                style={[
                  styles.stepTitle,
                  { color: step.done ? theme.mutedText : theme.text },
                ]}
              >
                {step.title}
              </Text>
              {checked ? (
                <Text style={[styles.detail, { color: theme.mutedText }]}>
                  {step.detail}
                </Text>
              ) : null}
            </View>
            {checked && !step.done && step.available ? (
              <Button
                title={step.action}
                variant={next ? "primary" : "outline"}
                compact
                shape="capsule"
                style={styles.action}
                onPress={() => run(step)}
                testID={`workspace-setup-action-${step.id}`}
              />
            ) : null}
          </View>
        );
      })}
      <CreateWorkspaceSheet
        visible={creating}
        client={client}
        catalog={catalog}
        organizationId={organizationId}
        onClose={() => setCreating(false)}
        onCreated={async () => {
          setCreating(false);
          await onCreated();
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    width: "100%",
    maxWidth: 520,
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    overflow: "hidden",
  },
  header: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
    paddingHorizontal: 18,
    paddingTop: 18,
    paddingBottom: 14,
  },
  headerText: { flex: 1, gap: 4 },
  title: { fontSize: 16, lineHeight: 21, fontWeight: "600" },
  progress: { fontSize: 13, lineHeight: 21, fontVariant: ["tabular-nums"] },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 18,
    paddingVertical: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  badge: {
    width: 26,
    height: 26,
    borderRadius: 13,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { fontSize: 13, fontWeight: "600", fontVariant: ["tabular-nums"] },
  rowText: { flex: 1, gap: 2 },
  stepTitle: { fontSize: 15, lineHeight: 20, fontWeight: "500" },
  detail: { fontSize: 13, lineHeight: 18 },
  action: { alignSelf: "center" },
});
