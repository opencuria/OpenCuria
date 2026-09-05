/**
 * Unit tests for OpenRouter catalog helpers.
 */

import { describe, expect, it } from 'vitest'
import {
  formatEffort,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from './harnessModels'

const withEffort: ProviderModel = {
  id: 'acme/think',
  name: 'Think',
  reasoning_efforts: ['low', 'medium', 'high'],
  default_effort: 'medium',
  supports_tools: true,
}

const plain: ProviderModel = {
  id: 'acme/plain',
  name: 'Plain',
  reasoning_efforts: [],
  default_effort: '',
  supports_tools: true,
}

describe('harnessModels', () => {
  it('formats known effort tokens', () => {
    expect(formatEffort('high')).toBe('High')
    expect(formatEffort('xhigh')).toBe('Extra High')
    expect(formatEffort('mystery')).toBe('mystery')
  })

  it('snaps unsupported effort to the model default', () => {
    expect(snapEffort(withEffort, 'high')).toBe('high')
    expect(snapEffort(withEffort, 'max')).toBe('medium')
    expect(snapEffort(plain, 'high')).toBe('')
    expect(snapEffort(undefined, 'high')).toBe('')
  })

  it('resolves Auto to the org default catalog row', () => {
    const models = [withEffort, plain]
    expect(resolveCatalogModel(models, '', 'acme/think')?.id).toBe('acme/think')
    expect(resolveCatalogModel(models, 'acme/plain')?.id).toBe('acme/plain')
  })
})
