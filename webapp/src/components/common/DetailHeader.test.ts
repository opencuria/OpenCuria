import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { Server } from '@lucide/vue'

import DetailHeader from './DetailHeader.vue'

describe('DetailHeader', () => {
  it('renders the back link, identity, meta, and actions and emits back', async () => {
    const wrapper = mount(DetailHeader, {
      props: { title: 'local-qemu', backLabel: 'Back to runners', icon: Server },
      slots: {
        meta: '<span>Online</span>',
        actions: '<button type="button">Refresh</button>',
      },
    })

    expect(wrapper.get('h2').text()).toBe('local-qemu')
    expect(wrapper.text()).toContain('Online')
    expect(wrapper.text()).toContain('Refresh')
    expect(wrapper.find('svg.lucide-server').exists()).toBe(true)
    await wrapper.get('[data-testid="detail-back"]').trigger('click')
    expect(wrapper.get('[data-testid="detail-back"]').text()).toBe('Back to runners')
    expect(wrapper.emitted('back')).toHaveLength(1)
  })

  it('omits empty optional regions', () => {
    const wrapper = mount(DetailHeader, {
      props: { title: 'Notion', backLabel: 'Back to plugins', description: '' },
    })

    expect(wrapper.find('p').exists()).toBe(false)
    expect(wrapper.find('.rounded-xl').exists()).toBe(false)
    expect(wrapper.findAll('header > div > div')).toHaveLength(1)
  })

  it('preserves multiline descriptions', () => {
    const wrapper = mount(DetailHeader, {
      props: { title: 'Notion', backLabel: 'Back to plugins', description: 'Line 1\nLine 2' },
    })

    expect(wrapper.get('p').classes()).toContain('whitespace-pre-wrap')
    expect(wrapper.get('p').text()).toBe('Line 1\nLine 2')
  })
})
