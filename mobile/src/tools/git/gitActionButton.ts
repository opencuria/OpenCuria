import {
  bulkUnstagedGitPaths,
  currentGitBranch,
  stagedGitChanges,
} from "@opencuria/chat-core/gitModel";
import type { GitRepo } from "@opencuria/chat-core/gitTypes";

export type GitActionKind = "commit" | "publish" | "sync";

/** The single source-control action button, as in VS Code. */
export interface GitActionButton {
  kind: GitActionKind;
  title: string;
  /** SF Symbol shown on iOS. */
  symbol: string;
  enabled: boolean;
  /** The running operation is this button's own action. */
  running: boolean;
  /** Nothing is staged yet: committing stages every change first. */
  stagesAll: boolean;
}

/** Operations (controller busy labels) that belong to each button. */
const OPERATIONS: Record<GitActionKind, readonly string[]> = {
  commit: ["commit"],
  publish: ["push"],
  sync: ["sync", "push", "pull"],
};

/**
 * VS Code's SCM action button: Commit while there are changes, otherwise
 * Publish Branch (no upstream yet), otherwise Sync Changes (ahead or behind),
 * otherwise a disabled Commit. During a merge it continues the merge once no
 * conflict is left. Like VS Code's suggested smart commit, Commit includes
 * unstaged changes when nothing is staged.
 */
export function gitActionButton(
  repo: GitRepo | null,
  busyOperation: string | null,
): GitActionButton {
  const busy = busyOperation !== null;
  const button = (
    kind: GitActionKind,
    title: string,
    symbol: string,
    enabled: boolean,
    stagesAll = false,
  ): GitActionButton => ({
    kind,
    title,
    symbol,
    enabled: enabled && !busy,
    running: busy && OPERATIONS[kind].includes(busyOperation),
    stagesAll,
  });

  const staged = stagedGitChanges(repo).length;
  const unstaged = bulkUnstagedGitPaths(repo).length;
  const hasChanges = staged + unstaged > 0;
  const merging = Boolean(repo?.mergeState.merging);
  const conflicted = Boolean(repo?.changes.some((c) => c.conflict !== null));
  const commit = button(
    "commit",
    merging ? "Continue" : "Commit",
    "arrow.up",
    !conflicted && (hasChanges || merging),
    staged === 0 && unstaged > 0,
  );
  if (!repo || hasChanges || merging) return commit;

  const branch = currentGitBranch(repo);
  if (!branch) return commit;
  if (!branch.upstream)
    return repo.remotes.length
      ? button("publish", "Publish Branch", "arrow.up", true)
      : commit;
  const ahead = branch.ahead ?? 0;
  const behind = branch.behind ?? 0;
  if (!ahead && !behind) return commit;
  const counts = `${behind ? ` ${behind}↓` : ""}${ahead ? ` ${ahead}↑` : ""}`;
  return button(
    "sync",
    `Sync Changes${counts}`,
    "arrow.up.arrow.down",
    true,
  );
}
