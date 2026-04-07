import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppSidebar from './AppSidebar.vue'

const authStore = {
  organizations: [
    { id: 'org-1', name: 'Acme', role: 'admin' },
    { id: 'org-2', name: 'Beta', role: 'member' },
  ],
  activeOrganization: { id: 'org-1', name: 'Acme', role: 'admin' },
  activeOrganizationId: 'org-1',
  isAdmin: true,
  user: { email: 'admin@example.com' },
  setActiveOrganization: vi.fn(),
  logout: vi.fn(),
}

vi.mock('vue-router', () => ({
  RouterLink: {
    name: 'RouterLink',
    template: '<a><slot /></a>',
  },
  useRoute: () => ({
    path: '/',
  }),
  useRouter: () => ({
    go: vi.fn(),
    push: vi.fn(),
  }),
}))

vi.mock('@/composables/useTheme', () => ({
  useTheme: () => ({
    mode: { value: 'light' },
    setTheme: vi.fn(),
  }),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authStore,
}))

vi.mock('@/services/socket', () => ({
  connect: vi.fn(),
  disconnect: vi.fn(),
  isConnected: { value: true },
}))

describe('AppSidebar organization switcher', () => {
  beforeEach(() => {
    authStore.setActiveOrganization.mockClear()
    authStore.logout.mockClear()
  })

  it('uses the dedicated border token for the light-theme organization switcher', async () => {
    const wrapper = shallowMount(AppSidebar, {
      props: {
        mobileOpen: false,
      },
      global: {
        stubs: {
          OpenCuriaLogo: true,
        },
      },
    })

    const toggleButton = wrapper.findAll('button').find((node) =>
      node.attributes('style')?.includes('--sidebar-org-switcher-border'),
    )

    expect(toggleButton).toBeDefined()
    expect(toggleButton?.attributes('style')).toContain(
      'border: 1px solid var(--sidebar-org-switcher-border)',
    )

    await toggleButton?.trigger('click')

    expect(wrapper.html()).toContain(
      'border: 1px solid var(--sidebar-org-switcher-border)',
    )
    expect(wrapper.html()).toContain(
      'border-top: 1px solid var(--sidebar-org-switcher-border)',
    )
  })

  it('uses the dedicated divider token for the sidebar edge', () => {
    const wrapper = shallowMount(AppSidebar, {
      props: {
        mobileOpen: false,
      },
      global: {
        stubs: {
          OpenCuriaLogo: true,
        },
      },
    })

    const sidebar = wrapper.find('aside')

    expect(sidebar.attributes('style')).toContain(
      'border-right: 1px solid var(--sidebar-divider-color)',
    )
    expect(sidebar.attributes('style')).toContain(
      'box-shadow: var(--glass-shadow-sm)',
    )
  })
})
