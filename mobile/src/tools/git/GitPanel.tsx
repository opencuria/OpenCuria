import React, {
  memo,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Platform,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import Svg, { Circle, Path } from "react-native-svg";
import * as Clipboard from "expo-clipboard";
import {
  Archive,
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  GitBranch,
  GitBranchPlus,
  Minus,
  Plus,
  RefreshCw,
  Sparkles,
  Undo2,
} from "lucide-react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { formatTimeAgo } from "@opencuria/chat-core/conversationGroups";
import {
  computeGitGraphLayout,
  filterReachableCommits,
} from "@opencuria/chat-core/gitGraph";
import {
  GIT_STATUS_LABELS,
  bulkUnstagedGitPaths,
  commitDetailsKey,
  currentGitBranch,
  gitRefTagsByHash,
  stagedGitChanges,
  suggestRemoteAction,
  unstagedGitChanges,
} from "@opencuria/chat-core/gitModel";
import {
  groupRefTags,
  type GitRefGroup,
} from "@opencuria/chat-core/gitRefGroups";
import { insertStashCommits } from "@opencuria/chat-core/gitStash";
import type {
  GitCommit,
  GitCommitFile,
  GitFileChange,
} from "@opencuria/chat-core/gitTypes";
import { NativeContextMenu } from "../../ui/ContextMenu";
import { Banner, Button, IconButton } from "../../ui/primitives";
import { NativeMenu, type NativeMenuAction } from "../../ui/NativeMenu";
import { useTheme, type OpenCuriaTheme } from "../../ui/theme";
import { WorkspaceFileIcon } from "../files/WorkspaceFileIcon";
import type { WorkspaceToolProps } from "../types";
import { GitBranchSheet, type GitBranchForm } from "./GitBranchSheet";
import { GitCommitSheet } from "./GitCommitSheet";
import { GitController, selectedGitRepo } from "./gitController";
import { GitDiffSheet, type GitDiffTarget } from "./GitDiffSheet";
import {
  branchActiveForeground,
  branchColor,
  dirName,
  fileName,
  statusColor,
} from "./gitStyle";
import {
  buildGraphRows,
  graphColumnWidth,
  graphLaneCount,
  type GraphRow,
} from "./graphRows";

const ROW_HEIGHT = 44;
const GRAPH_GRID_X = 14;
const GRAPH_OFFSET_X = 14;
const GRAPH_MIN_GRID_X = 8;
/** The graph column never takes more than this many points. */
const GRAPH_MAX_WIDTH = 132;
const NODE_RADIUS = 4.5;

const controllers = new WeakMap<OpenCuriaClient, Map<string, GitController>>();
const commitDrafts = new WeakMap<GitController, string>();

/** One controller per workspace, so reopening the sheet shows data at once. */
function gitControllerFor(
  client: OpenCuriaClient,
  workspaceId: string,
): GitController {
  let byWorkspace = controllers.get(client);
  if (!byWorkspace) {
    byWorkspace = new Map();
    controllers.set(client, byWorkspace);
  }
  let controller = byWorkspace.get(workspaceId);
  if (!controller) {
    controller = new GitController(client, workspaceId);
    byWorkspace.set(workspaceId, controller);
  }
  return controller;
}

/** Stop polling and drop cached Git state of a client (sign-out, org change). */
export function releaseGitControllers(client: OpenCuriaClient): void {
  const byWorkspace = controllers.get(client);
  controllers.delete(client);
  byWorkspace?.forEach((controller) => controller.dispose());
}

type Item =
  | { type: "changes-group"; key: string; side: "staged" | "unstaged" }
  | {
      type: "change";
      key: string;
      side: "staged" | "unstaged";
      change: GitFileChange;
    }
  | { type: "empty"; key: string; text: string }
  | { type: "graph-header"; key: string }
  | { type: "commit"; key: string; commit: GitCommit; index: number }
  | { type: "graph-empty"; key: string; text: string };

/**
 * The webapp Git tab with native controls: repository and branch header,
 * commit box with Commit / Commit & Push and the suggested remote action,
 * Staged Changes and Changes, then the commit graph (vscode-git-graph lanes,
 * branch/remote/stash badges with their actions). Diffs and commit details
 * open in native sheets.
 */
export function GitPanel({
  client,
  workspaceId,
  active,
  bottomInset,
}: WorkspaceToolProps): React.JSX.Element {
  const { theme } = useTheme();
  const controller = useMemo(
    () => gitControllerFor(client, workspaceId),
    [client, workspaceId],
  );
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  useEffect(() => {
    controller.setVisible(active);
  }, [active, controller]);
  useEffect(() => () => controller.setVisible(false), [controller]);

  const repo = selectedGitRepo(state);
  const branch = currentGitBranch(repo);
  const busy = state.busyOperation !== null;
  const staged = useMemo(() => stagedGitChanges(repo), [repo]);
  const unstaged = useMemo(() => unstagedGitChanges(repo), [repo]);
  const bulkPaths = useMemo(() => bulkUnstagedGitPaths(repo), [repo]);
  const detailsPending = Boolean(
    repo && state.detailsLoading[repo.path] && !state.repoDetails[repo.path],
  );

  // The commit message survives closing and reopening the sheet.
  const [message, setMessageState] = useState(
    () => commitDrafts.get(controller) ?? "",
  );
  const setMessage = (value: string) => {
    commitDrafts.set(controller, value);
    setMessageState(value);
  };
  const [stagedOpen, setStagedOpen] = useState(true);
  const [changesOpen, setChangesOpen] = useState(true);
  const [filter, setFilter] = useState<string | null>(null);
  const [diff, setDiff] = useState<GitDiffTarget | null>(null);
  const [commitHash, setCommitHash] = useState<string | null>(null);
  const [branchForm, setBranchForm] = useState<GitBranchForm | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // -- graph ------------------------------------------------------------------
  const activeFilter = repo?.branches.some((b) => b.name === filter)
    ? filter
    : null;
  const commits = useMemo(() => {
    if (!repo) return [];
    let base = repo.commits;
    if (activeFilter) {
      const tip = repo.branches.find((b) => b.name === activeFilter)?.tipHash;
      if (tip) base = filterReachableCommits(repo.commits, tip);
    }
    return insertStashCommits(base, repo.stashes ?? []);
  }, [activeFilter, repo]);
  const layout = useMemo(
    () => computeGitGraphLayout(commits, { headHash: repo?.headHash ?? null }),
    [commits, repo?.headHash],
  );
  const lanes = graphLaneCount(layout);
  const gridX = Math.max(
    GRAPH_MIN_GRID_X,
    Math.min(
      GRAPH_GRID_X,
      (GRAPH_MAX_WIDTH - 2 * GRAPH_OFFSET_X) / Math.max(1, lanes - 1),
    ),
  );
  const geometry = useMemo(
    () => ({ rowHeight: ROW_HEIGHT, gridX, offsetX: GRAPH_OFFSET_X }),
    [gridX],
  );
  const graphRows = useMemo(
    () => buildGraphRows(layout, geometry),
    [geometry, layout],
  );
  const graphWidth = Math.min(
    GRAPH_MAX_WIDTH,
    graphColumnWidth(lanes, geometry),
  );
  const refGroups = useMemo(() => {
    const map = new Map<string, GitRefGroup[]>();
    for (const [hash, tags] of gitRefTagsByHash(repo))
      map.set(hash, groupRefTags(tags, repo?.remotes ?? []));
    return map;
  }, [repo]);
  const isDetached = repo?.currentBranch === null;
  const isUnborn = !repo || repo.commits.length === 0 || repo.headHash === null;

  // -- actions ------------------------------------------------------------------
  const openWorkingDiff = (
    change: GitFileChange,
    side: "staged" | "unstaged",
  ) => {
    const isStaged = side === "staged";
    setDiff({
      path: change.path,
      label: isStaged
        ? "Staged"
        : change.unstaged === "untracked"
          ? "Untracked"
          : "Working tree",
      load: () => controller.workingDiff(change.path, change.oldPath, isStaged),
      actions: isStaged
        ? [{ id: "unstage", title: "Unstage changes", symbol: "minus" }]
        : change.conflict
          ? []
          : [
              { id: "stage", title: "Stage changes", symbol: "plus" },
              {
                id: "discard",
                title: "Discard changes",
                symbol: "arrow.uturn.backward",
                destructive: true,
              },
            ],
      onAction: (id) => {
        if (id === "unstage")
          void controller
            .unstage(change.path)
            .then((ok) => ok && setDiff(null));
        else if (id === "stage")
          void controller.stage(change.path).then((ok) => ok && setDiff(null));
        else if (id === "discard")
          confirmDiscard([change.path], () => setDiff(null));
      },
    });
  };

  const confirmDiscard = (paths: string[], after?: () => void) => {
    const untracked = unstaged.filter(
      (c) => paths.includes(c.path) && c.unstaged === "untracked",
    ).length;
    const detail =
      paths.length === 1
        ? `Are you sure you want to discard changes in ${paths[0]}?`
        : `Are you sure you want to discard changes in ${paths.length} files?${
            untracked
              ? ` ${untracked === 1 ? "1 untracked file" : `${untracked} untracked files`} will be deleted.`
              : ""
          }`;
    Alert.alert(
      paths.length > 1 ? "Discard all changes" : "Discard changes",
      `${detail} This cannot be undone.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: paths.length > 1 ? "Discard All" : "Discard",
          style: "destructive",
          onPress: () => {
            const run =
              paths.length > 1
                ? controller.discardAll(paths)
                : controller.discard(paths[0]!);
            void run.then((ok) => ok && after?.());
          },
        },
      ],
    );
  };

  const canCommit = message.trim().length > 0 && staged.length > 0 && !busy;
  const commit = async (andPush: boolean) => {
    if (!canCommit) return;
    if (await controller.commit(message, andPush)) setMessage("");
  };
  const generate = async () => {
    const generated = await controller.generateCommitMessage();
    if (generated) setMessage(generated);
  };

  const ahead = branch?.ahead ?? 0;
  const behind = branch?.behind ?? 0;
  const hasUpstream = branch?.upstream != null;
  const primary = suggestRemoteAction(repo, busy);
  const canPush = !busy && !isDetached && (ahead > 0 || !hasUpstream);
  const canTrack = !busy && !isDetached && hasUpstream;
  const canPrimary =
    primary === "publish" || primary === "push"
      ? canPush
      : primary === "pull" || primary === "sync"
        ? canTrack
        : false;
  const primaryLabel =
    primary === "publish"
      ? "Publish"
      : primary === "pull"
        ? `Pull${behind ? ` ${behind}` : ""}`
        : primary === "sync"
          ? `Sync ↓${behind} ↑${ahead}`
          : `Push${primary === "push" && ahead ? ` ${ahead}` : ""}`;
  const runPrimary = () => {
    if (primary === "publish" || primary === "push") void controller.push();
    else if (primary === "pull") void controller.pull();
    else if (primary === "sync") void controller.sync();
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      await controller.fetchAndRefresh();
    } finally {
      setRefreshing(false);
    }
  };

  const confirmMergeAbort = () =>
    Alert.alert(
      "Abort merge?",
      "This discards the in-progress merge and restores the working tree to its state before the merge. Local merge changes will be lost.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Abort merge",
          style: "destructive",
          onPress: () => void controller.mergeAbort(),
        },
      ],
    );

  const branchActions = (group: GitRefGroup): NativeMenuAction[] => {
    const actions: NativeMenuAction[] = [];
    if (group.local) {
      const name = group.local.name;
      const current = group.local.current;
      actions.push(
        {
          id: `checkout:${name}`,
          title: "Checkout",
          symbol: "checkmark",
          disabled: current || busy,
        },
        {
          id: `rename:${name}`,
          title: "Rename Branch",
          symbol: "pencil",
          disabled: busy,
        },
        {
          id: `delete:${name}`,
          title: "Delete Branch",
          symbol: "trash",
          destructive: true,
          disabled: current || busy,
        },
        {
          id: `merge-into:${name}`,
          title: "Merge into Current Branch",
          symbol: "arrow.triangle.merge",
          disabled: current || isDetached || busy,
        },
        {
          id: `merge-current:${name}`,
          title: `Merge Current into '${name}'`,
          symbol: "arrow.triangle.merge",
          disabled: current || isDetached || busy,
        },
      );
    }
    for (const chip of group.remotes) {
      actions.push(
        {
          id: `checkout-remote:${chip.full}`,
          title: group.local ? `Checkout tracking '${chip.full}'` : "Checkout",
          symbol: "arrow.down.circle",
          disabled: busy,
        },
        {
          id: `copy:${chip.full}`,
          title: group.local ? `Copy '${chip.full}'` : "Copy name",
          symbol: "doc.on.doc",
        },
      );
    }
    return actions;
  };
  const onBranchAction = (id: string) => {
    const [kind, ...rest] = id.split(":");
    const name = rest.join(":");
    const current = repo?.currentBranch ?? "";
    if (kind === "checkout") void controller.checkoutBranch(name);
    else if (kind === "rename") setBranchForm({ mode: "rename", branch: name });
    else if (kind === "delete")
      Alert.alert(
        "Delete branch",
        `Delete local branch ${name}? Only fully merged branches can be safely deleted (git branch -d). Unmerged work cannot be deleted from here.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Delete",
            style: "destructive",
            onPress: () => void controller.deleteBranch(name),
          },
        ],
      );
    else if (kind === "merge-into" || kind === "merge-current") {
      const [source, target] =
        kind === "merge-into" ? [name, current] : [current, name];
      Alert.alert(
        "Merge branch",
        `Merge ${source} into ${target}. Uses fast-forward when possible, otherwise creates a merge commit on ${target}.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Merge",
            onPress: () =>
              void (kind === "merge-into"
                ? controller.mergeIntoCurrent(name)
                : controller.mergeCurrentInto(name)),
          },
        ],
      );
    } else if (kind === "checkout-remote")
      void controller.checkoutRemoteBranch(name);
    else if (kind === "copy") void Clipboard.setStringAsync(name);
  };

  const commitActions = (row: GitCommit): NativeMenuAction[] =>
    row.stash
      ? [
          {
            id: "stash-apply",
            title: "Apply Stash",
            symbol: "checkmark",
            disabled: busy,
          },
          {
            id: "stash-pop",
            title: "Pop Stash",
            symbol: "tray.and.arrow.up",
            disabled: busy,
          },
          {
            id: "stash-branch",
            title: "Create Branch From Stash",
            symbol: "arrow.triangle.branch",
            disabled: busy,
          },
          { id: "stash-copy", title: "Copy Stash Name", symbol: "doc.on.doc" },
          {
            id: "stash-drop",
            title: "Drop Stash",
            symbol: "trash",
            destructive: true,
            disabled: busy,
          },
        ]
      : [
          {
            id: "checkout-commit",
            title: "Checkout Commit",
            symbol: "smallcircle.filled.circle",
            disabled: busy,
          },
          {
            id: "branch-here",
            title: "Create Branch Here",
            symbol: "arrow.triangle.branch",
            disabled: busy || isUnborn,
          },
          { id: "copy-hash", title: "Copy Commit Hash", symbol: "doc.on.doc" },
        ];
  const onCommitAction = (row: GitCommit, id: string) => {
    const selector = row.stash?.selector ?? "";
    if (id === "checkout-commit") void controller.checkoutCommit(row.hash);
    else if (id === "branch-here")
      setBranchForm({
        mode: "create",
        fromHash: row.hash,
        baseLabel: row.hash.slice(0, 8),
      });
    else if (id === "copy-hash") void Clipboard.setStringAsync(row.hash);
    else if (id === "stash-apply") void controller.applyStash(selector);
    else if (id === "stash-pop") void controller.popStash(selector);
    else if (id === "stash-branch")
      setBranchForm({
        mode: "create",
        fromHash: "",
        baseLabel: selector,
        stash: selector,
      });
    else if (id === "stash-copy") void Clipboard.setStringAsync(selector);
    else if (id === "stash-drop")
      Alert.alert(
        "Drop stash",
        `Drop stash entry ${selector}? This permanently removes the stashed changes.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Drop",
            style: "destructive",
            onPress: () => void controller.dropStash(selector),
          },
        ],
      );
  };

  const openCommit = (hash: string) => {
    setCommitHash(hash);
    void controller.ensureCommitDetails(hash);
  };
  const sheetCommit = commitHash
    ? (commits.find((c) => c.hash === commitHash) ?? null)
    : null;
  const sheetKey =
    repo && commitHash ? commitDetailsKey(repo.path, commitHash) : "";

  // -- list -----------------------------------------------------------------------
  const items = useMemo<Item[]>(() => {
    const list: Item[] = [];
    if (!repo || detailsPending) return list;
    list.push({ type: "changes-group", key: "group-staged", side: "staged" });
    if (stagedOpen) {
      for (const change of staged)
        list.push({
          type: "change",
          key: `s:${change.path}`,
          side: "staged",
          change,
        });
      if (!staged.length)
        list.push({
          type: "empty",
          key: "empty-staged",
          text: "No staged changes",
        });
    }
    list.push({
      type: "changes-group",
      key: "group-changes",
      side: "unstaged",
    });
    if (changesOpen) {
      for (const change of unstaged)
        list.push({
          type: "change",
          key: `u:${change.path}`,
          side: "unstaged",
          change,
        });
      if (!unstaged.length)
        list.push({ type: "empty", key: "empty-changes", text: "No changes" });
    }
    list.push({ type: "graph-header", key: "graph-header" });
    if (!commits.length)
      list.push({
        type: "graph-empty",
        key: "graph-empty",
        text: isUnborn
          ? "No commits yet — commit something to start the history"
          : "No commits to show",
      });
    commits.forEach((row, index) =>
      list.push({ type: "commit", key: `c:${row.hash}`, commit: row, index }),
    );
    return list;
  }, [
    changesOpen,
    commits,
    detailsPending,
    isUnborn,
    repo,
    staged,
    stagedOpen,
    unstaged,
  ]);

  const renderItem = ({ item }: { item: Item }) => {
    switch (item.type) {
      case "changes-group": {
        const isStaged = item.side === "staged";
        const open = isStaged ? stagedOpen : changesOpen;
        const count = isStaged ? staged.length : unstaged.length;
        return (
          <View style={styles.groupRow}>
            <Pressable
              accessibilityRole="button"
              accessibilityState={{ expanded: open }}
              accessibilityLabel={`${isStaged ? "Staged Changes" : "Changes"}, ${count}`}
              onPress={() =>
                isStaged ? setStagedOpen(!open) : setChangesOpen(!open)
              }
              style={styles.groupToggle}
              testID={isStaged ? "git-staged-trigger" : "git-changes-trigger"}
            >
              {open ? (
                <ChevronDown size={15} color={theme.mutedText} />
              ) : (
                <ChevronRight size={15} color={theme.mutedText} />
              )}
              <Text style={[styles.groupTitle, { color: theme.text }]}>
                {isStaged ? "Staged Changes" : "Changes"}
              </Text>
              <Text style={[styles.count, { color: theme.subtleText }]}>
                {count}
              </Text>
            </Pressable>
            {isStaged && staged.length ? (
              <RowAction
                label="Unstage All Changes"
                disabled={busy}
                onPress={() => void controller.unstageAll()}
                testID="git-unstage-all"
              >
                <Minus size={17} color={theme.mutedText} />
              </RowAction>
            ) : null}
            {!isStaged && bulkPaths.length ? (
              <RowAction
                label="Discard All Changes"
                disabled={busy}
                onPress={() => confirmDiscard([...bulkPaths])}
                testID="git-discard-all"
              >
                <Undo2 size={16} color={theme.mutedText} />
              </RowAction>
            ) : null}
            {!isStaged && bulkPaths.length ? (
              <RowAction
                label="Stage All Changes"
                disabled={busy}
                onPress={() => void controller.stageAll()}
                testID="git-stage-all"
              >
                <Plus size={17} color={theme.mutedText} />
              </RowAction>
            ) : null}
          </View>
        );
      }
      case "change":
        return (
          <ChangeRow
            change={item.change}
            side={item.side}
            busy={busy}
            onOpen={() => openWorkingDiff(item.change, item.side)}
            onStage={() => void controller.stage(item.change.path)}
            onUnstage={() => void controller.unstage(item.change.path)}
            onDiscard={() => confirmDiscard([item.change.path])}
          />
        );
      case "empty":
        return (
          <Text style={[styles.emptyRow, { color: theme.subtleText }]}>
            {item.text}
          </Text>
        );
      case "graph-header":
        return (
          <View
            style={[
              styles.graphHeader,
              { backgroundColor: theme.background, borderColor: theme.border },
            ]}
          >
            <NativeMenu
              label="Show branch"
              actions={[
                {
                  id: "",
                  title: "All branches",
                  selected: activeFilter === null,
                },
                ...(repo?.branches ?? []).map((b) => ({
                  id: b.name,
                  title: b.name,
                  selected: activeFilter === b.name,
                })),
              ]}
              onSelect={(id) => setFilter(id || null)}
              style={styles.filterMenu}
              testID="git-graph-filter"
            >
              <View style={[styles.pill, { borderColor: theme.border }]}>
                <GitBranch size={14} color={theme.mutedText} />
                <Text
                  numberOfLines={1}
                  style={[styles.pillText, { color: theme.text }]}
                >
                  {activeFilter ?? "All branches"}
                </Text>
                <ChevronDown size={14} color={theme.subtleText} />
              </View>
            </NativeMenu>
            <View style={styles.flex} />
            <IconButton
              label="Create branch"
              symbol="plus"
              icon={<GitBranchPlus size={18} color={theme.text} />}
              tone="quiet"
              disabled={isUnborn || busy}
              onPress={() =>
                setBranchForm({
                  mode: "create",
                  fromHash: repo?.headHash ?? "",
                  baseLabel: repo?.currentBranch ?? "HEAD",
                })
              }
              testID="git-create-branch"
            />
          </View>
        );
      case "graph-empty":
        return (
          <Text style={[styles.graphEmpty, { color: theme.subtleText }]}>
            {item.text}
          </Text>
        );
      case "commit": {
        const row = item.commit;
        const groups = refGroups.get(row.hash) ?? [];
        return (
          <CommitRow
            commit={row}
            graph={graphRows[item.index]}
            graphWidth={graphWidth}
            groups={
              isDetached && row.hash === repo?.headHash
                ? [
                    {
                      key: "head:HEAD",
                      local: { name: "HEAD", current: true },
                      remotes: [],
                      isHead: true,
                    },
                    ...groups,
                  ]
                : groups
            }
            isHead={row.hash === repo?.headHash}
            actions={commitActions(row)}
            branchActions={branchActions}
            onBranchAction={onBranchAction}
            onAction={(id) => onCommitAction(row, id)}
            onPress={() => openCommit(row.hash)}
          />
        );
      }
    }
  };

  // -- states -----------------------------------------------------------------------
  if (
    (state.loading || (!state.repos.length && !state.error)) &&
    !state.repos.length
  ) {
    return (
      <View style={styles.center} testID="git-loading">
        <ActivityIndicator color={theme.subtleText} />
      </View>
    );
  }
  if (!state.repos.length) {
    return (
      <View
        style={styles.center}
        testID={state.error ? "git-error" : "git-empty"}
      >
        <Text
          style={[
            styles.centerText,
            { color: state.error ? theme.danger : theme.mutedText },
          ]}
        >
          {state.error ?? "No git repositories found in this workspace."}
        </Text>
        <Button
          title={state.error ? "Retry" : "Refresh"}
          compact
          onPress={() => void controller.refresh()}
          style={{ alignSelf: "center" }}
          testID="git-retry"
        />
      </View>
    );
  }

  const header = (
    <View>
      <View style={styles.repoRow}>
        <NativeMenu
          label="Repository"
          actions={state.repos.map((item) => ({
            id: item.id,
            title: item.currentBranch
              ? `${item.name} (${item.currentBranch})`
              : item.name,
            selected: item.id === state.selectedRepoId,
          }))}
          onSelect={(id) => controller.selectRepo(id)}
          style={styles.repoMenu}
          testID="git-repo-select"
        >
          <View
            style={[styles.repoPill, { backgroundColor: theme.surfaceMuted }]}
          >
            <Text
              numberOfLines={1}
              style={[styles.repoName, { color: theme.text }]}
            >
              {repo?.name}
            </Text>
            <ChevronDown size={16} color={theme.mutedText} />
          </View>
        </NativeMenu>
        <IconButton
          label="Fetch and refresh"
          symbol="arrow.clockwise"
          icon={<RefreshCw size={18} color={theme.text} />}
          tone="quiet"
          disabled={busy || refreshing}
          onPress={() => void refresh()}
          testID="git-refresh"
        />
      </View>
      <View style={styles.branchRow}>
        <GitBranch size={14} color={theme.mutedText} />
        <Text
          numberOfLines={1}
          style={[styles.branchName, { color: theme.text }]}
          testID="git-current-branch"
        >
          {repo?.currentBranch ?? "DETACHED HEAD"}
        </Text>
        {ahead > 0 ? (
          <View
            style={styles.counter}
            accessibilityLabel={`${ahead} commits ahead`}
          >
            <ArrowUp size={12} color={theme.mutedText} />
            <Text style={[styles.counterText, { color: theme.mutedText }]}>
              {ahead}
            </Text>
          </View>
        ) : null}
        {behind > 0 ? (
          <View
            style={styles.counter}
            accessibilityLabel={`${behind} commits behind`}
          >
            <ArrowDown size={12} color={theme.mutedText} />
            <Text style={[styles.counterText, { color: theme.mutedText }]}>
              {behind}
            </Text>
          </View>
        ) : null}
        {state.busyOperation ? (
          <ActivityIndicator size="small" color={theme.subtleText} />
        ) : null}
        <Text
          numberOfLines={1}
          ellipsizeMode="head"
          style={[styles.repoPath, { color: theme.subtleText }]}
        >
          {repo?.path}
        </Text>
      </View>

      {detailsPending ? null : (
        <View style={styles.commitBox}>
          <View>
            <TextInput
              value={message}
              onChangeText={setMessage}
              multiline
              placeholder={`Message (commit on ${repo?.currentBranch ? `'${repo.currentBranch}'` : "detached HEAD"})`}
              placeholderTextColor={theme.subtleText}
              style={[
                styles.message,
                { color: theme.text, backgroundColor: theme.surfaceMuted },
              ]}
              testID="git-commit-message"
            />
            <View style={styles.generate}>
              {state.generatingCommitMessage ? (
                <ActivityIndicator
                  size="small"
                  color={theme.mutedText}
                  style={styles.generateSpinner}
                />
              ) : (
                <IconButton
                  label="Generate commit message"
                  symbol="sparkles"
                  icon={<Sparkles size={17} color={theme.mutedText} />}
                  tone="quiet"
                  disabled={!staged.length && !unstaged.length}
                  onPress={() => void generate()}
                  testID="git-generate-message"
                />
              )}
            </View>
          </View>
          <View style={styles.buttons}>
            <View style={styles.split}>
              <Button
                title="Commit"
                symbol="checkmark"
                variant="primary"
                disabled={!canCommit}
                onPress={() => void commit(false)}
                style={styles.flex}
                testID="git-commit"
              />
              <NativeMenu
                label="More commit actions"
                actions={[
                  {
                    id: "commit",
                    title: "Commit",
                    symbol: "checkmark",
                    disabled: !canCommit,
                  },
                  {
                    id: "commit-push",
                    title: "Commit & Push",
                    symbol: "arrow.up.circle",
                    disabled: !canCommit,
                  },
                ]}
                onSelect={(id) => void commit(id === "commit-push")}
                style={styles.chevronMenu}
                testID="git-commit-more"
              >
                <MenuChevron disabled={!canCommit} />
              </NativeMenu>
            </View>
            <View style={styles.split}>
              <Button
                title={primaryLabel}
                variant="outline"
                style={styles.flex}
                disabled={!canPrimary}
                onPress={runPrimary}
                testID="git-remote-primary"
              />
              <NativeMenu
                label="Remote actions"
                actions={[
                  {
                    id: "push",
                    title: hasUpstream
                      ? `Push${ahead ? ` (${ahead})` : ""}`
                      : "Publish",
                    symbol: "arrow.up",
                    disabled: !canPush,
                  },
                  {
                    id: "pull",
                    title: `Pull${behind ? ` (${behind})` : ""}`,
                    symbol: "arrow.down",
                    disabled: !canTrack,
                  },
                  {
                    id: "sync",
                    title: "Sync",
                    symbol: "arrow.triangle.2.circlepath",
                    disabled: !canTrack,
                  },
                ]}
                onSelect={(id) => {
                  if (id === "push") void controller.push();
                  else if (id === "pull") void controller.pull();
                  else void controller.sync();
                }}
                style={styles.chevronMenu}
                testID="git-remote-actions"
              >
                <MenuChevron disabled={busy || isDetached} />
              </NativeMenu>
            </View>
          </View>
          {repo?.mergeState.merging ? (
            <Banner
              tone="warning"
              title="Merge in progress"
              message={`Resolve ${conflictCount(repo.changes)} then commit the result.`}
              actionLabel="Abort merge"
              onAction={busy ? undefined : confirmMergeAbort}
              testID="git-merge-in-progress"
            />
          ) : null}
        </View>
      )}
      {detailsPending ? (
        <View style={styles.detailsLoading} testID="git-details-loading">
          <ActivityIndicator color={theme.subtleText} />
        </View>
      ) : null}
    </View>
  );

  return (
    <View style={styles.root} testID="side-panel-git">
      <FlatList
        data={items}
        keyExtractor={(item) => item.key}
        renderItem={renderItem}
        ListHeaderComponent={header}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
        onEndReachedThreshold={0.6}
        onEndReached={() => {
          if (repo?.hasMore)
            void controller.loadMoreHistory(activeFilter ?? undefined);
        }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => void refresh()}
          />
        }
        ListFooterComponent={
          state.historyLoading ? (
            <ActivityIndicator style={styles.footer} color={theme.subtleText} />
          ) : null
        }
        contentContainerStyle={{ paddingBottom: bottomInset + 72 }}
        initialNumToRender={30}
        windowSize={11}
        testID="git-list"
      />
      {state.notice ? (
        <View
          style={[styles.notice, { bottom: bottomInset + 12 }]}
          pointerEvents="box-none"
        >
          <Banner
            tone={state.notice.tone}
            title={state.notice.title}
            message={state.notice.message ?? ""}
            onDismiss={() => controller.dismissNotice()}
            testID="git-notice"
          />
        </View>
      ) : null}
      <GitDiffSheet target={diff} onClose={() => setDiff(null)} />
      <GitCommitSheet
        commit={sheetCommit}
        details={sheetKey ? (state.commitDetails[sheetKey] ?? null) : null}
        loading={Boolean(sheetKey && state.commitDetailsLoading[sheetKey])}
        error={sheetKey ? (state.commitDetailsError[sheetKey] ?? null) : null}
        actions={sheetCommit ? commitActions(sheetCommit) : []}
        onAction={(id) => {
          if (!sheetCommit) return;
          if (id === "branch-here" || id === "stash-branch")
            setCommitHash(null);
          onCommitAction(sheetCommit, id);
        }}
        onRetry={() =>
          commitHash && void controller.ensureCommitDetails(commitHash)
        }
        onOpenFile={(file: GitCommitFile) =>
          sheetCommit &&
          setDiff({
            path: file.newPath,
            label: sheetCommit.hash.slice(0, 8),
            load: () =>
              controller.commitDiff(
                sheetCommit.hash,
                file.newPath,
                file.oldPath,
              ),
          })
        }
        onClose={() => setCommitHash(null)}
      />
      <GitBranchSheet
        form={branchForm}
        busy={busy}
        onClose={() => setBranchForm(null)}
        onSubmit={(name, checkout) => {
          const form = branchForm;
          if (!form) return Promise.resolve(false);
          if (form.mode === "rename")
            return controller.renameBranch(form.branch, name);
          if (form.stash)
            return controller.createBranchFromStash(form.stash, name);
          return controller.createBranch(name, form.fromHash, checkout);
        }}
      />
    </View>
  );
}

