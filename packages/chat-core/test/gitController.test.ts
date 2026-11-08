import { afterEach, describe, expect, it, vi } from "vitest";
import {
  GitController,
  selectedGitRepo,
  type GitSelectionStore,
} from "../src/gitController";
import type {
  RawGitCommit,
  RawGitRepoSnapshot,
} from "../src/gitTypes";

function snapshot(patch: Partial<RawGitRepoSnapshot> = {}): RawGitRepoSnapshot {
  return {
    id: "/workspace/app",
    name: "app",
    path: "/workspace/app",
    current_branch: "main",
    head_hash: "aaa",
    branches: [
      { name: "main", tip_hash: "aaa", upstream: "origin/main", ahead: 1, behind: 0 },
    ],
    remote_refs: [{ name: "origin/main", tip_hash: "000" }],
    remotes: ["origin"],
    default_remote: null,
    upstream: "origin/main",
    ahead: 1,
    behind: 0,
    merge_state: { merging: false, rebasing: false, cherry_picking: false },
    commits: [],
    stashes: [],
    has_more: false,
    history_skip: 0,
    history_limit: 50,
    changes: [
      { path: "a.ts", old_path: null, status: "M", staged: true, staged_kind: "M", unstaged: null, conflict: null },
    ],
    ...patch,
  };
}

function commit(hash: string): RawGitCommit {
  return {
    hash,
    message: hash,
    body: "",
    author: "Ada",
    author_email: "ada@example.com",
    timestamp: "2026-10-31T10:00:00Z",
    author_date: "2026-10-31T10:00:00Z",
    committer: "Ada",
    committer_email: "ada@example.com",
    committer_date: "2026-10-31T10:00:00Z",
    parents: [],
  };
}

function summaries(...names: string[]) {
  return {
    ok: true,
    repos: names.map((name) => ({
      id: `/workspace/${name}`,
      name,
      path: `/workspace/${name}`,
      current_branch: "main",
      head_hash: "aaa",
    })),
  };
}

function apiError(status: number, message: string, code: string, data?: unknown) {
  return Object.assign(new Error(message), { status, code, data });
}

function memorySelection(): GitSelectionStore & { saved: string | null } {
  return {
    saved: null,
    async load() {
      return this.saved;
    },
    async save(path) {
      this.saved = path;
    },
  };
}

function setup(
  repo: RawGitRepoSnapshot = snapshot(),
  selection: GitSelectionStore = memorySelection(),
) {
  const git = {
    repos: vi.fn(async () => summaries("app")),
    repo: vi.fn(async (_workspace: string, path: string) => ({
      ok: true,
      snapshot: { ...repo, id: path, path },
    })),
    history: vi.fn(),
    commitDetails: vi.fn(),
    operation: vi.fn(async () => ({ ok: true, snapshot: repo })),
    commitMessage: vi.fn(async () => ({ message: " Subject " })),
    fileContent: vi.fn(),
  };
  const notify = vi.fn();
  const controller = new GitController(git, "ws", { notify, selection });
  return { git, notify, controller, selection };
}

function lastPayload(git: ReturnType<typeof setup>["git"]) {
  return git.operation.mock.calls.at(-1)?.[1];
}

afterEach(() => {
  vi.useRealTimers();
});

