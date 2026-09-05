import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessMentionSheet from './HarnessMentionSheet.vue'
import type { MentionCandidate } from '@/lib/harnessMentions'

function fileCandidate(index: number): MentionCandidate {
  return {
    kind: 'file',
    label: `f${index}.ts`,
    insert: `file:/workspace/f${index}.ts`,
  }
}

describe('HarnessMentionSheet', () => {
  it('scrolls the active option into view when the index changes', async () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    const wrapper = mount(HarnessMentionSheet, {
      props: {
        candidates: Array.from({ length: 10 }, (_, index) => fileCandidate(index)),
        activeIndex: 0,
      },
    })
    await wrapper.setProps({ activeIndex: 9 })
    await nextTick()
    expect(scrollIntoView).toHaveBeenCalled()
    const active = wrapper.find('[aria-selected="true"]')
    expect(active.attributes('data-mention-index')).toBe('9')
  })

  it('ignores hover while the pointer stays still', async () => {
    const wrapper = mount(HarnessMentionSheet, {
      props: {
        candidates: Array.from({ length: 4 }, (_, index) => fileCandidate(index)),
        activeIndex: 0,
      },
    })
    const options = wrapper.findAll('[data-testid="composer-mention-option"]')
    await options[1]!.trigger('mousemove', { clientX: 12, clientY: 40 })
    await options[2]!.trigger('mousemove', { clientX: 12, clientY: 40 })
    expect(wrapper.emitted('hover')).toEqual([[1]])
    await options[2]!.trigger('mousemove', { clientX: 18, clientY: 40 })
    expect(wrapper.emitted('hover')).toEqual([[1], [2]])
  })
})
