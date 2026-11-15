import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { BookText } from '@lucide/vue'

import EmptyState from '@/components/common/EmptyState.vue'
import SettingsRow from './SettingsRow.vue'

describe('EmptyState', () => {
  it('renders title, description, and action slot', () => {
    const wrapper = mount(EmptyState, {
      props: {
        icon: BookText,
        title: 'No skills yet',
        description: 'Create your first skill.',
      },
      slots: {
        action: '<button>New Skill</button>',
      },
    })

    expect(wrapper.text()).toContain('No skills yet')
    expect(wrapper.text()).toContain('Create your first skill.')
    expect(wrapper.find('button').text()).toBe('New Skill')
  })
})

describe('SettingsRow', () => {
  it('renders icon, body, badges, and actions', () => {
    const wrapper = mount(SettingsRow, {
      slots: {
        icon: '<span data-testid="row-icon">icon</span>',
        default: '<span data-testid="row-body">Title</span>',
        badges: '<span data-testid="row-badge">Badge</span>',
        actions: '<button data-testid="row-action">Edit</button>',
      },
    })

    expect(wrapper.get('[data-testid="row-icon"]').text()).toBe('icon')
    expect(wrapper.get('[data-testid="row-body"]').text()).toBe('Title')
    expect(wrapper.get('[data-testid="row-badge"]').text()).toBe('Badge')
    expect(wrapper.get('[data-testid="row-action"]').text()).toBe('Edit')
  })
})