function conflictCount(changes: GitFileChange[]): string {
  const count = changes.filter((change) => change.conflict !== null).length;
  return count === 1 ? "1 conflict" : `${count} conflicts`;
}

/** Chevron half of a split button; the native menu opens on tap. */
function MenuChevron({ disabled }: { disabled: boolean }) {
  const { theme } = useTheme();
  return (
    <View
      style={[
        styles.chevron,
        { backgroundColor: theme.surfaceMuted, opacity: disabled ? 0.5 : 1 },
      ]}
    >
      <ChevronDown size={18} color={theme.text} />
    </View>
  );
}

/** Compact inline action in a list row (stage, unstage, discard). */
function RowAction({
  label,
  disabled,
  onPress,
  children,
  testID,
}: React.PropsWithChildren<{
  label: string;
  disabled?: boolean;
  onPress: () => void;
  testID?: string;
}>) {
  const { theme } = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: Boolean(disabled) }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={4}
      android_ripple={{
        color: theme.controlFill,
        borderless: true,
        radius: 18,
      }}
      style={({ pressed }) => [
        styles.rowAction,
        { opacity: disabled ? 0.4 : pressed ? 0.5 : 1 },
      ]}
      testID={testID}
    >
      {children}
    </Pressable>
  );
}

const ChangeRow = memo(function ChangeRow({
  change,
  side,
  busy,
  onOpen,
  onStage,
  onUnstage,
  onDiscard,
}: {
  change: GitFileChange;
  side: "staged" | "unstaged";
  busy: boolean;
  onOpen: () => void;
  onStage: () => void;
  onUnstage: () => void;
  onDiscard: () => void;
}) {
  const { theme } = useTheme();
  const staged = side === "staged";
  const conflicted = change.conflict !== null;
  const actions: NativeMenuAction[] = [
    { id: "open", title: "Open Changes", symbol: "doc.text.magnifyingglass" },
    ...(staged
      ? [
          {
            id: "unstage",
            title: "Unstage Changes",
            symbol: "minus",
            disabled: busy,
          },
        ]
      : conflicted
        ? []
        : [
            {
              id: "stage",
              title: "Stage Changes",
              symbol: "plus",
              disabled: busy,
            },
            {
              id: "discard",
              title: "Discard Changes",
              symbol: "arrow.uturn.backward",
              destructive: true,
              disabled: busy,
            },
          ]),
    { id: "copy", title: "Copy Path", symbol: "doc.on.doc" },
  ];
  const select = (id: string) => {
    if (id === "open") onOpen();
    else if (id === "stage") onStage();
    else if (id === "unstage") onUnstage();
    else if (id === "discard") onDiscard();
    else if (id === "copy") void Clipboard.setStringAsync(change.path);
  };
  return (
    <NativeContextMenu label={change.path} actions={actions} onSelect={select}>
      {(showMenu) => (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`${change.path}, ${GIT_STATUS_LABELS[change.status]}`}
          accessibilityHint="Shows the changes"
          onPress={onOpen}
          onLongPress={showMenu}
          android_ripple={{ color: theme.controlFill }}
          style={({ pressed }) => [
            styles.changeRow,
            pressed && Platform.OS === "ios"
              ? { backgroundColor: theme.controlFill }
              : null,
          ]}
          testID={staged ? "git-staged-file" : "git-changed-file"}
        >
          <WorkspaceFileIcon path={change.path} size={18} />
          <Text
            numberOfLines={1}
            style={[styles.changeName, { color: theme.text }]}
          >
            {fileName(change.path)}
            {dirName(change.path) ? (
              <Text
                style={{ color: theme.subtleText }}
              >{`  ${dirName(change.path)}`}</Text>
            ) : null}
          </Text>
          {staged ? (
            <RowAction
              label="Unstage Changes"
              disabled={busy}
              onPress={onUnstage}
              testID="git-unstage-file"
            >
              <Minus size={17} color={theme.mutedText} />
            </RowAction>
          ) : conflicted ? null : (
            <>
              <RowAction
                label="Discard Changes"
                disabled={busy}
                onPress={onDiscard}
                testID="git-discard-file"
              >
                <Undo2 size={16} color={theme.mutedText} />
              </RowAction>
              <RowAction
                label="Stage Changes"
                disabled={busy}
                onPress={onStage}
                testID="git-stage-file"
              >
                <Plus size={17} color={theme.mutedText} />
              </RowAction>
            </>
          )}
          <Text
            style={[
              styles.status,
              { color: statusColor(theme, change.status) },
            ]}
          >
            {change.status}
          </Text>
        </Pressable>
      )}
    </NativeContextMenu>
  );
});

