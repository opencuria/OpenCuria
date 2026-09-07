import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import OpenCuriaLogo from './OpenCuriaLogo.vue'

describe('OpenCuriaLogo', () => {
  it('crops the icon viewBox so the mark fills the box', () => {
    const wrapper = mount(OpenCuriaLogo, { props: { iconOnly: true } })

    expect(wrapper.get('svg').attributes('viewBox')).toBe('13 13 38 38')
    expect(wrapper.get('svg').classes()).toContain('size-8')
  })

  it('lets callers override the default icon size', () => {
    const wrapper = mount(OpenCuriaLogo, {
      props: { iconOnly: true },
      attrs: { class: 'size-16' },
    })

    expect(wrapper.get('svg').classes()).toContain('size-16')
    expect(wrapper.get('svg').classes()).not.toContain('size-8')
  })

  it('renders the wordmark at a larger default height', () => {
    const wrapper = mount(OpenCuriaLogo)

    expect(wrapper.get('svg').attributes('viewBox')).toBe('0 0 244 64')
    expect(wrapper.get('svg').classes()).toContain('h-10')
  })

  it('lets callers override the default wordmark height', () => {
    const wrapper = mount(OpenCuriaLogo, { attrs: { class: 'h-14 w-auto' } })

    expect(wrapper.get('svg').classes()).toContain('h-14')
    expect(wrapper.get('svg').classes()).not.toContain('h-10')
  })
})
