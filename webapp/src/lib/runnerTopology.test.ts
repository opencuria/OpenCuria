import { describe, expect, it } from 'vitest'
import { buildRunnerTopology, projectRunnerTopology, topologyPath } from './runnerTopology'
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

describe('collapsed runner topology', () => {
  const key = (kind: string, id: string, type = 'qemu') => JSON.stringify([type, kind, id])
  it('collapses disk, boot seed, volume and orphan owned files without inflating image or capacity', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('vm', {
            kind: 'workspace',
            workspace: ws('a'),
            state: 'running',
            dependencies: ['disk', 'seed.iso', 'volume'],
          }),
          resource('disk', {
            allocated_bytes: 10,
            logical_bytes: 20,
            virtual_bytes: 100,
            dependencies: ['base'],
          }),
          resource('seed.iso', {
            kind: 'file',
            allocated_bytes: 2,
            logical_bytes: 50,
            virtual_bytes: 50,
          }),
          resource('volume', {
            kind: 'volume',
            allocated_bytes: 5,
            logical_bytes: 7,
            virtual_bytes: 30,
          }),
          resource('orphan', { kind: 'file', workspace: ws('a'), allocated_bytes: 3 }),
          resource('base', { kind: 'image', image_id: 'image', allocated_bytes: 500 }),
        ]),
      ],
      [generation()],
    )
    const before = JSON.stringify(graph)
    const projected = projectRunnerTopology(graph)
    expect(JSON.stringify(graph)).toBe(before)
    expect(projected.nodes).toHaveLength(2)
    const workspace = projected.nodes.find((node) => node.workspace)!
    expect(workspace).toMatchObject({
      key: key('workspace', 'a'),
      label: 'Workspace a',
      column: 0,
      resource: { physical_id: 'vm', state: 'running', kind: 'workspace' },
      storage: { allocatedBytes: 20, logicalBytes: 27, virtualBytes: 130, unknownCount: 0 },
    })
    expect(workspace.storage?.resources).toHaveLength(4)
    expect(workspace.resource).toBe(
      graph.nodes.find((node) => node.resource?.physical_id === 'vm')?.resource,
    )
    expect(projected.nodes.find((node) => node.generation)?.resource?.allocated_bytes).toBe(500)
    expect(projected.edges).toEqual([
      { from: key('workspace', 'a'), to: key('generation', 'image'), kind: 'physical' },
    ])
    expect(projected.physicalToPresentation?.get(key('physical', 'disk'))).toBe(workspace.key)
  })
  it('keeps shared, foreign and contradictory owner evidence inspectable', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', {
            kind: 'workspace',
            workspace: ws('a'),
            dependencies: ['shared', 'conflict', 'foreign-shared'],
          }),
          resource('b', { kind: 'workspace', workspace: ws('b'), dependencies: ['shared'] }),
          resource('foreign', {
            kind: 'workspace',
            managed: false,
            dependencies: ['foreign-shared'],
          }),
          resource('shared', { workspace: ws('a'), dependencies: ['child'] }),
          resource('child', { workspace: ws('a') }),
          resource('conflict', { workspace: ws('b') }),
          resource('foreign-shared', { workspace: ws('a') }),
          resource('image-owned', { workspace: ws('a'), image_id: 'other' }),
        ]),
      ],
      [],
    )
    const projected = projectRunnerTopology(graph)
    for (const id of ['shared', 'child', 'conflict', 'foreign-shared', 'image-owned', 'foreign'])
      expect(
        projected.nodes.find((node) => node.key === key('physical', id))?.resource?.physical_id,
      ).toBe(id)
    expect(
      projected.nodes
        .filter((node) => node.storage)
        .every((node) => node.storage?.resources.length === 0),
    ).toBe(true)
  })
  it('preserves ambiguous aliases, dangling evidence and cycles without arbitrary ownership', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', {
            kind: 'workspace',
            workspace: ws('a'),
            dependencies: ['alias', 'cycle-a'],
          }),
          resource('first', { aliases: ['alias'] }),
          resource('second', { aliases: ['alias'] }),
          resource('cycle-a', { dependencies: ['cycle-b', 'missing'] }),
          resource('cycle-b', { dependencies: ['cycle-a'] }),
          resource('unowned-cycle-a', { dependencies: ['unowned-cycle-b'] }),
          resource('unowned-cycle-b', { dependencies: ['unowned-cycle-a'] }),
        ]),
      ],
      [],
    )
    const projected = projectRunnerTopology(graph)
    expect(projected.nodes.filter((node) => node.unresolved)).toHaveLength(2)
    expect(projected.physicalToPresentation?.get(key('physical', 'cycle-b'))).toBe(
      key('workspace', 'a'),
    )
    expect(projected.edges.some((edge) => edge.from === edge.to)).toBe(false)
    expect(
      projected.nodes.find((node) => node.key === key('physical', 'unowned-cycle-a')),
    ).toBeDefined()
    expect(projected.nodes.find((node) => node.key === key('physical', 'first'))).toBeDefined()
  })
  it('uses stable domain keys before and after inventory arrives, scoped by runtime', () => {
    const absent = projectRunnerTopology(buildRunnerTopology([], [generation()]))
    const present = projectRunnerTopology(
      buildRunnerTopology(
        [
          runtime([
            resource('vm', { kind: 'workspace', workspace: ws('a') }),
            resource('base-alias', { kind: 'image' }),
          ]),
        ],
        [generation()],
      ),
    )
    expect(present.nodes.map((node) => node.key).sort()).toEqual(
      absent.nodes.map((node) => node.key).sort(),
    )
    const scoped = projectRunnerTopology(
      buildRunnerTopology(
        [
          runtime([resource('same', { workspace: ws('a') })]),
          runtime([resource('same', { workspace: ws('a') })], 'docker'),
        ],
        [],
      ),
    )
    expect(scoped.nodes.map((node) => node.key).sort()).toEqual(
      [key('workspace', 'a'), key('workspace', 'a', 'docker')].sort(),
    )
    expect(scoped.nodes.every((node) => !node.resource)).toBe(true)
  })
  it('never treats disk state as VM state and discloses partial allocated totals', () => {
    const projected = projectRunnerTopology(
      buildRunnerTopology(
        [
          runtime([
            resource('disk', {
              workspace: ws('a'),
              state: 'running',
              allocated_bytes: 10,
              logical_bytes: 20,
              virtual_bytes: 100,
            }),
            resource('unknown', {
              workspace: ws('a'),
              allocated_bytes: null,
              logical_bytes: null,
              virtual_bytes: null,
            }),
          ]),
        ],
        [],
      ),
    )
    expect(projected.nodes[0]).toMatchObject({
      storage: { allocatedBytes: 10, logicalBytes: null, virtualBytes: null, unknownCount: 1 },
    })
    expect(projected.nodes[0]?.resource).toBeUndefined()
    expect(projected.nodes[0]?.workspace?.observed_state).toBe('unknown')
    const empty = projectRunnerTopology(buildRunnerTopology([], [generation()])).nodes.find(
      (node) => node.workspace,
    )
    expect(empty?.storage).toEqual({
      resources: [],
      allocatedBytes: null,
      logicalBytes: null,
      virtualBytes: null,
      unknownCount: 0,
    })
  })
  it('prefers physical evidence over remapped usage fallback and paths do not flood siblings', () => {
    const graph = buildRunnerTopology(
      [
        runtime([
          resource('a', {
            kind: 'workspace',
            workspace: ws('a'),
            dependencies: ['disk-a', 'seed'],
          }),
          resource('b', { kind: 'workspace', workspace: ws('b'), dependencies: ['disk-b'] }),
          resource('disk-a', { dependencies: ['base'] }),
          resource('seed', { kind: 'file', dependencies: ['base'] }),
          resource('disk-b', { dependencies: ['base'] }),
          resource('base', { kind: 'image', image_id: 'image' }),
        ]),
      ],
      [generation()],
    )
    graph.edges.push({ from: key('physical', 'a'), to: key('physical', 'base'), kind: 'usage' })
    const projected = projectRunnerTopology(graph)
    expect(projected.edges.filter((edge) => edge.from === key('workspace', 'a'))).toHaveLength(1)
    expect(topologyPath([key('workspace', 'a')], projected.edges)).toEqual(
      new Set([key('workspace', 'a'), key('generation', 'image')]),
    )
  })
})

