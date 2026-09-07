import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SidebarBrandHeader from './SidebarBrandHeader.vue'
import { SidebarProvider } from '@/components/ui/sidebar'

const authStore = {
  organizations: [{ id: 'org-1', name: 'Acme', role: 'admin' }],
  activeOrganization: { id: 'org-1', name: 'Acme', role: 'admin' },
  activeOrganizationId: 'org-1',
}

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authStore,
}))

vi.mock('vue-router', () => ({
  RouterLink: {
    name: 'RouterLink',
    template: '<a><slot /></a>',
  },
}))

const HeaderWrapper = defineComponent({
  components: { SidebarProvider, SidebarBrandHeader },
  template: '<SidebarProvider><SidebarBrandHeader /></SidebarProvider>',
})

function mountHeader() {
  setActivePinia(createPinia())
  return mount(HeaderWrapper, {
    global: {
      stubs: {
        Tooltip: { template: '<div><slot /></div>' },
        TooltipContent: true,
        TooltipTrigger: { template: '<div><slot /></div>' },
        TooltipProvider: { template: '<div><slot /></div>' },
        DropdownMenu: { template: '<div><slot /></div>' },
        DropdownMenuTrigger: { template: '<div><slot /></div>' },
        DropdownMenuContent: { template: '<div><slot /></div>' },
        DropdownMenuItem: { template: '<button type="button"><slot /></button>' },
        DropdownMenuSeparator: true,
      },
    },
  })
}

describe('SidebarBrandHeader', () => {
  beforeEach(() => {
    authStore.organizations = [{ id: 'org-1', name: 'Acme', role: 'admin' }]
    authStore.activeOrganization = { id: 'org-1', name: 'Acme', role: 'admin' }
    authStore.activeOrganizationId = 'org-1'
  })

  it('renders the brand mark at size-8 with important so sidebar svg rules cannot shrink it', () => {
    const wrapper = mountHeader()
    const logo = wrapper.get('svg[aria-label="OpenCuria"]')

    expect(logo.classes()).toContain('size-8!')
    expect(logo.attributes('viewBox')).toBe('13 13 38 38')
  })

  it('keeps the org-switcher chevron at the default icon size', () => {
    authStore.organizations = [
      { id: 'org-1', name: 'Acme', role: 'admin' },
      { id: 'org-2', name: 'Beta', role: 'member' },
    ]
    const wrapper = mountHeader()
    const svgs = wrapper.findAll('svg')
    const logo = svgs.find((node) => node.attributes('aria-label') === 'OpenCuria')
    const chevron = svgs.find((node) => node.attributes('aria-label') !== 'OpenCuria')

    expect(logo?.classes()).toContain('size-8!')
    expect(chevron?.classes()).toContain('size-4')
    expect(chevron?.classes()).not.toContain('size-8!')
  })
})
