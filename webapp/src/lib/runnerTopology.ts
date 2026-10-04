import type {
  StorageGeneration,
  StorageResource,
  StorageRuntime,
  StorageWorkspace,
} from '@/types/runnerStorage'

export interface RunnerTopologyNode {
  key: string
  runtime: string
  label: string
  column: number
  storage?: {
    resources: StorageResource[]
    allocatedBytes: number | null
    logicalBytes: number | null
    virtualBytes: number | null
    unknownCount: number
  }
  resource?: StorageResource
  generation?: StorageGeneration
  workspace?: StorageWorkspace
  unresolved?: boolean
  unassigned?: boolean
}
export interface RunnerTopologyEdge {
  from: string
  to: string
  kind: 'physical' | 'usage'
}
export interface RunnerTopology {
  physicalToPresentation?: Map<string, string>
  nodes: RunnerTopologyNode[]
  edges: RunnerTopologyEdge[]
}
const keyFor = (runtime: string, kind: string, id: string) => JSON.stringify([runtime, kind, id])
function shortLabel(ref: string): string {
  const basename = ref.replace(/\/$/, '').split('/').pop() || ref
  const uuid = basename.match(/([0-9a-f]{8}-[0-9a-f-]{27})(?:\.|$)/i)
  if (uuid)
    return /\.(qcow2|img|raw|vhdx?|iso)$/i.test(basename)
      ? `Disk ${uuid[1]!.slice(0, 8)}`
      : `Unlinked workspace ${uuid[1]!.slice(0, 8)}`
  return basename.slice(0, 48)
}

/** Traverse each direction separately: siblings sharing an image are not a selected path. */
export function topologyPath(keys: Iterable<string>, edges: RunnerTopologyEdge[]): Set<string> {
  const seeds = [...keys]
  const result = new Set(seeds)
  for (const reverse of [false, true]) {
    const visited = new Set(seeds)
    const queue = [...seeds]
    while (queue.length) {
      const key = queue.pop()!
      for (const edge of edges) {
        if ((reverse ? edge.to : edge.from) !== key) continue
        const next = reverse ? edge.from : edge.to
        if (visited.has(next)) continue
        visited.add(next)
        result.add(next)
        queue.push(next)
      }
    }
  }
  return result
}

