import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as SecureStore from "expo-secure-store";
import { ApiRequestError, type OpenCuriaClient } from "@opencuria/client";
import type { RawGitRepoSnapshot } from "@opencuria/chat-core/gitTypes";
import { __values } from "../../platform/test-mocks/expo-secure-store";
import { GitController, selectedGitRepo } from "./gitController";

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

function fakeClient(baseUrl = "https://server.test/api/v1", organizationId = "org") {
  const git = {
    repos: vi.fn(async () => ({
      ok: true,
      repos: [{ id: "/workspace/app", name: "app", path: "/workspace/app", current_branch: "main", head_hash: "aaa" }],
    })),
    repo: vi.fn(async (_workspace: string, path: string) => ({
      ok: true,
      snapshot: snapshot({ id: path, path }),
    })),
    history: vi.fn(),
    commitDetails: vi.fn(),
    operation: vi.fn(async () => ({ ok: true, snapshot: snapshot() })),
    commitMessage: vi.fn(async () => ({ message: " Subject " })),
    fileContent: vi.fn(),
  };
  const api = { getContext: () => ({ baseUrl, organizationId }) };
  return { client: { api, git } as unknown as OpenCuriaClient, git };
}

function multiRepoClient(baseUrl?: string, organizationId?: string) {
  const result = fakeClient(baseUrl, organizationId);
  result.git.repos.mockResolvedValue({
    ok: true,
    repos: ["app", "other"].map((name) => ({
      id: `/workspace/${name}`,
      path: `/workspace/${name}`,
      name,
      current_branch: "main",
      head_hash: "aaa",
    })),
  });
  return result;
}

beforeEach(() => __values.clear());
afterEach(() => vi.restoreAllMocks());

