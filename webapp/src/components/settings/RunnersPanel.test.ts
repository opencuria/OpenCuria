import { mount, flushPromises } from '@vue/test-utils'
import { it, expect, vi } from 'vitest'
import RunnersPanel from './RunnersPanel.vue'
vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => ({
    runners: [{ id: 'r', name: 'Selected runner' }],
    loading: false,
    error: null,
    fetchRunners: vi.fn(),
  }),
}))
vi.mock('@/composables/usePolling', () => ({ usePolling: () => ({ start: vi.fn() }) }))
it('opens selected runner within settings with back navigation', async () => {
  const w = mount(RunnersPanel, {
    global: {
      stubs: {
        CreateRunnerDialog: true,
        RunnerList: {
          props: ['runners'],
          emits: ['select'],
          template: '<button @click="$emit(\'select\', runners[0])">Selected runner</button>',
        },
        RunnerStorageDetail: {
          props: ['runner'],
          emits: ['back'],
          template:
            '<div>{{ runner.id }} storage <button @click="$emit(\'back\')">Back</button></div>',
        },
      },
    },
  })
  await w.get('button').trigger('click')
  await flushPromises()
  expect(w.text()).toContain('r storage')
  await w.get('button').trigger('click')
  expect(w.text()).toContain('Selected runner')
  w.unmount()
})