/** Build an evidence graph; domain usage never masquerades as physical inventory. */
export function buildRunnerTopology(
  runtimes: StorageRuntime[],
  generations: StorageGeneration[],
): RunnerTopology {
  const nodes: RunnerTopologyNode[] = []
  const edges: RunnerTopologyEdge[] = []
  // The caller owns runtime filtering; missing snapshots are not absent generations.
  const images = generations
  const edgeKeys = new Set<string>()
  function connect(from: string, to: string, kind: RunnerTopologyEdge['kind']) {
    const key = JSON.stringify([from, to, kind])
    if (!edgeKeys.has(key)) {
      edgeKeys.add(key)
      edges.push({ from, to, kind })
    }
  }
  for (const runtime of runtimes) {
    const type = runtime.runtime_type
    const lookup = new Map<string, RunnerTopologyNode>()
    for (const resource of runtime.resources) {
      if (lookup.has(resource.physical_id)) continue
      const column = ['workspace', 'container'].includes(resource.kind)
        ? 0
        : ['image', 'build_cache'].includes(resource.kind) ||
            (resource.image_id && !resource.workspace && !resource.dependencies.length)
          ? 2
          : 1
      const node: RunnerTopologyNode = {
        key: keyFor(type, 'physical', resource.physical_id),
        runtime: type,
        label: resource.workspace?.name
          ? column === 1
            ? `${resource.workspace.name} · ${/(?:cloud[-_]?init|seed).*\.iso$/i.test(resource.physical_id.split('/').pop() || '') ? 'Boot seed' : resource.kind === 'volume' ? 'Workspace volume' : resource.kind === 'file' ? 'Workspace file' : 'Workspace disk'}`
            : resource.workspace.name
          : shortLabel(resource.physical_id),
        column,
        resource,
        workspace: resource.workspace ?? undefined,
      }
      nodes.push(node)
      lookup.set(resource.physical_id, node)
    }
    // Exact physical IDs win over aliases; ambiguous aliases are not evidence of a join.
    const aliases = new Map<string, RunnerTopologyNode | null>()
    for (const node of lookup.values())
      for (const alias of node.resource!.aliases) {
        aliases.set(alias, aliases.has(alias) && aliases.get(alias) !== node ? null : node)
      }
    for (const [alias, node] of aliases) if (node && !lookup.has(alias)) lookup.set(alias, node)
    for (const node of nodes.filter((n) => n.runtime === type && n.resource)) {
      for (const dependency of node.resource!.dependencies) {
        let target = lookup.get(dependency)
        if (!target) {
          target = {
            key: keyFor(type, 'physical', dependency),
            runtime: type,
            label: `Unresolved · ${shortLabel(dependency)}`,
            column: 1,
            unresolved: true,
          }
          lookup.set(dependency, target)
          nodes.push(target)
        }
        connect(node.key, target.key, 'physical')
      }
    }
    for (const generation of images.filter(
      (g) => g.runtime_type === type && g.status !== 'deleted',
    )) {
      const candidates = nodes.filter(
        (n) =>
          n.runtime === type &&
          n.resource &&
          !['workspace', 'container'].includes(n.resource.kind) &&
          n.resource.image_id === generation.id,
      )
      const reference = lookup.get(generation.runner_ref)
      const target =
        candidates[0] ||
        (reference?.resource &&
        (reference.resource.kind === 'image' ||
          (type === 'qemu' && generation.is_legacy && reference.resource.kind === 'disk')) &&
        (!reference.resource.image_id || reference.resource.image_id === generation.id)
          ? reference
          : undefined)
      if (target && !target.generation) {
        target.generation = generation
        target.label = generation.name
        target.column = 2
      }
    }
  }
  for (const generation of images) {
    const targets = nodes.filter(
      (n) => n.generation?.id === generation.id && n.runtime === generation.runtime_type,
    )
    if (!targets.length) {
      const node = {
        key: keyFor(generation.runtime_type, 'generation', generation.id),
        runtime: generation.runtime_type,
        label: generation.name,
        column: 2,
        generation,
      }
      nodes.push(node)
      targets.push(node)
    }
    // Tombstones are isolated historical records, never evidence of current dependencies.
    if (generation.status === 'deleted') continue
    for (const workspace of generation.dependencies) {
      let consumers = nodes.filter(
        (n) => n.runtime === generation.runtime_type && n.workspace?.id === workspace.id,
      )
      if (!consumers.length) {
        const node = {
          key: keyFor(generation.runtime_type, 'workspace', workspace.id),
          runtime: generation.runtime_type,
          label: workspace.name,
          column: 0,
          workspace,
        }
        nodes.push(node)
        consumers = [node]
      }
      // Reachability is outgoing only and physical only. A sibling is not a path.
      const pending = consumers.map((n) => n.key)
      const confirmed = new Set(pending)
      while (pending.length) {
        const from = pending.pop()!
        for (const edge of edges)
          if (edge.kind === 'physical' && edge.from === from && !confirmed.has(edge.to)) {
            confirmed.add(edge.to)
            pending.push(edge.to)
          }
      }
      if (!targets.some((n) => confirmed.has(n.key)))
        connect(consumers[0]!.key, targets[0]!.key, 'usage')
    }
  }
  const linked = topologyPath(
    nodes.filter((n) => n.workspace || n.generation).map((n) => n.key),
    edges,
  )
  for (const node of nodes) node.unassigned = !linked.has(node.key)
  return { nodes: orderTopologyNodes(nodes, edges), edges }
}

/**
 * Collapse only exclusive workspace storage, without rewriting physical evidence.
 * Allocated bytes are the known subtotal (null when none are known); unknownCount
 * counts unique storage identities without an allocated size. Logical/virtual
 * capacity requires all disk/volume members to be known and consistent within
 * each identity. Conflicting allocations count as unknown identities. Files (including boot ISOs) are not capacity.
 * Allocated totals deduplicate filesystem/file identities; all member records
 * remain inspectable. Resource objects remain the original inventory/state authority.
 */
