import type { PluginFormModel } from "@opencuria/chat-core/pluginForms";

/**
 * A plugin being created (`pluginId` null) or edited. The editor and its
 * skill, MCP server and requirement pages edit one shared draft; every
 * change returns a new draft so native snapshots stay immutable.
 */
export interface PluginDraft {
  pluginId: string | null;
  form: PluginFormModel;
}

let drafts = 0;

/** A route-safe key for a new draft. */
export function newDraftKey(): string {
  drafts += 1;
  return `${Date.now().toString(36)}${drafts}`;
}
