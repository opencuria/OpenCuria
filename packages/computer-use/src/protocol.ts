import type { CallToolResult, ContentBlock } from '@modelcontextprotocol/sdk/types.js';
import contract from './contract.json' with { type: 'json' };

export const VERSION = '0.2.0';
export const MAX_CODE_BYTES = 64 * 1024;
export const MAX_TEXT_BYTES = 64 * 1024;
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
export const MAX_RESULT_BYTES = 6 * 1024 * 1024;
export const DEFAULT_TIMEOUT_MS = 30_000;
export const MAX_TIMEOUT_MS = 2_147_483_647;

export type RuntimeContext = {
  session_id: string;
  turn_id: string;
  generation?: number;
  input_generation?: number;
  epoch?: string;
  display?: string;
  xauthority?: string;
  dbus_session_bus_address?: string;
  screenshot_max_dimension?: number;
  request_meta?: Record<string, unknown>;
};

export type NativeReply = Record<string, any>;
export type NativeRequest = { op: string; args: Record<string, unknown> };
export type WorkerRequest = { type: 'execute'; id: string; code: string; timeout: number; context: RuntimeContext };
export type WorkerEvent =
  | { type: 'native'; id: string; requestId: number; request: NativeRequest }
  | { type: 'content'; id: string; content: ContentBlock; name?: string }
  | { type: 'done'; id: string; error?: string };

export class CuaError extends Error {
  constructor(public code: string, message: string, public effectStatus = 'none') {
    super(message);
  }
}

export function errorResult(error: unknown): CallToolResult {
  const message = error instanceof Error ? error.message : 'Computer use failed';
  const code = error instanceof CuaError ? error.code : 'COMPUTER_USE_ERROR';
  const effect_status = error instanceof CuaError ? error.effectStatus : 'unknown';
  return {
    isError: true,
    content: [{ type: 'text', text: message.slice(0, 4000) }],
    _meta: { 'codex/nodeReplExecutionDurationMs': 0, computer_use: { code, effect_status } },
  };
}

/** Versioned agent-facing contract of the computer-only Linux reference. */
export const DOCUMENTATION = contract.documentation;
export const TOOLS = contract.tools;
