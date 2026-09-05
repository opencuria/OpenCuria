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
})