it('reports missing optional snapshot metrics as unknown, never NaN', () => {
  const disk = resource('disk', { workspace: ws('a') })
  // Older/incomplete snapshot payloads can omit metrics entirely.
  Reflect.deleteProperty(disk, 'allocated_bytes')
  Reflect.deleteProperty(disk, 'logical_bytes')
  Reflect.deleteProperty(disk, 'virtual_bytes')
  const projected = projectRunnerTopology(buildRunnerTopology([runtime([disk])], []))
  expect(projected.nodes[0]?.storage).toMatchObject({
    allocatedBytes: null,
    logicalBytes: null,
    virtualBytes: null,
    unknownCount: 1,
  })
})

it.each(['workspace', 'container'])(
  'treats %s image_id as usage and prioritizes domain workspace metadata',
  (kind) => {
    const domain = { ...ws('a'), observed_state: 'running', base_image_instance_id: 'pin' }
    const graph = buildRunnerTopology(
      [
        runtime(
          [
            resource('disk', { workspace: ws('a') }),
            resource('domain', {
              kind,
              workspace: domain,
              image_id: 'image',
              state: 'running',
              dependencies: ['disk'],
            }),
          ],
          'docker',
        ),
      ],
      [],
    )
    // Force disk-first input to exercise projection independently of graph ordering.
    graph.nodes.reverse()
    const projected = projectRunnerTopology(graph)
    expect(projected.nodes).toHaveLength(1)
    expect(projected.nodes[0]?.workspace).toBe(domain)
    expect(projected.nodes[0]?.resource?.physical_id).toBe('domain')
    expect(projected.nodes[0]?.storage?.allocatedBytes).toBe(1024)
  },
)

