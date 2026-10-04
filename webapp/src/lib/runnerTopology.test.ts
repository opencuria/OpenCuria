import { describe, expect, it } from 'vitest'
import { buildRunnerTopology, topologyPath } from './runnerTopology'
import type {
  StorageGeneration,
  StorageResource,
  StorageRuntime,
  StorageWorkspace,
} from '@/types/runnerStorage'
const ws = (id: string): StorageWorkspace => ({
  id,
  name: `Workspace ${id}`,
  owner_id: null,
  owner_label: 'Owner',
  status: 'ready',
  observed_state: 'unknown',
  last_activity_at: null,
})
const resource = (
  physical_id: string,
  overrides: Partial<StorageResource> = {},
): StorageResource => ({
  physical_id,
  kind: 'disk',
  managed: true,
  state: 'unknown',
  allocated_bytes: 1024,
  logical_bytes: null,
  virtual_bytes: null,
  shared_bytes: null,
  reclaimable_bytes: null,
  aliases: [],
  dependencies: [],
  image_id: null,
  workspace: null,
  provenance: '',
  ...overrides,
})
const runtime = (resources: StorageResource[], runtime_type = 'qemu'): StorageRuntime => ({
  runtime_type,
  resources,
  fresh: true,
  snapshot_id: 1,
  collected_at: null,
  received_at: null,
  filesystems: [],
  diagnostics: null,
})
const generation = (overrides: Partial<StorageGeneration> = {}): StorageGeneration => ({
  id: 'image',
  name: 'Base image',
  owner_label: 'Owner',
  runtime_type: 'qemu',
  definition_id: null,
  definition_name: null,
  build_job_id: null,
  generation: 1,
  status: 'ready',
  assignment_status: null,
  runner_ref: 'base-alias',
  size_bytes: null,
  size_source: 'unknown',
  origin_type: 'definition_build',
  revision_id: null,
  is_legacy: false,
  is_current: true,
  is_pending: false,
  observed_state: 'unknown',
  dependencies: [ws('a')],
  ...overrides,
})

describe('runner topology', () => {
  it('resolves recursive aliases once, shares dependencies, preserves cycles and dangling evidence', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', { kind: 'workspace', workspace: ws('a'), dependencies: ['overlay'] }),
          resource('b', { kind: 'container', workspace: ws('b'), dependencies: ['overlay'] }),
          resource('overlay', { dependencies: ['base-alias', 'missing'] }),
          resource('/images/base', {
            aliases: ['base-alias'],
            image_id: 'image',
            dependencies: ['overlay'],
            kind: 'image',
          }),
          resource('loose'),
        ]),
      ],
      [generation()],
    )
    expect(graph.nodes).toHaveLength(6)
    expect(graph.edges).toHaveLength(5)
    expect(graph.edges.every((e) => e.kind === 'physical')).toBe(true)
    const base = graph.nodes.find((n) => n.generation)!
    expect(base.label).toBe('Base image')
    expect(graph.nodes.find((n) => n.unresolved)?.unassigned).toBe(false)
    expect(graph.nodes.find((n) => n.label === 'loose')?.unassigned).toBe(true)
    expect(base.generation?.observed_state).toBe('unknown')
  })
  it('does not flood selected ancestors and descendants with unrelated siblings', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', { kind: 'workspace', workspace: ws('a'), dependencies: ['base'] }),
          resource('b', { kind: 'workspace', workspace: ws('b'), dependencies: ['base'] }),
          resource('base', { kind: 'image' }),
        ]),
      ],
      [],
    )
    const a = graph.nodes.find((n) => n.workspace?.id === 'a')!
    const b = graph.nodes.find((n) => n.workspace?.id === 'b')!
    expect(topologyPath([a.key], graph.edges).has(b.key)).toBe(false)
  })
  it('adds dashed domain usage only where no confirmed physical path exists', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', { kind: 'workspace', workspace: ws('a'), dependencies: ['base'] }),
          resource('base', { image_id: 'image' }),
        ]),
      ],
      [generation({ dependencies: [ws('a'), ws('logical')] })],
    )
    expect(graph.nodes.find((n) => n.resource?.physical_id === 'base')?.column).toBe(2)
    expect(graph.edges.filter((e) => e.kind === 'usage')).toHaveLength(1)
    expect(
      graph.nodes.find((n) => n.key === graph.edges.find((e) => e.kind === 'usage')?.from)?.label,
    ).toBe('Workspace logical')
  })
  it('includes unconfirmed generations without inventing observed ready, and scopes runtimes', () => {
    expect(buildRunnerTopology([], [generation()]).nodes).toHaveLength(2)
    expect(buildRunnerTopology([runtime([], 'docker')], [generation()]).nodes).toHaveLength(2)
    const graph = buildRunnerTopology(
      [runtime([resource('same')]), runtime([resource('same')], 'docker')],
      [],
    )
    expect(new Set(graph.nodes.map((n) => n.key)).size).toBe(2)
  })
  it('does not join ambiguous aliases or mistake a workspace image_id for the image', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', { kind: 'workspace', workspace: ws('a'), image_id: 'image' }),
          resource('first', { kind: 'image', aliases: ['base-alias'] }),
          resource('second', { kind: 'image', aliases: ['base-alias'] }),
        ]),
      ],
      [generation()],
    )
    expect(graph.nodes.find((n) => n.generation)?.resource).toBeUndefined()
    expect(graph.edges[0]?.kind).toBe('usage')
  })
})

