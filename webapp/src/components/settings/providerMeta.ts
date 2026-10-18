/**
 * Static presentation metadata for the supported harness providers.
 * Shared by the provider list (ProviderConfigTab) and the connection dialog.
 */

import { Bot, Cloud, Globe, Plug } from '@lucide/vue'
import type { ProviderId } from '@/lib/harnessModels'

/** Settings-only Claude connection row, not a native catalog ProviderId. */
export type SettingsProviderId = ProviderId | 'claude-agent'

export interface ProviderMeta {
  id: SettingsProviderId
  name: string
  /** Shown on the provider row while not connected. */
  description: string
  /** Shown in the connection dialog header. */
  dialogDescription: string
  /** Shown in the inline disconnect confirmation. */
  disconnectConfirm: string
  icon: typeof Globe
}

export const PROVIDER_META: ProviderMeta[] = [
  {
    id: 'openrouter',
    name: 'OpenRouter',
    description: 'Route requests through OpenRouter with your API key.',
    dialogDescription: 'Enter your OpenRouter API key and optional custom base URL.',
    disconnectConfirm: 'Disconnect OpenRouter? The harness will stop using this API key.',
    icon: Globe,
  },
  {
    id: 'chatgpt',
    name: 'OpenAI ChatGPT',
    description: 'Use your ChatGPT Plus or Pro subscription via device login.',
    dialogDescription:
      'Use your ChatGPT Plus or Pro subscription. You will authorize via OpenAI device login.',
    disconnectConfirm: 'Disconnect ChatGPT? Subscription access will be removed.',
    icon: Bot,
  },
  {
    id: 'amazon-bedrock',
    name: 'Amazon Bedrock',
    description: 'Connect AWS credentials or a bearer token for Bedrock models.',
    dialogDescription: 'Connect with IAM access keys or a bearer token for your Bedrock region.',
    disconnectConfirm: 'Disconnect Amazon Bedrock? The harness will stop using these credentials.',
    icon: Cloud,
  },
  {
    id: 'openai-compatible',
    name: 'OpenAI Compatible',
    description:
      'Custom OpenAI-compatible endpoint for self-hosted models (e.g. HuggingFace UI-TARS).',
    dialogDescription:
      'Connect a custom OpenAI-compatible endpoint and list its model ids manually.',
    disconnectConfirm:
      'Disconnect this endpoint? The harness will stop using this base URL and its models.',
    icon: Plug,
  },
  {
    id: 'claude-agent',
    name: 'Claude Agent',
    description: 'Run the separate Claude Agent harness with an Anthropic account.',
    dialogDescription:
      'Use your own Anthropic API token or subscription token. Usage is billed or governed by Anthropic.',
    disconnectConfirm:
      'Disconnect Claude Agent? Its personal Anthropic connection will be removed.',
    icon: Bot,
  },
]

/** Look up presentation metadata for a provider id. */
export function providerMeta(id: SettingsProviderId | null): ProviderMeta | undefined {
  return PROVIDER_META.find((meta) => meta.id === id)
}