it('keeps explicit foreign references and their owned descendants uncollapsed', () => {
  const projected = projectRunnerTopology(
    buildRunnerTopology(
      [
        runtime([
          resource('domain', { kind: 'workspace', workspace: ws('a'), dependencies: ['foreign'] }),
          resource('foreign', {
            kind: 'foreign_reference',
            workspace: ws('a'),
            dependencies: ['disk'],
          }),
          resource('disk', { workspace: ws('a') }),
        ]),
      ],
      [],
    ),
  )
  expect(projected.nodes.find((node) => node.storage)?.storage?.resources).toEqual([])
  expect(
    projected.nodes
      .filter((node) => !node.storage)
      .map((node) => node.resource?.physical_id)
      .sort(),
  ).toEqual(['disk', 'foreign'])
})

it('deduplicates allocated hardlinks while retaining all members and runtime/filesystem scope', () => {
  const projected = projectRunnerTopology(
    buildRunnerTopology(
      [
        runtime([
          resource('one', {
            workspace: ws('a'),
            filesystem_id: 'fs',
            file_identity: 'inode',
            allocated_bytes: null,
          }),
          resource('two', {
            workspace: ws('a'),
            filesystem_id: 'fs',
            file_identity: 'inode',
            allocated_bytes: 10,
          }),
          resource('other-fs', {
            workspace: ws('a'),
            filesystem_id: 'fs2',
            file_identity: 'inode',
            allocated_bytes: 20,
          }),
          resource('no-identity', { workspace: ws('a'), allocated_bytes: 5 }),
          resource('unknown', { workspace: ws('a'), allocated_bytes: null }),
        ]),
        runtime(
          [
            resource('one', {
              workspace: ws('a'),
              filesystem_id: 'fs',
              file_identity: 'inode',
              allocated_bytes: 30,
            }),
          ],
          'docker',
        ),
      ],
      [],
    ),
  )
  expect(projected.nodes.find((node) => node.runtime === 'qemu')?.storage).toMatchObject({
    allocatedBytes: 35,
    unknownCount: 1,
  })
  expect(projected.nodes.find((node) => node.runtime === 'qemu')?.storage?.resources).toHaveLength(
    5,
  )
  expect(projected.nodes.find((node) => node.runtime === 'docker')?.storage?.allocatedBytes).toBe(
    30,
  )
})

it('keeps contradictory hardlink ownership inspectable instead of charging both workspaces', () => {
  const projected = projectRunnerTopology(
    buildRunnerTopology(
      [
        runtime([
          resource('one', {
            workspace: ws('a'),
            filesystem_id: 'fs',
            file_identity: 'inode',
            dependencies: ['child'],
          }),
          resource('two', { workspace: ws('b'), filesystem_id: 'fs', file_identity: 'inode' }),
          resource('child', { workspace: ws('a') }),
        ]),
      ],
      [],
    ),
  )
  expect(
    projected.nodes
      .filter((node) => node.storage)
      .every((node) => node.storage?.allocatedBytes === null),
  ).toBe(true)
  expect(projected.nodes.filter((node) => !node.storage)).toHaveLength(3)
})

