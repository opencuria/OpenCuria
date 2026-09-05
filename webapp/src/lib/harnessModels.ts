/**
 * OpenRouter catalog helpers for the composer model/effort picker.
 */

export interface ProviderModel {
  id: string
  name: string
  reasoning_efforts: string[]
  default_effort: string
  supports_tools: boolean
}

export const EFFORT_LABELS: Record<string, string> = {
  none: 'None',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra High',
  max: 'Max',
}

/** Human-readable label for an OpenRouter effort token. */
export function formatEffort(effort: string): string {
  const key = effort.trim().toLowerCase()
  return EFFORT_LABELS[key] ?? effort
}

/**
 * Keep the current effort when the model still supports it; otherwise fall
 * back to the model's default (or first) effort.
 */
export function snapEffort(model: ProviderModel | undefined, current: string): string {
  if (!model || model.reasoning_efforts.length === 0) return ''
  const token = current.trim().toLowerCase()
  if (token && model.reasoning_efforts.includes(token)) return token
  if (model.default_effort && model.reasoning_efforts.includes(model.default_effort)) {
    return model.default_effort
  }
  return model.reasoning_efforts[0] ?? ''
}

/** Catalog row for the selected model id, or the org default when Auto. */
export function resolveCatalogModel(
  models: ProviderModel[],
  modelId: string,
  defaultModel = '',
): ProviderModel | undefined {
  const effective = modelId.trim() || defaultModel.trim()
  if (!effective) return undefined
  return models.find((item) => item.id === effective)
}