export function projectRunnerTopology(graph: RunnerTopology): RunnerTopology {
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]))
  const incoming = new Map<string, string[]>()
  for (const edge of graph.edges) {
    if (edge.kind !== 'physical') continue
    const from = byKey.get(edge.from)
    const to = byKey.get(edge.to)
    if (!from || !to || from.runtime !== to.runtime) continue
    incoming.set(edge.to, [...(incoming.get(edge.to) ?? []), edge.from])
  }
  const isDomain = (node: RunnerTopologyNode) =>
    ['workspace', 'container'].includes(node.resource?.kind ?? '')
  const isImage = (node: RunnerTopologyNode) =>
    !!node.generation ||
    (!isDomain(node) && !!node.resource?.image_id) ||
    node.resource?.kind === 'image'
  const identity = (node: Pick<RunnerTopologyNode, 'key' | 'runtime' | 'resource'>) => {
    const resource = node.resource
    return resource?.file_identity && resource.filesystem_id
      ? JSON.stringify([node.runtime, resource.filesystem_id, resource.file_identity])
      : node.key
  }
  // Hardlink aliases share ownership evidence even without physical dependency
  // edges. Keep conflicting identities inspectable, including their descendants.
  const identities = new Map<string, string[]>()
  for (const node of graph.nodes) {
    if (!node.resource || isDomain(node)) continue
    const id = identity(node)
    identities.set(id, [...(identities.get(id) ?? []), node.key])
  }
  // Propagate ownership to a fixed point so aliases and cycles cannot pick an
  // arbitrary first consumer. Images are boundaries, not workspace storage.
  const owners = new Map<string, Set<string>>()
  const foreign = new Set<string>()
  for (const node of graph.nodes) {
    owners.set(node.key, new Set(node.workspace && !isImage(node) ? [node.workspace.id] : []))
    if (
      isImage(node) ||
      node.unresolved ||
      node.resource?.kind === 'foreign_reference' ||
      (!node.workspace &&
        (['workspace', 'container'].includes(node.resource?.kind ?? '') ||
          !incoming.get(node.key)?.length))
    )
      foreign.add(node.key)
  }
  // Seed uncertain roots from actual physical consumers before adding identity
  // peers: a hardlink alone is not evidence that an unowned alias belongs to a VM.
  for (const peers of identities.values())
    for (const key of peers)
      incoming.set(key, [...(incoming.get(key) ?? []), ...peers.filter((peer) => peer !== key)])
  let changed = true
  while (changed) {
    changed = false
    for (const node of graph.nodes) {
      if (isImage(node)) continue
      const own = owners.get(node.key)!
      for (const source of incoming.get(node.key) ?? []) {
        for (const owner of owners.get(source)!) {
          if (!own.has(owner)) {
            own.add(owner)
            changed = true
          }
        }
        if (foreign.has(source) && !foreign.has(node.key)) {
          foreign.add(node.key)
          changed = true
        }
      }
    }
  }
  const workspaces = new Map<string, RunnerTopologyNode>()
  const workspaceRanks = new Map<string, number>()
  for (const node of graph.nodes) {
    if (!node.workspace) continue
    const key = keyFor(node.runtime, 'workspace', node.workspace.id)
    const rank = isDomain(node) ? 2 : !node.resource ? 1 : 0
    const existing = workspaces.get(key)
    if (existing && rank > workspaceRanks.get(key)!) {
      existing.workspace = node.workspace
      existing.label = node.workspace.name
    }
    workspaceRanks.set(key, Math.max(workspaceRanks.get(key) ?? -1, rank))
    if (!existing)
      workspaces.set(key, {
        key,
        runtime: node.runtime,
        label: node.workspace.name,
        column: 0,
        workspace: node.workspace,
        storage: {
          resources: [],
          allocatedBytes: null,
          logicalBytes: null,
          virtualBytes: null,
          unknownCount: 0,
        },
      })
  }
  const physicalToPresentation = new Map<string, string>()
  const nodes: RunnerTopologyNode[] = []
  for (const node of graph.nodes) {
    let key = node.key
    const own = owners.get(node.key)!
    const owner = [...own][0]
    const exclusive = own.size === 1 && !foreign.has(node.key) && !isImage(node)
    const workspace = owner ? workspaces.get(keyFor(node.runtime, 'workspace', owner)) : undefined
    if (node.generation) {
      key = keyFor(node.runtime, 'generation', node.generation.id)
      nodes.push({ ...node, key })
    } else if (
      workspace &&
      exclusive &&
      (!node.resource ||
        ['workspace', 'container', 'disk', 'file', 'volume'].includes(node.resource.kind))
    ) {
      key = workspace.key
      if (node.resource) {
        if (isDomain(node)) {
          if (!workspace.resource) {
            workspace.resource = node.resource
            workspace.workspace = node.workspace
            workspace.label = node.workspace?.name ?? workspace.label
          }
        } else workspace.storage!.resources.push(node.resource)
      }
    } else nodes.push({ ...node })
    physicalToPresentation.set(node.key, key)
  }
  for (const workspace of workspaces.values()) {
    const storage = workspace.storage!
    const members = new Map<string, StorageResource[]>()
    for (const resource of storage.resources) {
      const id = identity({
        key: keyFor(workspace.runtime, 'physical', resource.physical_id),
        runtime: workspace.runtime,
        resource,
      })
      members.set(id, [...(members.get(id) ?? []), resource])
    }
    function measured(
      resources: StorageResource[],
      metric: 'allocated_bytes' | 'logical_bytes' | 'virtual_bytes',
      requireComplete: boolean,
    ): number | null {
      const values = resources.map((resource) => resource[metric])
      if (requireComplete && values.some((value) => value == null)) return null
      const known = new Set(values.filter((value): value is number => value != null))
      return known.size === 1 ? [...known][0]! : null
    }
    const allocations = [...members.values()].map((resources) =>
      measured(resources, 'allocated_bytes', false),
    )
    const known = allocations.filter((value): value is number => value != null)
    storage.allocatedBytes = known.length ? known.reduce((sum, value) => sum + value, 0) : null
    storage.unknownCount = allocations.length - known.length
    const capacity = [...members.values()]
      .map((resources) =>
        resources.filter(
          (resource) =>
            ['disk', 'volume'].includes(resource.kind) && !/\.iso$/i.test(resource.physical_id),
        ),
      )
      .filter((resources) => resources.length)
    for (const [field, metric] of [
      ['logicalBytes', 'logical_bytes'],
      ['virtualBytes', 'virtual_bytes'],
    ] as const) {
      const values = capacity.map((resources) => measured(resources, metric, true))
      storage[field] =
        values.length && values.every((value) => value != null)
          ? values.reduce<number>((sum, value) => sum + value!, 0)
          : null
    }
    nodes.push(workspace)
  }
  const edgeKeys = new Set<string>()
  const edges: RunnerTopologyEdge[] = []
  for (const edge of graph.edges) {
    const from = physicalToPresentation.get(edge.from) ?? edge.from
    const to = physicalToPresentation.get(edge.to) ?? edge.to
    const key = JSON.stringify([from, to, edge.kind])
    if (from === to || edgeKeys.has(key)) continue
    edgeKeys.add(key)
    edges.push({ from, to, kind: edge.kind })
  }
  const physicalPairs = new Set(
    edges
      .filter((edge) => edge.kind === 'physical')
      .map((edge) => JSON.stringify([edge.from, edge.to])),
  )
  const presentationEdges = edges.filter(
    (edge) => edge.kind === 'physical' || !physicalPairs.has(JSON.stringify([edge.from, edge.to])),
  )
  const linked = topologyPath(
    nodes.filter((node) => node.workspace || node.generation).map((node) => node.key),
    presentationEdges,
  )
  for (const node of nodes) node.unassigned = !linked.has(node.key)
  return {
    nodes: orderTopologyNodes(nodes, presentationEdges),
    edges: presentationEdges,
    physicalToPresentation,
  }
}

