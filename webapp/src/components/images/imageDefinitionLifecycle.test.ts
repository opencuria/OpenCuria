import { describe, expect, it } from 'vitest'

import {
  buildNeedsPolling,
  definitionNeedsPolling,
  getDefinitionActions,
  getDefinitionStatus,
  getRunnerBuildActions,
  getRunnerBuildStatus,
  isDefinitionHidden,
  isDefinitionLocked,
  isDefinitionSelectableForWorkspace,
  visibleImageDefinitions,
} from '@opencuria/chat-core/imageDefinitionLifecycle'
import type { ImageDefinition } from '@/types'

function makeDefinition(overrides: Partial<ImageDefinition> = {}): ImageDefinition {
  return {
    id: 'def-1',
    organization_id: 'org-1',
    name: 'Custom Image',
    description: '',
    is_standard: false,
    base_distro: 'ubuntu:24.04',
    packages: [],
    env_vars: {},
    custom_init_script: '',
    is_active: true,
    status: 'active',
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    ...overrides,
  }
}

const summary = (counts: Partial<NonNullable<ImageDefinition['runner_build_summary']>>) => ({
  active: 0,
  building: 0,
  failed: 0,
  inactive: 0,
  removing: 0,
  ...counts,
})

describe('getDefinitionStatus', () => {
  it('shows one state across runners: progress, then failures, then ready', () => {
    const status = (counts: Parameters<typeof summary>[0]) =>
      getDefinitionStatus({ status: 'active', runner_build_summary: summary(counts) })

    expect(status({})).toEqual({ label: 'Not built', tone: 'muted' })
    expect(getDefinitionStatus({ status: 'active' })).toEqual({ label: 'Not built', tone: 'muted' })
    expect(status({ active: 2 })).toEqual({ label: 'Ready', tone: 'success' })
    expect(status({ active: 1, building: 1 })).toEqual({ label: 'Building', tone: 'progress' })
    expect(status({ active: 1, failed: 1 })).toEqual({ label: 'Failed', tone: 'danger' })
    expect(status({ inactive: 1 })).toEqual({ label: 'Disabled', tone: 'muted' })
    expect(status({ removing: 1 })).toEqual({ label: 'Removing', tone: 'progress' })
  })

  it('lets the definition lifecycle win over its builds', () => {
    const builds = summary({ active: 1 })
    expect(getDefinitionStatus({ status: 'deleting', runner_build_summary: builds }).label).toBe(
      'Deleting',
    )
    expect(getDefinitionStatus({ status: 'delete_failed', runner_build_summary: builds })).toEqual({
      label: 'Delete failed',
      tone: 'danger',
    })
    expect(getDefinitionStatus({ status: 'deactivated', runner_build_summary: builds }).label).toBe(
      'Disabled',
    )
  })
})

describe('getRunnerBuildActions', () => {
  const ids = (status: string | null, extra: { pending_generation_id?: string } = {}) =>
    getRunnerBuildActions(status === null ? null : { status, ...extra }).map((action) => action.id)

  it('offers Build when the runner has no image yet', () => {
    expect(getRunnerBuildActions(null)).toEqual([{ id: 'build', label: 'Build', kind: 'primary' }])
    expect(ids('deleted')).toEqual(['build'])
  })

  it('makes Rebuild the one next step for a ready image', () => {
    const actions = getRunnerBuildActions({ status: 'active' })
    expect(actions.map((action) => action.label)).toEqual(['Rebuild', 'Disable', 'Remove'])
    expect(actions.filter((action) => action.kind === 'primary')).toHaveLength(1)
  })

  it('shows the log instead of Rebuild while a new version builds', () => {
    expect(ids('active', { pending_generation_id: 'gen-2' })).toEqual([
      'view_log',
      'deactivate',
      'remove',
    ])
    expect(ids('deactivated', { pending_generation_id: 'gen-2' })).toEqual([
      'activate',
      'view_log',
      'remove',
    ])
  })

  it('enables a disabled image before offering a rebuild', () => {
    expect(getRunnerBuildActions({ status: 'deactivated' }).map((action) => action.label)).toEqual([
      'Enable',
      'Rebuild',
      'Remove',
    ])
  })

  it('offers retry and the build log for failed builds', () => {
    expect(getRunnerBuildActions({ status: 'failed' }).map((action) => action.label)).toEqual([
      'Retry',
      'Build log',
      'Remove',
    ])
  })

  it('hides runner mutations while a definition delete is in flight', () => {
    expect(getRunnerBuildActions({ status: 'active' }, { definitionLocked: true })).toEqual([])
  })
})

