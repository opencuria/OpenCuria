import React from "react";
import { Alert } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import type {
  RawGitChange,
  RawGitRepoSnapshot,
} from "@opencuria/chat-core/gitTypes";
import { ThemeProvider } from "../../ui/theme";
import { ToastHost } from "../../ui/ToastHost";
import { dismissToast } from "../../ui/toast";
import { GitPanel } from "./GitPanel";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-svg", () => {
  const React = require("react");
  const { View } = require("react-native");
  const Shape = (props: Record<string, unknown>) =>
    React.createElement(View, props);
  return { __esModule: true, default: Shape, Circle: Shape, Path: Shape };
});
jest.mock("../files/WorkspaceFileIcon", () => ({ WorkspaceFileIcon: () => null }));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

const staged: RawGitChange = {
  path: "src/a.ts",
  old_path: null,
  status: "M",
  staged: true,
  staged_kind: "M",
  unstaged: null,
  conflict: null,
};
const modified: RawGitChange = {
  ...staged,
  path: "src/b.ts",
  staged: false,
  staged_kind: null,
  unstaged: "M",
};

function snapshot(patch: Partial<RawGitRepoSnapshot> = {}): RawGitRepoSnapshot {
  return {
    id: "/workspace/app",
    name: "app",
    path: "/workspace/app",
    current_branch: "main",
    head_hash: "aaaaaaaa11",
    branches: [
      { name: "main", tip_hash: "aaaaaaaa11", upstream: "origin/main", ahead: 2, behind: 0 },
    ],
    remote_refs: [{ name: "origin/main", tip_hash: "000" }],
    remotes: ["origin"],
    default_remote: null,
    upstream: "origin/main",
    ahead: 2,
    behind: 0,
    merge_state: { merging: false, rebasing: false, cherry_picking: false },
    commits: [
      {
        hash: "aaaaaaaa11",
        message: "Add the commit sheet",
        body: "",
        author: "Ada",
        author_email: "ada@example.com",
        timestamp: "2026-10-31T10:00:00Z",
        author_date: "2026-10-31T10:00:00Z",
        committer: "Ada",
        committer_email: "ada@example.com",
        committer_date: "2026-10-31T10:00:00Z",
        parents: [],
      },
    ],
    stashes: [],
    has_more: false,
    history_skip: 0,
    history_limit: 50,
    changes: [staged],
    ...patch,
  };
}

function fakeClient(repo: RawGitRepoSnapshot = snapshot()) {
  const git = {
    repos: jest.fn(async () => ({
      ok: true,
      repos: [
        {
          id: repo.id,
          name: repo.name,
          path: repo.path,
          current_branch: repo.current_branch,
          head_hash: repo.head_hash,
        },
      ],
    })),
    repo: jest.fn(async () => ({ ok: true, snapshot: repo })),
    history: jest.fn(),
    commitDetails: jest.fn(async () => ({
      ok: true,
      details: {
        hash: "aaaaaaaa11",
        message: "Add the commit sheet",
        body: "",
        author: "Ada",
        author_email: "ada@example.com",
        author_date: "2026-10-31T10:00:00Z",
        committer: "Ada",
        committer_email: "ada@example.com",
        committer_date: "2026-10-31T10:00:00Z",
        parents: [],
        file_changes: [
          { old_path: "src/a.ts", new_path: "src/a.ts", status: "M", additions: 3, deletions: 1 },
        ],
      },
    })),
    operation: jest.fn(async () => ({ ok: true, snapshot: repo })),
    commitMessage: jest.fn(async () => ({ message: "Subject" })),
    fileContent: jest.fn(async () => ({ content: "export const a = 1;\n" })),
  };
  const api = {
    getContext: () => ({ baseUrl: "https://server.test/api/v1", organizationId: "org" }),
  };
  return { client: { api, git } as unknown as OpenCuriaClient, git };
}

function renderPanel(client: OpenCuriaClient) {
  return render(
    <ThemeProvider>
      <GitPanel
        client={client}
        workspaceId="ws"
        workspaceName="demo"
        active
        media={null}
        bottomInset={0}
      />
      <ToastHost />
    </ThemeProvider>,
  );
}

afterEach(() => act(() => dismissToast()));

it("shows repository, branch and outgoing commits in one row and commits like VS Code", async () => {
  const { client, git } = fakeClient();
  renderPanel(client);
  const action = await screen.findByTestId("git-action");
  expect(screen.getByLabelText("Repository app, branch main")).toBeTruthy();
  expect(screen.getByLabelText("2 commits to push")).toBeTruthy();
  expect(screen.getByText("Commit")).toBeTruthy();
  expect(screen.queryByText("/workspace/app")).toBeNull();

  // An empty message asks for one instead of committing.
  fireEvent.press(action);
  expect(git.operation).not.toHaveBeenCalledWith(
    "ws",
    expect.objectContaining({ operation: "commit" }),
  );

  fireEvent.changeText(screen.getByTestId("git-commit-message"), "Ship it");
  fireEvent.press(action);
  await waitFor(() =>
    expect(git.operation).toHaveBeenCalledWith("ws", {
      operation: "commit",
      message: "Ship it",
      repo_path: "/workspace/app",
    }),
  );
  expect(await screen.findByLabelText("Committed on main")).toBeTruthy();
});