/** Align dependencies with their upstream consumers without changing graph evidence. */
export function orderTopologyNodes(
  nodes: RunnerTopologyNode[],
  edges: RunnerTopologyEdge[],
): RunnerTopologyNode[] {
  const consumers = [...nodes]
    .filter((node) => node.column === 0)
    .sort(
      (a, b) =>
        a.runtime.localeCompare(b.runtime) ||
        a.label.localeCompare(b.label) ||
        a.key.localeCompare(b.key),
    )
  const ranks = new Map(consumers.map((node, index) => [node.key, index]))
  const incoming = new Map<string, string[]>()
  for (const edge of edges) incoming.set(edge.to, [...(incoming.get(edge.to) ?? []), edge.from])
  const anchors = new Map<string, number>()
  for (const node of nodes) {
    const visited = new Set<string>()
    const pending = [node.key]
    const upstream: number[] = []
    while (pending.length) {
      const key = pending.pop()!
      if (visited.has(key)) continue
      visited.add(key)
      const rank = ranks.get(key)
      if (rank !== undefined) upstream.push(rank)
      pending.push(...(incoming.get(key) ?? []))
    }
    anchors.set(
      node.key,
      upstream.length
        ? upstream.reduce((sum, rank) => sum + rank, 0) / upstream.length
        : Number.MAX_SAFE_INTEGER,
    )
  }
  return [...nodes].sort(
    (a, b) =>
      a.runtime.localeCompare(b.runtime) ||
      a.column - b.column ||
      anchors.get(a.key)! - anchors.get(b.key)! ||
      a.label.localeCompare(b.label) ||
      a.key.localeCompare(b.key),
  )
}