describe("GitController", () => {
  it("loads summaries, then the selected repository's details", async () => {
    const { client, git } = fakeClient();
    const controller = new GitController(client, "ws");
    await controller.refresh();
    const repo = selectedGitRepo(controller.getSnapshot());
    expect(git.repo).toHaveBeenCalledWith("ws", "/workspace/app");
    expect(repo?.branches[0]).toMatchObject({ name: "main", ahead: 1 });
    expect(repo?.changes).toHaveLength(1);
  });

  it("still loads details when activation joins a silent summaries poll", async () => {
    const { client, git } = fakeClient();
    const controller = new GitController(client, "ws");
    const poll = controller.refresh({ silent: true, withDetails: false });
    const joined = controller.refresh();
    await Promise.all([poll, joined]);
    expect(git.repos).toHaveBeenCalledTimes(1);
    expect(git.repo).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().repoDetails["/workspace/app"]).toBeTruthy();
  });

  it("restores the saved repository with a new client and controller", async () => {
    const first = multiRepoClient();
    const controller = new GitController(first.client, "ws");
    await controller.refresh();
    controller.selectRepo("/workspace/other");
    controller.dispose();

    const reopened = multiRepoClient();
    const next = new GitController(reopened.client, "ws");
    await next.refresh();
    expect(next.repo?.path).toBe("/workspace/other");
    expect(reopened.git.repo).toHaveBeenCalledWith("ws", "/workspace/other");
    expect(reopened.git.repo).not.toHaveBeenCalledWith("ws", "/workspace/app");
    expect([...__values.values()]).toEqual(["/workspace/other"]);
  });

  it.each([
    ["ws-2", "https://server.test/api/v1", "org"],
    ["ws", "https://another-server.test/api/v1", "org"],
    ["ws", "https://server.test/api/v1", "org-2"],
  ])("isolates preferences for workspace %s, server %s, organization %s", async (
    workspaceId, baseUrl, organizationId,
  ) => {
    const { client } = multiRepoClient();
    const first = new GitController(client, "ws");
    await first.refresh();
    first.selectRepo("/workspace/other");
    first.dispose();

    const other = new GitController(
      multiRepoClient(baseUrl, organizationId).client, workspaceId,
    );
    await other.refresh();
    expect(other.repo?.path).toBe("/workspace/app");

    const reopened = new GitController(multiRepoClient().client, "ws");
    await reopened.refresh();
    expect(reopened.repo?.path).toBe("/workspace/other");
  });

  it("restores by path when repository IDs change", async () => {
    const { client, git } = multiRepoClient();
    const first = new GitController(client, "ws");
    await first.refresh({ withDetails: false });
    first.selectRepo("/workspace/other");
    first.dispose();
    const result = await git.repos();
    git.repos.mockResolvedValue({
      ...result,
      repos: result.repos.map((repo) => ({ ...repo, id: `new:${repo.path}` })),
    });
    const reopened = new GitController(client, "ws");
    await reopened.refresh({ withDetails: false });
    expect(reopened.getSnapshot().selectedRepoId).toBe("new:/workspace/other");
  });

  it("falls back when the saved repository has been removed", async () => {
    const { client, git } = multiRepoClient();
    const first = new GitController(client, "ws");
    await first.refresh();
    first.selectRepo("/workspace/other");
    first.dispose();

    git.repos.mockResolvedValue({
      ok: true,
      repos: (await git.repos()).repos.slice(0, 1),
    });
    const reopened = new GitController(client, "ws");
    await reopened.refresh();
    expect(reopened.repo?.path).toBe("/workspace/app");
  });

  it("keeps the preference through an empty repository listing", async () => {
    const { client, git } = multiRepoClient();
    const first = new GitController(client, "ws");
    await first.refresh();
    first.selectRepo("/workspace/other");
    first.dispose();

    git.repos.mockResolvedValueOnce({ ok: true, repos: [] });
    const reopened = new GitController(client, "ws");
    await reopened.refresh();
    expect(reopened.repo).toBeNull();
    await reopened.refresh();
    expect(reopened.repo?.path).toBe("/workspace/other");
  });

  it("keeps the latest selection after rapid changes", async () => {
    const { client } = multiRepoClient();
    const first = new GitController(client, "ws");
    await first.refresh();
    first.selectRepo("/workspace/other");
    first.selectRepo("/workspace/app");
    first.selectRepo("/workspace/other");
    first.dispose();
    first.selectRepo("/workspace/app");

    const reopened = new GitController(client, "ws");
    await reopened.refresh();
    expect(reopened.repo?.path).toBe("/workspace/other");
  });

  it("keeps Git usable when native preference storage fails", async () => {
    vi.spyOn(SecureStore, "getItemAsync").mockRejectedValue(new Error("Unavailable"));
    vi.spyOn(SecureStore, "setItemAsync").mockRejectedValue(new Error("Unavailable"));
    const { client } = multiRepoClient();
    const controller = new GitController(client, "ws");
    await expect(controller.refresh()).resolves.toBe(true);
    controller.selectRepo("/workspace/other");
    await expect(controller.refresh()).resolves.toBe(true);
    expect(controller.repo?.path).toBe("/workspace/other");
    expect(controller.getSnapshot().error).toBeNull();
  });

  it("commits staged changes and pushes the new commit", async () => {
    const { client, git } = fakeClient();
    const notify = vi.fn();
    const controller = new GitController(client, "ws", notify);
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
  });

  it("publishes a branch without upstream", async () => {
    const { client, git } = fakeClient();
    git.repo.mockResolvedValueOnce({
      ok: true,
      snapshot: snapshot({
        branches: [{ name: "feat", tip_hash: "aaa", upstream: null, ahead: 0, behind: 0 }],
        current_branch: "feat",
      }),
    });
    git.repos.mockResolvedValueOnce({
      ok: true,
      repos: [{ id: "/workspace/app", name: "app", path: "/workspace/app", current_branch: "feat", head_hash: "aaa" }],
    });
    const controller = new GitController(client, "ws");
    await controller.refresh();
    await controller.push();
    expect(git.operation).toHaveBeenCalledWith("ws", {
      operation: "push",
      remote: "origin",
      set_upstream: true,
      repo_path: "/workspace/app",
    });
  });

  it("applies the snapshot of a 409 merge conflict and warns", async () => {
    const { client, git } = fakeClient();
    const notify = vi.fn();
    const controller = new GitController(client, "ws", notify);
    await controller.refresh();
    git.operation.mockRejectedValueOnce(
      new ApiRequestError(409, "Merge conflict in a.ts", "conflict", {
        snapshot: snapshot({ merge_state: { merging: true, rebasing: false, cherry_picking: false } }),
      }),
    );
    await expect(controller.pull()).resolves.toBe(false);
    const state = controller.getSnapshot();
    expect(selectedGitRepo(state)?.mergeState.merging).toBe(true);
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "warning", title: "Merge conflict" }),
    );
    expect(state.busyOperation).toBeNull();
  });

  it("reports a failed operation without throwing", async () => {
    const { client, git } = fakeClient();
    const notify = vi.fn();
    const controller = new GitController(client, "ws", notify);
    await controller.refresh();
    git.operation.mockRejectedValueOnce(new ApiRequestError(400, "rejected", "error"));
    await expect(controller.stage("a.ts")).resolves.toBe(false);
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "error", title: "Stage failed", message: "rejected" }),
    );
  });

  it("reloads the open commit's details after an operation", async () => {
    const { client, git } = fakeClient();
    git.commitDetails.mockResolvedValue({
      ok: true,
      details: {
        hash: "aaa",
        message: "Initial",
        body: "",
        author: "Ada",
        author_email: "ada@example.com",
        author_date: "2026-10-31T10:00:00Z",
        committer: "Ada",
        committer_email: "ada@example.com",
        committer_date: "2026-10-31T10:00:00Z",
        parents: [],
        file_changes: [
          { old_path: "a.ts", new_path: "a.ts", status: "M", additions: 1, deletions: 0 },
        ],
      },
    });
    const controller = new GitController(client, "ws", vi.fn());
    await controller.refresh();
    await controller.openCommitDetails("aaa");
    const key = "/workspace/app::aaa";
    expect(controller.getSnapshot().commitDetails[key]?.fileChanges).toHaveLength(1);

    await controller.checkoutCommit("aaa");
    await vi.waitFor(() =>
      expect(controller.getSnapshot().commitDetails[key]?.fileChanges).toHaveLength(1),
    );
    expect(git.commitDetails).toHaveBeenCalledTimes(2);
  });

  it("trims a generated commit subject", async () => {
    const { client } = fakeClient();
    const controller = new GitController(client, "ws");
    await controller.refresh();
    await expect(controller.generateCommitMessage()).resolves.toBe("Subject");
  });
});
