import React, { useEffect, useState } from "react";
import { Modal, Platform, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { SegmentedControl } from "@expo/ui/community/segmented-control";
import { X } from "lucide-react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import type { MediaCache } from "../platform";
import { IconButton } from "../ui/controls";
import { useTheme } from "../ui/theme";
import { TOOL_PANELS } from "./panels";
import type { WorkspaceToolId, WorkspaceToolProps } from "./types";

const TOOLS: { id: WorkspaceToolId; label: string }[] = [
  { id: "git", label: "Git" },
  { id: "terminal", label: "Terminal" },
  { id: "files", label: "Files" },
];
/** The sections hug the trailing edge; Android's segmented buttons need room. */
const SEGMENTS_MAX_WIDTH = Platform.OS === "ios" ? 240 : 300;

export interface WorkspaceToolsSheetProps {
  visible: boolean;
  onClose: () => void;
  client: OpenCuriaClient;
  workspaceId: string;
  workspaceName: string;
  media: MediaCache | null;
  onInsertIntoChat?: (text: string) => void;
}

/**
 * Git, Terminal and Files of the chat's workspace in a full-screen native
 * sheet: UIKit full-screen presentation on iOS, a system dialog on Android.
 * Close sits leading, the sections trailing; Git opens first. Sections mount
 * on first use and stay alive while the sheet is open.
 */
export function WorkspaceToolsSheet({
  visible,
  onClose,
  client,
  workspaceId,
  workspaceName,
  media,
  onInsertIntoChat,
}: WorkspaceToolsSheetProps): React.JSX.Element | null {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const [tool, setTool] = useState<WorkspaceToolId>("git");
  const [mounted, setMounted] = useState<Set<WorkspaceToolId>>(
    () => new Set(["git"]),
  );
  useEffect(() => {
    if (visible) return;
    // Every presentation starts on Git again.
    setTool("git");
    setMounted(new Set(["git"]));
  }, [visible]);
  useEffect(() => {
    setMounted((current) =>
      current.has(tool) ? current : new Set([...current, tool]),
    );
  }, [tool]);

  if (!visible) return null;
  const select = (index: number) => {
    const next = TOOLS[index]?.id;
    if (next) setTool(next);
  };
  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle={Platform.OS === "ios" ? "fullScreen" : undefined}
      onRequestClose={onClose}
    >
      <View
        testID="workspace-tools-sheet"
        accessibilityViewIsModal
        onAccessibilityEscape={onClose}
        style={[styles.root, { backgroundColor: theme.background }]}
      >
        <View
          style={[
            styles.header,
            { paddingTop: insets.top + (Platform.OS === "ios" ? 4 : 6) },
          ]}
        >
          <IconButton
            label="Close"
            symbol="xmark"
            icon={<X size={22} color={theme.text} />}
            tone="floating"
            onPress={onClose}
            testID="workspace-tools-close"
          />
          <SegmentedControl
            values={TOOLS.map((item) => item.label)}
            selectedIndex={TOOLS.findIndex((item) => item.id === tool)}
            appearance={theme.scheme}
            onChange={({ nativeEvent }) =>
              select(nativeEvent.selectedSegmentIndex)
            }
            style={styles.segments}
            testID="workspace-tools-sections"
          />
        </View>
        <View style={styles.content}>
          {TOOLS.filter((item) => mounted.has(item.id)).map((item) => {
            const Panel = TOOL_PANELS[item.id];
            const props: WorkspaceToolProps = {
              client,
              workspaceId,
              workspaceName,
              active: tool === item.id,
              media,
              bottomInset: insets.bottom,
              onInsertIntoChat,
            };
            return (
              <View
                key={item.id}
                testID={`workspace-tools-${item.id}`}
                pointerEvents={tool === item.id ? "auto" : "none"}
                accessibilityElementsHidden={tool !== item.id}
                importantForAccessibility={
                  tool === item.id ? "auto" : "no-hide-descendants"
                }
                style={[
                  StyleSheet.absoluteFill,
                  tool === item.id ? null : styles.hidden,
                ]}
              >
                <Panel {...props} />
              </View>
            );
          })}
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 12,
    paddingBottom: 8,
    gap: 12,
  },
  segments: { flex: 1, maxWidth: SEGMENTS_MAX_WIDTH, marginLeft: "auto" },
  content: { flex: 1 },
  hidden: { opacity: 0 },
});
