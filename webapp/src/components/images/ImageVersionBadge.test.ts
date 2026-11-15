import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ImageVersionBadge from './ImageVersionBadge.vue'
import type { ImageVersionRef, Workspace } from '@/types'

function base(patch: Partial<ImageVersionRef> = {}): ImageVersionRef {
  return {
    id: 'v1',
    line_kind: 'captured',
    line_id: 'line',
    name: 'Node dev',
    version: 1,
    message: 'Initial',
    status: 'ready',
    latest_id: 'v1',
    latest_version: 1,
    update_available: false,
    ...patch,
  }
}

const render = (patch: Partial<Workspace>) =>
  mount(ImageVersionBadge, { props: { workspace: { id: 'ws', ...patch } as Workspace } })

describe('ImageVersionBadge', () => {
  it('shows which version a workspace is based on', () => {
    const w = render({ base_image: base() })
    expect(w.get('[data-testid="image-version-badge"]').text()).toContain('Node dev · v1')
    expect(w.get('[data-testid="image-version-badge"]').attributes('title')).toContain('Initial')
    expect(w.find('[data-testid="image-version-update"]').exists()).toBe(false)
  })

  it('offers an update when a newer version exists', async () => {
    const w = render({
      base_image: base({ update_available: true, latest_id: 'v4', latest_version: 4 }),
    })
    const update = w.get('[data-testid="image-version-update"]')
    expect(update.text()).toBe('v4 available')
    await update.trigger('click')
    expect(w.emitted('update')).toHaveLength(1)
  })

  it('shows the target while a reset or update is pending', () => {
    const w = render({
      base_image: base({ update_available: true, latest_version: 2 }),
      pending_base_image: base({ id: 'v2', version: 2 }),
    })
    expect(w.text()).toContain('→ v2')
    expect(w.find('[data-testid="image-version-update"]').exists()).toBe(false)
  })

  it('falls back to the legacy base image name', () => {
    expect(render({ base_image_name: 'Legacy base' }).text()).toContain('Legacy base')
    expect(render({}).text()).toContain('—')
  })
})
