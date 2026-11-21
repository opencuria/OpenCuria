import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import FileUploadZone from './FileUploadZone.vue'
import { listWorkspaceFiles } from '@/services/socket'
import { resetWorkspaceFiles, workspaceFiles } from '@/services/workspaceFiles.fixtures'
import { FileTransferError } from '@opencuria/client/files'

vi.mock('@/services/socket', () => ({
  listWorkspaceFiles: vi.fn(async () => ({ entries: [] })),
  findWorkspaceFiles: vi.fn(),
}))
vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const WS_ID = 'ws-1'
const TARGET = '/workspace/docs'
const files = workspaceFiles()

/** Build a FileList-like with real File objects (jsdom has File but no DataTransfer). */
function makeFileList(list: File[]): FileList {
  const fileList = {
    length: list.length,
    item: (index: number) => list[index] ?? null,
  } as unknown as FileList & { [index: number]: File }
  for (let i = 0; i < list.length; i++) fileList[i] = list[i]!
  return fileList as FileList
}

/** Dispatch a real DOM drop event carrying our files onto the zone element. */
function dispatchDrop(el: HTMLElement, list: FileList): void {
  const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent
  Object.defineProperty(event, 'dataTransfer', { value: { files: list } })
  el.dispatchEvent(event)
}

function mountZone() {
  return mount(FileUploadZone, {
    props: { workspaceId: WS_ID, targetPath: TARGET },
    slots: { default: '<div data-testid="child" />' },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  resetWorkspaceFiles()
})

describe('FileUploadZone', () => {
  it('uploads dropped files to the target directory, replacing existing ones', async () => {
    const first = new File(['aaa'], 'a.txt')
    const second = new File(['bbb'], 'b.txt')
    let finish!: () => void
    files.upload.mockImplementationOnce(
      (_workspaceId, input) =>
        new Promise((resolve) => {
          finish = () => resolve({ path: `${input.directory}/${input.filename}`, size: 3 })
        }),
    )
    const wrapper = mountZone()

    dispatchDrop(wrapper.element as HTMLElement, makeFileList([first, second]))
    await flushPromises()
    expect(wrapper.text()).toContain('Uploading…')
    expect(files.upload.mock.calls.map(([workspaceId, input]) => [workspaceId, input])).toEqual([
      [WS_ID, { directory: TARGET, filename: 'a.txt', body: first, conflict: 'replace' }],
      [WS_ID, { directory: TARGET, filename: 'b.txt', body: second, conflict: 'replace' }],
    ])

    finish()
    await flushPromises()
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('Uploading…')
    expect(listWorkspaceFiles).toHaveBeenCalledWith(WS_ID, TARGET, expect.any(AbortSignal))
  })

  it('skips files above 500 MB with a visible toast and never reads them', async () => {
    const big = new File(['x'], 'big.bin')
    Object.defineProperty(big, 'size', { value: 500 * 1024 * 1024 + 1 })
    const arrayBuffer = vi.spyOn(big, 'arrayBuffer')
    const wrapper = mountZone()

    dispatchDrop(wrapper.element as HTMLElement, makeFileList([big]))
    await flushPromises()

    expect(files.upload).not.toHaveBeenCalled()
    expect(arrayBuffer).not.toHaveBeenCalled()
    const { toast } = await import('vue-sonner')
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Upload failed',
      expect.objectContaining({ description: expect.stringContaining('500 MB') }),
    )
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
  })

  it('reports a failed upload and clears the overlay', async () => {
    files.upload.mockRejectedValueOnce(new FileTransferError('Disk full', 'http_507'))
    const wrapper = mountZone()

    dispatchDrop(wrapper.element as HTMLElement, makeFileList([new File(['hi'], 'a.txt')]))
    await flushPromises()

    const { toast } = await import('vue-sonner')
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Upload failed',
      expect.objectContaining({ description: 'Disk full' }),
    )
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('Uploading…')
  })
})
