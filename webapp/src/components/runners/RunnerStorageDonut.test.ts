import { toRaw } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import RunnerStorageDonut from './RunnerStorageDonut.vue'
import { buildRunnerTopology, projectRunnerTopology } from '@/lib/runnerTopology'
import { storageRingPath } from '@/lib/runnerStorageChart'
import type {
  StorageGeneration,
  StorageResource,
  StorageRuntime,
  StorageWorkspace,
} from '@/types/runnerStorage'

const ws: StorageWorkspace = {
  id: 'ws',
  name: 'My workspace',
  owner_id: null,
  owner_label: 'Owner',
  status: 'ready',
  last_activity_at: null,
}
const file = (
  physical_id: string,
  bytes: number,
  overrides: Partial<StorageResource> = {},
): StorageResource => ({
  physical_id,
  allocated_bytes: bytes,
  kind: 'disk',
  filesystem_id: 'root',
  file_identity: physical_id,
  managed: true,
  state: 'present',
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
function fixture(): StorageRuntime[] {
  return [
    {
      runtime_type: 'qemu',
      fresh: true,
      snapshot_id: 1,
      collected_at: null,
      received_at: null,
      diagnostics: null,
      filesystems: [
        {
          filesystem_id: 'root',
          path: '/',
          capacity_bytes: 1000,
          used_bytes: 700,
          available_bytes: 200,
        },
      ],
      resources: [
        file('Base image', 100, { kind: 'image' }),
        file('vm', 0, { kind: 'workspace', workspace: ws, dependencies: ['overlay', 'seed'] }),
        file('overlay', 200, { workspace: ws, dependencies: ['Base image'] }),
        file('seed', 50, { kind: 'file', workspace: ws }),
      ],
    },
  ]
}
const wrappers: VueWrapper[] = []
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
})
function render(runtimes = fixture(), selected = false, generations: StorageGeneration[] = []) {
  const topology = projectRunnerTopology(buildRunnerTopology(runtimes, generations))
  const workspaceNode = topology.nodes.find((node) => node.workspace?.id === ws.id)!
  const imageNode = topology.nodes.find(
    (node) => node.resource?.kind === 'image' || node.generation,
  )!
  const wrapper = mount(RunnerStorageDonut, {
    props: { runtimes, topology, selectedKey: selected ? workspaceNode.key : null },
  })
  wrappers.push(wrapper)
  return { wrapper, workspaceNode, imageNode }
}
const segment = (wrapper: VueWrapper, key: string) =>
  wrapper
    .findAll('path[data-segment-key]')
    .find((path) => path.attributes('data-segment-key') === key)!

