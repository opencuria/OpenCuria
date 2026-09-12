/**
 * Static presentation metadata for the supported harness providers.
 * Shared by the provider list (ProviderConfigTab) and the connection dialog.
 */

import { Bot, Cloud, Globe, Plug } from '@lucide/vue'
import type { ProviderId } from '@/lib/harnessModels'
import type { ProviderConnection } from '@/services/harness.api'

export interface ProviderMeta {
  id: ProviderId
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
]

/** Look up presentation metadata for a provider id. */
export function providerMeta(id: ProviderId | null): ProviderMeta | undefined {
  return PROVIDER_META.find((meta) => meta.id === id)
}

/** Short human summary of a connection (key hint, account, region). */
export function connectionDetail(connection: ProviderConnection): string {
  if (!connection.connected) return ''
  switch (connection.provider) {
    case 'openrouter':
      return [connection.api_key_hint, connection.base_url].filter(Boolean).join(' · ')
    case 'chatgpt':
      return connection.account_id ? `Account ${connection.account_id}` : 'Connected'
    case 'amazon-bedrock': {
      const auth =
        connection.auth_method === 'bearer'
          ? 'Bearer token'
          : connection.auth_method === 'access_keys'
            ? 'Access keys'
            : ''
      return [connection.region, auth].filter(Boolean).join(' · ')
    }
    case 'openai-compatible': {
      const parts: string[] = []
      if (connection.base_url) parts.push(connection.base_url)
      const count = connection.models?.length ?? 0
      if (count > 0) parts.push(`${count} model${count === 1 ? '' : 's'}`)
      else if (parts.length === 0) return 'Connected'
      return parts.join(' · ')
    }
  }
}
