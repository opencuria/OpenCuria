import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppSidebar from './AppSidebar.vue'
import { SidebarProvider } from '@/components/ui/sidebar'

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

const SidebarTestWrapper = defineComponent({
  components: { SidebarProvider, AppSidebar },
  template: '<SidebarProvider><AppSidebar /></SidebarProvider>',
})

function mountSidebar() {
  return mount(SidebarTestWrapper, {
    global: {
      stubs: {
        OpenCuriaLogo: true,
        Tooltip: { template: '<div><slot /></div>' },
        TooltipContent: true,
        TooltipTrigger: { template: '<div><slot /></div>' },
        TooltipProvider: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('AppSidebar organization switcher', () => {
  beforeEach(() => {
    authStore.setActiveOrganization.mockClear()
    authStore.logout.mockClear()
  })

  it('renders the active organization name', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Acme')
  })

  it('renders navigation sections', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Dashboard')
    expect(wrapper.text()).toContain('Workspaces')
    expect(wrapper.text()).toContain('Runners')
  })
})
