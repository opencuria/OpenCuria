import { mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { beforeEach, it, expect, vi } from 'vitest'
import RunnersPanel from './RunnersPanel.vue'
const store = reactive({
  runners: [{ id: 'r', name: 'Selected runner', status: 'online' }],
  loading: false,
  error: null,
})
vi.mock('@/stores/runners', () => ({ useRunnerStore: () => store }))
vi.mock('@/composables/usePolling', () => ({ usePolling: () => ({ start: vi.fn() }) }))
const stubs = {
  CreateRunnerDialog: true,
  RunnerList: {
    props: ['runners'],
    emits: ['select'],
    template: '<button @click="$emit(\'select\', runners[0])">Select runner</button>',
  },
  RunnerStorageDetail: {
    props: ['runner'],
    emits: ['back'],
    template:
      '<div>{{ runner.name }} {{ runner.status }}<button @click="$emit(\'back\')">Back</button></div>',
  },
}
beforeEach(() => {
  store.runners = [{ id: 'r', name: 'Selected runner', status: 'online' }]
})
it('Back persists across polls; a fresh context can reopen the same runner', async () => {
  const w = mount(RunnersPanel, { props: { runnerId: 'r', contextVersion: 1 }, global: { stubs } })
  expect(w.text()).toContain('online')
  await w.get('button').trigger('click')
  store.runners = [...store.runners]
  await w.vm.$nextTick()
  expect(w.text()).toContain('Select runner')
  await w.setProps({ contextVersion: 2 })
  expect(w.text()).toContain('online')
  expect(w.emitted('detail-change')).toEqual([[true], [false], [true]])
  w.unmount()
  const events = w.emitted('detail-change')!
  expect(events[events.length - 1]).toEqual([false])
})
it('derives selection from refreshed data and handles removal and changed links', async () => {
  const w = mount(RunnersPanel, { props: { runnerId: 'r' }, global: { stubs } })
  store.runners = [{ id: 'r', name: 'Renamed', status: 'offline' }]
  await w.vm.$nextTick()
  expect(w.text()).toContain('Renamed offline')
  store.runners = []
  await w.vm.$nextTick()
  expect(w.text()).toContain('Select runner')
  store.runners = [{ id: 'other', name: 'Other', status: 'online' }]
  await w.setProps({ runnerId: 'other' })
  expect(w.text()).toContain('Other online')
  await w.get('button').trigger('click')
  await w.get('button').trigger('click')
  store.runners = [{ id: 'other', name: 'Poll update', status: 'offline' }]
  await w.vm.$nextTick()
  expect(w.text()).toContain('Poll update offline')
  w.unmount()
})
