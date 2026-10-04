import { describe, expect, it } from 'vitest'
import { buildRunnerStorageChart, storageRingPath } from './runnerStorageChart'
import { buildRunnerTopology, projectRunnerTopology } from './runnerTopology'
import type {
  StorageGeneration,
  StorageResource,
  StorageRuntime,
  StorageWorkspace,
} from '@/types/runnerStorage'

const workspace = (id = 'ws', pin?: string): StorageWorkspace => ({
  id,
  name: id,
  owner_id: null,
  owner_label: 'Owner',
  status: 'ready',
  last_activity_at: null,
  base_image_instance_id: pin,
})
const file = (
  physical_id: string,
  allocated_bytes = 100,
  overrides: Partial<StorageResource> = {},
): StorageResource => ({
  physical_id,
  kind: 'disk',
  managed: true,
  state: 'present',
  allocated_bytes,
  filesystem_id: 'root',
  file_identity: physical_id,
  logical_bytes: 9000,
  virtual_bytes: 20000,
  shared_bytes: null,
  reclaimable_bytes: null,
  aliases: [],
  dependencies: [],
  image_id: null,
  workspace: null,
  provenance: 'inventory',
  ...overrides,
})
const generation = (
  id = 'base',
  overrides: Partial<StorageGeneration> = {},
): StorageGeneration => ({
  id,
  name: id,
  owner_label: 'Owner',
  runtime_type: 'qemu',
  definition_id: null,
  definition_name: null,
  build_job_id: null,
  generation: 1,
  status: 'ready',
  assignment_status: null,
  runner_ref: id,
  size_bytes: 999999,
  size_source: 'logical',
  origin_type: 'definition_build',
  revision_id: null,
  is_legacy: false,
  is_current: true,
  is_pending: false,
  observed_state: 'present',
  dependencies: [],
  ...overrides,
})
const filesystem = (
  filesystem_id = 'root',
  capacity_bytes = 1000,
  used_bytes = 700,
  available_bytes = 200,
) => ({
  filesystem_id,
  path: '/',
  capacity_bytes,
  used_bytes,
  available_bytes,
})
const runtime = (
  resources: StorageResource[],
  overrides: Partial<StorageRuntime> = {},
): StorageRuntime => ({
  runtime_type: 'qemu',
  fresh: true,
  snapshot_id: 1,
  collected_at: null,
  received_at: null,
  filesystems: [filesystem()],
  resources,
  diagnostics: null,
  ...overrides,
})
function fixture() {
  const ws = workspace()
  return runtime([
    file('base', 100, { kind: 'image', image_id: 'base' }),
    file('vm', 0, { kind: 'workspace', workspace: ws, dependencies: ['overlay', 'seed'] }),
    file('overlay', 200, { workspace: ws, dependencies: ['base'] }),
    file('seed', 50, { kind: 'file', workspace: ws }),
  ])
}
function chart(runtimes: StorageRuntime[], generations = [generation()]) {
  // Use the actual presentation pipeline, retaining the authoritative resource references.
  const topology = projectRunnerTopology(buildRunnerTopology(runtimes, generations))
  return { topology, result: buildRunnerStorageChart(runtimes, topology) }
}

