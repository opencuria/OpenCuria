import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, it, expect, vi } from 'vitest'
import RunnerOperationsPanel from './RunnerOperationsPanel.vue'
const api = vi.hoisted(() => ({ inspectOperation: vi.fn(), disposeOperation: vi.fn() }))
vi.mock('@/services/runnerStorage.api', () => api)
beforeEach(() => vi.clearAllMocks())
it('only offers server-inspected actions, preserves diagnostics and explicitly warns before acknowledgment', async () => {
  api.inspectOperation.mockResolvedValue({
    operation_id: 'op',
    diagnostic: 'Interrupted',
    evidence: { status: 'unknown' },
    permitted_actions: [],
  })
  const w = mount(RunnerOperationsPanel, {
    props: {
      operations: [
        {
          task_id: 'op',
          task__status: 'failed',
          phase: 'intervention',
          task__error: 'Disk retained',
          target: 'ws',
          deliveries: 1,
        },
      ],
    },
    global: {
      stubs: Object.fromEntries(
        [
          'Dialog',
          'DialogContent',
          'DialogHeader',
          'DialogTitle',
          'DialogDescription',
          'DialogFooter',
        ].map((n) => [n, { template: '<div><slot /></div>' }]),
      ),
    },
  })
  const inspect = () =>
    w
      .findAll('button')
      .find((b) => b.attributes('aria-label') === 'Inspect current runner evidence')!
  await inspect().trigger('click')
  await flushPromises()
  expect(w.text()).toContain('Disk retained')
  expect(w.text()).toContain('No safe action')
  expect(w.findAll('button').some((b) => b.text() === 'retry')).toBe(false)
  api.inspectOperation.mockResolvedValue({
    operation_id: 'op',
    diagnostic: 'Interrupted',
    evidence: { quiescent: true },
    permitted_actions: ['acknowledge_interrupted'],
  })
  await inspect().trigger('click')
  await flushPromises()
  await w
    .findAll('button')
    .find((b) => b.text() === 'acknowledge interrupted')!
    .trigger('click')
  expect(api.disposeOperation).not.toHaveBeenCalled()
  expect(w.text()).toContain('Resources are preserved')
  await w
    .findAll('button')
    .find((b) => b.text() === 'Acknowledge, preserving resources')!
    .trigger('click')
  await flushPromises()
  expect(api.disposeOperation).toHaveBeenCalledWith('op', 'acknowledge_interrupted')
  w.unmount()
})

const operations = [
  {
    task_id: 'op',
    task__status: 'failed',
    phase: 'intervention',
    task__error: '',
    target: 'ws',
    deliveries: 1,
  },
]
it('hides empty activity and resolves target names', () => {
  const empty = mount(RunnerOperationsPanel, { props: { operations: [] } })
  expect(empty.find('#runner-operations').exists()).toBe(false)
  empty.unmount()
  const w = mount(RunnerOperationsPanel, {
    props: { operations, targets: { ws: 'Workspace name' } },
  })
  expect(w.text()).toContain('Workspace name')
  expect(w.get('[aria-label="Inspect current runner evidence"]').text()).toBe('Inspect')
  w.unmount()
})
it.each(['Inspection failed', 'Unauthorized'])(
  'shows inspection failure %s without actions',
  async (message) => {
    api.inspectOperation.mockRejectedValue(new Error(message))
    const w = mount(RunnerOperationsPanel, { props: { operations } })
    await w.get('[aria-label="Inspect current runner evidence"]').trigger('click')
    await flushPromises()
    expect(w.get('[role="alert"]').text()).toBe(message)
    expect(api.disposeOperation).not.toHaveBeenCalled()
    w.unmount()
  },
)
it('collapses evidence and clears eligibility on server refusal', async () => {
  api.inspectOperation.mockResolvedValue({
    operation_id: 'op',
    diagnostic: 'Eligible',
    evidence: { marker: 'evidence-only' },
    permitted_actions: ['retry'],
  })
  api.disposeOperation.mockRejectedValue(new Error('Server refused fresh evidence'))
  const w = mount(RunnerOperationsPanel, { props: { operations } })
  await w.get('[aria-label="Inspect current runner evidence"]').trigger('click')
  await flushPromises()
  expect(w.text()).not.toContain('evidence-only')
  await w
    .findAll('button')
    .find((b) => b.text() === 'Execution evidence')!
    .trigger('click')
  await flushPromises()
  expect(w.text()).toContain('evidence-only')
  await w
    .findAll('button')
    .find((b) => b.text() === 'retry')!
    .trigger('click')
  await flushPromises()
  expect(w.get('[role="alert"]').text()).toContain('Server refused')
  expect(w.findAll('button').some((b) => b.text() === 'retry')).toBe(false)
  expect(w.emitted('changed')).toBeUndefined()
  w.unmount()
})
it('prevents duplicate inspections while busy', async () => {
  let resolve!: (value: unknown) => void
  api.inspectOperation.mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r
      }),
  )
  const w = mount(RunnerOperationsPanel, { props: { operations } })
  const button = w.get('[aria-label="Inspect current runner evidence"]')
  await button.trigger('click')
  await button.trigger('click')
  expect(api.inspectOperation).toHaveBeenCalledTimes(1)
  expect(button.attributes('disabled')).toBeDefined()
  resolve({ operation_id: 'op', diagnostic: 'Safe', evidence: {}, permitted_actions: [] })
  await flushPromises()
  expect(button.attributes('disabled')).toBeUndefined()
  w.unmount()
})
