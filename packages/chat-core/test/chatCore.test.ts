import { describe, expect, it } from "vitest";

import * as publicApi from "@opencuria/chat-core";
import * as reducerApi from "@opencuria/chat-core/harnessReducer";
import {
  createChunkedTransferStore,
  splitBase64Chunks,
} from "@opencuria/chat-core/fileChunks";
import {
  detectMentionQuery,
  filterMentionCandidates,
} from "@opencuria/chat-core/harnessMentions";
import {
  classifyWorkspaceFile,
  resolveWorkspaceLinkPath,
} from "@opencuria/chat-core/workspaceFileRefs";
import type { HarnessMessage } from "@opencuria/chat-core/harness";

describe("public package exports", () => {
  it("preserves source export metadata for package consumers", async () => {
    const { readFile } = await import("node:fs/promises");
    const manifestUrl = new URL("../package.json", import.meta.url);
    const manifest = JSON.parse(await readFile(manifestUrl, "utf8")) as {
      name: string;
      version: string;
      private: boolean;
      license: string;
      type: string;
      exports: Record<string, string>;
    };

    expect(manifest).toMatchObject({
      name: "@opencuria/chat-core",
      version: "0.0.0",
      private: true,
      license: "AGPL-3.0-only",
      type: "module",
      exports: { ".": "./src/index.ts", "./*": "./src/*.ts" },
    });
  });

  it("exposes framework-neutral chat APIs and direct source subpaths", () => {
    expect(publicApi.applyPartDelta).toBe(reducerApi.applyPartDelta);
    expect(publicApi.parseComposerSegments).toBeTypeOf("function");
    expect(publicApi.isWorkspaceReadyForChat).toBeTypeOf("function");
    expect(
      publicApi.isWorkspaceReadyForChat({
        status: "running",
        runner_online: true,
      }),
    ).toBe(true);
    expect(
      publicApi.isWorkspaceReadyForChat({
        status: "stopped",
        runner_online: true,
      }),
    ).toBe(false);
    expect(
      publicApi.isWorkspaceReadyForChat({
        status: "running",
        runner_online: true,
        active_operation: "capturing_image",
      }),
    ).toBe(false);
    expect(
      publicApi.isWorkspaceReadyForChat({
        status: "running",
        runner_online: true,
        intervention_required: true,
      }),
    ).toBe(false);
    expect(
      publicApi.isWorkspaceReadyForChat(
        { status: "running", runner_online: true },
        true,
      ),
    ).toBe(false);
    expect(publicApi.toolDisplayLabel).toBeTypeOf("function");
  });
});

describe("harness reducer", () => {
  it("applies a streaming delta in place without replacing the message snapshot", () => {
    const messages: HarnessMessage[] = [
      {
        id: "user-1",
        session_id: "session-1",
        role: "user",
        content: "hello",
        parts: [],
      },
    ];
    const initialMessages = messages;

    const assistant = reducerApi.applyPartDelta(
      messages,
      "session-1",
      { text: "Hello" },
      {
        messageId: "assistant-1",
        partId: "text-1",
      },
    );

    expect(messages).toBe(initialMessages);
    expect(assistant).toMatchObject({ id: "assistant-1", content: "Hello" });
    expect(assistant.parts).toMatchObject([
      { id: "text-1", type: "text", output: "Hello" },
    ]);
  });
});

describe("mentions", () => {
  it("detects agent queries and ranks matching file candidates", () => {
    expect(detectMentionQuery("look @agent:pl", 14)).toBe("agent:pl");
    expect(
      filterMentionCandidates("file:util", [
        "/workspace/src/utilities.ts",
        "/workspace/src/util.ts",
      ]).map((candidate) => candidate.label),
    ).toEqual(["src/util.ts", "src/utilities.ts"]);
  });
});

describe("chunking", () => {
  it("splits base64 in aligned chunks and reassembles path-pinned transfers", () => {
    const chunks = splitBase64Chunks("QUJDREVGR0g=", 8);
    expect(chunks).toEqual(["QUJDREVG", "R0g="]);

    const store = createChunkedTransferStore({ maxChars: 32 });
    store.start("request-1", 2, { size: 8 }, "/workspace/file.txt");
    store.addChunk("request-1", {
      workspace_id: "workspace-1",
      request_id: "request-1",
      path: "/workspace/file.txt",
      index: 1,
      total_chunks: 2,
      content: chunks[1]!,
    });
    store.addChunk("request-1", {
      workspace_id: "workspace-1",
      request_id: "request-1",
      path: "/workspace/file.txt",
      index: 0,
      total_chunks: 2,
      content: chunks[0]!,
    });

    expect(store.finish("request-1")).toEqual({
      content: "QUJDREVGR0g=",
      meta: { size: 8 },
      path: "/workspace/file.txt",
    });
  });
});

describe("workspace paths", () => {
  it("classifies local assets and rejects paths escaping the workspace", () => {
    expect(classifyWorkspaceFile("/workspace/photo.PNG")).toBe("image");
    expect(resolveWorkspaceLinkPath("docs/plan%20one.md")).toBe(
      "/workspace/docs/plan one.md",
    );
    expect(resolveWorkspaceLinkPath("../secrets.txt")).toBeNull();
  });

  it("supports workspace link parsing on Hermes without Unicode property escapes", () => {
    expect(resolveWorkspaceLinkPath("docs/plan.pdf")).toBe(
      "/workspace/docs/plan.pdf",
    );
    expect(resolveWorkspaceLinkPath("docs/bad%00name.pdf")).toBeNull();
    expect(resolveWorkspaceLinkPath("docs/bad%7fname.pdf")).toBeNull();
    expect(resolveWorkspaceLinkPath("docs/bad%0aname.pdf")).toBeNull();
    expect(resolveWorkspaceLinkPath("docs%5cplan.pdf")).toBeNull();
  });
});

describe("framework-neutral source portability", () => {
  it("keeps chat-core source free from browser globals and framework imports", async () => {
    const { readFile, readdir } = await import("node:fs/promises");
    const { join } = await import("node:path");
    const sourceRoot = new URL("../src/", import.meta.url);
    const sourceFiles = (await readdir(sourceRoot)).filter((file) =>
      file.endsWith(".ts"),
    );
    const forbiddenImports = [
      /from\s+['\"]vue(?:\/|['\"])/,
      /from\s+['\"]@lucide\//,
      /from\s+['\"]@\//,
    ];
    const forbiddenGlobals = [
      /\b(?:window|document|navigator|localStorage|sessionStorage|DragEvent|DataTransfer)\b/,
      /\b(?:File|Blob|btoa|atob)\b/,
    ];

    for (const file of sourceFiles) {
      const source = await readFile(join(sourceRoot.pathname, file), "utf8");
      const executableSource = source
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/^[ \t]*\/\/.*$/gm, "");
      // Plain string literals (e.g. an icon named "document") are no globals.
      const codeOnly = executableSource.replace(/"(?:[^"\\\n]|\\.)*"/g, '""');
      expect(
        source,
        `${file} must not use Unicode property escapes`,
      ).not.toMatch(/\\p\{/);
      for (const pattern of forbiddenImports) {
        expect(
          executableSource,
          `${file} must not include ${pattern}`,
        ).not.toMatch(pattern);
      }
      for (const pattern of forbiddenGlobals) {
        expect(codeOnly, `${file} must not include ${pattern}`).not.toMatch(
          pattern,
        );
      }
    }
  });
});
