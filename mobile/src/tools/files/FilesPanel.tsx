import React, { useEffect, useRef, useState } from "react";
import { Platform, Pressable, StyleSheet, TextInput, View } from "react-native";
import { CircleX, Search } from "lucide-react-native";
import { useTheme } from "../../ui/theme";
import type { WorkspaceToolProps } from "../types";
import { FileTreeBrowser } from "./FileTreeBrowser";
import { FileViewerSheet } from "./FileViewerSheet";
import {
  absoluteWorkspacePath,
  useFileTreeEntries,
} from "./useFileTreeEntries";

/**
 * Workspace files like t3code's Files screen: a search field, the lazily
 * listed tree (pull to refresh) and a native file viewer sheet.
 */
export function FilesPanel({
  client,
  workspaceId,
  active,
  media,
  bottomInset,
  onInsertIntoChat,
}: WorkspaceToolProps): React.JSX.Element {
  const { theme } = useTheme();
  const [query, setQuery] = useState("");
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(active);
  const input = useRef<TextInput>(null);
  useEffect(() => {
    if (active) setEnabled(true);
    else input.current?.blur();
  }, [active]);
  const tree = useFileTreeEntries({
    client,
    workspaceId,
    searchQuery: query,
    enabled,
  });

  return (
    <View style={styles.root} testID="files-panel">
      <View style={styles.searchRow}>
        <View style={[styles.search, { backgroundColor: theme.surfaceMuted }]}>
          <Search size={16} color={theme.subtleText} />
          <TextInput
            ref={input}
            value={query}
            onChangeText={setQuery}
            placeholder="Search files"
            placeholderTextColor={theme.subtleText}
            autoCapitalize="none"
            autoCorrect={false}
            returnKeyType="search"
            clearButtonMode="never"
            accessibilityLabel="Search files"
            style={[styles.searchInput, { color: theme.text }]}
            testID="files-search"
          />
          {query ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Clear file search"
              hitSlop={8}
              onPress={() => setQuery("")}
            >
              <CircleX size={16} color={theme.subtleText} />
            </Pressable>
          ) : null}
        </View>
      </View>
      <FileTreeBrowser
        tree={tree}
        searchQuery={query}
        bottomInset={bottomInset}
        onSelectFile={(path) => {
          tree.selectPath(path);
          setOpenPath(absoluteWorkspacePath(path));
        }}
      />
      <FileViewerSheet
        client={client}
        workspaceId={workspaceId}
        media={media}
        path={openPath}
        bottomInset={bottomInset}
        onInsertIntoChat={onInsertIntoChat}
        onClose={() => setOpenPath(null)}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  searchRow: { paddingHorizontal: 16, paddingTop: 4, paddingBottom: 8 },
  search: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderRadius: Platform.OS === "ios" ? 12 : 24,
    paddingHorizontal: 12,
    minHeight: 40,
  },
  searchInput: {
    flex: 1,
    fontSize: 16,
    paddingVertical: Platform.OS === "ios" ? 9 : 6,
  },
});