const GraphCell = memo(function GraphCell({
  row,
  width,
}: {
  row: GraphRow | undefined;
  width: number;
}) {
  const { theme } = useTheme();
  if (!row) return <View style={{ width }} />;
  const node = row.node;
  return (
    <Svg
      width={width}
      height={ROW_HEIGHT}
      style={{ width, height: ROW_HEIGHT }}
    >
      {row.paths.map((path, index) => (
        <React.Fragment key={index}>
          <Path
            d={path.d}
            stroke={theme.background}
            strokeOpacity={0.75}
            strokeWidth={4}
            fill="none"
          />
          <Path
            d={path.d}
            stroke={branchColor(theme, path.colour)}
            strokeWidth={2}
            fill="none"
          />
        </React.Fragment>
      ))}
      {node ? (
        <Circle
          cx={node.cx}
          cy={ROW_HEIGHT / 2}
          r={NODE_RADIUS}
          fill={
            node.current ? theme.background : branchColor(theme, node.colour)
          }
          stroke={branchColor(theme, node.colour)}
          strokeWidth={node.current ? 2 : 1}
        />
      ) : null}
    </Svg>
  );
});

const CommitRow = memo(function CommitRow({
  commit,
  graph,
  graphWidth,
  groups,
  isHead,
  actions,
  branchActions,
  onBranchAction,
  onAction,
  onPress,
}: {
  commit: GitCommit;
  graph: GraphRow | undefined;
  graphWidth: number;
  groups: GitRefGroup[];
  isHead: boolean;
  actions: NativeMenuAction[];
  branchActions: (group: GitRefGroup) => NativeMenuAction[];
  onBranchAction: (id: string) => void;
  onAction: (id: string) => void;
  onPress: () => void;
}) {
  const { theme } = useTheme();
  const colour = graph?.node?.colour ?? 0;
  const stashGraph =
    commit.stash && graph?.node
      ? { ...graph, node: { ...graph.node, current: true } }
      : graph;
  return (
    <NativeContextMenu
      label={commit.stash?.selector ?? commit.hash.slice(0, 8)}
      actions={actions}
      onSelect={onAction}
    >
      {(showMenu) => (
        // Badges open their own menus, so the row press sits behind the
        // content instead of wrapping it.
        <View style={styles.commitRow}>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`${commit.message}, ${commit.author}, ${formatTimeAgo(commit.timestamp)}`}
            accessibilityHint="Shows the commit details"
            onPress={onPress}
            onLongPress={showMenu}
            android_ripple={{ color: theme.controlFill }}
            style={({ pressed }) => [
              StyleSheet.absoluteFill,
              pressed && Platform.OS === "ios"
                ? { backgroundColor: theme.controlFill }
                : null,
            ]}
            testID="git-graph-row"
          />
          <View pointerEvents="none">
            <GraphCell row={stashGraph} width={graphWidth} />
          </View>
          <View style={styles.commitBody} pointerEvents="box-none">
            {commit.stash ? (
              <View
                pointerEvents="none"
                style={[
                  styles.badge,
                  styles.stashBadge,
                  { borderColor: theme.subtleText },
                ]}
              >
                <Archive size={11} color={theme.mutedText} />
                <Text style={[styles.badgeText, { color: theme.mutedText }]}>
                  {commit.stash.selector.replace(/^stash/, "")}
                </Text>
              </View>
            ) : null}
            {groups.map((group) => (
              <RefBadge
                key={group.key}
                group={group}
                colour={colour}
                actions={group.isHead ? [] : branchActions(group)}
                onAction={onBranchAction}
              />
            ))}
            <View style={styles.commitMessageBox} pointerEvents="none">
              <Text
                numberOfLines={1}
                style={[
                  styles.commitMessage,
                  { color: theme.text },
                  isHead && styles.bold,
                ]}
              >
                {commit.message}
              </Text>
            </View>
          </View>
          <View pointerEvents="none">
            <Text style={[styles.date, { color: theme.subtleText }]}>
              {formatTimeAgo(commit.timestamp)}
            </Text>
          </View>
        </View>
      )}
    </NativeContextMenu>
  );
});

