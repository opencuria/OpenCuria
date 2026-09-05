import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessTodoSheet from './HarnessTodoSheet.vue'

describe('HarnessTodoSheet', () => {
  const todos = [
    { id: 't1', content: 'first', status: 'completed', priority: 'medium', order: 1 },
    { id: 't2', content: 'second', status: 'in_progress', priority: 'high', order: 0 },
  ] as const

  it('renders the progress count and sorted rows', () => {
    const wrapper = mount(HarnessTodoSheet, {
      props: { todos: [...todos] },
    })

    expect(wrapper.find('[data-testid="composer-todo-count"]').text()).toBe('1/2')
    const rows = wrapper.findAll('[data-testid="composer-todo-row"]')
    expect(rows.map((row) => row.text())).toEqual([
      expect.stringContaining('second'),
      expect.stringContaining('first'),
    ])
  })

  it('toggles the collapsible content', async () => {
    const wrapper = mount(HarnessTodoSheet, {
      props: { todos: [...todos], open: true },
    })
    expect(wrapper.findAll('[data-testid="composer-todo-row"]')).toHaveLength(2)

    await wrapper.find('[data-testid="composer-todo-trigger"]').trigger('click')
    expect(wrapper.emitted('update:open')).toEqual([[false]])
  })
})
