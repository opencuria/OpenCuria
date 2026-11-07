import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * Rules shared by the web and native clients have exactly one
 * implementation in chat-core. Clients import them; a client function
 * named like a chat-core export is a second copy of that rule.
 */
const REPO = new URL("../../../", import.meta.url);
const CORE_SOURCES = new URL("packages/chat-core/src/", REPO);
const CLIENT_SOURCES = ["webapp/src/", "mobile/src/"];

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
});