function badgeStyle(theme: OpenCuriaTheme, group: GitRefGroup, colour: number) {
  if (!group.local)
    return {
      borderColor: theme.border,
      color: theme.mutedText,
      backgroundColor: "transparent",
    };
  const color = branchColor(theme, colour);
  if (group.local.current)
    return {
      borderColor: color,
      color: branchActiveForeground(theme),
      backgroundColor: color,
    };
  return { borderColor: color, color, backgroundColor: `${color}1F` };
}

function RefBadge({
  group,
  colour,
  actions,
  onAction,
}: {
  group: GitRefGroup;
  colour: number;
  actions: NativeMenuAction[];
  onAction: (id: string) => void;
}) {
  const { theme } = useTheme();
  const style = badgeStyle(theme, group, colour);
  const label = group.local?.name ?? group.remotes[0]?.full ?? group.key;
  const badge = (
    <View
      style={[
        styles.badge,
        {
          borderColor: style.borderColor,
          backgroundColor: style.backgroundColor,
        },
      ]}
    >
      <GitBranch size={11} color={style.color} />
      <Text
        numberOfLines={1}
        style={[
          styles.badgeText,
          { color: style.color },
          group.local?.current && styles.bold,
        ]}
      >
        {label}
      </Text>
      {group.local
        ? group.remotes.map((chip) => (
            <Text
              key={chip.full}
              style={[
                styles.remoteChip,
                { color: style.color, borderLeftColor: `${style.color}66` },
              ]}
            >
              {chip.remote}
            </Text>
          ))
        : null}
    </View>
  );
  if (!actions.length) return <View pointerEvents="none">{badge}</View>;
  return (
    <NativeMenu
      label={label}
      actions={actions}
      onSelect={onAction}
      style={styles.badgeMenu}
      testID={`git-branch-tag-${label}`}
    >
      {badge}
    </NativeMenu>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  flex: { flex: 1 },
  bold: { fontWeight: "600" },
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    padding: 32,
  },
  centerText: { fontSize: 15, textAlign: "center" },
  repoRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingLeft: 16,
    paddingRight: 8,
    paddingTop: 4,
  },
  repoMenu: { flex: 1, minHeight: 40 },
  repoPill: {
    flexDirection: "row",
    alignItems: "center",
    alignSelf: "flex-start",
    gap: 6,
    borderRadius: 12,
    paddingHorizontal: 12,
    minHeight: 36,
    maxWidth: "100%",
  },
  repoName: { fontSize: 17, fontWeight: "600", flexShrink: 1 },
  branchRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 18,
    paddingTop: 6,
  },
  branchName: { fontSize: 14, fontWeight: "600", flexShrink: 1 },
  counter: { flexDirection: "row", alignItems: "center" },
  counterText: { fontSize: 13, fontVariant: ["tabular-nums"] },
  repoPath: { flex: 1, textAlign: "right", fontSize: 12, marginLeft: 8 },
  commitBox: {
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 6,
    gap: 10,
  },
  message: {
    minHeight: 64,
    maxHeight: 140,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingTop: 12,
    paddingBottom: 12,
    paddingRight: 48,
    fontSize: 15,
    textAlignVertical: "top",
  },
  generate: {
    position: "absolute",
    top: 0,
    right: 0,
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  generateSpinner: { margin: 12 },
  buttons: { flexDirection: "row", gap: 10 },
  split: { flex: 1, flexDirection: "row", alignItems: "center", gap: 4 },
  chevronMenu: { width: 36, height: 44 },
  chevron: {
    width: 36,
    height: 44,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  detailsLoading: { paddingVertical: 32, alignItems: "center" },
  groupRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 12,
    paddingRight: 8,
    minHeight: 40,
    marginTop: 6,
  },
  groupToggle: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    minHeight: 40,
  },
  groupTitle: { fontSize: 15, fontWeight: "600" },
  count: { fontSize: 13, fontVariant: ["tabular-nums"] },
  rowAction: {
    width: 36,
    height: 36,
    alignItems: "center",
    justifyContent: "center",
  },
  changeRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 44,
    paddingLeft: 18,
    paddingRight: 12,
  },
  changeName: { flex: 1, fontSize: 15 },
  status: { width: 16, textAlign: "center", fontSize: 12, fontWeight: "700" },
  emptyRow: { paddingHorizontal: 40, paddingVertical: 8, fontSize: 13 },
  graphHeader: {
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 12,
    paddingRight: 8,
    minHeight: 48,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: StyleSheet.hairlineWidth,
    marginTop: 12,
  },
  filterMenu: { minHeight: 36, maxWidth: "70%" },
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    paddingHorizontal: 10,
    minHeight: 34,
  },
  pillText: { fontSize: 14, flexShrink: 1 },
  graphEmpty: { textAlign: "center", padding: 24, fontSize: 13 },
  commitRow: {
    flexDirection: "row",
    alignItems: "center",
    height: ROW_HEIGHT,
    paddingRight: 12,
  },
  commitBody: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    minWidth: 0,
    overflow: "hidden",
  },
  commitMessageBox: { flex: 1, minWidth: 40 },
  commitMessage: { fontSize: 14 },
  date: { fontSize: 12, marginLeft: 8, fontVariant: ["tabular-nums"] },
  badge: {
    flexDirection: "row",
    alignItems: "center",
    gap: 3,
    height: 20,
    borderRadius: 6,
    borderWidth: 1,
    paddingHorizontal: 5,
    maxWidth: 140,
    overflow: "hidden",
  },
  stashBadge: { borderStyle: "dashed" },
  badgeText: { fontSize: 11, flexShrink: 1 },
  remoteChip: {
    fontSize: 11,
    fontWeight: "500",
    fontStyle: "italic",
    borderLeftWidth: 1,
    paddingLeft: 4,
    marginLeft: 2,
  },
  badgeMenu: { flexShrink: 1 },
  footer: { padding: 16 },
  notice: { position: "absolute", left: 12, right: 12 },
});
