import { defineComponent, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import { useAttentionTabTitle } from './useAttentionTabTitle'

const TitleHost = defineComponent({
  setup() {
    const count = ref(2)
    useAttentionTabTitle(count)
    return { count }
  },
  template: '<div />',
})

describe('useAttentionTabTitle', () => {
  afterEach(() => {
    document.title = 'OpenCuria'
  })

  it('prefixes the document title with the attention count', async () => {
    const wrapper = mount(TitleHost)
    expect(document.title).toBe('(2) OpenCuria')

    wrapper.vm.count = 0
    await wrapper.vm.$nextTick()
    expect(document.title).toBe('OpenCuria')

    wrapper.unmount()
    expect(document.title).toBe('OpenCuria')
  })
})
