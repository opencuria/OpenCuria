import { mount, flushPromises } from '@vue/test-utils'
import { expect, it } from 'vitest'
import RunnerActivityList from './RunnerActivityList.vue'
import type { DeletionRequest, RunnerStorage } from '@/types/runnerStorage'
const data = (): RunnerStorage => ({
  runner_id: 'r',
  runner_online: true,
  latest_snapshot_id: null,
  latest_complete: true,
  runtimes: [],
  generations: [],
  operations: [],
  capture_requests: [],
})
const deletion = (phase: string, can_cancel = false): DeletionRequest => ({
  id: phase,
  phase,
  can_cancel,
  diagnostic: 'Server diagnostic',
  target_type: 'image',
  target_id: 'image',
  mode: 'deferred',
  approval: {
    fingerprint: 'f',
    blockers: [],
    counts: { images: 1, workspaces: 0 },
    images: [{ id: 'image', name: 'Saved image', owner_id: null, runner_id: 'r' }],
    workspaces: [],
  },
  fingerprint: 'f',
})
const mountList = (storage = data(), deletions: DeletionRequest[] = [], busy = false) =>
  mount(RunnerActivityList, {
    props: { data: storage, deletions, busy },
    global: {
      stubs: {
        RunnerOperationsPanel: {
          name: 'RunnerOperationsPanel',
          props: ['operations', 'targets'],
          emits: ['changed'],
          template: '<div />',
        },
      },
    },
  })
it('shows only empty activity when nothing is recorded', () => {
  const w = mountList()
  expect(w.text()).toBe('No recorded activity')
  expect(w.find('section').exists()).toBe(false)
  w.unmount()
})
it('gates cancellation on server permission and busy state and emits explicit graph review', async () => {
  const w = mountList(
    data(),
    [deletion('waiting', true), deletion('reconfirmation_required'), deletion('completed')],
    true,
  )
  const cancel = () => w.findAll('button').find((b) => b.text() === 'Cancel pending request')!
  expect(w.findAll('button').filter((b) => b.text() === 'Cancel pending request')).toHaveLength(1)
  await cancel().trigger('click')
  expect(w.emitted('cancel')).toBeUndefined()
  await w
    .findAll('button')
    .find((b) => b.text() === 'Review changed graph and confirm again')!
    .trigger('click')
  expect(w.emitted('review')).toBeUndefined()
  await w.setProps({ busy: false })
  await cancel().trigger('click')
  expect(w.emitted('cancel')).toEqual([['waiting']])
  await w
    .findAll('button')
    .find((b) => b.text() === 'Review changed graph and confirm again')!
    .trigger('click')
  expect(w.emitted('review')).toEqual([
    [{ target_type: 'image', target_id: 'image' }, 'Saved image'],
  ])
  expect(w.findAll('[role="alert"]')).toHaveLength(3)
  w.unmount()
})
it('keeps truthful deletion outcomes with active requests first', () => {
  const w = mountList(data(), [
    deletion('completed'),
    deletion('cancelled'),
    deletion('waiting', true),
  ])
  const text = w.get('section').text()
  expect(text).toContain('Cancelled — resources retained')
  expect(text).toContain('Completed')
  expect(text.indexOf('waiting')).toBeLessThan(text.indexOf('Completed'))
  w.unmount()
})
it('resolves workspace and image names, shows suppression, and collapses terminal capture history', async () => {
  const storage = data()
  storage.generations = [
    { id: 'image', name: 'Saved image', dependencies: [{ id: 'ws', name: 'Source workspace' }] },
  ] as RunnerStorage['generations']
  storage.capture_requests = [
    {
      id: 'done',
      workspace_id: 'ws',
      image_id: 'image',
      phase: 'completed',
      diagnostic: 'Capture finished',
      resume_suppressed: false,
    },
    {
      id: 'active',
      workspace_id: 'ws',
      image_id: 'image',
      phase: 'capturing',
      diagnostic: 'Waiting for evidence',
      resume_suppressed: true,
    },
    {
      id: 'cancelled',
      workspace_id: 'ws',
      image_id: 'image',
      phase: 'cancelled',
      diagnostic: 'Capture cancelled',
      resume_suppressed: true,
    },
  ]
  storage.operations = [
    {
      task_id: 'op',
      target: 'image',
      task__status: 'pending',
      task__error: '',
      phase: 'waiting',
      deliveries: 1,
    },
  ]
  const w = mountList(storage)
  expect(w.text()).toContain('Source workspace → Saved image')
  expect(w.text()).toContain('Restart suppressed')
  expect(w.text()).toContain('Completed history (1)')
  expect(w.text()).not.toContain('Capture finished')
  const operations = w.findComponent({ name: 'RunnerOperationsPanel' })
  expect(operations.props('targets')).toEqual({ image: 'Saved image' })
  operations.vm.$emit('changed')
  expect(w.emitted('changed')).toEqual([[]])
  await w
    .findAll('button')
    .find((b) => b.text() === 'Completed history (1)')!
    .trigger('click')
  await flushPromises()
  expect(w.text()).toContain('Capture finished')
  expect(w.text()).toContain('Cancelled — resources retained')
  w.unmount()
})
it('truncates unknown targets but preserves complete IDs in technical details', async () => {
  const storage = data()
  const id = 'unknown-workspace-12345678901234567890'
  storage.capture_requests = [
    {
      id: 'capture',
      workspace_id: id,
      image_id: 'image',
      phase: 'waiting',
      diagnostic: '',
      resume_suppressed: false,
    },
  ]
  const w = mountList(storage)
  expect(w.text()).toContain('unknown-work…7890')
  expect(w.text()).not.toContain(id)
  await w
    .findAll('button')
    .find((b) => b.text() === 'Technical identifiers')!
    .trigger('click')
  await flushPromises()
  expect(w.text()).toContain(id)
  w.unmount()
})

it('resolves resource workspace and generation identities without using provenance', () => {
  const storage = data()
  storage.runtimes = [
    {
      resources: [
        { workspace: { id: 'ws', name: 'Running workspace' }, provenance: 'not-a-dependency' },
      ],
    },
  ] as RunnerStorage['runtimes']
  storage.generations = [
    { id: 'image', name: 'Current image', dependencies: [] },
  ] as unknown as RunnerStorage['generations']
  storage.operations = ['ws', 'image', 'not-a-dependency'].map((target) => ({
    task_id: target,
    target,
    task__status: 'pending',
    task__error: '',
    phase: 'waiting',
    deliveries: 1,
  }))
  const w = mountList(storage)
  expect(w.findComponent({ name: 'RunnerOperationsPanel' }).props('targets')).toEqual({
    ws: 'Running workspace',
    image: 'Current image',
    'not-a-dependency': 'not-a-dependency',
  })
  w.unmount()
})