describe("GitController loading", () => {
  it("loads summaries, then the selected repository's details", async () => {
    const { controller, git } = setup();
    await controller.refresh();
    const repo = selectedGitRepo(controller.getSnapshot());
    expect(git.repo).toHaveBeenCalledWith("ws", "/workspace/app");
    expect(repo?.branches[0]).toMatchObject({ name: "main", ahead: 1 });
    expect(repo?.changes).toHaveLength(1);
  });

  it("finishes loading when the workspace has no repositories", async () => {
    const { controller, git } = setup();
    git.repos.mockResolvedValue({ ok: true, repos: [] });
    expect(controller.getSnapshot().loaded).toBe(false);
    await controller.refresh();
    expect(controller.getSnapshot()).toMatchObject({
      repos: [],
      loading: false,
      loaded: true,
      error: null,
    });
    expect(git.repo).not.toHaveBeenCalled();
  });

  it("reports load errors only while no repository is known", async () => {
    const { controller, git } = setup();
    git.repos.mockRejectedValueOnce(new Error("boom"));
    await controller.refresh();
    expect(controller.getSnapshot()).toMatchObject({ error: "boom", loading: false });
    await controller.refresh();
    git.repos.mockRejectedValueOnce(new Error("poll failed"));
    await controller.refresh({ silent: true });
    expect(controller.getSnapshot().error).toBeNull();
  });

  it("still loads details when activation joins a silent summaries poll", async () => {
    const { controller, git } = setup();
    const poll = controller.refresh({ silent: true, withDetails: false });
    const joined = controller.refresh();
    await Promise.all([poll, joined]);
    expect(git.repos).toHaveBeenCalledTimes(1);
    expect(git.repo).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().repoDetails["/workspace/app"]).toBeTruthy();
  });

  it("drops results that arrive after dispose", async () => {
    const { controller, git } = setup();
    let resolve!: (value: ReturnType<typeof summaries>) => void;
    git.repos.mockReturnValueOnce(new Promise((r) => (resolve = r)));
    const pending = controller.refresh();
    controller.dispose();
    resolve(summaries("app"));
    await expect(pending).resolves.toBe(false);
    expect(controller.getSnapshot().repos).toEqual([]);
  });
});

describe("GitController selection", () => {
  it("restores the saved repository path, even when IDs change", async () => {
    const selection = memorySelection();
    const first = setup(snapshot(), selection);
    first.git.repos.mockResolvedValue(summaries("app", "other"));
    await first.controller.refresh();
    first.controller.selectRepo("/workspace/other");
    first.controller.dispose();
    expect(selection.saved).toBe("/workspace/other");

    const next = setup(snapshot(), selection);
    const renamed = summaries("app", "other");
    renamed.repos = renamed.repos.map((repo) => ({ ...repo, id: `new:${repo.path}` }));
    next.git.repos.mockResolvedValue(renamed);
    await next.controller.refresh();
    expect(next.controller.getSnapshot().selectedRepoId).toBe("new:/workspace/other");
    expect(next.git.repo).toHaveBeenCalledWith("ws", "/workspace/other");
    expect(next.git.repo).not.toHaveBeenCalledWith("ws", "/workspace/app");
  });

  it("falls back to the first repository and keeps the preference meanwhile", async () => {
    const selection = memorySelection();
    selection.saved = "/workspace/other";
    const { controller, git } = setup(snapshot(), selection);
    git.repos.mockResolvedValueOnce(summaries("app"));
    await controller.refresh();
    expect(controller.repo?.path).toBe("/workspace/app");
    expect(selection.saved).toBe("/workspace/app");
  });

  it("keeps Git usable when preference storage fails", async () => {
    const { controller, git } = setup(snapshot(), {
      load: () => Promise.reject(new Error("Unavailable")),
      save: () => Promise.reject(new Error("Unavailable")),
    });
    git.repos.mockResolvedValue(summaries("app", "other"));
    await expect(controller.refresh()).resolves.toBe(true);
    controller.selectRepo("/workspace/other");
    await expect(controller.refresh()).resolves.toBe(true);
    expect(controller.repo?.path).toBe("/workspace/other");
    expect(controller.getSnapshot().error).toBeNull();
  });

  it("closes commit details and fetches after switching repositories", async () => {
    const { controller, git } = setup();
    git.repos.mockResolvedValue(summaries("app", "other"));
    await controller.refresh();
    await controller.openCommitDetails("aaa");
    controller.selectRepo("/workspace/other");
    expect(controller.getSnapshot().expandedCommitHash).toBeNull();
    await vi.waitFor(() =>
      expect(lastPayload(git)).toEqual({ operation: "fetch", repo_path: "/workspace/other" }),
    );
  });
});

