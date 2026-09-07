import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessNoticeSheet from './HarnessNoticeSheet.vue'

describe('HarnessNoticeSheet', () => {
  it('renders an abort notice with info tone', () => {
    const wrapper = mount(HarnessNoticeSheet, {
      props: {
        notice: {
          messageId: 'msg-1',
          text: 'Run stopped by user',
          tone: 'info',
        },
      },
    })

    expect(wrapper.get('[data-testid="composer-notice-text"]').text()).toBe(
      'Run stopped by user',
    )
    expect(wrapper.find('svg.lucide-info').exists()).toBe(true)
  })

  it('renders an error notice and emits dismiss', async () => {
    const wrapper = mount(HarnessNoticeSheet, {
      props: {
        notice: { messageId: 'msg-2', text: 'provider exploded', tone: 'error' },
      },
    })

    expect(wrapper.get('[data-testid="composer-notice-text"]').text()).toBe(
      'provider exploded',
    )
    await wrapper.get('[data-testid="composer-notice-dismiss"]').trigger('click')
    expect(wrapper.emitted('dismiss')).toEqual([['msg-2']])
  })
})