describe('runner storage accounting', () => {
  it('partitions actual image, overlay and boot blocks; reserved space belongs to Other', () => {
    const scan = fixture()
    const { topology, result } = chart([scan])
    expect(result).toMatchObject({
      capacity: 1000,
      free: 200,
      other: 450,
      filesystemCount: 1,
      unknownCount: 0,
      unavailable: null,
    })
    expect(result.groups).toHaveLength(1)
    expect(result.groups[0]!.bytes).toBe(350)
    expect(result.groups[0]!.children.map((child) => child.bytes)).toEqual([100, 250])
    const vm = topology.nodes.find((node) => node.workspace?.id === 'ws')!
    expect(vm.storage!.resources).toContain(scan.resources[2])
    expect(result.groups[0]!.children[1]!.node).toBe(vm)
    expect(result.groups[0]!.node!.resource).toBe(scan.resources[0])
  })
  it('counts same-owner hardlinks once', () => {
    const scan = fixture()
    scan.resources.push(
      file('alias', 200, {
        file_identity: 'overlay',
        workspace: workspace(),
        dependencies: ['base'],
      }),
    )
    expect(chart([scan]).result.groups[0]!.bytes).toBe(350)
  })
  it.each(['conflicting', 'unowned'])('keeps %s hardlink aliases in Other', (mode) => {
    const scan = fixture()
    scan.resources.push(
      file('alias', 200, {
        file_identity: 'overlay',
        ...(mode === 'conflicting'
          ? { workspace: workspace('other'), dependencies: ['base'] }
          : {}),
      }),
    )
    const { result } = chart([scan])
    expect(result.unavailable).toBeNull()
    expect(result.groups[0]!.bytes).toBe(150)
    expect(result.other).toBe(650)
  })
  it('deduplicates root filesystem identities, not coincidentally equal counters', () => {
    const scan = fixture()
    scan.filesystems.push({ ...filesystem(), path: '/images' })
    expect(chart([scan]).result.capacity).toBe(1000)
    scan.filesystems[1]!.filesystem_id = 'second'
    expect(chart([scan]).result).toMatchObject({
      capacity: 2000,
      free: 400,
      filesystemCount: 2,
      other: 1250,
    })
  })
  it.each(['capacity_bytes', 'used_bytes', 'available_bytes'] as const)(
    'rejects duplicate filesystem identities with differing valid %s',
    (metric) => {
      const scan = fixture()
      const duplicate = { ...filesystem(), path: '/images' }
      duplicate[metric] += 1
      scan.filesystems.push(duplicate)
      const { result } = chart([scan])
      expect(result.unavailable).toMatch(/Filesystem measurements changed/)
      expect(result.groups).toEqual([])
    },
  )
  it.each(['conflicting', 'unowned'])(
    'keeps a known image allocation with an unknown %s hardlink alias in Other',
    (mode) => {
      const scan = fixture()
      scan.resources.push(
        file('image-alias', 0, {
          file_identity: 'base',
          allocated_bytes: null,
          ...(mode === 'conflicting' ? { kind: 'image', image_id: 'other' } : {}),
        }),
      )
      const { result } = chart([scan])
      expect(result).toMatchObject({ unknownCount: 1, other: 550, unavailable: null })
      const base = result.groups.find((group) => group.label === 'base')!
      expect(base.bytes).toBe(250)
      expect(base.children.find((child) => child.node?.generation?.id === 'base')).toMatchObject({
        bytes: 0,
        unknown: true,
      })
      expect(result.groups.reduce((sum, group) => sum + group.bytes, 0)).toBe(250)
    },
  )
  it('does not replace an unresolved physical backing with a logical base pin', () => {
    const scan = fixture()
    scan.resources[1]!.workspace = workspace('ws', 'base')
    scan.resources[2]!.dependencies = ['missing-backing']
    const { topology, result } = chart([scan])
    expect(topology.nodes.some((node) => node.unresolved)).toBe(true)
    expect(result).toMatchObject({ other: 700, unavailable: null })
    expect(result.groups[0]!.bytes).toBe(100)
    expect(result.groups[0]!.children.some((child) => child.node?.workspace)).toBe(false)
  })
  it('retains measured zero image and workspace allocations without calling them unknown', () => {
    const scan = fixture()
    for (const resource of scan.resources) resource.allocated_bytes = 0
    const { result } = chart([scan])
    expect(result).toMatchObject({ unknownCount: 0, other: 800, unavailable: null })
    expect(result.groups).toHaveLength(1)
    expect(result.groups[0]).toMatchObject({
      bytes: 0,
      children: [
        { bytes: 0, unknown: false },
        { bytes: 0, unknown: false },
      ],
    })
  })
  it('validates each filesystem instead of letting spare usage on another mask overflow', () => {
    const scan = fixture()
    scan.filesystems = [filesystem('root', 1000, 300, 200), filesystem('second', 1000, 900, 50)]
    expect(chart([scan]).result.unavailable).toMatch(/exceed filesystem usage/)
  })
  it('uses the nearest physical image, not its transitive ancestor', () => {
    const scan = fixture()
    scan.resources[0]!.dependencies = ['ancestor']
    scan.resources.push(file('ancestor', 70, { kind: 'image', image_id: 'ancestor' }))
    const { result } = chart([scan], [generation(), generation('ancestor')])
    expect(result.groups.map((group) => [group.label, group.bytes])).toEqual([
      ['base', 350],
      ['ancestor', 70],
    ])
  })
  it('uses an explicit logical base pin when physical evidence is absent, not usage edges', () => {
    const scan = fixture()
    scan.resources[2]!.dependencies = []
    const gen = generation('base', { dependencies: [workspace()] })
    expect(chart([scan], [gen]).result.groups[0]!.bytes).toBe(100)
    scan.resources[1]!.workspace = workspace('ws', 'base')
    expect(chart([scan], [gen]).result.groups[0]!.bytes).toBe(350)
  })
  it('does not guess an ancestor behind an unidentified direct backing image', () => {
    const scan = fixture()
    scan.resources[2]!.dependencies = ['unidentified']
    scan.resources.push(file('unidentified', 30, { kind: 'image', dependencies: ['base'] }))
    const { result } = chart([scan])
    expect(result.groups.find((group) => group.label === 'base')!.bytes).toBe(100)
    expect(result.groups.find((group) => group.label === 'unidentified')!.bytes).toBe(280)
  })
  it('terminates cyclic storage paths and leaves shared workspace files unassigned', () => {
    const scan = fixture()
    scan.resources[2]!.dependencies.push('cycle')
    scan.resources.push(file('cycle', 20, { dependencies: ['overlay'], workspace: workspace() }))
    expect(chart([scan]).result.groups[0]!.bytes).toBe(370)
    scan.resources.push(
      file('other-vm', 0, {
        kind: 'workspace',
        workspace: workspace('other'),
        dependencies: ['overlay'],
      }),
    )
    expect(chart([scan]).result.groups[0]!.bytes).toBe(150)
  })
  it('reports partial unknown allocations without inventing image sizes or attributing orphans', () => {
    const scan = fixture()
    scan.resources[0]!.allocated_bytes = null
    scan.resources.push(file('orphan', 40), file('unknown', 0, { allocated_bytes: null }))
    const { result } = chart([scan])
    expect(result).toMatchObject({ unknownCount: 2, other: 550, unavailable: null })
    expect(result.groups[0]!.bytes).toBe(250)
    const absent = chart([runtime([])], [generation()]).result
    expect(absent).toMatchObject({ unknownCount: 1, other: 800, unavailable: null })
    expect(absent.groups).toHaveLength(1)
    expect(absent.groups[0]).toMatchObject({ bytes: 0, children: [{ bytes: 0, unknown: true }] })
  })
  it('excludes Docker counters and resources but preserves QEMU staleness', () => {
    const scan = fixture()
    scan.fresh = false
    const docker = runtime([file('docker', 100000)], {
      runtime_type: 'docker',
      fresh: false,
      filesystems: [],
    })
    expect(chart([scan, docker]).result).toMatchObject({ capacity: 1000, stale: true, other: 450 })
    expect(chart([docker]).result.unavailable).toMatch(/No QEMU/)
  })
  it.each(['filesystem', 'file_identity', 'filesystem_id'] as const)(
    'requires %s identity rather than guessing from paths',
    (field) => {
      const scan = fixture()
      if (field === 'filesystem') delete scan.filesystems[0]!.filesystem_id
      else delete scan.resources[0]![field]
      expect(chart([scan]).result.unavailable).toMatch(/Refresh inventory/)
    },
  )
  it('requires filesystem measurements and rejects unknown filesystem references', () => {
    const scan = fixture()
    scan.filesystems = []
    expect(chart([scan]).result.unavailable).toMatch(/Filesystem measurements unavailable/)
    scan.filesystems = [filesystem()]
    scan.resources[0]!.filesystem_id = 'missing'
    expect(chart([scan]).result.unavailable).toMatch(/filesystem is missing/)
  })
  it.each([NaN, Infinity, -1, 1.5])('rejects invalid filesystem counters %s', (value) => {
    for (const metric of ['capacity_bytes', 'used_bytes', 'available_bytes'] as const) {
      const scan = fixture()
      scan.filesystems[0]![metric] = value
      expect(chart([scan]).result.unavailable).toMatch(/incomplete or inconsistent/)
    }
  })
  it.each([NaN, Infinity, -1, 1.5])('treats invalid file allocations %s as unmeasured', (value) => {
    const scan = fixture()
    scan.resources[0]!.allocated_bytes = value
    expect(chart([scan]).result).toMatchObject({ unknownCount: 1, unavailable: null })
    expect(chart([scan]).result.groups[0]!.bytes).toBe(250)
  })
  it('rejects impossible counters and hardlinks with changing allocation measurements', () => {
    const scan = fixture()
    scan.filesystems[0]!.available_bytes = 400
    expect(chart([scan]).result.unavailable).toMatch(/inconsistent/)
    scan.filesystems = [filesystem()]
    scan.resources.push(file('alias', 201, { file_identity: 'overlay', workspace: workspace() }))
    expect(chart([scan]).result.unavailable).toMatch(/changed during inventory/)
  })
})

describe('storageRingPath', () => {
  it('uses two finite arcs on each boundary for a complete annulus', () => {
    const path = storageRingPath(70, 107, 0, 360)
    expect(path).not.toMatch(/NaN|Infinity/)
    expect(path.match(/A 107 107/g)).toHaveLength(2)
    expect(path.match(/A 70 70/g)).toHaveLength(2)
    expect(path).toContain('160.0000 53.0000')
    expect(path).toContain('160.0000 267.0000')
    expect(storageRingPath(70, 107, 0, 720)).toBe(path)
    expect(storageRingPath(70, 107, 0, 0)).toBe('')
    expect(storageRingPath(70, 107, 0, -1)).toBe('')
  })
})
