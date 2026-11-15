import { describe, expect, it } from "vitest";
import { normalizeRepoSnapshot } from "@opencuria/chat-core/gitModel";
import type {
  RawGitChange,
  RawGitRepoSnapshot,
} from "@opencuria/chat-core/gitTypes";
import { gitActionButton } from "./gitActionButton";

function repo(patch: Partial<RawGitRepoSnapshot> = {}) {
  return normalizeRepoSnapshot({
    id: "/workspace/app",
    name: "app",
    path: "/workspace/app",
    current_branch: "main",
    head_hash: "aaa",
    branches: [
      { name: "main", tip_hash: "aaa", upstream: "origin/main", ahead: 0, behind: 0 },
    ],
    remote_refs: [{ name: "origin/main", tip_hash: "aaa" }],
    remotes: ["origin"],
    default_remote: null,
    upstream: "origin/main",
    ahead: 0,
    behind: 0,
    merge_state: { merging: false, rebasing: false, cherry_picking: false },
    commits: [],
    stashes: [],
    has_more: false,
    history_skip: 0,
    history_limit: 50,
    changes: [],
    ...patch,
  });
}

const staged: RawGitChange = {
  path: "a.ts",
  old_path: null,
  status: "M",
  staged: true,
  staged_kind: "M",
  unstaged: null,
  conflict: null,
};
const modified: RawGitChange = {
  ...staged,
  path: "b.ts",
  staged: false,
  staged_kind: null,
  unstaged: "M",
};

describe("gitActionButton", () => {
  it("commits while there are changes, staging all when nothing is staged", () => {
    expect(gitActionButton(repo({ changes: [staged, modified] }), null)).toMatchObject({
      kind: "commit",
      title: "Commit",
      symbol: "arrow.up",
      enabled: true,
      stagesAll: false,
    });
    expect(gitActionButton(repo({ changes: [modified] }), null)).toMatchObject({
      kind: "commit",
      enabled: true,
      stagesAll: true,
    });
  });

  it("publishes a branch without upstream, then syncs ahead/behind counts", () => {
    const local = [{ name: "main", tip_hash: "aaa", upstream: null, ahead: 0, behind: 0 }];
    expect(gitActionButton(repo({ branches: local }), null)).toMatchObject({
      kind: "publish",
      title: "Publish Branch",
      enabled: true,
    });
    expect(gitActionButton(repo({ branches: local, remotes: [] }), null)).toMatchObject({
      kind: "commit",
      enabled: false,
    });
    const diverged = [
      { name: "main", tip_hash: "aaa", upstream: "origin/main", ahead: 2, behind: 1 },
    ];
    expect(gitActionButton(repo({ branches: diverged }), null)).toMatchObject({
      kind: "sync",
      title: "Sync Changes 1↓ 2↑",
      symbol: "arrow.up.arrow.down",
      enabled: true,
    });
  });

  it("falls back to a disabled Commit when everything is up to date or detached", () => {
    expect(gitActionButton(repo(), null)).toMatchObject({
      kind: "commit",
      enabled: false,
    });
    expect(gitActionButton(repo({ current_branch: null }), null)).toMatchObject({
      kind: "commit",
      enabled: false,
    });
  });

  it("continues a merge only once no conflict is left", () => {
    const merging = { merging: true, rebasing: false, cherry_picking: false };
    const conflict = { ...modified, status: "U" as const, conflict: "UU" };
    expect(
      gitActionButton(repo({ merge_state: merging, changes: [conflict] }), null),
    ).toMatchObject({ title: "Continue", enabled: false });
    expect(gitActionButton(repo({ merge_state: merging }), null)).toMatchObject({
      title: "Continue",
      enabled: true,
    });
  });

  it("is disabled while an operation runs and marks its own operation", () => {
    const changed = repo({ changes: [staged] });
    expect(gitActionButton(changed, "commit")).toMatchObject({
      enabled: false,
      running: true,
    });
    expect(gitActionButton(changed, "fetch")).toMatchObject({
      enabled: false,
      running: false,
    });
  });
});
