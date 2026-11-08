/**
 * Settings provider rows: the provider metadata shared with the native
 * settings plus the icon the web shows for it.
 */

import { Bot, Cloud, Globe, Plug } from '@lucide/vue'
import {
  PROVIDER_ACCOUNT_PROVIDERS,
  type ProviderAccountInfo,
  type ProviderAccountProvider,
} from '@opencuria/chat-core/providerAccounts'

export type SettingsProviderId = ProviderAccountProvider

export interface ProviderMeta extends ProviderAccountInfo {
  icon: typeof Globe
}

const ICONS: Record<SettingsProviderId, typeof Globe> = {
  openrouter: Globe,
  chatgpt: Bot,
  'amazon-bedrock': Cloud,
  'openai-compatible': Plug,
  'claude-agent': Bot,
}

export const PROVIDER_META: ProviderMeta[] = PROVIDER_ACCOUNT_PROVIDERS.map((info) => ({
  ...info,
  icon: ICONS[info.id],
}))

/** Look up presentation metadata for a provider id. */
export function providerMeta(id: SettingsProviderId | null): ProviderMeta | undefined {
  return PROVIDER_META.find((meta) => meta.id === id)
}
