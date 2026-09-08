/**
 * Unit tests for OpenRouter catalog helpers.
 */

import { describe, expect, it } from 'vitest'
import {
  formatContextLength,
  formatEffort,
  formatHarnessModelEffort,
  providerDisplayName,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from './harnessModels'

const withEffort: ProviderModel = {
  id: 'openrouter/think',
  name: 'Think',
  provider: 'openrouter',
  reasoning_efforts: ['low', 'medium', 'high'],
  default_effort: 'medium',
  supports_tools: true,
  context_length: 128_000,
  max_output_tokens: 16_384,
}

const plain: ProviderModel = {
  id: 'chatgpt/plain',
  name: 'Plain',
  provider: 'chatgpt',
  reasoning_efforts: [],
  default_effort: '',
  supports_tools: true,
  context_length: 0,
  max_output_tokens: 0,
}

describe('harnessModels', () => {
  it('formats known effort tokens', () => {
    expect(formatEffort('high')).toBe('High')
    expect(formatEffort('xhigh')).toBe('Extra High')
    expect(formatEffort('mystery')).toBe('mystery')
  })

  it('formats context lengths compactly', () => {
    expect(formatContextLength(128_000)).toBe('128k')
    expect(formatContextLength(200_000)).toBe('200k')
    expect(formatContextLength(1_000_000)).toBe('1m')
    expect(formatContextLength(1_500_000)).toBe('1.5m')
    expect(formatContextLength(500)).toBe('500')
    expect(formatContextLength(0)).toBe('')
    expect(formatContextLength(-1)).toBe('')
  })

  it('snaps unsupported effort to the model default', () => {
    expect(snapEffort(withEffort, 'high')).toBe('high')
    expect(snapEffort(withEffort, 'max')).toBe('medium')
    expect(snapEffort(plain, 'high')).toBe('')
    expect(snapEffort(undefined, 'high')).toBe('')
  })

  it('resolves Auto to the org default catalog row', () => {
    const models = [withEffort, plain]
    expect(resolveCatalogModel(models, '', 'openrouter/think')?.id).toBe('openrouter/think')
    expect(resolveCatalogModel(models, 'chatgpt/plain')?.id).toBe('chatgpt/plain')
  })

  it('maps provider ids to display names', () => {
    expect(providerDisplayName('openrouter')).toBe('OpenRouter')
    expect(providerDisplayName('chatgpt')).toBe('ChatGPT')
    expect(providerDisplayName('amazon-bedrock')).toBe('Amazon Bedrock')
    expect(providerDisplayName('custom')).toBe('custom')
  })

  it('formats model and effort for conversation cards', () => {
    const models = [withEffort, plain]
    expect(formatHarnessModelEffort('openrouter/think', 'high', models)).toBe('Think High')
    expect(formatHarnessModelEffort('', 'medium', models, 'openrouter/think')).toBe('Auto Medium')
    expect(formatHarnessModelEffort('chatgpt/plain', '', models)).toBe('Plain')
    expect(formatHarnessModelEffort('missing/id', 'low', [])).toBe('missing/id Low')
  })
})
