import { ApiRequestError, type OpenCuriaClient } from "@opencuria/client";
import {
  GIT_AUTO_FETCH_THROTTLE_MS,
  GIT_DETAILS_POLL_MS,
  GIT_HISTORY_LIMIT,
  GIT_POLL_INTERVAL_MS,
  bulkUnstagedGitPaths,
  chunkGitPaths,
  commitDetailsKey,
  conflictSnapshotOf,
  currentGitBranch,
  gitPushRemote,
  isValidStashSelector,
  mergeGitHistoryPage,
  normalizeCommitDetails,
  normalizeRepoSnapshot,
  normalizeRepoSummary,
  remoteBranchLocalName,
  shallowGitRepo,
  stagedGitChanges,
} from "@opencuria/chat-core/gitModel";
import type {
  GitCommitDetails,
  GitOperationRequest,
  GitRepo,
  GitRepoSummary,
} from "@opencuria/chat-core/gitTypes";

export type GitNoticeTone = "success" | "info" | "warning" | "error";

/** Feedback for a finished action, shown briefly above the panel's end. */
export interface GitNotice {
  id: number;
  tone: GitNoticeTone;
  title: string;
  message?: string;
}

/** Immutable state published to the Git panel. */
export interface GitSnapshot {
  repos: GitRepoSummary[];
  /** Full details keyed by repo path, loaded lazily. */
  repoDetails: Readonly<Record<string, GitRepo>>;
  detailsLoading: Readonly<Record<string, boolean>>;
  selectedRepoId: string;
  /** First load of the summaries. */
  loading: boolean;
  /** Load error while no repository is known yet. */
  error: string | null;
  /** Label of the running mutation; mutations are serialized. */
  busyOperation: string | null;
  generatingCommitMessage: boolean;
  historyLoading: boolean;
  expandedCommitHash: string | null;
  commitDetails: Readonly<Record<string, GitCommitDetails>>;
  commitDetailsLoading: Readonly<Record<string, boolean>>;
  commitDetailsError: Readonly<Record<string, string | null>>;
  notice: GitNotice | null;
}

interface OperationFeedback {
  successTitle?: string;
  successMessage?: string;
  errorTitle?: string;
}

const INITIAL: GitSnapshot = {
  repos: [],
  repoDetails: {},
  detailsLoading: {},
  selectedRepoId: "",
  loading: false,
  error: null,
  busyOperation: null,
  generatingCommitMessage: false,
  historyLoading: false,
  expandedCommitHash: null,
  commitDetails: {},
  commitDetailsLoading: {},
  commitDetailsError: {},
  notice: null,
};

/** Last selected repository per workspace for this app session. */
const selectedRepos = new Map<string, string>();

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message || "Unknown error";
  return typeof error === "string" && error ? error : "Unknown error";
}

/** The selected repository, falling back to its summary until details load. */
export function selectedGitRepo(snapshot: GitSnapshot): GitRepo | null {
  const summary = snapshot.repos.find(
    (repo) => repo.id === snapshot.selectedRepoId,
  );
  if (!summary) return null;
  return snapshot.repoDetails[summary.path] ?? shallowGitRepo(summary);
}

/**
 * Git state of one workspace for the mobile Git panel. Mirrors the webapp
 * Git store: light summaries plus lazy details, silent polling while the
 * panel is visible, serialized typed mutations (409 conflict snapshots are
 * applied) and lazy commit details. Snapshots are immutable.
 */
