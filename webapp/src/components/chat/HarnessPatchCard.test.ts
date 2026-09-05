import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HarnessPatchCard from './HarnessPatchCard.vue'
import type { HarnessPart } from '@/types/harness'

describe('HarnessPatchCard', () => {
  it('renders unified diff output', () => {
    const part: HarnessPart = {
      id: 'p1',
      session_id: 's1',
      type: 'patch',
      state: 'completed',
      title: 'Patch /workspace/a.txt',
      output: '--- a/a.txt\n+++ b/a.txt\n-old\n+new',
      meta: {
        path: '/workspace/a.txt',
        old_content: 'old',
        new_content: 'new',
      },
    }
    const wrapper = mount(HarnessPatchCard, { props: { part } })
    expect(wrapper.text()).toContain('-old')
    expect(wrapper.text()).toContain('+new')
  })
})
