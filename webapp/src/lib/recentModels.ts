/**
 * Recent composer models: server-persisted usage (model + last effort).
 *
 * Written on successful sends via `saveRecentModel` (fire-and-forget, never
 * blocks sending); read once per composer mount and mirrored into
 * `HarnessModelPicker` as `recentModels`. Capped at 6 entries in the UI;
 * the server keeps 10 (LRU) so deletions stay invisible.
 */

import { ref } from 'vue'

import type { ProviderModel } from './harnessModels'
import { listRecentModels, saveRecentModel } from '@/services/harness.api'

export const MAX_RECENT_MODELS = 6

export interface RecentModelEntry {
  id: string
  effort: string
}

/** In-memory mirror of the server list (single-flight load, shared). */
const entries = ref<RecentModelEntry[]>([])
let inflight: Promise<RecentModelEntry[]> | null = null

function normalize(rows: RecentModelUsageRow[]): RecentModelEntry[] {
  const seen = new Set<string>()
  const out: RecentModelEntry[] = []
  for (const row of rows) {
    const id = (row?.model ?? '').trim()
    if (!id || seen.has(id)) continue
    seen.add(id)
    out.push({ id, effort: (row?.effort ?? '').trim().toLowerCase() })
    if (out.length >= MAX_RECENT_MODELS) break
  }
  return out
}

interface RecentModelUsageRow {
  model?: string
  effort?: string
}

async function fetchRecents(): Promise<RecentModelEntry[]> {
  const rows = await listRecentModels()
  entries.value = normalize(Array.isArray(rows) ? rows : [])
  return entries.value
}

/** Load the user's recent models (coalesced; failures yield []). */
export function loadRecentModels(): Promise<RecentModelEntry[]> {
  if (inflight == null) {
    inflight = fetchRecents().catch(() => {
      entries.value = []
      return entries.value
    })
  }
  return inflight
}

/** Shared reactive mirror (read-only view for the picker parent). */
export function useRecentModels(): { entries: typeof entries } {
  return { entries }
}

/** Last-used effort for a model id ('' when unknown). */
export function recentEffortFor(modelId: string): string {
  return entries.value.find((entry) => entry.id === modelId)?.effort ?? ''
}

/**
 * Order catalog ids by recency; unknown ids keep catalog order at the end.
 * Used to build the picker's default Recent slice.
 */
export function orderByRecency(ids: string[]): string[] {
  const rank = new Map(entries.value.map((entry, index) => [entry.id, index]))
  return [...ids].sort((a, b) => {
    const ra = rank.get(a)
    const rb = rank.get(b)
    if (ra == null && rb == null) return 0
    if (ra == null) return 1
    if (rb == null) return -1
    return ra - rb
  })
}

/**
 * Keep only catalog-known recents (in recency order), capped at 6.
 * Unknown/stale ids are dropped so removed models never render.
 */
export function recentCatalogModels(models: ProviderModel[]): ProviderModel[] {
  const byId = new Map(models.map((item) => [item.id, item]))
  const out: ProviderModel[] = []
  for (const entry of entries.value) {
    const found = byId.get(entry.id)
    if (found) out.push(found)
    if (out.length >= MAX_RECENT_MODELS) break
  }
  return out
}

/**
 * Record one usage after a successful send. Updates the local mirror
 * optimistically (LRU) and persists server-side without blocking.
 */
export function recordRecentModelUsage(modelId: string, effort = ''): void {
  const id = (modelId ?? '').trim()
  if (!id) return
  const normalizedEffort = (effort ?? '').trim().toLowerCase()
  entries.value = [
    { id, effort: normalizedEffort },
    ...entries.value.filter((entry) => entry.id !== id),
  ].slice(0, MAX_RECENT_MODELS)
  // Fire-and-forget: a failed save must never break sending. The
  // Promise.resolve guard also tolerates mocked clients in tests.
  void Promise.resolve()
    .then(() => saveRecentModel(id, normalizedEffort))
    .catch(() => undefined)
}

/** Drop caches (tests only). */
export function resetRecentModelsCache(): void {
  entries.value = []
  inflight = null
}
