// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-highlights/LICENSE.
import React, { memo, useEffect, useMemo } from "react";
import {
  ActivityIndicator,
  FlatList,
  Platform,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { ChevronDown, ChevronRight } from "lucide-react-native";
import { Button } from "../../ui/controls";
import { useTheme } from "../../ui/theme";
import {
  buildFileTree,
  flattenFileTree,
  type FileTreeNode,
  type ProjectEntry,
  type VisibleFileTreeNode,
} from "./fileTree";
import type { FileTreeEntries } from "./useFileTreeEntries";
import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

const ROW_HEIGHT = 44;
const INDENT = 18;
const treeCache = new WeakMap<
  ReadonlyArray<ProjectEntry>,
  ReadonlyArray<FileTreeNode>
>();

function cachedFileTree(
  entries: ReadonlyArray<ProjectEntry>,
): ReadonlyArray<FileTreeNode> {
  const cached = treeCache.get(entries);
  if (cached) return cached;
  const tree = buildFileTree(entries);
  treeCache.set(entries, tree);
  return tree;
}

const FileTreeRow = memo(function FileTreeRow({
  item,
  selected,
  expanded,
  loading,
  onPressDirectory,
  onPressFile,
}: {
  item: VisibleFileTreeNode;
  selected: boolean;
  expanded: boolean;
  loading: boolean;
  onPressDirectory: (path: string, expand: boolean) => void;
  onPressFile: (path: string) => void;
}) {
  const { theme } = useTheme();
  const { node, depth } = item;
  const directory = node.kind === "directory";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={node.path}
      accessibilityState={{
        selected,
        expanded: directory ? expanded : undefined,
      }}
      onPress={() =>
        directory
          ? onPressDirectory(node.path, !expanded)
          : onPressFile(node.path)
      }
      android_ripple={{ color: theme.controlFill }}
      style={({ pressed }) => [
        styles.row,
        { paddingLeft: 10 + depth * INDENT },
        selected
          ? { backgroundColor: theme.controlFill }
          : pressed && Platform.OS === "ios"
            ? { backgroundColor: theme.controlFill }
            : null,
      ]}
      testID="files-tree-row"
    >
      {directory ? (
        expanded ? (
          <ChevronDown size={14} color={theme.subtleText} />
        ) : (
          <ChevronRight size={14} color={theme.subtleText} />
        )
      ) : (
        <View style={styles.chevronSpace} />
      )}
      <WorkspaceFileIcon
        path={node.path}
        kind={node.kind}
        expanded={expanded}
        size={18}
      />
      <Text
        numberOfLines={1}
        style={[
          styles.name,
          { color: node.name === ".git" ? theme.subtleText : theme.text },
          selected && styles.selected,
        ]}
      >
        {node.name}
      </Text>
      {directory && expanded && loading ? (
        <ActivityIndicator
          size="small"
          color={theme.subtleText}
          accessibilityLabel={`Loading ${node.name}`}
        />
      ) : null}
    </Pressable>
  );
});

/** Expandable workspace tree with search results, like t3code's file browser. */
export function FileTreeBrowser({
  tree: state,
  searchQuery,
  bottomInset,
  onSelectFile,
}: {
  tree: FileTreeEntries;
  searchQuery: string;
  bottomInset: number;
  onSelectFile: (path: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const {
    expandedPaths,
    loadDirectory,
    setExpandedPaths,
    loadingDirectories,
    selectedPath,
  } = state;
  const tree = cachedFileTree(state.entries);
  const visible = useMemo(
    () =>
      flattenFileTree({
        nodes: tree,
        expanded: expandedPaths,
        searchResultPaths: state.searchResultPaths,
      }),
    [expandedPaths, state.searchResultPaths, tree],
  );

  // List expanded directories that have not been loaded yet.
  useEffect(() => {
    if (state.searchResultPaths) return;
    const visit = (nodes: ReadonlyArray<FileTreeNode>) => {
      for (const node of nodes) {
        if (node.kind !== "directory" || !expandedPaths.has(node.path))
          continue;
        loadDirectory(node.path);
        visit(node.children);
      }
    };
    visit(tree);
  }, [expandedPaths, loadDirectory, state.searchResultPaths, tree]);

  const toggleDirectory = (path: string, expand: boolean) => {
    if (expand) loadDirectory(path);
    setExpandedPaths((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  return (
    <FlatList
      data={visible}
      keyExtractor={(item) => item.node.path}
      renderItem={({ item }) => (
        <FileTreeRow
          item={item}
          selected={
            item.node.kind === "file" && item.node.path === selectedPath
          }
          expanded={
            expandedPaths.has(item.node.path) ||
            Boolean(state.searchResultPaths)
          }
          loading={loadingDirectories.has(item.node.path)}
          onPressDirectory={toggleDirectory}
          onPressFile={onSelectFile}
        />
      )}
      extraData={{ expandedPaths, loadingDirectories, selectedPath }}
      getItemLayout={(_data, index) => ({
        length: ROW_HEIGHT,
        offset: ROW_HEIGHT * index,
        index,
      })}
      keyboardDismissMode="on-drag"
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={{ paddingTop: 4, paddingBottom: bottomInset + 24 }}
      refreshControl={
        <RefreshControl
          refreshing={state.isRefreshing}
          onRefresh={state.refresh}
        />
      }
      ListHeaderComponent={
        state.error && state.entries.length > 0 ? (
          <Text
            accessibilityRole="alert"
            style={[styles.note, { color: theme.danger }]}
          >
            {state.error}
          </Text>
        ) : state.searchTruncated ? (
          <Text style={[styles.note, { color: theme.mutedText }]}>
            More search results available. Refine your search to see them.
          </Text>
        ) : null
      }
      ListEmptyComponent={
        <View style={styles.empty}>
          {state.error ? (
            <>
              <Text style={[styles.emptyTitle, { color: theme.text }]}>
                Files unavailable
              </Text>
              <Text
                accessibilityRole="alert"
                style={[styles.emptyDetail, { color: theme.mutedText }]}
              >
                {state.error}
              </Text>
              <Button
                title="Try again"
                compact
                onPress={state.refresh}
                disabled={state.isPending}
                style={{ alignSelf: "flex-start" }}
                testID="files-retry"
              />
            </>
          ) : state.isPending ? (
            <ActivityIndicator color={theme.subtleText} />
          ) : (
            <>
              <Text style={[styles.emptyTitle, { color: theme.text }]}>
                No files found
              </Text>
              <Text style={[styles.emptyDetail, { color: theme.mutedText }]}>
                {searchQuery.trim()
                  ? "Try a different search."
                  : "The workspace is empty."}
              </Text>
            </>
          )}
        </View>
      }
      testID="files-tree"
    />
  );
}

const styles = StyleSheet.create({
  row: {
    height: ROW_HEIGHT,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingRight: 16,
    marginHorizontal: 6,
    borderRadius: 10,
  },
  chevronSpace: { width: 14 },
  name: { flex: 1, fontSize: 15 },
  selected: { fontWeight: "600" },
  note: { marginHorizontal: 16, marginVertical: 8, fontSize: 13 },
  empty: { paddingHorizontal: 20, paddingVertical: 24, gap: 8 },
  emptyTitle: { fontSize: 15, fontWeight: "600" },
  emptyDetail: { fontSize: 13 },
});