it("shows the empty state instead of loading forever without repositories", async () => {
  const { client, git } = fakeClient();
  git.repos.mockResolvedValue({ ok: true, repos: [] });
  renderPanel(client);
  expect(
    await screen.findByText("No git repositories found in this workspace."),
  ).toBeTruthy();
  expect(screen.queryByTestId("git-loading")).toBeNull();

  fireEvent.press(screen.getByTestId("git-retry"));
  await waitFor(() => expect(git.repos).toHaveBeenCalledTimes(2));
  expect(await screen.findByTestId("git-empty")).toBeTruthy();
});

it("offers to stage every change when nothing is staged", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
  const { client, git } = fakeClient(snapshot({ changes: [modified] }));
  renderPanel(client);
  fireEvent.changeText(await screen.findByTestId("git-commit-message"), "Both");
  fireEvent.press(screen.getByTestId("git-action"));
  const buttons = alert.mock.calls[0]?.[2] ?? [];
  const stageAll = buttons.find((button) => button.text === "Stage All & Commit");
  await act(async () => stageAll?.onPress?.());
  expect(git.operation).toHaveBeenCalledWith("ws", {
    operation: "stage",
    paths: ["src/b.ts"],
    repo_path: "/workspace/app",
  });
  alert.mockRestore();
});

it("opens the other actions on hold without committing, then runs Pull", async () => {
  const { client, git } = fakeClient();
  renderPanel(client);
  fireEvent.changeText(await screen.findByTestId("git-commit-message"), "Keep draft");
  expect(screen.queryByTestId("git-more-actions")).toBeNull();
  expect(screen.queryByRole("button", { name: "Pull" })).toBeNull();

  const operationsBeforeHold = git.operation.mock.calls.length;
  fireEvent(screen.getByTestId("git-action"), "longPress");
  expect(git.operation).toHaveBeenCalledTimes(operationsBeforeHold);
  expect(screen.getByRole("button", { name: "Commit & Push" })).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Pull" }));
  await waitFor(() => expect(git.operation).toHaveBeenCalledWith("ws", {
    operation: "pull", repo_path: "/workspace/app",
  }));
  expect(screen.getByTestId("git-commit-message")).toHaveDisplayValue("Keep draft");
  expect(screen.queryByRole("button", { name: "Pull" })).toBeNull();
});

it("keeps the menu available when the suggested commit is unavailable", async () => {
  const { client, git } = fakeClient(snapshot({
    changes: [],
    branches: [{ name: "main", tip_hash: "aaaaaaaa11", upstream: "origin/main", ahead: 0, behind: 0 }],
  }));
  renderPanel(client);
  const action = await screen.findByTestId("git-action");
  const operationsBeforeTap = git.operation.mock.calls.length;
  fireEvent.press(action);
  expect(git.operation).toHaveBeenCalledTimes(operationsBeforeTap);
  fireEvent(action, "longPress");
  fireEvent.press(screen.getByRole("button", { name: "Pull" }));
  await waitFor(() => expect(git.operation).toHaveBeenCalledWith("ws", {
    operation: "pull", repo_path: "/workspace/app",
  }));
});

it("generates a subject from the composer button and preserves it after a failed commit", async () => {
  const { client, git } = fakeClient();
  renderPanel(client);
  fireEvent.press(await screen.findByTestId("git-generate-message"));
  await waitFor(() => expect(screen.getByTestId("git-commit-message")).toHaveDisplayValue("Subject"));
  git.operation.mockRejectedValueOnce(new Error("Cannot commit"));
  fireEvent.press(screen.getByTestId("git-action"));
  expect(await screen.findByLabelText("Commit failed, Cannot commit")).toBeTruthy();
  expect(screen.getByTestId("git-commit-message")).toHaveDisplayValue("Subject");
});

it("syncs a clean branch that is ahead and behind", async () => {
  const { client, git } = fakeClient(
    snapshot({
      changes: [],
      branches: [
        { name: "main", tip_hash: "aaaaaaaa11", upstream: "origin/main", ahead: 1, behind: 2 },
      ],
    }),
  );
  renderPanel(client);
  fireEvent.press(await screen.findByText("Sync Changes 2↓ 1↑"));
  await waitFor(() =>
    expect(git.operation).toHaveBeenCalledWith("ws", {
      operation: "sync",
      repo_path: "/workspace/app",
    }),
  );
});

it("keeps a commit's files after a checkout and opens a file's diff above it", async () => {
  const { client, git } = fakeClient();
  renderPanel(client);
  fireEvent.press(await screen.findByTestId("git-graph-row"));
  expect(await screen.findByText("1 FILE CHANGED")).toBeTruthy();

  fireEvent.press(screen.getByLabelText("Actions for aaaaaaaa"));
  fireEvent.press(screen.getByLabelText("Checkout Commit"));
  await waitFor(() => expect(git.commitDetails).toHaveBeenCalledTimes(2));
  expect(screen.getByText("1 FILE CHANGED")).toBeTruthy();
  expect(screen.queryByText("0 FILES CHANGED")).toBeNull();

  fireEvent.press(screen.getByTestId("git-commit-file"));
  expect(await screen.findByTestId("git-diff-sheet")).toBeTruthy();
  await waitFor(() =>
    expect(git.fileContent).toHaveBeenCalledWith(
      "ws",
      "/workspace/app",
      "src/a.ts",
      "aaaaaaaa11",
    ),
  );
});
