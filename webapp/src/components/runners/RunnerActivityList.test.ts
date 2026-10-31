import { mount, flushPromises } from '@vue/test-utils'
import { expect, it } from 'vitest'
import RunnerActivityList from './RunnerActivityList.vue'
import type { DeletionRequest, RunnerActivity, RunnerStorage } from '@/types/runnerStorage'

const activity = (patch: Partial<RunnerActivity> = {}): RunnerActivity => ({
  id: 'blender-stop',
  kind: 'operation',
  target_name: 'Blender',
  title: 'Stop workspace',
  state: 'attention',
  outcome: 'blocked',
  summary: 'Stop could not be confirmed.',
  diagnostic: 'Unknown outcome; intervention required',
  operation_ids: ['op'],
  created_at: new Date().toISOString(),
  ...patch,
})
const data = (activities: RunnerActivity[] = []): RunnerStorage => ({
  runner_id: 'r',
  runner_online: true,
  latest_snapshot_id: null,
  latest_complete: true,
  runtimes: [],
  generations: [],
  operations: [
    {
      task_id: 'op',
      target: 'ws',
      task__status: 'failed',
      task__error: 'Unknown outcome',
      phase: 'intervention',
      deliveries: 1,
    },
  ],
  capture_requests: [],
  activities,
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
          props: ['operations', 'diagnostic'],
          emits: ['changed'],
          template: '<button @click="$emit(\'changed\')">Check status</button>',
        },
      },
    },
  })

it('shows empty activity without recovery controls', () => {
  const w = mountList()
  expect(w.text()).toBe('No recorded activity')
  expect(w.find('button').exists()).toBe(false)
  w.unmount()
})

it('shows one unresolved workflow and hides handled failures in history', async () => {
  const w = mountList(
    data([
      activity(),
      activity({
        id: 'capture',
        kind: 'capture',
        target_name: 'OpenCuria 3',
        title: 'Save workspace as image',
        state: 'history',
        outcome: 'resolved',
        summary: 'Interrupted operation closed. Existing resources were kept.',
        operation_ids: [],
      }),
    ]),
  )
  expect(w.get('[aria-label="Needs attention"]').findAll('article')).toHaveLength(1)
  expect(w.text()).toContain('Blender')
  expect(w.text()).not.toContain('OpenCuria 3')
  expect(w.text()).not.toContain('intervention')
  const panel = w.findComponent({ name: 'RunnerOperationsPanel' })
  expect(panel.props('operations')).toHaveLength(1)
  await w
    .findAll('button')
    .find((button) => button.text() === 'History (1)')!
    .trigger('click')
  await flushPromises()
  expect(w.get('[aria-label="History"]').text()).toContain('OpenCuria 3')
  expect(w.get('[aria-label="History"]').text()).toContain('Handled')
  expect(w.get('[aria-label="History"]').text()).not.toContain('Check status')
  await w
    .findAll('button')
    .find((button) => button.text() === 'Check status')!
    .trigger('click')
  expect(w.emitted('changed')).toEqual([[]])
  w.unmount()
})

it('renders a capture and its child as one card with one recovery action', () => {
  const w = mountList(data([activity({ kind: 'capture', title: 'Save workspace as image' })]))
  expect(w.findAll('article')).toHaveLength(1)
  expect(w.findAll('button').filter((button) => button.text() === 'Check status')).toHaveLength(1)
  expect(w.text()).not.toContain('Deliveries')
  w.unmount()
})

it('gates cancellation on server permission and busy state and reviews changed deletion', async () => {
  const w = mountList(
    data([
      activity({ id: 'waiting', kind: 'deletion', state: 'active', operation_ids: [] }),
      activity({
        id: 'reconfirmation_required',
        kind: 'deletion',
        target_name: 'Saved image',
        operation_ids: [],
      }),
    ]),
    [deletion('waiting', true), deletion('reconfirmation_required')],
    true,
  )
  const button = (text: string) => w.findAll('button').find((button) => button.text() === text)!
  await button('Cancel request').trigger('click')
  await button('Review deletion').trigger('click')
  expect(w.emitted('cancel')).toBeUndefined()
  expect(w.emitted('review')).toBeUndefined()
  await w.setProps({ busy: false })
  await button('Cancel request').trigger('click')
  await button('Review deletion').trigger('click')
  expect(w.emitted('cancel')).toEqual([['waiting']])
  expect(w.emitted('review')).toEqual([
    [{ target_type: 'image', target_id: 'image' }, 'Saved image'],
  ])
  w.unmount()
})

it('keeps terminal failures and suppressed restarts in collapsed history', async () => {
  const w = mountList(
    data([
      activity({
        state: 'history',
        outcome: 'failed',
        operation_ids: [],
        diagnostic: 'Restart was suppressed for this interrupted capture.',
      }),
    ]),
  )
  expect(w.find('[aria-label="Needs attention"]').exists()).toBe(false)
  await w
    .findAll('button')
    .find((button) => button.text() === 'History (1)')!
    .trigger('click')
  await flushPromises()
  expect(w.text()).toContain('Failed')
  expect(w.text()).not.toContain('Restart was suppressed')
  await w
    .findAll('button')
    .find((button) => button.text() === 'Details')!
    .trigger('click')
  await flushPromises()
  expect(w.text()).toContain('Restart was suppressed')
  expect(w.text()).not.toContain('Check status')
  w.unmount()
})

it('reports unavailable activity when connected to an older backend', () => {
  const storage = data()
  delete storage.activities
  const w = mountList(storage)
  expect(w.text()).toContain('Activity status unavailable')
  w.unmount()
})

it('shows automatic recovery in progress without an administrator alert', () => {
  const w = mountList(
    data([
      activity({
        state: 'active',
        outcome: 'recovering',
        summary: 'OpenCuria is checking the interrupted operation automatically.',
      }),
    ]),
  )
  expect(w.find('[aria-label="Needs attention"]').exists()).toBe(false)
  expect(w.get('[aria-label="In progress"]').text()).toContain('Recovering')
  expect(w.text()).toContain('automatically')
  w.unmount()
})
