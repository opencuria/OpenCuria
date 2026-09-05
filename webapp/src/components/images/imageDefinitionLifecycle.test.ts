import { describe, expect, it } from 'vitest'

import {
  formatRunnerBuildSummary,
  getDefinitionActions,
  getRunnerBuildActions,
  getRunnerBuildStatusLabel,
  isDefinitionHidden,
  isDefinitionLocked,
  isDefinitionSelectableForWorkspace,
  visibleImageDefinitions,
} from './imageDefinitionLifecycle'
import type { ImageDefinition } from '@/types'

function makeDefinition(
  overrides: Partial<ImageDefinition> = {},
): ImageDefinition {
  return {
    id: 'def-1',
    organization_id: 'org-1',
    name: 'Custom Image',
    description: '',
    is_standard: false,
    runtime_type: 'docker',
    base_distro: 'ubuntu:24.04',
    packages: [],
    env_vars: {},
    custom_dockerfile: '',
    custom_init_script: '',
    is_active: true,
    status: 'active',
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    ...overrides,
  }
}

describe('formatRunnerBuildSummary', () => {
  it('uses API counts without requiring expanded builds', () => {
    expect(
      formatRunnerBuildSummary({
        active: 2,
        building: 1,
        failed: 0,
        inactive: 0,
        removing: 0,
      }),
    ).toBe('2 active, 1 building, 0 failed')
  })

  it('shows a clear empty state instead of zeros', () => {
    expect(formatRunnerBuildSummary()).toBe('Not built on any runner')
    expect(
      formatRunnerBuildSummary({
        active: 0,
        building: 0,
        failed: 0,
        inactive: 0,
        removing: 0,
      }),
    ).toBe('Not built on any runner')
  })

  it('includes inactive and removing counts when present', () => {
    expect(
      formatRunnerBuildSummary({
        active: 1,
        building: 0,
        failed: 1,
        inactive: 2,
        removing: 1,
      }),
    ).toBe('1 active, 0 building, 1 failed, 2 inactive, 1 removing')
  })
})

describe('getRunnerBuildActions', () => {
  it('offers Build when the runner has never been assigned', () => {
    expect(getRunnerBuildActions(null).map((action) => action.label)).toEqual(['Build'])
    expect(getRunnerBuildActions('deleted').map((action) => action.label)).toEqual(['Build'])
  })

  it('offers Rebuild as the primary action for an active image', () => {
    const labels = getRunnerBuildActions('active').map((action) => action.label)
    expect(labels).toEqual(['Rebuild', 'Deactivate', 'Remove'])
    expect(labels).not.toContain('Rebuild / Activate')
    expect(labels).not.toContain('Assign & Activate')
  })

  it('activates a deactivated image without requiring rebuild as the primary action', () => {
    expect(getRunnerBuildActions('deactivated').map((action) => action.id)).toEqual([
      'activate',
      'rebuild',
      'remove',
    ])
  })

  it('offers retry and log for failed builds', () => {
    expect(getRunnerBuildActions('failed').map((action) => action.id)).toEqual([
      'retry',
      'view_log',
      'remove',
    ])
  })

  it('hides runner mutations while a definition delete is in flight', () => {
    expect(getRunnerBuildActions('active', { definitionLocked: true })).toEqual([])
  })
})

describe('getRunnerBuildStatusLabel', () => {
  it('labels pending offline builds as waiting for the runner', () => {
    expect(getRunnerBuildStatusLabel('pending', false)).toBe('Waiting for runner')
    expect(getRunnerBuildStatusLabel('pending', true)).toBe('Building')
    expect(getRunnerBuildStatusLabel(null, true)).toBe('Not built')
  })
})

describe('definition visibility and actions', () => {
  it('hides fully deleted definitions', () => {
    expect(isDefinitionHidden('deleted')).toBe(true)
    expect(
      visibleImageDefinitions([
        makeDefinition({ id: 'keep', status: 'active' }),
        makeDefinition({ id: 'gone', status: 'deleted' }),
      ]).map((definition) => definition.id),
    ).toEqual(['keep'])
  })

  it('locks mutations during deletion and exposes restore after failure', () => {
    expect(isDefinitionLocked('pending_deletion')).toBe(true)
    expect(getDefinitionActions(makeDefinition({ status: 'active' }))).toEqual({
      canDuplicate: true,
      canEdit: true,
      canDelete: true,
      canRetryDelete: false,
      canRestore: false,
    })
    expect(getDefinitionActions(makeDefinition({ status: 'delete_failed' }))).toEqual({
      canDuplicate: false,
      canEdit: false,
      canDelete: false,
      canRetryDelete: true,
      canRestore: true,
    })
    expect(
      getDefinitionActions(makeDefinition({ is_standard: true, status: 'active' })),
    ).toMatchObject({ canEdit: false, canDelete: false, canDuplicate: true })
  })

  it('keeps deleting definitions out of the workspace picker', () => {
    expect(
      isDefinitionSelectableForWorkspace(makeDefinition({ status: 'active', is_active: true })),
    ).toBe(true)
    expect(
      isDefinitionSelectableForWorkspace(
        makeDefinition({ status: 'delete_failed', is_active: false }),
      ),
    ).toBe(false)
    expect(
      isDefinitionSelectableForWorkspace(
        makeDefinition({ status: 'deleted', is_active: false }),
      ),
    ).toBe(false)
  })
})
