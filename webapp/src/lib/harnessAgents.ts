/**
 * Agent-config types and helpers for the agent settings surface.
 */

export type AgentConfigId = 'build' | 'plan' | 'general' | 'explore' | 'computeruse'

export type AgentMode = 'primary' | 'subagent'

export type EffortStrategy = 'fixed' | 'inherit' | 'lowest' | 'medium' | 'highest'

export interface AgentConfig {
  agent: string
  mode: AgentMode
  description: string
  model: string
  effort: string
  inherit_model: boolean
  effort_strategy: EffortStrategy
}

export const CONFIGURABLE_AGENTS: AgentConfigId[] = [
  'build',
  'plan',
  'general',
  'explore',
  'computeruse',
]

export const PRIMARY_AGENTS: AgentConfigId[] = ['build', 'plan']

export const SUBAGENT_IDS: AgentConfigId[] = ['general', 'explore', 'computeruse']

export interface EffortStrategyOption {
  value: EffortStrategy
  label: string
  hint: string
}

export const EFFORT_STRATEGIES: EffortStrategyOption[] = [
  { value: 'fixed', label: 'Fixed', hint: 'Always use the configured effort.' },
  { value: 'inherit', label: 'Inherit', hint: 'Use the parent run effort.' },
  { value: 'lowest', label: 'Lowest', hint: 'Use the lowest supported effort.' },
  { value: 'medium', label: 'Medium', hint: 'Use the middle supported effort.' },
  { value: 'highest', label: 'Highest', hint: 'Use the highest supported effort.' },
]

const AGENT_DISPLAY_NAMES: Record<string, string> = {
  build: 'Build',
  plan: 'Plan',
  general: 'General',
  explore: 'Explore',
  computeruse: 'Computer Use',
}

/** Human-readable label for an agent id. */
export function agentDisplayName(agent: string): string {
  return AGENT_DISPLAY_NAMES[agent] ?? agent
}

/** True when the mode (or config) describes a subagent. */
export function isSubagent(modeOrConfig: AgentMode | string | AgentConfig): boolean {
  const mode =
    typeof modeOrConfig === 'string' ? modeOrConfig : (modeOrConfig.mode ?? '')
  return mode === 'subagent'
}

/**
 * Preview the strategy-resolved effort for a model's effort list.
 * Mirrors `backend/apps/harness/agents/effort_strategy.py`:
 * lowest = efforts[0], medium = efforts[floor(n/2)], highest = last,
 * anything else (fixed/inherit/empty) resolves to ''.
 */
export function resolvePreviewEffort(efforts: string[], strategy: string): string {
  if (!efforts || efforts.length === 0) return ''
  switch (strategy) {
    case 'lowest':
      return efforts[0] ?? ''
    case 'medium':
      return efforts[Math.floor(efforts.length / 2)] ?? ''
    case 'highest':
      return efforts[efforts.length - 1] ?? ''
    default:
      return ''
  }
}
