/**
 * Static presentation metadata for the supported harness providers.
 * Shared by the provider list (ProviderConfigTab) and the connection dialog.
 */

import { Bot, Cloud, Globe, Plug } from '@lucide/vue'
import { providerAccountInfo } from '@opencuria/chat-core/providerAccounts'
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

/** Id, name and row description shared with the native settings. */
function shared(id: SettingsProviderId): Pick<ProviderMeta, 'id' | 'name' | 'description'> {
  const info = providerAccountInfo(id)!
  return { id, name: info.name, description: info.description }
}

export const PROVIDER_META: ProviderMeta[] = [
  {
    ...shared('openrouter'),
    dialogDescription: 'Enter your OpenRouter API key and optional custom base URL.',
    disconnectConfirm: 'Disconnect OpenRouter? The harness will stop using this API key.',
    icon: Globe,
  },
  {
    ...shared('chatgpt'),
    dialogDescription:
      'Use your ChatGPT Plus or Pro subscription. You will authorize via OpenAI device login.',
    disconnectConfirm: 'Disconnect ChatGPT? Subscription access will be removed.',
    icon: Bot,
  },
  {
    ...shared('amazon-bedrock'),
    dialogDescription: 'Connect with IAM access keys or a bearer token for your Bedrock region.',
    disconnectConfirm: 'Disconnect Amazon Bedrock? The harness will stop using these credentials.',
    icon: Cloud,
  },
  {
    ...shared('openai-compatible'),
    dialogDescription:
      'Connect a custom OpenAI-compatible endpoint and list its model ids manually.',
    disconnectConfirm:
      'Disconnect this endpoint? The harness will stop using this base URL and its models.',
    icon: Plug,
  },
  {
    ...shared('claude-agent'),
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