it('keeps an unowned root hardlink and its owned peer uncertain without inventing consumers', () => {
  const graph = buildRunnerTopology(
    [
      runtime([
        resource('domain', { kind: 'workspace', workspace: ws('a'), dependencies: ['overlay'] }),
        resource('overlay', { workspace: ws('a'), filesystem_id: 'fs', file_identity: 'inode' }),
        resource('unowned-alias', { filesystem_id: 'fs', file_identity: 'inode' }),
      ]),
    ],
    [],
  )
  const before = JSON.stringify(graph)
  const projected = projectRunnerTopology(graph)
  expect(JSON.stringify(graph)).toBe(before)
  expect(projected.nodes.find((node) => node.storage)?.storage?.resources).toEqual([])
  for (const id of ['overlay', 'unowned-alias']) {
    const key = JSON.stringify(['qemu', 'physical', id])
    expect(projected.physicalToPresentation?.get(key)).toBe(key)
    expect(projected.nodes.find((node) => node.key === key)?.resource?.physical_id).toBe(id)
  }
  expect(projected.edges).toHaveLength(1)
})

it('collapses hardlink aliases with unique actual workspace consumers even without explicit owners', () => {
  const projected = projectRunnerTopology(
    buildRunnerTopology(
      [
        runtime([
          resource('domain', {
            kind: 'workspace',
            workspace: ws('a'),
            dependencies: ['one', 'two'],
          }),
          resource('one', { filesystem_id: 'fs', file_identity: 'inode', allocated_bytes: 10 }),
          resource('two', { filesystem_id: 'fs', file_identity: 'inode', allocated_bytes: 10 }),
        ]),
      ],
      [],
    ),
  )
  expect(projected.nodes).toHaveLength(1)
  expect(projected.nodes[0]?.storage).toMatchObject({ allocatedBytes: 10, unknownCount: 0 })
  expect(projected.nodes[0]?.storage?.resources).toHaveLength(2)
})

it('counts same-owner hardlink capacity and file size once, excluding boot ISO capacity', () => {
  const disk = resource('one', {
    workspace: ws('a'),
    filesystem_id: 'fs',
    file_identity: 'inode',
    allocated_bytes: 10,
    logical_bytes: 20,
    virtual_bytes: 100,
  })
  const projected = projectRunnerTopology(
    buildRunnerTopology(
      [
        runtime([
          disk,
          { ...disk, physical_id: 'two' },
          resource('boot.iso', {
            workspace: ws('a'),
            allocated_bytes: 2,
            logical_bytes: 900,
            virtual_bytes: 900,
          }),
        ]),
      ],
      [],
    ),
  )
  expect(projected.nodes[0]?.storage).toMatchObject({
    allocatedBytes: 12,
    logicalBytes: 20,
    virtualBytes: 100,
    unknownCount: 0,
  })
  expect(projected.nodes[0]?.storage?.resources).toHaveLength(3)
})

it('excludes conflicting allocated identities from known subtotal regardless of alias order', () => {
  const disk = resource('one', {
    workspace: ws('a'),
    filesystem_id: 'fs',
    file_identity: 'inode',
    allocated_bytes: 10,
    logical_bytes: 20,
    virtual_bytes: 100,
  })
  const aliases = [
    disk,
    { ...disk, physical_id: 'two', allocated_bytes: 15 },
    { ...disk, physical_id: 'three', allocated_bytes: null },
  ]
  for (const resources of [aliases, [...aliases].reverse()]) {
    const projected = projectRunnerTopology(
      buildRunnerTopology(
        [
          runtime([
            ...resources,
            resource('known', {
              workspace: ws('a'),
              allocated_bytes: 2,
              logical_bytes: 5,
              virtual_bytes: 10,
            }),
          ]),
        ],
        [],
      ),
    )
    expect(projected.nodes[0]?.storage).toMatchObject({
      allocatedBytes: 2,
      logicalBytes: 25,
      virtualBytes: 110,
      unknownCount: 1,
    })
    expect(projected.nodes[0]?.storage?.resources).toHaveLength(4)
  }
})

it.each(['logical_bytes', 'virtual_bytes'] as const)(
  'requires complete consistent hardlink %s measurements',
  (metric) => {
    const disk = resource('one', {
      workspace: ws('a'),
      filesystem_id: 'fs',
      file_identity: 'inode',
      allocated_bytes: 10,
      logical_bytes: 20,
      virtual_bytes: 100,
    })
    const field = metric === 'logical_bytes' ? 'logicalBytes' : 'virtualBytes'
    for (const value of [null, disk[metric]! + 1]) {
      const projected = projectRunnerTopology(
        buildRunnerTopology(
          [runtime([disk, { ...disk, physical_id: 'two', [metric]: value }])],
          [],
        ),
      )
      expect(projected.nodes[0]?.storage?.[field]).toBeNull()
      expect(projected.nodes[0]?.storage?.allocatedBytes).toBe(10)
    }
  },
)
