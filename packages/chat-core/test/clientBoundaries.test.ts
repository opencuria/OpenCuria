import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * Rules shared by the web and native clients have exactly one
 * implementation in chat-core. Clients import them; a client function
 * named like a chat-core export is a second copy of that rule.
 *
 * Clients import chat-core from its own module paths. A client module
 * that re-exports chat-core hides the owner behind a client-local path
 * and grows a parallel namespace for the same rules.
 */
const REPO = new URL("../../../", import.meta.url);
const CORE_SOURCES = new URL("packages/chat-core/src/", REPO);
const CLIENT_SOURCES = ["webapp/src/", "mobile/src/"];

const CORE_SPEC = String.raw`["']@opencuria/chat-core(?:/[\w-]+)?["']`;
/** `export * from`, `export type * from` and `export { … } from` chat-core. */
const REEXPORT_FROM = new RegExp(
  String.raw`^[ \t]*export[ \t]+(?:type[ \t]+)?(?:\*|\{[^}]*\})[ \t]*from[ \t]*${CORE_SPEC}`,
  "gm",
);
const CORE_IMPORT = new RegExp(
  String.raw`^[ \t]*import[ \t]+(?:type[ \t]+)?\{([^}]*)\}[ \t]*from[ \t]*${CORE_SPEC}`,
  "gm",
);
const LOCAL_EXPORT_LIST =
  /^[ \t]*export[ \t]+(?:type[ \t]+)?\{([^}]*)\}(?![ \t]*from)/gm;
/** `export const alias = name` and `export type Alias = Name` (no type arguments). */
const EXPORT_ALIAS =
  /^[ \t]*export[ \t]+(?:const|type)[ \t]+\w+[ \t]*=[ \t]*(\w+)[ \t]*;?[ \t]*$/gm;

/** Binding names of an import/export list; `local` picks the `as` alias. */
function bindingNames(list: string, local: boolean): string[] {
  return list
    .split(",")
    .map((entry) => entry.trim().replace(/^type\s+/, ""))
    .filter(Boolean)
    .map((entry) => {
      const [name, alias] = entry.split(/\s+as\s+/);
      return (local ? (alias ?? name) : name)!.trim();
    });
}

/** Chat-core re-exports: `export … from` chat-core, or exports of its imports. */
function coreReexports(source: string): string[] {
  const imported = new Set(
    [...source.matchAll(CORE_IMPORT)].flatMap((match) =>
      bindingNames(match[1]!, true),
    ),
  );
  return [
    ...[...source.matchAll(REEXPORT_FROM)].map((match) =>
      match[0].trim().replace(/\s+/g, " "),
    ),
    ...[...source.matchAll(LOCAL_EXPORT_LIST)].flatMap((match) =>
      bindingNames(match[1]!, false).filter((name) => imported.has(name)),
    ),
    ...[...source.matchAll(EXPORT_ALIAS)]
      .map((match) => match[1]!)
      .filter((name) => imported.has(name)),
  ];
}

/** Top-level function and arrow-function declarations, exported or not. */
const DEFINITION =
  /^[ \t]*(?:export[ \t]+)?(?:(?:async[ \t]+)?function[ \t]+(\w+)|const[ \t]+(\w+)[ \t]*=[ \t]*(?:async[ \t]*)?\()/gm;

function definedFunctions(source: string): string[] {
  return [...source.matchAll(DEFINITION)].map((match) => match[1] ?? match[2]!);
}

function sourceFiles(dir: URL): string[] {
  return readdirSync(dir, { recursive: true, encoding: "utf8" }).filter(
    (path) =>
      /\.(ts|tsx|vue)$/.test(path) &&
      !/\.test\.tsx?$/.test(path) &&
      !path.split(/[\\/]/).includes("node_modules"),
  );
}

function sharedFunctions(): Set<string> {
  return new Set(
    sourceFiles(CORE_SOURCES).flatMap((path) =>
      [
        ...readFileSync(new URL(path, CORE_SOURCES), "utf8").matchAll(
          /^export[ \t]+(?:(?:async[ \t]+)?function[ \t]+(\w+)|const[ \t]+(\w+)[ \t]*=[ \t]*(?:async[ \t]*)?\()/gm,
        ),
      ].map((match) => match[1] ?? match[2]!),
    ),
  );
}

describe("client boundaries", () => {
  it("finds declared and arrow functions, not calls or other constants", () => {
    expect(
      definedFunctions(
        [
          "export function setMcpTransport(form) {}",
          "  async function syncRequirementService() {}",
          "const mcpLabel = (mcp, index) => index",
          "export const loadAll = async () => []",
          "const LIMIT = 5",
          "applyEdit(setMcpTransport(form, uid, 'sse'))",
        ].join("\n"),
      ),
    ).toEqual([
      "setMcpTransport",
      "syncRequirementService",
      "mcpLabel",
      "loadAll",
    ]);
  });

  it("leaves every chat-core function to chat-core", () => {
    const shared = sharedFunctions();
    expect(shared).toContain("setMcpTransport");
    const copies = CLIENT_SOURCES.flatMap((root) => {
      const dir = new URL(root, REPO);
      return sourceFiles(dir).flatMap((path) =>
        definedFunctions(readFileSync(new URL(path, dir), "utf8"))
          .filter((name) => shared.has(name))
          .map((name) => `${root}${path}: ${name}`),
      );
    });
    expect(copies).toEqual([]);
  });

  it("finds chat-core re-exports, not client-owned exports", () => {
    expect(
      coreReexports(
        [
          "import { formatStorageBytes, type UsageLevel } from '@opencuria/chat-core/workspaceDisk'",
          "import type { ProviderAccountProvider } from '@opencuria/chat-core/providerAccounts'",
          "import type { WorkspaceConversationGroup as Group } from '@opencuria/chat-core/conversationGroups'",
          "import { ref } from 'vue'",
          "export * from '@opencuria/chat-core/gitGraph'",
          'export type * from "@opencuria/chat-core/gitTypes"',
          "export type {\n  Plugin,\n} from '@opencuria/chat-core/plugins'",
          "export { UsageLevel }",
          "export const storageBytes = formatStorageBytes",
          "export type SettingsProviderId = ProviderAccountProvider",
          "export type WorkspaceGroup = Group<Workspace>",
          "export { ref }",
          "export { cn } from './utils'",
          "export const GIT_GRAPH_COLORS = ['var(--git-branch-1)']",
          "export function workspaceAttachmentTokens() {}",
        ].join("\n"),
      ),
    ).toEqual([
      "export * from '@opencuria/chat-core/gitGraph'",
      'export type * from "@opencuria/chat-core/gitTypes"',
      "export type { Plugin, } from '@opencuria/chat-core/plugins'",
      "UsageLevel",
      "formatStorageBytes",
      "ProviderAccountProvider",
    ]);
  });

  it("lets clients import chat-core directly instead of re-exporting it", () => {
    const reexports = CLIENT_SOURCES.flatMap((root) => {
      const dir = new URL(root, REPO);
      return sourceFiles(dir).flatMap((path) =>
        coreReexports(readFileSync(new URL(path, dir), "utf8")).map(
          (entry) => `${root}${path}: ${entry}`,
        ),
      );
    });
    expect(reexports).toEqual([]);
  });
});
