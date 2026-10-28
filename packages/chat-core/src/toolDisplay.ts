/** Pure tool labels, argument parsing and computer-use summaries. */

import type { HarnessPart } from "./harness";

/** Parse `input.arguments` whether it is a JSON string or an object. */
export function parseToolArguments(part: HarnessPart): Record<string, unknown> {
  const raw = part.input?.["arguments"];
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw !== "string" || !raw.trim()) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return {};
  }
  return {};
}

/** Tool id from `part.tool` or persisted `input.tool`. */
export function resolveToolName(part: HarnessPart): string {
  const named = (part.tool || "").trim();
  if (named) return named;
  const fromInput = part.input?.["tool"];
  if (typeof fromInput === "string" && fromInput.trim())
    return fromInput.trim();
  return "";
}

/** Copy a fetched part so `tool` is populated from `input.tool` after reload. */
export function hydrateHarnessPart(part: HarnessPart): HarnessPart {
  const tool = resolveToolName(part);
  if (tool && part.tool !== tool) {
    return { ...part, tool };
  }
  return part;
}

/** String argument helper for display labels. */
export function stringArg(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return "";
}

export function truncatePreview(text: string, max = 2000): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}\n…[truncated ${text.length} chars total]`;
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const slash = trimmed.lastIndexOf("/");
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed;
}

/** Recognize the stable product tool identity, including persisted calls. */
export function isComputerUseTool(part: HarnessPart): boolean {
  return /^mcp_computer-use_cua_repl_js(?:_reset)?$/.test(resolveToolName(part));
}

/** Compact one-line summary for computer-use tool arguments. */
export function computerUseSummary(part: HarnessPart): string {
  const tool = resolveToolName(part);
  const args = parseToolArguments(part);
  return (
    stringArg(args, "title").slice(0, 80) ||
    (tool.endsWith("_reset") ? "Reset Computer Use" : "Computer Use")
  );
}

/** Row label: persisted title, then a tool-specific fallback from arguments. */
export function toolDisplayLabel(part: HarnessPart): string {
  if (part.type === "reasoning") return "Thought";
  if (isComputerUseTool(part)) return part.title || computerUseSummary(part);
  if ((part.title || "").trim()) return part.title;
  const tool = resolveToolName(part);
  const args = parseToolArguments(part);
  switch (tool) {
    case "bash": {
      const command = stringArg(args, "command").split("\n")[0] ?? "";
      return command ? `$ ${command}` : "bash";
    }
    case "read": {
      const path = stringArg(args, "path");
      return path ? `Read ${basename(path)}` : "read";
    }
    case "write": {
      const path = stringArg(args, "path");
      return path ? `Write ${basename(path)}` : "write";
    }
    case "edit": {
      const path = stringArg(args, "path");
      return path ? `Edit ${basename(path)}` : "edit";
    }
    case "glob":
      return stringArg(args, "pattern")
        ? `Glob ${stringArg(args, "pattern")}`
        : "glob";
    case "grep":
      return stringArg(args, "pattern")
        ? `Grep ${stringArg(args, "pattern")}`
        : "grep";
    case "list": {
      const path = stringArg(args, "path");
      return path ? `List ${path}` : "list";
    }
    case "webfetch":
      return stringArg(args, "url")
        ? `Fetch ${stringArg(args, "url")}`
        : "webfetch";
    case "websearch": {
      const query = stringArg(args, "query");
      const action = stringArg(args, "action");
      if (!query) return action ? `Web search · ${action}` : "Web search";
      return `Search ${query}${action ? ` · ${action}` : ""}`;
    }
    case "memory": {
      const operation = stringArg(args, "operation");
      return operation ? `Memory · ${operation}` : "Memory";
    }
    case "image_generation": {
      const prompt = stringArg(args, "prompt");
      return prompt ? `Generate image: ${prompt}` : "Generate image";
    }
    case "tool_call": {
      const name = stringArg(args, "name");
      return name ? `Call ${name}` : "Tool call";
    }
    case "todowrite":
      return "Update todos";
    case "question":
      return "Question";
    case "task":
      return "Subagent";
    default:
      return tool || "tool";
  }
}
