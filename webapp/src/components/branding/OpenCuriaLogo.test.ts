import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import OpenCuriaLogo from './OpenCuriaLogo.vue'

describe('OpenCuriaLogo', () => {
  it('crops the icon viewBox so the mark fills the box', () => {
    const wrapper = mount(OpenCuriaLogo, { props: { iconOnly: true } })

    expect(wrapper.get('svg').attributes('viewBox')).toBe('13 13 38 38')
    expect(wrapper.get('svg').classes()).toContain('size-8')
    expect(wrapper.get('svg').attributes('data-motion')).toBe('none')
    expect(wrapper.get('svg').classes()).not.toContain('oc-logo--idle')
    expect(wrapper.get('svg').classes()).not.toContain('oc-logo--working')
    expect(wrapper.get('svg').classes()).not.toContain('oc-logo--enter')
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
    expect(wrapper.get('text.oc-wordmark').text()).toContain('Open')
    expect(wrapper.get('text.oc-wordmark').text()).toContain('Curia')
  })

  it('lets callers override the default wordmark height', () => {
    const wrapper = mount(OpenCuriaLogo, { attrs: { class: 'h-14 w-auto' } })

    expect(wrapper.get('svg').classes()).toContain('h-14')
    expect(wrapper.get('svg').classes()).not.toContain('h-10')
  })

  it('applies the idle motion class on the home hero mark', () => {
    const wrapper = mount(OpenCuriaLogo, {
      props: { iconOnly: true, motion: 'idle' },
    })

    expect(wrapper.get('svg').attributes('data-motion')).toBe('idle')
    expect(wrapper.get('svg').classes()).toContain('oc-logo--idle')
  })

  it('applies the working motion class for in-flight work', () => {
    const wrapper = mount(OpenCuriaLogo, {
      props: { iconOnly: true, motion: 'working' },
    })

    expect(wrapper.get('svg').attributes('data-motion')).toBe('working')
    expect(wrapper.get('svg').classes()).toContain('oc-logo--working')
    expect(wrapper.findAll('.oc-face')).toHaveLength(6)
    expect(wrapper.findAll('.oc-face--primary')).toHaveLength(2)
  })

  it('applies the enter motion class on the wordmark', () => {
    const wrapper = mount(OpenCuriaLogo, { props: { motion: 'enter' } })

    expect(wrapper.get('svg').attributes('data-motion')).toBe('enter')
    expect(wrapper.get('svg').classes()).toContain('oc-logo--enter')
    expect(wrapper.find('.oc-wordmark').exists()).toBe(true)
  })

  it('hides decorative marks from assistive tech', () => {
    const wrapper = mount(OpenCuriaLogo, {
      props: { iconOnly: true, alt: '', motion: 'working' },
    })

    expect(wrapper.get('svg').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('svg').attributes('aria-label')).toBeUndefined()
    expect(wrapper.get('svg').attributes('role')).toBeUndefined()
  })
})
