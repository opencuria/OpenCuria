import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import WorkspaceGuestBadge from './WorkspaceGuestBadge.vue'
import { WorkspaceStatus, type Workspace } from '@/types'

function render(patch: Partial<Workspace>) {
  return mount(WorkspaceGuestBadge, {
    props: {
      workspace: { id: 'ws', status: WorkspaceStatus.RUNNING, ...patch } as Workspace,
    },
  })
}

const license = (activation_status: string, activation_code = '') =>
  ({ kind: 'device_key', activation_status, activation_code }) as Workspace['windows_license']

describe('WorkspaceGuestBadge', () => {
  it('renders nothing for Linux workspaces and older servers', () => {
    expect(render({ guest_os: 'linux' }).html()).toBe('<!--v-if-->')
    expect(render({}).html()).toBe('<!--v-if-->')
  })

  it.each([
    ['activated', '', 'Windows · Activated', 'bg-secondary'],
    ['failed', '0xC004F074', 'Windows · Activation failed (0xC004F074)', 'text-destructive'],
    ['not_activated', '', 'Windows · Not activated', 'border-border'],
    ['pending', '', 'Windows · Activating…', 'border-border'],
  ])('shows the %s license state', (status, code, text, variantClass) => {
    const badge = render({
      guest_os: 'windows',
      windows_license: license(status, code),
    }).get('[data-testid="workspace-guest-badge"]')
    expect(badge.text()).toBe(text)
    expect(badge.attributes('title')).toBe(text)
    expect(badge.classes()).toContain(variantClass)
  })

  it('shows only the OS without a license state', () => {
    expect(
      render({ guest_os: 'windows', windows_license: null })
        .get('[data-testid="workspace-guest-badge"]')
        .text(),
    ).toBe('Windows')
  })
})