describe('RunnerStorageDonut', () => {
  it('renders accessible inner totals and outer allocations with exact capacity proportions', () => {
    const { wrapper, imageNode, workspaceNode } = render()
    expect(wrapper.get('section').attributes('aria-label')).toBe('QEMU storage breakdown')
    expect(wrapper.get('svg[aria-label="Two-ring storage chart"]').attributes('aria-label')).toBe(
      'Two-ring storage chart',
    )
    expect(wrapper.findAll('path[data-segment-key]')).toHaveLength(7)
    const inner = segment(wrapper, `inner-${imageNode.key}`)
    expect(inner.attributes()).toMatchObject({
      role: 'img',
      tabindex: '0',
      'aria-label': 'Base image · Image + workspaces: 350 B · 35.0%',
      d: storageRingPath(70, 107, 0, 126),
    })
    expect(inner.attributes('aria-pressed')).toBeUndefined()
    expect(segment(wrapper, `outer-${imageNode.key}`).attributes('d')).toBe(
      storageRingPath(111, 148, 0, 36),
    )
    expect(segment(wrapper, `outer-${workspaceNode.key}`).attributes()).toMatchObject({
      role: 'button',
      'aria-label': 'My workspace: 250 B · 25.0%',
      d: storageRingPath(111, 148, 36, 90),
      'aria-pressed': 'false',
    })
    expect(segment(wrapper, 'inner-other').attributes('d')).toBe(storageRingPath(70, 107, 126, 162))
    expect(segment(wrapper, 'outer-free').attributes('d')).toBe(storageRingPath(111, 148, 288, 72))
    expect(
      wrapper
        .get('svg[aria-label="Two-ring storage chart"]')
        .findAll('text')
        .map((text) => text.text()),
    ).toEqual(['Total capacity', '1000 B'])
    expect(wrapper.get('[aria-live="polite"]').text()).toContain('1 distinct filesystem')
  })
  it('announces focused and hovered values, clearing them on blur and mouseleave', async () => {
    const { wrapper, workspaceNode, imageNode } = render()
    const live = wrapper.get('[aria-live="polite"]')
    await segment(wrapper, `outer-${workspaceNode.key}`).trigger('focus')
    expect(live.text()).toContain('My workspace')
    expect(live.text()).toContain('250 B · 25.0%')
    await segment(wrapper, `outer-${workspaceNode.key}`).trigger('blur')
    expect(live.text()).toContain('1 distinct filesystem')
    await segment(wrapper, `inner-${imageNode.key}`).trigger('mouseenter')
    expect(live.text()).toContain('350 B · 35.0%')
    await wrapper.get('svg[aria-label="Two-ring storage chart"]').trigger('mouseleave')
    expect(live.text()).toContain('1 distinct filesystem')
  })
  it.each(['click', 'enter', 'space'])(
    'selects only outer nodes with %s and emits the exact presentation node',
    async (action) => {
      const { wrapper, workspaceNode, imageNode } = render()
      const activate = async (key: string) => {
        const path = segment(wrapper, key)
        if (action === 'click') await path.trigger('click')
        else
          await path.trigger('keydown', {
            key: action === 'enter' ? 'Enter' : ' ',
            code: action === 'enter' ? 'Enter' : 'Space',
          })
      }
      for (const key of [
        `inner-${imageNode.key}`,
        'inner-other',
        'outer-other',
        'inner-free',
        'outer-free',
      ])
        await activate(key)
      expect(wrapper.emitted('select')).toBeUndefined()
      await activate(`outer-${workspaceNode.key}`)
      await activate(`outer-${imageNode.key}`)
      expect(wrapper.emitted('select')).toEqual([[workspaceNode], [imageNode]])
      expect(toRaw(wrapper.emitted('select')![0]![0])).toBe(workspaceNode)
      await wrapper.setProps({ selectedKey: workspaceNode.key })
      expect(segment(wrapper, `outer-${workspaceNode.key}`).attributes('aria-pressed')).toBe('true')
      expect(segment(wrapper, `outer-${imageNode.key}`).attributes('aria-pressed')).toBe('false')
    },
  )
  it('expands the legend without selecting and lets a child select its node', async () => {
    const { wrapper, workspaceNode } = render()
    const legend = wrapper.get('[aria-label="Storage legend"]')
    const trigger = legend.get('button[aria-expanded]')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    await trigger.trigger('click')
    await flushPromises()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(wrapper.emitted('select')).toBeUndefined()
    const child = legend.get('button[aria-label="Inspect storage My workspace"]')
    expect(child.attributes('aria-pressed')).toBe('false')
    await child.trigger('click')
    expect(wrapper.emitted('select')).toEqual([[workspaceNode]])
    await wrapper.setProps({ selectedKey: workspaceNode.key })
    expect(child.attributes('aria-pressed')).toBe('true')
    expect(legend.findAll('button')).toHaveLength(3)
    expect(legend.text()).toContain('Other')
    expect(legend.text()).toContain('Free')
  })
  it('opens the selected workspace legend by default', async () => {
    const { wrapper } = render(fixture(), true)
    await flushPromises()
    expect(wrapper.get('button[aria-expanded]').attributes('aria-expanded')).toBe('true')
    expect(
      wrapper.get('button[aria-label="Inspect storage My workspace"]').attributes('aria-pressed'),
    ).toBe('true')
  })
  it('retains a physically missing image as an Unknown selectable legend entry, never a guessed ring', async () => {
    const runtimes = fixture()
    runtimes[0]!.resources = []
    const generation: StorageGeneration = {
      id: 'missing',
      name: 'Missing image',
      owner_label: 'Owner',
      runtime_type: 'qemu',
      definition_id: null,
      definition_name: null,
      build_job_id: null,
      generation: 1,
      status: 'ready',
      assignment_status: null,
      runner_ref: 'missing',
      size_bytes: 999999,
      size_source: 'logical',
      origin_type: 'definition_build',
      revision_id: null,
      is_legacy: false,
      is_current: true,
      is_pending: false,
      observed_state: 'unknown',
      dependencies: [],
    }
    const { wrapper, imageNode } = render(runtimes, false, [generation])
    expect(
      wrapper.findAll('path[data-segment-key]').map((path) => path.attributes('data-segment-key')),
    ).toEqual(['inner-other', 'outer-other', 'inner-free', 'outer-free'])
    const legend = wrapper.get('[aria-label="Storage legend"]')
    expect(legend.get('button[aria-expanded]').text()).toContain('Partial')
    await legend.get('button[aria-expanded]').trigger('click')
    await flushPromises()
    const child = legend.get('button[aria-label="Inspect storage Missing image"]')
    expect(child.text()).toContain('Unknown')
    expect(child.text()).not.toContain('999999')
    expect(wrapper.text()).toContain('unmeasured · group totals are partial.')
    await child.trigger('click')
    expect(toRaw(wrapper.emitted('select')![0]![0])).toBe(imageNode)
    await wrapper.setProps({ selectedKey: imageNode.key })
    expect(child.attributes('aria-pressed')).toBe('true')
  })
  it.each(['image', 'workspace'])(
    'keeps a measured zero %s selectable in the legend as 0 B rather than Unknown',
    async (target) => {
      const runtimes = fixture()
      for (const resource of runtimes[0]!.resources) resource.allocated_bytes = 0
      const { wrapper, imageNode, workspaceNode } = render(runtimes)
      expect(wrapper.findAll('path[data-segment-key]')).toHaveLength(4)
      const legend = wrapper.get('[aria-label="Storage legend"]')
      expect(legend.text()).not.toMatch(/Partial|Unknown/)
      await legend.get('button[aria-expanded]').trigger('click')
      await flushPromises()
      const node = target === 'image' ? imageNode : workspaceNode
      const child = legend.get(`button[aria-label="Inspect storage ${node.label}"]`)
      expect(child.text()).toContain('0 B')
      expect(child.text()).not.toContain('Unknown')
      expect(wrapper.text()).not.toContain('unmeasured')
      await child.trigger('click')
      expect(toRaw(wrapper.emitted('select')![0]![0])).toBe(node)
      await wrapper.setProps({ selectedKey: node.key })
      expect(child.attributes('aria-pressed')).toBe('true')
    },
  )
  it('honestly labels stale and partial inventories', () => {
    const runtimes = fixture()
    runtimes[0]!.fresh = false
    runtimes[0]!.resources[0]!.allocated_bytes = null
    const { wrapper } = render(runtimes)
    expect(wrapper.text()).toContain('Last confirmed inventory · Outdated')
    expect(wrapper.text()).toContain('1 file allocation unmeasured · group totals are partial.')
    expect(wrapper.text()).toContain(
      'Other includes system files, unassigned storage and reserved space.',
    )
    expect(wrapper.find('svg[aria-label="Two-ring storage chart"]').exists()).toBe(true)
  })
  it.each(['absent', 'filesystem', 'identity'])(
    'shows an honest unavailable state for %s inventory without a guessed chart',
    (mode) => {
      const runtimes = mode === 'absent' ? [] : fixture()
      if (mode === 'filesystem') runtimes[0]!.filesystems = []
      if (mode === 'identity') delete runtimes[0]!.resources[0]!.file_identity
      const { wrapper } = render(runtimes)
      expect(wrapper.find('svg[aria-label="Two-ring storage chart"]').exists()).toBe(false)
      expect(wrapper.find('[aria-label="Storage legend"]').exists()).toBe(false)
      expect(
        wrapper
          .findAll('[role="status"]')
          .some((status) => /No QEMU|Refresh inventory/.test(status.text())),
      ).toBe(true)
    },
  )
})
