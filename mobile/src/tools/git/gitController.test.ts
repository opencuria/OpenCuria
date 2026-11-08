import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OpenCuriaClient } from "@opencuria/client";
import { __values } from "../../platform/test-mocks/expo-secure-store";
import { createGitController } from "./gitController";

function fakeClient(baseUrl = "https://server.test/api/v1", organizationId = "org") {
  const git = {
    repos: vi.fn(async () => ({
      ok: true,
      repos: ["app", "other"].map((name) => ({
        id: `/workspace/${name}`,
        path: `/workspace/${name}`,
        name,
        current_branch: "main",
        head_hash: "aaa",
      })),
    })),
  };
  const api = { getContext: () => ({ baseUrl, organizationId }) };
  return { api, git } as unknown as OpenCuriaClient;
}

beforeEach(() => __values.clear());

describe("createGitController", () => {
  it.each([
    ["ws-2", "https://server.test/api/v1", "org"],
    ["ws", "https://another-server.test/api/v1", "org"],
    ["ws", "https://server.test/api/v1", "org-2"],
  ])("keeps the selection per workspace %s, server %s, organization %s", async (
    workspaceId, baseUrl, organizationId,
  ) => {
    const first = createGitController(fakeClient(), "ws", vi.fn());
    await first.refresh({ withDetails: false });
    first.selectRepo("/workspace/other");
    first.dispose();

    const other = createGitController(
      fakeClient(baseUrl, organizationId), workspaceId, vi.fn(),
    );
    await other.refresh({ withDetails: false });
    expect(other.repo?.path).toBe("/workspace/app");

    const reopened = createGitController(fakeClient(), "ws", vi.fn());
    await reopened.refresh({ withDetails: false });
    expect(reopened.repo?.path).toBe("/workspace/other");
    expect([...__values.values()]).toContain("/workspace/other");
  });
});
