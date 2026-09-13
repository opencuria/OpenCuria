import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import FileViewer from './FileViewer.vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  sendFilesRead: vi.fn(),
  sendFilesDownload: vi.fn(),
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

function mountViewer() {
  return mount(FileViewer, { props: { workspaceId: 'ws-1' } })
}

describe('FileViewer error/retry and truncation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the error block with a retry button and retries on click', async () => {
    const store = useFileExplorerStore()
    store.selectedPath = '/workspace/a.txt'
    store.isLoadingContent = false
    store.contentError = 'File read timed out. Please retry.'

    const wrapper = mountViewer()
    expect(wrapper.find('[data-testid="file-viewer-error"]').exists()).toBe(true)
    const retry = wrapper.find('[data-testid="file-viewer-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')

    expect(vi.mocked(sendFilesRead)).toHaveBeenCalledWith(
      'ws-1',
      expect.any(String),
      '/workspace/a.txt',
    )
    await nextTick()
  })

  it('shows a dynamic "Showing first X of Y" truncation line', async () => {
    const store = useFileExplorerStore()
    // 16 base64 chars -> previewBytes 12; total size 5 MiB.
    store.setFileContent('/workspace/big.txt', btoa('hello world'), 5 * 1024 * 1024, true)
    expect(store.viewingFile?.truncated).toBe(true)

    const wrapper = mountViewer()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('Showing first 12 B of 5.0 MB')
    // Guard against a hardcoded size label: the shown-bytes half must be dynamic.
    expect(text).not.toContain('Showing first 5 MB')
  })
})
