import { mount, flushPromises } from '@vue/test-utils'
import { it, expect, vi } from 'vitest'
import RunnerOperationsPanel from './RunnerOperationsPanel.vue'
const api = vi.hoisted(() => ({ inspectOperation: vi.fn(), disposeOperation: vi.fn() }))
vi.mock('@/services/runnerStorage.api', () => api)
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
    w.findAll('button').find((b) => b.text() === 'Inspect current runner evidence')!
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
