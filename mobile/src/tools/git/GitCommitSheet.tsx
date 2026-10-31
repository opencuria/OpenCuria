import React from "react";
import {
  ActivityIndicator,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import * as Clipboard from "expo-clipboard";
import type {
  GitCommit,
  GitCommitDetails,
  GitCommitFile,
} from "@opencuria/chat-core/gitTypes";
import { Button } from "../../ui/controls";
import type { NativeMenuAction } from "../../ui/NativeMenu";
import { useTheme } from "../../ui/theme";
import { WorkspaceFileIcon } from "../files/WorkspaceFileIcon";
import { ToolDetailSheet } from "../ToolDetailSheet";
import { dirName, fileName, statusColor } from "./gitStyle";

function formatDate(iso: string | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      });
}

/** Commit summary and changed files (tap a file for its diff). */
export function GitCommitSheet({
  commit,
  details,
  loading,
  error,
  actions,
  onAction,
  onRetry,
  onOpenFile,
  onClose,
}: {
  commit: GitCommit | null;
  details: GitCommitDetails | null;
  loading: boolean;
  error: string | null;
  actions: NativeMenuAction[];
  onAction: (id: string) => void;
  onRetry: () => void;
  onOpenFile: (file: GitCommitFile) => void;
  onClose: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  if (!commit) return null;
  const subject = details?.message || commit.message;
  const body = (details?.body ?? commit.body ?? "").trim();
  const files = details?.fileChanges ?? [];
  const additions = files.reduce((sum, file) => sum + file.additions, 0);
  const deletions = files.reduce((sum, file) => sum + file.deletions, 0);
  const short = commit.hash.slice(0, 8);
  return (
    <ToolDetailSheet
      visible
      onClose={onClose}
      title={commit.stash ? commit.stash.selector : short}
      subtitle={commit.author}
      actions={actions}
      onAction={onAction}
      testID="git-commit-sheet"
    >
      <ScrollView contentContainerStyle={styles.content}>
        <Text selectable style={[styles.subject, { color: theme.text }]}>
          {subject}
        </Text>
        {body ? (
          <Text selectable style={[styles.body, { color: theme.text }]}>
            {body}
          </Text>
        ) : null}
        <View style={[styles.meta, { borderColor: theme.border }]}>
          <MetaRow
            label="Author"
            value={`${commit.author}${commit.authorEmail ? ` <${commit.authorEmail}>` : ""}`}
          />
          <MetaRow
            label="Date"
            value={formatDate(details?.authorDate || commit.timestamp)}
          />
          {details &&
          details.committer &&
          details.committer !== commit.author ? (
            <MetaRow label="Committer" value={details.committer} />
          ) : null}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Copy commit hash"
            onPress={() => void Clipboard.setStringAsync(commit.hash)}
          >
            <MetaRow label="Commit" value={commit.hash} mono />
          </Pressable>
          {commit.parents.length ? (
            <MetaRow
              label={commit.parents.length > 1 ? "Parents" : "Parent"}
              value={commit.parents.map((hash) => hash.slice(0, 8)).join(", ")}
              mono
            />
          ) : null}
        </View>
        <View style={styles.filesHeader}>
          <Text style={[styles.section, { color: theme.mutedText }]}>
            {files.length === 1
              ? "1 FILE CHANGED"
              : `${files.length} FILES CHANGED`}
          </Text>
          {files.length ? (
            <Text style={styles.stats}>
              <Text style={{ color: statusColor(theme, "A") }}>
                +{additions}
              </Text>{" "}
              <Text style={{ color: statusColor(theme, "D") }}>
                −{deletions}
              </Text>
            </Text>
          ) : null}
        </View>
        {loading && !details ? (
          <ActivityIndicator
            color={theme.subtleText}
            style={{ marginTop: 16 }}
          />
        ) : error && !details ? (
          <View style={{ gap: 10 }}>
            <Text style={{ color: theme.danger }}>{error}</Text>
            <Button
              title="Try again"
              compact
              onPress={onRetry}
              style={{ alignSelf: "flex-start" }}
            />
          </View>
        ) : (
          files.map((file) => (
            <Pressable
              key={`${file.oldPath}->${file.newPath}`}
              accessibilityRole="button"
              accessibilityLabel={`Show changes in ${file.newPath}`}
              onPress={() => onOpenFile(file)}
              android_ripple={{ color: theme.controlFill }}
              style={({ pressed }) => [
                styles.file,
                pressed && { backgroundColor: theme.controlFill },
              ]}
              testID="git-commit-file"
            >
              <WorkspaceFileIcon path={file.newPath} size={18} />
              <View style={styles.fileText}>
                <Text
                  numberOfLines={1}
                  style={[styles.fileName, { color: theme.text }]}
                >
                  {fileName(file.newPath)}
                </Text>
                {dirName(file.newPath) || file.oldPath !== file.newPath ? (
                  <Text
                    numberOfLines={1}
                    style={[styles.fileDir, { color: theme.subtleText }]}
                  >
                    {file.oldPath !== file.newPath
                      ? `${file.oldPath} → ${file.newPath}`
                      : dirName(file.newPath)}
                  </Text>
                ) : null}
              </View>
              <Text style={styles.stats}>
                <Text style={{ color: statusColor(theme, "A") }}>
                  +{file.additions}
                </Text>{" "}
                <Text style={{ color: statusColor(theme, "D") }}>
                  −{file.deletions}
                </Text>
              </Text>
              <Text
                style={[
                  styles.status,
                  { color: statusColor(theme, file.status) },
                ]}
              >
                {file.status}
              </Text>
            </Pressable>
          ))
        )}
      </ScrollView>
    </ToolDetailSheet>
  );
}

function MetaRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  const { theme } = useTheme();
  return (
    <View style={styles.metaRow}>
      <Text style={[styles.metaLabel, { color: theme.mutedText }]}>
        {label}
      </Text>
      <Text
        selectable
        numberOfLines={2}
        style={[styles.metaValue, { color: theme.text }, mono && styles.mono]}
      >
        {value}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  content: { padding: 20, gap: 12, paddingBottom: 48 },
  subject: { fontSize: 18, fontWeight: "600", lineHeight: 24 },
  body: { fontSize: 15, lineHeight: 21 },
  meta: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingVertical: 8,
    gap: 6,
  },
  metaRow: { flexDirection: "row", gap: 12 },
  metaLabel: { width: 84, fontSize: 13 },
  metaValue: { flex: 1, fontSize: 13 },
  mono: {
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
    fontSize: 12,
  },
  filesHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: 4,
  },
  section: { fontSize: 12, fontWeight: "600", letterSpacing: 0.4 },
  stats: { fontSize: 12, fontVariant: ["tabular-nums"] },
  file: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 48,
    paddingHorizontal: 4,
    borderRadius: 10,
  },
  fileText: { flex: 1, minWidth: 0 },
  fileName: { fontSize: 15 },
  fileDir: { fontSize: 12 },
  status: { width: 14, textAlign: "center", fontSize: 12, fontWeight: "700" },
});
