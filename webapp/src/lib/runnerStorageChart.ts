import type { RunnerTopology, RunnerTopologyNode } from './runnerTopology'
import type { StorageResource, StorageRuntime } from '@/types/runnerStorage'

export interface StorageSlice {
  key: string
  label: string
  bytes: number
  node?: RunnerTopologyNode
  unknown?: boolean
}
export interface StorageGroup extends StorageSlice {
  children: StorageSlice[]
}
export interface RunnerStorageChart {
  capacity: number
  free: number
  other: number
  filesystemCount: number
  groups: StorageGroup[]
  unknownCount: number
  stale: boolean
  unavailable: string | null
}
const validBytes = (value: number | null | undefined): value is number =>
  value != null && Number.isSafeInteger(value) && value >= 0

/** Partition QEMU file allocations, never guest capacity or overlapping image sizes. */
export function buildRunnerStorageChart(
  runtimes: StorageRuntime[],
  graph: RunnerTopology,
): RunnerStorageChart {
  const result: RunnerStorageChart = {
    capacity: 0,
    free: 0,
    other: 0,
    filesystemCount: 0,
    groups: [],
    unknownCount: 0,
    stale: false,
    unavailable: null,
  }
  const scans = runtimes.filter((runtime) => runtime.runtime_type === 'qemu')
  result.stale = scans.some((runtime) => !runtime.fresh)
  function unavailable(message: string) {
    return { ...result, groups: [], unavailable: message }
  }
  if (!scans.length) return unavailable('No QEMU storage inventory available.')
  const filesystems = new Map<string, StorageRuntime['filesystems'][number]>()
  for (const scan of scans) {
    if (!scan.filesystems.length)
      return unavailable('Filesystem measurements unavailable. Refresh inventory.')
    for (const fs of scan.filesystems) {
      if (!fs.filesystem_id)
        return unavailable(
          'Refresh inventory to measure distinct filesystems and file allocations.',
        )
      if (
        !validBytes(fs.capacity_bytes) ||
        !fs.capacity_bytes ||
        !validBytes(fs.used_bytes) ||
        !validBytes(fs.available_bytes) ||
        fs.used_bytes + fs.available_bytes > fs.capacity_bytes
      )
        return unavailable(
          'Filesystem measurements are incomplete or inconsistent. Refresh inventory.',
        )
      const previous = filesystems.get(fs.filesystem_id)
      if (
        previous &&
        (previous.capacity_bytes !== fs.capacity_bytes ||
          previous.used_bytes !== fs.used_bytes ||
          previous.available_bytes !== fs.available_bytes)
      )
        return unavailable('Filesystem measurements changed during inventory. Refresh inventory.')
      if (!previous) filesystems.set(fs.filesystem_id, fs)
    }
  }
  result.filesystemCount = filesystems.size
  for (const fs of filesystems.values()) {
    result.capacity += fs.capacity_bytes!
    result.free += fs.available_bytes!
  }
  const images = graph.nodes.filter(
    (node) =>
      node.runtime === 'qemu' &&
      node.column === 2 &&
      node.generation?.status !== 'deleted' &&
      node.resource?.kind !== 'build_cache',
  )
  const groups = new Map(
    images.map((node) => [
      node.key,
      {
        key: node.key,
        label: node.label,
        bytes: 0,
        node,
        children: [],
      } as StorageGroup,
    ]),
  )
  const imageKeys = new Set(groups.keys())
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]))
  function baseImage(node: RunnerTopologyNode): string | undefined {
    const found = new Set<string>()
    let unresolved = false
    const visited = new Set([node.key])
    const pending = [node.key]
    while (pending.length) {
      const key = pending.pop()!
      for (const edge of graph.edges) {
        if (edge.from !== key || edge.kind !== 'physical' || visited.has(edge.to)) continue
        visited.add(edge.to)
        if (
          !byKey.get(edge.to) ||
          byKey.get(edge.to)?.unresolved ||
          byKey.get(edge.to)?.resource?.kind === 'unknown'
        )
          unresolved = true
        if (imageKeys.has(edge.to)) found.add(edge.to)
        // An unidentified backing image is not evidence of its ancestor being the direct base.
        else if (byKey.get(edge.to)?.resource?.kind !== 'image') pending.push(edge.to)
      }
    }
    if (unresolved) return undefined
    if (found.size) return found.size === 1 ? [...found][0] : undefined
    const pin = node.workspace?.base_image_instance_id
    return images.find((image) => image.generation?.id === pin)?.key
  }
  const owners = new Map<StorageResource, { node: RunnerTopologyNode; groupKey: string }>()
  const slices = new Map<string, StorageSlice>()
  function addSlice(node: RunnerTopologyNode, groupKey: string, resources: StorageResource[]) {
    const slice: StorageSlice = {
      key: node.key,
      label: node.label,
      bytes: 0,
      node,
      unknown: !resources.length,
    }
    if (!resources.length) result.unknownCount++
    slices.set(node.key, slice)
    groups.get(groupKey)!.children.push(slice)
    for (const resource of resources) owners.set(resource, { node, groupKey })
  }
  for (const image of images) addSlice(image, image.key, image.resource ? [image.resource] : [])
  for (const node of graph.nodes.filter((node) => node.runtime === 'qemu' && node.column === 0)) {
    const groupKey = baseImage(node)
    if (groupKey) addSlice(node, groupKey, node.storage?.resources ?? [])
  }
  // Reconcile all hardlink aliases, including unknown measurements, before attributing bytes.
  const files = new Map<string, StorageResource[]>()
  for (const scan of scans)
    for (const resource of scan.resources) {
      if (!['disk', 'image', 'file'].includes(resource.kind)) continue
      if (!validBytes(resource.allocated_bytes)) {
        result.unknownCount++
        const owner = owners.get(resource)
        if (owner) slices.get(owner.node.key)!.unknown = true
      }
      if (!resource.filesystem_id || !resource.file_identity) {
        if (owners.has(resource) && validBytes(resource.allocated_bytes))
          return unavailable('File identities unavailable. Refresh inventory.')
        continue
      }
      if (!filesystems.has(resource.filesystem_id))
        return unavailable(
          'A resource filesystem is missing from the inventory. Refresh inventory.',
        )
      const key = JSON.stringify([resource.filesystem_id, resource.file_identity])
      files.set(key, [...(files.get(key) ?? []), resource])
    }
  const allocatedByFilesystem = new Map<string, number>()
  for (const resources of files.values()) {
    const assignments = resources.map((item) => owners.get(item))
    const owner = assignments[0]
    if (!owner || assignments.some((item) => item?.node.key !== owner.node.key)) {
      for (const assignment of assignments)
        if (assignment) slices.get(assignment.node.key)!.unknown = true
      continue
    }
    const resource = resources.find((item) => validBytes(item.allocated_bytes))
    if (!resource) continue
    if (
      resources.some(
        (other) =>
          validBytes(other.allocated_bytes) && other.allocated_bytes !== resource.allocated_bytes,
      )
    )
      return unavailable(
        'File allocation measurements changed during inventory. Refresh inventory.',
      )
    allocatedByFilesystem.set(
      resource.filesystem_id!,
      (allocatedByFilesystem.get(resource.filesystem_id!) ?? 0) + resource.allocated_bytes!,
    )
    slices.get(owner.node.key)!.bytes += resource.allocated_bytes!
    groups.get(owner.groupKey)!.bytes += resource.allocated_bytes!
  }
  for (const [id, allocated] of allocatedByFilesystem)
    if (allocated > filesystems.get(id)!.used_bytes!)
      return unavailable(
        'File allocations exceed filesystem usage. Shared extents or inconsistent measurements prevent an additive breakdown.',
      )
  result.groups = [...groups.values()].sort(
    (a, b) => b.bytes - a.bytes || a.key.localeCompare(b.key),
  )
  for (const group of result.groups) {
    group.unknown = group.children.some((child) => child.unknown)
    group.children.sort(
      (a, b) =>
        Number(!!b.node?.generation) - Number(!!a.node?.generation) ||
        a.label.localeCompare(b.label),
    )
  }
  result.other =
    result.capacity - result.free - result.groups.reduce((sum, group) => sum + group.bytes, 0)
  return result
}

/** SVG annular sector; full circles use two arcs instead of a degenerate single arc. */
export function storageRingPath(
  inner: number,
  outer: number,
  start: number,
  sweep: number,
): string {
  const point = (radius: number, angle: number) => {
    const radians = ((angle - 90) * Math.PI) / 180
    return `${(160 + radius * Math.cos(radians)).toFixed(4)} ${(160 + radius * Math.sin(radians)).toFixed(4)}`
  }
  const span = Math.min(360, Math.max(0, sweep))
  if (!span) return ''
  if (span >= 359.9999) {
    const mid = start + 180
    return `M ${point(outer, start)} A ${outer} ${outer} 0 1 1 ${point(outer, mid)} A ${outer} ${outer} 0 1 1 ${point(outer, start)} L ${point(inner, start)} A ${inner} ${inner} 0 1 0 ${point(inner, mid)} A ${inner} ${inner} 0 1 0 ${point(inner, start)} Z`
  }
  const large = span > 180 ? 1 : 0
  return `M ${point(outer, start)} A ${outer} ${outer} 0 ${large} 1 ${point(outer, start + span)} L ${point(inner, start + span)} A ${inner} ${inner} 0 ${large} 0 ${point(inner, start)} Z`
}
