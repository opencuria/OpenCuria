import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'
import { useAuthStore } from '@/stores/auth'
import { provideAutosaveBoundary, useAutosave, type AutosaveState } from './useAutosave'

vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))
enableAutoUnmount(afterEach)

function deferred() {
  let resolve!: () => void
  let reject!: (error: Error) => void
  const promise = new Promise<void>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function setup() {
  const value = ref('initial')
  const saved = ref('initial')
  const valid = ref(true)
  const enabled = ref(true)
  const request = vi.fn(async (_value: string) => {})
  let state!: AutosaveState
  const wrapper = mount(
    defineComponent({
      setup() {
        state = useAutosave({
          source: () => value.value,
          dirty: () => value.value !== saved.value,
          valid: () => valid.value,
          enabled: () => enabled.value,
          errorMessage: 'Could not save settings',
          save: async (isCurrent) => {
            const snapshot = value.value
            await request(snapshot)
            if (isCurrent()) saved.value = snapshot
          },
        })
        return () => null
      },
    }),
  )
  return { value, saved, valid, enabled, request, state, wrapper }
}

async function settle() {
  await nextTick()
  await vi.advanceTimersByTimeAsync(500)
  await flushPromises()
}

describe('useAutosave', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.clearAllMocks()
  })
  afterEach(() => vi.useRealTimers())

  it('coalesces typing without writing initial data or unchanged values', async () => {
    const form = setup()
    await settle()
    expect(form.request).not.toHaveBeenCalled()
    form.value.value = 'first'
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)
    form.value.value = 'latest'
    await settle()
    expect(form.request).toHaveBeenCalledExactlyOnceWith('latest')
    expect(form.state.dirty).toBe(false)
    expect(toast.error).not.toHaveBeenCalled()
  })

  it.each(['latest', 'initial'])(
    'serializes a newer edit to %s during an active write',
    async (latest) => {
      const form = setup()
      const pending = deferred()
      form.request.mockImplementationOnce(() => pending.promise)
      form.value.value = 'first'
      await settle()
      form.value.value = latest
      await settle()
      expect(form.request).toHaveBeenCalledTimes(1)
      pending.resolve()
      await flushPromises()
      expect(form.request.mock.calls.map(([value]) => value)).toEqual(['first', latest])
      expect(form.value.value).toBe(latest)
      expect(form.saved.value).toBe(latest)
      expect(form.state.saving).toBe(false)
    },
  )

  it('keeps invalid input local and saves when it becomes valid', async () => {
    const form = setup()
    form.valid.value = false
    form.value.value = ''
    await settle()
    expect(await form.state.flush()).toBe(false)
    expect(form.request).not.toHaveBeenCalled()
    form.value.value = 'corrected'
    form.valid.value = true
    await settle()
    expect(form.request).toHaveBeenCalledExactlyOnceWith('corrected')
  })

  it('retains errors and dirty input for a retry without an endless request loop', async () => {
    const form = setup()
    form.request.mockRejectedValueOnce(new Error('offline'))
    form.value.value = 'edited'
    await settle()
    expect(form.state.error).toBe('offline')
    expect(form.state.dirty).toBe(true)
    await vi.advanceTimersByTimeAsync(10000)
    expect(form.request).toHaveBeenCalledTimes(1)
    expect(await form.state.flush()).toBe(true)
    expect(form.saved.value).toBe('edited')
    expect(form.state.error).toBeNull()
  })

  it('flushes a pending edit on unmount, including another edit during the write', async () => {
    const form = setup()
    const pending = deferred()
    form.request.mockImplementationOnce(() => pending.promise)
    form.value.value = 'first'
    await settle()
    form.value.value = 'last'
    form.wrapper.unmount()
    pending.resolve()
    await flushPromises()
    expect(form.request.mock.calls.map(([value]) => value)).toEqual(['first', 'last'])
  })

  it('does not flush an old organization draft into a different organization', async () => {
    const form = setup()
    form.value.value = 'old organization'
    await nextTick()
    useAuthStore().activeOrganizationId = 'different'
    form.wrapper.unmount()
    await settle()
    expect(form.request).not.toHaveBeenCalled()
  })

  it('ignores an in-flight response after the organization changes', async () => {
    const form = setup()
    const pending = deferred()
    form.request.mockImplementationOnce(() => pending.promise)
    form.value.value = 'old organization'
    await settle()
    useAuthStore().activeOrganizationId = 'different'
    pending.resolve()
    await flushPromises()
    expect(form.saved.value).toBe('initial')
    expect(form.request).toHaveBeenCalledTimes(1)
  })

  it('blocks leaving a settings boundary on invalid input or failure and flushes before leaving', async () => {
    let leave!: () => Promise<boolean>
    const fail = vi.fn(async () => {})
    const value = ref('initial')
    const saved = ref('initial')
    const valid = ref(true)
    const Child = defineComponent({
      setup() {
        useAutosave({
          source: () => value.value,
          dirty: () => value.value !== saved.value,
          valid: () => valid.value,
          errorMessage: 'Could not save',
          save: async () => {
            const snapshot = value.value
            await fail()
            saved.value = snapshot
          },
        })
        return () => null
      },
    })
    mount(
      defineComponent({
        components: { Child },
        setup() {
          leave = provideAutosaveBoundary()
        },
        template: '<Child />',
      }),
    )
    value.value = 'edited'
    valid.value = false
    expect(await leave()).toBe(false)
    expect(fail).not.toHaveBeenCalled()
    valid.value = true
    fail.mockRejectedValueOnce(new Error('offline'))
    expect(await leave()).toBe(false)
    expect(await leave()).toBe(true)
    expect(saved.value).toBe('edited')
  })
})
