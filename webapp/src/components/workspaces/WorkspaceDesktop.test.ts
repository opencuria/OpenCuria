import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspaceDesktop from './WorkspaceDesktop.vue'

const workspacesApiMocks = vi.hoisted(() => ({
  getDesktopStatus: vi.fn(),
  startDesktop: vi.fn(),
  stopDesktop: vi.fn(),
  readDesktopClipboard: vi.fn(),
  writeDesktopClipboard: vi.fn(),
}))

const desktopStore = {
  isOpen: true,
  isMinimized: false,
  isConnecting: false,
  isConnected: false,
  proxyUrl: null as string | null,
  workspaceId: null as string | null,
  setConnecting: vi.fn(),
  setConnected: vi.fn(),
  setDisconnected: vi.fn(),
  reset: vi.fn(),
  close: vi.fn(),
  minimize: vi.fn(),
}

vi.mock('@/stores/desktop', () => ({
  useDesktopStore: () => desktopStore,
}))

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({
    success: vi.fn(),
    error: vi.fn(),
  }),
}))

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: workspacesApiMocks.getDesktopStatus,
  startDesktop: workspacesApiMocks.startDesktop,
  stopDesktop: workspacesApiMocks.stopDesktop,
  readDesktopClipboard: workspacesApiMocks.readDesktopClipboard,
  writeDesktopClipboard: workspacesApiMocks.writeDesktopClipboard,
}))

vi.mock('@/services/socket', () => ({
  onEvent: () => () => {},
}))

vi.mock('@/services/config', () => ({
  getConfig: () => ({
    wsBaseUrl: '',
  }),
}))

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe('WorkspaceDesktop', () => {
  beforeEach(() => {
    desktopStore.isOpen = true
    desktopStore.isMinimized = false
    desktopStore.isConnecting = false
    desktopStore.isConnected = false
    desktopStore.proxyUrl = null
    desktopStore.workspaceId = null
    desktopStore.setConnecting.mockReset()
    desktopStore.setConnected.mockReset()
    desktopStore.setDisconnected.mockReset()
    desktopStore.reset.mockReset()
    desktopStore.close.mockReset()
    desktopStore.minimize.mockReset()
    workspacesApiMocks.getDesktopStatus.mockReset()
    workspacesApiMocks.startDesktop.mockReset()
    workspacesApiMocks.stopDesktop.mockReset()
    workspacesApiMocks.readDesktopClipboard.mockReset()
    workspacesApiMocks.writeDesktopClipboard.mockReset()
    workspacesApiMocks.getDesktopStatus.mockResolvedValue({ active: false, proxy_url: null })
    globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver
  })

  it('starts the desktop with the selected command id', async () => {
    shallowMount(WorkspaceDesktop, {
      props: {
        workspaceId: 'workspace-1',
        selectedDesktopStartCommandId: 'cmd-2',
      },
    })

    await flushPromises()

    expect(workspacesApiMocks.startDesktop).toHaveBeenCalledWith('workspace-1', 'cmd-2')
  })
})
