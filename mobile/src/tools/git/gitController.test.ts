import { describe, expect, it, vi } from "vitest";
import { ApiRequestError, type OpenCuriaClient } from "@opencuria/client";
import type { RawGitRepoSnapshot } from "@opencuria/chat-core/gitTypes";
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

function fakeClient() {
  const git = {
    repos: vi.fn(async () => ({
      ok: true,
      repos: [{ id: "/workspace/app", name: "app", path: "/workspace/app", current_branch: "main", head_hash: "aaa" }],
    })),
    repo: vi.fn(async () => ({ ok: true, snapshot: snapshot() })),
    history: vi.fn(),
    commitDetails: vi.fn(),
    operation: vi.fn(async () => ({ ok: true, snapshot: snapshot() })),
    commitMessage: vi.fn(async () => ({ message: " Subject " })),
    fileContent: vi.fn(),
  };
  return { client: { git } as unknown as OpenCuriaClient, git };
}

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

  it("commits staged changes and pushes the new commit", async () => {
    const { client, git } = fakeClient();
    const controller = new GitController(client, "ws");
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
    expect(controller.getSnapshot().notice?.title).toBe("Pushed to origin/main");
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
    const controller = new GitController(client, "ws");
    await controller.refresh();
    git.operation.mockRejectedValueOnce(
      new ApiRequestError(409, "Merge conflict in a.ts", "conflict", {
        snapshot: snapshot({ merge_state: { merging: true, rebasing: false, cherry_picking: false } }),
      }),
    );
    await expect(controller.pull()).resolves.toBe(false);
    const state = controller.getSnapshot();
    expect(selectedGitRepo(state)?.mergeState.merging).toBe(true);
    expect(state.notice).toMatchObject({ tone: "warning", title: "Merge conflict" });
    expect(state.busyOperation).toBeNull();
  });

  it("reports a failed operation without throwing", async () => {
    const { client, git } = fakeClient();
    const controller = new GitController(client, "ws");
    await controller.refresh();
    git.operation.mockRejectedValueOnce(new ApiRequestError(400, "rejected", "error"));
    await expect(controller.stage("a.ts")).resolves.toBe(false);
    expect(controller.getSnapshot().notice).toMatchObject({ tone: "error", title: "Stage failed" });
  });

  it("trims a generated commit subject", async () => {
    const { client } = fakeClient();
    const controller = new GitController(client, "ws");
    await controller.refresh();
    await expect(controller.generateCommitMessage()).resolves.toBe("Subject");
  });
});