it('joins a non-leaf backing disk by image_id and refuses conflicting runner_ref evidence', () => {
  const graph = buildRunnerTopology(
    [
      runtime([
        resource('backing', { image_id: 'image', dependencies: ['lower'] }),
        resource('base-alias', { kind: 'image', image_id: 'different-image' }),
      ]),
    ],
    [generation(), generation({ id: 'other' })],
  )
  expect(graph.nodes.find((n) => n.resource?.physical_id === 'backing')).toMatchObject({
    column: 2,
    generation: { id: 'image' },
  })
  expect(graph.nodes.find((n) => n.generation?.id === 'other')?.resource).toBeUndefined()
  expect(
    graph.nodes.find((n) => n.resource?.physical_id === 'base-alias')?.generation,
  ).toBeUndefined()
})
it('gives attached disks distinct safe labels without changing inventory kind or management', () => {
  const id = '12345678-1234-1234-1234-123456789abc'
  const graph = buildRunnerTopology(
    [
      runtime([
        resource(`/private/${id}.qcow2`),
        resource(`dead-domain-${id}`, { kind: 'workspace', managed: false }),
        resource('/private/cloud-init-seed.iso', {
          workspace: ws('a'),
          kind: 'file',
          managed: false,
        }),
        resource('/private/overlay.qcow2', { workspace: ws('a') }),
      ]),
    ],
    [],
  )
  expect(graph.nodes.map((n) => n.label)).toEqual(
    expect.arrayContaining([
      'Disk 12345678',
      'Unlinked workspace 12345678',
      'Workspace a · Boot seed',
      'Workspace a · Workspace disk',
    ]),
  )
  expect(graph.nodes.find((n) => n.label.endsWith('Boot seed'))?.resource).toMatchObject({
    kind: 'file',
    managed: false,
  })
})

it('keeps supplied generations and domain workspaces when their runtime snapshot is absent', () => {
  const graph = buildRunnerTopology([runtime([], 'docker')], [generation()])
  expect(graph.nodes).toHaveLength(2)
  expect(graph.nodes.every((node) => node.runtime === 'qemu')).toBe(true)
  expect(graph.edges).toHaveLength(1)
  expect(graph.edges[0]?.kind).toBe('usage')
})
it('never lets a tombstone take an active physical reference or create domain edges', () => {
  const graph = buildRunnerTopology(
    [runtime([resource('base-alias', { kind: 'image' })])],
    [generation({ id: 'deleted', status: 'deleted', name: 'Deleted image' }), generation()],
  )
  expect(graph.nodes.find((node) => node.resource)?.generation?.id).toBe('image')
  const tombstone = graph.nodes.find((node) => node.generation?.id === 'deleted')!
  expect(tombstone.resource).toBeUndefined()
  expect(graph.edges.some((edge) => edge.from === tombstone.key || edge.to === tombstone.key)).toBe(
    false,
  )
})

it('aligns shared dependency columns with upstream consumers rather than image names', () => {
  const graph = buildRunnerTopology(
    [
      runtime([
        resource('a', { kind: 'workspace', workspace: ws('a'), dependencies: ['disk-a'] }),
        resource('b', { kind: 'workspace', workspace: ws('b'), dependencies: ['disk-b'] }),
        resource('disk-b', { dependencies: ['aaa-image'] }),
        resource('disk-a', { dependencies: ['zzz-image'] }),
        resource('aaa-image', { kind: 'image' }),
        resource('zzz-image', { kind: 'image' }),
      ]),
    ],
    [],
  )
  const imageOrder = graph.nodes.filter((node) => node.column === 2).map((node) => node.label)
  expect(imageOrder).toEqual(['zzz-image', 'aaa-image'])
})

it.each(['volume', 'file', 'build_cache'])(
  'does not promote a %s alias into a physical image without server association',
  (kind) => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', { kind: 'workspace', workspace: ws('a'), dependencies: ['other'] }),
          resource('other', {
            kind,
            aliases: ['base-alias'],
            workspace: kind === 'volume' ? ws('a') : null,
          }),
        ]),
      ],
      [generation()],
    )
    const image = graph.nodes.find((node) => node.generation)!
    expect(image.resource).toBeUndefined()
    expect(
      graph.nodes.find((node) => node.resource?.physical_id === 'other')?.generation,
    ).toBeUndefined()
    expect(graph.edges.some((edge) => edge.to === image.key && edge.kind === 'usage')).toBe(true)
    if (kind === 'volume')
      expect(graph.nodes.find((node) => node.resource?.physical_id === 'other')?.label).toContain(
        'Workspace volume',
      )
  },
)
it('permits the supported legacy QEMU disk reference, but not an unassociated modern artifact', () => {
  const runtimeEvidence = runtime([resource('base-alias')])
  expect(
    buildRunnerTopology([runtimeEvidence], [generation({ is_legacy: true })]).nodes.find(
      (node) => node.generation,
    )?.resource?.physical_id,
  ).toBe('base-alias')
  expect(
    buildRunnerTopology([runtimeEvidence], [generation({ is_legacy: false })]).nodes.find(
      (node) => node.generation,
    )?.resource,
  ).toBeUndefined()
})
