import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import WorkspaceFileIcon from './WorkspaceFileIcon.vue'

describe('WorkspaceFileIcon', () => {
  it('renders a colored TS icon for TypeScript files', () => {
    const wrapper = mount(WorkspaceFileIcon, { props: { path: '/workspace/src/main.ts' } })
    const img = wrapper.get('[data-testid="workspace-file-icon"]')
    expect(img.attributes('data-icon')).toBe('typescript')
    // Small SVGs are inlined as data URLs by Vite; the key assertion is data-icon.
    expect(img.attributes('src')).toMatch(/^(data:|.*\.svg)/)
  })

  it('resolves exact names (Dockerfile) and folder open variants', () => {
    const docker = mount(WorkspaceFileIcon, { props: { name: 'Dockerfile' } })
    expect(docker.get('[data-testid="workspace-file-icon"]').attributes('data-icon')).toBe(
      'docker',
    )

    const folder = mount(WorkspaceFileIcon, {
      props: { path: '/workspace/src', directory: true },
    })
    expect(folder.get('[data-testid="workspace-file-icon"]').attributes('data-icon')).toBe(
      'folder-src',
    )

    const open = mount(WorkspaceFileIcon, {
      props: { path: '/workspace/src', directory: true, expanded: true },
    })
    expect(open.get('[data-testid="workspace-file-icon"]').attributes('data-icon')).toBe(
      'folder-src-open',
    )
  })

  it('falls back to the generic file icon for unknown types', () => {
    const wrapper = mount(WorkspaceFileIcon, { props: { name: 'mystery.unknownext' } })
    const img = wrapper.get('[data-testid="workspace-file-icon"]')
    expect(img.attributes('data-icon')).toBe('file')
    expect(img.attributes('src')).toMatch(/^(data:|.*\.svg)/)
  })

  it('honors the size prop', () => {
    const wrapper = mount(WorkspaceFileIcon, { props: { path: '/workspace/a.ts', size: 20 } })
    const img = wrapper.get('[data-testid="workspace-file-icon"]')
    expect(img.attributes('width')).toBe('20')
    expect(img.attributes('height')).toBe('20')
  })
})