const buildStates = [
  [undefined, 'Not built', 'muted', ['build'], false],
  ['pending', 'Building', 'progress', ['view_log'], true],
  ['building', 'Building', 'progress', ['view_log'], true],
  ['active', 'Ready', 'success', ['rebuild', 'deactivate', 'remove'], false],
  ['deactivated', 'Disabled', 'muted', ['activate', 'rebuild', 'remove'], false],
  ['failed', 'Failed', 'danger', ['retry', 'view_log', 'remove'], false],
  ['pending_deletion', 'Removing', 'progress', [], true],
  ['deleting', 'Removing', 'progress', [], true],
  ['delete_failed', 'Removal failed', 'danger', ['retry_remove'], false],
  ['deleted', 'Not built', 'muted', ['build'], false],
] as const
let buildCaseIndex = 40
const buildCases = buildStates.flatMap(([status, label, tone, actions, polling]) =>
  [true, false].map((online) => ({
    id: `F${String(++buildCaseIndex).padStart(3, '0')}`,
    status,
    online,
    label:
      !online && status === 'pending'
        ? 'Waiting for runner'
        : !online && status === 'building'
          ? 'Runner offline'
          : label,
    tone: !online && (status === 'pending' || status === 'building') ? 'muted' : tone,
    actions,
    polling,
  })),
)
it.each(buildCases)('$id image $status online=$online', (row) => {
  const build = { status: row.status }
  expect(getRunnerBuildStatus(build, row.online)).toEqual({ label: row.label, tone: row.tone })
  expect(getRunnerBuildActions(build).map((action) => action.id)).toEqual(row.actions)
  expect(getRunnerBuildActions(build, { definitionLocked: true })).toEqual([])
  expect(buildNeedsPolling(build)).toBe(row.polling)
})

describe('runner build status', () => {
  it('reads as Updating while a ready image builds its next version', () => {
    const build = { status: 'active', pending_generation_id: 'gen-2' }
    expect(getRunnerBuildStatus(build, true)).toEqual({ label: 'Updating', tone: 'progress' })
    expect(buildNeedsPolling(build)).toBe(true)
    expect(getRunnerBuildStatus(null, true)).toEqual({ label: 'Not built', tone: 'muted' })
  })
})

describe('definition polling', () => {
  it('polls while a definition is deleted, built or removed somewhere', () => {
    expect(definitionNeedsPolling(makeDefinition({ status: 'deleting' }))).toBe(true)
    expect(
      definitionNeedsPolling(makeDefinition({ runner_build_summary: summary({ building: 1 }) })),
    ).toBe(true)
    expect(
      definitionNeedsPolling(makeDefinition({ runner_build_summary: summary({ removing: 1 }) })),
    ).toBe(true)
    expect(
      definitionNeedsPolling(makeDefinition({ runner_build_summary: summary({ active: 1 }) })),
    ).toBe(false)
    expect(definitionNeedsPolling(null)).toBe(false)
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
      canEnable: false,
    })
    expect(getDefinitionActions(makeDefinition({ status: 'delete_failed' }))).toEqual({
      canDuplicate: false,
      canEdit: false,
      canDelete: false,
      canRetryDelete: true,
      canRestore: true,
      canEnable: false,
    })
    expect(
      getDefinitionActions(makeDefinition({ is_standard: true, status: 'active' })),
    ).toMatchObject({ canEdit: false, canDelete: false, canDuplicate: true })
  })

  it('lets admins enable their own disabled definitions only', () => {
    expect(getDefinitionActions(makeDefinition({ status: 'deactivated' })).canEnable).toBe(true)
    expect(
      getDefinitionActions(makeDefinition({ status: 'deactivated', is_standard: true })).canEnable,
    ).toBe(false)
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
      isDefinitionSelectableForWorkspace(makeDefinition({ status: 'deleted', is_active: false })),
    ).toBe(false)
  })
})