describe("GitController mutations", () => {
  it("commits staged changes and pushes only after the commit succeeded", async () => {
    const { controller, git, notify } = setup();
    await controller.refresh();
    await expect(controller.commit("  Ship it  ", true)).resolves.toBe(true);
    expect(git.operation).toHaveBeenNthCalledWith(1, "ws", {
      operation: "commit",
      message: "Ship it",
      repo_path: "/workspace/app",
    });
    expect(git.operation).toHaveBeenNthCalledWith(2, "ws", {
      operation: "push",
      remote: "origin",
      repo_path: "/workspace/app",
    });
    expect(notify).toHaveBeenLastCalledWith(
      expect.objectContaining({ tone: "success", title: "Pushed to origin/main" }),
    );

    git.operation.mockClear();
    git.operation.mockRejectedValueOnce(apiError(400, "Nothing to commit", "error"));
    await expect(controller.commit("Again", true)).resolves.toBe(false);
    expect(git.operation).toHaveBeenCalledTimes(1);
    await expect(controller.commit("   ")).resolves.toBe(false);
    expect(git.operation).toHaveBeenCalledTimes(1);
  });

  it.each([
    [["origin"], "origin"],
    [["upstream"], "upstream"],
    [["upstream", "origin"], "origin"],
  ])("publishes a branch without upstream via %j to %s", async (remotes, remote) => {
    const { controller, git, notify } = setup(
      snapshot({
        branches: [{ name: "feat", tip_hash: "aaa", upstream: null, ahead: 0, behind: 0 }],
        current_branch: "feat",
        remotes,
      }),
    );
    await controller.refresh();
    await expect(controller.push()).resolves.toBe(true);
    expect(git.operation).toHaveBeenCalledWith("ws", {
      operation: "push",
      remote,
      set_upstream: true,
      repo_path: "/workspace/app",
    });
    expect(notify).toHaveBeenLastCalledWith(
      expect.objectContaining({ title: `Published feat to ${remote}` }),
    );
  });

  it("does not push a detached HEAD or an up-to-date branch", async () => {
    const detached = setup(snapshot({ current_branch: null }));
    await detached.controller.refresh();
    await expect(detached.controller.push()).resolves.toBe(false);
    const clean = setup(
      snapshot({
        branches: [{ name: "main", tip_hash: "aaa", upstream: "origin/main", ahead: 0, behind: 0 }],
      }),
    );
    await clean.controller.refresh();
    await expect(clean.controller.push()).resolves.toBe(true);
    expect(clean.notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "info", title: "Everything up to date" }),
    );
    expect(detached.git.operation).not.toHaveBeenCalled();
    expect(clean.git.operation).not.toHaveBeenCalled();
  });

  it("applies the snapshot of a 409 merge conflict, warns and flags it", async () => {
    const { controller, git, notify } = setup();
    await controller.refresh();
    git.operation.mockRejectedValueOnce(
      apiError(409, "Merge conflict in a.ts", "conflict", {
        snapshot: snapshot({ merge_state: { merging: true, rebasing: false, cherry_picking: false } }),
      }),
    );
    await expect(controller.mergeIntoCurrent("feat")).resolves.toBe(false);
    const state = controller.getSnapshot();
    expect(selectedGitRepo(state)?.mergeState.merging).toBe(true);
    expect(state).toMatchObject({ lastConflict: true, busyOperation: null });
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "warning", title: "Merge conflict" }),
    );

    await expect(controller.fetchRemote()).resolves.toBe(true);
    expect(controller.getSnapshot().lastConflict).toBe(false);
  });

  it.each([
    apiError(409, "Runner is offline", "runner_offline", { code: "runner_offline" }),
    apiError(409, "Conflict", "conflict", { code: "conflict" }),
    apiError(409, "Conflict", "conflict", { snapshot: { repos: [] } }),
    apiError(400, "rejected", "error"),
  ])("reports %s as a plain failure without applying a snapshot", async (error) => {
    const { controller, git, notify } = setup();
    await controller.refresh();
    git.operation.mockRejectedValueOnce(error);
    await expect(controller.mergeIntoCurrent("feat")).resolves.toBe(false);
    expect(controller.getSnapshot().lastConflict).toBe(false);
    expect(controller.repo?.mergeState.merging).toBe(false);
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "error", title: "Merge failed", message: error.message }),
    );
  });

  it("serializes concurrent operations in call order", async () => {
    const { controller, git } = setup();
    await controller.refresh();
    const order: string[] = [];
    git.operation.mockImplementation(async (_ws: string, payload: { operation: string }) => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      order.push(payload.operation);
      return { ok: true, snapshot: snapshot() };
    });
    await Promise.all([controller.stage("a.ts"), controller.unstage("b.ts"), controller.pull()]);
    expect(order).toEqual(["stage", "unstage", "pull"]);
    expect(controller.getSnapshot().busyOperation).toBeNull();
  });

  it("batches bulk path operations and stops at the first failed batch", async () => {
    const changes = Array.from({ length: 300 }, (_, i) => ({
      path: `f${i}.ts`, old_path: null, status: "M" as const, staged: false,
      staged_kind: null, unstaged: "M" as const, conflict: null,
    }));
    const { controller, git } = setup(snapshot({ changes }));
    await controller.refresh();
    await expect(controller.stageAll()).resolves.toBe(true);
    expect(git.operation.mock.calls.map((call) => call[1].paths?.length)).toEqual([256, 44]);

    git.operation.mockClear();
    git.operation.mockRejectedValueOnce(apiError(400, "boom", "git_failed"));
    await expect(controller.discardAll(changes.map((c) => c.path))).resolves.toBe(false);
    expect(git.operation).toHaveBeenCalledTimes(1);
  });

  it("sends typed branch, remote and stash payloads", async () => {
    const { controller, git } = setup();
    await controller.refresh();
    const cases: [() => Promise<boolean>, object][] = [
      [() => controller.checkoutRemoteBranch("origin/feat/x"), { operation: "checkout_remote_branch", remote_ref: "origin/feat/x" }],
      [() => controller.checkoutRemoteBranch("origin/feat/x", " local "), { operation: "checkout_remote_branch", remote_ref: "origin/feat/x", local_name: "local" }],
      [() => controller.createBranch(" feat ", "aaa", true), { operation: "create_branch", branch: "feat", start_point: "aaa", checkout: true }],
      [() => controller.renameBranch("main", "trunk"), { operation: "rename_branch", new_branch: "trunk", old_branch: "main" }],
      [() => controller.mergeCurrentInto("release"), { operation: "merge_current_into", target: "release", message: "Merge branch 'main' into release" }],
      [() => controller.createBranchFromStash("stash@{0}", "wip"), { operation: "stash_branch", stash: "stash@{0}", branch: "wip" }],
      [() => controller.dropStash(" stash@{1} "), { operation: "stash_drop", stash: "stash@{1}" }],
    ];
    for (const [run, payload] of cases) {
      await expect(run()).resolves.toBe(true);
      expect(lastPayload(git)).toEqual({ ...payload, repo_path: "/workspace/app" });
    }

    git.operation.mockClear();
    await expect(controller.applyStash("HEAD")).resolves.toBe(false);
    await expect(controller.popStash("stash@{0}; evil")).resolves.toBe(false);
    await expect(controller.createBranchFromStash("stash@{0}", "  ")).resolves.toBe(false);
    await expect(controller.deleteBranch("main")).resolves.toBe(false);
    expect(git.operation).not.toHaveBeenCalled();
  });

  it("skips fetching silently without remotes, while busy and on failure", async () => {
    const bare = setup(snapshot({ remotes: [] }));
    await bare.controller.refresh();
    await expect(bare.controller.fetchRemote()).resolves.toBe(false);
    expect(bare.git.operation).not.toHaveBeenCalled();

    const { controller, git, notify } = setup();
    await controller.refresh();
    let finish!: () => void;
    git.operation.mockImplementationOnce(
      () => new Promise((resolve) => (finish = () => resolve({ ok: true, snapshot: snapshot() }))),
    );
    const staging = controller.stage("a.ts");
    await vi.waitFor(() => expect(controller.getSnapshot().busyOperation).toBe("stage"));
    await expect(controller.fetchRemote()).resolves.toBe(false);
    finish();
    await staging;
    expect(git.operation).toHaveBeenCalledTimes(1);

    git.operation.mockRejectedValueOnce(apiError(500, "fetch failed", "error"));
    await expect(controller.fetchRemote({ silent: true })).resolves.toBe(false);
    expect(notify).not.toHaveBeenCalled();
  });

  it("reloads the open commit's details after an operation", async () => {
    const { controller, git } = setup();
    git.commitDetails.mockResolvedValue({
      ok: true,
      details: {
        ...commit("aaa"),
        file_changes: [{ old_path: "a.ts", new_path: "a.ts", status: "M", additions: 1, deletions: 0 }],
      },
    });
    await controller.refresh();
    await controller.toggleCommitDetails("aaa");
    const key = "/workspace/app::aaa";
    expect(controller.getSnapshot().commitDetails[key]?.fileChanges).toHaveLength(1);

    await controller.checkoutCommit("aaa");
    await vi.waitFor(() =>
      expect(controller.getSnapshot().commitDetails[key]?.fileChanges).toHaveLength(1),
    );
    expect(git.commitDetails).toHaveBeenCalledTimes(2);

    git.commitDetails.mockRejectedValueOnce(new Error("unknown commit"));
    await controller.toggleCommitDetails("bbb");
    expect(controller.getSnapshot().commitDetailsError["/workspace/app::bbb"]).toBe("unknown commit");
  });

  it("trims a generated commit subject", async () => {
    const { controller } = setup();
    await controller.refresh();
    await expect(controller.generateCommitMessage()).resolves.toBe("Subject");
    expect(controller.getSnapshot().generatingCommitMessage).toBe(false);
  });
});

