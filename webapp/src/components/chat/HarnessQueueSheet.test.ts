import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessQueueSheet from './HarnessQueueSheet.vue'
import source from './HarnessQueueSheet.vue?raw'
import type { HarnessSteer } from '@/types/harness'

function makeSteer(id: string, content: string): HarnessSteer {
  return { id, content, created_at: '2026-10-16T10:00:00Z' }
}

describe('HarnessQueueSheet', () => {
  it('lists queued messages in order, one truncated line each, with the full text as tooltip', () => {
    const long = 'Please also update the changelog\nand bump the version in package.json'
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'Use pnpm'), makeSteer('b', long)] },
    })

    const rows = wrapper.findAll('[data-testid="composer-queue-row"]')
    expect(rows).toHaveLength(2)
    const texts = wrapper.findAll('[data-testid="composer-queue-text"]')
    expect(texts[0]!.text()).toBe('Use pnpm')
    expect(texts[1]!.text()).toBe(
      'Please also update the changelog and bump the version in package.json',
    )
    expect(texts[1]!.classes()).toContain('truncate')
    expect(texts[1]!.attributes('title')).toBe(long)
    expect(wrapper.get('[data-testid="composer-queue-sheet"]').attributes('aria-label')).toBe(
      'Queued messages',
    )
  })

  it('emits withdraw with the entry id from its ghost button', async () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first'), makeSteer('b', 'second')] },
    })

    const buttons = wrapper.findAll('[data-testid="composer-queue-withdraw"]')
    expect(buttons[0]!.attributes('aria-label')).toBe('Withdraw queued message')
    await buttons[1]!.trigger('click')

    expect(wrapper.emitted('withdraw')).toEqual([['b']])
  })

  it('removes a row when its entry leaves the queue', async () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first'), makeSteer('b', 'second')] },
    })

    await wrapper.setProps({ steers: [makeSteer('b', 'second')] })

    const texts = wrapper.findAll('[data-testid="composer-queue-text"]')
    expect(texts.map((node) => node.text())).toEqual(['second'])
  })

  it('is read-only as a peek edge and never takes focus', () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first')], peek: true },
      attachTo: document.body,
    })

    expect(wrapper.find('[data-testid="composer-queue-withdraw"]').exists()).toBe(false)
    expect(document.activeElement).toBe(document.body)
    wrapper.unmount()
  })

  it('only animates when motion is allowed', () => {
    const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? ''
    const guarded = /@media \(prefers-reduced-motion: no-preference\)\s*\{([\s\S]*?)\n\}\n/.exec(
      style,
    )?.[1]
    expect(guarded).toContain('animation:')
    expect(guarded).toContain('transition:')
    const outside = style.replace(guarded ?? '', '')
    expect(outside).not.toMatch(/\banimation\s*:/)
    expect(outside).not.toMatch(/\btransition\s*:/)
  })
})