export class GitController {
  private snapshot: GitSnapshot = INITIAL;
  private readonly listeners = new Set<() => void>();
  private disposed = false;
  private visible = false;
  private generation = 0;
  private pollGeneration = 0;
  private summaryTimer: ReturnType<typeof setInterval> | null = null;
  private detailsTimer: ReturnType<typeof setInterval> | null = null;
  private refreshTask: Promise<boolean> | null = null;
  private readonly detailsInFlight = new Set<string>();
  private opQueue: Promise<void> = Promise.resolve();
  private lastAutoFetch: { key: string; at: number } | null = null;
  private noticeId = 0;
  private noticeTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly client: OpenCuriaClient,
    readonly workspaceId: string,
  ) {}

  readonly getSnapshot = (): GitSnapshot => this.snapshot;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  dispose(): void {
    this.disposed = true;
    this.generation += 1;
    this.stopPolling();
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    this.listeners.clear();
  }

  // -- state ------------------------------------------------------------------

  private set(patch: Partial<GitSnapshot>): void {
    if (this.disposed) return;
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  private current(gen: number): boolean {
    return !this.disposed && gen === this.generation;
  }

  get repo(): GitRepo | null {
    return selectedGitRepo(this.snapshot);
  }

  private notify(tone: GitNoticeTone, title: string, message?: string): void {
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    const id = ++this.noticeId;
    this.set({ notice: { id, tone, title, message } });
    // Errors stay until dismissed; confirmations fade on their own.
    if (tone !== "error" && tone !== "warning") {
      this.noticeTimer = setTimeout(() => this.dismissNotice(id), 2800);
    }
  }

  dismissNotice(id?: number): void {
    if (id !== undefined && this.snapshot.notice?.id !== id) return;
    this.set({ notice: null });
  }

  private applySummaries(next: GitRepoSummary[]): void {
    let repoDetails = this.snapshot.repoDetails;
    for (const summary of next) {
      const detail = repoDetails[summary.path];
      if (
        detail &&
        (detail.currentBranch !== summary.currentBranch ||
          detail.headHash !== summary.headHash)
      ) {
        repoDetails = {
          ...repoDetails,
          [summary.path]: {
            ...detail,
            currentBranch: summary.currentBranch,
            headHash: summary.headHash,
          },
        };
      }
    }
    const previous = this.snapshot.repos.find(
      (repo) => repo.id === this.snapshot.selectedRepoId,
    );
    const remembered = selectedRepos.get(this.workspaceId);
    const selected =
      next.find((repo) => repo.id === remembered) ??
      next.find((repo) => repo.id === this.snapshot.selectedRepoId) ??
      (previous
        ? next.find((repo) => repo.path === previous.path)
        : undefined) ??
      next[0];
    const selectedRepoId = selected?.id ?? "";
    if (selectedRepoId) selectedRepos.set(this.workspaceId, selectedRepoId);
    const changed = selectedRepoId !== this.snapshot.selectedRepoId;
    this.set({
      repos: next,
      repoDetails,
      selectedRepoId,
      ...(changed ? { expandedCommitHash: null } : {}),
    });
  }

  private applyRepoSnapshot(repo: GitRepo): void {
    const summary: GitRepoSummary = {
      id: repo.id,
      name: repo.name,
      path: repo.path,
      currentBranch: repo.currentBranch,
      headHash: repo.headHash,
    };
    const index = this.snapshot.repos.findIndex(
      (item) => item.path === repo.path || item.id === repo.id,
    );
    const repos = [...this.snapshot.repos];
    if (index === -1) repos.push(summary);
    else repos[index] = summary;
    this.set({
      repos,
      repoDetails: { ...this.snapshot.repoDetails, [repo.path]: repo },
      selectedRepoId: this.snapshot.selectedRepoId || repo.id,
    });
  }

  private invalidateCommitDetails(repoPath: string): void {
    const prefix = `${repoPath}::`;
    const without = <T>(record: Readonly<Record<string, T>>) =>
      Object.fromEntries(
        Object.entries(record).filter(([key]) => !key.startsWith(prefix)),
      ) as Record<string, T>;
    this.set({
      commitDetails: without(this.snapshot.commitDetails),
      commitDetailsLoading: without(this.snapshot.commitDetailsLoading),
      commitDetailsError: without(this.snapshot.commitDetailsError),
    });
  }

  // -- lifecycle --------------------------------------------------------------

  /**
   * The panel became visible or hidden. Visible: load summaries, then the
   * selected repo's details, fetch silently (throttled) and poll. Hidden:
   * stop polling; late results are dropped.
   */
  setVisible(visible: boolean): void {
    if (this.visible === visible) return;
    this.visible = visible;
    if (!visible) {
      this.stopPolling();
      return;
    }
    void this.activate();
  }

  private async activate(): Promise<void> {
    const gen = this.generation;
    await this.refresh({ silent: this.snapshot.repos.length > 0 });
    if (!this.current(gen) || !this.visible) return;
    this.autoFetch();
    this.startPolling();
  }

  private autoFetch(): void {
    const repo = this.repo;
    if (!repo || !this.visible || this.snapshot.busyOperation) return;
    const key = repo.path;
    const now = Date.now();
    if (
      this.lastAutoFetch?.key === key &&
      now - this.lastAutoFetch.at < GIT_AUTO_FETCH_THROTTLE_MS
    )
      return;
    this.lastAutoFetch = { key, at: now };
    void this.fetchRemote({ silent: true });
  }

  /** Summaries, then the selected repo's details (unless `withDetails` is false). */
  refresh(
    options: { silent?: boolean; withDetails?: boolean; force?: boolean } = {},
  ): Promise<boolean> {
    if (this.refreshTask) {
      // A silent summaries poll may be in flight; still load details after it.
      if (options.withDetails === false) return this.refreshTask;
      return this.refreshTask.then(async (ok) => {
        const repo = this.repo;
        if (repo) await this.ensureDetails(repo.path, { silent: true });
        return ok;
      });
    }
    const gen = this.generation;
    const silent = options.silent === true;
    const holder: { task?: Promise<boolean> } = {};
    const task = (async () => {
      if (!silent) this.set({ loading: true, error: null });
      try {
        const res = await this.client.git.repos(this.workspaceId);
        if (!this.current(gen)) return false;
        this.applySummaries((res.repos ?? []).map(normalizeRepoSummary));
        if (!silent) this.set({ loading: false, error: null });
        const repo = this.repo;
        if (options.withDetails !== false && repo)
          await this.ensureDetails(repo.path, {
            force: options.force ?? silent,
            silent,
          });
        return this.current(gen);
      } catch (error) {
        if (!this.current(gen)) return false;
        const message = errorMessage(error);
        if (!silent || this.snapshot.repos.length === 0)
          this.set({ loading: false, error: message });
        return false;
      } finally {
        if (this.refreshTask === holder.task) this.refreshTask = null;
      }
    })();
    holder.task = task;
    this.refreshTask = task;
    return task;
  }

  /** Load (or reload with `force`) one repo's full details. */
  async ensureDetails(
    repoPath: string,
    options: { force?: boolean; silent?: boolean } = {},
  ): Promise<boolean> {
    if (this.detailsInFlight.has(repoPath)) return false;
    if (!options.force && this.snapshot.repoDetails[repoPath]) return true;
    const gen = this.generation;
    this.detailsInFlight.add(repoPath);
    this.set({
      detailsLoading: { ...this.snapshot.detailsLoading, [repoPath]: true },
    });
    try {
      const res = await this.client.git.repo(this.workspaceId, repoPath);
      if (!this.current(gen)) return false;
      this.applyRepoSnapshot(normalizeRepoSnapshot(res.snapshot));
      return true;
    } catch (error) {
      if (!this.current(gen)) return false;
      if (!options.silent && !Object.keys(this.snapshot.repoDetails).length)
        this.set({ error: errorMessage(error) });
      return false;
    } finally {
      this.detailsInFlight.delete(repoPath);
      if (this.current(gen)) {
        const detailsLoading = { ...this.snapshot.detailsLoading };
        delete detailsLoading[repoPath];
        this.set({ detailsLoading });
      }
    }
  }

  private startPolling(): void {
    if (this.summaryTimer || this.detailsTimer) return;
    const poll = ++this.pollGeneration;
    this.summaryTimer = setInterval(() => {
      if (poll !== this.pollGeneration || this.snapshot.busyOperation) return;
      void this.refresh({ silent: true, withDetails: false });
    }, GIT_POLL_INTERVAL_MS);
    this.detailsTimer = setInterval(() => {
      const repo = this.repo;
      if (poll !== this.pollGeneration || this.snapshot.busyOperation || !repo)
        return;
      // Reload loaded details; load them if an earlier request was lost.
      void this.ensureDetails(repo.path, {
        force: Boolean(this.snapshot.repoDetails[repo.path]),
        silent: true,
      });
    }, GIT_DETAILS_POLL_MS);
  }

  private stopPolling(): void {
    this.pollGeneration += 1;
    if (this.summaryTimer) clearInterval(this.summaryTimer);
    if (this.detailsTimer) clearInterval(this.detailsTimer);
    this.summaryTimer = null;
    this.detailsTimer = null;
  }

  /** Explicit Fetch + Refresh from the panel (errors are reported). */
  async fetchAndRefresh(): Promise<void> {
    if (this.repo) {
      this.lastAutoFetch = { key: this.repo.path, at: Date.now() };
      await this.fetchRemote();
    }
    await this.refresh({ silent: true, force: true });
  }

  selectRepo(id: string): void {
    const summary = this.snapshot.repos.find((repo) => repo.id === id);
    if (!summary || id === this.snapshot.selectedRepoId) return;
    selectedRepos.set(this.workspaceId, id);
    this.set({ selectedRepoId: id, expandedCommitHash: null });
    const gen = this.generation;
    void this.ensureDetails(summary.path).then((ok) => {
      if (ok && this.current(gen)) void this.fetchRemote({ silent: true });
    });
  }

  // -- commit details & history -----------------------------------------------

  async toggleCommitDetails(hash: string): Promise<void> {
    if (this.snapshot.expandedCommitHash === hash) {
      this.set({ expandedCommitHash: null });
      return;
    }
    this.set({ expandedCommitHash: hash });
    await this.ensureCommitDetails(hash);
  }

  closeCommitDetails(): void {
    this.set({ expandedCommitHash: null });
  }

  async ensureCommitDetails(hash: string): Promise<void> {
    const repo = this.repo;
    if (!repo) return;
    const key = commitDetailsKey(repo.path, hash);
    if (
      this.snapshot.commitDetails[key] ||
      this.snapshot.commitDetailsLoading[key]
    )
      return;
    const gen = this.generation;
    this.set({
      commitDetailsLoading: {
        ...this.snapshot.commitDetailsLoading,
        [key]: true,
      },
      commitDetailsError: { ...this.snapshot.commitDetailsError, [key]: null },
    });
    try {
      const res = await this.client.git.commitDetails(
        this.workspaceId,
        repo.path,
        hash,
      );
      if (!this.current(gen)) return;
      this.set({
        commitDetails: {
          ...this.snapshot.commitDetails,
          [key]: normalizeCommitDetails(res.details),
        },
      });
    } catch (error) {
      if (!this.current(gen)) return;
      this.set({
        commitDetailsError: {
          ...this.snapshot.commitDetailsError,
          [key]: errorMessage(error),
        },
      });
    } finally {
      if (this.current(gen))
        this.set({
          commitDetailsLoading: {
            ...this.snapshot.commitDetailsLoading,
            [key]: false,
          },
        });
    }
  }

  /** Append the next history page (optionally filtered to one branch). */
  async loadMoreHistory(branch?: string): Promise<void> {
    const repo = this.repo;
    if (
      !repo ||
      !repo.hasMore ||
      this.snapshot.historyLoading ||
      this.snapshot.busyOperation ||
      !this.snapshot.repoDetails[repo.path]
    )
      return;
    const gen = this.generation;
    const pageSize = repo.historyLimit ?? GIT_HISTORY_LIMIT;
    const skip = repo.commits.length;
    this.set({ historyLoading: true });
    try {
      const page = await this.client.git.history(this.workspaceId, repo.path, {
        limit: pageSize,
        skip,
        ...(branch ? { branch } : {}),
      });
      const current = this.snapshot.repoDetails[repo.path];
      if (!this.current(gen) || !current) return;
      this.set({
        repoDetails: {
          ...this.snapshot.repoDetails,
          [repo.path]: mergeGitHistoryPage(current, page, skip, pageSize),
        },
      });
    } catch (error) {
      if (this.current(gen))
        this.notify(
          "error",
          "Failed to load more history",
          errorMessage(error),
        );
    } finally {
      if (this.current(gen)) this.set({ historyLoading: false });
    }
  }

  // -- mutations ----------------------------------------------------------------

  /**
   * Run one typed mutation against the selected repo: serialized, with busy
   * state, snapshot application (including 409 conflict snapshots) and a
   * notice. Never throws; true on success.
   */
  private runOperation(
    label: string,
    build: (repo: GitRepo) => Omit<GitOperationRequest, "repo_path">,
    feedback: OperationFeedback = {},
    options: { silent?: boolean } = {},
  ): Promise<boolean> {
    const repo = this.repo;
    if (!repo) {
      if (!options.silent) this.notify("error", "No repository selected");
      return Promise.resolve(false);
    }
    const repoPath = repo.path;
    const payload = build(repo);
    const gen = this.generation;
    const task = this.opQueue.then(async (): Promise<boolean> => {
      if (!this.current(gen)) return false;
      this.set({ busyOperation: label });
      try {
        const res = await this.client.git.operation(this.workspaceId, {
          ...payload,
          repo_path: repoPath,
        });
        if (!this.current(gen)) return false;
        this.applyRepoSnapshot(normalizeRepoSnapshot(res.snapshot));
        this.invalidateCommitDetails(repoPath);
        if (this.snapshot.expandedCommitHash)
          void this.ensureCommitDetails(this.snapshot.expandedCommitHash);
        if (feedback.successTitle && !options.silent)
          this.notify(
            "success",
            feedback.successTitle,
            feedback.successMessage,
          );
        return true;
      } catch (error) {
        if (!this.current(gen)) return false;
        if (
          error instanceof ApiRequestError &&
          error.status === 409 &&
          error.code === "conflict"
        ) {
          const snapshot = conflictSnapshotOf(error.data);
          if (snapshot) {
            this.applyRepoSnapshot(normalizeRepoSnapshot(snapshot));
            this.invalidateCommitDetails(repoPath);
            if (!options.silent)
              this.notify("warning", "Merge conflict", errorMessage(error));
            return false;
          }
        }
        if (!options.silent)
          this.notify(
            "error",
            feedback.errorTitle ?? "Git operation failed",
            errorMessage(error),
          );
        return false;
      } finally {
        if (this.current(gen)) this.set({ busyOperation: null });
      }
    });
    this.opQueue = task.then(
      () => undefined,
      () => undefined,
    );
    return task;
  }

  private async runPathsOperation(
    operation: "stage" | "unstage" | "discard",
    paths: string[],
    feedback: OperationFeedback,
  ): Promise<boolean> {
    const batches = chunkGitPaths(paths);
    for (const [index, batch] of batches.entries()) {
      const last = index === batches.length - 1;
      const ok = await this.runOperation(
        operation,
        () => ({ operation, paths: batch }),
        last ? feedback : { errorTitle: feedback.errorTitle },
      );
      if (!ok) return false;
    }
    return true;
  }

  stage(path: string): Promise<boolean> {
    return this.runOperation(
      "stage",
      () => ({ operation: "stage", paths: [path] }),
      {
        errorTitle: "Stage failed",
      },
    );
  }

  unstage(path: string): Promise<boolean> {
    return this.runOperation(
      "unstage",
      () => ({ operation: "unstage", paths: [path] }),
      { errorTitle: "Unstage failed" },
    );
  }

  stageAll(): Promise<boolean> {
    return this.runPathsOperation("stage", bulkUnstagedGitPaths(this.repo), {
      errorTitle: "Stage all failed",
    });
  }

  unstageAll(): Promise<boolean> {
    return this.runPathsOperation(
      "unstage",
      stagedGitChanges(this.repo).map((change) => change.path),
      { errorTitle: "Unstage all failed" },
    );
  }

  discard(path: string): Promise<boolean> {
    return this.runOperation(
      "discard",
      () => ({ operation: "discard", paths: [path] }),
      {
        successTitle: "Discarded changes",
        successMessage: path,
        errorTitle: "Discard failed",
      },
    );
  }

  /** Discard the confirmed paths (untracked files are deleted). */
  discardAll(paths: string[]): Promise<boolean> {
    return this.runPathsOperation("discard", paths, {
      successTitle: "Discarded changes",
      successMessage: paths.length === 1 ? paths[0] : `${paths.length} files`,
      errorTitle: "Discard all failed",
    });
  }

  /** Small-model subject for staged (else all) changes; null on failure. */
  async generateCommitMessage(): Promise<string | null> {
    const repo = this.repo;
    if (!repo || this.snapshot.generatingCommitMessage) return null;
    const gen = this.generation;
    this.set({ generatingCommitMessage: true });
    try {
      const res = await this.client.git.commitMessage(
        this.workspaceId,
        repo.path,
      );
      if (!this.current(gen) || this.repo?.path !== repo.path) return null;
      return res.message?.trim() || null;
    } catch (error) {
      if (this.current(gen))
        this.notify(
          "error",
          "Failed to generate commit message",
          errorMessage(error),
        );
      return null;
    } finally {
      if (this.current(gen)) this.set({ generatingCommitMessage: false });
    }
  }

  /** Commit staged changes; with `andPush` the push runs after a commit. */
  async commit(message: string, andPush = false): Promise<boolean> {
    const repo = this.repo;
    const trimmed = message.trim();
    if (!repo || !trimmed || !stagedGitChanges(repo).length) return false;
    const ok = await this.runOperation(
      "commit",
      () => ({ operation: "commit", message: trimmed }),
      {
        successTitle: `Committed on ${repo.currentBranch ?? "detached HEAD"}`,
        errorTitle: "Commit failed",
      },
    );
    return ok && andPush ? this.push() : ok;
  }

  push(): Promise<boolean> {
    const repo = this.repo;
    const branch = currentGitBranch(repo);
    if (!repo || !branch) {
      this.notify("error", "Cannot push while HEAD is detached");
      return Promise.resolve(false);
    }
    if (branch.ahead === 0 && branch.upstream) {
      this.notify("info", "Everything up to date");
      return Promise.resolve(true);
    }
    const remote = gitPushRemote(repo);
    if (!branch.upstream)
      return this.runOperation(
        "push",
        () => ({
          operation: "push",
          ...(remote ? { remote } : {}),
          set_upstream: true,
        }),
        {
          successTitle: `Published ${branch.name} to ${remote ?? "origin"}`,
          errorTitle: "Push failed",
        },
      );
    return this.runOperation(
      "push",
      () => ({ operation: "push", ...(remote ? { remote } : {}) }),
      {
        successTitle: `Pushed to ${remote ?? "origin"}/${branch.name}`,
        errorTitle: "Push failed",
      },
    );
  }

  fetchRemote(options: { silent?: boolean } = {}): Promise<boolean> {
    const repo = this.repo;
    if (!repo || this.snapshot.busyOperation || repo.remotes.length === 0)
      return Promise.resolve(false);
    return this.runOperation(
      "fetch",
      () => ({ operation: "fetch" }),
      { errorTitle: "Fetch failed" },
      options,
    );
  }

  pull(): Promise<boolean> {
    return this.runOperation("pull", () => ({ operation: "pull" }), {
      successTitle: "Pulled latest changes",
      errorTitle: "Pull failed",
    });
  }

  sync(): Promise<boolean> {
    return this.runOperation("sync", () => ({ operation: "sync" }), {
      successTitle: "Synced with remote",
      errorTitle: "Sync failed",
    });
  }

  checkoutBranch(name: string): Promise<boolean> {
    return this.runOperation(
      "checkout_branch",
      () => ({ operation: "checkout_branch", branch: name }),
      { successTitle: `Checked out ${name}`, errorTitle: "Checkout failed" },
    );
  }

  checkoutCommit(hash: string): Promise<boolean> {
    return this.runOperation(
      "checkout_commit",
      () => ({ operation: "checkout_commit", commit: hash }),
      {
        successTitle: "Detached HEAD",
        successMessage: `Checked out commit ${hash.slice(0, 8)}`,
        errorTitle: "Checkout failed",
      },
    );
  }

  checkoutRemoteBranch(remoteRef: string): Promise<boolean> {
    return this.runOperation(
      "checkout_remote_branch",
      () => ({ operation: "checkout_remote_branch", remote_ref: remoteRef }),
      {
        successTitle: `Checked out ${remoteBranchLocalName(remoteRef)}`,
        errorTitle: "Checkout failed",
      },
    );
  }

  createBranch(
    name: string,
    fromHash: string,
    checkout: boolean,
  ): Promise<boolean> {
    const trimmed = name.trim();
    if (!trimmed) return Promise.resolve(false);
    return this.runOperation(
      "create_branch",
      () => ({
        operation: "create_branch",
        branch: trimmed,
        ...(fromHash ? { start_point: fromHash } : {}),
        checkout,
      }),
      {
        successTitle: checkout
          ? `Checked out ${trimmed}`
          : `Created branch ${trimmed}`,
        errorTitle: "Create branch failed",
      },
    );
  }

  renameBranch(oldName: string, newName: string): Promise<boolean> {
    const trimmed = newName.trim();
    if (!trimmed) return Promise.resolve(false);
    if (trimmed === oldName) return Promise.resolve(true);
    return this.runOperation(
      "rename_branch",
      () => ({
        operation: "rename_branch",
        new_branch: trimmed,
        ...(oldName ? { old_branch: oldName } : {}),
      }),
      {
        successTitle: `Renamed branch to ${trimmed}`,
        errorTitle: "Rename branch failed",
      },
    );
  }

  deleteBranch(name: string): Promise<boolean> {
    if (name === this.repo?.currentBranch) return Promise.resolve(false);
    return this.runOperation(
      "delete_branch",
      () => ({ operation: "delete_branch", branch: name }),
      {
        successTitle: `Deleted branch ${name}`,
        errorTitle: "Delete branch failed",
      },
    );
  }

  mergeIntoCurrent(branch: string): Promise<boolean> {
    return this.runOperation(
      "merge_into_current",
      (repo) => ({
        operation: "merge_into_current",
        branch,
        ...(repo.currentBranch
          ? { message: `Merge branch '${branch}' into ${repo.currentBranch}` }
          : {}),
      }),
      { successTitle: "Merged successfully", errorTitle: "Merge failed" },
    );
  }

  mergeCurrentInto(target: string): Promise<boolean> {
    return this.runOperation(
      "merge_current_into",
      (repo) => ({
        operation: "merge_current_into",
        target,
        ...(repo.currentBranch
          ? { message: `Merge branch '${repo.currentBranch}' into ${target}` }
          : {}),
      }),
      { successTitle: "Merged successfully", errorTitle: "Merge failed" },
    );
  }

  mergeAbort(): Promise<boolean> {
    return this.runOperation(
      "merge_abort",
      () => ({ operation: "merge_abort" }),
      {
        successTitle: "Merge aborted",
        errorTitle: "Abort merge failed",
      },
    );
  }

  private stashOperation(
    operation: "stash_apply" | "stash_pop" | "stash_drop",
    selector: string,
    done: string,
    failed: string,
  ): Promise<boolean> {
    const stash = selector.trim();
    if (!isValidStashSelector(stash)) return Promise.resolve(false);
    return this.runOperation(operation, () => ({ operation, stash }), {
      successTitle: `${done} ${stash}`,
      errorTitle: failed,
    });
  }

  applyStash(selector: string): Promise<boolean> {
    return this.stashOperation(
      "stash_apply",
      selector,
      "Applied",
      "Apply stash failed",
    );
  }

  popStash(selector: string): Promise<boolean> {
    return this.stashOperation(
      "stash_pop",
      selector,
      "Popped",
      "Pop stash failed",
    );
  }

  dropStash(selector: string): Promise<boolean> {
    return this.stashOperation(
      "stash_drop",
      selector,
      "Dropped",
      "Drop stash failed",
    );
  }

  createBranchFromStash(selector: string, branch: string): Promise<boolean> {
    const stash = selector.trim();
    const name = branch.trim();
    if (!isValidStashSelector(stash) || !name) return Promise.resolve(false);
    return this.runOperation(
      "create_branch_from_stash",
      () => ({ operation: "stash_branch", stash, branch: name }),
      {
        successTitle: `Created branch ${name} from ${stash}`,
        errorTitle: "Create branch from stash failed",
      },
    );
  }

  // -- diffs --------------------------------------------------------------------

  /** Both sides of a working-tree change (staged: HEAD→index, else index→worktree). */
  async workingDiff(
    path: string,
    oldPath: string | null,
    staged: boolean,
  ): Promise<{ original: string; modified: string }> {
    const repo = this.repo;
    if (!repo) throw new Error("No repository selected");
    const git = this.client.git;
    const [original, modified] = await Promise.all([
      git.fileContent(
        this.workspaceId,
        repo.path,
        staged ? (oldPath ?? path) : path,
        staged ? "head" : "index",
      ),
      git.fileContent(
        this.workspaceId,
        repo.path,
        path,
        staged ? "index" : "worktree",
      ),
    ]);
    return { original: original.content, modified: modified.content };
  }

  /** Both sides of one file in a commit (parent → commit). */
  async commitDiff(
    hash: string,
    path: string,
    oldPath: string,
  ): Promise<{ original: string; modified: string }> {
    const repo = this.repo;
    if (!repo) throw new Error("No repository selected");
    const git = this.client.git;
    const [original, modified] = await Promise.all([
      git.fileContent(this.workspaceId, repo.path, oldPath, `${hash}^`),
      git.fileContent(this.workspaceId, repo.path, path, hash),
    ]);
    return { original: original.content, modified: modified.content };
  }
}