describe("GitController history and polling", () => {
  it("appends deduplicated history pages with the branch filter", async () => {
    const { controller, git } = setup(
      snapshot({ has_more: true, commits: [commit("c3"), commit("c2")] }),
    );
    git.history.mockResolvedValueOnce({
      ok: true,
      repo_path: "/workspace/app",
      commits: [commit("c2"), commit("c1")],
      has_more: false,
      history_skip: 2,
      history_limit: 50,
    });
    await controller.refresh();
    await controller.loadMoreHistory("feat");
    expect(git.history).toHaveBeenCalledWith("ws", "/workspace/app", { limit: 50, skip: 2, branch: "feat" });
    expect(controller.repo?.commits.map((c) => c.hash)).toEqual(["c3", "c2", "c1"]);
    expect(controller.repo?.hasMore).toBe(false);
  });

  it("polls only while visible, skipping detail reloads while busy", async () => {
    vi.useFakeTimers();
    const { controller, git } = setup();
    controller.setVisible(true);
    await vi.waitFor(() => expect(git.operation).toHaveBeenCalledWith("ws", {
      operation: "fetch",
      repo_path: "/workspace/app",
    }));
    git.repos.mockClear();
    git.repo.mockClear();

    await vi.advanceTimersByTimeAsync(4000);
    expect(git.repos).toHaveBeenCalledTimes(1);
    expect(git.repo).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(11_000);
    expect(git.repo).toHaveBeenCalledTimes(1);

    let finish!: () => void;
    git.operation.mockImplementationOnce(
      () => new Promise((resolve) => (finish = () => resolve({ ok: true, snapshot: snapshot() }))),
    );
    const staging = controller.stage("a.ts");
    git.repos.mockClear();
    git.repo.mockClear();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(git.repos).not.toHaveBeenCalled();
    expect(git.repo).not.toHaveBeenCalled();
    finish();
    await staging;

    controller.setVisible(false);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(git.repos).not.toHaveBeenCalled();
  });
});
